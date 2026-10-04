"""Test helpers: build Dukascopy-style payloads and a fake provider (no network)."""

from __future__ import annotations

from backend.data.chunks import next_chunk
from backend.data.instruments import LEVEL_SECONDS
from backend.data.providers import Candle


def encode_dukascopy(candles: list[Candle], chunk_start: int, step: int, multiplier: float = 0.001) -> dict:
    """Inverse of parse_candles: produce the delta-encoded JSON the API returns."""
    if not candles:
        return {"timestamp": chunk_start * 1000, "multiplier": multiplier, "shift": step * 1000,
                "open": 0, "high": 0, "low": 0, "close": 0,
                "times": [], "opens": [], "highs": [], "lows": [], "closes": [], "volumes": []}
    units = lambda p: round(p / multiplier)  # noqa: E731
    first = candles[0]
    base = {k: units(getattr(first, k)) for k in ("open", "high", "low", "close")}
    data = {"timestamp": chunk_start * 1000, "multiplier": multiplier, "shift": step * 1000,
            **{k: v * multiplier for k, v in base.items()},
            "times": [], "opens": [], "highs": [], "lows": [], "closes": [], "volumes": []}
    prev_step = 0
    prev = dict(base)
    for c in candles:
        s = (c.ts - chunk_start) // step
        data["times"].append(s - prev_step)
        prev_step = s
        for k, col in (("open", "opens"), ("high", "highs"), ("low", "lows"), ("close", "closes")):
            u = units(getattr(c, k))
            data[col].append(u - prev[k])
            prev[k] = u
        data["volumes"].append(c.volume)
    return data


def synthetic(start: int, end: int, step: int, price: float = 2000.0, weekend_gap: bool = True) -> list[Candle]:
    """Deterministic, realistic-looking candles (a function of time, so chunks join up).

    No candles on Saturday/Sunday when weekend_gap is set.
    """
    import math
    import random

    def mid(t: float) -> float:
        return price * (1 + 0.04 * math.sin(t / (86400 * 9)) + 0.012 * math.sin(t / (86400 * 1.3))
                        + 0.002 * math.sin(t / 7200))

    out = []
    ts = start
    while ts < end:
        weekday = ((ts // 86400) + 3) % 7  # 0 = Monday (1970-01-01 was a Thursday)
        if not (weekend_gap and weekday >= 5):
            rnd = random.Random(ts)
            o, c = round(mid(ts), 5), round(mid(ts + step), 5)
            wick = price * 0.0004 * (step / 60) ** 0.5
            h = round(max(o, c) + wick * rnd.random(), 5)
            lo = round(min(o, c) - wick * rnd.random(), 5)
            out.append(Candle(ts, o, h, lo, c, round(1 + 4 * rnd.random(), 2)))
        ts += step
    return out


class FakeProvider:
    """Serves synthetic candles and records which chunks were requested."""

    name = "dukascopy"

    def __init__(self, missing: set[int] | None = None, failing: set[int] | None = None):
        self.calls: list[tuple[str, int, bool]] = []
        self.missing = missing or set()
        self.failing = failing or set()

    async def fetch_chunk(self, client, instrument, level, chunk_start, active):
        from backend.data.providers import ProviderError

        self.calls.append((level, chunk_start, active))
        if chunk_start in self.failing:
            raise ProviderError("boom")
        if chunk_start in self.missing:
            return None
        price = {"EURUSD": 1.16, "EURJPY": 172.0, "GBPUSD": 1.34, "USDJPY": 148.0}.get(instrument.symbol, 2000.0)
        return synthetic(chunk_start, next_chunk(chunk_start, level), LEVEL_SECONDS[level], price=price)
