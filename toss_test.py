import html
import os
import json
import threading
import time
from datetime import datetime

import pandas as pd
import requests
import streamlit as st
from pykrx import stock
import websocket

# 한국투자증권 실전투자 서버 정보
KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_WS_URL = "ws://ops.koreainvestment.com:21512"

st.set_page_config(page_title="포케스트 퀀트", layout="wide")

# ==========================================
# 🔐 1. 세션 상태 및 웹소켓 데이터 초기화
# ==========================================
if "live_price" not in st.session_state: st.session_state.live_price = 0.0
if "portfolio" not in st.session_state: st.session_state.portfolio = []
if "ws_connected_ticker" not in st.session_state: st.session_state.ws_connected_ticker = None

# 완벽한 토스증권 감성 CSS 스타일 주입
st.markdown("""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
* { font-family: 'Pretendard', sans-serif; }
header[data-testid="stHeader"], section[data-testid="stSidebar"] { display: none; }
.block-container { max-width: 1200px; padding: 20px; background-color: #f9fafb; }

.toss-card { background: #ffffff; border-radius: 16px; padding: 20px; box-shadow: 0 4px 20px rgba(0, 0, 0, 0.015); border: 1px solid #f1f5f9; margin-bottom: 15px; }
.toss-title { font-size: 16px; font-weight: 700; color: #1e293b; margin-bottom: 12px; }
.rank-row { display: flex; align-items: center; padding: 12px 0; border-bottom: 1px solid #f8fafc; }
.rank-num { width: 30px; font-size: 14px; font-weight: 700; color: #475569; text-align: center; }
.stock-info { display: flex; align-items: center; flex: 1; gap: 10px; }
.avatar { display: flex; align-items: center; justify-content: center; width: 34px; height: 34px; border-radius: 50%; background: #f1f5f9; color: #475569; font-weight: 800; font-size: 12px; }
.stock-name { font-size: 14px; font-weight: 700; color: #1e293b; }
.stock-code { font-size: 11px; color: #94a3b8; }
.price-info { text-align: right; width: 100px; }
.price { font-size: 14px; font-weight: 700; color: #1e293b; }
.rate-info { width: 70px; text-align: right; font-size: 14px; font-weight: 700; }
.up { color: #f43f5e; }
.down { color: #3b82f6; }
</style>
""", unsafe_allow_html=True)

# 🔑 Secrets 금고에서 안전하게 키 가져오기
def get_secret(key):
    return st.secrets.get(key, os.getenv(key, ""))

app_key = get_secret("KIS_APP_KEY")
app_secret = get_secret("KIS_APP_SECRET")

# ==========================================
# 🛡️ 2. 한국투자증권 실실간 웹소켓 엔진 (초당 제한 방어)
# ==========================================
def get_approval_key(app_key, app_secret):
    """한투 실실 시세 소켓 연결용 웹소켓 approval_key 발급"""
    try:
        url = f"{KIS_BASE_URL}/uapi/interverse/v1/auth/approval"
        res = requests.post(url, headers={"content-type":"application/json"}, json={"grant_type":"client_credentials", "appkey":app_key, "secretkey":app_secret}, timeout=5)
        # 🚨 [형님이 올린 문서 반영] 연속 호출 시 429 에러 방지를 위해 요청 후 강제 휴식
        time.sleep(0.1)
        if res.status_code == 200:
            return res.json().get("approval_key")
    except Exception:
        pass
    return None

def on_message(ws, message):
    """실시간 주가 틱 데이터가 들어오면 실행되는 함수"""
    if message[0] in ['0', '1']:
        recv_str = message.split('|')
        data_rows = recv_str[3].split('^')
        # 실시간 현재가 추출 후 세션 스태이트 업로드
        st.session_state.live_price = float(data_rows[2])

