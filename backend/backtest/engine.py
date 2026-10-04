"""Bar-by-bar backtest.

For every bar: the executor first fills orders from the previous bar at this
bar's open and checks stop-loss/take-profit, then the strategy sees the bar
as closed. A signal on bar N is therefore executed on bar N+1: no lookahead.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from ..data.instruments import FALLBACK_EUR_RATES, Instrument
from ..execution.backtest import BacktestExecutor
from ..execution.base import CostModel, SizingRules
from ..execution.events import EventLog
from ..runner import Runner
from ..strategies.base import Bar, Strategy
from .metrics import compute_metrics, drawdown_curve

MIN_TRADES_RELIABLE = 30
MAX_CURVE_POINTS = 5000


class RateSeries:
    """Quote-currency units per 1 EUR over time (daily closes), without lookahead."""

    def __init__(self, points: list[tuple[int, float]], fallback: float, currency: str):
        self.ts = [t for t, _ in points]
        self.rates = [r for _, r in points]
        self.fallback = fallback
        self.currency = currency
        self.used_fallback = False

    @classmethod
    def constant(cls, rate: float, currency: str = "EUR") -> "RateSeries":
        """A fixed rate that does not count as a fallback (used in tests and for EUR)."""
        return cls([(0, rate)], rate, currency)

    def to_eur(self, ts: int) -> float:
        """EUR per 1 unit of quote currency, using the last daily close known at time ts."""
        if self.currency == "EUR":
            return 1.0
        # A daily candle opening at t is only known (closed) from t + 1 day on.
        i = bisect.bisect_right(self.ts, ts - 86400) - 1
        if i < 0:
            if self.rates:
                rate = self.rates[0]
            else:
                self.used_fallback = True
                rate = self.fallback
        else:
            rate = self.rates[i]
        return 1.0 / rate


@dataclass(frozen=True)
class BacktestConfig:
    capital: float
    costs: CostModel
    sizing: SizingRules

    def validate(self) -> None:
        if not 10 <= self.capital <= 100_000_000:
            raise ValueError("Startkapitaal moet tussen €10 en €100 miljoen liggen.")
        self.costs.validate()
        self.sizing.validate()


def run_backtest(
    bars: list[Bar],
    strategy: Strategy,
    instrument: Instrument,
    config: BacktestConfig,
    rates: RateSeries | None = None,
    close_at_end: bool = True,
) -> dict:
    """close_at_end=False leaves a final open position open (marked at the last price), e.g. to
    compare with a paper session in which that position is still running."""
    config.validate()
    if rates is None:
        rates = RateSeries([], FALLBACK_EUR_RATES.get(instrument.quote_currency, 1.0), instrument.quote_currency)

    log = EventLog()
    executor = BacktestExecutor(instrument, config.costs, config.sizing, config.capital, log)
    runner = Runner(strategy, executor, instrument.symbol, log, digits=instrument.digits)

    for i, bar in enumerate(bars):
        executor.on_bar(bar, rates.to_eur(bar.ts))
        runner.on_closed_bar(bars, i + 1)

    if bars:
        if close_at_end:
            executor.close_all(bars[-1], rates.to_eur(bars[-1].ts), "Einde backtest")
        if executor.pending:
            log.add(bars[-1].ts, "info", "Signaal op de laatste candle is niet meer uitgevoerd.")

    periods = 365 if instrument.category == "crypto" else 252
    metrics = compute_metrics(
        executor.equity_curve, executor.trades, config.capital, periods, len(bars), executor.bars_in_market
    )
    metrics["buy_hold_pct"] = (bars[-1].close / bars[0].open - 1) * 100 if bars else None
    metrics["skipped_signals"] = sum(1 for e in log.events if e["kind"] == "skip")

    warnings = []
    n = metrics["trades"]
    if n < MIN_TRADES_RELIABLE:
        warnings.append(
            f"Slechts {n} trade{'s' if n != 1 else ''}: te weinig voor betrouwbare conclusies. "
            f"Gebruik minimaal {MIN_TRADES_RELIABLE} trades (liever 100 of meer): kies een langere periode "
            "of een kleinere timeframe."
        )
    size_skips = sum(1 for e in log.events if e.get("reason_code") == "size")
    if size_skips:
        warnings.append(
            f"{size_skips} signaal/signalen overgeslagen omdat de kleinste lotgrootte meer risico gaf dan ingesteld. "
            "Verhoog het kapitaal of het risico, of kies 'Ideaal (fractionele lots)' om zuivere percentages te zien."
        )
    if rates.used_fallback:
        warnings.append(
            f"Geen wisselkoersdata voor {instrument.quote_currency}→EUR gevonden: "
            f"omgerekend met een vaste koers van {rates.fallback}."
        )
    if bars and len(bars) < strategy.warmup() + 2:
        warnings.append("De periode is korter dan de opwarmtijd van de strategie: er kon niet gehandeld worden.")

    curve = executor.equity_curve
    dd = drawdown_curve(curve)
    step = max(1, len(curve) // MAX_CURVE_POINTS)
    keep = list(range(0, len(curve), step))
    if curve and keep[-1] != len(curve) - 1:
        keep.append(len(curve) - 1)

    return {
        "metrics": metrics,
        "warnings": warnings,
        "trades": executor.trades,
        "equity": [{"time": curve[i][0], "value": round(curve[i][1], 2)} for i in keep],
        "drawdown": [{"time": dd[i][0], "value": round(dd[i][1], 3)} for i in keep],
        "events": log.events,
        "bars": len(bars),
        "warmup": strategy.warmup(),
    }
