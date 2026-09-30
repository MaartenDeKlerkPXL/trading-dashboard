"""Market data providers.

A provider downloads one chunk of candles (a day of m1, a month of h1 or a
year of d1). The store decides which chunks are needed and caches them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple, Protocol

import httpx

from .instruments import LEVEL_SECONDS, Instrument

log = logging.getLogger(__name__)


class Candle(NamedTuple):
    ts: int        # candle open time, unix seconds UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


class ProviderError(RuntimeError):
    """Temporary failure (network, server error). The chunk can be retried later."""


class DataProvider(Protocol):
    name: str

    async def fetch_chunk(
        self, client: httpx.AsyncClient, instrument: Instrument, level: str, chunk_start: int, active: bool
    ) -> list[Candle] | None:
        """Return candles for the chunk, [] when the chunk has no trading, None when unknown (404)."""
        ...


class DukascopyProvider:
    """Free historical BID candles from Dukascopy's public data API (no account needed)."""

    name = "dukascopy"
    root = "https://jetta.dukascopy.com/v1"
    _source = {"m1": "minute", "h1": "hour", "d1": "day"}

    def __init__(self, retries: int = 3, backoff: float = 1.0):
        self.retries = retries
        self.backoff = backoff

    def chunk_url(self, instrument: Instrument, level: str, chunk_start: int, active: bool) -> str:
        from datetime import datetime, timezone

        base = f"{self.root}/candles/{self._source[level]}/{instrument.dukascopy_code}/BID"
        if active:
            # The running day/month/year is served incrementally from a start time in ms.
            return f"{base}?from={chunk_start * 1000}"
        d = datetime.fromtimestamp(chunk_start, tz=timezone.utc)
        if level == "m1":
            return f"{base}/{d.year}/{d.month}/{d.day}"
        if level == "h1":
            return f"{base}/{d.year}/{d.month}"
        return f"{base}/{d.year}"

    async def fetch_chunk(
        self, client: httpx.AsyncClient, instrument: Instrument, level: str, chunk_start: int, active: bool
    ) -> list[Candle] | None:
        url = self.chunk_url(instrument, level, chunk_start, active)
        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 404:
                    return None
                if resp.status_code == 200:
                    if not resp.content.strip():
                        return []
                    try:
                        return parse_candles(resp.json(), LEVEL_SECONDS[level])
                    except (ValueError, KeyError, TypeError) as exc:
                        raise ProviderError(f"Onverwacht antwoord van Dukascopy ({url}): {exc}") from exc
                last_error = f"HTTP {resp.status_code}"
            if attempt < self.retries:
                await asyncio.sleep(self.backoff * (2**attempt))
        log.warning("Dukascopy fetch failed: %s (%s)", url, last_error)
        raise ProviderError(f"Dukascopy niet bereikbaar ({last_error}). Controleer je internetverbinding en probeer het later opnieuw.")


def parse_candles(data: dict, step_seconds: int) -> list[Candle]:
    """Decode Dukascopy's delta-encoded candle JSON.

    Format: `timestamp` is the chunk start in ms, `shift` the candle size in ms.
    `times` holds step deltas between consecutive candles; `opens/highs/lows/closes`
    hold price deltas in units of `multiplier`, relative to the base values
    `open/high/low/close`. Missing steps (gaps) simply have no candle.
    """
    times = data["times"]
    n = len(times)
    columns = [data["opens"], data["highs"], data["lows"], data["closes"], data["volumes"]]
    if any(len(col) != n for col in columns):
        raise ValueError("column lengths do not match")
    if n == 0:
        return []

    mult = float(data["multiplier"])
    if mult <= 0:
        raise ValueError("multiplier must be positive")
    shift_ms = int(data.get("shift") or step_seconds * 1000)
    decimals = _decimals(mult)

    ts_ms = int(data["timestamp"])
    o = round(data["open"] / mult)
    h = round(data["high"] / mult)
    lo = round(data["low"] / mult)
    c = round(data["close"] / mult)

    out: list[Candle] = []
    for i in range(n):
        if times[i] < 0:
            raise ValueError("negative time delta")
        ts_ms += int(times[i]) * shift_ms
        o += data["opens"][i]
        h += data["highs"][i]
        lo += data["lows"][i]
        c += data["closes"][i]
        vol = float(data["volumes"][i])
        # Skip placeholder candles (no ticks at all).
        if vol == 0 and o == h == lo == c:
            continue
        out.append(
            Candle(
                ts_ms // 1000,
                round(o * mult, decimals),
                round(h * mult, decimals),
                round(lo * mult, decimals),
                round(c * mult, decimals),
                vol,
            )
        )
    return out


def _decimals(mult: float) -> int:
    text = f"{mult:.12f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def get_provider(name: str) -> DataProvider:
    if name == "dukascopy":
        return DukascopyProvider()
    raise ValueError(f"Onbekende data-provider '{name}'")
