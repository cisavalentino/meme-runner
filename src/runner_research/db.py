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
    bonding_curve   TEXT,
    migrate_tx      TEXT,
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
    liquidity_usd  REAL,
    volume_24h_usd REAL,
    txns_24h       INTEGER,
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
    offset       TEXT NOT NULL CHECK (offset IN ('15m','1h','6h','24h','25h','72h')),
    price        REAL, liquidity REAL, mcap REAL,
    volume_usd   REAL,      -- DexScreener volume.h24
    txns         INTEGER,   -- DexScreener txns.h1 (buys + sells in the last hour)
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

-- Cursors for the RPC graduation scanner and other incremental jobs.
CREATE TABLE IF NOT EXISTS state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);

-- FR4 live path: first-hour trades polled from GeckoTerminal (last 300 per call).
CREATE TABLE IF NOT EXISTS trades_1h (
    id      TEXT PRIMARY KEY,
    mint    TEXT NOT NULL,
    ts      TEXT NOT NULL,
    trader  TEXT,
    side    TEXT NOT NULL,
    usd     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_trades_mint ON trades_1h(mint, ts);

-- One row per trades poll, to tell whether the first hour is fully covered.
CREATE TABLE IF NOT EXISTS trade_polls (
    mint      TEXT NOT NULL,
    polled_at TEXT NOT NULL,
    n         INTEGER NOT NULL,
    oldest    TEXT,
    newest    TEXT,
    PRIMARY KEY (mint, polled_at)
);

CREATE TABLE IF NOT EXISTS features_1h (
    mint       TEXT PRIMARY KEY,
    features   TEXT NOT NULL,   -- JSON, trade-level FR4 values
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_ts(v) -> str | None:
    """Normalize any timestamp (ISO, Dune '2026-08-01 12:00:00.000 UTC', datetime)
    to UTC ISO-8601 with seconds, so string comparison in SQL is chronological."""
    if v is None:
        return None
    if isinstance(v, datetime):
        d = v
    else:
        s = str(v).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = path or os.environ.get("RR_DB_PATH", "data/runner.sqlite")
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)  # collector and backfill may write concurrently
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute("INSERT INTO state (key, value, updated_at) VALUES (?, ?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                 (key, value, now_iso()))
    conn.commit()


def upsert(conn: sqlite3.Connection, table: str, rows: list[dict], key: tuple[str, ...]) -> int:
    """Insert or update rows on their natural key. Adds fetched_at if missing."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    has_fetched = any(r[1] == "fetched_at" for r in conn.execute(f"PRAGMA table_info({table})"))
    if has_fetched and "fetched_at" not in cols:
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
