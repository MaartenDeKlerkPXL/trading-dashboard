"""Reconciliation: does our own bookkeeping agree with what the account reports?

For paper trading the "broker" is the simulated account, so we check that the account
(balance, open position, number of trades) agrees with the trade log and the equity
history. In live trading (phase 6) the right-hand column becomes what the broker reports.
"""

from __future__ import annotations

import json
import time

from .risk import RiskLimits

TOLERANCE_EUR = 0.01


def _money(v: float) -> str:
    return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _check(name: str, ours, theirs, ok: bool, note: str = "") -> dict:
    return {"name": name, "ours": ours, "theirs": theirs, "ok": ok, "note": note}


def reconcile_session(row, trades: list[dict], last_equity: float | None) -> list[dict]:
    settings = json.loads(row["settings"])
    account = (json.loads(row["state"]).get("account")) or {}
    capital = settings["capital"]
    if not account:
        return [_check("Rekening", "nog geen", "nog geen", True, "Er is nog niets verwerkt.")]
    position = account.get("position")
    balance = account["balance"]
    open_costs = (position["commission_eur"] + position["financing_eur"]) if position else 0.0
    expected = capital + sum(t["pnl"] for t in trades) - open_costs
    checks = [
        _check("Saldo", _money(expected), _money(balance), abs(expected - balance) < TOLERANCE_EUR,
               "Startkapitaal + resultaat van alle trades − kosten van de open positie, tegenover het saldo "
               "van de rekening."),
        _check("Aantal trades", str(len(trades)), str(account.get("trade_count", 0)),
               len(trades) == account.get("trade_count", 0)),
    ]
    ids = sorted(t["id"] for t in trades)
    checks.append(_check("Trade-nummers", "1 t/m " + str(len(ids)) if ids else "geen",
                         "aaneengesloten" if ids == list(range(1, len(ids) + 1)) else "gaten of dubbel",
                         ids == list(range(1, len(ids) + 1))))
    if position:
        side = "long" if position["side"] == "long" else "short"
        pos_text = f"{side} {position['lots']:.4g} lot"
        checks.append(_check("Open positie", pos_text, pos_text, position["stop_loss"] is not None,
                             "Elke open positie moet een stop-loss hebben."))
    if last_equity is not None and row["status"] != "stopped":
        eq = account.get("last_equity", capital)
        checks.append(_check("Equity", _money(last_equity), _money(eq), abs(last_equity - eq) < TOLERANCE_EUR,
                             "Laatst opgeslagen equity tegenover de rekening."))
    return checks


def reconcile_paper(paper, risk: RiskLimits) -> dict:
    now = int(time.time())
    sessions, total = [], 0
    rows = paper.conn.execute("SELECT * FROM paper_sessions ORDER BY id DESC").fetchall()
    for row in rows:
        last = paper.conn.execute("SELECT equity FROM paper_equity WHERE session_id=? ORDER BY ts DESC LIMIT 1",
                                  (row["id"],)).fetchone()
        checks = reconcile_session(row, paper.trades(row["id"]), last["equity"] if last else None)
        diff = sum(1 for c in checks if not c["ok"])
        total += diff
        summary = paper.summary(row)
        sessions.append({"id": row["id"], "label": f"{summary['strategy_label']} {row['version']}",
                         "symbol": row["symbol"], "timeframe": row["timeframe"], "status": row["status"],
                         "checks": checks, "differences": diff})
    open_positions = paper.open_positions()
    limit_ok = open_positions <= risk.max_open_positions
    total += 0 if limit_ok else 1
    return {
        "checked_at": now,
        "source": "paper",
        "sessions": sessions,
        "open_positions": open_positions,
        "open_positions_ok": limit_ok,
        "differences": total,
    }
