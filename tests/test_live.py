"""Live trading against a fake cTrader server (same protocol, in memory). No real orders, ever."""

import asyncio
import json
import math
import threading
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.broker.ctrader import CTraderClient
from backend.broker.ctrader_messages import OpenApiMessages_pb2 as M
from backend.config import ExecutionSettings, Settings
from backend.live.api import parse_amount
from backend.live.engine import client_order_id
from backend.main import create_app
from tests.fake_ctrader import ACCOUNT, CLIENT_ID, CLIENT_SECRET, GOLD_ID, TOKEN, FakeCTrader
from tests.helpers import FakeProvider
from tests.test_risk import FakeSender

MON = 1709510400 + 9 * 3600     # Monday 2024-03-04 09:00 UTC
ENV = {"CTRADER_CLIENT_ID": CLIENT_ID, "CTRADER_CLIENT_SECRET": CLIENT_SECRET, "CTRADER_ACCESS_TOKEN": TOKEN}
PARAMS = {"fast": 3, "slow": 8}


def gold(symbol_id, ts):
    """Swinging gold price: plenty of moving-average crossings on M5."""
    return round(2000 + 6 * math.sin(ts / 2400) + 2 * math.sin(ts / 700), 2) if symbol_id == GOLD_ID else 1.08


class FakeBroker:
    """Runs the fake cTrader server in its own thread and event loop."""

    def __init__(self, clock, **kwargs):
        self.loop = asyncio.new_event_loop()
        self.fake = FakeCTrader(price=gold, now=lambda: clock["now"], **kwargs)
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.port = asyncio.run_coroutine_threadsafe(self.fake.start(), self.loop).result(5)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.fake.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)

    def call(self, fn, *args):
        return self.loop.call_soon_threadsafe(fn, *args)


def make_client(tmp_path, clock, mode="live", is_live=False, transport=None, env=ENV, timeout=3.0):
    broker = FakeBroker(clock, is_live=is_live)
    settings = replace(Settings(), db_path=tmp_path / "live.sqlite", execution=ExecutionSettings(mode=mode))
    app = create_app(settings, provider=FakeProvider(), start_loop=False, env=env, email_sender=FakeSender(),
                     broker_client_factory=lambda is_live: CTraderClient("127.0.0.1", broker.port, use_ssl=False,
                                                                         timeout=timeout, history_spacing=0),
                     oauth_transport=transport)
    return app, broker


@pytest.fixture
def live(tmp_path):
    clock = {"now": MON}
    app, broker = make_client(tmp_path, clock)
    with TestClient(app) as client:
        for obj in (app.state.live, app.state.paper, app.state.store, app.state.alerter, app.state.live.link):
            obj.now = lambda: clock["now"]
        yield client, clock, broker
    broker.stop()


def connect(client):
    accounts = client.post("/api/live/accounts").json()
    assert accounts == [{"account_id": ACCOUNT, "is_live": False, "login": 5551234}]
    assert client.post("/api/live/account", json={"account_id": ACCOUNT}).json()["account_id"] == ACCOUNT


START = {"strategy": "sma_cross@v1", "params": PARAMS, "symbol": "XAUUSD", "timeframe": "M5", "capital": 500,
         "risk_pct": 2}
TYPED = {"capital": "500,00", "risk_eur": "€ 10,00", "daily_loss_eur": "10"}


def start_session(client):
    plan = client.post("/api/live/sessions/prepare", json=START).json()
    assert plan["capital"] == 500 and plan["risk_eur"] == 10 and plan["daily_loss_eur"] == 10
    assert plan["account"]["is_live"] is False and plan["account"]["currency"] == "EUR"
    s = client.post("/api/live/sessions", json={**START, "typed": TYPED}).json()
    assert s["mode"] == "live" and s["status"] == "running", s
    return s


