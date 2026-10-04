import math
from dataclasses import replace

import pytest

from backend.backtest.engine import BacktestConfig, RateSeries, run_backtest
from backend.backtest.metrics import compute_metrics, daily_returns, drawdown_curve
from backend.data.instruments import INSTRUMENTS
from backend.execution.base import CostModel, SizingRules, size_position
from backend.strategies import load_strategies
from backend.strategies.base import Bar, History, Param, Signal, Strategy

GOLD = INSTRUMENTS["XAUUSD"]
T0 = 1704067200  # 2024-01-01 00:00 UTC (Monday)
H = 3600


def bars_from(prices, step=H, start=T0, wick=0.5):
    """Bars whose open is the previous close; high/low a small wick around the body."""
    out, prev = [], prices[0]
    for i, p in enumerate(prices):
        out.append(Bar(start + i * step, prev, max(prev, p) + wick, min(prev, p) - wick, p, 1.0))
        prev = p
    return out


class Scripted(Strategy):
    """Emits pre-defined signals at given bar counts; records what it could see."""

    name, version, label, description = "scripted", "v1", "Test", "Test"

    def __init__(self, script, **kw):
        self.script = script
        self.seen = []
        super().__init__(**kw)

    def on_bar(self, history: History, position):
        self.seen.append((len(history), history[-1].ts, position.side))
        return self.script.get(len(history))


def config(capital=10_000.0, spread=0.3, slippage=0.1, commission=0.0, financing=0.0, risk=2.0, mode="fractional"):
    return BacktestConfig(capital, CostModel(spread, slippage, commission, financing), SizingRules(risk, mode))


USD = RateSeries.constant(1.0, "USD")  # 1 USD = 1 EUR keeps the arithmetic readable


def test_history_cannot_see_the_future():
    bars = bars_from([1, 2, 3, 4])
    h = History(bars, 2)
    assert len(h) == 2 and h[-1] is bars[1]
    with pytest.raises(IndexError):
        h[2]
    assert h[0:10] == bars[:2]
    assert h.closes(5) == [1, 2]


def test_signal_on_bar_n_fills_at_open_of_bar_n_plus_1():
    prices = [2000, 2001, 2002, 2010, 2011, 2012]
    bars = bars_from(prices)
    strat = Scripted({2: Signal("long", stop_loss=1990, reason="test")})
    result = run_backtest(bars, strat, GOLD, config(), USD)
    trade = result["trades"][0]
    # The signal came after bar index 1 closed; fill at open of bar index 2 (+ spread + slippage).
    assert trade["entry_ts"] == bars[2].ts
    assert trade["entry_price"] == pytest.approx(bars[2].open + 0.3 + 0.1)
    # The strategy never saw a bar beyond the one that just closed.
    assert all(count == i + 1 for i, (count, ts, _) in enumerate(strat.seen))
    assert strat.seen[-1][1] == bars[-1].ts


def test_round_trip_on_flat_price_costs_exactly_spread_slippage_commission():
    bars = bars_from([2000] * 6, wick=0.2)
    strat = Scripted({1: Signal("long", stop_loss=1990), 3: Signal("flat")})
    cfg = config(commission=3.0)
    result = run_backtest(bars, strat, GOLD, cfg, USD)
    t = result["trades"][0]
    units = t["lots"] * GOLD.contract_size
    expected = -(0.3 + 2 * 0.1) * units - 2 * 3.0 * t["lots"]
    assert t["pnl"] == pytest.approx(expected)
    assert t["costs"]["commission"] == pytest.approx(6.0 * t["lots"])
    assert result["metrics"]["final_equity"] == pytest.approx(10_000 + expected)


def test_risk_sizing_loses_about_two_percent_at_stop():
    prices = [2000, 2000, 2000, 1980, 1970]
    bars = bars_from(prices, wick=0.0)
    strat = Scripted({1: Signal("long", stop_loss=1990)})
    result = run_backtest(bars, strat, GOLD, config(spread=0.3, slippage=0.1), USD)
    t = result["trades"][0]
    assert t["exit_reason"].startswith("Stop-loss")
    # Risk is measured from the fill to the stop; slippage on the exit makes it a little worse.
    assert t["return_pct"] == pytest.approx(-2.0, abs=0.05)


def test_gap_through_stop_fills_at_open():
    bars = [
        Bar(T0, 2000, 2001, 1999, 2000, 1),
        Bar(T0 + H, 2000, 2001, 1999, 2000, 1),   # fill here
        Bar(T0 + 2 * H, 1980, 1985, 1975, 1982, 1),  # opens below the stop at 1990
    ]
    strat = Scripted({1: Signal("long", stop_loss=1990)})
    t = run_backtest(bars, strat, GOLD, config(), USD)["trades"][0]
    assert t["exit_reason"] == "Stop-loss (koersgat)"
    assert t["exit_price"] == pytest.approx(1980 - 0.1)


