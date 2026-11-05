"""FR1 via Solana RPC and FR2 via GeckoTerminal OHLCV.

- `scan_backward`: historical graduations, newest -> oldest, resumable through
  the `rpc_oldest_sig` cursor (each page is committed before the cursor moves).
- `scan_forward`: new graduations since the `rpc_newest_sig` cursor (live).
- `fetch_candles`: 5m candles T0..T0+72h for tokens whose window has closed.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db
from .config import WINDOW_HOURS
from .gecko import Gecko
from .labels import grid_t0
from .solana import MIGRATION_ACCOUNT, Migration, SolanaRpc, decode_bonding_curve_creator, mint_metadata, parse_migration

log = logging.getLogger(__name__)


def sampled(key: str, rate: float) -> bool:
    """Deterministic, outcome-independent sample on an address or signature."""
    if rate >= 1:
        return True
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) / 0x1_0000_0000 < rate


def store_migrations(conn: sqlite3.Connection, rpc: SolanaRpc, migs: list[Migration],
                     with_created_at: bool = False) -> int:
    """Upsert tokens + canonical pools, with creator (bonding curve account) and
    name/symbol (Token-2022 metadata). Creation time costs 1–3 RPC calls per
    token, so it is only fetched when asked (live path)."""
    if not migs:
        return 0
    curves = rpc.accounts([m.bonding_curve for m in migs], encoding="base64")
    mints = rpc.accounts([m.mint for m in migs], encoding="jsonParsed")
    toks, pools = [], []
    for m, curve, mint in zip(migs, curves, mints):
        meta = mint_metadata(mint)
        created = rpc.oldest_signature_time(m.bonding_curve) if with_created_at else None
        toks.append({
            "mint": m.mint, "name": meta.get("name"), "symbol": meta.get("symbol"),
            "creator": decode_bonding_curve_creator(curve["data"][0]) if curve else None,
            "created_at": created.isoformat(timespec="seconds") if created else None,
            "graduated_at": m.graduated_at.isoformat(timespec="seconds"), "launchpad": "pumpfun",
            "canonical_pool": m.pool,
            "bonding_minutes": (m.graduated_at - created).total_seconds() / 60 if created else None,
            "bonding_curve": m.bonding_curve, "migrate_tx": m.signature, "source": "rpc",
        })
        pools.append({"pool": m.pool, "mint": m.mint, "dex": "pumpswap", "quote_mint": "So11111111111111111111111111111111111111112",
                      "created_at": m.graduated_at.isoformat(timespec="seconds"), "is_canonical": 1, "source": "rpc"})
    db.upsert(conn, "tokens", toks, key=("mint",))
    db.upsert(conn, "pools", pools, key=("pool",))
    return len(toks)


def _migrations(rpc: SolanaRpc, sigs: list[dict], sample: float, end_ts: int | None) -> list[Migration]:
    out = []
    for s in sigs:
        if s.get("err") is not None or (end_ts and (s.get("blockTime") or 0) >= end_ts):
            continue
        if not sampled(s["signature"], sample):
            continue
        m = parse_migration(rpc.transaction(s["signature"]), s["signature"])
        if m:
            out.append(m)
    return out


def scan_backward(conn: sqlite3.Connection, rpc: SolanaRpc, start: datetime, end: datetime | None = None,
                  sample: float = 1.0, max_pages: int | None = None) -> int:
    """Graduations in [start, end), newest first. Re-running resumes below the
    oldest page already stored; ranges newer than the first run are the live
    scanner's job."""
    before = db.get_state(conn, "rpc_oldest_sig")
    end_ts = int(end.timestamp()) if end else None
    n = pages = 0
    for page in rpc.iter_signatures(MIGRATION_ACCOUNT, before=before, stop_before_ts=int(start.timestamp())):
        if not page:
            break
        if db.get_state(conn, "rpc_newest_sig") is None:
            db.set_state(conn, "rpc_newest_sig", page[0]["signature"])
        n += store_migrations(conn, rpc, _migrations(rpc, page, sample, end_ts))
        db.set_state(conn, "rpc_oldest_sig", page[-1]["signature"])
        db.set_state(conn, "rpc_oldest_ts", datetime.fromtimestamp(page[-1]["blockTime"], timezone.utc).isoformat())
        pages += 1
        log.info("backfill page %d -> %s, %d graduations so far", pages, db.get_state(conn, "rpc_oldest_ts"), n)
        if max_pages and pages >= max_pages:
            break
    return n


