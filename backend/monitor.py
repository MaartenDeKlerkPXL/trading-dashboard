"""Watches the loop after every round: loop health, price/broker connection, heartbeat, reconciliation.

What a crashed program cannot do is report its own crash. So:
- every round writes a heartbeat; at the next start, a stale heartbeat without a clean shutdown means
  the dashboard stopped unexpectedly, and that is reported (and e-mailed) right away;
- optionally, every round pings an external service (HEARTBEAT_URL in .env, e.g. healthchecks.io)
  that e-mails you when the pings stop, even if the Mac itself is off.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .alerts import Alerter
from .config import Settings
from .db import get_state, set_state
from .reconcile import reconcile_paper

log = logging.getLogger(__name__)


class Monitor:
    def __init__(self, paper, alerter: Alerter, settings: Settings, heartbeat_url: str = "", now=time.time,
                 live=None):
        self.paper = paper
        self.live = live
        self.conn = paper.conn
        self.alerter = alerter
        self.settings = settings
        self.heartbeat_url = heartbeat_url.strip()
        self.now = now
        self.loop_failures: dict[str, int] = {}
        self.feed_down_since: dict[str, int] = {}
        self.last_reconcile = 0
        self.heartbeat_error: str | None = None
        self.tz = ZoneInfo(settings.app.timezone)

    def _time(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%d-%m-%Y %H:%M")

    # ---------- start and stop ----------

    async def on_startup(self) -> None:
        now = int(self.now())
        beat = get_state(self.conn, "heartbeat")
        clean = get_state(self.conn, "clean_shutdown", True)
        if beat and not clean and self.paper.any_running():
            await self.alerter.raise_(
                "unclean_stop", "urgent", "Dashboard was onverwacht gestopt",
                f"Het dashboard is onverwacht gestopt (laatste teken van leven: {self._time(beat)}) en is om "
                f"{self._time(now)} weer gestart. Candles uit die tijd zijn niet verhandeld; stop-loss en "
                "take-profit worden nagelopen. Mogelijke oorzaken: de Mac sliep of viel uit, of het "
                "Terminal-venster is gesloten.",
                email=True)
        set_state(self.conn, "clean_shutdown", False)

    def on_shutdown(self) -> None:
        set_state(self.conn, "clean_shutdown", True)

    # ---------- after every loop round ----------

    async def after_tick(self, result: dict | None, error: Exception | None) -> None:
        """After every paper round (the paper loop always runs): also heartbeat and reconciliation."""
        now = int(self.now())
        set_state(self.conn, "heartbeat", now)
        await self._loop_health("paper", result, error)
        await self._feed("paper", result or {}, now)
        await self._ping()
        if now - self.last_reconcile >= self.settings.alerts.reconcile_minutes * 60:
            await self.reconcile()

    async def after_live_tick(self, result: dict | None, error: Exception | None) -> None:
        now = int(self.now())
        await self._loop_health("live", result, error)
        await self._feed("live", result or {}, now)

    async def _loop_health(self, source: str, result: dict | None, error: Exception | None) -> None:
        key = "loop" if source == "paper" else "loop:live"
        name = "Paper trading-loop" if source == "paper" else "Live trading-loop"
        failed = error is not None or bool(result and result.get("session_errors"))
        if not failed:
            self.loop_failures[source] = 0
            self.alerter.resolve(key)
            return
        self.loop_failures[source] = self.loop_failures.get(source, 0) + 1
        n = self.loop_failures[source]
        if n >= self.settings.alerts.loop_errors:
            detail = f"{type(error).__name__}: {error}" if error else "; ".join(result["session_errors"])
            await self.alerter.raise_(
                key, "urgent", f"{name} werkt niet",
                f"De loop is {n} rondes achter elkaar mislukt, dus er wordt niet gehandeld. "
                f"Laatste fout: {detail[:500]}\nKijk in het dashboard of in data/app.log.",
                email=True)

    async def _feed(self, source: str, result: dict, now: int) -> None:
        feed = result.get("feed") or {}
        if not result.get("sessions"):
            # Nothing running: the connection does not matter right now.
            for key in [k for k in self.feed_down_since if k.startswith(f"{source}:")]:
                self.alerter.resolve(f"feed:{key.split(':', 1)[1]}")
                del self.feed_down_since[key]
            return
        limit = self.settings.alerts.feed_down_minutes * 60
        for symbol, problem in feed.items():
            key = f"{source}:{symbol}"
            if problem is None:
                if self.feed_down_since.pop(key, None) is not None:
                    self.alerter.resolve(f"feed:{symbol}")
                continue
            since = self.feed_down_since.setdefault(key, now)
            minutes = (now - since) // 60
            if now - since >= limit:
                what = "met de broker (cTrader)" if symbol == "cTrader" else f"voor {symbol}"
                await self.alerter.raise_(
                    f"feed:{symbol}", "urgent", f"Geen verbinding {what}",
                    f"Al {minutes} minuten lukt het niet om koersen {what} op te halen "
                    f"(sinds {self._time(since)}). Zolang dat zo is, wordt er niet gehandeld en worden stop-loss "
                    f"en take-profit niet gecontroleerd. Fout: {problem}",
                    email=True)

    async def _ping(self) -> None:
        if not self.heartbeat_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                (await client.get(self.heartbeat_url)).raise_for_status()
            self.heartbeat_error = None
        except Exception as exc:   # the URL may contain a secret id: never log it
            self.heartbeat_error = f"Heartbeat-ping mislukt ({type(exc).__name__})."

    # ---------- reconciliation ----------

    async def reconcile(self) -> dict:
        self.last_reconcile = int(self.now())
        report = reconcile_paper(self.paper, self.settings.risk)
        if self.live is not None:
            broker = await self.live.reconcile_broker()
            report["sessions"] = broker["sessions"] + report["sessions"]
            report["differences"] += broker["differences"]
            report["broker_error"] = broker["error"]
            report["unknown_positions"] = broker["unknown_positions"]
            report["live_open_positions"] = self.live.open_positions()
        set_state(self.conn, "reconciliation", report)
        if report["differences"]:
            names = ", ".join(f"sessie {s['id']}" for s in report["sessions"] if s["differences"])
            await self.alerter.raise_(
                "reconcile", "warning", "Administratie wijkt af",
                f"De reconciliatie vond {report['differences']} afwijking(en) bij {names}. "
                "Bekijk de details op de pagina Risico & alerts.",
                email=self.settings.alerts.email_on_reconciliation)
        else:
            self.alerter.resolve("reconcile")
        return report

    def status(self) -> dict:
        beat = get_state(self.conn, "heartbeat")
        return {
            "heartbeat": beat,
            "heartbeat_url": bool(self.heartbeat_url),
            "heartbeat_error": self.heartbeat_error,
            "loop_failures": self.loop_failures,
            "feed_down": {k.split(":", 1)[1]: t for k, t in self.feed_down_since.items()},
        }
