"""One paper-trading session: advancing it over newly arrived one-minute candles.

`advance` is a pure function of (configuration, saved state, candles, clock):
processing new minutes in one go or spread over many ticks gives the same result,
and nothing is ever processed twice. That is what makes restarts safe.

Timeline per minute candle:
  1. If it belongs to a newer strategy candle, the previous strategy candle has
     closed: the strategy decides, and orders wait for the next price.
  2. The minute is fed to the executor: waiting orders fill at its open,
     stop-loss / take-profit are checked against its high and low, financing
     is charged at midnight, and equity is marked at its close.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..backtest.engine import RateSeries
from ..data.instruments import TIMEFRAMES, Instrument
from ..data.providers import Candle
from ..data.store import resample
from ..execution.base import CostModel, SizingRules
from ..execution.events import EventLog
from ..execution.paper import PaperExecutor
from ..risk import RiskLimits
from ..runner import Runner
from ..strategies.base import Bar, Strategy

GRACE = 120            # a strategy candle counts as closed this long after its end, even without newer data
OFFLINE_AFTER = 600    # a gap between ticks this long means the dashboard was not running
EQUITY_WINDOW = 900    # equity is stored once per 15 minutes


@dataclass(frozen=True)
class SessionConfig:
    id: int
    symbol: str
    timeframe: str
    strategy: str
    params: dict
    capital: float
    risk_pct: float
    sizing_mode: str
    leverage: int
    costs: dict

    @property
    def tf_seconds(self) -> int:
        return TIMEFRAMES[self.timeframe].seconds


@dataclass
class AdvanceResult:
    state: dict
    events: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    equity: list[tuple[int, float]] = field(default_factory=list)
    outbox: list[dict] = field(default_factory=list)   # live trading: orders for the broker


def data_start(started_at: int, warmup: int, tf_seconds: int) -> int:
    """First minute needed so the strategy has its warm-up history (weekends included)."""
    return started_at - int(warmup * tf_seconds * 1.6) - 3 * 86400


def new_state(started_at: int) -> dict:
    return {
        "started_at": started_at,
        "last_m1_ts": started_at - started_at % 60 - 60,
        "cur_bucket": None,
        "last_bar_ts": None,
        "last_tick_at": None,
        "m1_processed": 0,
        "account": None,
        "last_price": None,
        "last_price_ts": None,
    }


def _duration(seconds: int) -> str:
    hours = seconds / 3600
    return f"{hours:.1f} uur" if hours < 48 else f"{hours / 24:.1f} dagen"


def advance(
    cfg: SessionConfig,
    state: dict,
    m1: list[Candle],
    now: int,
    rates: RateSeries,
    strategy_cls: type[Strategy],
    instrument: Instrument,
    offline_after: int = OFFLINE_AFTER,
    risk: RiskLimits | None = None,
    open_elsewhere: int = 0,
    executor_cls=PaperExecutor,
) -> AdvanceResult:
    """risk: hard limits enforced by the executor. open_elsewhere: positions open in other sessions."""
    state = dict(state)
    tf = cfg.tf_seconds
    log = EventLog()
    executor = executor_cls(
        instrument,
        CostModel(**cfg.costs),
        SizingRules(cfg.risk_pct, cfg.sizing_mode, cfg.leverage),
        cfg.capital,
        log,
        risk=risk,
        bar_seconds=60,
    )
    if state["account"]:
        executor.restore(state["account"])
    executor.open_elsewhere = open_elsewhere
    strategy = strategy_cls(**cfg.params)
    runner = Runner(strategy, executor, cfg.symbol, log, digits=instrument.digits)

    offline_until = None
    if state["last_tick_at"] and now - state["last_tick_at"] > offline_after:
        offline_until = now - GRACE
        log.add(now, "warning",
                f"Het dashboard was {_duration(now - state['last_tick_at'])} niet actief. Candles uit die tijd worden "
                "niet alsnog verhandeld; stop-loss en take-profit zijn wel nagelopen, zoals een broker dat zou doen.")

    complete = [c for c in m1 if c.ts + 60 <= now]
    bars = [Bar(*c) for c in resample(complete, tf)]
    index = {b.ts: i for i, b in enumerate(bars)}

    def close_bar(bucket: int) -> None:
        if state["last_bar_ts"] is not None and bucket <= state["last_bar_ts"]:
            return
        state["last_bar_ts"] = bucket
        if bucket + tf <= state["started_at"] or bucket not in index:
            return
        if offline_until is not None and bucket + tf < offline_until:
            log.add(bucket + tf, "skip", "Candle gemist: het dashboard stond uit. Geen beslissing genomen.")
            return
        runner.on_closed_bar(bars, index[bucket] + 1)

    cur = state["cur_bucket"]
    for c in complete:
        if c.ts <= state["last_m1_ts"]:
            continue
        bucket = c.ts - c.ts % tf
        if cur is not None and bucket > cur:
            close_bar(cur)
        cur = bucket
        executor.on_bar(Bar(*c), rates.to_eur(c.ts))
        state["last_m1_ts"] = c.ts
        state["m1_processed"] += 1
        state["last_price"], state["last_price_ts"] = c.close, c.ts

    # A quiet or closed market sends no new minutes: close the candle on time anyway.
    # Orders from that decision wait for the next price.
    if cur is not None and now >= cur + tf + GRACE:
        close_bar(cur)
    state["cur_bucket"] = cur
    if hasattr(executor, "finish") and complete:
        executor.finish(Bar(*complete[-1]), rates.to_eur(complete[-1].ts))

    trades = executor.trades
    for t in trades:
        t["bars_held"] = round((t["exit_ts"] - t["entry_ts"]) / tf, 1)
    windows: dict[int, float] = {}
    for ts, value in executor.equity_curve:
        windows[ts - ts % EQUITY_WINDOW] = value

    state["account"] = executor.to_state()
    state["last_tick_at"] = now
    return AdvanceResult(state, log.events, trades, sorted(windows.items()), getattr(executor, "outbox", []))
