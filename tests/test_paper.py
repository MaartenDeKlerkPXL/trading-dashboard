import asyncio
import json
from dataclasses import replace

import pytest

from backend.backtest.engine import BacktestConfig, RateSeries, run_backtest
from backend.compare import match_trades
from backend.config import Settings
from backend.data.instruments import INSTRUMENTS
from backend.data.providers import Candle
from backend.data.store import CandleStore, resample
from backend.db import connect
from backend.execution.base import CostModel, SizingRules
from backend.paper.engine import PaperEngine
from backend.paper.session import SessionConfig, advance, new_state
from backend.strategies import load_strategies
from backend.strategies.base import Bar
from tests.helpers import FakeProvider, synthetic

GOLD = INSTRUMENTS["XAUUSD"]
USD = RateSeries.constant(1.0, "USD")
MON = 1709510400            # 2024-03-04 00:00 UTC, a Monday
SMA = load_strategies()["sma_cross@v1"]
PARAMS = {"fast": 5, "slow": 12}
COSTS = {"spread": 0.3, "slippage": 0.1, "commission_per_lot": 0.0, "financing_pct": 0.0}


def cfg(timeframe="M15", params=PARAMS):
    return SessionConfig(1, "XAUUSD", timeframe, "sma_cross@v1", params, 1000.0, 2.0, "fractional", 100, COSTS)


def minutes(start, end):
    return [Candle(*c) for c in synthetic(start, end, 60)]


def run_ticks(times, m1, started_at, config=None):
    config = config or cfg()
    state = new_state(started_at)
    trades, events = {}, []
    for now in times:
        res = advance(config, state, [c for c in m1 if c.ts <= now], now, USD, SMA, GOLD)
        state = res.state
        trades.update({t["id"]: t for t in res.trades})
        events += res.events
    return state, [trades[k] for k in sorted(trades)], events


def test_one_tick_equals_many_ticks():
    m1 = minutes(MON - 3 * 86400, MON + 2 * 86400)
    start = MON + 3600
    end = MON + 2 * 86400
    one_state, one_trades, _ = run_ticks([end], m1, start)
    many_state, many_trades, _ = run_ticks(list(range(start + 30, end, 300)) + [end], m1, start)
    assert one_trades and one_trades == many_trades
    assert one_state["account"] == many_state["account"]
    assert one_state["last_m1_ts"] == many_state["last_m1_ts"]


def test_nothing_is_processed_twice():
    m1 = minutes(MON - 3 * 86400, MON + 86400)
    start, now = MON + 3600, MON + 86400
    state, trades, _ = run_ticks([now], m1, start)
    again = advance(cfg(), state, m1, now, USD, SMA, GOLD)
    assert again.trades == [] and again.events == []
    assert again.state["account"] == state["account"]


def test_no_trades_before_session_start_and_fills_at_next_minute():
    m1 = minutes(MON - 3 * 86400, MON + 2 * 86400)
    start = MON + 6 * 3600 + 7 * 60
    state, trades, events = run_ticks([MON + 2 * 86400], m1, start)
    assert trades and all(t["entry_ts"] >= start for t in trades)
    by_ts = {c.ts: c for c in m1}
    for t in trades:
        # Decisions happen when a 15-minute candle closes; the fill is at the open of the next minute.
        assert t["entry_ts"] % (15 * 60) == 0
        c = by_ts[t["entry_ts"]]
        expected = c.open + 0.3 + 0.1 if t["side"] == "long" else c.open - 0.1
        assert t["entry_price"] == pytest.approx(expected)
    kinds = {e["kind"] for e in events}
    assert {"signal", "order", "fill"} <= kinds


def test_paper_matches_backtest_on_the_same_data():
    m1 = minutes(MON - 3 * 86400, MON + 3 * 86400)
    start = MON
    _, paper, _ = run_ticks([MON + 3 * 86400], m1, start)
    bars = [Bar(*c) for c in resample(m1, 15 * 60)]
    first = next(i for i, b in enumerate(bars) if b.ts >= start)
    warm = SMA(**PARAMS).warmup()
    config = BacktestConfig(1000.0, CostModel(**COSTS), SizingRules(2.0, "fractional", 100))
    bt = run_backtest(bars[first - warm + 1:], SMA(**PARAMS), GOLD, config, USD)
    match = match_trades(bt["trades"], paper, "M15")
    # Same signals; paper may differ only where minute-level stops cut a trade earlier.
    assert match["match_pct"] >= 80


