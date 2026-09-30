"""Candle cache in SQLite: decides what to download, stores it, and serves any timeframe."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .chunks import chunks_between, next_chunk
from .instruments import INSTRUMENTS, LEVEL_SECONDS, TIMEFRAMES
from .providers import Candle, DataProvider, ProviderError

log = logging.getLogger(__name__)

# A finished period is only treated as final after this delay, because the
# provider may still be completing it.
SETTLE_SECONDS = 6 * 3600
# A missing (404) chunk older than this is considered permanently empty.
MISSING_IS_FINAL_AFTER = 7 * 86400
# Incomplete (running) chunks are re-downloaded at most this often.
REFRESH_INCOMPLETE_AFTER = 5 * 60


@dataclass
class SyncResult:
    total: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] | None = None


def resample(candles: list[Candle], seconds: int) -> list[Candle]:
    """Aggregate candles into buckets of `seconds`, aligned to UTC epoch."""
    out: list[Candle] = []
    cur: list | None = None
    for c in candles:
        bucket = c.ts - c.ts % seconds
        if cur is None or cur[0] != bucket:
            if cur is not None:
                out.append(Candle(*cur))
            cur = [bucket, c.open, c.high, c.low, c.close, c.volume]
        else:
            cur[2] = max(cur[2], c.high)
            cur[3] = min(cur[3], c.low)
            cur[4] = c.close
            cur[5] += c.volume
    if cur is not None:
        out.append(Candle(*cur))
    return out


class CandleStore:
    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: DataProvider,
        now: Callable[[], float] = time.time,
        concurrency: int = 5,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.conn = conn
        self.provider = provider
        self.now = now
        self.concurrency = concurrency
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=30, headers={"User-Agent": "trading-dashboard/0.1"})
        )

    # ---------- planning ----------

    def plan(self, symbol: str, level: str, start: int, end: int) -> tuple[list[tuple[int, bool]], int]:
        """Chunks that must be downloaded, as (chunk_start, is_active), plus the total chunk count."""
        now = int(self.now())
        known = {
            row["chunk_start"]: row
            for row in self.conn.execute(
                "SELECT chunk_start, complete, fetched_at FROM fetched_chunks "
                "WHERE source=? AND symbol=? AND level=? AND chunk_start>=? AND chunk_start<?",
                (self.provider.name, symbol, level, start - 400 * 86400, end),
            )
        }
        todo, total = [], 0
        for cs in chunks_between(start, min(end, now + 1), level):
            total += 1
            ce = next_chunk(cs, level)
            active = cs <= now < ce
            row = known.get(cs)
            if row is not None:
                if row["complete"]:
                    continue
                if now - row["fetched_at"] < REFRESH_INCOMPLETE_AFTER:
                    continue
            todo.append((cs, active))
        return todo, total

    # ---------- downloading ----------

    async def sync(
        self,
        symbol: str,
        level: str,
        start: int,
        end: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> SyncResult:
        instrument = INSTRUMENTS[symbol]
        todo, total = self.plan(symbol, level, start, end)
        result = SyncResult(total=total, skipped=total - len(todo), errors=[])
        done = result.skipped
        if progress:
            progress(done, total)
        if not todo:
            return result

        sem = asyncio.Semaphore(self.concurrency)
        async with self.client_factory() as client:

            async def one(cs: int, active: bool) -> None:
                nonlocal done
                async with sem:
                    try:
                        candles = await self.provider.fetch_chunk(client, instrument, level, cs, active)
                    except ProviderError as exc:
                        result.failed += 1
                        if len(result.errors) < 5:
                            result.errors.append(str(exc))
                    else:
                        self._save_chunk(symbol, level, cs, active, candles)
                        result.downloaded += 1
                    done += 1
                    if progress:
                        progress(done, total)

            await asyncio.gather(*(one(cs, active) for cs, active in todo))
        return result

    def _save_chunk(self, symbol: str, level: str, cs: int, active: bool, candles: list[Candle] | None) -> None:
        now = int(self.now())
        ce = next_chunk(cs, level)
        if candles is None:
            complete = ce < now - MISSING_IS_FINAL_AFTER
            candles = []
        else:
            complete = (not active) and ce <= now - SETTLE_SECONDS
        # Keep only candles inside the chunk that have actually started.
        rows = [
            (self.provider.name, symbol, level, *c)
            for c in candles
            if cs <= c.ts < ce and c.ts <= now
        ]
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO candles (source, symbol, level, ts, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO fetched_chunks (source, symbol, level, chunk_start, fetched_at, complete, candles) "
                "VALUES (?,?,?,?,?,?,?)",
                (self.provider.name, symbol, level, cs, now, int(complete), len(rows)),
            )

    # ---------- reading ----------

    def read(self, symbol: str, timeframe: str, start: int, end: int) -> list[Candle]:
        tf = TIMEFRAMES[timeframe]
        start -= start % tf.seconds
        rows = self.conn.execute(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE source=? AND symbol=? AND level=? AND ts>=? AND ts<? ORDER BY ts",
            (self.provider.name, symbol, tf.level, start, end),
        ).fetchall()
        candles = [Candle(*r) for r in rows]
        if tf.seconds == LEVEL_SECONDS[tf.level]:
            return candles
        return resample(candles, tf.seconds)

    def missing_count(self, symbol: str, level: str, start: int, end: int) -> int:
        todo, _ = self.plan(symbol, level, start, end)
        # Running chunks are always "refreshable"; only count chunks never downloaded.
        fetched = {
            r[0]
            for r in self.conn.execute(
                "SELECT chunk_start FROM fetched_chunks WHERE source=? AND symbol=? AND level=?",
                (self.provider.name, symbol, level),
            )
        }
        return sum(1 for cs, _ in todo if cs not in fetched)
