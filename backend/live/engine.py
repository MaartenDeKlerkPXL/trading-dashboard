"""Live trading: the same loop as paper trading, but prices come from the broker and orders go to the broker.

Every round, for every running live strategy:
  1. fetch the broker's one-minute candles (BlackBull's own bid prices);
  2. ask the broker which positions are open (reconcile) and record trades the broker closed
     (stop-loss, take-profit) from its deal history;
  3. let the strategy decide on closed candles, exactly as in backtest and paper trading;
  4. send the resulting orders. Each order gets a unique id that is stored *before* it is sent,
     so a crash or restart can never send it twice. The stop-loss is attached to the order and
     then set to the exact level, so it is always held by the broker, not only by this program.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import asdict

from ..backtest.service import eur_rates
from ..broker.ctrader import BrokerError
from ..data.instruments import INSTRUMENTS
from ..data.providers import Candle
from ..execution.backtest import DAY, _Open
from ..execution.events import nl, nl_lots
from ..execution.live import LiveExecutor
from ..paper.engine import ACTIVE, PaperEngine
from ..paper.session import EQUITY_WINDOW, OFFLINE_AFTER, data_start
from ..reconcile import reconcile_session
from ..strategies import code_hash, load_strategies
from .link import BrokerLink, describe

log = logging.getLogger(__name__)

SOURCE = "ctrader"
CANDLE_CHUNK = 86400          # one day of minute candles per request
UNCERTAIN = ("sending", "unknown")


def label(session_id: int) -> str:
    return f"td-{session_id}"


def relative(distance: float, digits: int) -> int:
    """A price distance in the broker's 1/100000 units, on whole ticks of the symbol, at least one tick."""
    tick = 10 ** max(0, 5 - digits)
    return max(1, round(distance * 10 ** digits)) * tick


def client_order_id(session_id: int, client_id: str) -> str:
    """Short, unique and always the same for the same decision of the same session."""
    return "td" + hashlib.sha1(f"{session_id}|{client_id}".encode()).hexdigest()[:24]


