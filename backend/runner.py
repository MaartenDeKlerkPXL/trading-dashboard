"""Turns strategy signals into orders. Shared by backtest, paper and live trading."""

from __future__ import annotations

from .execution.base import BrokerExecutor, OrderRequest
from .execution.events import EventLog, nl
from .strategies.base import Bar, History, Strategy


class Runner:
    def __init__(self, strategy: Strategy, executor: BrokerExecutor, symbol: str, log: EventLog, digits: int = 5):
        self.strategy = strategy
        self.executor = executor
        self.symbol = symbol
        self.log = log
        self.digits = digits
        self.warmup = strategy.warmup()

    def on_closed_bar(self, bars: list[Bar], count: int) -> None:
        """Called after bars[count - 1] has closed and the executor has processed it."""
        if count < self.warmup:
            return
        history = History(bars, count)
        position = self.executor.position()
        signal = self.strategy.on_bar(history, position)
        if signal is None:
            return

        ts = bars[count - 1].ts
        prefix = f"{self.strategy.key()}:{self.symbol}:{ts}"
        fmt = lambda v: "—" if v is None else nl(v, self.digits)  # noqa: E731
        self.log.add(
            ts, "signal",
            f"Signaal {signal.action}" + (f" — {signal.reason}" if signal.reason else "")
            + (f" (stop-loss {fmt(signal.stop_loss)}, take-profit {fmt(signal.take_profit)})"
               if signal.action != "flat" else ""),
            action=signal.action, stop_loss=signal.stop_loss, take_profit=signal.take_profit,
        )

        requests: list[OrderRequest] = []
        if signal.action == "flat" or (position.side and position.side != signal.action):
            if position.side:
                requests.append(OrderRequest("close", f"{prefix}:close", ts, reason="Signaal"))
        if signal.action in ("long", "short") and position.side != signal.action:
            close = bars[count - 1].close
            sl = signal.stop_loss
            if sl is None:
                self.log.add(ts, "skip", "Signaal zonder stop-loss: niet uitgevoerd (een stop-loss is verplicht).")
            elif (signal.action == "long" and sl >= close) or (signal.action == "short" and sl <= close):
                self.log.add(ts, "skip", "Stop-loss ligt aan de verkeerde kant van de koers: niet uitgevoerd.")
            else:
                requests.append(OrderRequest(
                    "open", f"{prefix}:open", ts, side=signal.action, stop_loss=sl,
                    take_profit=signal.take_profit, risk_fraction=signal.size, reason=signal.reason,
                ))
        if requests:
            for r in requests:
                self.log.add(ts, "order", f"Order: {'sluiten' if r.kind == 'close' else 'openen ' + r.side}"
                             " op de opening van de volgende candle", client_id=r.client_id)
            self.executor.submit(requests)
