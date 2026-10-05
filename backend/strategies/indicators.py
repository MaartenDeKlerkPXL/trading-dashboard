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


def highest(history: History, period: int, offset: int = 1) -> float | None:
    """Highest high of `period` bars, ending `offset` bars back (1 = excluding the latest bar)."""
    end = len(history) - offset
    if period <= 0 or end < period:
        return None
    return max(b.high for b in history[end - period:end])


def lowest(history: History, period: int, offset: int = 1) -> float | None:
    """Lowest low of `period` bars, ending `offset` bars back (1 = excluding the latest bar)."""
    end = len(history) - offset
    if period <= 0 or end < period:
        return None
    return min(b.low for b in history[end - period:end])


def stdev(history: History, period: int, offset: int = 0) -> float | None:
    """Population standard deviation of closes (like Pine's ta.stdev with biased=true)."""
    end = len(history) - offset
    if period <= 0 or end < period:
        return None
    closes = [b.close for b in history[end - period:end]]
    mean = sum(closes) / period
    return (sum((c - mean) ** 2 for c in closes) / period) ** 0.5


def rsi_values(history: History, period: int, count: int = 1, lookback: int | None = None) -> list[float] | None:
    """The last `count` values of Wilder's RSI (like Pine's ta.rsi), oldest first, in one pass.

    Wilder smoothing forgets old data exponentially, so it is computed over a recent window
    (default max(8 × period, 100) bars) instead of the full history. After that many bars
    the difference with a full-history RSI is negligible, while each call stays fast.
    """
    end = len(history)
    window = (lookback or max(8 * period, 100)) + count
    if period <= 0 or end < period + count:
        return None
    bars = history[max(0, end - window - 1):end]
    gains, losses = [], []
    for prev, cur in zip(bars, bars[1:]):
        change = cur.close - prev.close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    values = [_rsi(avg_gain, avg_loss)]
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        values.append(_rsi(avg_gain, avg_loss))
    return values[-count:] if len(values) >= count else None


def rsi(history: History, period: int, lookback: int | None = None) -> float | None:
    values = rsi_values(history, period, 1, lookback)
    return values[-1] if values else None


