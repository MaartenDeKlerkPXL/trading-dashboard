"""Executor contract and the rules every executor shares: costs and position sizing.

Backtest, paper and live executors all implement BrokerExecutor. The runner
only talks to this interface, so strategy code is identical in every mode.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from ..data.instruments import Instrument
from ..strategies.base import Bar, Position, Side

SIZING_MODES = ("realistic", "fractional")


@dataclass(frozen=True)
class OrderRequest:
    """What the runner asks an executor to do. Executed at the next available price."""

    kind: Literal["open", "close"]
    client_id: str                  # unique and deterministic: a restart never duplicates an order
    signal_ts: int
    side: Side | None = None        # for "open"
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_fraction: float = 1.0
    reason: str = ""


@dataclass(frozen=True)
class CostModel:
    spread: float              # price units, paid on every round trip
    slippage: float            # price units, per market fill
    commission_per_lot: float  # EUR per lot per side
    financing_pct: float       # % per year on the position value, per night held

    def validate(self) -> None:
        if not self.spread > 0:
            raise ValueError("De spread moet groter dan 0 zijn: zonder kosten is een backtest te rooskleurig.")
        if not self.slippage > 0:
            raise ValueError("De slippage moet groter dan 0 zijn: zonder kosten is een backtest te rooskleurig.")
        if self.commission_per_lot < 0 or self.financing_pct < 0:
            raise ValueError("Commissie en financiering kunnen niet negatief zijn.")

    @classmethod
    def defaults(cls, instrument: Instrument) -> "CostModel":
        return cls(instrument.spread, instrument.slippage, instrument.commission_per_lot, 6.0)


@dataclass(frozen=True)
class SizingRules:
    risk_pct: float            # % of equity risked per trade (distance to the stop-loss)
    mode: str = "realistic"    # "realistic": broker lot sizes; "fractional": exact size
    leverage: int = 100
    margin_buffer: float = 0.95  # never use more than this share of equity as margin

    def validate(self) -> None:
        if not 0 < self.risk_pct <= 10:
            raise ValueError("Risico per trade moet tussen 0 en 10% liggen.")
        if self.mode not in SIZING_MODES:
            raise ValueError("Onbekende manier van positiegrootte bepalen.")


def size_position(
    equity: float,
    entry_price: float,
    stop_loss: float,
    risk_fraction: float,
    instrument: Instrument,
    to_eur: float,
    rules: SizingRules,
) -> tuple[float, str]:
    """Lots to trade so that hitting the stop-loss costs `risk_pct` of equity.

    Returns (lots, note). lots == 0 means: do not trade; the note says why.
    """
    risk_eur = equity * rules.risk_pct / 100 * max(0.0, min(risk_fraction, 1.0))
    per_lot = abs(entry_price - stop_loss) * instrument.contract_size * to_eur
    if risk_eur <= 0 or per_lot <= 0:
        return 0.0, "Geen risicobudget of stop-loss op de instapprijs."
    lots = risk_eur / per_lot
    note = ""

    leverage = min(rules.leverage, instrument.max_leverage)
    margin_per_lot = entry_price * instrument.contract_size * to_eur / leverage
    max_lots = equity * rules.margin_buffer / margin_per_lot
    if lots > max_lots:
        lots = max_lots
        note = f"Positie verkleind tot de beschikbare marge (hefboom 1:{leverage})."

    if rules.mode == "fractional":
        return lots, note

    steps = math.floor(lots / instrument.lot_step + 1e-9)
    lots = round(steps * instrument.lot_step, 8)
    if lots < instrument.min_lot:
        min_lot_risk = per_lot * instrument.min_lot / equity * 100
        return 0.0, (
            f"Overgeslagen: de kleinste positie ({instrument.min_lot} lot) zou {min_lot_risk:.1f}% risico geven, "
            f"meer dan de ingestelde {rules.risk_pct * risk_fraction:.1f}%."
        )
    return lots, note


class BrokerExecutor(ABC):
    """One virtual account trading one instrument for one strategy."""

    mode: str

    @abstractmethod
    def submit(self, requests: list[OrderRequest]) -> None:
        """Queue orders. They are executed at the next available price."""

    @abstractmethod
    def position(self) -> Position:
        ...

    @abstractmethod
    def equity(self) -> float:
        """Account value in EUR including open profit/loss."""

    @abstractmethod
    def on_bar(self, bar: Bar, to_eur: float) -> None:
        """Process a newly closed bar: fills, stop-loss/take-profit, costs, equity."""
