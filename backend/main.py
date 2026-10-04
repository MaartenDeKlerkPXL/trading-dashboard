"""FastAPI app: JSON API under /api, dashboard files under /."""

from __future__ import annotations

import logging
import platform
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import FRONTEND_DIR, Settings, load_settings
from .backtest.engine import BacktestConfig, RateSeries, run_backtest
from .data.instruments import EUR_CONVERSION, FALLBACK_EUR_RATES, INSTRUMENTS, TIMEFRAMES, Instrument
from .data.jobs import JobManager
from .data.providers import DataProvider, get_provider
from .data.store import CandleStore
from .db import connect
from .execution.base import CostModel, SizingRules
from .strategies import describe, load_strategies
from .strategies.base import Bar

log = logging.getLogger(__name__)


class SyncRequest(BaseModel):
    symbol: str
    timeframe: str
    start: str  # YYYY-MM-DD, UTC
    end: str    # YYYY-MM-DD, inclusive


class BacktestRequest(BaseModel):
    symbol: str
    timeframe: str
    start: str
    end: str
    strategy: str                       # "name@version"
    params: dict = Field(default_factory=dict)
    capital: float | None = None        # EUR; default from config.toml
    risk_pct: float | None = None       # default from config.toml
    sizing_mode: str = "realistic"
    spread: float | None = None         # price units; default per instrument
    slippage: float | None = None
    commission_per_lot: float | None = None
    financing_pct: float = 6.0


async def _eur_rates(store: CandleStore, instrument: Instrument, start: int, end: int) -> RateSeries:
    """Daily quote→EUR rates for the period, downloaded if needed; falls back to a fixed rate."""
    quote = instrument.quote_currency
    if quote == "EUR":
        return RateSeries.constant(1.0, "EUR")
    fallback = FALLBACK_EUR_RATES[quote]
    symbol = EUR_CONVERSION[quote]
    begin = start - 10 * 86400
    try:
        await store.sync(symbol, "d1", begin, end)
    except Exception:  # conversion data is a refinement; never block the backtest on it
        log.exception("Could not download %s for currency conversion", symbol)
    rows = store.read(symbol, "D1", begin, end)
    return RateSeries([(c.ts, c.close) for c in rows], fallback, quote)


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
                {
                    "symbol": i.symbol, "name": i.name, "category": i.category, "digits": i.digits,
                    "quote_currency": i.quote_currency, "contract_size": i.contract_size,
                    "min_lot": i.min_lot, "lot_step": i.lot_step, "max_leverage": i.max_leverage,
                    "costs": {"spread": i.spread, "slippage": i.slippage,
                              "commission_per_lot": i.commission_per_lot, "financing_pct": 6.0},
                }
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

    @app.get("/api/strategies")
    def strategies():
        return [describe(cls) for cls in load_strategies().values()]

    @app.post("/api/backtest")
    async def backtest(req: BacktestRequest, request: Request):
        start_ts, end_ts = _validate(req.symbol, req.timeframe, req.start, req.end)
        store: CandleStore = request.app.state.store
        instrument = INSTRUMENTS[req.symbol]
        tf = TIMEFRAMES[req.timeframe]

        cls = load_strategies().get(req.strategy)
        if cls is None:
            raise HTTPException(400, f"Onbekende strategie '{req.strategy}'.")
        try:
            strategy = cls(**req.params)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

        if store.missing_count(req.symbol, tf.level, start_ts, end_ts):
            raise HTTPException(409, "Nog niet alle koersdata voor deze periode is opgehaald. Laad eerst de data.")
        bars = [Bar(*c) for c in store.read(req.symbol, req.timeframe, start_ts, end_ts)]
        if not bars:
            raise HTTPException(400, "Geen koersdata in deze periode.")

        account = settings.account
        config = BacktestConfig(
            capital=req.capital if req.capital is not None else account.starting_capital,
            costs=CostModel(
                spread=req.spread if req.spread is not None else instrument.spread,
                slippage=req.slippage if req.slippage is not None else instrument.slippage,
                commission_per_lot=(req.commission_per_lot if req.commission_per_lot is not None
                                    else instrument.commission_per_lot),
                financing_pct=req.financing_pct,
            ),
            sizing=SizingRules(
                risk_pct=req.risk_pct if req.risk_pct is not None else account.risk_per_trade_pct,
                mode=req.sizing_mode,
                leverage=account.leverage,
            ),
        )
        try:
            config.validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

        rates = await _eur_rates(store, instrument, start_ts, end_ts)
        result = await run_in_threadpool(run_backtest, bars, strategy, instrument, config, rates)
        result["settings"] = {
            "symbol": req.symbol, "timeframe": req.timeframe, "start": req.start, "end": req.end,
            "strategy": cls.key(), "strategy_label": cls.label, "version": cls.version,
            "params": strategy.p, "capital": config.capital, "risk_pct": config.sizing.risk_pct,
            "sizing_mode": config.sizing.mode, "leverage": min(config.sizing.leverage, instrument.max_leverage),
            "costs": {"spread": config.costs.spread, "slippage": config.costs.slippage,
                      "commission_per_lot": config.costs.commission_per_lot,
                      "financing_pct": config.costs.financing_pct},
            "currency": "EUR", "quote_currency": instrument.quote_currency,
        }
        return result

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="dashboard")
    return app
