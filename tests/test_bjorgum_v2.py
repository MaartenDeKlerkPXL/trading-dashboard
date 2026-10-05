"""3Commas Bot v2: checked trade by trade against an independent emulation of the original Pine Script."""

import math

import pytest

from backend.backtest.engine import BacktestConfig, RateSeries, run_backtest
from backend.data.instruments import INSTRUMENTS
from backend.data.providers import Candle
from backend.execution.base import CostModel, SizingRules
from backend.paper.session import SessionConfig, advance, new_state
from backend.strategies import describe, load_strategies
from backend.strategies.base import Bar, History
from backend.strategies.bjorgum_3commas_v2 import in_session
from backend.strategies.pine import PINE_MA_TYPES, HaOpen, Vwap, moving_average
from tests.helpers import synthetic

GOLD = INSTRUMENTS["XAUUSD"]
USD = RateSeries.constant(1.0, "USD")
MON = 1709510400
V2 = load_strategies()["bjorgum_3commas@v2"]
TINY = CostModel(1e-9, 1e-9, 0.0, 0.0)          # practically free, so fills equal Pine's prices
CONFIG = BacktestConfig(10_000.0, TINY, SizingRules(2.0, "fractional", 100))


def data(days=25, step=300):
    return [Bar(*c) for c in synthetic(MON, MON + days * 86400, step)]


# ---------- an independent, literal emulation of the Pine Script ----------

def _ema(xs, n):
    out, seed, prev = [], [], None
    for x in xs:
        if x is None:
            out.append(prev)
            continue
        if prev is None:
            seed.append(x)
            prev = sum(seed) / n if len(seed) == n else None
        else:
            prev = 2 / (n + 1) * x + (1 - 2 / (n + 1)) * prev
        out.append(prev)
    return out


def _sma(xs, n):
    return [None if i + 1 < n else sum(xs[i + 1 - n:i + 1]) / n for i in range(len(xs))]


def _atr(bars, n):
    trs = [bars[0].high - bars[0].low] + [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
                                          for a, b in zip(bars, bars[1:])]
    out, seed, prev = [], [], None
    for tr in trs:
        if prev is None:
            seed.append(tr)
            prev = sum(seed) / n if len(seed) == n else None
        else:
            prev = (prev * (n - 1) + tr) / n
        out.append(prev)
    return out


def pine(bars, p):
    """The script's logic, statement by statement, with its `var` state. Orders fill on the next bar:
    entries at the open, exits at their stop/limit level (at the open on a gap; stop first if both)."""
    closes = [b.close for b in bars]
    ma = {"EMA": _ema, "SMA": _sma}
    ma1, ma2 = ma[p["ma_type1"]](closes, p["ma_len1"]), ma[p["ma_type2"]](closes, p["ma_len2"])
    atr = _atr(bars, p["atr_len"])
    lb = p["swing_lookback"]

    pos = None                 # strategy.position_size sign
    look_for_exit = False
    trade_stop = trade_target = trigger = 0.0
    trailing = 0.0
    pending = None             # entry order for the next bar
    exit_stop = exit_limit = None
    trades, entry_ts = [], None

    for i, bar in enumerate(bars):
        # --- orders from the previous bar ---
        if pending:
            if pos and pos != pending:
                trades.append((pos, entry_ts, bar.ts, "reverse"))
            pos, entry_ts, pending = pending, bar.ts, None
        if pos and exit_stop is not None:
            hit = None
            if pos == "long":
                if bar.open <= exit_stop or bar.low <= exit_stop:
                    hit = "stop"
                elif exit_limit is not None and bar.high >= exit_limit:
                    hit = "limit"
            else:
                if bar.open >= exit_stop or bar.high >= exit_stop:
                    hit = "stop"
                elif exit_limit is not None and bar.low <= exit_limit:
                    hit = "limit"
            if hit:
                trades.append((pos, entry_ts, bar.ts, hit))
                pos = None

        # --- the script on the bar close ---
        if any(v is None for v in (ma1[i], ma2[i], atr[i])) or i == 0 or None in (ma1[i - 1], ma2[i - 1]):
            continue
        lowest = min(b.low for b in bars[max(0, i + 1 - lb):i + 1])
        highest = max(b.high for b in bars[max(0, i + 1 - lb):i + 1])
        within = not p["use_time_filter"] or not in_session(bar.ts, p["ignore_from"], p["ignore_until"])
        valid_long = ma1[i] > ma2[i] and ma1[i - 1] <= ma2[i - 1]
        valid_short = ma1[i] < ma2[i] and ma1[i - 1] >= ma2[i - 1]

        src = {"Close": bars[i - 1].close, "Open": bars[i - 1].open}
        if pos == "long" and p["trail_stop"] and look_for_exit:
            trailing = max(trailing, src.get(p["trail_source"], lowest) - atr[i] * p["trail_mult"])
        if pos == "short" and p["trail_stop"] and look_for_exit:
            trailing = min(trailing, src.get(p["trail_source"], highest) + atr[i] * p["trail_mult"])

        long_stop, short_stop = lowest - atr[i] * p["risk_adj"], highest + atr[i] * p["risk_adj"]
        long_limit = bar.close + p["rr"] * (bar.close - long_stop)
        short_limit = bar.close - p["rr"] * (short_stop - bar.close)

        if valid_short and (pos is None or (pos == "long" and p["flip"])) and within and p["short_trades"]:
            trade_stop, trade_target = short_stop, short_limit if p["use_limit"] else None
            trigger = bar.close + (short_limit - bar.close) * p["rr_exit"]
            look_for_exit, trailing = False, trade_stop
        else:
            valid_short = False
        if valid_long and (pos is None or (pos == "short" and p["flip"])) and within and p["long_trades"]:
            trade_stop, trade_target = long_stop, long_limit if p["use_limit"] else None
            trigger = bar.close - (bar.close - long_limit) * p["rr_exit"]
            look_for_exit, trailing = False, trade_stop
        else:
            valid_long = False

        if (pos == "long" and p["rr_exit"] != 0 and bar.high >= trigger and p["trail_stop"]) or \
                (p["rr_exit"] == 0 and p["trail_stop"]):
            look_for_exit = True
        if (pos == "short" and p["rr_exit"] != 0 and bar.low <= trigger and p["trail_stop"]) or \
                (p["rr_exit"] == 0 and p["trail_stop"]):
            look_for_exit = True

        if valid_long:
            pending = "long"
        if valid_short:
            pending = "short"
        exit_stop = trailing if p["trail_stop"] else trade_stop
        exit_limit = trade_target
    return trades


