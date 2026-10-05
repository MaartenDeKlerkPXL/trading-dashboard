"""Simulated broker for backtests.

Price data are BID candles. Buying happens at the ask (bid + spread), selling
at the bid. Market fills also pay slippage. Orders submitted after bar N are
filled at the open of bar N+1. Within a bar, if both the stop-loss and the
take-profit could have been hit, the stop-loss is assumed to come first.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from ..data.instruments import Instrument
from ..risk import WEEKEND_CATEGORIES, RiskLimits, before_weekend, day_key
from ..strategies.base import Bar, Position, Side
from .base import BrokerExecutor, CostModel, OrderRequest, SizingRules, size_position
from .events import EventLog, nl, nl_lots

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
    position_id: int | None = None   # the broker's id (live trading only)
    initial_stop: float | None = None  # the stop-loss at entry (a trailing stop moves stop_loss)

    @property
    def direction(self) -> int:
        return 1 if self.side == "long" else -1


class BacktestExecutor(BrokerExecutor):
    mode = "backtest"

    def __init__(self, instrument: Instrument, costs: CostModel, sizing: SizingRules, capital: float, log: EventLog,
                 risk: RiskLimits | None = None, bar_seconds: int = 60):
        self.instrument = instrument
        self.risk = risk                # hard limits; None = pure strategy test without limits
        self.bar_seconds = bar_seconds  # length of the bars this executor is fed
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
        self.trade_offset = 0   # trades closed in earlier sessions of a long-running (paper) account
        self.open_elsewhere = 0  # open positions of other strategies that count towards the same limit
        self.day: int | None = None
        self.day_start_equity = float(capital)
        self.blocked_day: int | None = None  # daily loss limit hit on this day: no new positions

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
        if self.risk:
            day = day_key(bar.ts, self.risk.timezone)
            if day != self.day:
                self.day, self.day_start_equity = day, self.last_equity
        self._charge_financing(bar, to_eur)
        pending, self.pending = self.pending, []
        for req in pending:
            self._execute(req, bar, to_eur)
        self._check_exits(bar, to_eur)
        if self.risk:
            self._weekend(bar, to_eur)
        if self.pos is not None:
            self.bars_in_market += 1
        self._mark(bar, to_eur)
        if self.risk:
            self._daily_loss(bar, to_eur)

    def cancel_pending(self, ts: int, reason: str) -> int:
        """Cancel every order that is still waiting for a price. Returns how many."""
        n = len(self.pending)
        for req in self.pending:
            self.log.add(ts, "risk", f"Order geannuleerd: {reason}.", client_id=req.client_id, reason_code="cancel")
        self.pending = []
        return n

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

    # ---------- persistence (paper/live keep an account alive across restarts) ----------

    def to_state(self) -> dict:
        return {
            "balance": self.balance,
            "last_equity": self.last_equity,
            "halted": self.halted,
            "bar_index": self.bar_index,
            "bars_in_market": self.bars_in_market,
            "trade_count": self.trade_offset + len(self.trades),
            "position": asdict(self.pos) if self.pos else None,
            "pending": [asdict(r) for r in self.pending],
            "risk": {"day": self.day, "day_start_equity": self.day_start_equity, "blocked_day": self.blocked_day},
        }

    def restore(self, state: dict) -> None:
        self.balance = state["balance"]
        self.last_equity = state["last_equity"]
        self.halted = state["halted"]
        self.bar_index = state["bar_index"]
        self.bars_in_market = state["bars_in_market"]
        self.trade_offset = state["trade_count"]
        self.pos = _Open(**state["position"]) if state.get("position") else None
        self.pending = [OrderRequest(**r) for r in state.get("pending", [])]
        risk = state.get("risk") or {}
        self.day = risk.get("day")
        self.day_start_equity = risk.get("day_start_equity", self.last_equity)
        self.blocked_day = risk.get("blocked_day")

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

        if req.kind == "modify":
            self._modify(req, bar)
            return

        if self.halted:
            self.log.add(bar.ts, "skip", "Order overgeslagen: de rekening is leeg.", client_id=req.client_id)
            return
        if self.pos is not None:
            self.log.add(bar.ts, "skip", "Order overgeslagen: er staat al een positie open.", client_id=req.client_id)
            return
        if self.risk:
            if self.blocked_day is not None and self.blocked_day == self.day:
                self.log.add(bar.ts, "risk", "Order geweigerd: het maximale dagverlies is bereikt. "
                             "Morgen mag er weer gehandeld worden.", client_id=req.client_id, reason_code="daily_loss")
                return
            if self.open_elsewhere >= self.risk.max_open_positions:
                self.log.add(bar.ts, "risk", f"Order geweigerd: er staan al {self.open_elsewhere} posities open "
                             f"(maximum {self.risk.max_open_positions}).", client_id=req.client_id,
                             reason_code="max_positions")
                return
        self._open(req, bar, to_eur)

    def _check_modify(self, req: OrderRequest, ts: int) -> bool:
        """A stop-loss may only move towards the price (less risk): checked outside the strategy."""
        p = self.pos
        if p is None or p.side != req.side:
            self.log.add(ts, "info", "Aanpassing genegeerd: de positie is er niet meer.", client_id=req.client_id)
            return False
        if req.stop_loss is not None:
            looser = req.stop_loss < p.stop_loss if p.side == "long" else req.stop_loss > p.stop_loss
            if looser:
                self.log.add(ts, "risk", "Stop-loss niet verplaatst: hij mag alleen dichter naar de koers, niet verder "
                             "weg (dat zou meer risico geven dan ingesteld).", client_id=req.client_id,
                             reason_code="stop_widening")
                return False
        return True

    def _modify(self, req: OrderRequest, bar: Bar) -> None:
        if not self._check_modify(req, bar.ts):
            return
        p = self.pos
        d = self.instrument.digits
        if req.stop_loss is not None and req.stop_loss != p.stop_loss:
            p.stop_loss = req.stop_loss
            self.log.add(bar.ts, "info", f"Stop-loss verplaatst naar {nl(req.stop_loss, d)}"
                         + (f" ({req.reason})" if req.reason else ""), client_id=req.client_id)
        if req.take_profit is not None and req.take_profit != p.take_profit:
            p.take_profit = req.take_profit
            self.log.add(bar.ts, "info", f"Take-profit verplaatst naar {nl(req.take_profit, d)}",
                         client_id=req.client_id)

    def _plan_open(self, req: OrderRequest, bar: Bar, to_eur: float) -> tuple[float, float, float | None] | None:
        """Fill price, lots and take-profit for an opening order at this bar's open, after every check.
        Returns None (and logs why) when the order must not be executed."""
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

        equity = self.equity_for_sizing()
        risk_fraction = max(0.0, min(req.risk_fraction, 1.0))
        if self.risk and self.sizing.risk_pct * risk_fraction > self.risk.max_risk_per_trade_pct:
            risk_fraction = self.risk.max_risk_per_trade_pct / self.sizing.risk_pct
            self.log.add(bar.ts, "risk", f"Risico verlaagd naar de harde limiet van "
                         f"{nl(self.risk.max_risk_per_trade_pct, 1)}% per trade.", client_id=req.client_id,
                         reason_code="risk_per_trade")
        lots, note = size_position(equity, fill, req.stop_loss, risk_fraction, self.instrument, to_eur, self.sizing)
        if lots <= 0:
            self.log.add(bar.ts, "skip", note, client_id=req.client_id, reason_code="size")
            return
        if note:
            self.log.add(bar.ts, "warning", note, client_id=req.client_id)
        if self.risk and lots > self.risk.lots_limit(self.instrument.symbol):
            limit = self.risk.lots_limit(self.instrument.symbol)
            if self.sizing.mode == "realistic":
                limit = round(math.floor(limit / self.instrument.lot_step + 1e-9) * self.instrument.lot_step, 8)
            if limit < self.instrument.min_lot:
                self.log.add(bar.ts, "risk", "Order geweigerd: de maximale positiegrootte is kleiner dan de "
                             "kleinste lotgrootte.", client_id=req.client_id, reason_code="max_lots")
                return
            self.log.add(bar.ts, "risk", f"Positie verkleind van {nl_lots(lots)} naar {nl_lots(limit)} lot: "
                         f"de maximale positiegrootte voor {self.instrument.symbol}.", client_id=req.client_id,
                         reason_code="max_lots")
            lots = limit
        return fill, lots, take_profit

    def equity_for_sizing(self) -> float:
        return self.balance

    def _open(self, req: OrderRequest, bar: Bar, to_eur: float) -> None:
        plan = self._plan_open(req, bar, to_eur)
        if plan is None:
            return
        fill, lots, take_profit = plan
        side = req.side
        spread, slip = self.costs.spread, self.costs.slippage
        equity = self.balance

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
            initial_stop=req.stop_loss,
        )
        self.log.add(
            bar.ts, "fill",
            f"{'Long' if side == 'long' else 'Short'} geopend: {nl_lots(lots)} lot op {nl(fill, self.instrument.digits)}",
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

    def _exit_price(self, price: float) -> float:
        """Price at which the open position would close now at market (price is a bid)."""
        spread, slip = self.costs.spread, self.costs.slippage
        return price - slip if self.pos.side == "long" else price + spread + slip

    def open_pnl(self, price: float, to_eur: float) -> float:
        """Net result in EUR if the open position were closed now at market, after all costs."""
        p = self.pos
        if p is None:
            return 0.0
        units = self._units(p.lots)
        gross = (self._exit_price(price) - p.entry_price) * p.direction * units * to_eur
        return gross - p.commission_eur - self.costs.commission_per_lot * p.lots - p.financing_eur

    def _weekend(self, bar: Bar, to_eur: float) -> None:
        r = self.risk
        if (not r.weekend_close or self.pos is None or self.instrument.category not in WEEKEND_CATEGORIES
                or not before_weekend(bar.ts + self.bar_seconds, r.weekend_close_minutes_before)):
            return
        pnl = self.open_pnl(bar.close, to_eur)
        if pnl > 0:
            self.log.add(bar.ts, "risk", f"Weekendregel: positie staat {nl(pnl)} EUR in de winst en wordt vóór "
                         "het weekend gesloten.", reason_code="weekend")
            self._close(bar.ts, self._exit_price(bar.close), "Weekend (in de winst)", to_eur, market=True)

    def _daily_loss(self, bar: Bar, to_eur: float) -> None:
        r = self.risk
        if self.blocked_day == self.day or self.day_start_equity <= 0:
            return
        loss_pct = (self.day_start_equity - self.last_equity) / self.day_start_equity * 100
        if loss_pct < r.max_daily_loss_pct:
            return
        self.blocked_day = self.day
        self.log.add(bar.ts, "risk", f"Maximaal dagverlies bereikt ({nl(loss_pct, 2)}%, limiet "
                     f"{nl(r.max_daily_loss_pct, 1)}%): open positie gesloten, wachtende orders geannuleerd. "
                     "Morgen mag er weer gehandeld worden.", reason_code="daily_loss")
        self.cancel_pending(bar.ts, "maximaal dagverlies bereikt")
        self.close_all(bar, to_eur, "Maximaal dagverlies")

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
            "id": self.trade_offset + len(self.trades) + 1,
            "side": p.side,
            "lots": p.lots,
            "entry_ts": p.entry_ts,
            "entry_price": p.entry_price,
            "exit_ts": ts,
            "exit_price": price,
            "stop_loss": p.stop_loss,
            "initial_stop": p.initial_stop if p.initial_stop is not None else p.stop_loss,
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
            f"{nl(price, self.instrument.digits)}: {'+' if net >= 0 else '−'}€ {nl(abs(net))}",
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
