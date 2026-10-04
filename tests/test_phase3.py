import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.backtest import optimize as opt
from backend.backtest.engine import BacktestConfig, RateSeries
from backend.backtest.service import Setup, CommonSettings, out_of_sample, split_index
from backend.compare import match_trades
from backend.config import Settings
from backend.data.instruments import INSTRUMENTS
from backend.execution.base import CostModel, SizingRules
from backend.importers.tradingview import ImportError_, build_run, parse_trades
from backend.main import create_app
from backend.strategies import code_hash, load_strategies
from backend.strategies.base import Bar, History
from backend.strategies.indicators import RsiTracker, highest, lowest, rsi, rsi_values, stdev
from tests.helpers import FakeProvider, synthetic

GOLD = INSTRUMENTS["XAUUSD"]
T0 = 1704067200
USD = RateSeries.constant(1.0, "USD")
CONFIG = BacktestConfig(1000, CostModel(0.3, 0.1, 0.0, 0.0), SizingRules(2.0, "fractional"))


def bars_from(closes, step=3600):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Bar(T0 + i * step, prev, max(prev, c) + 0.5, min(prev, c) - 0.5, c, 1.0))
        prev = c
    return out


def year_of_bars(step=3600, days=200):
    return [Bar(*c) for c in synthetic(T0, T0 + days * 86400, step)]


# ---------- indicators ----------

def full_history_rsi(closes, period):
    """Reference: Wilder's RSI from the very first bar, like Pine's ta.rsi."""
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        ag, al = (ag * (period - 1) + g) / period, (al * (period - 1) + l) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def test_rsi_matches_full_history_reference():
    closes = [c.close for c in year_of_bars()][:1500]
    h = History(bars_from(closes), len(closes))
    assert rsi(h, 14) == pytest.approx(full_history_rsi(closes, 14), abs=0.01)
    prev, now = rsi_values(h, 14, count=2)
    assert now == pytest.approx(rsi(h, 14), abs=0.01)


def test_rsi_tracker_is_exact_bar_by_bar():
    closes = [c.close for c in year_of_bars()][:400]
    bars = bars_from(closes)
    tracker = RsiTracker(14)
    for n in range(2, len(bars) + 1):
        values = tracker.update(History(bars, n))
        if n > 15:
            assert values[-1] == pytest.approx(full_history_rsi(closes[:n], 14), rel=1e-9)
    # A jump (e.g. a new history) triggers an exact recalculation.
    fresh = RsiTracker(14)
    assert fresh.update(History(bars, 300))[-1] == pytest.approx(full_history_rsi(closes[:300], 14), rel=1e-9)


def test_rsi_extremes_and_channels():
    up = History(bars_from([float(i) for i in range(1, 40)]), 39)
    assert rsi(up, 14) == 100.0
    bars = bars_from([10, 12, 11, 15, 9, 13])
    h = History(bars, len(bars))
    assert highest(h, 3) == max(b.high for b in bars[2:5])   # excludes the latest bar
    assert lowest(h, 3, offset=0) == min(b.low for b in bars[3:6])
    assert stdev(History(bars_from([2, 4, 4, 4, 5, 5, 7, 9]), 8), 8) == pytest.approx(2.0)


# ---------- strategies ----------

def test_all_strategies_load_and_run():
    strategies = load_strategies()
    assert {"sma_cross@v1", "rsi_reversal@v1", "donchian_breakout@v1", "bollinger_reversion@v1"} <= set(strategies)
    assert not any(k.startswith("my_idea") for k in strategies)  # the template is not loaded
    from backend.backtest.engine import run_backtest
    bars = year_of_bars(days=60)
    for key, cls in strategies.items():
        result = run_backtest(bars, cls(), GOLD, CONFIG, USD)
        assert result["metrics"]["trades"] > 0, key
        for t in result["trades"]:
            assert (t["stop_loss"] < t["entry_price"]) == (t["side"] == "long"), key
        assert len(code_hash(cls)) == 12


def test_param_combination_validation():
    s = load_strategies()
    with pytest.raises(ValueError):
        s["rsi_reversal@v1"](oversold=60, overbought=55)
    with pytest.raises(ValueError):
        s["donchian_breakout@v1"](entry_period=10, exit_period=20)


