"""Saved runs: every backtest (and every TradingView import) is stored so it can be reviewed later."""

from __future__ import annotations

import json
import sqlite3
import time
import zlib

# Fields of a run result that are stored compressed (they can be large).
HEAVY_FIELDS = ("trades", "equity", "drawdown", "events", "warnings", "oos", "bars", "warmup")


def _pack(data: dict) -> bytes:
    return zlib.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 6)


def _unpack(blob: bytes) -> dict:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class RunStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(
        self,
        result: dict,
        source: str = "engine",
        code_hash: str = "",
        group_id: str | None = None,
        name: str = "",
    ) -> int:
        s = result["settings"]
        heavy = {k: result[k] for k in HEAVY_FIELDS if k in result}
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (created_at, source, name, symbol, timeframe, start, end, strategy, version, "
                "code_hash, settings, metrics, result, group_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    int(time.time()), source, name, s.get("symbol"), s.get("timeframe"), s.get("start"), s.get("end"),
                    s.get("strategy"), s.get("version"), code_hash,
                    json.dumps(s), json.dumps(result["metrics"]), _pack(heavy), group_id,
                ),
            )
        return int(cur.lastrowid)

    def list(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, created_at, source, name, note, symbol, timeframe, start, end, strategy, version, "
            "code_hash, settings, metrics, group_id FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._summary(r) for r in rows]

    def get(self, run_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        data = self._summary(row)
        data.update(_unpack(row["result"]))
        return data

    def delete(self, run_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return cur.rowcount > 0

    def update(self, run_id: int, name: str | None = None, note: str | None = None) -> bool:
        fields, values = [], []
        if name is not None:
            fields.append("name = ?")
            values.append(name.strip()[:120])
        if note is not None:
            fields.append("note = ?")
            values.append(note.strip()[:5000])
        if not fields:
            return True
        with self.conn:
            cur = self.conn.execute(f"UPDATE runs SET {', '.join(fields)} WHERE id = ?", (*values, run_id))
        return cur.rowcount > 0

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "source": row["source"],
            "name": row["name"],
            "note": row["note"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "start": row["start"],
            "end": row["end"],
            "strategy": row["strategy"],
            "version": row["version"],
            "code_hash": row["code_hash"],
            "group_id": row["group_id"],
            "settings": json.loads(row["settings"]),
            "metrics": json.loads(row["metrics"]),
        }
