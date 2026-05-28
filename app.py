import streamlit as st
import yfinance as yf
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from backtesting.test import SMA

st.set_page_config(page_title="백테스팅 프로그램", layout="wide")

st.title("주식 백테스팅 프로그램")

ticker = st.text_input("종목 코드", "AAPL")

start_date = st.date_input("시작 날짜", value=None)
end_date = st.date_input("종료 날짜", value=None)

cash = st.number_input("초기 자금", value=10000)

if st.button("백테스트 실행"):

    data = yf.download(
        ticker,
        start="2020-01-01",
        end="2026-01-01",
        auto_adjust=False,
        group_by="column"
    )

    if data.columns.nlevels > 1:
        data.columns = data.columns.droplevel(1)

    data = data[["Open", "High", "Low", "Close", "Volume"]]
    data = data.dropna()

    class MovingAverageStrategy(Strategy):

        def init(self):
            self.ma20 = self.I(SMA, self.data.Close, 20)
            self.ma60 = self.I(SMA, self.data.Close, 60)

        def next(self):

            if crossover(self.ma20, self.ma60):
                self.buy()

            elif crossover(self.ma60, self.ma20):
                self.position.close()

    bt = Backtest(
        data,
        MovingAverageStrategy,
        cash=cash,
        commission=0.001
    )

    result = bt.run()

    st.subheader("백테스트 결과")

    st.write(result)

    st.line_chart(result["_equity_curve"]["Equity"])