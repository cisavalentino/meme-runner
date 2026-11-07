"""FR3 — live collector: scheduling plus one `run_once` step that discovers
graduations (RPC), takes due snapshots (DexScreener, + Gecko holders at 1h),
polls first-hour trades (Gecko, sampled), finalizes trade features, records
other pools of a mint (FR6) and fetches candles for closed windows."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db
from .config import FEATURE_CUTOFF_MIN, GECKO_DETAIL_SAMPLE, HOLDERS_SAMPLE, SNAPSHOT_OFFSETS_MIN, TRADE_POLL_EVERY_MIN
from .features import Trade, trade_features

log = logging.getLogger(__name__)

MAX_LATENESS = timedelta(minutes=2)
# a snapshot later than this is still taken, but flagged unusable for features
GIVE_UP_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class Due:
    mint: str
    offset: str
    target_at: datetime


def due_snapshots(conn: sqlite3.Connection, now: datetime) -> list[Due]:
    horizon = now - timedelta(minutes=max(SNAPSHOT_OFFSETS_MIN.values())) - GIVE_UP_AFTER
    toks = conn.execute(
        "SELECT mint, graduated_at FROM tokens WHERE graduated_at >= ? AND graduated_at <= ?",
        (horizon.isoformat(), now.isoformat())).fetchall()
    taken = {(r["mint"], r["offset"]) for r in conn.execute(
        "SELECT mint, offset FROM snapshots WHERE target_at >= ?", (horizon.isoformat(),))}
    due = []
    for t in toks:
        t0 = datetime.fromisoformat(t["graduated_at"])
        for name, mins in SNAPSHOT_OFFSETS_MIN.items():
            target = t0 + timedelta(minutes=mins)
            if target <= now <= target + GIVE_UP_AFTER and (t["mint"], name) not in taken:
                due.append(Due(t["mint"], name, target))
    return sorted(due, key=lambda d: d.target_at)  # most overdue first


def next_wakeup(conn: sqlite3.Connection, now: datetime) -> datetime | None:
    """Earliest future target, so the loop can sleep until then (<= 5 min poll)."""
    best = None
    for t in conn.execute("SELECT graduated_at FROM tokens WHERE graduated_at >= ?",
                          ((now - timedelta(days=4)).isoformat(),)):
        t0 = datetime.fromisoformat(t["graduated_at"])
        for mins in SNAPSHOT_OFFSETS_MIN.values():
            target = t0 + timedelta(minutes=mins)
            if target > now and (best is None or target < best):
                best = target
    return best


def is_on_time(target_at: datetime, taken_at: datetime) -> bool:
    return taken_at - target_at <= MAX_LATENESS


# ---------------------------------------------------------------- snapshots

def take_snapshots(conn: sqlite3.Connection, dexs, gecko, now: datetime,
                   detail_sample: float = HOLDERS_SAMPLE) -> int:
    from .ingest import sampled
    due = due_snapshots(conn, now)
    if not due:
        return 0
    canon = {r["mint"]: r["canonical_pool"] for r in conn.execute(
        f"SELECT mint, canonical_pool FROM tokens WHERE mint IN ({','.join('?' * len(due))})",
        [d.mint for d in due])}
    got = dexs.tokens({m: p for m, p in canon.items() if p})
    rows = []
    for d in due:
        snap = got.get(d.mint)
        if snap is None:
            continue  # not listed yet; retried until GIVE_UP_AFTER
        snap.pop("_canon", None)
        row = {"mint": d.mint, "offset": d.offset, "price": None, "price_native": None, "liquidity": None, "mcap": None,
               "volume_usd": None, "txns": None, "holders": None, "top10_pct": None, "dev_pct": None,
               "mint_auth": None, "freeze_auth": None, "dexs_profile": None, "dexs_boosts": None,
               **snap, "target_at": d.target_at.isoformat(timespec="seconds"),
               "taken_at": now.isoformat(timespec="seconds"), "source": "dexscreener"}
        if d.offset in ("1h", "24h") and gecko is not None and sampled(d.mint, detail_sample):
            try:
                info = gecko.token_info(d.mint)
                row.update({k: info[k] for k in ("holders", "top10_pct", "dev_pct", "mint_auth", "freeze_auth")})
                row["source"] = "dexscreener+geckoterminal"
            except Exception as e:
                log.warning("token_info %s: %s", d.mint, e)
        rows.append(row)
    db.upsert(conn, "snapshots", rows, key=("mint", "offset"))
    for r in rows:
        if r["offset"] == "24h":
            record_other_pools(conn, dexs, r["mint"], canon.get(r["mint"]))
    return len(rows)


def record_other_pools(conn: sqlite3.Connection, dexs, mint: str, canonical: str | None) -> int:
    """FR6: every other pool of the mint, flagged by the fake-pool rules.
    DexScreener has no distinct-trader count, so txns_24h stands in for it."""
    from .fakecheck import fake_pool_reasons
    try:
        pairs = dexs.token_pairs(mint)
    except Exception as e:
        log.warning("token_pairs %s: %s", mint, e)
        return 0
    rows = []
    for p in pairs:
        canon = p["pool"] == canonical
        reasons = fake_pool_reasons(is_canonical=canon, has_launchpad_record=True,
                                    volume_24h_usd=p["volume_24h_usd"], liquidity_usd=p["liquidity_usd"],
                                    distinct_traders_24h=p["txns_24h"])
        rows.append({"pool": p["pool"], "mint": mint, "dex": p["dex"], "quote_mint": p["quote_mint"],
                     "created_at": p["created_at"], "is_canonical": int(canon), "fake_flag": int(bool(reasons)),
                     "fake_reason": ",".join(reasons) or None, "liquidity_usd": p["liquidity_usd"],
                     "volume_24h_usd": p["volume_24h_usd"], "txns_24h": p["txns_24h"], "source": "dexscreener"})
    return db.upsert(conn, "pools", rows, key=("pool",))


# ---------------------------------------------------------------- first-hour trades

def trade_poll_targets(conn: sqlite3.Connection, now: datetime, sample: float = GECKO_DETAIL_SAMPLE) -> list:
    from .ingest import sampled
    lo = (now - timedelta(minutes=FEATURE_CUTOFF_MIN + 5)).isoformat()
    toks = conn.execute("SELECT mint, canonical_pool, graduated_at FROM tokens "
                        "WHERE graduated_at >= ? AND graduated_at <= ? AND canonical_pool IS NOT NULL",
                        (lo, now.isoformat())).fetchall()
    # live test: every token it has not rejected (no row yet = not decided)
    rejected = {r[0] for r in conn.execute(
        "SELECT mint FROM live_signals WHERE t0 >= ? AND status = 'rejected'", (lo,))}
    out = []
    for t in toks:
        if not sampled(t["mint"], sample) and t["mint"] in rejected:
            continue
        t0 = datetime.fromisoformat(t["graduated_at"])
        last = conn.execute("SELECT MAX(polled_at) FROM trade_polls WHERE mint = ?", (t["mint"],)).fetchone()[0]
        end = t0 + timedelta(minutes=FEATURE_CUTOFF_MIN)
        if last and datetime.fromisoformat(last) >= end:
            continue  # final poll after the cutoff is done
        if last is None or now - datetime.fromisoformat(last) >= timedelta(minutes=TRADE_POLL_EVERY_MIN) or now >= end:
            out.append(t)
    return out


TRADE_POLLS_PER_LOOP = 3   # keeps one loop short so snapshots stay on time; the rest wait a loop


def poll_trades(conn: sqlite3.Connection, gecko, now: datetime, sample: float = GECKO_DETAIL_SAMPLE,
                limit: int | None = TRADE_POLLS_PER_LOOP) -> int:
    n = 0
    targets = trade_poll_targets(conn, now, sample)
    for t in targets[:limit] if limit else targets:
        try:
            trades = gecko.trades(t["canonical_pool"], t["mint"])
        except Exception as e:
            log.warning("trades %s: %s", t["mint"], e)
            continue
        if trades:
            db.upsert(conn, "trades_1h", [{"id": i, "mint": t["mint"], "ts": tr.ts.isoformat(timespec="seconds"),
                                           "trader": tr.trader, "side": tr.side, "usd": tr.usd}
                                          for i, tr in trades], key=("id",))
        ts = sorted(tr.ts for _, tr in trades)
        conn.execute("INSERT OR REPLACE INTO trade_polls VALUES (?, ?, ?, ?, ?)",
                     (t["mint"], now.isoformat(timespec="seconds"), len(trades),
                      ts[0].isoformat() if ts else None, ts[-1].isoformat() if ts else None))
        conn.commit()
        n += 1
    return n


TRADES_PER_CALL = 300


def first_hour_complete(t0: datetime, polls: list[sqlite3.Row]) -> bool:
    """True if consecutive polls overlap and cover T0..T0+1h. A poll that
    returned fewer than 300 trades reaches back to the pool's first trade."""
    if not polls or datetime.fromisoformat(polls[-1]["polled_at"]) < t0 + timedelta(minutes=FEATURE_CUTOFF_MIN):
        return False
    covered_to = None
    for p in polls:
        full = p["n"] >= TRADES_PER_CALL
        if covered_to is None:
            if full and datetime.fromisoformat(p["oldest"]) > t0:
                return False
        elif full and datetime.fromisoformat(p["oldest"]) > covered_to:
            return False
        covered_to = datetime.fromisoformat(p["polled_at"])
    return True


