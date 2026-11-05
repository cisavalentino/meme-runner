"""Read candles/snapshots from the store, write labels (FR5) with features (FR4)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .config import DEFAULT_LABEL_RULES, RULE_VERSION, WINDOW_HOURS, LabelRules
from .db import upsert
from .features import context_features, price_features
from .labels import Candle, label_path

FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS features_1h (
    mint       TEXT PRIMARY KEY,
    features   TEXT NOT NULL,   -- JSON, trade-level FR4 values from Dune or live trades
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
"""


def load_candles(conn: sqlite3.Connection, pool: str) -> list[Candle]:
    rows = conn.execute(
        "SELECT ts, open, close, trades, liquidity_usd FROM candles_5m WHERE pool = ? ORDER BY ts", (pool,))
    return [Candle(datetime.fromisoformat(r["ts"]), r["close"], r["trades"] or 0, r["liquidity_usd"])
            for r in rows]


def graduation_price(conn: sqlite3.Connection, pool: str) -> float | None:
    """Open of the first candle in the canonical pool = first trade price."""
    r = conn.execute("SELECT open FROM candles_5m WHERE pool = ? ORDER BY ts LIMIT 1", (pool,)).fetchone()
    return r["open"] if r and r["open"] else None


def label_tokens(conn: sqlite3.Connection, now: datetime | None = None,
                 rules: LabelRules = DEFAULT_LABEL_RULES, rule_version: str = RULE_VERSION) -> int:
    conn.executescript(FEATURES_DDL)
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=WINDOW_HOURS)).isoformat()
    toks = conn.execute(
        "SELECT mint, canonical_pool, graduated_at FROM tokens "
        "WHERE canonical_pool IS NOT NULL AND graduated_at <= ? ORDER BY graduated_at", (cutoff,)).fetchall()
    out = []
    for t in toks:
        p0 = graduation_price(conn, t["canonical_pool"])
        if p0 is None:
            continue
        t0 = datetime.fromisoformat(t["graduated_at"])
        candles = load_candles(conn, t["canonical_pool"])
        res = label_path(t0, p0, candles, rules)
        feats = {**price_features(t0, p0, candles), **context_features(conn, t["mint"])}
        tf = conn.execute("SELECT features FROM features_1h WHERE mint = ?", (t["mint"],)).fetchone()
        if tf:
            feats.update(json.loads(tf["features"]))
        out.append({
            "mint": t["mint"], "rule_version": rule_version, "label": res.label,
            "peak_multiple": res.peak_multiple, "hours_to_peak": res.hours_to_peak,
            "close_24h_vs_peak": res.close_24h_vs_peak,
            "features": json.dumps(feats, sort_keys=True), "source": "derived",
        })
    return upsert(conn, "labels", out, key=("mint", "rule_version"))
