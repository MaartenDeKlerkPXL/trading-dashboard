"""Live executor: real orders at the broker.

It makes exactly the same decisions as the paper executor (same runner, same risk rules,
same position sizing), but it never simulates a fill or an exit. Opening and closing
orders become intents in `outbox`; the live engine sends them to the broker, which fills
them and holds the stop-loss and take-profit itself.
"""

from __future__ import annotations

from ..strategies.base import Bar
from .backtest import BacktestExecutor
from .base import OrderRequest
from .events import nl, nl_lots


class LiveExecutor(BacktestExecutor):
    mode = "live"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outbox: list[dict] = []

    # The broker charges swap and executes stop-loss/take-profit: nothing to simulate.
    def _charge_financing(self, bar: Bar, to_eur: float) -> None:
        return

    def _check_exits(self, bar: Bar, to_eur: float) -> None:
        return

    def _queued(self, kind: str) -> bool:
        return any(o["kind"] == kind for o in self.outbox)

    def _execute(self, req: OrderRequest, bar: Bar, to_eur: float) -> None:
        if req.kind == "modify":
            if self._check_modify(req, bar.ts):
                self.outbox = [o for o in self.outbox if o["kind"] != "modify"]   # only the latest counts
                self.outbox.append({"kind": "modify", "client_id": req.client_id, "stop_loss": req.stop_loss,
                                    "take_profit": req.take_profit, "reason": req.reason, "ts": bar.ts,
                                    "position_id": getattr(self.pos, "position_id", None)})
                # Mirror it locally so later decisions in this round see the new level.
                if req.stop_loss is not None:
                    self.pos.stop_loss = req.stop_loss
                if req.take_profit is not None:
                    self.pos.take_profit = req.take_profit
            return
        if req.kind == "close":
            if self.pos is None:
                self.log.add(bar.ts, "info", "Sluitorder genegeerd: er is geen open positie.", client_id=req.client_id)
                return
            self._queue_close(bar.ts, req.client_id, req.reason or "Signaal")
            return
        if self._queued("close"):
            # A reversal: the position is being closed by the order queued just before this one.
            saved, self.pos = self.pos, None
            try:
                super()._execute(req, bar, to_eur)
            finally:
                self.pos = saved
            return
        super()._execute(req, bar, to_eur)

    def _open(self, req: OrderRequest, bar: Bar, to_eur: float) -> None:
        if self._queued("open"):
            self.log.add(bar.ts, "skip", "Order overgeslagen: er wacht al een order op de broker.",
                         client_id=req.client_id)
            return
        plan = self._plan_open(req, bar, to_eur)
        if plan is None:
            return
        est_price, lots, take_profit = plan
        self.outbox.append({"kind": "open", "client_id": req.client_id, "side": req.side, "lots": lots,
                            "stop_loss": req.stop_loss, "take_profit": take_profit, "est_price": est_price,
                            "signal_ts": req.signal_ts, "reason": req.reason, "ts": bar.ts})
        d = self.instrument.digits
        self.log.add(bar.ts, "order", f"Naar de broker: {'long' if req.side == 'long' else 'short'} "
                     f"{nl_lots(lots)} lot, stop-loss {nl(req.stop_loss, d)}"
                     + (f", take-profit {nl(take_profit, d)}" if take_profit is not None else ""),
                     client_id=req.client_id)

    def _queue_close(self, ts: int, client_id: str, reason: str) -> None:
        if self.pos is None or self._queued("close"):
            return
        self.outbox.append({"kind": "close", "client_id": client_id, "reason": reason, "ts": ts,
                            "position_id": getattr(self.pos, "position_id", None)})
        self.log.add(ts, "order", f"Naar de broker: positie sluiten ({reason})", client_id=client_id)

    def _close(self, ts: int, price: float, reason: str, to_eur: float, market: bool) -> None:
        if self.pos is not None:
            self._queue_close(ts, f"{self.pos.client_id}:{reason}", reason)

    def close_all(self, bar: Bar, to_eur: float, reason: str) -> None:
        self._close(bar.ts, bar.close, reason, to_eur, market=True)

    def finish(self, last: Bar, to_eur: float) -> None:
        """Orders decided on the last closed candle go out at the latest price."""
        pending, self.pending = self.pending, []
        bar = Bar(last.ts + 60, last.close, last.close, last.close, last.close, 0.0)
        for req in pending:
            self._execute(req, bar, to_eur)