class LiveEngine(PaperEngine):
    MODE = "live"

    def __init__(self, conn, store, settings, link: BrokerLink, alerter=None, now=time.time):
        super().__init__(conn, store, settings, now)
        self.link = link
        self.alerter = alerter
        self.status["enabled"] = settings.execution.mode == "live"
        self.last_snapshot: dict | None = None

    # ---------- broker candles ----------

    def broker_candles(self, symbol: str, start: int, end: int) -> list[Candle]:
        rows = self.conn.execute(
            "SELECT ts, open, high, low, close, volume FROM candles WHERE source=? AND symbol=? AND level='m1' "
            "AND ts>=? AND ts<? ORDER BY ts", (SOURCE, symbol, start, end)).fetchall()
        return [Candle(*r) for r in rows]

    async def sync_candles(self, client, account_id: int, symbol: str, start: int, now: int) -> int:
        details = await self.link.symbol(symbol)
        last = self.conn.execute("SELECT MAX(ts) FROM candles WHERE source=? AND symbol=? AND level='m1' AND ts>=?",
                                 (SOURCE, symbol, start)).fetchone()[0]
        begin = last if last is not None else start      # the last stored minute may have been incomplete
        stored = 0
        while begin < now:
            end = min(begin + CANDLE_CHUNK, now + 60)
            candles = await client.trendbars(account_id, details["symbol_id"], begin, end)
            with self.conn:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO candles (source, symbol, level, ts, open, high, low, close, volume) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [(SOURCE, symbol, "m1", *c) for c in candles if c.ts <= now])
            stored += len(candles)
            begin = end
        return stored

    def candles(self, cfg, started_at: int, until: int):
        """Price history for the comparison with a backtest: the broker's own candles."""
        cls = load_strategies().get(cfg.strategy)
        warmup = cls(**cfg.params).warmup() if cls else 1
        start = data_start(started_at, warmup, cfg.tf_seconds)
        return start, self.broker_candles(cfg.symbol, start, until + 60)

    # ---------- orders bookkeeping ----------

    def order(self, coid: str):
        return self.conn.execute("SELECT * FROM live_orders WHERE client_order_id=?", (coid,)).fetchone()

    def orders(self, session_id: int | None = None, limit: int = 200) -> list[dict]:
        if session_id is None:
            rows = self.conn.execute("SELECT * FROM live_orders ORDER BY created_at DESC LIMIT ?", (limit,))
        else:
            rows = self.conn.execute("SELECT * FROM live_orders WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
                                     (session_id, limit))
        return [dict(r) for r in rows]

    def _record(self, coid: str, session_id: int, intent: dict, status: str, **fields) -> bool:
        """Write-ahead: store the order before it is sent. False if this order id already exists."""
        now = int(self.now())
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO live_orders (client_order_id, session_id, client_id, kind, side, volume, lots, "
                    "stop_loss, take_profit, reason, status, position_id, error, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (coid, session_id, intent["client_id"], intent["kind"], intent.get("side"), fields.get("volume"),
                     intent.get("lots"), intent.get("stop_loss"), intent.get("take_profit"), intent.get("reason", ""),
                     status, fields.get("position_id"), fields.get("error"), now, now))
        except Exception as exc:
            if "UNIQUE" in str(exc):
                return False
            raise
        return True

    def _update(self, coid: str, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE live_orders SET {sets}, updated_at=? WHERE client_order_id=?",
                              (*fields.values(), int(self.now()), coid))

    def _log(self, session_id: int, ts: int, kind: str, message: str, **data) -> None:
        with self.conn:
            self._event(session_id, ts, kind, message, data, int(self.now()))

    async def _alert(self, key: str, level: str, title: str, message: str, email: bool = False) -> None:
        if self.alerter:
            await self.alerter.raise_(key, level, title, message, email=email)

    # ---------- the loop ----------

    async def _tick(self) -> dict:
        now = int(self.now())
        rows = self.conn.execute("SELECT * FROM paper_sessions WHERE status='running' AND mode='live' ORDER BY id"
                                 ).fetchall()
        empty = {"sessions": 0, "errors": [], "session_errors": [], "feed": {}}
        if not rows or self.kill_switch().get("active"):
            self.status["last_error"], self.status["errors_in_a_row"] = None, 0
            return empty
        feed = {"cTrader": None}
        try:
            client, account = await self.link.ensure()
            snapshot = await client.reconcile(account["account_id"])
        except Exception as exc:
            feed["cTrader"] = describe(exc)
            self.status["last_error"] = feed["cTrader"]
            self.status["errors_in_a_row"] += 1
            return {"sessions": len(rows), "errors": [feed["cTrader"]], "session_errors": [], "feed": feed}
        self.last_snapshot = {"ts": now, **snapshot}
        await self._recover(snapshot)

        strategies = load_strategies()
        errors, session_errors = [], []
        groups: dict[str, list] = {}
        for row in rows:
            groups.setdefault(row["symbol"], []).append(row)
        for symbol, group in groups.items():
            starts = []
            for row in group:
                cfg = self.config(row)
                cls = strategies.get(cfg.strategy)
                warmup = cls(**cfg.params).warmup() if cls else 1
                starts.append(data_start(json.loads(row["state"])["started_at"], warmup, cfg.tf_seconds))
            try:
                await self.sync_candles(client, account["account_id"], symbol, min(starts), now)
            except Exception as exc:
                feed["cTrader"] = describe(exc)
                errors.append(f"{symbol}: {feed['cTrader']}")
                continue
            rates = await eur_rates(self.store, INSTRUMENTS[symbol], min(starts), now, log)
            for row, start in zip(group, starts):
                try:
                    await self._advance_live(row, start, now, rates, strategies, client, account, snapshot)
                except Exception as exc:
                    log.exception("Live session %s failed", row["id"])
                    text = describe(exc) if isinstance(exc, BrokerError) else str(exc)
                    errors.append(f"sessie {row['id']}: {text}")
                    session_errors.append(f"sessie {row['id']}: {text}")
                    self._log(row["id"], now, "error", f"Fout tijdens verwerken: {text}")
        self.status["last_error"] = "; ".join(errors) if errors else None
        self.status["errors_in_a_row"] = self.status["errors_in_a_row"] + 1 if errors else 0
        return {"sessions": len(rows), "errors": errors, "session_errors": session_errors, "feed": feed}

    async def _recover(self, snapshot: dict) -> None:
        """Orders whose outcome is not known (e.g. the program stopped while sending): look them up at the
        broker. They are never sent again."""
        positions = {p["position_id"]: p for p in snapshot["positions"]}
        for row in self.conn.execute("SELECT * FROM live_orders WHERE status IN (?,?)", UNCERTAIN).fetchall():
            coid, sid = row["client_order_id"], row["session_id"]
            if row["kind"] == "open":
                match = next((p for p in snapshot["positions"] if p["comment"] == coid), None)
                if match:
                    self._update(coid, status="filled", position_id=match["position_id"], price=match["price"])
                    self._log(sid, int(self.now()), "info", f"Order {coid} blijkt uitgevoerd (positie "
                              f"#{match['position_id']}); hij wordt niet opnieuw verstuurd.", client_id=row["client_id"])
                    continue
            elif row["position_id"] and row["position_id"] not in positions:
                self._update(coid, status="filled")
                continue
            self._update(coid, status="not_found",
                         error="Niet gevonden bij de broker; niet opnieuw verstuurd.")
            self._log(sid, int(self.now()), "warning", f"De uitkomst van order {coid} is onbekend en hij staat niet "
                      "bij de broker. Hij wordt niet opnieuw verstuurd. Controleer cTrader voor de zekerheid.",
                      client_id=row["client_id"])
            await self._alert(f"order_unknown:{coid}", "warning", "Uitkomst van een order onbekend",
                              f"Order {coid} van sessie {sid} is mogelijk niet uitgevoerd. Controleer cTrader.",
                              email=self.settings.alerts.email_on_reconciliation)

    async def _advance_live(self, row, start: int, now: int, rates, strategies, client, account, snapshot) -> None:
        from ..paper.session import advance

        cfg = self.config(row)
        cls = strategies.get(cfg.strategy)
        if cls is None:
            self.set_status(row["id"], "blocked", "het strategiebestand bestaat niet meer")
            return
        if code_hash(cls) != row["code_hash"]:
            self.set_status(row["id"], "blocked",
                            f"het bestand van {cfg.strategy} is gewijzigd; maak er een nieuwe versie van")
            return
        instrument = INSTRUMENTS[cfg.symbol]
        state = json.loads(row["state"])
        await self._sync_position(cfg, state, client, account, snapshot, now, rates)
        m1 = self.broker_candles(cfg.symbol, start, now + 60)
        offline_after = max(OFFLINE_AFTER, 3 * self.settings.paper.poll_seconds)
        result = await asyncio.to_thread(advance, cfg, state, m1, now, rates, cls, instrument, offline_after,
                                         self.settings.risk, self.open_positions(exclude=cfg.id), LiveExecutor)
        with self.conn:
            for e in result.events:
                data = {k: v for k, v in e.items() if k not in ("ts", "kind", "message")}
                self._event(cfg.id, e["ts"], e["kind"], e["message"], data, now)
            self.conn.executemany("INSERT OR REPLACE INTO paper_equity (session_id, ts, equity) VALUES (?,?,?)",
                                  [(cfg.id, ts, eq) for ts, eq in result.equity])
            self.conn.execute("UPDATE paper_sessions SET state=?, updated_at=? WHERE id=?",
                              (json.dumps(result.state), now, cfg.id))
        stale_after = max(180, 3 * self.settings.paper.poll_seconds)
        for intent in sorted(result.outbox, key=lambda o: o["kind"] != "close"):
            if now - intent["ts"] > stale_after + 60:
                self._log(cfg.id, now, "skip", "Order niet verstuurd: de beslissing is te oud (het dashboard liep "
                          "achter).", client_id=intent["client_id"])
                continue
            if intent["kind"] == "close":
                await self._send_close(cfg, result.state, intent, client, account, snapshot)
            else:
                await self._send_open(cfg, intent, client, account)

    # ---------- position bookkeeping ----------

    def _closed_trades_pnl(self, session_id: int) -> float:
        return sum(t["pnl"] for t in self.trades(session_id))

    async def _sync_position(self, cfg, state, client, account, snapshot, now, rates) -> None:
        """Make the saved account agree with the broker before the strategy decides."""
        acc_state = state.get("account")
        known = (acc_state or {}).get("position")
        mine = [p for p in snapshot["positions"] if p["label"] == label(cfg.id)]
        if len(mine) > 1:
            self._log(cfg.id, now, "error", f"Er staan {len(mine)} posities met het label van deze strategie bij de "
                      "broker. Alleen de oudste wordt gevolgd; controleer cTrader.")
        broker_pos = min(mine, key=lambda p: p["open_ts"]) if mine else None

        if known and known.get("position_id") and (broker_pos is None
                                                   or broker_pos["position_id"] != known["position_id"]):
            if not await self._book_closed(cfg, state, known, client, account, now):
                missing = state.setdefault("missing_since", now)
                if now - missing > 600:
                    self._log(cfg.id, now, "error", f"Positie #{known['position_id']} staat niet meer open, maar de "
                              "broker meldt (nog) geen afsluiting. Controleer cTrader.")
                return   # keep the old view until the deal shows up
            known = None

        position, ours = None, None
        if broker_pos is not None:
            if known and known.get("position_id") == broker_pos["position_id"]:
                position = dict(known)
            else:
                position = self._adopt(cfg, broker_pos, acc_state, now, rates)
                ours = self.conn.execute("SELECT 1 FROM live_orders WHERE position_id=? AND kind='open' "
                                         "AND status='filled'", (broker_pos["position_id"],)).fetchone()
            if not (known and known.get("position_id") == broker_pos["position_id"]) and not ours:
                self._log(cfg.id, broker_pos["open_ts"], "fill",
                          f"{'Long' if broker_pos['side'] == 'long' else 'Short'} open bij de broker: "
                          f"{nl_lots(position['lots'])} lot op {nl(broker_pos['price'], INSTRUMENTS[cfg.symbol].digits)} "
                          f"(positie #{broker_pos['position_id']})", client_id=position["client_id"],
                          price=broker_pos["price"], lots=position["lots"], position_id=broker_pos["position_id"])
            position["stop_loss"] = broker_pos["stop_loss"] if broker_pos["stop_loss"] is not None else position["stop_loss"]
            position["take_profit"] = broker_pos["take_profit"]
            position["commission_eur"] = abs(broker_pos["commission"])
            position["financing_eur"] = -broker_pos["swap"]
            if broker_pos["stop_loss"] is None:
                await self._protect(cfg, broker_pos, position, client, account, now)

        capital = cfg.capital
        balance = capital + self._closed_trades_pnl(cfg.id)
        if position:
            balance -= position["commission_eur"] + position["financing_eur"]
        if acc_state is None:
            acc_state = {"balance": balance, "last_equity": balance, "halted": False, "bar_index": -1,
                         "bars_in_market": 0, "trade_count": 0, "position": None, "pending": [], "risk": {}}
        acc_state["balance"] = balance
        acc_state["position"] = position
        acc_state["pending"] = []
        state["account"] = acc_state

    async def _book_closed(self, cfg, state: dict, known: dict, client, account, now: int) -> bool:
        """Record a position the broker has closed as a trade, from the broker's deal history.
        False if the broker does not report the closing deal (yet)."""
        acc_state = state["account"]
        trade = await self._closed_trade(cfg, acc_state, known, client, account, now)
        if trade is None:
            return False
        state.pop("missing_since", None)
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO paper_trades (session_id, trade_id, exit_ts, data) "
                              "VALUES (?,?,?,?)", (cfg.id, trade["id"], trade["exit_ts"], json.dumps(trade)))
            self._event(cfg.id, trade["exit_ts"], "exit",
                        f"{'Long' if trade['side'] == 'long' else 'Short'} gesloten bij de broker "
                        f"({trade['exit_reason']}) op {nl(trade['exit_price'], INSTRUMENTS[cfg.symbol].digits)}: "
                        f"{'+' if trade['pnl'] >= 0 else '−'}€ {nl(abs(trade['pnl']))}",
                        {"client_id": trade["client_id"], "price": trade["exit_price"], "pnl": trade["pnl"],
                         "position_id": known["position_id"]}, int(self.now()))
        acc_state["trade_count"] = trade["id"]
        acc_state["position"] = None
        acc_state["balance"] = cfg.capital + self._closed_trades_pnl(cfg.id)
        acc_state["last_equity"] = acc_state["balance"]
        return True

    def _adopt(self, cfg, broker_pos: dict, acc_state, now: int, rates) -> dict:
        """A position the broker reports for this strategy that we did not know yet (normally: our order was
        just filled)."""
        instrument = INSTRUMENTS[cfg.symbol]
        order = self.order(broker_pos["comment"]) if broker_pos["comment"] else None
        units = broker_pos["volume"] / 100
        lots = units / instrument.contract_size
        stop = broker_pos["stop_loss"] if broker_pos["stop_loss"] is not None else (order["stop_loss"] if order else None)
        to_eur = rates.to_eur(now)
        equity = (acc_state or {}).get("balance", cfg.capital)
        return asdict(_Open(
            side=broker_pos["side"], lots=lots, entry_price=broker_pos["price"], entry_ts=broker_pos["open_ts"],
            entry_index=(acc_state or {}).get("bar_index", 0), stop_loss=stop if stop is not None else 0.0,
            take_profit=broker_pos["take_profit"],
            client_id=order["client_id"] if order else f"broker:{broker_pos['position_id']}",
            reason=order["reason"] if order else "", equity_at_entry=equity,
            initial_risk_eur=abs(broker_pos["price"] - stop) * units * to_eur if stop else 0.0,
            spread_eur=0.0, slippage_eur=0.0, commission_eur=abs(broker_pos["commission"]),
            financing_eur=-broker_pos["swap"], financed_day=broker_pos["open_ts"] // DAY,
            position_id=broker_pos["position_id"],
        ))

    async def _protect(self, cfg, broker_pos, position, client, account, now) -> None:
        """A position without a stop-loss at the broker: set it right away."""
        stop = position.get("stop_loss")
        if not stop:
            self._log(cfg.id, now, "error", f"Positie #{broker_pos['position_id']} heeft geen stop-loss bij de broker "
                      "en er is geen stop-loss bekend. Sluit of beveilig de positie in cTrader.")
            await self._alert(f"no_sl:{broker_pos['position_id']}", "urgent", "Positie zonder stop-loss",
                              f"Positie #{broker_pos['position_id']} van sessie {cfg.id} heeft geen stop-loss.",
                              email=True)
            return
        try:
            digits = (await self.link.symbol(cfg.symbol))["digits"]
            await client.amend_sltp(account["account_id"], broker_pos["position_id"], round(stop, digits),
                                    broker_pos["take_profit"])
            self._log(cfg.id, now, "info", f"Stop-loss op {nl(stop, INSTRUMENTS[cfg.symbol].digits)} bij de broker "
                      "gezet (ontbrak).", position_id=broker_pos["position_id"])
        except BrokerError as exc:
            self._log(cfg.id, now, "error", f"Stop-loss zetten mislukt: {describe(exc)}")

    async def _closed_trade(self, cfg, acc_state, known: dict, client, account, now: int) -> dict | None:
        # The broker returns at most one week of deals per request.
        start = max(known["entry_ts"] - 120, now - 6 * 86400)
        deals = await client.deals(account["account_id"], start, now + 60)
        mine = [d for d in deals if d["position_id"] == known["position_id"]]
        closing = [d for d in mine if d["close"]]
        if not closing:
            return None
        volume = sum(d["close"]["closed_volume"] or d["filled_volume"] for d in closing) or 1
        exit_price = sum(d["price"] * (d["close"]["closed_volume"] or d["filled_volume"]) for d in closing) / volume
        exit_ts = max(d["ts"] for d in closing)
        gross = sum(d["close"]["gross"] for d in closing)
        swap = sum(d["close"]["swap"] for d in closing)
        commission = sum(abs(d["commission"]) for d in mine)
        if not any(not d["close"] for d in mine):     # the opening deal is older than the requested week
            commission += known.get("commission_eur", 0.0)
        pnl = gross + swap - commission
        close_orders = {r["broker_order_id"]: r for r in self.conn.execute(
            "SELECT * FROM live_orders WHERE session_id=? AND kind='close' AND position_id=?",
            (cfg.id, known["position_id"])).fetchall()}
        ours = next((close_orders[d["order_id"]] for d in closing if d["order_id"] in close_orders), None)
        tick = 10 ** -INSTRUMENTS[cfg.symbol].digits * 5
        if ours:
            reason = ours["reason"] or "Signaal"
        elif known.get("stop_loss") and abs(exit_price - known["stop_loss"]) <= max(tick, abs(exit_price) * 0.0005):
            reason = "Stop-loss (bij de broker)"
        elif known.get("take_profit") and abs(exit_price - known["take_profit"]) <= max(tick, abs(exit_price) * 0.0005):
            reason = "Take-profit (bij de broker)"
        else:
            reason = "Gesloten bij de broker"
        trade_id = (acc_state or {}).get("trade_count", 0) + 1
        costs = {"spread": 0.0, "slippage": 0.0, "commission": commission, "financing": -swap}
        return {
            "id": trade_id, "side": known["side"], "lots": known["lots"], "entry_ts": known["entry_ts"],
            "entry_price": known["entry_price"], "exit_ts": exit_ts, "exit_price": exit_price,
            "stop_loss": known.get("stop_loss"), "take_profit": known.get("take_profit"),
            "entry_reason": known.get("reason", ""), "exit_reason": reason, "pnl": pnl,
            "return_pct": pnl / known["equity_at_entry"] * 100 if known.get("equity_at_entry") else 0.0,
            "r_multiple": pnl / known["initial_risk_eur"] if known.get("initial_risk_eur") else 0.0,
            "costs": costs, "costs_total": commission + max(0.0, -swap),
            "bars_held": round((exit_ts - known["entry_ts"]) / cfg.tf_seconds, 1),
            "client_id": known.get("client_id"), "position_id": known["position_id"],
            "gross_eur": gross, "source": "broker",
        }

    # ---------- sending orders ----------

    def _volume(self, lots: float, symbol: str, details: dict) -> int:
        units = lots * INSTRUMENTS[symbol].contract_size
        step = details["step_volume"] or 1
        volume = int(math.floor(units * 100 / step + 1e-9) * step)
        return min(volume, details["max_volume"]) if details["max_volume"] else volume

    async def _send_open(self, cfg, intent: dict, client, account) -> None:
        now = int(self.now())
        coid = client_order_id(cfg.id, intent["client_id"])
        if self.order(coid) is not None:
            self._log(cfg.id, now, "skip", "Order al eerder verstuurd: niet opnieuw.", client_id=intent["client_id"])
            return
        if account["is_live"] and not self.settings.live.allow_real_money:
            self._record(coid, cfg.id, intent, "skipped", error="echt geld staat uit")
            self._log(cfg.id, now, "error", "Order niet verstuurd: dit is een echt-geld-account en live.allow_real_money "
                      "staat uit.", client_id=intent["client_id"])
            return
        details = await self.link.symbol(cfg.symbol)
        volume = self._volume(intent["lots"], cfg.symbol, details)
        if volume < (details["min_volume"] or 1):
            self._record(coid, cfg.id, intent, "skipped", volume=volume, error="kleiner dan de minimale grootte")
            self._log(cfg.id, now, "skip", f"Order overgeslagen: {nl_lots(intent['lots'])} lot is kleiner dan de "
                      "kleinste positie die de broker toestaat.", client_id=intent["client_id"], reason_code="size")
            return
        # Prices at the broker have at most the symbol's number of decimals.
        digits = details["digits"]
        est = intent["est_price"]
        sl = round(intent["stop_loss"], digits)
        tp = round(intent["take_profit"], digits) if intent["take_profit"] is not None else None
        rel_sl = relative(abs(est - sl), digits)
        rel_tp = relative(abs(tp - est), digits) if tp is not None else None
        self._record(coid, cfg.id, intent, "sending", volume=volume)   # before sending: never twice
        try:
            res = await client.market_order(account["account_id"], details["symbol_id"], intent["side"], volume,
                                            label=label(cfg.id), comment=coid, client_order_id=coid,
                                            relative_sl=rel_sl, relative_tp=rel_tp)
        except BrokerError as exc:
            if exc.code in ("DISCONNECTED", "TIMEOUT"):
                self._update(coid, status="unknown", error=describe(exc))
                self._log(cfg.id, now, "warning", f"Geen bevestiging van de broker ({describe(exc)}). De order wordt "
                          "in de volgende ronde opgezocht en nooit opnieuw verstuurd.", client_id=intent["client_id"])
            else:
                self._update(coid, status="rejected", error=describe(exc))
                self._log(cfg.id, now, "error", f"Order geweigerd door de broker: {describe(exc)}",
                          client_id=intent["client_id"])
                await self._alert(f"rejected:{cfg.id}", "warning", "Order geweigerd",
                                  f"Sessie {cfg.id}: {describe(exc)}")
            return
        if res["status"] != "ORDER_FILLED" or not res["position"]:
            self._update(coid, status="rejected", error=res["error"] or res["status"])
            self._log(cfg.id, now, "error", f"Order niet uitgevoerd: {res['error'] or res['status']}",
                      client_id=intent["client_id"])
            return
        pos = res["position"]
        self._update(coid, status="filled", position_id=pos["position_id"], broker_order_id=res["order_id"],
                     price=res["price"])
        digits = INSTRUMENTS[cfg.symbol].digits
        self._log(cfg.id, now, "fill", f"{'Long' if intent['side'] == 'long' else 'Short'} uitgevoerd bij de broker: "
                  f"{nl_lots(volume / 100 / INSTRUMENTS[cfg.symbol].contract_size)} lot op {nl(res['price'], digits)} "
                  f"(positie #{pos['position_id']})", client_id=intent["client_id"], price=res["price"],
                  position_id=pos["position_id"], volume=volume)
        # The stop-loss was attached as a distance; now put it on the exact level of the signal.
        price = res["price"]
        long = intent["side"] == "long"
        sl_ok = sl < price if long else sl > price
        tp_ok = tp is not None and (tp > price if long else tp < price)
        if not sl_ok:
            self._log(cfg.id, now, "warning", "De koers stond bij uitvoering al voorbij de gewenste stop-loss: de "
                      "stop-loss op afstand blijft staan.", client_id=intent["client_id"])
            return
        try:
            await client.amend_sltp(account["account_id"], pos["position_id"], sl, tp if tp_ok else None)
            self._log(cfg.id, now, "info", f"Stop-loss staat bij de broker op {nl(sl, digits)}"
                      + (f", take-profit op {nl(tp, digits)}" if tp_ok else "") + ".",
                      client_id=intent["client_id"], position_id=pos["position_id"])
        except BrokerError as exc:
            self._log(cfg.id, now, "warning", f"Stop-loss op het exacte niveau zetten mislukt ({describe(exc)}); "
                      "de stop-loss op afstand blijft staan.", client_id=intent["client_id"])

    async def _send_close(self, cfg, state, intent: dict, client, account, snapshot) -> None:
        now = int(self.now())
        pos = (state.get("account") or {}).get("position")
        if not pos or not pos.get("position_id"):
            return
        broker_pos = next((p for p in snapshot["positions"] if p["position_id"] == pos["position_id"]), None)
        if broker_pos is None:
            return
        await self._close_at_broker(cfg.id, intent["client_id"], intent["reason"], broker_pos, client, account, now)

    async def _close_at_broker(self, session_id: int, client_id: str, reason: str, broker_pos: dict, client,
                               account, now: int) -> bool:
        coid = client_order_id(session_id, client_id)
        intent = {"kind": "close", "client_id": client_id, "reason": reason, "side": broker_pos["side"]}
        if not self._record(coid, session_id, intent, "sending", volume=broker_pos["volume"],
                            position_id=broker_pos["position_id"]):
            self._log(session_id, now, "skip", "Sluitorder al eerder verstuurd: niet opnieuw.", client_id=client_id)
            return False
        try:
            res = await client.close_position(account["account_id"], broker_pos["position_id"], broker_pos["volume"])
        except BrokerError as exc:
            status = "unknown" if exc.code in ("DISCONNECTED", "TIMEOUT") else "rejected"
            self._update(coid, status=status, error=describe(exc))
            self._log(session_id, now, "error", f"Positie sluiten mislukt: {describe(exc)}", client_id=client_id)
            raise
        ok = res["status"] == "ORDER_FILLED"
        self._update(coid, status="filled" if ok else "rejected", broker_order_id=res["order_id"],
                     price=res["price"], error=None if ok else (res["error"] or res["status"]))
        self._log(session_id, now, "order" if ok else "error",
                  f"Positie #{broker_pos['position_id']} gesloten bij de broker ({reason})" if ok
                  else f"Positie sluiten geweigerd: {res['error'] or res['status']}", client_id=client_id)
        return ok

    # ---------- stop, kill switch ----------

    async def flatten(self, row, reason: str, close: bool = True) -> dict:
        """Close every position the broker holds for this strategy (found by its label, so also a position
        opened after the last loop round) and book the trades right away."""
        if not close:
            return {"closed": 0, "cancelled": 0}
        cfg = self.config(row)
        client, account = await self.link.ensure()
        snapshot = await client.reconcile(account["account_id"])
        mine = [p for p in snapshot["positions"] if p["label"] == label(cfg.id)]
        state = json.loads(row["state"])
        if not mine and not ((state.get("account") or {}).get("position")):
            return {"closed": 0, "cancelled": 0}
        now = int(self.now())
        rates = await eur_rates(self.store, INSTRUMENTS[cfg.symbol], now - 10 * 86400, now, log)
        # First agree with the broker: book what it closed since the last round, adopt what it opened.
        await self._sync_position(cfg, state, client, account, snapshot, now, rates)
        closed = 0
        for broker_pos in mine:
            known = (state.get("account") or {}).get("position")
            if not known or known.get("position_id") != broker_pos["position_id"]:
                known = self._adopt(cfg, broker_pos, state.get("account"), now, rates)
            if await self._close_at_broker(cfg.id, f"{known['client_id']}:{reason}:{now}", reason.capitalize(),
                                           broker_pos, client, account, now):
                closed += 1
                await self._book_closed(cfg, state, known, client, account, now)
        acc = state.get("account") or {}
        with self.conn:
            if closed and "balance" in acc:
                self.conn.execute("INSERT OR REPLACE INTO paper_equity (session_id, ts, equity) VALUES (?,?,?)",
                                  (cfg.id, now - now % EQUITY_WINDOW, acc["balance"]))
            self.conn.execute("UPDATE paper_sessions SET state=?, updated_at=? WHERE id=?",
                              (json.dumps(state), now, cfg.id))
        return {"closed": closed, "cancelled": 0}

    # ---------- reconciliation with the broker ----------

    async def reconcile_broker(self) -> dict:
        rows = self.conn.execute("SELECT * FROM paper_sessions WHERE mode='live' ORDER BY id DESC").fetchall()
        report = {"sessions": [], "differences": 0, "error": None, "unknown_positions": []}
        if not rows:
            return report
        try:
            client, account = await self.link.ensure()
            snapshot = await client.reconcile(account["account_id"])
        except Exception as exc:
            report["error"] = describe(exc)
            report["differences"] = 1
            return report
        by_label = {}
        for p in snapshot["positions"]:
            by_label.setdefault(p["label"], []).append(p)
        active_labels = set()
        for row in rows:
            state = json.loads(row["state"])
            ours = (state.get("account") or {}).get("position")
            theirs = by_label.get(label(row["id"]), [])
            if row["status"] in ACTIVE:
                active_labels.add(label(row["id"]))
            elif not ours and not theirs:
                continue
            last = self.conn.execute("SELECT equity FROM paper_equity WHERE session_id=? ORDER BY ts DESC LIMIT 1",
                                     (row["id"],)).fetchone()
            checks = [c for c in reconcile_session(row, self.trades(row["id"]), last["equity"] if last else None)
                      if c["name"] != "Open positie"]
            ours_text = f"{ours['side']} #{ours.get('position_id')}" if ours else "geen"
            theirs_text = ", ".join(f"{p['side']} #{p['position_id']}" for p in theirs) or "geen"
            same = (not ours and not theirs) or (ours and len(theirs) == 1
                                                 and theirs[0]["position_id"] == ours.get("position_id"))
            checks.append({"name": "Open positie", "ours": ours_text, "theirs": theirs_text, "ok": bool(same),
                           "note": "Volgens de eigen administratie tegenover volgens de broker."})
            for p in theirs:
                checks.append({"name": f"Stop-loss #{p['position_id']}", "ours": "verplicht",
                               "theirs": "geen" if p["stop_loss"] is None else nl(p["stop_loss"],
                                                                                    INSTRUMENTS[row["symbol"]].digits),
                               "ok": p["stop_loss"] is not None, "note": "De stop-loss moet bij de broker staan."})
            stuck = self.conn.execute("SELECT COUNT(*) FROM live_orders WHERE session_id=? AND status IN (?,?)",
                                      (row["id"], *UNCERTAIN)).fetchone()[0]
            if stuck:
                checks.append({"name": "Orders met onbekende uitkomst", "ours": "0", "theirs": str(stuck), "ok": False,
                               "note": "Worden in de volgende ronde opgezocht."})
            diff = sum(1 for c in checks if not c["ok"])
            report["differences"] += diff
            summary = self.summary(row)
            report["sessions"].append({"id": row["id"], "label": f"LIVE · {summary['strategy_label']} {row['version']}",
                                       "symbol": row["symbol"], "timeframe": row["timeframe"], "status": row["status"],
                                       "checks": checks, "differences": diff})
        known_ids = {((json.loads(r["state"]).get("account") or {}).get("position") or {}).get("position_id")
                     for r in rows}
        for lbl, positions in by_label.items():
            if lbl.startswith("td-") and lbl not in active_labels:
                for p in positions:
                    if p["position_id"] not in known_ids:
                        report["unknown_positions"].append(p)
                        report["differences"] += 1
        return report