def finalize_trade_features(conn: sqlite3.Connection, now: datetime) -> int:
    end_cut = (now - timedelta(minutes=FEATURE_CUTOFF_MIN)).isoformat()
    toks = conn.execute(
        """SELECT t.mint, t.graduated_at, t.creator FROM tokens t
           WHERE t.graduated_at <= ? AND EXISTS (SELECT 1 FROM trade_polls p WHERE p.mint = t.mint)
             AND NOT EXISTS (SELECT 1 FROM features_1h f WHERE f.mint = t.mint)""", (end_cut,)).fetchall()
    rows = []
    for t in toks:
        t0 = datetime.fromisoformat(t["graduated_at"])
        polls = conn.execute("SELECT * FROM trade_polls WHERE mint = ? ORDER BY polled_at", (t["mint"],)).fetchall()
        if datetime.fromisoformat(polls[-1]["polled_at"]) < t0 + timedelta(minutes=FEATURE_CUTOFF_MIN):
            if now - t0 < timedelta(minutes=FEATURE_CUTOFF_MIN + 30):
                continue  # final poll may still come
        trades = [Trade(datetime.fromisoformat(r["ts"]), r["trader"], r["side"], r["usd"]) for r in
                  conn.execute("SELECT ts, trader, side, usd FROM trades_1h WHERE mint = ?", (t["mint"],))]
        complete = first_hour_complete(t0, polls)
        # incomplete coverage would bias every count; keep only the flag then
        f = trade_features(t0, trades, t["creator"]) if complete else {}
        f["trades_complete"] = complete
        rows.append({"mint": t["mint"], "features": json.dumps(f, sort_keys=True), "source": "geckoterminal"})
    return db.upsert(conn, "features_1h", rows, key=("mint",))


