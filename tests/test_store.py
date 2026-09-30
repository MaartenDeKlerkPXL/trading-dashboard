import asyncio
from datetime import datetime, timezone

from backend.data.chunks import chunks_between, next_chunk
from backend.data.providers import Candle
from backend.data.store import CandleStore, resample
from backend.db import connect
from tests.helpers import FakeProvider


def ts(*args):
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def test_chunks():
    assert chunks_between(ts(2024, 1, 30, 12), ts(2024, 2, 2), "m1") == [
        ts(2024, 1, 30), ts(2024, 1, 31), ts(2024, 2, 1)]
    assert chunks_between(ts(2024, 11, 15), ts(2025, 1, 2), "h1") == [ts(2024, 11, 1), ts(2024, 12, 1), ts(2025, 1, 1)]
    assert chunks_between(ts(2023, 6, 1), ts(2024, 6, 1), "d1") == [ts(2023, 1, 1), ts(2024, 1, 1)]
    assert next_chunk(ts(2024, 12, 1), "h1") == ts(2025, 1, 1)


def test_resample_ohlcv():
    base = ts(2024, 3, 4, 10)
    m1 = [
        Candle(base, 10, 12, 9, 11, 1),
        Candle(base + 60, 11, 15, 10, 14, 2),
        Candle(base + 14 * 60, 14, 14, 8, 9, 3),
        Candle(base + 15 * 60, 9, 10, 9, 10, 1),
    ]
    assert resample(m1, 900) == [Candle(base, 10, 15, 8, 9, 6), Candle(base + 900, 9, 10, 9, 10, 1)]


def test_h4_buckets_align_to_utc_midnight():
    base = ts(2024, 3, 4)
    h1 = [Candle(base + h * 3600, 1, 2, 0.5, 1.5, 1) for h in range(3, 8)]
    assert [c.ts for c in resample(h1, 4 * 3600)] == [base, base + 4 * 3600]


def make_store(tmp_path, now, provider=None):
    provider = provider or FakeProvider()
    return CandleStore(connect(tmp_path / "t.sqlite"), provider, now=lambda: now), provider


def test_sync_caches_completed_chunks(tmp_path):
    now = ts(2024, 6, 1)
    store, provider = make_store(tmp_path, now)
    start, end = ts(2024, 3, 4), ts(2024, 3, 11)  # Mon..next Mon, includes a weekend
    res = asyncio.run(store.sync("XAUUSD", "m1", start, end))
    assert res.total == 7 and res.downloaded == 7 and res.failed == 0
    assert len(provider.calls) == 7

    # Second sync downloads nothing.
    res2 = asyncio.run(store.sync("XAUUSD", "m1", start, end))
    assert res2.downloaded == 0 and res2.skipped == 7 and len(provider.calls) == 7

    m15 = store.read("XAUUSD", "M15", start, end)
    assert len(m15) == 5 * 96  # 5 trading days of 15-minute candles, weekend empty
    assert store.missing_count("XAUUSD", "m1", start, end) == 0


def test_running_chunk_is_refetched_later(tmp_path):
    now = ts(2024, 3, 6, 12)
    store, provider = make_store(tmp_path, now)
    res = asyncio.run(store.sync("XAUUSD", "m1", ts(2024, 3, 5), ts(2024, 3, 7)))
    assert res.total == 2  # the 7th is in the future relative to 'now'
    assert provider.calls[-1] == ("m1", ts(2024, 3, 6), True)
    # Candles that have not started yet are never stored.
    assert store.read("XAUUSD", "M1", ts(2024, 3, 6), ts(2024, 3, 7))[-1].ts == now

    # Immediately again: the running day is not re-downloaded (rate limit)...
    asyncio.run(store.sync("XAUUSD", "m1", ts(2024, 3, 5), ts(2024, 3, 7)))
    assert len(provider.calls) == 2
    # ...but ten minutes later it is, while the finished day stays cached.
    store.now = lambda: now + 600
    asyncio.run(store.sync("XAUUSD", "m1", ts(2024, 3, 5), ts(2024, 3, 7)))
    assert provider.calls[2:] == [("m1", ts(2024, 3, 6), True)]


def test_missing_and_failing_chunks(tmp_path):
    now = ts(2024, 6, 1)
    missing_day, failing_day = ts(2024, 3, 5), ts(2024, 3, 6)
    store, provider = make_store(tmp_path, now, FakeProvider(missing={missing_day}, failing={failing_day}))
    res = asyncio.run(store.sync("XAUUSD", "m1", ts(2024, 3, 4), ts(2024, 3, 7)))
    assert res.downloaded == 2 and res.failed == 1
    # The old missing day is final; the failed day is retried next time.
    asyncio.run(store.sync("XAUUSD", "m1", ts(2024, 3, 4), ts(2024, 3, 7)))
    assert [c[1] for c in provider.calls[3:]] == [failing_day]
    assert store.missing_count("XAUUSD", "m1", ts(2024, 3, 4), ts(2024, 3, 7)) == 1


def test_d1_and_h4_read(tmp_path):
    now = ts(2024, 6, 1)
    store, _ = make_store(tmp_path, now)
    asyncio.run(store.sync("XAUUSD", "h1", ts(2024, 3, 4), ts(2024, 3, 9)))
    h4 = store.read("XAUUSD", "H4", ts(2024, 3, 4), ts(2024, 3, 9))
    assert len(h4) == 5 * 6
    assert all(c.ts % (4 * 3600) == 0 for c in h4)