def test_bollinger_exits_at_middle_band():
    from backend.backtest.engine import run_backtest
    closes = [100.0] * 30 + [97.0, 96.0, 99.5, 100.5, 101.0, 101.0]
    cls = load_strategies()["bollinger_reversion@v1"]
    result = run_backtest(bars_from(closes), cls(period=20, allow_short=False), GOLD,
                          replace(CONFIG, costs=CostModel(0.01, 0.01, 0, 0)), USD)
    trades = result["trades"]
    assert trades and trades[0]["side"] == "long"
    assert trades[0]["exit_reason"] in ("Signaal", "Einde backtest")


# ---------- out-of-sample and optimizer ----------

def make_setup(bars):
    common = CommonSettings(symbol="XAUUSD", timeframe="H1", start="2024-01-01", end="2024-07-01")
    return Setup(common, GOLD, bars, CONFIG, USD, bars[0].ts, bars[-1].ts + 3600)


def test_out_of_sample_split_uses_separate_segments():
    bars = year_of_bars()
    cls = load_strategies()["sma_cross@v1"]
    res = out_of_sample(make_setup(bars), cls, {"fast": 10, "slow": 30}, 30)
    split = split_index(len(bars), 30)
    assert res["split_ts"] == bars[split].ts
    assert res["in_sample"]["trades"] > 0 and res["out_of_sample"]["trades"] > 0


def test_build_axes_validation():
    cls = load_strategies()["sma_cross@v1"]
    axes = opt.build_axes(cls, [{"name": "fast", "start": 5, "stop": 20, "step": 5}])
    assert axes[0].values == (5, 10, 15, 20)
    with pytest.raises(ValueError, match="te veel"):
        opt.build_axes(cls, [{"name": "fast", "start": 2, "stop": 40, "step": 1},
                             {"name": "slow", "start": 50, "stop": 89, "step": 1}])
    with pytest.raises(ValueError):
        opt.build_axes(cls, [{"name": "allow_short", "start": 0, "stop": 1, "step": 1}])
    with pytest.raises(ValueError):
        opt.build_axes(cls, [{"name": "fast", "start": 10, "stop": 5, "step": 1}])


def test_neighbors_on_grid():
    axes = [opt.Axis("a", "A", (1, 2, 3)), opt.Axis("b", "B", (1, 2, 3))]
    assert sorted(opt._neighbors(4, axes)) == [0, 1, 2, 3, 5, 6, 7, 8]
    assert sorted(opt._neighbors(0, axes)) == [1, 3, 4]


def test_optimizer_never_trains_on_out_of_sample(monkeypatch):
    bars = year_of_bars()
    split = split_index(len(bars), 30)
    calls = []
    real = opt._evaluate

    def spy(key, params, start, end):
        calls.append((start, end))
        return real(key, params, start, end)

    monkeypatch.setattr(opt, "_evaluate", spy)
    cls = load_strategies()["sma_cross@v1"]
    res = opt.optimize(bars, cls, GOLD, CONFIG, USD, {}, [
        {"name": "fast", "start": 10, "stop": 20, "step": 10},
        {"name": "slow", "start": 30, "stop": 50, "step": 20}], oos_pct=30, workers=1)
    training = calls[:-1]
    assert training and all(end <= split for _, end in training)
    assert calls[-1][1] == len(bars)                     # the final call tests the locked period
    assert res["best"]["params"]["fast"] in (10, 20)
    assert len(res["grid"]) == 4 and res["variants"] == 4
    assert res["best"]["split_ts"] == bars[split].ts


def test_walk_forward_windows_roll_forward(monkeypatch):
    bars = year_of_bars()
    calls = []
    real = opt._evaluate
    monkeypatch.setattr(opt, "_evaluate", lambda k, p, s, e: calls.append((s, e)) or real(k, p, s, e))
    cls = load_strategies()["sma_cross@v1"]
    res = opt.optimize(bars, cls, GOLD, CONFIG, USD, {}, [{"name": "fast", "start": 10, "stop": 20, "step": 10}],
                       oos_pct=30, folds=3, workers=1)
    assert len(res["windows"]) == 3
    tests = [w["test_from"] for w in res["windows"]]
    assert tests == sorted(tests)
    assert res["walk_forward"]["trades"] == sum(w["out_of_sample"]["trades"] for w in res["windows"])


def test_optimizer_reports_too_few_trades():
    bars = year_of_bars(days=60)
    cls = load_strategies()["sma_cross@v1"]
    with pytest.raises(ValueError, match="trades"):
        opt.optimize(bars, cls, GOLD, CONFIG, USD, {}, [{"name": "fast", "start": 10, "stop": 20, "step": 10}],
                     min_trades=500, workers=1)


