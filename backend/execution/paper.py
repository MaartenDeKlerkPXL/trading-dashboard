"""Paper executor: live prices, fake money.

It uses the same fill rules as the backtest executor, but is fed one-minute
candles as they arrive. Orders are filled at the first price after the
decision (the open of the next minute), and stop-loss/take-profit are checked
minute by minute instead of per strategy candle.
"""

from __future__ import annotations

from .backtest import BacktestExecutor


class PaperExecutor(BacktestExecutor):
    mode = "paper"