def test_dashboard_offline_skips_decisions_but_checks_stops():
    m1 = minutes(MON - 3 * 86400, MON + 2 * 86400)
    start = MON + 3600
    state, _, _ = run_ticks([start + 600], m1, start)
    later = start + 6 * 3600
    res = advance(cfg(), state, [c for c in m1 if c.ts <= later], later, USD, SMA, GOLD)
    messages = [e["message"] for e in res.events]
    assert any("niet actief" in m for m in messages)
    assert any(m.startswith("Candle gemist") for m in messages)
    assert not any(e["kind"] == "signal" and e["ts"] < later - 300 for e in res.events)


def test_quiet_market_closes_candle_on_time_and_orders_wait():
    # Data stops on Friday evening; the last candle still closes and orders wait for Monday.
    fri = MON + 4 * 86400
    m1 = [c for c in minutes(MON - 3 * 86400, fri + 22 * 3600)]
    start = MON
    state, _, _ = run_ticks([fri + 22 * 3600 + 30], m1, start)
    res = advance(cfg(), state, m1, fri + 23 * 3600, USD, SMA, GOLD)
    assert res.state["last_bar_ts"] == res.state["cur_bucket"]


# ---------- engine with storage ----------

def make_engine(tmp_path, now):
    conn = connect(tmp_path / "paper.sqlite")
    clock = {"now": now}
    store = CandleStore(conn, FakeProvider(), now=lambda: clock["now"])
    engine = PaperEngine(conn, store, replace(Settings(), db_path=tmp_path / "paper.sqlite"), now=lambda: clock["now"])
    return engine, clock


def start_session(engine, timeframe="M15"):
    return engine.create({
        "symbol": "XAUUSD", "timeframe": timeframe, "strategy": "sma_cross@v1", "params": PARAMS,
        "capital": 1000.0, "risk_pct": 2.0, "sizing_mode": "fractional", "leverage": 100, "costs": COSTS,
    })


def test_engine_persists_and_survives_restart(tmp_path):
    engine, clock = make_engine(tmp_path, MON + 3600)
    sid = start_session(engine)
    for i in range(1, 50):
        clock["now"] = MON + 3600 + i * 300
        asyncio.run(engine.tick())
    trades_before = engine.trades(sid)
    events_before = len(engine.events(sid))
    assert trades_before and engine.equity(sid)

    # "Restart": a new engine object on the same database continues where it stopped.
    engine2, clock2 = make_engine(tmp_path, clock["now"])
    asyncio.run(engine2.tick())  # same clock: nothing new may happen
    assert engine2.trades(sid) == trades_before
    assert len(engine2.events(sid)) == events_before
    summary = engine2.summary(engine2.row(sid))
    assert summary["status"] == "running" and summary["trades"] == len(trades_before)
    m = engine2.metrics(sid)
    assert m["trades"] == len(trades_before)


def test_changed_strategy_file_blocks_session(tmp_path, monkeypatch):
    engine, clock = make_engine(tmp_path, MON + 3600)
    sid = start_session(engine)
    import backend.paper.engine as pe
    monkeypatch.setattr(pe, "code_hash", lambda cls: "different")
    clock["now"] += 300
    asyncio.run(engine.tick())
    row = engine.row(sid)
    assert row["status"] == "blocked" and "nieuwe versie" in row["status_reason"]


def test_paused_session_is_not_advanced(tmp_path):
    engine, clock = make_engine(tmp_path, MON + 3600)
    sid = start_session(engine)
    engine.set_status(sid, "paused")
    clock["now"] += 3 * 3600
    asyncio.run(engine.tick())
    state = json.loads(engine.row(sid)["state"])
    assert state["last_tick_at"] is None


# ---------- API ----------

from fastapi.testclient import TestClient  # noqa: E402

from backend.main import create_app  # noqa: E402


