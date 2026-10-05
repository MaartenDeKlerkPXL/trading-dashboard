import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.config import ConfigError, Settings, load_settings
from backend.main import create_app
from tests.helpers import FakeProvider


@pytest.fixture
def client(tmp_path):
    settings = replace(Settings(), db_path=tmp_path / "api.sqlite")
    with TestClient(create_app(settings, provider=FakeProvider(), start_loop=False)) as c:
        yield c


def test_health_and_config(client):
    assert client.get("/api/health").json()["status"] == "ok"
    cfg = client.get("/api/config").json()
    assert cfg["defaults"]["symbol"] == "XAUUSD"
    assert "H1" in [t["code"] for t in cfg["timeframes"]]


def test_dashboard_files_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>" in r.text
    assert client.get("/script.js").status_code == 200
    assert client.get("/style.css").status_code == 200
    assert client.get("/common.js").status_code == 200
    assert client.get("/backtest.js").status_code == 200


def test_validation_messages(client):
    r = client.get("/api/candles", params={"symbol": "NOPE", "timeframe": "H1", "start": "2024-01-01", "end": "2024-01-02"})
    assert r.status_code == 400 and "Onbekend instrument" in r.json()["detail"]
    r = client.get("/api/candles", params={"symbol": "XAUUSD", "timeframe": "M1", "start": "2020-01-01", "end": "2024-01-02"})
    assert r.status_code == 400 and "te lang" in r.json()["detail"]
    r = client.get("/api/candles", params={"symbol": "XAUUSD", "timeframe": "H1", "start": "2024-02-01", "end": "2024-01-02"})
    assert r.status_code == 400


def test_sync_then_read(client):
    body = {"symbol": "XAUUSD", "timeframe": "H1", "start": "2024-03-04", "end": "2024-03-08"}
    job = client.post("/api/data/sync", json=body).json()
    for _ in range(100):
        job = client.get(f"/api/data/jobs/{job['id']}").json()
        if job["status"] != "running":
            break
        time.sleep(0.02)
    assert job["status"] == "done", job
    data = client.get("/api/candles", params=body).json()
    assert data["count"] == 5 * 24 and data["missing_chunks"] == 0
    first = data["candles"][0]
    assert set(first) == {"time", "open", "high", "low", "close", "volume"}


def test_execution_modes(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[execution]\nmode = "live"\n')
    s = load_settings(cfg)
    assert s.execution.mode == "live" and s.live.allow_real_money is False   # real money stays off by default
    cfg.write_text('[execution]\nmode = "echt"\n')
    with pytest.raises(ConfigError):
        load_settings(cfg)
    cfg.write_text('[execution]\nmode = "paper"\n[data]\nnope = 1\n')
    with pytest.raises(ConfigError):
        load_settings(cfg)


def _sync(client, body):
    job = client.post("/api/data/sync", json=body).json()
    for _ in range(200):
        job = client.get(f"/api/data/jobs/{job['id']}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("sync did not finish")


def test_strategies_listed(client):
    data = client.get("/api/strategies").json()
    sma = next(s for s in data if s["key"] == "sma_cross@v1")
    assert sma["version"] == "v1" and {p["name"] for p in sma["params"]} >= {"fast", "slow", "atr_stop"}


def test_backtest_requires_data_first(client):
    body = {"symbol": "XAUUSD", "timeframe": "H1", "start": "2024-03-01", "end": "2024-03-31",
            "strategy": "sma_cross@v1"}
    r = client.post("/api/backtest", json=body)
    assert r.status_code == 409 and "Laad eerst" in r.json()["detail"]


def test_backtest_end_to_end(client):
    period = {"symbol": "XAUUSD", "timeframe": "H1", "start": "2024-01-01", "end": "2024-06-30"}
    assert _sync(client, period)["status"] == "done"
    body = {**period, "strategy": "sma_cross@v1", "params": {"fast": 10, "slow": 30},
            "capital": 1000, "risk_pct": 2, "sizing_mode": "fractional"}
    r = client.post("/api/backtest", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    m = data["metrics"]
    for key in ("total_return_pct", "max_drawdown_pct", "sharpe", "sortino", "winrate_pct",
                "profit_factor", "trades", "avg_trade", "expectancy_r"):
        assert key in m
    assert m["trades"] > 0 and data["equity"] and data["trades"]
    assert data["settings"]["params"]["fast"] == 10
    assert data["settings"]["costs"]["spread"] > 0
    # Conversion data (EURUSD daily) was downloaded through the provider, so no fallback warning.
    assert not any("vaste koers" in w for w in data["warnings"])


def test_backtest_validation_errors(client):
    period = {"symbol": "XAUUSD", "timeframe": "H1", "start": "2024-01-01", "end": "2024-01-31"}
    _sync(client, period)
    base = {**period, "strategy": "sma_cross@v1"}
    assert client.post("/api/backtest", json={**base, "strategy": "nope@v1"}).status_code == 400
    r = client.post("/api/backtest", json={**base, "params": {"fast": 60, "slow": 20}})
    assert r.status_code == 400 and "korter" in r.json()["detail"]
    r = client.post("/api/backtest", json={**base, "spread": 0})
    assert r.status_code == 400 and "spread" in r.json()["detail"]
