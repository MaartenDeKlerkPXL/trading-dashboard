import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.alerts import Alerter
from backend.config import ConfigError, Settings, load_settings
from backend.data.instruments import INSTRUMENTS
from backend.db import connect, get_state, set_state
from backend.execution.backtest import BacktestExecutor
from backend.execution.base import CostModel, OrderRequest, SizingRules
from backend.execution.events import EventLog
from backend.main import create_app
from backend.monitor import Monitor
from backend.paper.engine import PaperEngine
from backend.reconcile import reconcile_paper
from backend.risk import RiskLimits, before_weekend, day_key
from backend.strategies.base import Bar
from tests.helpers import FakeProvider

GOLD = INSTRUMENTS["XAUUSD"]
BTC = INSTRUMENTS["BTCUSD"]
MON = 1709510400            # 2024-03-04 00:00 UTC, a Monday (New York and Amsterdam still on winter time)
FRI_CLOSE = MON + 4 * 86400 + 22 * 3600   # Friday 17:00 New York = 22:00 UTC
COSTS = CostModel(0.3, 0.1, 0.0, 0.0)
LIMITS = RiskLimits()


def executor(risk=LIMITS, risk_pct=2.0, instrument=GOLD, capital=1000.0):
    return BacktestExecutor(instrument, COSTS, SizingRules(risk_pct, "fractional", 100), capital, EventLog(), risk=risk)


def bar(ts, o, h=None, low=None, c=None):
    return Bar(ts, o, h if h is not None else o, low if low is not None else o, c if c is not None else o, 0.0)


def open_long(ex, ts, price=2000.0, sl=1990.0, tp=None):
    ex.submit([OrderRequest("open", f"t:{ts}", ts - 60, side="long", stop_loss=sl, take_profit=tp)])
    ex.on_bar(bar(ts, price), 1.0)


def risk_events(ex, code=None):
    return [e for e in ex.log.events if e["kind"] == "risk" and (code is None or e.get("reason_code") == code)]


# ---------- the limits themselves ----------

def test_risk_per_trade_is_capped():
    ex = executor(risk_pct=5.0)
    open_long(ex, MON + 3600)
    # 2% of €1000 = €20; the stop is 10.4 away (fill at ask 2000.4) on 100 oz per lot.
    assert ex.pos.lots == pytest.approx(20 / (10.4 * 100))
    assert risk_events(ex, "risk_per_trade")

    no_limits = executor(risk=None, risk_pct=5.0)
    open_long(no_limits, MON + 3600)
    assert no_limits.pos.lots == pytest.approx(50 / (10.4 * 100))


def test_max_position_size_per_instrument():
    ex = executor(risk=replace(LIMITS, max_lots={"XAUUSD": 0.01}))
    open_long(ex, MON + 3600)
    assert ex.pos.lots == 0.01
    assert risk_events(ex, "max_lots")


def test_max_open_positions_over_all_sessions():
    ex = executor()
    ex.open_elsewhere = 3
    open_long(ex, MON + 3600)
    assert ex.pos is None and risk_events(ex, "max_positions")
    ex.open_elsewhere = 2
    open_long(ex, MON + 7200)
    assert ex.pos is not None


def test_daily_loss_closes_and_blocks_until_tomorrow():
    ex = executor(risk=replace(LIMITS, max_daily_loss_pct=1.0))
    t = MON + 9 * 3600
    open_long(ex, t, sl=1900.0)
    ex.on_bar(bar(t + 60, 1940), 1.0)    # well past −1% of equity, stop-loss not hit
    assert ex.pos is None and risk_events(ex, "daily_loss")
    assert ex.trades[-1]["exit_reason"] == "Maximaal dagverlies"

    # Same day: a new order is refused.
    open_long(ex, t + 3600, price=1940, sl=1930)
    assert ex.pos is None
    assert any("dagverlies" in e["message"] for e in ex.log.events if e.get("client_id") == f"t:{t + 3600}")

    # Next day (00:00 Amsterdam = 23:00 UTC in winter): trading is allowed again.
    open_long(ex, MON + 23 * 3600 + 60, price=1940, sl=1930)
    assert ex.pos is not None