def run_until(client, clock, broker, condition, minutes=600, step=120):
    for _ in range(max(1, minutes * 60 // step)):
        clock["now"] += step
        r = client.portal.call(client.app.state.live.tick)
        assert not r["errors"], r
        if condition():
            return
    raise AssertionError("condition never met")


def new_orders(broker):
    return broker.fake.payloads(M.ProtoOANewOrderReq)


# ---------- tests ----------

def test_parse_amount():
    assert parse_amount("1.000,00") == 1000 and parse_amount("€ 10,50") == 10.5 and parse_amount("1000.5") == 1000.5
    assert parse_amount("1.000") == 1000 and parse_amount("") is None and parse_amount("tien") is None


def test_connect_account_and_symbols(live):
    client, _, _ = live
    st = client.get("/api/live/status").json()
    assert st["enabled"] and st["link"]["configured"] and st["link"]["linked"] and st["link"]["account"] is None
    connect(client)
    acc = client.get("/api/live/account").json()
    assert acc["info"]["balance"] == 1000 and acc["info"]["broker"] == "BlackBull Markets"
    gold_row = next(s for s in acc["symbols"] if s["symbol"] == "XAUUSD")
    assert gold_row["matches"] and gold_row["min_lots"] == 0.01
    assert next(s for s in acc["symbols"] if s["symbol"] == "BTCUSD")["broker_name"] is None


def test_typed_confirmation_is_required(live):
    client, _, _ = live
    connect(client)
    assert client.post("/api/live/sessions", json=START).status_code == 400
    wrong = client.post("/api/live/sessions", json={**START, "typed": {**TYPED, "risk_eur": "20"}})
    assert wrong.status_code == 400 and "risico per trade" in wrong.json()["detail"]
    assert client.post("/api/live/sessions/prepare", json={**START, "risk_pct": 3}).status_code == 400
    assert client.post("/api/live/sessions/prepare", json={**START, "capital": 5000}).status_code == 400
    assert start_session(client)["broker"]["label"].startswith("td-")


def test_live_trades_at_the_broker_with_stop_loss_there(live):
    client, clock, broker = live
    connect(client)
    s = start_session(client)
    run_until(client, clock, broker, lambda: bool(broker.fake.positions))
    order = new_orders(broker)[0]
    assert order.label == f"td-{s['id']}" and order.relativeStopLoss > 0 and order.clientOrderId == order.comment
    assert order.volume % 100 == 0                       # whole 0.01 lots of gold
    amend = broker.fake.payloads(M.ProtoOAAmendPositionSLTPReq)
    assert amend and amend[0].HasField("stopLoss")       # exact stop-loss level set at the broker
    position = next(iter(broker.fake.positions.values()))
    assert position.stopLoss == amend[0].stopLoss

    run_until(client, clock, broker, lambda: bool(client.get(f"/api/paper/sessions/{s['id']}").json()["position"]),
              minutes=5)
    report = client.post("/api/reconcile").json()
    live_rows = [x for x in report["sessions"] if x["label"].startswith("LIVE")]
    assert live_rows and live_rows[0]["differences"] == 0, live_rows

    # The broker hits the stop-loss: the next round books the trade from the broker's deals.
    broker.loop.call_soon_threadsafe(broker.fake.hit_stop, position.positionId)
    run_until(client, clock, broker, lambda: bool(client.get(f"/api/paper/sessions/{s['id']}").json()["trades"]),
              minutes=5)
    trade = client.get(f"/api/paper/sessions/{s['id']}").json()["trades"][0]
    assert trade["exit_reason"] == "Stop-loss (bij de broker)" and trade["source"] == "broker"
    assert trade["pnl"] < 0 and trade["exit_price"] == pytest.approx(position.stopLoss)
    report = client.post("/api/reconcile").json()
    assert all(x["differences"] == 0 for x in report["sessions"] if x["label"].startswith("LIVE")), report


def test_no_duplicate_order_after_lost_confirmation(live):
    client, clock, broker = live
    connect(client)
    s = start_session(client)
    broker.fake.drop_fill_reply = True                    # the broker fills, but the confirmation never arrives
    run_until(client, clock, broker, lambda: bool(broker.fake.positions))
    broker.fake.drop_fill_reply = False
    first = new_orders(broker)[0]
    orders = client.get(f"/api/live/orders?session_id={s['id']}").json()
    assert orders[0]["status"] == "unknown"
    run_until(client, clock, broker, lambda: client.get(f"/api/live/orders?session_id={s['id']}").json()[-1]["status"]
              == "filled", minutes=3)
    same = [o for o in new_orders(broker) if o.clientOrderId == first.clientOrderId]
    assert len(same) == 1                                   # found at the broker, never sent again
    assert len(broker.fake.positions) == 1


def test_idempotent_order_ids():
    assert client_order_id(1, "a:b:1") == client_order_id(1, "a:b:1")
    assert client_order_id(1, "a:b:1") != client_order_id(2, "a:b:1")
    assert len(client_order_id(1, "x")) == 26


def test_kill_switch_closes_live_positions_at_the_broker(live):
    client, clock, broker = live
    connect(client)
    s = start_session(client)
    run_until(client, clock, broker, lambda: bool(broker.fake.positions))
    run_until(client, clock, broker, lambda: bool(client.get(f"/api/paper/sessions/{s['id']}").json()["position"]),
              minutes=5)
    r = client.post("/api/risk/kill", json={"close_positions": True}).json()
    assert r["closed"] == 1 and not r["errors"]
    assert broker.fake.positions == {}
    d = client.get(f"/api/paper/sessions/{s['id']}").json()
    assert d["status"] == "paused" and d["position"] is None
    assert d["trades"][-1]["exit_reason"] == "Kill switch"
    assert client.post("/api/live/sessions", json={**START, "typed": TYPED}).status_code == 409


def test_stop_closes_live_position(live):
    client, clock, broker = live
    connect(client)
    s = start_session(client)
    run_until(client, clock, broker, lambda: bool(broker.fake.positions))
    run_until(client, clock, broker, lambda: bool(client.get(f"/api/paper/sessions/{s['id']}").json()["position"]),
              minutes=5)
    d = client.post(f"/api/paper/sessions/{s['id']}/stop").json()
    assert d["status"] == "stopped" and broker.fake.positions == {}


def test_demo_test_order(live):
    client, _, broker = live
    connect(client)
    r = client.post("/api/live/test-order").json()
    assert r["ok"] and any("Stop-loss staat bij de broker" in step for step in r["steps"]), r
    assert broker.fake.positions == {}


def test_real_money_and_paper_mode_are_refused(tmp_path):
    clock = {"now": MON}
    app, broker = make_client(tmp_path, clock, is_live=True)
    with TestClient(app) as client:
        client.post("/api/live/accounts")
        client.post("/api/live/account", json={"account_id": ACCOUNT})
        r = client.post("/api/live/sessions/prepare", json=START)
        assert r.status_code == 409 and "echt-geld" in r.json()["detail"]
        assert client.post("/api/live/test-order").status_code == 409
    broker.stop()

    app, broker = make_client(tmp_path / "p", clock, mode="paper")
    with TestClient(app) as client:
        r = client.post("/api/live/sessions/prepare", json=START)
        assert r.status_code == 409 and "Live trading staat uit" in r.json()["detail"]
    broker.stop()


def test_oauth_flow_and_refresh(tmp_path):
    calls = []

    def handler(request: httpx.Request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json={"accessToken": TOKEN, "refreshToken": "r2", "expiresIn": 86400,
                                         "tokenType": "bearer"})

    clock = {"now": MON}
    env = {"CTRADER_CLIENT_ID": CLIENT_ID, "CTRADER_CLIENT_SECRET": CLIENT_SECRET}
    app, broker = make_client(tmp_path, clock, transport=httpx.MockTransport(handler), env=env)
    with TestClient(app) as client:
        assert client.get("/api/live/status").json()["link"]["linked"] is False
        r = client.get("/ctrader/connect", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"].startswith("https://openapi.ctrader.com/apps/auth")
        assert "scope=trading" in r.headers["location"] and CLIENT_ID in r.headers["location"]
        r = client.get("/ctrader/callback?code=abc", follow_redirects=False)
        assert r.headers["location"] == "/#live?gekoppeld=1"
        assert calls[0]["grant_type"] == "authorization_code" and calls[0]["code"] == "abc"
        token = json.loads((tmp_path / "ctrader_token.json").read_text())
        assert token["access_token"] == TOKEN
        assert (tmp_path / "ctrader_token.json").stat().st_mode & 0o077 == 0     # only readable by you
        st = client.get("/api/live/status").json()["link"]
        assert st["linked"] and st["accounts"][0]["account_id"] == ACCOUNT
        assert "access_token" not in json.dumps(st)                               # never shown in the dashboard
        # Expires within 3 days: renewed automatically.
        client.post("/api/live/account", json={"account_id": ACCOUNT})
        assert calls[-1]["grant_type"] == "refresh_token" and calls[-1]["refresh_token"] == "r2"
    broker.stop()


def test_unknown_and_unprotected_positions_are_reported(live):
    client, clock, broker = live
    connect(client)
    start_session(client)

    def add_foreign():
        from backend.broker.ctrader_messages import OpenApiModelMessages_pb2 as MM
        p = MM.ProtoOAPosition(positionId=9999, positionStatus=MM.POSITION_STATUS_OPEN, price=2000.0, swap=0,
                               moneyDigits=2)
        p.tradeData.symbolId, p.tradeData.volume, p.tradeData.tradeSide = GOLD_ID, 100, MM.BUY
        p.tradeData.label = "td-77"
        broker.fake.positions[9999] = p

    broker.loop.call_soon_threadsafe(add_foreign)
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0.05), broker.loop).result(2)
    report = client.post("/api/reconcile").json()
    assert report["differences"] >= 1 and report["unknown_positions"][0]["position_id"] == 9999


def test_kill_right_after_an_order_closes_the_new_position(live):
    """The order went out in this round, so the saved account does not know the position yet."""
    client, clock, broker = live
    connect(client)
    s = start_session(client)
    run_until(client, clock, broker, lambda: bool(broker.fake.positions))
    assert client.get(f"/api/paper/sessions/{s['id']}").json()["position"] is None
    r = client.post("/api/risk/kill", json={"close_positions": True}).json()
    assert r["closed"] == 1 and broker.fake.positions == {}
    d = client.get(f"/api/paper/sessions/{s['id']}").json()
    assert d["position"] is None and d["trades"][-1]["exit_reason"] == "Kill switch"
    report = client.post("/api/reconcile").json()
    assert all(x["differences"] == 0 for x in report["sessions"] if x["label"].startswith("LIVE")), report


def test_trailing_stop_is_moved_at_the_broker(live):
    client, clock, broker = live
    connect(client)
    body = {**START, "strategy": "bjorgum_3commas@v1",
            "params": {"fast": 3, "slow": 8, "use_trailing": True, "trail_trigger": 0.2, "trail_atr": 0.5}}
    plan = client.post("/api/live/sessions/prepare", json=body).json()
    s = client.post("/api/live/sessions", json={**body, "typed": TYPED}).json()
    assert s["mode"] == "live", (plan, s)

    def moved_twice():
        amends = broker.fake.payloads(M.ProtoOAAmendPositionSLTPReq)
        per_position = {}
        for a in amends:
            per_position[a.positionId] = per_position.get(a.positionId, 0) + 1
        return any(n >= 2 for n in per_position.values())

    run_until(client, clock, broker, moved_twice, minutes=900)
    events = client.get(f"/api/paper/sessions/{s['id']}").json()["events"]
    assert any(e["message"].startswith("Stop-loss bij de broker verplaatst") for e in events)
    orders = client.get(f"/api/live/orders?session_id={s['id']}").json()
    assert any(o["kind"] == "modify" and o["status"] == "filled" for o in orders)
