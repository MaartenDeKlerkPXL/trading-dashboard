"""HTTP endpoints for risk, the kill switch, alerts and reconciliation."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .config import Settings
from .db import get_state
from .paper.engine import ACTIVE
from .risk import day_key


class KillRequest(BaseModel):
    close_positions: bool = True


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api")
    risk = settings.risk

    @router.get("/risk/brief")
    async def brief(request: Request):
        """Small status for the bar at the top of every page."""
        return {"kill_switch": request.app.state.paper.kill_switch(),
                "open_alerts": request.app.state.alerter.open_counts()}

    @router.get("/risk")
    async def overview(request: Request):
        paper, alerter, monitor = request.app.state.paper, request.app.state.alerter, request.app.state.monitor
        now = int(time.time())
        today = day_key(now, risk.timezone)
        sessions = []
        for s in paper.list():
            if s["status"] not in ACTIVE:
                continue
            rs = s["risk_state"]
            day_start = rs.get("day_start_equity") if rs.get("day") == today else s["equity"]
            sessions.append({
                "id": s["id"], "label": f"{s['strategy_label']} {s['version']}", "symbol": s["symbol"],
                "timeframe": s["timeframe"], "status": s["status"], "status_label": s["status_label"],
                "status_reason": s["status_reason"], "equity": s["equity"],
                "day_start_equity": day_start,
                "day_pnl_pct": (s["equity"] / day_start - 1) * 100 if day_start else 0.0,
                "blocked_today": rs.get("blocked_day") is not None and rs.get("blocked_day") == today,
                "position": s["position"], "pending_orders": s["pending_orders"],
                "risk_pct": s["settings"]["risk_pct"], "lots_limit": risk.lots_limit(s["symbol"]),
            })
        sender = alerter.sender
        return {
            "mode": settings.execution.mode,
            "limits": risk.to_dict(),
            "kill_switch": paper.kill_switch(),
            "sessions": sessions,
            "open_positions": paper.open_positions(),
            "email": {"configured": alerter.email_configured, "to": sender.to if sender else "",
                      "repeat_minutes": settings.alerts.repeat_minutes,
                      "feed_down_minutes": settings.alerts.feed_down_minutes,
                      "on_reconciliation": settings.alerts.email_on_reconciliation},
            "monitor": monitor.status(),
            "loop": paper.status,
            "alerts": alerter.list(60),
            "open_alerts": alerter.open_counts(),
            "reconciliation": get_state(paper.conn, "reconciliation"),
            "reconcile_minutes": settings.alerts.reconcile_minutes,
            "server_time": now,
        }

    @router.post("/risk/kill")
    async def kill(body: KillRequest, request: Request):
        paper, alerter = request.app.state.paper, request.app.state.alerter
        result = await paper.kill(body.close_positions)
        await alerter.raise_(
            "kill_switch", "warning", "Kill switch gebruikt",
            f"Alle strategieën zijn gepauzeerd ({result['paused']}), {result['cancelled']} wachtende order(s) "
            f"geannuleerd en {result['closed']} positie(s) gesloten. Nieuwe sessies starten kan pas weer "
            "als de kill switch is opgeheven.")
        request.app.state.keep_awake.update(paper.any_running())
        return result

    @router.post("/risk/release")
    async def release(request: Request):
        paper = request.app.state.paper
        if not paper.kill_switch().get("active"):
            raise HTTPException(400, "De kill switch is niet actief.")
        paper.release()
        request.app.state.alerter.resolve("kill_switch")
        return paper.kill_switch()

    @router.post("/alerts/test")
    async def test_email(request: Request):
        error = await request.app.state.alerter.send_test()
        if error:
            raise HTTPException(400, error)
        return {"ok": True, "to": request.app.state.alerter.sender.to}

    @router.post("/alerts/{alert_id}/resolve")
    async def resolve(alert_id: int, request: Request):
        if not request.app.state.alerter.resolve_id(alert_id):
            raise HTTPException(404, "Deze melding bestaat niet of is al afgehandeld.")
        return {"ok": True}

    @router.post("/reconcile")
    async def reconcile(request: Request):
        return await request.app.state.monitor.reconcile()

    return router
