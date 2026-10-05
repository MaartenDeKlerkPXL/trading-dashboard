"""HTTP endpoints for paper trading."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..backtest.service import eur_rates, make_strategy, strategy_class
from ..config import Settings
from ..data.instruments import INSTRUMENTS, TIMEFRAMES
from ..execution.base import CostModel, SizingRules
from .engine import STATUS_LABELS, KillSwitchActive, PaperEngine
from .review import CRITERIA, backtest_same_period, comparison, evaluate, validate_criteria

DECISIONS = {"open": "Nog geen besluit", "doorgaan": "Doorgaan", "aanpassen": "Aanpassen (nieuwe versie)",
             "stoppen": "Stoppen"}


class PaperStart(BaseModel):
    symbol: str
    timeframe: str
    strategy: str
    params: dict = Field(default_factory=dict)
    capital: float | None = None
    risk_pct: float | None = None
    sizing_mode: str = "fractional"
    spread: float | None = None
    slippage: float | None = None
    commission_per_lot: float | None = None
    financing_pct: float = 6.0


class EvaluationUpdate(BaseModel):
    criteria: list[dict] | None = None
    notes: str | None = None
    decision: str | None = None
    lock: bool = False


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/paper")

    def engine(request: Request) -> PaperEngine:
        return request.app.state.paper

    def session_row(request: Request, session_id: int):
        row = engine(request).row(session_id)
        if row is None:
            raise HTTPException(404, f"Paper-sessie {session_id} bestaat niet.")
        return row

    @router.get("/status")
    async def status(request: Request):
        eng = engine(request)
        keep = request.app.state.keep_awake
        return {
            **eng.status,
            "mode": settings.execution.mode,
            "sessions_running": sum(1 for s in eng.list() if s["status"] == "running"),
            "keep_awake": {"enabled": keep.enabled, "active": keep.active, "requested": settings.paper.keep_awake},
            "server_time": int(time.time()),
        }

    @router.get("/sessions")
    async def sessions(request: Request):
        return engine(request).list()

    @router.post("/sessions")
    async def start(body: PaperStart, request: Request):
        if body.symbol not in INSTRUMENTS:
            raise HTTPException(400, f"Onbekend instrument '{body.symbol}'.")
        if body.timeframe not in TIMEFRAMES:
            raise HTTPException(400, f"Onbekende timeframe '{body.timeframe}'.")
        cls = strategy_class(body.strategy)
        strategy = make_strategy(cls, body.params)
        inst = INSTRUMENTS[body.symbol]
        account = settings.account
        capital = body.capital if body.capital is not None else account.starting_capital
        costs = CostModel(
            spread=body.spread if body.spread is not None else inst.spread,
            slippage=body.slippage if body.slippage is not None else inst.slippage,
            commission_per_lot=body.commission_per_lot if body.commission_per_lot is not None else inst.commission_per_lot,
            financing_pct=body.financing_pct,
        )
        sizing = SizingRules(body.risk_pct if body.risk_pct is not None else account.risk_per_trade_pct,
                             body.sizing_mode, account.leverage)
        try:
            costs.validate()
            sizing.validate()
            if not 10 <= capital <= 100_000_000:
                raise ValueError("Startkapitaal moet tussen €10 en €100 miljoen liggen.")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if body.risk_pct is not None and body.risk_pct > settings.risk.max_risk_per_trade_pct:
            raise HTTPException(400, f"Risico per trade mag niet hoger zijn dan de harde limiet van "
                                     f"{settings.risk.max_risk_per_trade_pct:g}% (zie config.toml).")
        try:
            engine(request).ensure_trading_allowed()
        except KillSwitchActive as exc:
            raise HTTPException(409, str(exc)) from None
        session_id = engine(request).create({
            "symbol": body.symbol, "timeframe": body.timeframe, "strategy": cls.key(), "params": strategy.p,
            "capital": capital, "risk_pct": sizing.risk_pct, "sizing_mode": sizing.mode,
            "leverage": min(sizing.leverage, inst.max_leverage),
            "costs": {"spread": costs.spread, "slippage": costs.slippage,
                      "commission_per_lot": costs.commission_per_lot, "financing_pct": costs.financing_pct},
        })
        request.app.state.keep_awake.update(True)
        return engine(request).summary(engine(request).row(session_id))

    @router.post("/sessions/{session_id}/{action}")
    async def change(session_id: int, action: str, request: Request):
        eng = engine(request)
        async with eng.lock:   # never change a session in the middle of a loop round
            row = session_row(request, session_id)
            current = row["status"]
            if action == "pause" and current == "running":
                eng.set_status(session_id, "paused")
            elif action == "resume" and current == "paused":
                try:
                    eng.ensure_trading_allowed()
                except KillSwitchActive as exc:
                    raise HTTPException(409, str(exc)) from None
                eng.set_status(session_id, "running")
            elif action == "stop" and current in ("running", "paused", "blocked"):
                # A stopped session is never processed again: close its position and cancel its orders.
                await eng.flatten(row, "sessie gestopt")
                eng.set_status(session_id, "stopped")
            else:
                raise HTTPException(400, f"Dat kan niet: de sessie is {STATUS_LABELS[current]}.")
        request.app.state.keep_awake.update(eng.any_running())
        return eng.summary(eng.row(session_id))

    @router.post("/tick")
    async def tick(request: Request):
        try:
            return await engine(request).tick()
        except Exception as exc:
            raise HTTPException(500, f"Bijwerken mislukt: {exc}") from None

    @router.get("/sessions/{session_id}")
    async def detail(session_id: int, request: Request):
        row = session_row(request, session_id)
        eng = engine(request)
        summary = eng.summary(row)
        capital = summary["settings"]["capital"]
        equity = dict([(summary["started_at"] - summary["started_at"] % 900, capital)] + eng.equity(session_id))
        equity = sorted(equity.items())
        return {
            **summary,
            "metrics": eng.metrics(session_id),
            "trades": eng.trades(session_id),
            "equity_curve": [{"time": ts, "value": round(v, 2)} for ts, v in equity],
            "events": eng.events(session_id),
        }

    async def _comparison(request: Request, session_id: int) -> dict:
        row = session_row(request, session_id)
        eng = engine(request)
        cfg = eng.config(row)
        cls = strategy_class(cfg.strategy)
        state = json.loads(row["state"])
        now = state.get("last_tick_at") or int(time.time())
        start, m1 = eng.candles(cfg, state["started_at"], now)
        rates = await eur_rates(eng.store, INSTRUMENTS[cfg.symbol], start, now)
        backtest = await run_in_threadpool(backtest_same_period, cfg, state["started_at"], m1, now, rates, cls,
                                           INSTRUMENTS[cfg.symbol], settings.risk)
        return comparison(cfg, state["started_at"], eng.equity(session_id), eng.trades(session_id),
                          eng.metrics(session_id), backtest)

    @router.get("/sessions/{session_id}/compare")
    async def compare(session_id: int, request: Request):
        return await _comparison(request, session_id)

    def _evaluation_row(request: Request, session_id: int):
        conn = engine(request).conn
        row = conn.execute("SELECT * FROM evaluations WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return {"criteria": [], "notes": "", "decision": "open", "locked_at": None, "updated_at": None}
        return {"criteria": json.loads(row["criteria"]), "notes": row["notes"], "decision": row["decision"],
                "locked_at": row["locked_at"], "updated_at": row["updated_at"]}

    @router.get("/sessions/{session_id}/evaluation")
    async def evaluation(session_id: int, request: Request):
        row = session_row(request, session_id)
        ev = _evaluation_row(request, session_id)
        eng = engine(request)
        state = json.loads(row["state"])
        days = ((state.get("last_tick_at") or int(time.time())) - state["started_at"]) / 86400
        backtest = None
        if any(c["type"] == "return_vs_backtest" for c in ev["criteria"]):
            backtest = (await _comparison(request, session_id))["backtest_metrics"]
        return {
            **ev,
            "days_running": days,
            "results": evaluate(ev["criteria"], eng.metrics(session_id), backtest, days),
            "catalog": [{"type": k, "label": v[0].replace(" {v}", " …"), "unit": v[1]} for k, v in CRITERIA.items()],
            "decisions": DECISIONS,
        }

    @router.put("/sessions/{session_id}/evaluation")
    async def save_evaluation(session_id: int, body: EvaluationUpdate, request: Request):
        session_row(request, session_id)
        ev = _evaluation_row(request, session_id)
        if body.criteria is not None:
            if ev["locked_at"]:
                raise HTTPException(400, "De criteria zijn al vastgelegd en kunnen niet meer veranderen.")
            try:
                ev["criteria"] = validate_criteria(body.criteria)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
        if body.notes is not None:
            ev["notes"] = body.notes[:20000]
        if body.decision is not None:
            if body.decision not in DECISIONS:
                raise HTTPException(400, "Onbekend besluit.")
            ev["decision"] = body.decision
        now = int(time.time())
        if body.lock and not ev["locked_at"]:
            if not ev["criteria"]:
                raise HTTPException(400, "Leg eerst minstens één criterium vast.")
            ev["locked_at"] = now
        conn = engine(request).conn
        with conn:
            conn.execute(
                "INSERT INTO evaluations (session_id, criteria, notes, decision, locked_at, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET criteria=excluded.criteria, "
                "notes=excluded.notes, decision=excluded.decision, locked_at=excluded.locked_at, "
                "updated_at=excluded.updated_at",
                (session_id, json.dumps(ev["criteria"]), ev["notes"], ev["decision"], ev["locked_at"], now),
            )
        return await evaluation(session_id, request)

    return router