@pytest.fixture
def api(tmp_path):
    settings = replace(Settings(), db_path=tmp_path / "api.sqlite")
    with TestClient(create_app(settings, provider=FakeProvider(), start_loop=False)) as client:
        clock = {"now": MON + 3600}
        client.app.state.paper.now = lambda: clock["now"]
        client.app.state.store.now = lambda: clock["now"]
        yield client, clock


def test_paper_api_lifecycle(api):
    client, clock = api
    body = {"symbol": "XAUUSD", "timeframe": "M15", "strategy": "sma_cross@v1", "params": PARAMS,
            "capital": 1000, "risk_pct": 2, "sizing_mode": "fractional"}
    s = client.post("/api/paper/sessions", json=body).json()
    assert s["status"] == "running" and s["version"] == "v1"
    for i in range(1, 40):
        clock["now"] = MON + 3600 + i * 600
        assert client.post("/api/paper/tick").status_code == 200

    d = client.get(f"/api/paper/sessions/{s['id']}").json()
    assert d["trades"] and d["events"] and len(d["equity_curve"]) > 2
    times = [p["time"] for p in d["equity_curve"]]
    assert times == sorted(set(times))
    assert d["metrics"]["trades"] == len(d["trades"])

    cmp = client.get(f"/api/paper/sessions/{s['id']}/compare").json()
    assert cmp["paper"] and cmp["backtest"] and cmp["deviation"]
    assert cmp["matching"]["match_pct"] >= 70

    status = client.get("/api/paper/status").json()
    assert status["sessions_running"] == 1 and status["last_error"] is None

    assert client.post(f"/api/paper/sessions/{s['id']}/resume").status_code == 400
    assert client.post(f"/api/paper/sessions/{s['id']}/pause").json()["status"] == "paused"
    assert client.post(f"/api/paper/sessions/{s['id']}/resume").json()["status"] == "running"
    assert client.post(f"/api/paper/sessions/{s['id']}/stop").json()["status"] == "stopped"
    assert client.post(f"/api/paper/sessions/{s['id']}/resume").status_code == 400


def test_paper_start_validation(api):
    client, _ = api
    base = {"symbol": "XAUUSD", "timeframe": "M15", "strategy": "sma_cross@v1"}
    assert client.post("/api/paper/sessions", json={**base, "strategy": "nope@v1"}).status_code == 400
    assert client.post("/api/paper/sessions", json={**base, "params": {"fast": 50, "slow": 10}}).status_code == 400
    assert client.post("/api/paper/sessions", json={**base, "spread": 0}).status_code == 400


def test_evaluation_card(api):
    client, clock = api
    s = client.post("/api/paper/sessions", json={"symbol": "XAUUSD", "timeframe": "M15", "strategy": "sma_cross@v1",
                                                 "params": PARAMS, "sizing_mode": "fractional"}).json()
    for i in range(1, 30):
        clock["now"] = MON + 3600 + i * 600
        client.post("/api/paper/tick")
    url = f"/api/paper/sessions/{s['id']}/evaluation"
    criteria = [
        {"type": "min_trades", "value": 1, "after_days": 0},
        {"type": "max_drawdown", "value": 50, "after_days": 28},
        {"type": "return_vs_backtest", "value": 100, "after_days": 0},
        {"type": "min_return", "value": 5, "after_days": 28},
    ]
    ev = client.put(url, json={"criteria": criteria, "notes": "Eerste test"}).json()
    statuses = [r["status"] for r in ev["results"]]
    assert statuses == ["pass", "pending", "pass", "pending"]
    assert ev["notes"] == "Eerste test" and not ev["locked_at"]
    ev = client.put(url, json={"lock": True, "decision": "doorgaan"}).json()
    assert ev["locked_at"] and ev["decision"] == "doorgaan"
    r = client.put(url, json={"criteria": []})
    assert r.status_code == 400 and "vastgelegd" in r.json()["detail"]
    assert client.put(url, json={"notes": "Nog steeds aanpasbaar"}).json()["notes"] == "Nog steeds aanpasbaar"
    assert client.put(url, json={"criteria": [{"type": "x", "value": 1}]}).status_code == 400