def engine(bars, p):
    result = run_backtest(bars, V2(**p), GOLD, CONFIG, USD, close_at_end=False)
    out = []
    for t in result["trades"]:
        reason = t["exit_reason"]
        kind = "stop" if reason.startswith("Stop-loss") else "limit" if reason.startswith("Take-profit") else "reverse"
        out.append((t["side"], t["entry_ts"], t["exit_ts"], kind))
    return out


DEFAULTS = V2().p

SCENARIOS = {
    "standaard (zoals op TradingView)": {},
    "trailing vanaf de instap": {"trail_stop": True},
    "trailing vanaf 50% zonder koersdoel": {"trail_stop": True, "use_limit": False, "rr_exit": 0.5},
    "trailing op slotkoers": {"trail_stop": True, "trail_source": "Close", "trail_mult": 2.0},
    "trailing op openingskoers, alleen long": {"trail_stop": True, "trail_source": "Open", "short_trades": False},
    "omkeertrades": {"flip": True, "rr": 2.0},
    "SMA 10/30": {"ma_type1": "SMA", "ma_type2": "SMA", "ma_len1": 10, "ma_len2": 30},
    "tijdfilter": {"use_time_filter": True, "ignore_from": 2200, "ignore_until": 600},
    "andere stop": {"swing_lookback": 12, "risk_adj": 0.3, "atr_len": 7, "rr": 1.5},
}


@pytest.mark.parametrize("name", SCENARIOS)
def test_same_trades_as_the_pine_script(name):
    bars = data()
    p = {**DEFAULTS, **SCENARIOS[name]}
    expected = pine(bars, p)
    assert len(expected) >= 10, name
    assert engine(bars, SCENARIOS[name]) == expected


# ---------- details ----------

def test_defaults_are_the_scripts_defaults():
    p = V2().p
    assert (p["ma_type1"], p["ma_len1"], p["ma_type2"], p["ma_len2"]) == ("EMA", 21, "EMA", 50)
    assert (p["rr"], p["risk_adj"], p["swing_lookback"], p["atr_len"]) == (1.0, 1.0, 5, 14)
    assert (p["use_limit"], p["trail_stop"], p["flip"], p["trail_source"], p["rr_exit"]) == (
        True, False, False, "High/Low", 0.0)
    assert (p["use_time_filter"], p["ignore_from"], p["ignore_until"]) == (False, 0, 300)
    info = describe(V2)
    assert next(x for x in info["params"] if x["name"] == "ma_type1")["choices"] == list(PINE_MA_TYPES)


