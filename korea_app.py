import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from pykrx import stock
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from backtesting.test import SMA

st.set_page_config(page_title="한국주식 백테스팅", layout="centered")

st.markdown("""
<style>
.main {
    background-color: #ffffff;
}
.block-container {
    max-width: 1100px;
    padding-top: 2rem;
}
.metric-card {
    background: #f8fafc;
    border: 1px solid #e5e7eb;
    border-radius: 16px;
    padding: 20px;
    text-align: center;
}
.metric-title {
    font-size: 14px;
    color: #64748b;
    margin-bottom: 8px;
}
.metric-value {
    font-size: 28px;
    font-weight: 700;
    color: #111827;
}
.section-title {
    font-size: 22px;
    font-weight: 700;
    margin-top: 30px;
    margin-bottom: 15px;
}
</style>
""", unsafe_allow_html=True)

st.title("한국주식 백테스팅 대시보드")
st.caption("KRX 데이터를 기반으로 이동평균선 전략을 테스트합니다.")

with st.sidebar:
    st.header("설정")

    ticker = st.text_input("종목 코드", "005930")
    start = st.text_input("시작일", "20200101")
    end = st.text_input("종료일", "20251231")
    cash = st.number_input("초기 자금", value=10000000, step=1000000)
    short_ma = st.number_input("단기 이동평균선", value=20, min_value=5)
    long_ma = st.number_input("장기 이동평균선", value=60, min_value=20)
    commission = st.number_input("수수료", value=0.0015, format="%.4f")

    run = st.button("백테스트 실행", use_container_width=True)

if run:
    data = stock.get_market_ohlcv_by_date(start, end, ticker)

    data = data.rename(columns={
        "시가": "Open",
        "고가": "High",
        "저가": "Low",
        "종가": "Close",
        "거래량": "Volume"
    })

    data = data[["Open", "High", "Low", "Close", "Volume"]].dropna()

    if len(data) < long_ma:
        st.error("데이터가 부족합니다. 기간을 더 길게 설정하세요.")
        st.stop()

    class MovingAverageStrategy(Strategy):
        def init(self):
            self.ma_short = self.I(SMA, self.data.Close, short_ma)
            self.ma_long = self.I(SMA, self.data.Close, long_ma)

        def next(self):
            if crossover(self.ma_short, self.ma_long):
                self.buy()
            elif crossover(self.ma_long, self.ma_short):
                self.position.close()

    bt = Backtest(
        data,
        MovingAverageStrategy,
        cash=cash,
        commission=commission
    )

    result = bt.run()

    win_rate = result["Win Rate [%]"]
    if pd.isna(win_rate):
        win_rate = 0

    st.markdown('<div class="section-title">핵심 성과</div>', unsafe_allow_html=True)

    c1, c2, c3, c4 = st.columns(4)

    c1.markdown(f"""
    <div class="metric-card">
        <div class="metric-title">총 수익률</div>
        <div class="metric-value">{result['Return [%]']:.2f}%</div>
    </div>
    """, unsafe_allow_html=True)

    c2.markdown(f"""
    <div class="metric-card">
        <div class="metric-title">승률</div>
        <div class="metric-value">{win_rate:.2f}%</div>
    </div>
    """, unsafe_allow_html=True)

    c3.markdown(f"""
    <div class="metric-card">
        <div class="metric-title">최대 낙폭</div>
        <div class="metric-value">{result['Max. Drawdown [%]']:.2f}%</div>
    </div>
    """, unsafe_allow_html=True)

    c4.markdown(f"""
    <div class="metric-card">
        <div class="metric-title">거래 횟수</div>
        <div class="metric-value">{int(result['# Trades'])}</div>
    </div>
    """, unsafe_allow_html=True)

    data[f"MA{short_ma}"] = data["Close"].rolling(short_ma).mean()
    data[f"MA{long_ma}"] = data["Close"].rolling(long_ma).mean()

    st.markdown('<div class="section-title">주가 차트</div>', unsafe_allow_html=True)

    fig = go.Figure()

    fig.add_trace(go.Candlestick(
        x=data.index,
        open=data["Open"],
        high=data["High"],
        low=data["Low"],
        close=data["Close"],
        name="캔들"
    ))

    fig.add_trace(go.Scatter(
        x=data.index,
        y=data[f"MA{short_ma}"],
        mode="lines",
        name=f"{short_ma}일 이동평균선"
    ))

    fig.add_trace(go.Scatter(
        x=data.index,
        y=data[f"MA{long_ma}"],
        mode="lines",
        name=f"{long_ma}일 이동평균선"
    ))

    fig.update_layout(
        height=520,
        margin=dict(l=20, r=20, t=40, b=20),
        xaxis_title="날짜",
        yaxis_title="가격",
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        template="plotly_white"
    )

    st.plotly_chart(fig, use_container_width=True)

    st.markdown('<div class="section-title">자산 변화</div>', unsafe_allow_html=True)

    equity = result["_equity_curve"]["Equity"]

    equity_fig = go.Figure()
    equity_fig.add_trace(go.Scatter(
        x=equity.index,
        y=equity,
        mode="lines",
        name="전략 자산"
    ))

    equity_fig.update_layout(
        height=400,
        margin=dict(l=20, r=20, t=40, b=20),
        xaxis_title="날짜",
        yaxis_title="자산",
        hovermode="x unified",
        template="plotly_white"
    )

    st.plotly_chart(equity_fig, use_container_width=True)

    tab1, tab2 = st.tabs(["상세 성과", "거래 내역"])

    with tab1:
        st.write(result)

    with tab2:
        trades = result["_trades"]
        st.dataframe(trades, use_container_width=True)

else:
    st.info("왼쪽 사이드바에서 조건을 설정한 뒤 백테스트를 실행하세요.")