# ---------------------------------------------------------------- loop

def run_once(conn: sqlite3.Connection, rpc, dexs, gecko, now: datetime, candles_per_step: int | None = 3,
             candles_until: float | None = None) -> dict:
    """Time-critical steps first; the candle backlog (live and backfilled tokens)
    then uses whatever is left of the loop (`candles_until`, a monotonic time)."""
    from .ingest import fetch_candles, scan_forward
    out = {}
    steps = [
        ("discovered", lambda: scan_forward(conn, rpc)),
        ("snapshots", lambda: take_snapshots(conn, dexs, gecko, now)),
        ("trade_polls", lambda: poll_trades(conn, gecko, now)),
        ("trade_features", lambda: finalize_trade_features(conn, now)),
        # at least 2 per loop, so a slow snapshot/trade step can't starve the backlog
        ("candles", lambda: fetch_candles(conn, gecko, now, limit=candles_per_step, deadline=candles_until,
                                          min_count=2 if candles_until is not None else 0)),
    ]
    for name, fn in steps:
        try:
            out[name] = fn()
        except Exception as e:  # one failing source must not stop the others
            log.exception("collector step %s failed", name)
            from .http import redact_url
            out[name] = redact_url(f"error: {type(e).__name__}: {e}")[:200]
    db.set_state(conn, "collector_heartbeat", json.dumps({"at": now.isoformat(timespec="seconds"), **out}))
    return out