def scan_forward(conn: sqlite3.Connection, rpc: SolanaRpc, with_created_at: bool = True) -> int:
    """New graduations since the last scan. First run starts at the newest page."""
    until = db.get_state(conn, "rpc_newest_sig")
    pages = list(rpc.iter_signatures(MIGRATION_ACCOUNT, until=until)) if until else [rpc.signatures(MIGRATION_ACCOUNT)]
    sigs = [s for p in pages for s in p]
    if not sigs:
        return 0
    n = store_migrations(conn, rpc, _migrations(rpc, sigs, 1.0, None), with_created_at=with_created_at)
    db.set_state(conn, "rpc_newest_sig", sigs[0]["signature"])
    if db.get_state(conn, "rpc_oldest_sig") is None:
        db.set_state(conn, "rpc_oldest_sig", sigs[-1]["signature"])
    return n


def candles_pending(conn: sqlite3.Connection, now: datetime, sample: float = 1.0) -> list[sqlite3.Row]:
    cutoff = (now - timedelta(hours=WINDOW_HOURS, minutes=10)).isoformat()
    rows = conn.execute(
        """SELECT t.mint, t.canonical_pool, t.graduated_at FROM tokens t
           WHERE t.canonical_pool IS NOT NULL AND t.graduated_at <= ?
             AND NOT EXISTS (SELECT 1 FROM candles_5m c WHERE c.pool = t.canonical_pool)
             AND NOT EXISTS (SELECT 1 FROM job_runs j WHERE j.job = 'fr2' AND j.day = t.mint AND j.status = 'done')
           ORDER BY t.graduated_at DESC""", (cutoff,)).fetchall()
    return [r for r in rows if sampled(r["mint"], sample)]


def fetch_candles(conn: sqlite3.Connection, gecko: Gecko, now: datetime | None = None,
                  sample: float = 1.0, limit: int | None = None) -> int:
    """One OHLCV call per token (1000 x 5m = 83h back from T0+72h covers the window).
    job_runs(job='fr2', day=<mint>) marks tokens done, including empty ones."""
    now = now or datetime.now(timezone.utc)
    done = 0
    for r in candles_pending(conn, now, sample)[:limit]:
        t0 = datetime.fromisoformat(r["graduated_at"])
        g0, end = grid_t0(t0), t0 + timedelta(hours=WINDOW_HOURS)
        try:
            rows = gecko.ohlcv_5m(r["canonical_pool"], before_ts=int(end.timestamp()) + 300)
        except Exception as e:
            log.warning("ohlcv %s: %s", r["mint"], e)
            db.upsert(conn, "job_runs", [{"job": "fr2", "day": r["mint"], "status": "failed",
                                          "detail": f"{type(e).__name__}: {e}"[:300], "updated_at": db.now_iso()}],
                      key=("job", "day"))
            continue
        rows = [{**c, "pool": r["canonical_pool"], "source": "geckoterminal"} for c in rows
                if g0.isoformat() <= c["ts"] < end.isoformat()]
        db.upsert(conn, "candles_5m", rows, key=("pool", "ts"))
        db.upsert(conn, "job_runs", [{"job": "fr2", "day": r["mint"], "status": "done", "rows": len(rows),
                                      "updated_at": db.now_iso()}], key=("job", "day"))
        done += 1
    return done