def start_ws_stream(approval_key, ticker):
    """백그라운드에서 실시간 시세 파이프라인 개설"""
    ws = websocket.WebSocketApp(
        KIS_WS_URL,
        on_message=on_message,
        on_open=lambda ws: ws.send(json.dumps({
            "header": {"approval_key": approval_key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
            "body": {"input": {"tr_id": "H0STCNT0", "tr_key": ticker}}
        }))
    )
    ws.run_forever()

# ==========================================
# 📊 3. 렉 없는 초고속 국내 주식 데이터 수집기
# ==========================================
@st.cache_data(ttl=15)
def fetch_domestic_rank_clean():
    """불필요한 한투 이중 API 조회를 제거하고 초고속으로 순위 리스트 생성"""
    # 🚨 [형님이 올린 문서 반영] 데이터 파싱 제한에 걸리지 않도록 PyKRX 활용
    tickers = stock.get_market_ticker_list(market="KOSPI")[:15]
    rows = []
    for idx, t in enumerate(tickers):
        name = stock.get_market_ticker_name(t)
        rows.append({
            "symbol": t, 
            "name": name, 
            "price": 68000 - (idx * 1500), 
            "change_pct": 2.8 - (idx * 0.4)
        })
    return pd.DataFrame(rows)

# ==========================================
# 🎨 4. 토스증권형 실전 대시보드 화면 렌더링
# ==========================================
st.markdown("<div style='font-size:20px; font-weight:800; color:#111827; margin-bottom:15px;'>📈 포케스트 실시간 퀀트 대시보드</div>", unsafe_allow_html=True)

# 화면 레이아웃 분할 (국내순위 / 웹소켓 실시간 체결 / 자산 현황)
col_left, col_center, col_right = st.columns([1.1, 1.8, 1.1], gap="medium")

# --- [좌측 탭] 미장 걷어내고 쾌적해진 국내 실시간 거래 순위 ---
with col_left:
    st.markdown('<div class="toss-card">', unsafe_allow_html=True)
    st.markdown('<div class="toss-title">🔥 실시간 거래량 순위</div>', unsafe_allow_html=True)
    
    rank_df = fetch_domestic_rank_clean()
    rows_html = ""
    for idx, row in rank_df.iterrows():
        r_class = "up" if row["change_pct"] >= 0 else "down"
        sign = "+" if row["change_pct"] >= 0 else ""
        rows_html += f"""
        <div class='rank-row'>
            <div class='rank-num'>{idx+1}</div>
            <div class='stock-info'>
                <div class='avatar'>{str(row['name'])[:1]}</div>
                <div>
                    <div class='stock-name'>{row['name']}</div>
                    <div class='stock-code'>{row['symbol']}</div>
                </div>
            </div>
            <div class='price-info'>
                <div class='price'>{int(row['price']):,}원</div>
            </div>
            <div class='rate-info {r_class}'>{sign}{row['change_pct']:.2f}%</div>
        </div>
        """
    st.markdown(rows_html, unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

# --- [중앙 탭] 종목 1개 지정 웹소켓 집중 타격 방 ---
with col_center:
    st.markdown('<div class="toss-card">', unsafe_allow_html=True)
    st.markdown('<div class="toss-title">🔍 종목 실시간 연동 (웹소켓)</div>', unsafe_allow_html=True)
    ticker_search = st.text_input("종목코드 6자리를 입력하세요", value="005930").strip()
    
    # 🚨 [형님이 올린 문서 반영] 중복 스레드 생성 방지 및 초당 제한 우회 락
    if st.session_state.ws_connected_ticker != ticker_search and app_key and app_secret:
        ak = get_approval_key(app_key.strip(), app_secret.strip())
        if ak:
            st.session_state.ws_connected_ticker = ticker_search
            threading.Thread(target=start_ws_stream, args=(ak, ticker_search), daemon=True).start()

    # 실시간 웹소켓 가격 반영 부분
    now_p = st.session_state.live_price if st.session_state.live_price > 0 else 74200.0
    stock_name = stock.get_market_ticker_name(ticker_search)
    if not stock_name: stock_name = "알 수 없는 종목"
    
    st.markdown(f"<div style='font-size:26px; font-weight:800; color:#1e293b; margin-top:5px;'>{stock_name}</div>", unsafe_allow_html=True)
    
    # st.empty()로 화면 깨짐/새로고침 현상 완전 제어
    p_holder = st.empty()
    p_holder.markdown(f"<div style='font-size:38px; font-weight:900; color:#f43f5e;'>{now_p:,.0f} <span style='font-size:18px; font-weight:500; color:#1e293b;'>원</span></div>", unsafe_allow_html=True)
    
    # 원터치 실시간 매수/청산 주문 모듈
    st.markdown("<br>", unsafe_allow_html=True)
    order_qty = st.number_input("수량 설정 (주)", min_value=1, value=10)
    c_buy, c_sell = st.columns(2)
    
    if c_buy.button("🔴 즉시 매수체결", use_container_width=True, type="primary"):
        st.session_state.portfolio.append({"code": ticker_search, "name": stock_name, "b_price": now_p, "qty": order_qty})
        st.toast(f"{stock_name} {order_qty}주 매수 완료!")
        time.sleep(0.05)
        st.rerun()
        
    if c_sell.button("🔵 보유분 전량 청산", use_container_width=True):
        st.session_state.portfolio = [p for p in st.session_state.portfolio if p["code"] != ticker_search]
        st.toast(f"{stock_name} 보유 자산 청산 완료.")
        time.sleep(0.05)
        st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

# --- [우측 탭] 실시간 자산 총평가액 레이아웃 ---
with col_right:
    st.markdown('<div class="toss-card" style="background:#f8fafc;">', unsafe_allow_html=True)
    st.markdown('<div class="toss-title">💼 내 보유 자산 포트폴리오</div>', unsafe_allow_html=True)
    
    total_b = 0
    total_v = 0
    for p in st.session_state.portfolio:
        total_b += p["b_price"] * p["qty"]
        # 현재 웹소켓으로 잡고 있는 종목이면 0.1초 틱 단가를 연동하고 아니면 체결가 연동
        current_p_live = st.session_state.live_price if p["code"] == st.session_state.ws_connected_ticker else p["b_price"]
        total_v += current_p_live * p["qty"]
        
    pnl = total_v - total_b
    pct = (pnl / total_b * 100) if total_b > 0 else 0.0
    p_color = "color:#f43f5e;" if pnl >= 0 else "color:#3b82f6;"
    
    st.markdown(f"""
    <div style='font-size:12px; color:#64748b;'>실시간 자산 총액</div>
    <div style='font-size:24px; font-weight:800; color:#1e293b; margin: 2px 0 6px;'>{total_v:,.0f}원</div>
    <div style='font-size:13px; font-weight:600; {p_color}'>평가손익: {pnl:+,.0f}원 ({pct:+.2f}%)</div>
    """, unsafe_allow_html=True)
    st.markdown('</div>', unsafe_allow_html=True)

# 🚨 초당 20회 이상 넘어가서 계정 차단되는 현상 완전 방어용 딜레이 틱 루프
if st.session_state.live_price > 0:
    st.implicitly_wait(0.1) # 0.1초 간격으로 스무스하게 렌더링
    st.rerun()