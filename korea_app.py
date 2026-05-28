import json
import os
import threading
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from backtesting.test import SMA
from pykrx import stock

try:
    import websocket
except ImportError:
    websocket = None


KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_APPROVAL_URL = f"{KIS_BASE_URL}/oauth2/Approval"
KIS_REAL_WS_URL = "ws://ops.koreainvestment.com:21000"
REALTIME_TR_ID = "H0STCNT0"
DEFAULT_TICKER = "005930"

LIVE_LOCK = threading.Lock()
LIVE_QUOTES: dict[str, dict] = {}
LIVE_STATUS: dict[str, dict] = {}
WS_THREADS: dict[str, threading.Thread] = {}
WS_APPS: dict[str, object] = {}


st.set_page_config(page_title="포케스트", layout="wide")


def config_value(name: str, default: str = "") -> str:
    value = os.getenv(name, "")
    if value:
        return value
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default


def normalize_ticker(value: str) -> str:
    return str(value).strip().zfill(6)


def to_float(value) -> float:
    if value is None:
        return 0.0
    text = str(value).replace(",", "").replace("%", "").strip()
    if text in {"", "-", "None", "nan"}:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def format_price(value: float) -> str:
    return f"{float(value or 0):,.0f}원"


def format_volume(value: float) -> str:
    return f"{float(value or 0):,.0f}주"


def format_trade_time(value: str) -> str:
    text = str(value or "")
    if len(text) >= 6:
        return f"{text[:2]}:{text[2:4]}:{text[4:6]}"
    return "-"


@st.cache_data(ttl=60 * 60 * 23, show_spinner=False)
def issue_approval_key(app_key: str, app_secret: str) -> str:
    response = requests.post(
        KIS_APPROVAL_URL,
        headers={"content-type": "application/json; charset=utf-8"},
        json={
            "grant_type": "client_credentials",
            "appkey": app_key,
            "secretkey": app_secret,
        },
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    approval_key = payload.get("approval_key")
    if not approval_key:
        raise RuntimeError(payload.get("msg1") or payload.get("error_description") or "Approval Key 발급 실패")
    return approval_key


@st.cache_data(ttl=60 * 60, show_spinner=False)
def ticker_name(ticker: str) -> str:
    try:
        return stock.get_market_ticker_name(ticker) or ticker
    except Exception:
        return ticker


def set_status(ticker: str, state: str, message: str = "") -> None:
    with LIVE_LOCK:
        LIVE_STATUS[ticker] = {
            "state": state,
            "message": message,
            "updated_at": datetime.now().strftime("%H:%M:%S"),
        }


def get_status(ticker: str) -> dict:
    with LIVE_LOCK:
        return LIVE_STATUS.get(ticker, {"state": "연결 전", "message": "", "updated_at": ""}).copy()


def set_quote(ticker: str, quote: dict) -> None:
    with LIVE_LOCK:
        LIVE_QUOTES[ticker] = {
            **quote,
            "received_at": datetime.now().strftime("%H:%M:%S"),
        }


def get_quote(ticker: str) -> dict:
    with LIVE_LOCK:
        return LIVE_QUOTES.get(ticker, {}).copy()


def parse_realtime_trade(message: str) -> dict | None:
    if not isinstance(message, str) or not message.startswith("0|"):
        return None

    parts = message.split("|")
    if len(parts) < 4 or parts[1] != REALTIME_TR_ID:
        return None

    fields = parts[3].split("^")
    if len(fields) < 14:
        return None

    return {
        "ticker": fields[0],
        "trade_time": fields[1],
        "price": to_float(fields[2]),
        "change_sign": fields[3],
        "change": to_float(fields[4]),
        "change_pct": to_float(fields[5]),
        "trade_volume": to_float(fields[12]),
        "acc_volume": to_float(fields[13]),
        "raw_received_at": datetime.now().isoformat(timespec="seconds"),
    }


def handle_control_message(ws, ticker: str, message: str) -> None:
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return

    header = payload.get("header", {})
    body = payload.get("body", {})
    tr_id = header.get("tr_id")

    if tr_id == "PINGPONG":
        ws.send(message)
        return

    rt_cd = str(body.get("rt_cd", ""))
    msg = body.get("msg1", "")
    if tr_id == REALTIME_TR_ID and rt_cd == "0":
        set_status(ticker, "연결 성공", msg or "실시간 체결 구독 중")
    elif rt_cd and rt_cd != "0":
        set_status(ticker, "연결 실패", msg or "WebSocket 구독 실패")


def build_subscribe_message(approval_key: str, ticker: str) -> str:
    return json.dumps(
        {
            "header": {
                "approval_key": approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": REALTIME_TR_ID,
                    "tr_key": ticker,
                }
            },
        },
        ensure_ascii=False,
    )


