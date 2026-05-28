import yfinance as yf
from backtesting import Backtest, Strategy
from backtesting.lib import crossover
from backtesting.test import SMA

data = yf.download(
    "AAPL",
    start="2020-01-01",
    end="2026-01-01",
    auto_adjust=False,
    group_by="column"
)

if isinstance(data.columns, type(data.columns)) and hasattr(data.columns, "droplevel"):
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

bt = Backtest(data, MovingAverageStrategy, cash=10000, commission=0.001)

result = bt.run()
print(result)

bt.plot()