def test_daily_loss_limit_exact():
    ex = executor(risk=replace(LIMITS, max_daily_loss_pct=1.0), risk_pct=2.0)
    t = MON + 9 * 3600
    open_long(ex, t, sl=1900.0)
    lots = ex.pos.lots
    # Equity drop of 1% = €10 → price drop of 10 / (lots × 100) below the entry.
    drop = 10 / (lots * 100)
    price = ex.pos.entry_price - drop - 0.5
    ex.on_bar(bar(t + 60, price, price, price, price), 1.0)
    assert ex.pos is None
    assert ex.blocked_day == day_key(t, "Europe/Amsterdam")
    assert ex.equity_curve[-1][1] == ex.balance


def test_weekend_closes_profitable_forex_and_metal_positions():
    ex = executor()
    t = FRI_CLOSE - 3600
    open_long(ex, t, price=2000, sl=1990)
    ex.on_bar(bar(FRI_CLOSE - 32 * 60, 2010), 1.0)    # ends 31 minutes before the close: not yet
    assert ex.pos is not None
    ex.on_bar(bar(FRI_CLOSE - 31 * 60, 2010), 1.0)    # ends 30 minutes before the close
    assert ex.pos is None and ex.trades[-1]["exit_reason"] == "Weekend (in de winst)"
    assert ex.trades[-1]["pnl"] > 0


def test_weekend_keeps_losing_positions_and_crypto():
    ex = executor()
    open_long(ex, FRI_CLOSE - 3600, price=2000, sl=1980)
    ex.on_bar(bar(FRI_CLOSE - 60, 1995), 1.0)
    assert ex.pos is not None   # in loss: stays open with its stop-loss

    crypto = BacktestExecutor(BTC, CostModel(10, 5, 0, 0), SizingRules(2, "fractional", 2), 1000, EventLog(),
                              risk=LIMITS)
    crypto.submit([OrderRequest("open", "b", FRI_CLOSE - 7200, side="long", stop_loss=60000)])
    crypto.on_bar(bar(FRI_CLOSE - 3600, 62000), 1.0)
    crypto.on_bar(bar(FRI_CLOSE - 60, 64000), 1.0)
    assert crypto.pos is not None   # crypto trades through the weekend


def test_before_weekend_follows_new_york_time():
    assert not before_weekend(FRI_CLOSE - 31 * 60, 30)
    assert before_weekend(FRI_CLOSE - 30 * 60, 30)
    assert before_weekend(FRI_CLOSE + 86400, 30)            # Saturday
    assert not before_weekend(FRI_CLOSE + 2 * 86400, 30)    # Sunday 17:00 New York: market open
    summer_friday_close = 1720213200                        # 2024-07-05 21:00 UTC = 17:00 New York (EDT)
    assert before_weekend(summer_friday_close - 30 * 60, 30)
    assert not before_weekend(summer_friday_close - 31 * 60, 30)


def test_risk_state_survives_restore():
    ex = executor(risk=replace(LIMITS, max_daily_loss_pct=1.0))
    t = MON + 9 * 3600
    open_long(ex, t, sl=1900.0)
    ex.on_bar(bar(t + 60, 1930), 1.0)
    state = json.loads(json.dumps(ex.to_state()))
    again = executor(risk=replace(LIMITS, max_daily_loss_pct=1.0))
    again.restore(state)
    assert again.blocked_day == ex.blocked_day and again.day_start_equity == ex.day_start_equity
    open_long(again, t + 3600, price=1930, sl=1920)
    assert again.pos is None


# ---------- configuration ----------

def test_config_file_has_the_agreed_limits():
    s = load_settings()
    assert s.risk.max_daily_loss_pct == 2.0 and s.risk.max_open_positions == 3
    assert s.risk.max_risk_per_trade_pct == 2.0 and s.risk.weekend_close
    assert s.risk.lots_limit("XAUUSD") == 1.0 and s.risk.timezone == "Europe/Amsterdam"
    assert s.alerts.feed_down_minutes == 15 and s.alerts.repeat_minutes == 60