def _rsi(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


class RsiTracker:
    """Wilder's RSI computed exactly like Pine's ta.rsi (from the first bar), updated incrementally.

    Call `update(history)` once per bar. When bars arrive one at a time (backtest, paper,
    live) each update is O(1); anything unexpected triggers a full, exact recalculation.
    """

    def __init__(self, period: int):
        self.period = period
        self._series = None
        self._n = 0
        self._changes = 0
        self._sum_gain = self._sum_loss = 0.0
        self._avg_gain = self._avg_loss = None
        self.values: list[float] = []   # RSI per bar, None-free; last item is the latest

    def update(self, history: History) -> list[float]:
        n = len(history)
        if self._series != history.series_id or n < self._n or n > self._n + 1:
            self.__init__(self.period)
            self._series = history.series_id
            for i in range(1, n):
                self._add(history[i].close - history[i - 1].close)
            self._n = n
        elif n == self._n + 1:
            if n >= 2:
                self._add(history[-1].close - history[-2].close)
            self._n = n
        return self.values

    def _add(self, change: float) -> None:
        gain, loss = max(change, 0.0), max(-change, 0.0)
        p = self.period
        self._changes += 1
        if self._avg_gain is None:
            self._sum_gain += gain
            self._sum_loss += loss
            if self._changes == p:
                self._avg_gain, self._avg_loss = self._sum_gain / p, self._sum_loss / p
                self.values.append(_rsi(self._avg_gain, self._avg_loss))
            return
        self._avg_gain = (self._avg_gain * (p - 1) + gain) / p
        self._avg_loss = (self._avg_loss * (p - 1) + loss) / p
        self.values.append(_rsi(self._avg_gain, self._avg_loss))
        if len(self.values) > 4:
            del self.values[0]


# ---------- moving averages and ATR as in Pine Script ----------

MA_TYPES = ("SMA", "EMA", "WMA", "HMA", "RMA", "VWMA")


def _wma(values: list[float]) -> float:
    n = len(values)
    return sum(v * (i + 1) for i, v in enumerate(values)) / (n * (n + 1) / 2)


class _Recursive:
    """A smoothed series updated bar by bar (EMA, RMA), seeded with the simple average of the first
    `period` values like Pine's ta.rma. Keeps one value per bar so any bar can be looked up."""

    def __init__(self, period: int, alpha: float, source):
        self.period, self.alpha, self.source = period, alpha, source
        self._series = None
        self.values: list[float | None] = []
        self._seed: list[float] = []

    def update(self, history: History) -> list[float | None]:
        n = len(history)
        if self._series != history.series_id or n < len(self.values) or n > len(self.values) + 1:
            self._series, self.values, self._seed = history.series_id, [], []
            for i in range(n):
                self._add(history, i)
        elif n == len(self.values) + 1:
            self._add(history, n - 1)
        return self.values

    def _add(self, history: History, i: int) -> None:
        x = self.source(history, i)
        prev = self.values[-1] if self.values else None
        if prev is None:
            self._seed.append(x)
            self.values.append(sum(self._seed) / self.period if len(self._seed) == self.period else None)
        else:
            self.values.append(self.alpha * x + (1 - self.alpha) * prev)


def _close(history: History, i: int) -> float:
    return history[i].close


def true_range(history: History, i: int) -> float:
    bar = history[i]
    if i == 0:
        return bar.high - bar.low
    prev = history[i - 1].close
    return max(bar.high - bar.low, abs(bar.high - prev), abs(bar.low - prev))


class MovingAverage:
    """Pine-style moving averages of the close: SMA, EMA, WMA, HMA, RMA and VWMA.

    value(history, offset) is the average `offset` bars back. EMA and RMA are updated incrementally,
    so calling this every bar stays fast."""

    def __init__(self, kind: str, period: int):
        if kind not in MA_TYPES:
            raise ValueError(f"Onbekend soort gemiddelde '{kind}'.")
        self.kind, self.period = kind, period
        if kind == "EMA":
            self._rec = _Recursive(period, 2 / (period + 1), _close)
        elif kind == "RMA":
            self._rec = _Recursive(period, 1 / period, _close)
        else:
            self._rec = None

    def warmup(self) -> int:
        if self.kind == "HMA":
            return self.period + int(self.period ** 0.5)
        return self.period

    def value(self, history: History, offset: int = 0) -> float | None:
        end = len(history) - offset          # the average over bars [.., end)
        p = self.period
        if p <= 0 or end < 1:
            return None
        if self._rec is not None:
            values = self._rec.update(history)
            return values[end - 1] if end - 1 < len(values) else None
        if self.kind == "HMA":
            half, root = max(1, p // 2), max(1, int(p ** 0.5))
            if end < p + root - 1:
                return None
            diffs = []
            for e in range(end - root + 1, end + 1):
                closes = [b.close for b in history[e - p:e]]
                diffs.append(2 * _wma(closes[-half:]) - _wma(closes))
            return _wma(diffs)
        if end < p:
            return None
        window = history[end - p:end]
        if self.kind == "SMA":
            return sum(b.close for b in window) / p
        if self.kind == "WMA":
            return _wma([b.close for b in window])
        volume = sum(b.volume for b in window)       # VWMA
        if volume <= 0:
            return sum(b.close for b in window) / p
        return sum(b.close * b.volume for b in window) / volume


class AtrRma:
    """ATR exactly like Pine's ta.atr: Wilder's smoothing (RMA) of the true range. value(history, offset)."""

    def __init__(self, period: int):
        self._rec = _Recursive(period, 1 / period, true_range)

    def value(self, history: History, offset: int = 0) -> float | None:
        values = self._rec.update(history)
        i = len(history) - 1 - offset
        return values[i] if 0 <= i < len(values) else None