def test_no_reversal_by_default():
    result = run_backtest(data(), V2(), GOLD, CONFIG, USD)
    assert all(t["exit_reason"].startswith(("Stop-loss", "Take-profit", "Einde")) for t in result["trades"])


def test_time_filter_session_is_gmt_minus_6():
    gmt6_midnight = MON + 6 * 3600                 # 00:00 GMT-6 = 06:00 UTC
    assert in_session(gmt6_midnight, 0, 300) and in_session(gmt6_midnight + 179 * 60, 0, 300)
    assert not in_session(gmt6_midnight + 180 * 60, 0, 300)
    assert in_session(gmt6_midnight - 60, 2200, 600) and not in_session(gmt6_midnight + 7 * 3600, 2200, 600)
    result = run_backtest(data(), V2(use_time_filter=True), GOLD, CONFIG, USD)
    signals = [e for e in result["events"] if e["kind"] == "signal" and e["action"] in ("long", "short")]
    assert signals and not any(in_session(e["ts"], 0, 300) for e in signals)


@pytest.mark.parametrize("kind", PINE_MA_TYPES)
def test_every_average_type_runs(kind):
    other = "EMA" if kind == "VWAP" else kind       # VWAP has no length: two VWAPs are the same line
    result = run_backtest(data(), V2(ma_type1=kind, ma_type2=other, ma_len1=10, ma_len2=30), GOLD, CONFIG, USD)
    assert result["trades"], kind


def test_pine_indicators():
    bars = data(5)
    closes = [b.close for b in bars]
    h = History(bars, len(bars))
    assert moving_average("EMA", 21).at(h) == pytest.approx(_ema(closes, 21)[-1])
    e1 = _ema(closes, 9)
    dema = 2 * e1[-1] - _ema(e1, 9)[-1]
    assert moving_average("DEMA", 9).at(h) == pytest.approx(dema)
    chain = [closes]
    for _ in range(6):
        chain.append(_ema(chain[-1], 5))
    ab = 0.7
    t3 = (-ab ** 3) * chain[6][-1] + (3 * ab ** 2 + 3 * ab ** 3) * chain[5][-1] \
        + (-6 * ab ** 2 - 3 * ab - 3 * ab ** 3) * chain[4][-1] + (1 + 3 * ab + ab ** 3 + 3 * ab ** 2) * chain[3][-1]
    assert moving_average("T3", 5).at(h) == pytest.approx(t3)
    ha = [(bars[0].open + bars[0].close) / 2]
    for a, b in zip(bars, bars[1:]):
        ha.append((ha[-1] + (a.open + a.high + a.low + a.close) / 4) / 2)
    assert HaOpen().at(h) == pytest.approx(ha[-1])
    assert moving_average("HEMA", 10).at(h) == pytest.approx(_ema(ha, 10)[-1])
    # VWAP restarts at 17:00 New York (22:00 UTC in winter).
    vwap = Vwap()
    first_of_day = next(i for i, b in enumerate(bars) if b.ts == MON + 22 * 3600)
    b = bars[first_of_day]
    assert vwap.update(h)[first_of_day] == pytest.approx((b.high + b.low + b.close) / 3)
    # Bar by bar gives the same as all at once.
    m = moving_average("T3", 5)
    for n in range(1, len(bars) + 1):
        last = m.at(History(bars, n))
    assert last == pytest.approx(t3)


def test_paper_with_trailing_one_tick_equals_many_ticks():
    m1 = [Candle(*c) for c in synthetic(MON - 4 * 86400, MON + 3 * 86400, 60)]
    cfg = SessionConfig(1, "XAUUSD", "M5", V2.key(), {"trail_stop": True, "ma_len1": 9, "ma_len2": 21}, 1000.0, 2.0,
                        "fractional", 100, {"spread": 0.3, "slippage": 0.1, "commission_per_lot": 0.0,
                                            "financing_pct": 0.0})
    start, end = MON + 3600, MON + 3 * 86400

    def run(times):
        state, trades = new_state(start), {}
        for now in times:
            res = advance(cfg, state, [c for c in m1 if c.ts <= now], now, USD, V2, GOLD)
            state = res.state
            trades.update({t["id"]: t for t in res.trades})
        return state, [trades[k] for k in sorted(trades)]

    one_state, one = run([end])
    many_state, many = run(list(range(start + 30, end, 300)) + [end])
    assert one and one == many and one_state["account"] == many_state["account"]
    assert math.isfinite(one_state["account"]["balance"])
