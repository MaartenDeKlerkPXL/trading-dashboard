"""Simulated broker for backtests.

Price data are BID candles. Buying happens at the ask (bid + spread), selling
at the bid. Market fills also pay slippage. Orders submitted after bar N are
filled at the open of bar N+1. Within a bar, if both the stop-loss and the
take-profit could have been hit, the stop-loss is assumed to come first.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..data.instruments import Instrument
from ..strategies.base import Bar, Position, Side
from .base import BrokerExecutor, CostModel, OrderRequest, SizingRules, size_position
from .events import EventLog

DAY = 86400


@dataclass
class _Open:
    side: Side
    lots: float
    entry_price: float
    entry_ts: int
    entry_index: int
    stop_loss: float
    take_profit: float | None
    client_id: str
    reason: str
    equity_at_entry: float
    initial_risk_eur: float
    spread_eur: float
    slippage_eur: float
    commission_eur: float
    financing_eur: float = 0.0
    financed_day: int = 0

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1


class BacktestExecutor(BrokerExecutor):
    mode = "backtest"

    def __init__(self, instrument: Instrument, costs: CostModel, sizing: SizingRules, capital: float, log: EventLog):
        self.instrument = instrument
        self.costs = costs
        self.sizing = sizing
        self.log = log
        self.balance = float(capital)
        self.pos: _Open | None = None
        self.pending: list[OrderRequest] = []
        self.trades: list[dict] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.last_equity = float(capital)
        self.bar_index = -1
        self.bars_in_market = 0
        self.halted = False

    # ---------- BrokerExecutor ----------

    def submit(self, requests: list[OrderRequest]) -> None:
        self.pending.extend(requests)

    def position(self) -> Position:
        p = self.pos
        if p is None:
            return Position()
        return Position(p.side, p.lots, p.entry_price, p.entry_ts, p.stop_loss, p.take_profit)

    def equity(self) -> float:
        return self.last_equity

    def on_bar(self, bar: Bar, to_eur: float) -> None:
        self.bar_index += 1
        self._charge_financing(bar, to_eur)
        pending, self.pending = self.pending, []
        for req in pending:
            self._execute(req, bar, to_eur)
        self._check_exits(bar, to_eur)
        if self.pos is not None:
            self.bars_in_market += 1
        self._mark(bar, to_eur)

    def close_all(self, bar: Bar, to_eur: float, reason: str) -> None:
        """Close at the last bar's close (end of the test) and update the last equity point."""
        if self.pos is None:
            return
        spread, slip = self.costs.spread, self.costs.slippage
        price = bar.close - slip if self.pos.side == "long" else bar.close + spread + slip
        self._close(bar.ts, price, reason, to_eur, market=True)
        self.last_equity = self.balance
        if self.equity_curve:
            self.equity_curve[-1] = (self.equity_curve[-1][0], self.balance)

    # ---------- internals ----------

    def _units(self, lots: float) -> float:
        return lots * self.instrument.contract_size

    def _charge_financing(self, bar: Bar, to_eur: float) -> None:
        p = self.pos
        if p is None or not self.costs.financing_pct:
            return
        day = bar.ts // DAY
        nights = day - p.financed_day
        if nights <= 0:
            return
        value = self._units(p.lots) * bar.open * to_eur
        cost = value * self.costs.financing_pct / 100 / 365 * nights
        p.financing_eur += cost
        p.financed_day = day
        self.balance -= cost

    def _execute(self, req: OrderRequest, bar: Bar, to_eur: float) -> None:
        if req.kind == "close":
            if self.pos is None:
                self.log.add(bar.ts, "info", "Sluitorder genegeerd: er is geen open positie.", client_id=req.client_id)
                return
            spread, slip = self.costs.spread, self.costs.slippage
            price = bar.open - slip if self.pos.side == "long" else bar.open + spread + slip
            self._close(bar.ts, price, req.reason or "Signaal", to_eur, market=True)
            return

        if self.halted:
            self.log.add(bar.ts, "skip", "Order overgeslagen: de rekening is leeg.", client_id=req.client_id)
            return
        if self.pos is not None:
            self.log.add(bar.ts, "skip", "Order overgeslagen: er staat al een positie open.", client_id=req.client_id)
            return
        self._open(req, bar, to_eur)

    def _open(self, req: OrderRequest, bar: Bar, to_eur: float) -> None:
        spread, slip = self.costs.spread, self.costs.slippage
        side = req.side
        if side == "long":
            fill = bar.open + spread + slip
            sl_ok = req.stop_loss < bar.open
            tp_ok = req.take_profit is None or req.take_profit > bar.open
        else:
            fill = bar.open - slip
            sl_ok = req.stop_loss > bar.open + spread
            tp_ok = req.take_profit is None or req.take_profit < bar.open + spread
        if not sl_ok:
            self.log.add(bar.ts, "skip", "Order overgeslagen: de koers opende al voorbij de stop-loss.",
                         client_id=req.client_id)
            return
        take_profit = req.take_profit
        if not tp_ok:
            self.log.add(bar.ts, "warning", "Take-profit vervallen: de koers opende er al voorbij.",
                         client_id=req.client_id)
            take_profit = None

        equity = self.balance
        lots, note = size_position(equity, fill, req.stop_loss, req.risk_fraction, self.instrument, to_eur, self.sizing)
        if lots <= 0:
            self.log.add(bar.ts, "skip", note, client_id=req.client_id, reason_code="size")
            return
        if note:
            self.log.add(bar.ts, "warning", note, client_id=req.client_id)

        units = self._units(lots)
        commission = self.costs.commission_per_lot * lots
        self.balance -= commission
        self.pos = _Open(
            side=side,
            lots=lots,
            entry_price=fill,
            entry_ts=bar.ts,
            entry_index=self.bar_index,
            stop_loss=req.stop_loss,
            take_profit=take_profit,
            client_id=req.client_id,
            reason=req.reason,
            equity_at_entry=equity,
            initial_risk_eur=abs(fill - req.stop_loss) * units * to_eur,
            spread_eur=spread * units * to_eur,
            slippage_eur=slip * units * to_eur,
            commission_eur=commission,
            financed_day=bar.ts // DAY,
        )
        self.log.add(
            bar.ts, "fill",
            f"{'Long' if side == 'long' else 'Short'} geopend: {lots:g} lot op {fill:.{self.instrument.digits}f}",
            client_id=req.client_id, side=side, lots=lots, price=fill,
            stop_loss=req.stop_loss, take_profit=take_profit, commission=commission,
        )

    def _check_exits(self, bar: Bar, to_eur: float) -> None:
        p = self.pos
        if p is None:
            return
        spread, slip = self.costs.spread, self.costs.slippage
        sl, tp = p.stop_loss, p.take_profit
        if p.side == "long":
            if bar.open <= sl:
                return self._close(bar.ts, bar.open - slip, "Stop-loss (koersgat)", to_eur, market=True)
            if bar.low <= sl:
                return self._close(bar.ts, sl - slip, "Stop-loss", to_eur, market=True)
            if tp is not None and bar.open >= tp:
                return self._close(bar.ts, bar.open, "Take-profit (koersgat)", to_eur, market=False)
            if tp is not None and bar.high >= tp:
                return self._close(bar.ts, tp, "Take-profit", to_eur, market=False)
        else:
            ask_open, ask_high, ask_low = bar.open + spread, bar.high + spread, bar.low + spread
            if ask_open >= sl:
                return self._close(bar.ts, ask_open + slip, "Stop-loss (koersgat)", to_eur, market=True)
            if ask_high >= sl:
                return self._close(bar.ts, sl + slip, "Stop-loss", to_eur, market=True)
            if tp is not None and ask_open <= tp:
                return self._close(bar.ts, ask_open, "Take-profit (koersgat)", to_eur, market=False)
            if tp is not None and ask_low <= tp:
                return self._close(bar.ts, tp, "Take-profit", to_eur, market=False)

    def _close(self, ts: int, price: float, reason: str, to_eur: float, market: bool) -> None:
        p = self.pos
        units = self._units(p.lots)
        commission = self.costs.commission_per_lot * p.lots
        price_pnl = (price - p.entry_price) * p.direction * units * to_eur
        self.balance += price_pnl - commission
        p.commission_eur += commission
        if market:
            p.slippage_eur += self.costs.slippage * units * to_eur
        net = price_pnl - p.commission_eur - p.financing_eur
        costs = {
            "spread": p.spread_eur,
            "slippage": p.slippage_eur,
            "commission": p.commission_eur,
            "financing": p.financing_eur,
        }
        trade = {
            "id": len(self.trades) + 1,
            "side": p.side,
            "lots": p.lots,
            "entry_ts": p.entry_ts,
            "entry_price": p.entry_price,
            "exit_ts": ts,
            "exit_price": price,
            "stop_loss": p.stop_loss,
            "take_profit": p.take_profit,
            "entry_reason": p.reason,
            "exit_reason": reason,
            "pnl": net,
            "return_pct": net / p.equity_at_entry * 100 if p.equity_at_entry else 0.0,
            "r_multiple": net / p.initial_risk_eur if p.initial_risk_eur else 0.0,
            "costs": costs,
            "costs_total": sum(costs.values()),
            "bars_held": self.bar_index - p.entry_index + 1,
            "client_id": p.client_id,
        }
        self.trades.append(trade)
        self.pos = None
        self.log.add(
            ts, "exit",
            f"{'Long' if p.side == 'long' else 'Short'} gesloten ({reason}) op "
            f"{price:.{self.instrument.digits}f}: {'+' if net >= 0 else '−'}€{abs(net):.2f}",
            client_id=p.client_id, price=price, pnl=net,
        )
        if self.balance <= 0:
            self.halted = True
            self.log.add(ts, "warning", "De rekening is leeg: er worden geen nieuwe posities meer geopend.")

    def _mark(self, bar: Bar, to_eur: float) -> None:
        equity = self.balance
        p = self.pos
        if p is not None:
            exit_price = bar.close if p.side == "long" else bar.close + self.costs.spread
            equity += (exit_price - p.entry_price) * p.direction * self._units(p.lots) * to_eur
        self.last_equity = equity
        self.equity_curve.append((bar.ts, equity))
