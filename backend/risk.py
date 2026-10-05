"""Hard risk limits. Enforced by the executor, outside the strategy: a strategy cannot override them.

- max risk per trade: the stop-loss distance never risks more than this share of equity;
- max position size per instrument (lots);
- max daily loss: once equity is this far below the start of the day (Dutch time), open positions are
  closed, waiting orders are cancelled and no new positions are opened until the next day;
- max open positions over all strategies of the same mode together (paper sessions count together);
- weekend: forex and metal positions that are in profit are closed shortly before the market closes on
  Friday (17:00 New York time). Losing positions stay open with their stop-loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
WEEKEND_CATEGORIES = ("forex", "metal")
MARKET_CLOSE_MINUTE = 17 * 60   # Friday 17:00 New York time


@dataclass(frozen=True)
class RiskLimits:
    max_daily_loss_pct: float = 2.0
    max_open_positions: int = 3
    max_risk_per_trade_pct: float = 2.0
    max_lots_default: float = 1.0
    max_lots: dict = field(default_factory=dict)   # per symbol, overrides max_lots_default
    weekend_close: bool = True
    weekend_close_minutes_before: int = 30
    timezone: str = "Europe/Amsterdam"            # the "day" of the daily loss limit

    def validate(self) -> None:
        if not 0 < self.max_daily_loss_pct <= 50:
            raise ValueError("risk.max_daily_loss_pct moet tussen 0 en 50 liggen.")
        if not 1 <= self.max_open_positions <= 100:
            raise ValueError("risk.max_open_positions moet tussen 1 en 100 liggen.")
        if not 0 < self.max_risk_per_trade_pct <= 10:
            raise ValueError("risk.max_risk_per_trade_pct moet tussen 0 en 10 liggen.")
        lots = [self.max_lots_default, *self.max_lots.values()]
        if any(not isinstance(v, (int, float)) or v <= 0 for v in lots):
            raise ValueError("risk.max_lots: elke waarde moet een getal groter dan 0 zijn.")
        if not 0 <= self.weekend_close_minutes_before <= 600:
            raise ValueError("risk.weekend_close_minutes_before moet tussen 0 en 600 liggen.")
        ZoneInfo(self.timezone)

    def lots_limit(self, symbol: str) -> float:
        return float(self.max_lots.get(symbol, self.max_lots_default))

    def to_dict(self) -> dict:
        return {
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_open_positions": self.max_open_positions,
            "max_risk_per_trade_pct": self.max_risk_per_trade_pct,
            "max_lots_default": self.max_lots_default,
            "max_lots": dict(self.max_lots),
            "weekend_close": self.weekend_close,
            "weekend_close_minutes_before": self.weekend_close_minutes_before,
            "timezone": self.timezone,
        }


@lru_cache(maxsize=4096)
def _day(hour: int, tz: str) -> int:
    return datetime.fromtimestamp(hour * 3600, ZoneInfo(tz)).date().toordinal()


def day_key(ts: int, tz: str) -> int:
    """The calendar day of ts in the given timezone (whole-hour offsets, so cached per hour)."""
    return _day(ts // 3600, tz)


def before_weekend(ts: int, minutes_before: int) -> bool:
    """True from `minutes_before` minutes before Friday's market close until the market reopens."""
    ny = datetime.fromtimestamp(ts, NEW_YORK)
    minute = ny.hour * 60 + ny.minute
    if ny.weekday() == 4:                       # Friday
        return minute >= MARKET_CLOSE_MINUTE - minutes_before
    if ny.weekday() == 5:                       # Saturday
        return True
    return ny.weekday() == 6 and minute < MARKET_CLOSE_MINUTE   # Sunday before the open
