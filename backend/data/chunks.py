"""Download chunks: data is fetched per day (m1), month (h1) or year (d1), in UTC."""

from __future__ import annotations

from datetime import datetime, timezone


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def chunk_start(ts: int, level: str) -> int:
    d = _dt(ts)
    if level == "m1":
        return _ts(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
    if level == "h1":
        return _ts(datetime(d.year, d.month, 1, tzinfo=timezone.utc))
    if level == "d1":
        return _ts(datetime(d.year, 1, 1, tzinfo=timezone.utc))
    raise ValueError(f"unknown level {level!r}")


def next_chunk(start: int, level: str) -> int:
    d = _dt(start)
    if level == "m1":
        return start + 86400
    if level == "h1":
        year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return _ts(datetime(year, month, 1, tzinfo=timezone.utc))
    if level == "d1":
        return _ts(datetime(d.year + 1, 1, 1, tzinfo=timezone.utc))
    raise ValueError(f"unknown level {level!r}")


def chunks_between(start: int, end: int, level: str) -> list[int]:
    """Start timestamps of all chunks overlapping [start, end)."""
    out = []
    cur = chunk_start(start, level)
    while cur < end:
        out.append(cur)
        cur = next_chunk(cur, level)
    return out
