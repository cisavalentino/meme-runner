"""SQLite store. Keys are mint/pool addresses, never symbols.

All writes are upserts on the natural key, so any job can be rerun for any
date range without creating duplicate rows.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tokens (
    mint            TEXT PRIMARY KEY,
    name            TEXT,
    symbol          TEXT,
    creator         TEXT,
    created_at      TEXT,
    graduated_at    TEXT,
    launchpad       TEXT NOT NULL DEFAULT 'pumpfun',
    canonical_pool  TEXT,
    bonding_minutes REAL,
    source          TEXT NOT NULL,
    fetched_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tokens_grad ON tokens(graduated_at);
CREATE INDEX IF NOT EXISTS ix_tokens_symbol ON tokens(symbol, created_at);
CREATE INDEX IF NOT EXISTS ix_tokens_name ON tokens(name, created_at);
CREATE INDEX IF NOT EXISTS ix_tokens_creator ON tokens(creator, created_at);

CREATE TABLE IF NOT EXISTS pools (
    pool         TEXT PRIMARY KEY,
    mint         TEXT NOT NULL,
    dex          TEXT,
    quote_mint   TEXT,
    created_at   TEXT,
    is_canonical INTEGER NOT NULL DEFAULT 0,
    fake_flag    INTEGER,
    fake_reason  TEXT,
    source       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pools_mint ON pools(mint);

CREATE TABLE IF NOT EXISTS candles_5m (
    pool       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    open       REAL, high REAL, low REAL, close REAL,
    volume_usd REAL,
    trades     INTEGER,
    buyers     INTEGER,
    sellers    INTEGER,
    liquidity_usd REAL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (pool, ts)
);

CREATE TABLE IF NOT EXISTS snapshots (
    mint         TEXT NOT NULL,
    offset       TEXT NOT NULL CHECK (offset IN ('15m','1h','6h','24h','72h')),
    price        REAL, liquidity REAL, mcap REAL, volume_usd REAL, txns INTEGER,
    holders      INTEGER, top10_pct REAL, dev_pct REAL,
    mint_auth    TEXT, freeze_auth TEXT,
    dexs_profile INTEGER, dexs_boosts INTEGER,
    target_at    TEXT NOT NULL,
    taken_at     TEXT NOT NULL,
    source       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (mint, offset)
);

CREATE TABLE IF NOT EXISTS labels (
    mint             TEXT NOT NULL,
    rule_version     TEXT NOT NULL,
    label            TEXT NOT NULL,
    peak_multiple    REAL,
    hours_to_peak    REAL,
    close_24h_vs_peak REAL,
    features         TEXT,      -- JSON of FR4 values (T+1h only)
    source           TEXT NOT NULL,
    fetched_at       TEXT NOT NULL,
    PRIMARY KEY (mint, rule_version)
);

-- Resumability for the backfill: one row per (job, UTC day).
CREATE TABLE IF NOT EXISTS job_runs (
    job        TEXT NOT NULL,
    day        TEXT NOT NULL,
    status     TEXT NOT NULL,
    rows       INTEGER,
    credits    REAL,
    detail     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job, day)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = path or os.environ.get("RR_DB_PATH", "data/runner.sqlite")
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, table: str, rows: list[dict], key: tuple[str, ...]) -> int:
    """Insert or update rows on their natural key. Adds fetched_at if missing."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    if "fetched_at" not in cols:
        cols.append("fetched_at")
    stamp = now_iso()
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in key)
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}"
    )
    conn.executemany(sql, [{"fetched_at": stamp, **r} for r in rows])
    conn.commit()
    return len(rows)
