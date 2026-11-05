"""Read candles/snapshots from the store, write labels (FR5) with features (FR4)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .config import DEFAULT_LABEL_RULES, RULE_VERSION, WINDOW_HOURS, LabelRules
from .db import upsert
from .features import context_features, price_features
from .labels import Candle, grid_t0, label_path

def load_candles(conn: sqlite3.Connection, pool: str) -> list[Candle]:
    rows = conn.execute(
        "SELECT ts, close, trades, liquidity_usd, volume_usd FROM candles_5m WHERE pool = ? ORDER BY ts", (pool,))
    return [Candle(datetime.fromisoformat(r["ts"]), r["close"], r["trades"], r["liquidity_usd"], r["volume_usd"])
            for r in rows]


def snapshot_trades_h24(conn: sqlite3.Connection, mint: str) -> int | None:
    r = conn.execute("SELECT txns FROM snapshots WHERE mint = ? AND offset = '25h'", (mint,)).fetchone()
    return r["txns"] if r else None


def graduation_price(conn: sqlite3.Connection, pool: str) -> float | None:
    """Open of the first candle in the canonical pool = first trade price."""
    r = conn.execute("SELECT open FROM candles_5m WHERE pool = ? ORDER BY ts LIMIT 1", (pool,)).fetchone()
    return r["open"] if r and r["open"] else None


def label_tokens(conn: sqlite3.Connection, now: datetime | None = None,
                 rules: LabelRules = DEFAULT_LABEL_RULES, rule_version: str = RULE_VERSION,
                 only_new: bool = False) -> int:
    """`only_new` skips tokens already labeled under `rule_version`; inputs of a
    labeled token (candles, T+1h features) do not change after its window closed."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=WINDOW_HOURS)).isoformat()
    toks = conn.execute(
        "SELECT mint, canonical_pool, graduated_at FROM tokens "
        "WHERE canonical_pool IS NOT NULL AND graduated_at <= ? "
        + ("AND mint NOT IN (SELECT mint FROM labels WHERE rule_version = ?) " if only_new else "")
        + "ORDER BY graduated_at", (cutoff, rule_version) if only_new else (cutoff,)).fetchall()
    out = []
    for t in toks:
        p0 = graduation_price(conn, t["canonical_pool"])
        if p0 is None:
            continue
        t0 = grid_t0(datetime.fromisoformat(t["graduated_at"]))
        candles = load_candles(conn, t["canonical_pool"])
        res = label_path(t0, p0, candles, rules, trades_h24=snapshot_trades_h24(conn, t["mint"]))
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
