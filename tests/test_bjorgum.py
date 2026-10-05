"""The 3Commas Bot (Bjorgum) strategy, Pine-style moving averages, pick-list parameters and trailing stops."""

import math

import pytest

from backend.backtest.engine import BacktestConfig, RateSeries, run_backtest
from backend.data.instruments import INSTRUMENTS
from backend.data.providers import Candle
from backend.execution.backtest import BacktestExecutor
from backend.execution.base import CostModel, OrderRequest, SizingRules
from backend.execution.events import EventLog
from backend.paper.session import SessionConfig, advance, new_state
from backend.risk import RiskLimits
from backend.strategies import describe, load_strategies
from backend.strategies.base import Bar, History
from backend.strategies.indicators import MA_TYPES, AtrRma, MovingAverage
from tests.helpers import synthetic

GOLD = INSTRUMENTS["XAUUSD"]
USD = RateSeries.constant(1.0, "USD")
MON = 1709510400
BOT = load_strategies()["bjorgum_3commas@v1"]
CONFIG = BacktestConfig(1000.0, CostModel(0.3, 0.1, 0.0, 0.0), SizingRules(2.0, "fractional", 100))


def bars(step=900, days=40):
    return [Bar(*c) for c in synthetic(MON, MON + days * 86400, step)]


# ---------- indicators ----------

def test_moving_averages_match_reference_formulas():
    data = bars(3600, 20)
    closes = [b.close for b in data]
    n, p = len(data), 10
    h = History(data, n)
    assert MovingAverage("SMA", p).value(h) == pytest.approx(sum(closes[-p:]) / p)
    weights = range(1, p + 1)
    assert MovingAverage("WMA", p).value(h) == pytest.approx(sum(w * c for w, c in zip(weights, closes[-p:])) / sum(weights))
    ema = sum(closes[:p]) / p                       # seeded with the simple average, then recursive
    for c in closes[p:]:
        ema = 2 / (p + 1) * c + (1 - 2 / (p + 1)) * ema
    assert MovingAverage("EMA", p).value(h) == pytest.approx(ema)
    # Bar by bar (as in a backtest) gives the same as one call on the full history.
    m = MovingAverage("EMA", p)
    for i in range(1, n + 1):
        last = m.value(History(data, i))
    assert last == pytest.approx(ema) and m.value(History(data, n), 3) == pytest.approx(
        MovingAverage("EMA", p).value(History(data, n - 3)))

    def wma(xs):
        return sum((i + 1) * x for i, x in enumerate(xs)) / (len(xs) * (len(xs) + 1) / 2)

    half, root = p // 2, int(math.sqrt(p))
    diffs = [2 * wma(closes[e - half:e]) - wma(closes[e - p:e]) for e in range(n - root + 1, n + 1)]
    assert MovingAverage("HMA", p).value(h) == pytest.approx(wma(diffs))


def test_atr_is_wilder_smoothing_of_true_range():
    data = bars(3600, 10)
    p = 14
    trs = [data[0].high - data[0].low] + [max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
                                          for a, b in zip(data, data[1:])]
    atr = sum(trs[:p]) / p
    for tr in trs[p:]:
        atr = (atr * (p - 1) + tr) / p
    assert AtrRma(p).value(History(data, len(data))) == pytest.approx(atr)


# ---------- the strategy ----------

def test_strategy_is_listed_with_a_pick_list():
    info = describe(BOT)
    ma = next(p for p in info["params"] if p["name"] == "ma_type")
    assert ma["type"] == "choice" and ma["choices"] == list(MA_TYPES) and ma["default"] == "EMA"
    with pytest.raises(ValueError):
        BOT(ma_type="XYZ")
    with pytest.raises(ValueError):
        BOT(fast=30, slow=20)


@pytest.mark.parametrize("ma_type", MA_TYPES)
def test_backtest_runs_with_every_average(ma_type):
    result = run_backtest(bars(), BOT(ma_type=ma_type), GOLD, CONFIG, USD)
    trades = result["trades"]
    assert len(trades) >= 5, ma_type
    for t in trades:
        if t["side"] == "long":
            assert t["stop_loss"] < t["entry_price"] < t["take_profit"]
        else:
            assert t["take_profit"] < t["entry_price"] < t["stop_loss"]