# ---------- TradingView import ----------

OLD_CSV = """Trade #,Type,Signal,Date/Time,Price USD,Contracts,Profit USD,Profit %,Cumulative profit USD,Cumulative profit %,Run-up USD,Run-up %,Drawdown USD,Drawdown %
1,Entry Long,RsiLE,2024-03-04 10:00,2100.50,1,12.5,0.6,12.5,0.13,15,0.7,-3,-0.1
1,Exit Long,RsiSE,2024-03-05 14:00,2113.00,1,12.5,0.6,12.5,0.13,15,0.7,-3,-0.1
2,Entry Short,RsiSE,2024-03-05 14:00,2113.00,1,-7.5,-0.35,5,0.05,2,0.1,-9,-0.4
2,Exit Short,RsiLE,2024-03-06 09:00,2120.50,1,-7.5,-0.35,5,0.05,2,0.1,-9,-0.4
3,Entry Long,RsiLE,2024-03-06 09:00,2120.50,1,3,0.1,8,0.08,4,0.2,-1,-0.05
3,Exit Long,Open,2024-03-06 15:00,2123.50,1,3,0.1,8,0.08,4,0.2,-1,-0.05
"""

NEW_CSV = (
    "Trade #;Type;Date/Time;Signal;Price EUR;Position size (qty);Position size (value);Net P&L EUR;Net P&L %;"
    "Run-up EUR;Run-up %;Drawdown EUR;Drawdown %;Cumulative P&L EUR;Cumulative P&L %\n"
    "1;Exit long;2024-03-05 14:00;Close;2113,00;2;4226;25,00;0,6;30;0,7;−6;−0,1;25,00;2,5\n"
    "1;Entry long;2024-03-04 10:00;Long;2100,50;2;4201;25,00;0,6;30;0,7;−6;−0,1;25,00;2,5\n"
)


def test_parse_old_tradingview_format():
    parsed = parse_trades(OLD_CSV, "Europe/Amsterdam")
    trades = parsed["trades"]
    assert len(trades) == 2 and parsed["open_trades"] == 1
    assert parsed["currency"] == "USD"
    assert trades[0]["side"] == "long" and trades[1]["side"] == "short"
    # 10:00 Amsterdam in March (CET, UTC+1) is 09:00 UTC.
    assert trades[0]["entry_ts"] == 1709542800
    assert trades[0]["pnl"] == 12.5 and trades[1]["pnl"] == -7.5
    assert parsed["capital"] == pytest.approx(5 / 0.0005)  # 5 USD = 0.05%


def test_parse_new_format_with_semicolons_and_decimal_commas():
    parsed = parse_trades(NEW_CSV.encode("utf-8"), "UTC")
    t = parsed["trades"][0]
    assert parsed["currency"] == "EUR"
    assert t["entry_price"] == 2100.5 and t["exit_price"] == 2113.0 and t["lots"] == 2
    assert t["entry_ts"] == 1709546400 and t["pnl"] == 25.0
    assert parsed["capital"] == pytest.approx(1000.0)
    run = build_run(parsed, "XAUUSD", "H1", "test", None)
    assert run["metrics"]["total_return_pct"] == pytest.approx(2.5)
    assert run["trades"][0]["return_pct"] == pytest.approx(2.5)


def test_parse_rejects_other_files():
    with pytest.raises(ImportError_, match="List of trades"):
        parse_trades("a,b,c\n1,2,3\n")
    with pytest.raises(ImportError_):
        parse_trades("")
    with pytest.raises(ImportError_, match="tijdzone"):
        parse_trades(OLD_CSV, "Mars/Olympus")


def test_match_trades():
    ours = [
        {"id": 1, "side": "long", "entry_ts": 100, "entry_price": 10.0, "exit_ts": 500, "exit_price": 11.0},
        {"id": 2, "side": "short", "entry_ts": 500, "entry_price": 11.0, "exit_ts": 900, "exit_price": 10.0},
        {"id": 3, "side": "long", "entry_ts": 9000, "entry_price": 12.0, "exit_ts": 9900, "exit_price": 13.0},
    ]
    tv = [
        {"id": 1, "side": "long", "entry_ts": 100, "entry_price": 10.2, "exit_ts": 500, "exit_price": 11.0},
        {"id": 2, "side": "long", "entry_ts": 500, "entry_price": 11.0, "exit_ts": 900, "exit_price": 10.0},
    ]
    res = match_trades(tv, ours, "M1")
    assert res["matched"] == 1 and res["only_reference"] == [2] and res["only_ours"] == [2, 3]
    assert res["avg_entry_price_diff"] == pytest.approx(0.2)


