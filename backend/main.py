"""FastAPI app: JSON API under /api, dashboard files under /."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import FRONTEND_DIR, Settings, load_settings
from .backtest.optimize import build_axes, optimize
from .backtest.service import (
    BacktestRequest, CommonSettings, SetupError, make_strategy, out_of_sample, prepare, run_segment,
    settings_dict, strategy_class, validate_period,
)
from .data.instruments import INSTRUMENTS, TIMEFRAMES
from .data.jobs import JobManager
from .data.providers import DataProvider, get_provider
from .data.store import CandleStore
from .db import connect
from .compare import build_comparison
from .importers.tradingview import ImportError_, build_run, parse_trades
from .alerts import Alerter, EmailSender
from .broker.oauth import TokenStore
from .env import load_env
from .live.api import build_router as live_router
from .live.engine import LiveEngine
from .live.link import BrokerLink
from .monitor import Monitor
from .paper.api import build_router as paper_router
from .paper.engine import PaperEngine
from .risk_api import build_router as risk_router
from .runs import RunStore
from .system import KeepAwake
from .tasks import TaskManager
from .strategies import code_hash, describe, load_strategies

log = logging.getLogger(__name__)


class SyncRequest(BaseModel):
    symbol: str
    timeframe: str
    start: str  # YYYY-MM-DD, UTC
    end: str    # YYYY-MM-DD, inclusive


class OptimizeRequest(CommonSettings):
    strategy: str
    fixed: dict = Field(default_factory=dict)       # values for the parameters that are not varied
    ranges: list[dict] = Field(default_factory=list)  # [{name, start, stop, step}], one or two
    target: str = "sharpe"
    min_trades: int = 10
    oos_pct: float = 30.0
    folds: int = 1


class TradingViewImport(BaseModel):
    filename: str = "tradingview.csv"
    content: str                      # the CSV file as text
    symbol: str
    timeframe: str
    timezone: str = "Europe/Amsterdam"
    capital: float | None = None      # only needed if it cannot be derived from the file


class RunUpdate(BaseModel):
    name: str | None = None
    note: str | None = None


class CompareAllRequest(CommonSettings):
    """Run several strategies on exactly the same data, period and costs."""

    entries: list[dict] = Field(default_factory=list)  # [{strategy, params}]; empty = every strategy, defaults


def _validate(symbol: str, timeframe: str, start: str, end: str) -> tuple[int, int]:
    try:
        return validate_period(symbol, timeframe, start, end)
    except SetupError as exc:
        raise HTTPException(exc.status, exc.message) from None


def create_app(settings: Settings | None = None, provider: DataProvider | None = None, store_kwargs: dict | None = None,
               start_loop: bool = True, env: dict | None = None, email_sender: EmailSender | None = None,
               broker_client_factory=None, oauth_transport=None) -> FastAPI:
    """env: secrets (default: the .env file). email_sender, broker_client_factory and oauth_transport replace
    Gmail and cTrader in tests."""
    settings = settings or load_settings()
    env = load_env() if env is None else env

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(settings.db_path)
        store = CandleStore(conn, provider or get_provider(settings.data.provider), **(store_kwargs or {}))
        app.state.store = store
        app.state.jobs = JobManager(store)
        app.state.runs = RunStore(conn)
        app.state.tasks = TaskManager()
        app.state.paper = paper = PaperEngine(conn, store, settings)
        app.state.keep_awake = KeepAwake(settings.paper.keep_awake)
        app.state.alerter = alerter = Alerter(
            conn, email_sender or EmailSender.from_env(env), settings.alerts.repeat_minutes * 60, settings.app.timezone)
        link = BrokerLink(conn, settings, env, TokenStore(settings.db_path.parent / "ctrader_token.json"),
                          broker_client_factory, oauth_transport)
        app.state.live = live = LiveEngine(conn, store, settings, link, alerter)
        app.state.monitor = monitor = Monitor(paper, alerter, settings, env.get("HEARTBEAT_URL", ""), live=live)
        tasks: list[asyncio.Task] = []
        if start_loop and settings.execution.mode in ("paper", "live"):
            await monitor.on_startup()

            async def after_tick(result, error):
                app.state.keep_awake.update(paper.any_running())
                await monitor.after_tick(result, error)

            def loop_ended(name: str):
                def callback(task: asyncio.Task) -> None:
                    if not task.cancelled() and task.exception() is not None:
                        log.error("%s loop crashed", name, exc_info=task.exception())
                        asyncio.get_running_loop().create_task(alerter.raise_(
                            f"loop_crash:{name}", "urgent", f"{name.capitalize()} trading-loop is gestopt",
                            f"De loop is gecrasht en draait niet meer: {task.exception()!r}. "
                            "Herstart het dashboard (Ctrl+C, daarna ./start.sh).", email=True))
                return callback

            tasks.append(asyncio.create_task(paper.run_forever(on_tick=after_tick)))
            tasks[-1].add_done_callback(loop_ended("paper"))
            if settings.execution.mode == "live":
                tasks.append(asyncio.create_task(live.run_forever(on_tick=monitor.after_live_tick)))
                tasks[-1].add_done_callback(loop_ended("live"))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if tasks:
                monitor.on_shutdown()
            await link.disconnect()
            app.state.keep_awake.stop()
            conn.close()

    app = FastAPI(title="Trading Dashboard", version=__version__, lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(SetupError)
    async def setup_error(request: Request, exc: SetupError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.middleware("http")
    async def no_cache_for_dashboard(request: Request, call_next):
        response = await call_next(request)
        # Always serve the latest dashboard files after an update.
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "mode": settings.execution.mode,
            "provider": settings.data.provider,
            "python": platform.python_version(),
            "server_time": int(datetime.now(timezone.utc).timestamp()),
        }

    @app.get("/api/config")
    async def config():
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
    async def get_job(job_id: str, request: Request):
        job = request.app.state.jobs.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Download-taak niet gevonden (is de server herstart?).")
        return job.to_dict()

    @app.get("/api/candles")
    async def candles(symbol: str, timeframe: str, start: str, end: str, request: Request):
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
    async def strategies():
        return [describe(cls) for cls in load_strategies().values()]

    def run_one(setup, cls, params: dict, oos_pct: float = 0.0) -> dict:
        strategy = make_strategy(cls, params)
        result = run_segment(setup.bars, cls, strategy.p, setup)
        if oos_pct:
            result["oos"] = out_of_sample(setup, cls, strategy.p, oos_pct)
        result["settings"] = settings_dict(setup, strategy)
        return result

    @app.post("/api/backtest")
    async def backtest(req: BacktestRequest, request: Request):
        if not 0 <= req.oos_pct <= 50:
            raise HTTPException(400, "Out-of-sample moet tussen 0 en 50% liggen.")
        cls = strategy_class(req.strategy)
        make_strategy(cls, req.params)  # validate parameters before touching data
        setup = await prepare(request.app.state.store, settings, req, log)
        result = await run_in_threadpool(run_one, setup, cls, req.params, req.oos_pct)
        result["settings"]["oos_pct"] = req.oos_pct
        result["run_id"] = request.app.state.runs.save(result, code_hash=code_hash(cls))
        return result

    @app.post("/api/compare/run")
    async def compare_run(req: CompareAllRequest, request: Request):
        strategies = load_strategies()
        entries = req.entries or [{"strategy": key, "params": {}} for key in strategies]
        if len(entries) > 8:
            raise HTTPException(400, "Vergelijk maximaal 8 strategieën tegelijk.")
        classes = [(strategy_class(e.get("strategy", "")), e.get("params") or {}) for e in entries]
        for cls, params in classes:
            make_strategy(cls, params)
        setup = await prepare(request.app.state.store, settings, req, log)
        group = uuid.uuid4().hex[:12]
        ids = []
        for cls, params in classes:
            result = await run_in_threadpool(run_one, setup, cls, params)
            ids.append(request.app.state.runs.save(result, code_hash=code_hash(cls), group_id=group))
        return {"group_id": group, "run_ids": ids}

    # ---------- Optimization ----------

    @app.post("/api/optimize")
    async def start_optimize(req: OptimizeRequest, request: Request):
        tasks: TaskManager = request.app.state.tasks
        if tasks.running("optimize"):
            raise HTTPException(409, "Er loopt al een optimalisatie. Wacht tot die klaar is of stop hem eerst.")
        cls = strategy_class(req.strategy)
        make_strategy(cls, req.fixed)
        # Validate the grid now, so mistakes show up immediately instead of as a failed task.
        try:
            build_axes(cls, req.ranges)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if not 1 <= req.min_trades <= 1000:
            raise HTTPException(400, "Minimum aantal trades moet tussen 1 en 1000 liggen.")
        setup = await prepare(request.app.state.store, settings, req, log)

        def work(task):
            result = optimize(
                setup.bars, cls, setup.instrument, setup.config, setup.rates, req.fixed, req.ranges,
                target=req.target, min_trades=req.min_trades, oos_pct=req.oos_pct, folds=req.folds,
                progress=task.progress,
            )
            result["settings"] = settings_dict(setup, make_strategy(cls, result["best"]["params"]))
            return result

        return tasks.start("optimize", work).to_dict(with_result=False)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        task = request.app.state.tasks.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "Taak niet gevonden (is de server herstart?).")
        return task.to_dict(with_result=task.status == "done")

    @app.delete("/api/tasks/{task_id}")
    async def cancel_task(task_id: str, request: Request):
        task = request.app.state.tasks.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "Taak niet gevonden.")
        task.cancel_requested = True
        return {"ok": True}

    # ---------- TradingView import and comparison ----------

    @app.post("/api/import/tradingview")
    async def import_tradingview(body: TradingViewImport, request: Request):
        if body.symbol not in INSTRUMENTS:
            raise HTTPException(400, f"Onbekend instrument '{body.symbol}'.")
        if body.timeframe not in TIMEFRAMES:
            raise HTTPException(400, f"Onbekende timeframe '{body.timeframe}'.")
        name = body.filename.rsplit("/", 1)[-1].rsplit(".", 1)[0][:60] or "import"
        try:
            parsed = parse_trades(body.content, body.timezone)
            result = build_run(parsed, body.symbol, body.timeframe, name, body.capital)
        except ImportError_ as exc:
            raise HTTPException(400, str(exc)) from None
        run_id = request.app.state.runs.save(result, source="tradingview", name=f"TradingView: {name}")
        return {"run_id": run_id, "trades": len(result["trades"]), "capital": result["settings"]["capital"],
                "capital_inferred": result["settings"]["capital_inferred"], "warnings": result["warnings"]}

    @app.get("/api/compare")
    async def compare(ids: str, request: Request):
        try:
            run_ids = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "Ongeldige lijst met runs.") from None
        if not 1 <= len(run_ids) <= 8:
            raise HTTPException(400, "Kies 1 tot 8 runs om te vergelijken.")
        hashes = _current_hashes()
        runs = []
        for run_id in dict.fromkeys(run_ids):
            run = request.app.state.runs.get(run_id)
            if run is None:
                raise HTTPException(404, f"Run {run_id} bestaat niet (meer).")
            runs.append(_flag_changed(run, hashes))
        return build_comparison(runs)

    # ---------- Saved runs ----------

    def _current_hashes() -> dict[str, str]:
        return {key: code_hash(cls) for key, cls in load_strategies().items()}

    def _flag_changed(run: dict, hashes: dict[str, str]) -> dict:
        current = hashes.get(run.get("strategy") or "")
        run["strategy_changed"] = bool(run["source"] == "engine" and run["code_hash"] and current
                                       and current != run["code_hash"])
        run["strategy_missing"] = run["source"] == "engine" and current is None
        return run

    @app.get("/api/runs")
    async def list_runs(request: Request):
        hashes = _current_hashes()
        return [_flag_changed(r, hashes) for r in request.app.state.runs.list()]

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: int, request: Request):
        run = request.app.state.runs.get(run_id)
        if run is None:
            raise HTTPException(404, f"Run {run_id} bestaat niet (meer).")
        run["run_id"] = run["id"]
        return _flag_changed(run, _current_hashes())

    @app.patch("/api/runs/{run_id}")
    async def update_run(run_id: int, body: RunUpdate, request: Request):
        if not request.app.state.runs.update(run_id, body.name, body.note):
            raise HTTPException(404, f"Run {run_id} bestaat niet (meer).")
        return {"ok": True}

    @app.delete("/api/runs/{run_id}")
    async def delete_run(run_id: int, request: Request):
        if not request.app.state.runs.delete(run_id):
            raise HTTPException(404, f"Run {run_id} bestaat niet (meer).")
        return {"ok": True}

    app.include_router(paper_router(settings))
    app.include_router(risk_router(settings))
    app.include_router(live_router(settings))

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="dashboard")
    return app