def test_config_rejects_bad_limits(tmp_path):
    def load(text):
        p = tmp_path / "c.toml"
        p.write_text(text)
        return load_settings(p)

    with pytest.raises(ConfigError):
        load("[risk]\nmax_daily_loss_pct = 0\n")
    with pytest.raises(ConfigError):
        load("[risk.max_lots]\nNOPE = 1\n")
    with pytest.raises(ConfigError):
        load("[account]\nrisk_per_trade_pct = 3.0\n[risk]\nmax_risk_per_trade_pct = 2.0\n")
    assert load("[risk]\nmax_open_positions = 5\n").risk.max_open_positions == 5


# ---------- alerts ----------

class FakeSender:
    configured = True
    to = "test@example.com"

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, subject, body):
        if self.fail:
            raise OSError("no network")
        self.sent.append((subject, body))


def test_at_most_one_email_per_problem_per_hour(tmp_path):
    conn = connect(tmp_path / "a.sqlite")
    clock = {"now": MON}
    sender = FakeSender()
    alerter = Alerter(conn, sender, 3600, now=lambda: clock["now"])
    run = asyncio.run

    run(alerter.raise_("feed:XAUUSD", "urgent", "Geen verbinding", "x", email=True))
    clock["now"] += 600
    run(alerter.raise_("feed:XAUUSD", "urgent", "Geen verbinding", "x", email=True))
    assert len(sender.sent) == 1
    alerter.resolve("feed:XAUUSD")
    clock["now"] += 600
    run(alerter.raise_("feed:XAUUSD", "urgent", "Geen verbinding", "x", email=True))  # back within the hour
    assert len(sender.sent) == 1
    run(alerter.raise_("loop", "urgent", "Loop", "y", email=True))  # another problem: its own e-mail
    assert len(sender.sent) == 2
    clock["now"] += 3600
    run(alerter.raise_("feed:XAUUSD", "urgent", "Geen verbinding", "x", email=True))
    assert len(sender.sent) == 3
    open_feed = [a for a in alerter.list() if a["key"] == "feed:XAUUSD" and a["resolved_at"] is None]
    assert len(open_feed) == 1 and open_feed[0]["count"] == 2
    run(alerter.raise_("info", "warning", "Geen mail", "z"))
    assert len(sender.sent) == 3


def test_failed_email_is_recorded_without_breaking(tmp_path):
    conn = connect(tmp_path / "a.sqlite")
    alerter = Alerter(conn, FakeSender(fail=True), 3600, now=lambda: MON)
    a = asyncio.run(alerter.raise_("loop", "urgent", "Loop", "x", email=True))
    assert a["emailed_at"] is None and "internet" in a["email_error"]
    assert asyncio.run(alerter.send_test()) is not None
    unconfigured = Alerter(conn, None, 3600, now=lambda: MON)
    a = asyncio.run(unconfigured.raise_("loop2", "urgent", "Loop", "x", email=True))
    assert "Geen e-mail ingesteld" in a["email_error"]


# ---------- monitor ----------

def make_monitor(tmp_path, clock, sender):
    settings = replace(Settings(), db_path=tmp_path / "m.sqlite")
    conn = connect(settings.db_path)
    from backend.data.store import CandleStore
    paper = PaperEngine(conn, CandleStore(conn, FakeProvider()), settings, now=lambda: clock["now"])
    alerter = Alerter(conn, sender, 3600, now=lambda: clock["now"])
    return Monitor(paper, alerter, settings, now=lambda: clock["now"]), paper, alerter


def test_feed_down_for_15_minutes_sends_one_email(tmp_path):
    clock = {"now": MON}
    sender = FakeSender()
    monitor, _, alerter = make_monitor(tmp_path, clock, sender)
    down = {"sessions": 1, "feed": {"XAUUSD": "timeout"}, "session_errors": []}
    for _ in range(29):                   # 14.5 minutes
        asyncio.run(monitor.after_tick(down, None))
        clock["now"] += 30
    assert sender.sent == []
    for _ in range(10):
        asyncio.run(monitor.after_tick(down, None))
        clock["now"] += 30
    assert len(sender.sent) == 1 and "XAUUSD" in sender.sent[0][0]
    asyncio.run(monitor.after_tick({"sessions": 1, "feed": {"XAUUSD": None}, "session_errors": []}, None))
    assert alerter.open_counts().get("urgent") is None