def test_target_is_risk_reward_times_the_stop_distance():
    data = bars()
    close_at = {b.ts: b.close for b in data}
    for rr in (1.0, 2.5):
        result = run_backtest(data, BOT(risk_reward=rr), GOLD, CONFIG, USD)
        signals = [e for e in result["events"] if e["kind"] == "signal" and e.get("take_profit")]
        assert signals
        for e in signals:
            close, sl, tp = close_at[e["ts"]], e["stop_loss"], e["take_profit"]
            assert abs(tp - close) == pytest.approx(rr * abs(close - sl))
            assert (sl < close < tp) if e["action"] == "long" else (tp < close < sl)


def test_trailing_stop_moves_only_towards_the_price():
    result = run_backtest(bars(), BOT(use_trailing=True, trail_trigger=0.3), GOLD, CONFIG, USD)
    moves = [e for e in result["events"] if e["message"].startswith("Stop-loss verplaatst")]
    assert moves, "the trailing stop never moved"
    assert all(t["take_profit"] is None for t in result["trades"])     # no fixed target with trailing
    assert any(t["exit_reason"].startswith("Stop-loss") and t["pnl"] > 0 for t in result["trades"])
    assert not [e for e in result["events"] if e.get("reason_code") == "stop_widening"]


def test_executor_refuses_a_looser_stop():
    ex = BacktestExecutor(GOLD, CostModel(0.3, 0.1, 0, 0), SizingRules(2, "fractional", 100), 1000, EventLog(),
                          risk=RiskLimits())
    ex.submit([OrderRequest("open", "o", MON, side="long", stop_loss=1990.0)])
    ex.on_bar(Bar(MON + 60, 2000, 2000, 2000, 2000, 0), 1.0)
    ex.submit([OrderRequest("modify", "m1", MON + 60, side="long", stop_loss=1980.0)])
    ex.on_bar(Bar(MON + 120, 2001, 2001, 2001, 2001, 0), 1.0)
    assert ex.pos.stop_loss == 1990.0
    assert any(e.get("reason_code") == "stop_widening" for e in ex.log.events)
    ex.submit([OrderRequest("modify", "m2", MON + 120, side="long", stop_loss=1995.0)])
    ex.on_bar(Bar(MON + 180, 2001, 2001, 2001, 2001, 0), 1.0)
    assert ex.pos.stop_loss == 1995.0
    # A stop moved above the next open closes the position at that open, like a stop order would.
    ex.submit([OrderRequest("modify", "m3", MON + 180, side="long", stop_loss=2005.0)])
    ex.on_bar(Bar(MON + 240, 2002, 2003, 2001, 2002, 0), 1.0)
    assert ex.pos is None and ex.trades[-1]["exit_reason"] == "Stop-loss (koersgat)"


def test_paper_with_trailing_one_tick_equals_many_ticks():
    m1 = [Candle(*c) for c in synthetic(MON - 4 * 86400, MON + 3 * 86400, 60)]
    cfg = SessionConfig(1, "XAUUSD", "M15", BOT.key(), {"use_trailing": True, "trail_trigger": 0.3}, 1000.0, 2.0,
                        "fractional", 100, {"spread": 0.3, "slippage": 0.1, "commission_per_lot": 0.0,
                                            "financing_pct": 0.0})
    start, end = MON + 3600, MON + 3 * 86400

    def run(times):
        state, trades = new_state(start), {}
        for now in times:
            res = advance(cfg, state, [c for c in m1 if c.ts <= now], now, USD, BOT, GOLD)
            state = res.state
            trades.update({t["id"]: t for t in res.trades})
        return state, [trades[k] for k in sorted(trades)]

    one_state, one = run([end])
    many_state, many = run(list(range(start + 30, end, 300)) + [end])
    assert one and one == many and one_state["account"] == many_state["account"]
