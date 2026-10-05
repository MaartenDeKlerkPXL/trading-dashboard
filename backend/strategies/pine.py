"""Indicators that follow Pine Script's built-ins bar by bar (ta.ema, ta.sma, ta.wma, ta.hma, ta.vwma, ta.vwap,
ta.atr, Heikin-Ashi open, DEMA, T3).

Every indicator is a series with one value per bar (None while it is not ready, like Pine's na). It is
updated incrementally: when the history grows by one bar only that bar is computed, so calling it every
bar of a backtest stays fast. A different bar list (a new paper round) is computed from scratch.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .base import History

PINE_MA_TYPES = ("EMA", "HEMA", "SMA", "HMA", "WMA", "DEMA", "VWMA", "VWAP", "T3")
NEW_YORK = ZoneInfo("America/New_York")


class Series:
    def __init__(self):
        self._sid = None
        self.values: list[float | None] = []

    def update(self, history: History) -> list[float | None]:
        n = len(history)
        if self._sid != history.series_id or n < len(self.values):
            self._sid = history.series_id
            self.values = []
            self._reset()
        while len(self.values) < n:
            self.values.append(self._compute(history, len(self.values)))
        return self.values

    def at(self, history: History, offset: int = 0) -> float | None:
        values = self.update(history)
        i = len(history) - 1 - offset
        return values[i] if 0 <= i < len(values) else None

    def _reset(self) -> None:
        pass

    def _compute(self, history: History, i: int) -> float | None:
        raise NotImplementedError


class Source(Series):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def _compute(self, history, i):
        return self.fn(history[i])


def close_source() -> Source:
    return Source(lambda b: b.close)


class HaOpen(Series):
    """Heikin-Ashi open: (previous HA open + previous HA close) / 2, starting at (open + close) / 2."""

    def _compute(self, history, i):
        bar = history[i]
        if i == 0:
            return (bar.open + bar.close) / 2
        prev = history[i - 1]
        return (self.values[i - 1] + (prev.open + prev.high + prev.low + prev.close) / 4) / 2


class Smoothed(Series):
    """Exponential smoothing (ta.ema with alpha 2/(n+1), ta.rma with alpha 1/n), seeded with the simple
    average of the first n values of the source, like Pine."""

    def __init__(self, src: Series, length: int, alpha: float):
        super().__init__()
        self.src, self.length, self.alpha = src, length, alpha

    def _reset(self):
        self._seed: list[float] = []

    def _compute(self, history, i):
        x = self.src.update(history)[i]
        prev = self.values[i - 1] if i > 0 else None
        if x is None:
            return prev
        if prev is None:
            self._seed.append(x)
            return sum(self._seed) / self.length if len(self._seed) == self.length else None
        return self.alpha * x + (1 - self.alpha) * prev


def ema(src: Series, length: int) -> Smoothed:
    return Smoothed(src, length, 2 / (length + 1))


def rma(src: Series, length: int) -> Smoothed:
    return Smoothed(src, length, 1 / length)


class Window(Series):
    """Simple or weighted average over the last n values of a source (ta.sma, ta.wma)."""

    def __init__(self, src: Series, length: int, weighted: bool = False):
        super().__init__()
        self.src, self.length, self.weighted = src, length, weighted

    def _compute(self, history, i):
        n = self.length
        if i + 1 < n:
            return None
        window = self.src.update(history)[i + 1 - n:i + 1]
        if any(v is None for v in window):
            return None
        if self.weighted:
            return sum(v * (k + 1) for k, v in enumerate(window)) / (n * (n + 1) / 2)
        return sum(window) / n


class Combine(Series):
    def __init__(self, fn, *sources: Series):
        super().__init__()
        self.fn, self.sources = fn, sources

    def _compute(self, history, i):
        xs = [s.update(history)[i] for s in self.sources]
        return None if any(x is None for x in xs) else self.fn(*xs)


class Vwma(Series):
    """ta.vwma: sma(close × volume) / sma(volume)."""

    def __init__(self, length: int):
        super().__init__()
        self.length = length

    def _compute(self, history, i):
        n = self.length
        if i + 1 < n:
            return None
        bars = history[i + 1 - n:i + 1]
        volume = sum(b.volume for b in bars)
        if volume <= 0:
            return None
        return sum(b.close * b.volume for b in bars) / volume


class Vwap(Series):
    """ta.vwap: volume-weighted hlc3, restarting every trading day (17:00 New York, as for forex and metals)."""

    def _reset(self):
        self._day = None
        self._pv = self._v = 0.0

    def _compute(self, history, i):
        bar = history[i]
        day = (datetime.fromtimestamp(bar.ts, NEW_YORK) + timedelta(hours=7)).date()
        if day != self._day:
            self._day, self._pv, self._v = day, 0.0, 0.0
        price = (bar.high + bar.low + bar.close) / 3
        self._pv += price * bar.volume
        self._v += bar.volume
        return self._pv / self._v if self._v > 0 else price


class TrueRange(Series):
    """ta.tr(true): high - low on the first bar."""

    def _compute(self, history, i):
        bar = history[i]
        if i == 0:
            return bar.high - bar.low
        prev = history[i - 1].close
        return max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev))


def atr(length: int) -> Smoothed:
    """ta.atr: rma of the true range."""
    return rma(TrueRange(), length)


def moving_average(kind: str, length: int) -> Series:
    """The getMA() function of the 3Commas Bot script."""
    close = close_source()
    if kind == "EMA":
        return ema(close, length)
    if kind == "HEMA":
        return ema(HaOpen(), length)
    if kind == "SMA":
        return Window(close, length)
    if kind == "WMA":
        return Window(close, length, weighted=True)
    if kind == "HMA":
        half, root = max(1, length // 2), max(1, int(length ** 0.5))
        diff = Combine(lambda a, b: 2 * a - b, Window(close, half, True), Window(close, length, True))
        return Window(diff, root, weighted=True)
    if kind == "VWMA":
        return Vwma(length)
    if kind == "VWAP":
        return Vwap()
    if kind == "DEMA":
        e1 = ema(close, length)
        return Combine(lambda a, b: 2 * a - b, e1, ema(e1, length))
    if kind == "T3":
        e1 = ema(close, length)
        e2 = ema(e1, length)
        e3 = ema(e2, length)
        e4 = ema(e3, length)
        e5 = ema(e4, length)
        e6 = ema(e5, length)
        ab = 0.7
        c1 = -ab ** 3
        c2 = 3 * ab ** 2 + 3 * ab ** 3
        c3 = -6 * ab ** 2 - 3 * ab - 3 * ab ** 3
        c4 = 1 + 3 * ab + ab ** 3 + 3 * ab ** 2
        return Combine(lambda a6, a5, a4, a3: c1 * a6 + c2 * a5 + c3 * a4 + c4 * a3, e6, e5, e4, e3)
    raise ValueError(f"Onbekend soort gemiddelde '{kind}'.")


def warmup_bars(kind: str, length: int) -> int:
    """Bars before the average has a value."""
    return {"DEMA": 2 * length, "T3": 6 * length, "HMA": length + int(length ** 0.5), "VWAP": 1}.get(kind, length)
