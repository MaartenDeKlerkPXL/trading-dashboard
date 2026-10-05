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
from ..db import get_state, set_state
from ..execution.base import CostModel, SizingRules
from ..execution.events import EventLog
from ..execution.paper import PaperExecutor
from ..strategies import code_hash, load_strategies
from ..strategies.base import Bar
from .session import EQUITY_WINDOW, OFFLINE_AFTER, SessionConfig, advance, data_start, new_state

log = logging.getLogger(__name__)

STATUSES = ("running", "paused", "stopped", "blocked")
STATUS_LABELS = {"running": "actief", "paused": "gepauzeerd", "stopped": "gestopt", "blocked": "geblokkeerd"}
ACTIVE = ("running", "paused", "blocked")   # sessions that may still hold a position


class KillSwitchActive(Exception):
    pass


class PaperEngine:
    MODE = "paper"

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

    # ---------- kill switch ----------

    def kill_switch(self) -> dict:
        return get_state(self.conn, "kill_switch", {"active": False})

    def ensure_trading_allowed(self) -> None:
        if self.kill_switch().get("active"):
            raise KillSwitchActive("De kill switch is actief. Hef hem eerst op via de pagina Risico & alerts.")

    async def kill(self, close_positions: bool) -> dict:
        """Stop every strategy now: pause all sessions, cancel waiting orders, optionally close positions."""
        async with self.lock:   # never in the middle of a loop round
            now = int(self.now())
            set_state(self.conn, "kill_switch", {"active": True, "since": now, "close_positions": close_positions})
            paused = closed = cancelled = 0
            errors = []
            for row in self.conn.execute(
                    "SELECT * FROM paper_sessions WHERE status IN (?,?,?) AND mode=?", (*ACTIVE, self.MODE)).fetchall():
                if row["status"] == "running":
                    self.set_status(row["id"], "paused", "kill switch")   # first stop deciding, then clean up
                    paused += 1
                try:
                    result = await self.flatten(row, "kill switch", close=close_positions)
                except Exception as exc:  # a broker problem must not stop the kill switch for the others
                    log.exception("Kill switch: session %s could not be flattened", row["id"])
                    errors.append(f"sessie {row['id']}: {exc}")
                    continue
                closed += result["closed"]
                cancelled += result["cancelled"]
            log.warning("Kill switch: %s paused, %s closed, %s cancelled", paused, closed, cancelled)
            return {"paused": paused, "closed": closed, "cancelled": cancelled, "since": now, "errors": errors}

    def release(self) -> None:
        state = self.kill_switch()
        set_state(self.conn, "kill_switch", {**state, "active": False, "released_at": int(self.now())})

    async def flatten(self, row: sqlite3.Row, reason: str, close: bool = True) -> dict:
        """Cancel waiting orders and (if close) close the open position at the last known price."""
        cfg = self.config(row)
        state = json.loads(row["state"])
        account = state.get("account")
        if not account or (not account.get("pending") and not (close and account.get("position"))):
            return {"closed": 0, "cancelled": 0}
        instrument = INSTRUMENTS[cfg.symbol]
        events = EventLog()
        ex = PaperExecutor(instrument, CostModel(**cfg.costs), SizingRules(cfg.risk_pct, cfg.sizing_mode, cfg.leverage),
                           cfg.capital, events)
        ex.restore(account)
        now = int(self.now())
        # Market time of the last known price: closing happens at that price, right after it.
        ts = min(now, (state.get("last_price_ts") or now - 60) + 60)
        cancelled = ex.cancel_pending(ts, reason)
        closed = 0
        if close and ex.pos is not None:
            price = state.get("last_price") or ex.pos.entry_price
            rates = await eur_rates(self.store, instrument, ts - 10 * 86400, ts)
            ex.close_all(Bar(ts, price, price, price, price, 0.0), rates.to_eur(ts), reason.capitalize())
            for t in ex.trades:
                t["bars_held"] = round((t["exit_ts"] - t["entry_ts"]) / cfg.tf_seconds, 1)
            closed = 1
        state["account"] = ex.to_state()
        with self.conn:
            for e in events.events:
                data = {k: v for k, v in e.items() if k not in ("ts", "kind", "message")}
                self._event(cfg.id, e["ts"], e["kind"], e["message"], data, now)
            self.conn.executemany(
                "INSERT OR REPLACE INTO paper_trades (session_id, trade_id, exit_ts, data) VALUES (?,?,?,?)",
                [(cfg.id, t["id"], t["exit_ts"], json.dumps(t)) for t in ex.trades])
            if closed:
                self.conn.execute("INSERT OR REPLACE INTO paper_equity (session_id, ts, equity) VALUES (?,?,?)",
                                  (cfg.id, ts - ts % EQUITY_WINDOW, ex.last_equity))
            self.conn.execute("UPDATE paper_sessions SET state=?, updated_at=? WHERE id=?",
                              (json.dumps(state), now, cfg.id))
        return {"closed": closed, "cancelled": cancelled}

    def open_positions(self, exclude: int | None = None) -> int:
        """Open positions over all sessions that can still hold one (they count towards one limit)."""
        n = 0
        for row in self.conn.execute("SELECT id, state FROM paper_sessions WHERE status IN (?,?,?) AND mode=?",
                                     (*ACTIVE, self.MODE)):
            if row["id"] != exclude and ((json.loads(row["state"]).get("account") or {}).get("position")):
                n += 1
        return n

    # ---------- sessions ----------

    def create(self, cfg: dict, broker: dict | None = None) -> int:
        """cfg: symbol, timeframe, strategy, params (validated), capital, risk_pct, sizing_mode, leverage, costs."""
        self.ensure_trading_allowed()
        cls = load_strategies()[cfg["strategy"]]
        now = int(self.now())
        settings = {k: cfg[k] for k in ("capital", "risk_pct", "sizing_mode", "leverage", "costs")}
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO paper_sessions (created_at, updated_at, status, symbol, timeframe, strategy, version, "
                "code_hash, params, settings, state, mode, broker) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, now, "running", cfg["symbol"], cfg["timeframe"], cls.key(), cls.version, code_hash(cls),
                 json.dumps(cfg["params"]), json.dumps(settings), json.dumps(new_state(now)), self.MODE,
                 json.dumps(broker or {})),
            )
            session_id = int(cur.lastrowid)
            where = "Paper trading" if self.MODE == "paper" else (
                f"Live trading ({'ECHT GELD' if (broker or {}).get('is_live') else 'demo'}-account "
                f"{(broker or {}).get('login', '')})")
            self._event(session_id, now, "info",
                        f"{where} gestart: {cls.label} {cls.version} op {cfg['symbol']} {cfg['timeframe']}, "
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
            "mode": row["mode"],
            "broker": json.loads(row["broker"] or "{}"),
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
            "risk_state": account.get("risk") or {},
        }

    def list(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM paper_sessions WHERE mode=? ORDER BY id DESC", (self.MODE,)).fetchall()
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
        rows = self.conn.execute("SELECT * FROM paper_sessions WHERE status='running' AND mode=? ORDER BY id",
                                 (self.MODE,)).fetchall()
        if not rows or self.kill_switch().get("active"):
            self.status["last_error"] = None
            self.status["errors_in_a_row"] = 0
            return {"sessions": 0, "errors": [], "session_errors": [], "feed": {}}

        strategies = load_strategies()
        by_symbol = defaultdict(list)
        for row in rows:
            by_symbol[row["symbol"]].append(row)

        errors, session_errors, feed = [], [], {}
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
            feed[symbol] = None
            try:
                sync = await self.store.sync(symbol, "m1", first, now + 60, refresh_after=refresh)
                if sync.failed and not sync.downloaded:
                    feed[symbol] = sync.errors[0] if sync.errors else "koersen niet opgehaald"
            except Exception as exc:  # network trouble must not stop the other symbols
                log.exception("Price sync for %s failed", symbol)
                feed[symbol] = f"{type(exc).__name__}: {exc}"
            if feed[symbol]:
                errors.append(f"{symbol}: {feed[symbol]}")
            instrument = INSTRUMENTS[symbol]
            rates = await eur_rates(self.store, instrument, first, now, log)

            for row, start in zip(group, starts):
                try:
                    await self._advance_one(row, start, now, rates, strategies)
                except Exception as exc:
                    log.exception("Paper session %s failed", row["id"])
                    errors.append(f"sessie {row['id']}: {exc}")
                    session_errors.append(f"sessie {row['id']}: {exc}")
                    with self.conn:
                        self._event(row["id"], now, "error", f"Fout tijdens verwerken: {exc}", {})

        self.status["last_error"] = "; ".join(errors) if errors else None
        self.status["errors_in_a_row"] = self.status["errors_in_a_row"] + 1 if errors else 0
        return {"sessions": len(rows), "errors": errors, "session_errors": session_errors, "feed": feed}

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
        result = await asyncio.to_thread(advance, cfg, state, m1, now, rates, cls, instrument, offline_after,
                                         self.settings.risk, self.open_positions(exclude=cfg.id))

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
        """on_tick(result, error) is awaited after every round, also when the round failed."""
        await asyncio.sleep(2)
        while True:
            result, error = None, None
            try:
                result = await self.tick()
            except Exception as exc:
                error = exc  # already logged and shown in the status
            if on_tick:
                try:
                    await on_tick(result, error)
                except Exception:
                    log.exception("After-tick handler failed")
            await asyncio.sleep(self.settings.paper.poll_seconds)
