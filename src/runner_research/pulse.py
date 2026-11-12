"""Snapshots of busy tokens every 5 minutes (`rr pulse`, pm2 rr-pulse).

Research data for an "early runner among busy tokens" rule: which signs come
first (holder growth, unique buyers, volume acceleration, retail share) is
only testable with a time series, and none of the APIs serve one for holders
or buyers. So we record it from now on.

Each tick:
  1. universe = pump.fun graduates of the last 24h + the trend watch list
     (old graduates still liquid, trending tokens) + other-launchpad tokens
     seen in the last 24h
  2. one DexScreener batch for all of them (30 mints per call, free); a row is
     stored for every token with 1h volume >= MIN_VOL1H
  3. deep check (Helius, daily budget) for the hottest few: holder count,
     top10, and the recent buy flow / trader fees of fees.fee_ratio
Rows are never updated; labels come later from the rows themselves.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db, early, trend
from .webpage import fetch_pairs

log = logging.getLogger(__name__)
MIN_VOL1H = 3_000             # store a row only for tokens with some trading
DEEP_MIN_LIQ = 10_000
DEEP_MIN_VOL1H = 10_000
DEEP_EVERY = timedelta(minutes=15)   # per token
DEEP_PER_DAY = int(os.environ.get("PULSE_DEEP_PER_DAY", "500"))
DEEP_MAX_TICK = 6
HOLDER_PAGES = 10             # DAS pages of 1000 accounts; more holders -> holders_capped
TICK = timedelta(minutes=5)
_iso = trend._iso


def universe(conn: sqlite3.Connection, now: datetime) -> dict[str, str]:
    """mint -> why it is watched (first match wins: signal, trending, grad24h, alive, ext)."""
    since = _iso(now - timedelta(hours=24))
    out: dict[str, str] = {}
    for (m,) in conn.execute("SELECT mint FROM live_signals WHERE status IN ('signal', 'scored') AND t0 >= ?", (since,)):
        out.setdefault(m, "signal")
    for m, why in trend._alive(conn).items():
        if why == "trending":
            out.setdefault(m, "trending")
    for (m,) in conn.execute("SELECT mint FROM tokens WHERE graduated_at >= ?", (since,)):
        out.setdefault(m, "grad24h")
    for m in trend._alive(conn):
        out.setdefault(m, "alive")
    for (m,) in conn.execute("SELECT mint FROM ext_tokens WHERE fetched_at >= ?", (since,)):
        out.setdefault(m, "ext")
    return out


def _n(d, *ks):
    for k in ks:
        d = (d or {}).get(k)
    try:
        return float(d) if d is not None else None
    except (TypeError, ValueError):
        return None


def row_of(m: str, src: str, p: dict, ts: str) -> dict:
    created = p.get("pairCreatedAt")
    return {
        "mint": m, "ts": ts, "src": src, "pool": p.get("pairAddress"), "dex": p.get("dexId"),
        "price": _n(p, "priceUsd"), "mcap": p.get("marketCap") or p.get("fdv"), "liq": _n(p, "liquidity", "usd"),
        "vol5m": _n(p, "volume", "m5"), "vol1h": _n(p, "volume", "h1"), "vol6h": _n(p, "volume", "h6"),
        "vol24h": _n(p, "volume", "h24"),
        "buys5m": _n(p, "txns", "m5", "buys"), "sells5m": _n(p, "txns", "m5", "sells"),
        "buys1h": _n(p, "txns", "h1", "buys"), "sells1h": _n(p, "txns", "h1", "sells"),
        "chg5m": _n(p, "priceChange", "m5"), "chg1h": _n(p, "priceChange", "h1"), "chg6h": _n(p, "priceChange", "h6"),
        "pair_age_h": round((datetime.fromisoformat(ts).timestamp() - created / 1000) / 3600, 2) if created else None,
        "boosts": _n(p, "boosts", "active"),
    }


def heat(r: dict) -> float:
    """How suddenly the token got busy: last hour vs its average hour of the day."""
    v1, v24 = r["vol1h"] or 0, r["vol24h"] or 0
    avg = max((v24 - v1) / 23, 500)
    return v1 / avg + 3 * ((r["vol5m"] or 0) * 12 / max(v1, 1))


def _budget(conn, now: datetime) -> tuple[str, int, int]:
    key = f"pulse_deep_{now:%Y-%m-%d}"
    used = int(db.get_state(conn, key) or 0)
    ticks_left = max(1, int((datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo)
                             - now) / TICK))
    allow = min(DEEP_MAX_TICK, max(0, DEEP_PER_DAY - used), math.ceil(max(0, DEEP_PER_DAY - used) / ticks_left))
    return key, used, allow


def deep(conn, api, rpc, r: dict, p: dict) -> dict:
    tok = trend._tok(conn, r["mint"])
    accs = early.token_accounts(rpc, r["mint"], max_pages=HOLDER_PAGES)
    exclude = {r["pool"]} | trend.program_owned(rpc, accs)
    if tok is not None:
        exclude |= {tok["canonical_pool"], tok["bonding_curve"]}
    hs = early.holder_stats(accs, {x for x in exclude if x}, tok["creator"] if tok is not None else None)
    f = {"holders": hs["holders"], "holders_capped": int(len(accs) >= HOLDER_PAGES * 1000),
         "top10": round(hs["top10"], 2), "dev": round(hs["dev"], 2) if hs.get("dev") is not None else None}
    fl = trend._fees(api, p, r["mint"])
    f.update({k: fl.get(k) for k in ("fee_ratio", "flow_buys", "flow_buyers", "retail_share", "small_share",
                                       "flow_span_s")})
    return f


def step(conn: sqlite3.Connection, api, rpc, now: datetime) -> dict:
    ts = _iso(now)
    uni = universe(conn, now)
    pairs = fetch_pairs(api, sorted(uni))
    rows = [row_of(m, uni[m], p, ts) for m, p in pairs.items()
            if m in uni and (_n(p, "volume", "h1") or 0) >= MIN_VOL1H]
    out = {"universe": len(uni), "pairs": len(pairs), "rows": len(rows), "deep": 0}
    key, used, allow = _budget(conn, now)
    if rpc is not None and allow:
        recent = {m for (m,) in conn.execute("SELECT DISTINCT mint FROM pulse WHERE holders IS NOT NULL AND ts > ?",
                                              (_iso(now - DEEP_EVERY + timedelta(seconds=30)),))}
        cands = sorted((r for r in rows if (r["liq"] or 0) >= DEEP_MIN_LIQ and (r["vol1h"] or 0) >= DEEP_MIN_VOL1H
                        and r["mint"] not in recent), key=heat, reverse=True)
        for r in cands[:allow]:
            used += 1
            db.set_state(conn, key, str(used))
            try:
                r.update(deep(conn, api, rpc, r, pairs[r["mint"]]))
                out["deep"] += 1
            except Exception as ex:
                log.warning("pulse deep %s: %s", r["mint"], ex)
    if rows:
        cols = sorted({k for r in rows for k in r})                 # upsert takes the columns of one row
        db.upsert(conn, "pulse", [{k: r.get(k) for k in cols} for r in rows], key=("mint", "ts"))
    out["deep_used_today"] = used
    return out


def stats(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT mint) mints, COUNT(holders) deep, MIN(ts) first, MAX(ts) last "
                     "FROM pulse").fetchone()
    by = dict(conn.execute("SELECT src, COUNT(DISTINCT mint) FROM pulse GROUP BY src").fetchall())
    return {**dict(r), "mints_by_source": by}