# ---------- API ----------

@pytest.fixture
def client(tmp_path):
    settings = replace(Settings(), db_path=tmp_path / "api.sqlite")
    with TestClient(create_app(settings, provider=FakeProvider())) as c:
        yield c


def sync(client, body):
    job = client.post("/api/data/sync", json=body).json()
    while job["status"] == "running":
        time.sleep(0.02)
        job = client.get(f"/api/data/jobs/{job['id']}").json()
    assert job["status"] == "done"


PERIOD = {"symbol": "XAUUSD", "timeframe": "H1", "start": "2024-01-01", "end": "2024-06-30"}


def test_backtest_is_saved_and_runs_can_be_managed(client):
    sync(client, PERIOD)
    r = client.post("/api/backtest", json={**PERIOD, "strategy": "sma_cross@v1", "sizing_mode": "fractional",
                                           "oos_pct": 30}).json()
    assert r["run_id"] and r["oos"]["in_sample"]["trades"] > 0
    runs = client.get("/api/runs").json()
    assert runs[0]["id"] == r["run_id"] and runs[0]["strategy"] == "sma_cross@v1"
    assert not runs[0]["strategy_changed"]
    full = client.get(f"/api/runs/{r['run_id']}").json()
    assert full["trades"] == r["trades"] and full["settings"]["oos_pct"] == 30
    assert client.patch(f"/api/runs/{r['run_id']}", json={"note": "Goed idee"}).json()["ok"]
    assert client.get(f"/api/runs/{r['run_id']}").json()["note"] == "Goed idee"
    assert client.delete(f"/api/runs/{r['run_id']}").status_code == 200
    assert client.get(f"/api/runs/{r['run_id']}").status_code == 404


def test_compare_all_strategies_and_compare_view(client):
    sync(client, PERIOD)
    r = client.post("/api/compare/run", json={**PERIOD, "sizing_mode": "fractional"}).json()
    assert len(r["run_ids"]) == len(load_strategies())
    cmp = client.get("/api/compare", params={"ids": ",".join(map(str, r["run_ids"]))}).json()
    assert len(cmp["runs"]) == len(r["run_ids"]) and not cmp["warnings"]
    assert cmp["runs"][0]["equity_pct"][0]["value"] == pytest.approx(0, abs=0.5)


def test_tradingview_import_and_matching(client):
    sync(client, PERIOD)
    eng = client.post("/api/backtest", json={**PERIOD, "strategy": "sma_cross@v1"}).json()
    imp = client.post("/api/import/tradingview", json={
        "filename": "rsi.csv", "content": OLD_CSV, "symbol": "XAUUSD", "timeframe": "H1"}).json()
    assert imp["trades"] == 2 and imp["capital_inferred"]
    cmp = client.get("/api/compare", params={"ids": f"{eng['run_id']},{imp['run_id']}"}).json()
    assert cmp["matchings"] and cmp["matchings"][0]["reference_trades"] == 2
    bad = client.post("/api/import/tradingview", json={"content": "x,y\n1,2\n", "symbol": "XAUUSD", "timeframe": "H1"})
    assert bad.status_code == 400


def test_optimize_task_end_to_end(client):
    sync(client, PERIOD)
    body = {**PERIOD, "strategy": "sma_cross@v1", "sizing_mode": "fractional",
            "ranges": [{"name": "fast", "start": 10, "stop": 20, "step": 10}], "min_trades": 3}
    task = client.post("/api/optimize", json=body).json()
    for _ in range(500):
        task = client.get(f"/api/tasks/{task['id']}").json()
        if task["status"] != "running":
            break
        time.sleep(0.02)
    assert task["status"] == "done", task
    assert task["result"]["best"]["params"]["fast"] in (10, 20)
    assert task["result"]["settings"]["strategy"] == "sma_cross@v1"
    bad = client.post("/api/optimize", json={**body, "ranges": [{"name": "nope", "start": 1, "stop": 2, "step": 1}]})
    assert bad.status_code == 400
