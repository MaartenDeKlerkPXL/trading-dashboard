"""Alerts: every problem is shown in the dashboard; urgent ones are also e-mailed.

One row per problem (key). While a problem lasts, the row is updated instead of
duplicated, and at most one e-mail per problem is sent per `repeat` seconds,
also when a problem disappears and comes back.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import sqlite3
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

LEVELS = ("urgent", "warning", "info")


class EmailSender:
    """Gmail via an app password (SMTP with STARTTLS). The password is never logged."""

    def __init__(self, user: str, password: str, to: str, host: str = "smtp.gmail.com", port: int = 587):
        self.user = user.strip()
        self.password = password.replace(" ", "")   # Gmail shows app passwords in groups of four
        self.to = to.strip() or self.user
        self.host = host
        self.port = port

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "EmailSender":
        return cls(env.get("SMTP_USER", ""), env.get("SMTP_APP_PASSWORD", ""), env.get("ALERT_EMAIL_TO", ""))

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password and self.to)

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.user
        msg["To"] = self.to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.user, self.password)
            smtp.send_message(msg)


def _email_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "Inloggen bij Gmail mislukt: controleer SMTP_USER en SMTP_APP_PASSWORD in .env."
    if isinstance(exc, (OSError, smtplib.SMTPException)):
        return f"Versturen mislukt ({type(exc).__name__}). Is er internet?"
    return f"Versturen mislukt ({type(exc).__name__})."


class Alerter:
    def __init__(self, conn: sqlite3.Connection, sender: EmailSender | None, repeat_seconds: int = 3600,
                 timezone: str = "Europe/Amsterdam", now=time.time):
        self.conn = conn
        self.sender = sender
        self.repeat = repeat_seconds
        self.tz = ZoneInfo(timezone)
        self.now = now

    @property
    def email_configured(self) -> bool:
        return bool(self.sender and self.sender.configured)

    def _time(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%d-%m-%Y %H:%M")

    async def raise_(self, key: str, level: str, title: str, message: str, email: bool = False) -> dict:
        """Record (or update) a problem. Returns the alert row as a dict."""
        assert level in LEVELS
        now = int(self.now())
        row = self.conn.execute("SELECT * FROM alerts WHERE key=? AND resolved_at IS NULL", (key,)).fetchone()
        with self.conn:
            if row is None:
                alert_id = self.conn.execute(
                    "INSERT INTO alerts (key, level, title, message, first_at, last_at) VALUES (?,?,?,?,?,?)",
                    (key, level, title, message, now, now)).lastrowid
                log.warning("Alert %s: %s", key, title)
            else:
                alert_id = row["id"]
                self.conn.execute("UPDATE alerts SET level=?, title=?, message=?, last_at=?, count=count+1 WHERE id=?",
                                  (level, title, message, now, alert_id))
        if email:
            last = self.conn.execute("SELECT MAX(emailed_at) FROM alerts WHERE key=?", (key,)).fetchone()[0]
            if last is None or now - last >= self.repeat:
                await self._email(alert_id, title, message)
        return self.get(alert_id)

    async def _email(self, alert_id: int, title: str, message: str) -> None:
        if not self.email_configured:
            error = "Geen e-mail ingesteld (zie .env)."
        else:
            body = (f"{message}\n\nTijd: {self._time(int(self.now()))} (Nederlandse tijd)\n\n"
                    "Dit is een automatische melding van je Trading Dashboard. "
                    "Je krijgt over dit probleem hooguit één e-mail per uur.\n")
            try:
                await asyncio.to_thread(self.sender.send, f"[Trading Dashboard] {title}", body)
                error = None
            except Exception as exc:   # never let e-mail problems break the loop
                error = _email_error(exc)
                log.error("Alert e-mail failed: %s", error)
        with self.conn:
            if error is None:
                self.conn.execute("UPDATE alerts SET emailed_at=?, email_error=NULL WHERE id=?",
                                  (int(self.now()), alert_id))
            else:
                self.conn.execute("UPDATE alerts SET email_error=? WHERE id=?", (error, alert_id))

    def resolve(self, key: str) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE alerts SET resolved_at=? WHERE key=? AND resolved_at IS NULL",
                                    (int(self.now()), key))
        return cur.rowcount > 0

    def resolve_id(self, alert_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE alerts SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
                                    (int(self.now()), alert_id))
        return cur.rowcount > 0

    def get(self, alert_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone())

    def list(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM alerts ORDER BY resolved_at IS NOT NULL, last_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def open_counts(self) -> dict:
        rows = self.conn.execute(
            "SELECT level, COUNT(*) AS n FROM alerts WHERE resolved_at IS NULL GROUP BY level").fetchall()
        return {r["level"]: r["n"] for r in rows}

    async def send_test(self) -> str | None:
        """Send a test e-mail now. Returns an error text, or None when it was sent."""
        if not self.email_configured:
            return "Geen e-mail ingesteld: vul SMTP_USER, SMTP_APP_PASSWORD en ALERT_EMAIL_TO in .env in."
        try:
            await asyncio.to_thread(
                self.sender.send, "[Trading Dashboard] Testmail",
                "Dit is een testmail van je Trading Dashboard. De e-mailmeldingen werken.\n\n"
                f"Verstuurd: {self._time(int(self.now()))} (Nederlandse tijd)\n")
        except Exception as exc:
            return _email_error(exc)
        return None
