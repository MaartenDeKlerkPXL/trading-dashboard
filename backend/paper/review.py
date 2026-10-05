"""Reviewing a paper session: side by side with a backtest of the same period, and evaluation criteria."""

from __future__ import annotations

import bisect

from ..backtest.engine import BacktestConfig, RateSeries, run_backtest
from ..compare import match_trades
from ..data.instruments import Instrument
from ..data.providers import Candle
from ..data.store import resample
from ..execution.base import CostModel, SizingRules
from ..risk import RiskLimits
from ..strategies.base import Bar, Strategy
from .session import SessionConfig


def backtest_same_period(cfg: SessionConfig, started_at: int, m1: list[Candle], now: int,
                         rates: RateSeries, cls: type[Strategy], instrument: Instrument,
                         risk: RiskLimits | None = None) -> dict:
    """A regular backtest on exactly the same minute data, starting decisions at the same candle.

    The same hard risk limits apply, except the limit on open positions over all sessions together:
    a single backtest cannot know about the other sessions."""
    tf = cfg.tf_seconds
    bars = [Bar(*c) for c in resample([c for c in m1 if c.ts + 60 <= now], tf)]
    start_bucket = started_at - started_at % tf
    first = next((i for i, b in enumerate(bars) if b.ts >= start_bucket), len(bars))
    warm = cls(**cfg.params).warmup()
    window = bars[max(0, first - warm + 1):]
    config = BacktestConfig(cfg.capital, CostModel(**cfg.costs), SizingRules(cfg.risk_pct, cfg.sizing_mode, cfg.leverage),
                            risk)
    return run_backtest(window, cls(**cfg.params), instrument, config, rates, close_at_end=False)


def _pct(points: list[tuple[int, float]], capital: float) -> list[dict]:
    return [{"time": ts, "value": round((v / capital - 1) * 100, 3)} for ts, v in points]


def comparison(cfg: SessionConfig, started_at: int, paper_equity: list[tuple[int, float]], paper_trades: list[dict],
               paper_metrics: dict, backtest: dict) -> dict:
    capital = cfg.capital
    paper_points = sorted(dict([(started_at - started_at % 900, capital)] + paper_equity).items())
    bt_points = [(p["time"], p["value"]) for p in backtest["equity"] if p["time"] >= started_at - cfg.tf_seconds]
    if not bt_points or bt_points[0][0] > paper_points[0][0]:
        bt_points.insert(0, (paper_points[0][0], capital))

    bt_times = [t for t, _ in bt_points]
    deviation = []
    for ts, value in paper_points:
        i = bisect.bisect_right(bt_times, ts) - 1
        if i >= 0:
            deviation.append({"time": ts, "value": round((value - bt_points[i][1]) / capital * 100, 3)})

    bm = backtest["metrics"]
    rows = []
    for key in ("total_return_pct", "max_drawdown_pct", "sharpe", "winrate_pct", "profit_factor", "trades",
                "avg_trade", "expectancy_r", "costs_total"):
        p, b = paper_metrics.get(key), bm.get(key)
        rows.append({"key": key, "paper": p, "backtest": b,
                     "difference": (p - b) if isinstance(p, (int, float)) and isinstance(b, (int, float)) else None})

    return {
        "paper": _pct(paper_points, capital),
        "backtest": _pct(bt_points, capital),
        "deviation": deviation,
        "metrics": rows,
        "matching": match_trades(backtest["trades"], paper_trades, cfg.timeframe),
        "backtest_metrics": bm,
    }


# ---------- evaluation criteria ----------

CRITERIA = {
    "return_vs_backtest": ("Rendement wijkt hooguit {v} procentpunt af van de backtest", "pp"),
    "max_drawdown": ("Maximale drawdown blijft onder {v}%", "%"),
    "min_trades": ("Minimaal {v} trades", ""),
    "min_return": ("Rendement minimaal {v}%", "%"),
    "min_winrate": ("Winrate minimaal {v}%", "%"),
    "min_profit_factor": ("Profit factor minimaal {v}", ""),
}


def validate_criteria(items: list[dict]) -> list[dict]:
    out = []
    if len(items) > 12:
        raise ValueError("Maximaal 12 criteria.")
    for item in items:
        kind = item.get("type")
        if kind not in CRITERIA:
            raise ValueError(f"Onbekend criterium '{kind}'.")
        try:
            value = float(item.get("value"))
            days = int(item.get("after_days", 0))
        except (TypeError, ValueError):
            raise ValueError("Vul bij elk criterium een getal en een aantal dagen in.") from None
        if not 0 <= days <= 365:
            raise ValueError("Het aantal dagen moet tussen 0 en 365 liggen.")
        out.append({"type": kind, "value": value, "after_days": days})
    return out


def _fmt(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".").rstrip("0").rstrip(",")


def evaluate(criteria: list[dict], paper: dict, backtest: dict | None, days_running: float) -> list[dict]:
    """Status per criterion: pass / fail / pending (too early to judge)."""
    results = []
    for c in criteria:
        template, unit = CRITERIA[c["type"]]
        text = template.format(v=_fmt(c["value"]))
        if c["after_days"]:
            text += f" (beoordelen na {c['after_days']} dagen)"
        actual, ok = None, None
        kind, v = c["type"], c["value"]
        if kind == "return_vs_backtest":
            if backtest is not None:
                diff = paper["total_return_pct"] - backtest["total_return_pct"]
                actual, ok = f"{'+' if diff >= 0 else '−'}{_fmt(abs(diff))} pp", abs(diff) <= v
        elif kind == "max_drawdown":
            actual, ok = f"{_fmt(paper['max_drawdown_pct'])}%", paper["max_drawdown_pct"] < v
        elif kind == "min_trades":
            actual, ok = str(paper["trades"]), paper["trades"] >= v
        elif kind == "min_return":
            actual, ok = f"{_fmt(paper['total_return_pct'])}%", paper["total_return_pct"] >= v
        elif kind == "min_winrate":
            wr = paper.get("winrate_pct")
            actual, ok = ("—", False) if wr is None else (f"{_fmt(wr)}%", wr >= v)
        elif kind == "min_profit_factor":
            pf = paper.get("profit_factor")
            if pf is None and paper.get("no_losing_trades"):
                actual, ok = "∞", True
            else:
                actual, ok = ("—", False) if pf is None else (_fmt(pf), pf >= v)

        if ok is None:
            status = "pending"
        elif days_running < c["after_days"]:
            # Too early to judge. A drawdown limit that is already broken stays broken.
            status = "fail" if kind == "max_drawdown" and not ok else "pending"
        else:
            status = "pass" if ok else "fail"
        results.append({**c, "text": text, "actual": actual, "status": status})
    return results