def test_stop_and_target_in_same_bar_assumes_stop_first():
    bars = [
        Bar(T0, 2000, 2000, 2000, 2000, 1),
        Bar(T0 + H, 2000, 2000.5, 1999.5, 2000, 1),
        Bar(T0 + 2 * H, 2000, 2020, 1980, 2000, 1),  # touches both 1990 and 2010
    ]
    strat = Scripted({1: Signal("long", stop_loss=1990, take_profit=2010)})
    t = run_backtest(bars, strat, GOLD, config(), USD)["trades"][0]
    assert t["exit_reason"] == "Stop-loss"


def test_short_stop_triggers_on_ask():
    bars = [
        Bar(T0, 2000, 2000, 2000, 2000, 1),
        Bar(T0 + H, 2000, 2000, 2000, 2000, 1),       # short fill at bid 2000 - slippage
        Bar(T0 + 2 * H, 2000, 2009.8, 1999, 2005, 1),  # bid high 2009.8 → ask high 2010.1 ≥ stop 2010
    ]
    strat = Scripted({1: Signal("short", stop_loss=2010)})
    t = run_backtest(bars, strat, GOLD, config(), USD)["trades"][0]
    assert t["side"] == "short" and t["exit_reason"] == "Stop-loss"
    assert t["entry_price"] == pytest.approx(1999.9)
    assert t["exit_price"] == pytest.approx(2010.1)


def test_reversal_closes_then_opens():
    bars = bars_from([2000, 2005, 2010, 2005, 2000, 1995, 1990])
    strat = Scripted({2: Signal("long", stop_loss=1950), 4: Signal("short", stop_loss=2050)})
    result = run_backtest(bars, strat, GOLD, config(), USD)
    sides = [t["side"] for t in result["trades"]]
    assert sides == ["long", "short"]
    assert result["trades"][0]["exit_ts"] == result["trades"][1]["entry_ts"] == bars[4].ts
    assert result["trades"][1]["exit_reason"] == "Einde backtest"


def test_signal_without_stop_loss_is_refused():
    bars = bars_from([2000, 2001, 2002, 2003])
    result = run_backtest(bars, Scripted({1: Signal("long")}), GOLD, config(), USD)
    assert result["trades"] == []
    assert any("stop-loss is verplicht" in e["message"] for e in result["events"])


def test_realistic_sizing_skips_when_min_lot_is_too_risky():
    # €1000 at 2% = €20 risk. A $30 stop on 0.01 lot of gold (1 oz) risks $30 > €20.
    bars = bars_from([2000, 2000, 2000, 2001])
    result = run_backtest(bars, Scripted({1: Signal("long", stop_loss=1970)}), GOLD,
                          config(capital=1000, mode="realistic"), USD)
    assert result["trades"] == []
    assert result["metrics"]["skipped_signals"] == 1
    assert any("kleinste lotgrootte" in w for w in result["warnings"])


def test_size_position_rounds_down_to_lot_step_and_respects_margin():
    rules = SizingRules(2.0, "realistic", leverage=100)
    lots, _ = size_position(10_000, 2000, 1990, 1.0, GOLD, 1.0, rules)
    assert lots == pytest.approx(0.2)  # €200 risk / ($10 × 100 oz) = 0.2 lot
    # A tiny stop would need a huge position; margin caps it (2000×100/100 = €2000 margin per lot).
    lots, note = size_position(10_000, 2000, 1999.99, 1.0, GOLD, 1.0, rules)
    assert lots == pytest.approx(4.75) and "marge" in note


def test_financing_charged_per_night():
    bars = bars_from([2000] * 50, wick=0.0)  # 50 hours: crosses two UTC midnights after entry
    strat = Scripted({1: Signal("long", stop_loss=1900)})
    result = run_backtest(bars, strat, GOLD, config(financing=36.5), USD)
    t = result["trades"][0]
    value = t["lots"] * GOLD.contract_size * 2000
    assert t["costs"]["financing"] == pytest.approx(value * 0.365 / 365 * 2, rel=0.01)


def test_pnl_is_converted_to_eur():
    prices = [2000, 2000, 2000, 2010, 2010]
    bars = bars_from(prices, wick=0.0)
    strat = Scripted({1: Signal("long", stop_loss=1900), 3: Signal("flat")})
    eur = run_backtest(bars, strat, GOLD, config(mode="fractional"), RateSeries.constant(1.25, "USD"))
    usd = run_backtest(bars, Scripted({1: Signal("long", stop_loss=1900), 3: Signal("flat")}), GOLD,
                       config(mode="fractional"), USD)
    # Same risk in EUR, so the same % result; the position is 1.25× larger in USD terms.
    assert eur["trades"][0]["lots"] == pytest.approx(usd["trades"][0]["lots"] * 1.25)
    assert eur["trades"][0]["return_pct"] == pytest.approx(usd["trades"][0]["return_pct"])


