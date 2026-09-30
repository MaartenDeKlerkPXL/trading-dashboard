import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from backend.data.instruments import INSTRUMENTS
from backend.data.providers import Candle, DukascopyProvider, ProviderError, parse_candles
from tests.helpers import encode_dukascopy

GOLD = INSTRUMENTS["XAUUSD"]
DAY = int(datetime(2024, 3, 5, tzinfo=timezone.utc).timestamp())


def test_parse_roundtrip_with_gaps():
    candles = [
        Candle(DAY + 0, 2100.123, 2100.5, 2099.9, 2100.2, 3.0),
        Candle(DAY + 60, 2100.2, 2101.0, 2100.1, 2100.9, 1.25),
        # gap of 3 minutes
        Candle(DAY + 300, 2100.9, 2100.95, 2099.0, 2099.5, 0.5),
    ]
    data = encode_dukascopy(candles, DAY, 60)
    assert parse_candles(data, 60) == candles


def test_parse_skips_placeholder_candles():
    candles = [Candle(DAY, 10.0, 10.0, 10.0, 10.0, 0.0), Candle(DAY + 60, 10.0, 11.0, 9.0, 10.5, 2.0)]
    parsed = parse_candles(encode_dukascopy(candles, DAY, 60), 60)
    assert parsed == [candles[1]]


def test_parse_empty_and_invalid():
    assert parse_candles(encode_dukascopy([], DAY, 60), 60) == []
    bad = encode_dukascopy([Candle(DAY, 1, 2, 0.5, 1.5, 1)], DAY, 60)
    bad["opens"].append(1)
    with pytest.raises(ValueError):
        parse_candles(bad, 60)


def test_parse_forex_precision():
    candles = [Candle(DAY, 1.08512, 1.08530, 1.08501, 1.08522, 12.0)]
    data = encode_dukascopy(candles, DAY, 60, multiplier=0.00001)
    assert parse_candles(data, 60) == candles


def test_urls_use_one_based_month_and_active_from():
    p = DukascopyProvider()
    base = "https://jetta.dukascopy.com/v1/candles"
    assert p.chunk_url(GOLD, "m1", DAY, False) == f"{base}/minute/XAU-USD/BID/2024/3/5"
    month = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    assert p.chunk_url(GOLD, "h1", month, False) == f"{base}/hour/XAU-USD/BID/2024/1"
    assert p.chunk_url(GOLD, "d1", month, False) == f"{base}/day/XAU-USD/BID/2024"
    assert p.chunk_url(GOLD, "m1", DAY, True) == f"{base}/minute/XAU-USD/BID?from={DAY * 1000}"


def _fetch(handler, provider=None):
    provider = provider or DukascopyProvider(retries=1, backoff=0)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await provider.fetch_chunk(client, GOLD, "m1", DAY, False)

    return asyncio.run(run())


def test_fetch_ok_404_and_retry():
    candles = [Candle(DAY, 2000.0, 2001.0, 1999.0, 2000.5, 1.0)]
    assert _fetch(lambda req: httpx.Response(200, json=encode_dukascopy(candles, DAY, 60))) == candles
    assert _fetch(lambda req: httpx.Response(404)) is None

    attempts = []

    def flaky(req):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=encode_dukascopy(candles, DAY, 60))

    assert _fetch(flaky) == candles and len(attempts) == 2


def test_fetch_gives_up_with_provider_error():
    with pytest.raises(ProviderError):
        _fetch(lambda req: httpx.Response(500))
