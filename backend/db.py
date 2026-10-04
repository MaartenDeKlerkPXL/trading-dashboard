"""SQLite connection and schema migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Each entry runs once, in order. Never edit an existing entry: append a new one.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE candles (
        source TEXT NOT NULL,
        symbol TEXT NOT NULL,
        level  TEXT NOT NULL,          -- 'm1' | 'h1' | 'd1'
        ts     INTEGER NOT NULL,       -- open time, unix seconds UTC
        open   REAL NOT NULL,
        high   REAL NOT NULL,
        low    REAL NOT NULL,
        close  REAL NOT NULL,
        volume REAL NOT NULL,
        PRIMARY KEY (source, symbol, level, ts)
    ) WITHOUT ROWID;

    CREATE TABLE fetched_chunks (
        source      TEXT NOT NULL,
        symbol      TEXT NOT NULL,
        level       TEXT NOT NULL,
        chunk_start INTEGER NOT NULL,
        fetched_at  INTEGER NOT NULL,
        complete    INTEGER NOT NULL,  -- 1 = final, never re-downloaded
        candles     INTEGER NOT NULL,
        PRIMARY KEY (source, symbol, level, chunk_start)
    ) WITHOUT ROWID;
    """,
    """
    CREATE TABLE runs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        source     TEXT NOT NULL,              -- 'engine' | 'tradingview'
        name       TEXT NOT NULL DEFAULT '',
        note       TEXT NOT NULL DEFAULT '',
        symbol     TEXT,
        timeframe  TEXT,
        start      TEXT,
        end        TEXT,
        strategy   TEXT,                       -- 'name@version'
        version    TEXT,
        code_hash  TEXT NOT NULL DEFAULT '',   -- fingerprint of the strategy file at run time
        settings   TEXT NOT NULL,              -- JSON
        metrics    TEXT NOT NULL,              -- JSON
        result     BLOB NOT NULL,              -- zlib-compressed JSON: trades, equity, events, ...
        group_id   TEXT
    );
    CREATE INDEX runs_group ON runs (group_id);
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(sql)
            conn.execute(f"PRAGMA user_version = {i}")
