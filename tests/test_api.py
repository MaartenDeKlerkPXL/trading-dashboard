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
    with TestClient(create_app(settings, provider=FakeProvider())) as c:
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


def test_live_mode_is_refused(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[execution]\nmode = "live"\n')
    with pytest.raises(ConfigError):
        load_settings(cfg)
    cfg.write_text('[execution]\nmode = "paper"\n[data]\nnope = 1\n')
    with pytest.raises(ConfigError):
        load_settings(cfg)