def test_loop_errors_send_email_after_three_rounds(tmp_path):
    clock = {"now": MON}
    sender = FakeSender()
    monitor, _, _ = make_monitor(tmp_path, clock, sender)
    for _ in range(2):
        asyncio.run(monitor.after_tick(None, RuntimeError("kapot")))
    assert sender.sent == []
    asyncio.run(monitor.after_tick(None, RuntimeError("kapot")))
    assert len(sender.sent) == 1 and "kapot" in sender.sent[0][1]


def test_unclean_stop_is_reported_at_next_start(tmp_path):
    clock = {"now": MON}
    sender = FakeSender()
    monitor, paper, _ = make_monitor(tmp_path, clock, sender)
    paper.create({"symbol": "XAUUSD", "timeframe": "H1", "strategy": "sma_cross@v1", "params": PARAMS,
                  "capital": 1000.0, "risk_pct": 2.0, "sizing_mode": "fractional", "leverage": 100,
                  "costs": {"spread": 0.3, "slippage": 0.1, "commission_per_lot": 0.0, "financing_pct": 0.0}})
    asyncio.run(monitor.on_startup())
    asyncio.run(monitor.after_tick({"sessions": 0}, None))   # writes a heartbeat
    monitor.on_shutdown()
    asyncio.run(monitor.on_startup())                       # clean shutdown: nothing to report
    assert sender.sent == []
    clock["now"] += 3 * 3600                                # crash: no on_shutdown
    asyncio.run(monitor.on_startup())
    assert len(sender.sent) == 1 and "onverwacht" in sender.sent[0][0]


# ---------- API: kill switch, stop, overview, reconciliation ----------

PARAMS = {"fast": 5, "slow": 12}
BODY = {"symbol": "XAUUSD", "timeframe": "M15", "strategy": "sma_cross@v1", "params": PARAMS,
        "capital": 1000, "risk_pct": 2, "sizing_mode": "fractional"}


@pytest.fixture
def api(tmp_path):
    settings = replace(Settings(), db_path=tmp_path / "api.sqlite")
    sender = FakeSender()
    with TestClient(create_app(settings, provider=FakeProvider(), start_loop=False, email_sender=sender)) as client:
        clock = {"now": MON + 3600}
        client.app.state.paper.now = lambda: clock["now"]
        client.app.state.store.now = lambda: clock["now"]
        client.app.state.alerter.now = lambda: clock["now"]
        yield client, clock, sender


def tick_until_position(client, clock, session_id, start=1):
    for i in range(start, start + 120):
        clock["now"] = MON + 3600 + i * 300
        client.post("/api/paper/tick")
        if client.get(f"/api/paper/sessions/{session_id}").json()["position"]:
            return i
    raise AssertionError("no position opened")


def test_kill_switch_stops_everything(api):
    client, clock, _ = api
    a = client.post("/api/paper/sessions", json=BODY).json()
    b = client.post("/api/paper/sessions", json={**BODY, "timeframe": "M5"}).json()
    tick_until_position(client, clock, a["id"])

    r = client.post("/api/risk/kill", json={"close_positions": True}).json()
    assert r["paused"] == 2 and r["closed"] >= 1
    for s in (a, b):
        d = client.get(f"/api/paper/sessions/{s['id']}").json()
        assert d["status"] == "paused" and d["status_reason"] == "kill switch"
        assert d["position"] is None and d["pending_orders"] == 0
    assert any(t["exit_reason"] == "Kill switch" for t in client.get(f"/api/paper/sessions/{a['id']}").json()["trades"])

    # Nothing can start or resume while the kill switch is active.
    assert client.post("/api/paper/sessions", json=BODY).status_code == 409
    assert client.post(f"/api/paper/sessions/{a['id']}/resume").status_code == 409
    assert client.post("/api/paper/tick").json()["sessions"] == 0
    overview = client.get("/api/risk").json()
    assert overview["kill_switch"]["active"] and overview["open_positions"] == 0
    assert client.get("/api/risk/brief").json()["kill_switch"]["active"]

    assert client.post("/api/risk/release").json()["active"] is False
    assert client.post("/api/risk/release").status_code == 400
    assert client.post(f"/api/paper/sessions/{a['id']}/resume").json()["status"] == "running"


