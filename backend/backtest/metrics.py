"""Performance metrics from an equity curve and a list of closed trades."""

from __future__ import annotations

import math

DAY = 86400


def drawdown_curve(equity: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Drawdown in % (0 or negative) at every point of the equity curve."""
    out, peak = [], -math.inf
    for ts, value in equity:
        peak = max(peak, value)
        out.append((ts, (value / peak - 1) * 100 if peak > 0 else -100.0))
    return out


def daily_returns(equity: list[tuple[int, float]]) -> list[float]:
    """Returns between the last equity value of consecutive UTC days with data."""
    closes: dict[int, float] = {}
    for ts, value in equity:
        closes[ts // DAY] = value
    values = [closes[d] for d in sorted(closes)]
    return [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]


def compute_metrics(
    equity: list[tuple[int, float]],
    trades: list[dict],
    capital: float,
    periods_per_year: int,
    bars_total: int,
    bars_in_market: int,
) -> dict:
    final = equity[-1][1] if equity else capital
    dd = drawdown_curve(equity)
    max_dd = max(0.0, -min((v for _, v in dd), default=0.0))

    rets = daily_returns(equity)
    sharpe = sortino = None
    if len(rets) >= 2:
        mean = sum(rets) / len(rets)
        std = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
        scale = math.sqrt(periods_per_year)
        sharpe = mean / std * scale if std > 0 else None
        sortino = mean / downside * scale if downside > 0 else None

    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    n = len(trades)

    return {
        "final_equity": final,
        "total_return_pct": (final / capital - 1) * 100 if capital else 0.0,
        "max_drawdown_pct": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "trades": n,
        "winrate_pct": len(wins) / n * 100 if n else None,
        # None when there are no losing trades (division by zero); the UI shows "∞" if there were wins.
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "no_losing_trades": n > 0 and gross_loss == 0,
        "avg_trade": sum(pnls) / n if n else None,
        "avg_win": gross_profit / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "expectancy_r": sum(t["r_multiple"] for t in trades) / n if n else None,
        "costs_total": sum(t["costs_total"] for t in trades),
        "exposure_pct": bars_in_market / bars_total * 100 if bars_total else 0.0,
        "avg_bars_held": sum(t["bars_held"] for t in trades) / n if n else None,
    }
