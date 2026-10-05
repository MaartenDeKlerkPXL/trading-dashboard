"""Turning an API request into a ready-to-run backtest. Shared by backtest, compare and optimize."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, Field

from ..config import Settings
from ..data.instruments import EUR_CONVERSION, FALLBACK_EUR_RATES, INSTRUMENTS, TIMEFRAMES, Instrument
from ..data.store import CandleStore
from ..execution.base import CostModel, SizingRules
from ..strategies import load_strategies
from ..strategies.base import Bar, Strategy
from .engine import BacktestConfig, RateSeries, run_backtest

MIN_SEGMENT_BARS = 20


class SetupError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class CommonSettings(BaseModel):
    """Everything except the strategy: what, when, how much and at what cost."""

    symbol: str
    timeframe: str
    start: str                          # YYYY-MM-DD, UTC
    end: str                            # YYYY-MM-DD, inclusive
    capital: float | None = None        # EUR; default from config.toml
    risk_pct: float | None = None       # default from config.toml
    sizing_mode: str = "realistic"
    spread: float | None = None         # price units; default per instrument
    slippage: float | None = None
    commission_per_lot: float | None = None
    financing_pct: float = 6.0
    apply_risk: bool = False            # apply the hard risk limits from config.toml, as in paper/live


class BacktestRequest(CommonSettings):
    strategy: str                       # "name@version"
    params: dict = Field(default_factory=dict)
    oos_pct: float = 0.0                # share of the period at the end kept apart as out-of-sample


@dataclass
class Setup:
    common: CommonSettings
    instrument: Instrument
    bars: list[Bar]
    config: BacktestConfig
    rates: RateSeries
    start_ts: int
    end_ts: int
    extra: dict = field(default_factory=dict)


def _parse_day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SetupError(400, f"Ongeldige {label}: '{value}'. Gebruik het formaat JJJJ-MM-DD.") from None


def validate_period(symbol: str, timeframe: str, start: str, end: str) -> tuple[int, int]:
    if symbol not in INSTRUMENTS:
        raise SetupError(400, f"Onbekend instrument '{symbol}'.")
    if timeframe not in TIMEFRAMES:
        raise SetupError(400, f"Onbekende timeframe '{timeframe}'.")
    d0, d1 = _parse_day(start, "startdatum"), _parse_day(end, "einddatum")
    if d1 < d0:
        raise SetupError(400, "De einddatum ligt vóór de startdatum.")
    tf = TIMEFRAMES[timeframe]
    if (d1 - d0).days + 1 > tf.max_days:
        raise SetupError(
            400,
            f"Periode te lang voor {timeframe}: maximaal {tf.max_days} dagen. "
            "Kies een kortere periode of een grotere timeframe.",
        )
    start_ts = int(datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc).timestamp())
    end_day = d1 + timedelta(days=1)
    end_ts = int(datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc).timestamp())
    return start_ts, end_ts


def strategy_class(key: str) -> type[Strategy]:
    cls = load_strategies().get(key)
    if cls is None:
        raise SetupError(400, f"Onbekende strategie '{key}'.")
    return cls


def make_strategy(cls: type[Strategy], params: dict) -> Strategy:
    try:
        return cls(**params)
    except ValueError as exc:
        raise SetupError(400, str(exc)) from None


async def eur_rates(store: CandleStore, instrument: Instrument, start: int, end: int, log=None) -> RateSeries:
    """Daily quote→EUR rates for the period, downloaded if needed; falls back to a fixed rate."""
    quote = instrument.quote_currency
    if quote == "EUR":
        return RateSeries.constant(1.0, "EUR")
    fallback = FALLBACK_EUR_RATES[quote]
    symbol = EUR_CONVERSION[quote]
    begin = start - 10 * 86400
    try:
        await store.sync(symbol, "d1", begin, end)
    except Exception:  # conversion data is a refinement; never block the backtest on it
        if log:
            log.exception("Could not download %s for currency conversion", symbol)
    rows = store.read(symbol, "D1", begin, end)
    return RateSeries([(c.ts, c.close) for c in rows], fallback, quote)


async def prepare(store: CandleStore, settings: Settings, common: CommonSettings, log=None) -> Setup:
    start_ts, end_ts = validate_period(common.symbol, common.timeframe, common.start, common.end)
    instrument = INSTRUMENTS[common.symbol]
    tf = TIMEFRAMES[common.timeframe]

    if store.missing_count(common.symbol, tf.level, start_ts, end_ts):
        raise SetupError(409, "Nog niet alle koersdata voor deze periode is opgehaald. Laad eerst de data.")
    bars = [Bar(*c) for c in store.read(common.symbol, common.timeframe, start_ts, end_ts)]
    if not bars:
        raise SetupError(400, "Geen koersdata in deze periode.")

    account = settings.account
    config = BacktestConfig(
        capital=common.capital if common.capital is not None else account.starting_capital,
        costs=CostModel(
            spread=common.spread if common.spread is not None else instrument.spread,
            slippage=common.slippage if common.slippage is not None else instrument.slippage,
            commission_per_lot=(common.commission_per_lot if common.commission_per_lot is not None
                                else instrument.commission_per_lot),
            financing_pct=common.financing_pct,
        ),
        sizing=SizingRules(
            risk_pct=common.risk_pct if common.risk_pct is not None else account.risk_per_trade_pct,
            mode=common.sizing_mode,
            leverage=account.leverage,
        ),
        risk=settings.risk if common.apply_risk else None,
    )
    try:
        config.validate()
    except ValueError as exc:
        raise SetupError(400, str(exc)) from None

    rates = await eur_rates(store, instrument, start_ts, end_ts, log)
    return Setup(common, instrument, bars, config, rates, start_ts, end_ts)


def settings_dict(setup: Setup, strategy: Strategy) -> dict:
    c, cfg = setup.common, setup.config
    return {
        "symbol": c.symbol, "timeframe": c.timeframe, "start": c.start, "end": c.end,
        "strategy": strategy.key(), "strategy_label": strategy.label, "version": strategy.version,
        "params": strategy.p, "capital": cfg.capital, "risk_pct": cfg.sizing.risk_pct,
        "sizing_mode": cfg.sizing.mode, "leverage": min(cfg.sizing.leverage, setup.instrument.max_leverage),
        "costs": {"spread": cfg.costs.spread, "slippage": cfg.costs.slippage,
                  "commission_per_lot": cfg.costs.commission_per_lot,
                  "financing_pct": cfg.costs.financing_pct},
        "currency": "EUR", "quote_currency": setup.instrument.quote_currency,
        "apply_risk": cfg.risk is not None,
    }


def split_index(n_bars: int, oos_pct: float) -> int:
    return int(round(n_bars * (1 - oos_pct / 100)))


def run_segment(bars: list[Bar], cls: type[Strategy], params: dict, setup: Setup) -> dict:
    return run_backtest(bars, cls(**params), setup.instrument, setup.config, setup.rates)


def out_of_sample(setup: Setup, cls: type[Strategy], params: dict, oos_pct: float) -> dict:
    """Run the in-sample part and the out-of-sample part separately, each starting with fresh capital.

    The out-of-sample run gets the strategy's warm-up bars from just before the split,
    so its indicators are ready but it cannot trade before the split.
    """
    bars = setup.bars
    split = split_index(len(bars), oos_pct)
    warmup = cls(**params).warmup()
    if split < warmup + MIN_SEGMENT_BARS or len(bars) - split < MIN_SEGMENT_BARS:
        return {"pct": oos_pct, "error": "Te weinig candles om de periode te splitsen. Kies een langere periode."}
    is_run = run_segment(bars[:split], cls, params, setup)
    oos_run = run_segment(bars[max(0, split - warmup):], cls, params, setup)
    notes = []
    m_is, m_oos = is_run["metrics"], oos_run["metrics"]
    if m_oos["trades"] < 30:
        notes.append(f"Out-of-sample heeft {m_oos['trades']} trades: te weinig om er zeker van te zijn.")
    if m_is["total_return_pct"] > 0 and m_oos["total_return_pct"] <= 0:
        notes.append("Winstgevend in-sample, maar niet out-of-sample: een teken dat de strategie op toeval of "
                     "op deze specifieke periode leunt.")
    return {
        "pct": oos_pct,
        "split_ts": bars[split].ts,
        "in_sample": m_is,
        "out_of_sample": m_oos,
        "notes": notes,
    }
