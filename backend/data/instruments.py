"""Tradable instruments and timeframes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str          # broker-style symbol, e.g. "XAUUSD"
    name: str            # human label shown in the UI
    category: str        # "metal" | "forex" | "crypto"
    digits: int          # price decimals shown in the UI
    dukascopy_code: str  # instrument code in the Dukascopy data API


INSTRUMENTS: dict[str, Instrument] = {
    i.symbol: i
    for i in [
        Instrument("XAUUSD", "Goud / US dollar", "metal", 2, "XAU-USD"),
        Instrument("XAGUSD", "Zilver / US dollar", "metal", 3, "XAG-USD"),
        Instrument("EURUSD", "Euro / US dollar", "forex", 5, "EUR-USD"),
        Instrument("GBPUSD", "Britse pond / US dollar", "forex", 5, "GBP-USD"),
        Instrument("USDJPY", "US dollar / Japanse yen", "forex", 3, "USD-JPY"),
        Instrument("EURJPY", "Euro / Japanse yen", "forex", 3, "EUR-JPY"),
        Instrument("BTCUSD", "Bitcoin / US dollar", "crypto", 1, "BTC-USD"),
        Instrument("ETHUSD", "Ether / US dollar", "crypto", 2, "ETH-USD"),
    ]
}


@dataclass(frozen=True)
class Timeframe:
    code: str        # "M15", "H1", ...
    seconds: int
    level: str       # source resolution it is built from: "m1" | "h1" | "d1"
    max_days: int    # largest range the chart loads at once (keeps the UI fast)


TIMEFRAMES: dict[str, Timeframe] = {
    t.code: t
    for t in [
        Timeframe("M1", 60, "m1", 14),
        Timeframe("M5", 5 * 60, "m1", 90),
        Timeframe("M15", 15 * 60, "m1", 366),
        Timeframe("M30", 30 * 60, "m1", 366),
        Timeframe("H1", 3600, "h1", 3 * 366),
        Timeframe("H4", 4 * 3600, "h1", 10 * 366),
        Timeframe("D1", 86400, "d1", 20 * 366),
    ]
}

# Source levels: candle size in seconds.
LEVEL_SECONDS = {"m1": 60, "h1": 3600, "d1": 86400}