def websocket_worker(ticker: str, approval_key: str) -> None:
    if websocket is None:
        set_status(ticker, "연결 실패", "websocket-client 패키지가 설치되지 않았습니다.")
        return

    def on_open(ws) -> None:
        set_status(ticker, "연결 성공", "실시간 체결 구독 요청 완료")
        ws.send(build_subscribe_message(approval_key, ticker))

    def on_message(ws, message: str) -> None:
        quote = parse_realtime_trade(message)
        if quote:
            set_quote(ticker, quote)
            set_status(ticker, "연결 성공", "실시간 체결 수신 중")
            return
        handle_control_message(ws, ticker, message)

    def on_error(ws, error: Exception) -> None:
        set_status(ticker, "연결 실패", str(error))

    def on_close(ws, status_code, close_msg) -> None:
        message = close_msg or f"종료 코드 {status_code}" if status_code else "연결 종료"
        set_status(ticker, "연결 전", message)

    ws = websocket.WebSocketApp(
        KIS_REAL_WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    with LIVE_LOCK:
        WS_APPS[ticker] = ws
    ws.run_forever(ping_interval=20, ping_timeout=10)


def close_ws(ticker: str) -> None:
    with LIVE_LOCK:
        ws = WS_APPS.pop(ticker, None)
        WS_THREADS.pop(ticker, None)
    if ws:
        try:
            ws.close()
        except Exception:
            pass


def ensure_realtime_connection(ticker: str, app_key: str, app_secret: str) -> None:
    thread = WS_THREADS.get(ticker)
    if thread and thread.is_alive():
        return

    set_status(ticker, "연결 중", "Approval Key 발급 중")
    approval_key = issue_approval_key(app_key, app_secret)
    set_status(ticker, "연결 중", "실전 WebSocket 연결 중")

    thread = threading.Thread(
        target=websocket_worker,
        args=(ticker, approval_key),
        name=f"kis-realtime-{ticker}",
        daemon=True,
    )
    with LIVE_LOCK:
        WS_THREADS[ticker] = thread
    thread.start()


def render_status_badge(status: dict) -> None:
    state = status.get("state", "연결 전")
    class_name = {
        "연결 전": "idle",
        "연결 중": "connecting",
        "연결 성공": "connected",
        "연결 실패": "failed",
    }.get(state, "idle")
    st.markdown(
        f"<div class='status-pill {class_name}'>{state}</div>",
        unsafe_allow_html=True,
    )
    if status.get("message"):
        st.caption(status["message"])


def render_live_panel(ticker: str) -> None:
    quote = get_quote(ticker)
    status = get_status(ticker)

    top_left, top_right = st.columns([1, 0.28])
    with top_left:
        st.subheader(f"{ticker_name(ticker)} · {ticker}")
    with top_right:
        render_status_badge(status)

    price = quote.get("price", 0)
    change_pct = quote.get("change_pct", 0)
    acc_volume = quote.get("acc_volume", 0)
    trade_time = quote.get("trade_time", "")

    cards = st.columns(4)
    cards[0].metric("실시간 현재가", format_price(price) if price else "-")
    cards[1].metric("체결시간", format_trade_time(trade_time))
    cards[2].metric("등락률", f"{change_pct:+.2f}%" if quote else "-")
    cards[3].metric("누적거래량", format_volume(acc_volume) if quote else "-")

    if status.get("state") == "연결 실패":
        st.error(status.get("message") or "WebSocket 연결에 실패했습니다.")
    elif not quote:
        st.info("실시간 체결 데이터 수신을 기다리는 중입니다. 장 운영 시간이 아니면 값이 바로 들어오지 않을 수 있습니다.")
    else:
        st.caption(f"마지막 수신: {quote.get('received_at', '-')}")


def fetch_backtest_data(ticker: str, start: str, end: str) -> pd.DataFrame:
    data = stock.get_market_ohlcv_by_date(start, end, ticker)
    if data.empty:
        return pd.DataFrame()

    data = data.rename(
        columns={
            "시가": "Open",
            "고가": "High",
            "저가": "Low",
            "종가": "Close",
            "거래량": "Volume",
        }
    )
    return data[["Open", "High", "Low", "Close", "Volume"]].dropna()


def run_backtest(data: pd.DataFrame, cash: int, commission: float, short_ma: int, long_ma: int):
    class MovingAverageStrategy(Strategy):
        def init(self):
            self.ma_short = self.I(SMA, self.data.Close, short_ma)
            self.ma_long = self.I(SMA, self.data.Close, long_ma)

        def next(self):
            if crossover(self.ma_short, self.ma_long):
                self.buy()
            elif crossover(self.ma_long, self.ma_short):
                self.position.close()

    bt = Backtest(data, MovingAverageStrategy, cash=cash, commission=commission)
    return bt.run()


def render_backtest(ticker: str, start: str, end: str, cash: int, commission: float, short_ma: int, long_ma: int) -> None:
    data = fetch_backtest_data(ticker, start, end)
    if data.empty or len(data) < long_ma:
        st.error("백테스트 데이터가 부족합니다. 기간을 더 길게 설정하세요.")
        return

    result = run_backtest(data, cash, commission, short_ma, long_ma)
    win_rate = 0 if pd.isna(result["Win Rate [%]"]) else result["Win Rate [%]"]

    metric_cols = st.columns(4)
    metric_cols[0].metric("총 수익률", f"{result['Return [%]']:.2f}%")
    metric_cols[1].metric("승률", f"{win_rate:.2f}%")
    metric_cols[2].metric("최대 낙폭", f"{result['Max. Drawdown [%]']:.2f}%")
    metric_cols[3].metric("거래 횟수", int(result["# Trades"]))

    data[f"MA{short_ma}"] = data["Close"].rolling(short_ma).mean()
    data[f"MA{long_ma}"] = data["Close"].rolling(long_ma).mean()

    price_fig = go.Figure()
    price_fig.add_trace(
        go.Candlestick(
            x=data.index,
            open=data["Open"],
            high=data["High"],
            low=data["Low"],
            close=data["Close"],
            name="캔들",
        )
    )
    price_fig.add_trace(go.Scatter(x=data.index, y=data[f"MA{short_ma}"], mode="lines", name=f"{short_ma}일선"))
    price_fig.add_trace(go.Scatter(x=data.index, y=data[f"MA{long_ma}"], mode="lines", name=f"{long_ma}일선"))
    price_fig.update_layout(height=520, template="plotly_white", xaxis_rangeslider_visible=False, hovermode="x unified")
    st.plotly_chart(price_fig, use_container_width=True)

    equity = result["_equity_curve"]["Equity"]
    equity_fig = go.Figure()
    equity_fig.add_trace(go.Scatter(x=equity.index, y=equity, mode="lines", name="전략 자산"))
    equity_fig.update_layout(height=380, template="plotly_white", hovermode="x unified")
    st.plotly_chart(equity_fig, use_container_width=True)

    tab_summary, tab_trades = st.tabs(["상세 성과", "거래 내역"])
    with tab_summary:
        st.write(result)
    with tab_trades:
        st.dataframe(result["_trades"], use_container_width=True)


st.markdown(
    """
<style>
header[data-testid="stHeader"] {
    display: none;
}
.block-container {
    max-width: 1120px;
    padding: 22px 28px 80px;
}
.forecast-nav {
    display: flex;
    align-items: center;
    gap: 28px;
    height: 52px;
    border-bottom: 1px solid #eef1f5;
    margin-bottom: 26px;
}
.brand {
    display: flex;
    align-items: center;
    gap: 9px;
    font-size: 20px;
    font-weight: 850;
    color: #111827;
}
.brand-dot {
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: linear-gradient(135deg, #111827 0%, #111827 48%, #15b8a6 49%, #15b8a6 100%);
}
.nav-item {
    color: #526173;
    font-size: 14px;
    font-weight: 700;
}
.market-line {
    display: flex;
    align-items: center;
    gap: 18px;
    color: #7b8794;
    font-size: 14px;
    font-weight: 700;
    margin-bottom: 18px;
}
.live-dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 6px;
    background: #15b8a6;
}
.status-pill {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 86px;
    border-radius: 999px;
    padding: 7px 12px;
    font-size: 13px;
    font-weight: 800;
}
.status-pill.idle {
    color: #64748b;
    background: #f1f5f9;
}
.status-pill.connecting {
    color: #0369a1;
    background: #e0f2fe;
}
.status-pill.connected {
    color: #0f766e;
    background: #ccfbf1;
}
.status-pill.failed {
    color: #dc2626;
    background: #fee2e2;
}
.live-card {
    border-top: 1px solid #edf1f5;
    border-bottom: 1px solid #edf1f5;
    padding: 20px 0 24px;
    margin-bottom: 28px;
}
</style>
""",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("설정")
    ticker_input = st.text_input("종목코드", DEFAULT_TICKER)
    ticker = normalize_ticker(ticker_input)

    st.divider()
    st.subheader("백테스팅")
    start = st.text_input("시작일", "20200101")
    end = st.text_input("종료일", datetime.today().strftime("%Y%m%d"))
    cash = st.number_input("초기 자금", value=10_000_000, step=1_000_000)
    short_ma = st.number_input("단기 이동평균선", value=20, min_value=5)
    long_ma = st.number_input("장기 이동평균선", value=60, min_value=20)
    commission = st.number_input("수수료", value=0.0015, format="%.4f")
    run = st.button("백테스트 실행", use_container_width=True)

app_key = config_value("KIS_APP_KEY")
app_secret = config_value("KIS_APP_SECRET")

st.markdown(
    "<div class='forecast-nav'>"
    "<div class='brand'><span class='brand-dot'></span>포케스트</div>"
    "<div class='nav-item'>실시간체결</div>"
    "<div class='nav-item'>백테스팅</div>"
    "</div>",
    unsafe_allow_html=True,
)

st.markdown(
    "<div class='market-line'>"
    "<span><span class='live-dot'></span>한국투자증권 실전 WebSocket · 국내주식 실시간체결</span>"
    "</div>",
    unsafe_allow_html=True,
)

if not app_key or not app_secret:
    st.error("KIS_APP_KEY와 KIS_APP_SECRET이 설정되지 않았습니다.")
    st.stop()

if websocket is None:
    st.error("websocket-client 패키지가 설치되지 않았습니다. requirements.txt 반영 후 앱을 다시 배포하거나 로컬에 설치해 주세요.")
    st.stop()

previous_ticker = st.session_state.get("active_ws_ticker")
if previous_ticker and previous_ticker != ticker:
    close_ws(previous_ticker)
st.session_state["active_ws_ticker"] = ticker

try:
    ensure_realtime_connection(ticker, app_key, app_secret)
except Exception as exc:
    set_status(ticker, "연결 실패", str(exc))


@st.fragment(run_every="1s")
def realtime_fragment(selected_ticker: str) -> None:
    st.markdown("<div class='live-card'>", unsafe_allow_html=True)
    render_live_panel(selected_ticker)
    st.markdown("</div>", unsafe_allow_html=True)


realtime_fragment(ticker)

st.subheader("백테스팅")
if run:
    render_backtest(ticker, start, end, cash, commission, short_ma, long_ma)
else:
    st.info("왼쪽 사이드바에서 조건을 설정한 뒤 백테스트를 실행하세요.")
