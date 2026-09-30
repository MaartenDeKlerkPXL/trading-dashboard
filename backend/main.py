"""FastAPI app: JSON API under /api, dashboard files under /."""

from __future__ import annotations

import platform
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import FRONTEND_DIR, Settings, load_settings
from .data.instruments import INSTRUMENTS, TIMEFRAMES
from .data.jobs import JobManager
from .data.providers import DataProvider, get_provider
from .data.store import CandleStore
from .db import connect


class SyncRequest(BaseModel):
    symbol: str
    timeframe: str
    start: str  # YYYY-MM-DD, UTC
    end: str    # YYYY-MM-DD, inclusive


def _parse_day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"Ongeldige {label}: '{value}'. Gebruik het formaat JJJJ-MM-DD.") from None


def _validate(symbol: str, timeframe: str, start: str, end: str) -> tuple[int, int]:
    if symbol not in INSTRUMENTS:
        raise HTTPException(400, f"Onbekend instrument '{symbol}'.")
    if timeframe not in TIMEFRAMES:
        raise HTTPException(400, f"Onbekende timeframe '{timeframe}'.")
    d0, d1 = _parse_day(start, "startdatum"), _parse_day(end, "einddatum")
    if d1 < d0:
        raise HTTPException(400, "De einddatum ligt vóór de startdatum.")
    tf = TIMEFRAMES[timeframe]
    if (d1 - d0).days + 1 > tf.max_days:
        raise HTTPException(
            400,
            f"Periode te lang voor {timeframe}: maximaal {tf.max_days} dagen. "
            "Kies een kortere periode of een grotere timeframe.",
        )
    start_ts = int(datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc).timestamp())
    end_day = d1 + timedelta(days=1)
    end_ts = int(datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc).timestamp())
    return start_ts, end_ts


def create_app(settings: Settings | None = None, provider: DataProvider | None = None, store_kwargs: dict | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(settings.db_path)
        store = CandleStore(conn, provider or get_provider(settings.data.provider), **(store_kwargs or {}))
        app.state.store = store
        app.state.jobs = JobManager(store)
        try:
            yield
        finally:
            conn.close()

    app = FastAPI(title="Trading Dashboard", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    @app.middleware("http")
    async def no_cache_for_dashboard(request: Request, call_next):
        response = await call_next(request)
        # Always serve the latest dashboard files after an update.
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "mode": settings.execution.mode,
            "provider": settings.data.provider,
            "python": platform.python_version(),
            "server_time": int(datetime.now(timezone.utc).timestamp()),
        }

    @app.get("/api/config")
    def config():
        return {
            "timezone": settings.app.timezone,
            "mode": settings.execution.mode,
            "provider": settings.data.provider,
            "account": {
                "currency": settings.account.currency,
                "starting_capital": settings.account.starting_capital,
                "leverage": settings.account.leverage,
                "risk_per_trade_pct": settings.account.risk_per_trade_pct,
            },
            "defaults": {"symbol": settings.data.default_symbol, "timeframe": settings.data.default_timeframe},
            "instruments": [
                {"symbol": i.symbol, "name": i.name, "category": i.category, "digits": i.digits}
                for i in INSTRUMENTS.values()
            ],
            "timeframes": [
                {"code": t.code, "seconds": t.seconds, "max_days": t.max_days} for t in TIMEFRAMES.values()
            ],
        }

    @app.post("/api/data/sync")
    async def start_sync(req: SyncRequest, request: Request):
        start_ts, end_ts = _validate(req.symbol, req.timeframe, req.start, req.end)
        job = request.app.state.jobs.start_sync(req.symbol, TIMEFRAMES[req.timeframe].level, start_ts, end_ts)
        return job.to_dict()

    @app.get("/api/data/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        job = request.app.state.jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Download-taak niet gevonden (is de server herstart?).")
        return job.to_dict()

    @app.get("/api/candles")
    def candles(symbol: str, timeframe: str, start: str, end: str, request: Request):
        start_ts, end_ts = _validate(symbol, timeframe, start, end)
        store: CandleStore = request.app.state.store
        rows = store.read(symbol, timeframe, start_ts, end_ts)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": store.provider.name,
            "price_type": "bid",
            "count": len(rows),
            "missing_chunks": store.missing_count(symbol, TIMEFRAMES[timeframe].level, start_ts, end_ts),
            "candles": [
                {"time": c.ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
                for c in rows
            ],
        }

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="dashboard")
    return app