def test_kill_switch_can_keep_positions(api):
    client, clock, _ = api
    a = client.post("/api/paper/sessions", json=BODY).json()
    tick_until_position(client, clock, a["id"])
    r = client.post("/api/risk/kill", json={"close_positions": False}).json()
    assert r["closed"] == 0
    d = client.get(f"/api/paper/sessions/{a['id']}").json()
    assert d["status"] == "paused" and d["position"] is not None


def test_stopping_a_session_closes_its_position(api):
    client, clock, _ = api
    a = client.post("/api/paper/sessions", json=BODY).json()
    tick_until_position(client, clock, a["id"])
    d = client.post(f"/api/paper/sessions/{a['id']}/stop").json()
    assert d["status"] == "stopped" and d["position"] is None
    trades = client.get(f"/api/paper/sessions/{a['id']}").json()["trades"]
    assert trades[-1]["exit_reason"] == "Sessie gestopt"


def test_risk_per_trade_above_limit_is_refused(api):
    client, _, _ = api
    r = client.post("/api/paper/sessions", json={**BODY, "risk_pct": 3})
    assert r.status_code == 400 and "harde limiet" in r.json()["detail"]


def test_overview_reconciliation_and_test_email(api):
    client, clock, sender = api
    a = client.post("/api/paper/sessions", json=BODY).json()
    for i in range(1, 60):
        clock["now"] = MON + 3600 + i * 600
        client.post("/api/paper/tick")
    overview = client.get("/api/risk").json()
    assert overview["limits"]["max_open_positions"] == 3
    assert overview["sessions"][0]["id"] == a["id"]
    assert "day_pnl_pct" in overview["sessions"][0]

    report = client.post("/api/reconcile").json()
    assert report["differences"] == 0, report
    assert report["sessions"][0]["checks"]

    # Tamper with the bookkeeping: the reconciliation must notice.
    conn = client.app.state.paper.conn
    row = conn.execute("SELECT state FROM paper_sessions WHERE id=?", (a["id"],)).fetchone()
    state = json.loads(row["state"])
    state["account"]["balance"] += 5
    with conn:
        conn.execute("UPDATE paper_sessions SET state=? WHERE id=?", (json.dumps(state), a["id"]))
    report = client.post("/api/reconcile").json()
    assert report["differences"] >= 1
    alerts = client.get("/api/risk").json()["alerts"]
    assert any(x["key"] == "reconcile" and x["resolved_at"] is None for x in alerts)
    assert sender.sent == []   # reconciliation e-mail is off by default

    assert client.post("/api/alerts/test").json()["ok"]
    assert sender.sent[-1][0] == "[Trading Dashboard] Testmail"
    alert_id = next(x["id"] for x in alerts if x["key"] == "reconcile")
    assert client.post(f"/api/alerts/{alert_id}/resolve").json()["ok"]
    assert client.post(f"/api/alerts/{alert_id}/resolve").status_code == 404


def test_reconciliation_of_closed_trades_and_open_positions(tmp_path):
    settings = replace(Settings(), db_path=tmp_path / "r.sqlite")
    conn = connect(settings.db_path)
    from backend.data.store import CandleStore
    paper = PaperEngine(conn, CandleStore(conn, FakeProvider()), settings)
    assert reconcile_paper(paper, settings.risk)["differences"] == 0
    set_state(conn, "x", {"a": 1})
    assert get_state(conn, "x") == {"a": 1} and get_state(conn, "missing", 5) == 5


def test_env_file_is_ignored_by_git():
    ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    assert ".env" in ignore.split()
