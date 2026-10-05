"""SQLite connection and schema migrations."""

from __future__ import annotations

import json
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
    """
    CREATE TABLE paper_sessions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at    INTEGER NOT NULL,
        updated_at    INTEGER NOT NULL,
        status        TEXT NOT NULL,           -- running | paused | stopped | blocked
        status_reason TEXT NOT NULL DEFAULT '',
        symbol        TEXT NOT NULL,
        timeframe     TEXT NOT NULL,
        strategy      TEXT NOT NULL,           -- 'name@version'
        version       TEXT NOT NULL,
        code_hash     TEXT NOT NULL,           -- the strategy file this session is locked to
        params        TEXT NOT NULL,           -- JSON
        settings      TEXT NOT NULL,           -- JSON: capital, risk, sizing, costs
        state         TEXT NOT NULL            -- JSON: account + loop position
    );
    CREATE TABLE paper_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES paper_sessions(id) ON DELETE CASCADE,
        ts         INTEGER NOT NULL,           -- market time of the event
        logged_at  INTEGER NOT NULL,           -- wall-clock time it was recorded
        kind       TEXT NOT NULL,
        message    TEXT NOT NULL,
        data       TEXT NOT NULL               -- JSON
    );
    CREATE INDEX paper_events_session ON paper_events (session_id, id);
    CREATE TABLE paper_trades (
        session_id INTEGER NOT NULL REFERENCES paper_sessions(id) ON DELETE CASCADE,
        trade_id   INTEGER NOT NULL,
        exit_ts    INTEGER NOT NULL,
        data       TEXT NOT NULL,              -- JSON
        PRIMARY KEY (session_id, trade_id)
    ) WITHOUT ROWID;
    CREATE TABLE paper_equity (
        session_id INTEGER NOT NULL REFERENCES paper_sessions(id) ON DELETE CASCADE,
        ts         INTEGER NOT NULL,           -- start of a 15-minute window
        equity     REAL NOT NULL,
        PRIMARY KEY (session_id, ts)
    ) WITHOUT ROWID;
    CREATE TABLE evaluations (
        session_id INTEGER PRIMARY KEY REFERENCES paper_sessions(id) ON DELETE CASCADE,
        criteria   TEXT NOT NULL DEFAULT '[]', -- JSON
        notes      TEXT NOT NULL DEFAULT '',
        decision   TEXT NOT NULL DEFAULT 'open',
        locked_at  INTEGER,
        updated_at INTEGER NOT NULL
    );
    """,
    """
    CREATE TABLE app_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL                    -- JSON
    ) WITHOUT ROWID;
    CREATE TABLE alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        key         TEXT NOT NULL,             -- one problem, e.g. 'feed:XAUUSD'
        level       TEXT NOT NULL,             -- urgent | warning | info
        title       TEXT NOT NULL,
        message     TEXT NOT NULL,
        first_at    INTEGER NOT NULL,
        last_at     INTEGER NOT NULL,
        count       INTEGER NOT NULL DEFAULT 1,
        emailed_at  INTEGER,
        email_error TEXT,
        resolved_at INTEGER
    );
    CREATE INDEX alerts_key ON alerts (key, resolved_at);
    """,
]


def get_state(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(conn: sqlite3.Connection, key: str, value) -> None:
    with conn:
        conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


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