def test_rate_series_uses_previous_day_only():
    day = 86400
    rates = RateSeries([(T0, 1.10), (T0 + day, 1.20)], 1.0, "USD")
    assert rates.to_eur(T0 + day + 5) == pytest.approx(1 / 1.10)   # day 2 not closed yet
    assert rates.to_eur(T0 + 2 * day) == pytest.approx(1 / 1.20)
    assert not rates.used_fallback
    empty = RateSeries([], 1.16, "USD")
    assert empty.to_eur(T0) == pytest.approx(1 / 1.16) and empty.used_fallback


def test_zero_costs_are_rejected():
    bars = bars_from([1, 2, 3])
    with pytest.raises(ValueError, match="spread"):
        run_backtest(bars, Scripted({}), GOLD, config(spread=0), USD)
    with pytest.raises(ValueError, match="slippage"):
        run_backtest(bars, Scripted({}), GOLD, config(slippage=0), USD)


def test_metrics_known_values():
    equity = [(T0, 100.0), (T0 + 86400, 110.0), (T0 + 2 * 86400, 99.0), (T0 + 3 * 86400, 121.0)]
    assert [round(v, 2) for _, v in drawdown_curve(equity)] == [0, 0, -10, 0]
    assert daily_returns(equity) == pytest.approx([0.1, -0.1, 121 / 99 - 1])
    trades = [
        {"pnl": 30, "r_multiple": 1.5, "costs_total": 1, "bars_held": 2},
        {"pnl": -10, "r_multiple": -0.5, "costs_total": 1, "bars_held": 4},
        {"pnl": 20, "r_multiple": 1.0, "costs_total": 1, "bars_held": 3},
    ]
    m = compute_metrics(equity, trades, 100.0, 252, 10, 5)
    assert m["total_return_pct"] == pytest.approx(21.0)
    assert m["max_drawdown_pct"] == pytest.approx(10.0)
    assert m["winrate_pct"] == pytest.approx(200 / 3)
    assert m["profit_factor"] == pytest.approx(5.0)
    assert m["avg_trade"] == pytest.approx(40 / 3)
    assert m["expectancy_r"] == pytest.approx(2 / 3)
    assert m["exposure_pct"] == pytest.approx(50.0)
    assert m["sharpe"] is not None and m["sortino"] is not None
    assert not math.isinf(m["sharpe"])


def test_few_trades_warning():
    bars = bars_from([2000, 2001, 2002, 2003, 2004])
    result = run_backtest(bars, Scripted({1: Signal("long", stop_loss=1900)}), GOLD, config(), USD)
    assert any("te weinig" in w for w in result["warnings"])


class TestSmaCross:
    S = load_strategies()["sma_cross@v1"]

    def test_params_validation(self):
        with pytest.raises(ValueError, match="korter"):
            self.S(fast=50, slow=20)
        with pytest.raises(ValueError, match="minimaal"):
            self.S(fast=1)
        with pytest.raises(ValueError, match="Onbekende"):
            self.S(nope=1)
        assert self.S(fast="10", slow="30").p["fast"] == 10

    def test_trades_on_crosses_with_atr_stop(self):
        # Down, then a clear rise, then a clear fall: one long, then one short.
        prices = [100 - i * 0.5 for i in range(40)] + [80 + i for i in range(40)] + [120 - i for i in range(40)]
        bars = bars_from(prices, wick=0.2)
        strat = self.S(fast=5, slow=15, atr_period=5, atr_stop=2.0)
        inst = replace(GOLD, spread=0.01, slippage=0.01)
        result = run_backtest(bars, strat, inst, config(spread=0.01, slippage=0.01), USD)
        sides = [t["side"] for t in result["trades"]]
        assert sides[:2] == ["long", "short"] or sides[0] == "short"
        assert "long" in sides and "short" in sides
        for t in result["trades"]:
            if t["side"] == "long":
                assert t["stop_loss"] < t["entry_price"]
            else:
                assert t["stop_loss"] > t["entry_price"]

    def test_no_short_mode_goes_flat(self):
        prices = [100 + i for i in range(40)] + [140 - i for i in range(40)]
        bars = bars_from(prices, wick=0.2)
        strat = self.S(fast=5, slow=15, atr_period=5, allow_short=False)
        result = run_backtest(bars, strat, GOLD, config(spread=0.01, slippage=0.01), USD)
        assert all(t["side"] == "long" for t in result["trades"])


def test_param_bool_coercion():
    p = Param("x", "X", True)
    assert p.coerce("false") is False and p.coerce(1) is True
