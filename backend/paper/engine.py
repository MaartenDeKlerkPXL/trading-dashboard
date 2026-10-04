"""The paper-trading loop: fetch fresh prices, advance every running session, store everything."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections import defaultdict

from ..backtest.metrics import compute_metrics
from ..backtest.service import eur_rates
from ..config import Settings
from ..data.instruments import INSTRUMENTS
from ..data.store import CandleStore
from ..strategies import code_hash, load_strategies
from .session import OFFLINE_AFTER, SessionConfig, advance, data_start, new_state

log = logging.getLogger(__name__)

STATUSES = ("running", "paused", "stopped", "blocked")
STATUS_LABELS = {"running": "actief", "paused": "gepauzeerd", "stopped": "gestopt", "blocked": "geblokkeerd"}


class PaperEngine:
    def __init__(self, conn: sqlite3.Connection, store: CandleStore, settings: Settings, now=time.time):
        self.conn = conn
        self.store = store
        self.settings = settings
        self.now = now
        self.lock = asyncio.Lock()
        self.status = {
            "enabled": settings.execution.mode == "paper",
            "interval": settings.paper.poll_seconds,
            "last_tick_at": None,
            "last_duration": None,
            "last_error": None,
            "errors_in_a_row": 0,
        }

    # ---------- sessions ----------

    def create(self, cfg: dict) -> int:
        """cfg: symbol, timeframe, strategy, params (validated), capital, risk_pct, sizing_mode, leverage, costs."""
        cls = load_strategies()[cfg["strategy"]]
        now = int(self.now())
        settings = {k: cfg[k] for k in ("capital", "risk_pct", "sizing_mode", "leverage", "costs")}
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO paper_sessions (created_at, updated_at, status, symbol, timeframe, strategy, version, "
                "code_hash, params, settings, state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now, now, "running", cfg["symbol"], cfg["timeframe"], cls.key(), cls.version, code_hash(cls),
                 json.dumps(cfg["params"]), json.dumps(settings), json.dumps(new_state(now))),
            )
            session_id = int(cur.lastrowid)
            self._event(session_id, now, "info",
                        f"Paper trading gestart: {cls.label} {cls.version} op {cfg['symbol']} {cfg['timeframe']}, "
                        f"startkapitaal €{cfg['capital']:.2f}. Deze versie is vastgezet.", {})
        return session_id

    def set_status(self, session_id: int, status: str, reason: str = "") -> bool:
        assert status in STATUSES
        with self.conn:
            cur = self.conn.execute(
                "UPDATE paper_sessions SET status=?, status_reason=?, updated_at=? WHERE id=?",
                (status, reason, int(self.now()), session_id),
            )
            if cur.rowcount:
                self._event(session_id, int(self.now()), "info",
                            f"Status: {STATUS_LABELS[status]}" + (f" ({reason})" if reason else ""), {})
        return cur.rowcount > 0

    def row(self, session_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM paper_sessions WHERE id=?", (session_id,)).fetchone()

    def config(self, row: sqlite3.Row) -> SessionConfig:
        s = json.loads(row["settings"])
        return SessionConfig(
            id=row["id"], symbol=row["symbol"], timeframe=row["timeframe"], strategy=row["strategy"],
            params=json.loads(row["params"]), capital=s["capital"], risk_pct=s["risk_pct"],
            sizing_mode=s["sizing_mode"], leverage=s["leverage"], costs=s["costs"],
        )

    def summary(self, row: sqlite3.Row) -> dict:
        state = json.loads(row["state"])
        settings = json.loads(row["settings"])
        account = state.get("account") or {}
        equity = account.get("last_equity", settings["capital"])
        cls = load_strategies().get(row["strategy"])
        position = account.get("position")
        return {
            "id": row["id"],
            "status": row["status"],
            "status_label": STATUS_LABELS[row["status"]],
            "status_reason": row["status_reason"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "strategy": row["strategy"],
            "strategy_label": cls.label if cls else row["strategy"],
            "version": row["version"],
            "params": json.loads(row["params"]),
            "settings": settings,
            "created_at": row["created_at"],
            "started_at": state["started_at"],
            "last_tick_at": state.get("last_tick_at"),
            "last_price": state.get("last_price"),
            "last_price_ts": state.get("last_price_ts"),
            "equity": equity,
            "return_pct": (equity / settings["capital"] - 1) * 100,
            "trades": account.get("trade_count", 0),
            "pending_orders": len(account.get("pending", [])),
            "position": None if not position else {
                k: position[k] for k in ("side", "lots", "entry_price", "entry_ts", "stop_loss", "take_profit")
            },
        }

    def list(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM paper_sessions ORDER BY id DESC").fetchall()
        return [self.summary(r) for r in rows]

    def trades(self, session_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT data FROM paper_trades WHERE session_id=? ORDER BY trade_id", (session_id,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def equity(self, session_id: int) -> list[tuple[int, float]]:
        rows = self.conn.execute(
            "SELECT ts, equity FROM paper_equity WHERE session_id=? ORDER BY ts", (session_id,)).fetchall()
        return [(r["ts"], r["equity"]) for r in rows]

    def events(self, session_id: int, limit: int = 5000) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, ts, logged_at, kind, message, data FROM paper_events WHERE session_id=? "
            "ORDER BY id DESC LIMIT ?", (session_id, limit)).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "logged_at": r["logged_at"], "kind": r["kind"],
                 "message": r["message"], **json.loads(r["data"])} for r in rows]

    def metrics(self, session_id: int) -> dict:
        row = self.row(session_id)
        summary = self.summary(row)
        capital = summary["settings"]["capital"]
        equity = [(summary["started_at"], capital)] + self.equity(session_id)
        state = json.loads(row["state"])
        account = state.get("account") or {}
        periods = 365 if INSTRUMENTS[row["symbol"]].category == "crypto" else 252
        return compute_metrics(equity, self.trades(session_id), capital, periods,
                               state.get("m1_processed", 0), account.get("bars_in_market", 0))

    def candles(self, cfg: SessionConfig, started_at: int, until: int):
        cls = load_strategies().get(cfg.strategy)
        warmup = cls(**cfg.params).warmup() if cls else 1
        start = data_start(started_at, warmup, cfg.tf_seconds)
        return start, self.store.read(cfg.symbol, "M1", start, until + 60)

    # ---------- loop ----------

    async def tick(self) -> dict:
        async with self.lock:
            started = time.monotonic()
            try:
                result = await self._tick()
            except Exception as exc:  # the loop must survive anything; report it in the dashboard
                log.exception("Paper tick failed")
                self.status["last_error"] = f"{type(exc).__name__}: {exc}"
                self.status["errors_in_a_row"] += 1
                raise
            self.status["last_tick_at"] = int(self.now())
            self.status["last_duration"] = round(time.monotonic() - started, 2)
            return result

    async def _tick(self) -> dict:
        now = int(self.now())
        rows = self.conn.execute("SELECT * FROM paper_sessions WHERE status='running'").fetchall()
        if not rows:
            self.status["last_error"] = None
            self.status["errors_in_a_row"] = 0
            return {"sessions": 0}

        strategies = load_strategies()
        by_symbol = defaultdict(list)
        for row in rows:
            by_symbol[row["symbol"]].append(row)

        errors = []
        refresh = max(10, self.settings.paper.poll_seconds // 2)
        for symbol, group in by_symbol.items():
            starts = []
            for row in group:
                cfg = self.config(row)
                state = json.loads(row["state"])
                cls = strategies.get(cfg.strategy)
                warmup = cls(**cfg.params).warmup() if cls else 1
                starts.append(data_start(state["started_at"], warmup, cfg.tf_seconds))
            first = min(starts)
            sync = await self.store.sync(symbol, "m1", first, now + 60, refresh_after=refresh)
            if sync.failed and not sync.downloaded:
                errors.append(f"{symbol}: {sync.errors[0] if sync.errors else 'koersen niet opgehaald'}")
            instrument = INSTRUMENTS[symbol]
            rates = await eur_rates(self.store, instrument, first, now, log)

            for row, start in zip(group, starts):
                try:
                    await self._advance_one(row, start, now, rates, strategies)
                except Exception as exc:
                    log.exception("Paper session %s failed", row["id"])
                    errors.append(f"sessie {row['id']}: {exc}")
                    with self.conn:
                        self._event(row["id"], now, "error", f"Fout tijdens verwerken: {exc}", {})

        self.status["last_error"] = "; ".join(errors) if errors else None
        self.status["errors_in_a_row"] = self.status["errors_in_a_row"] + 1 if errors else 0
        return {"sessions": len(rows), "errors": errors}

    async def _advance_one(self, row, start: int, now: int, rates, strategies) -> None:
        cfg = self.config(row)
        cls = strategies.get(cfg.strategy)
        if cls is None:
            self.set_status(row["id"], "blocked", "het strategiebestand bestaat niet meer")
            return
        if code_hash(cls) != row["code_hash"]:
            # A running version is never changed silently: changes must become a new version.
            self.set_status(row["id"], "blocked",
                            f"het bestand van {cfg.strategy} is gewijzigd; maak er een nieuwe versie van")
            return
        m1 = self.store.read(cfg.symbol, "M1", start, now + 60)
        state = json.loads(row["state"])
        instrument = INSTRUMENTS[cfg.symbol]
        offline_after = max(OFFLINE_AFTER, 3 * self.settings.paper.poll_seconds)
        result = await asyncio.to_thread(advance, cfg, state, m1, now, rates, cls, instrument, offline_after)

        with self.conn:
            for e in result.events:
                data = {k: v for k, v in e.items() if k not in ("ts", "kind", "message")}
                self._event(cfg.id, e["ts"], e["kind"], e["message"], data, now)
            self.conn.executemany(
                "INSERT OR REPLACE INTO paper_trades (session_id, trade_id, exit_ts, data) VALUES (?,?,?,?)",
                [(cfg.id, t["id"], t["exit_ts"], json.dumps(t)) for t in result.trades],
            )
            self.conn.executemany(
                "INSERT OR REPLACE INTO paper_equity (session_id, ts, equity) VALUES (?,?,?)",
                [(cfg.id, ts, eq) for ts, eq in result.equity],
            )
            self.conn.execute("UPDATE paper_sessions SET state=?, updated_at=? WHERE id=?",
                              (json.dumps(result.state), now, cfg.id))

    def _event(self, session_id: int, ts: int, kind: str, message: str, data: dict, logged_at: int | None = None):
        self.conn.execute(
            "INSERT INTO paper_events (session_id, ts, logged_at, kind, message, data) VALUES (?,?,?,?,?,?)",
            (session_id, ts, logged_at or int(self.now()), kind, message, json.dumps(data)),
        )

    def any_running(self) -> bool:
        return self.conn.execute("SELECT 1 FROM paper_sessions WHERE status='running' LIMIT 1").fetchone() is not None

    async def run_forever(self, on_tick=None) -> None:
        await asyncio.sleep(2)
        while True:
            try:
                await self.tick()
            except Exception:
                pass  # already logged and shown in the status
            if on_tick:
                on_tick()
            await asyncio.sleep(self.settings.paper.poll_seconds)
