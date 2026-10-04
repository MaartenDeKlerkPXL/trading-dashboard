"""Indicator helpers. They only read the History they are given, so they cannot look ahead."""

from __future__ import annotations

from .base import History


def sma(history: History, period: int, offset: int = 0) -> float | None:
    """Simple moving average of closes, `offset` bars back (0 = ending at the latest bar)."""
    end = len(history) - offset
    if period <= 0 or end < period:
        return None
    window = history[end - period:end]
    return sum(b.close for b in window) / period


def atr(history: History, period: int) -> float | None:
    """Average True Range over the last `period` bars (simple average of true ranges)."""
    n = len(history)
    if n < period + 1:
        return None
    bars = history[n - period - 1:n]
    total = 0.0
    for prev, cur in zip(bars, bars[1:]):
        total += max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
    return total / period
