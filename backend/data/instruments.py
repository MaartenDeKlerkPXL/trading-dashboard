"""Tradable instruments and timeframes.

Contract specs and default costs approximate a BlackBull Markets *Standard*
account (no commission, costs are in the spread). They are estimates until
they can be read from the broker itself (cTrader symbol info, phase 4).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str            # broker-style symbol, e.g. "XAUUSD"
    name: str              # human label shown in the UI
    category: str          # "metal" | "forex" | "crypto"
    digits: int            # price decimals shown in the UI
    dukascopy_code: str    # instrument code in the Dukascopy data API
    quote_currency: str    # currency the price (and P&L) is expressed in
    contract_size: float   # units per 1.00 lot
    min_lot: float = 0.01
    lot_step: float = 0.01
    max_leverage: int = 100
    # Default trading costs, in price units (spread/slippage) and EUR (commission).
    spread: float = 0.0
    slippage: float = 0.0
    commission_per_lot: float = 0.0  # EUR per lot per side


INSTRUMENTS: dict[str, Instrument] = {
    i.symbol: i
    for i in [
        Instrument("XAUUSD", "Goud / US dollar", "metal", 2, "XAU-USD", "USD", 100,
                   spread=0.30, slippage=0.10),
        Instrument("XAGUSD", "Zilver / US dollar", "metal", 3, "XAG-USD", "USD", 5000,
                   spread=0.030, slippage=0.010),
        Instrument("EURUSD", "Euro / US dollar", "forex", 5, "EUR-USD", "USD", 100_000,
                   spread=0.00012, slippage=0.00002),
        Instrument("GBPUSD", "Britse pond / US dollar", "forex", 5, "GBP-USD", "USD", 100_000,
                   spread=0.00016, slippage=0.00003),
        Instrument("USDJPY", "US dollar / Japanse yen", "forex", 3, "USD-JPY", "JPY", 100_000,
                   spread=0.014, slippage=0.003),
        Instrument("EURJPY", "Euro / Japanse yen", "forex", 3, "EUR-JPY", "JPY", 100_000,
                   spread=0.020, slippage=0.004),
        Instrument("BTCUSD", "Bitcoin / US dollar", "crypto", 1, "BTC-USD", "USD", 1,
                   max_leverage=10, spread=35.0, slippage=10.0),
        Instrument("ETHUSD", "Ether / US dollar", "crypto", 2, "ETH-USD", "USD", 1,
                   max_leverage=10, spread=2.5, slippage=0.8),
    ]
}

# Instrument whose price converts a quote currency into EUR (price = quote units per 1 EUR).
EUR_CONVERSION = {"USD": "EURUSD", "JPY": "EURJPY"}
# Used only when no conversion data is available; the result then shows a warning.
FALLBACK_EUR_RATES = {"USD": 1.16, "JPY": 172.0, "EUR": 1.0}


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
