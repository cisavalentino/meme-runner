"""FR1 via Solana RPC and FR2 via GeckoTerminal OHLCV.

- `scan_backward`: historical graduations, newest -> oldest, resumable through
  the `rpc_oldest_sig` cursor (each page is committed before the cursor moves).
- `scan_forward`: new graduations since the `rpc_newest_sig` cursor (live).
- `fetch_candles`: 5m candles T0..T0+72h for tokens whose window has closed.
"""
from __future__ import annotations

import hashlib

import httpx
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
    from .meta import uri_from_account
    toks, pools, uris = [], [], []
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
        pools.append({"pool": m.pool, "mint": m.mint, "dex": "pumpswap",
                      "quote_mint": m.quote_mint or "So11111111111111111111111111111111111111112",
                      "pool_base": m.pool_base, "pool_quote": m.pool_quote,
                      "created_at": m.graduated_at.isoformat(timespec="seconds"), "is_canonical": 1, "source": "rpc"})
        uris.append((m.mint, uri_from_account(mint)))
    db.upsert(conn, "tokens", toks, key=("mint",))
    db.upsert(conn, "pools", pools, key=("pool",))
    # metadata uri is in the mint account we already read; the JSON is fetched later
    conn.executemany("INSERT OR IGNORE INTO token_meta (mint, uri, status, fetched_at) VALUES (?, ?, ?, ?)",
                     [(m, u, None if u else "no_uri", db.now_iso()) for m, u in uris])
    conn.commit()
    return len(toks)


def _migrations(rpc: SolanaRpc, sigs: list[dict], sample: float, end_ts: int | None) -> list[Migration]:
    keep = [s["signature"] for s in sigs
            if s.get("err") is None and not (end_ts and (s.get("blockTime") or 0) >= end_ts)
            and sampled(s["signature"], sample)]
    txs = rpc.transactions(keep) if hasattr(rpc, "transactions") else [rpc.transaction(k) for k in keep]
    return [m for m in (parse_migration(tx, sig) for sig, tx in zip(keep, txs)) if m]


def scan_backward(conn: sqlite3.Connection, rpc: SolanaRpc, start: datetime, end: datetime | None = None,
                  sample: float = 1.0, max_pages: int | None = None, with_created_at: bool = False) -> int:
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
        n += store_migrations(conn, rpc, _migrations(rpc, page, sample, end_ts), with_created_at=with_created_at)
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


def candles_pending(conn: sqlite3.Connection, now: datetime, sample: float = 1.0,
                    historical_sample: float | None = None) -> list[sqlite3.Row]:
    """Tokens whose 72h window closed and that still need candles. Live tokens
    (graduated after state `live_since`) come first, oldest first, all of them;
    older tokens follow on a hash sample of `historical_sample` (default
    config.CANDLE_SAMPLE_HISTORICAL; `sample` multiplies both), in hash order so
    every partial backlog covers all months evenly."""
    from .config import CANDLE_SAMPLE_HISTORICAL
    hs = CANDLE_SAMPLE_HISTORICAL if historical_sample is None else historical_sample
    live_since = db.get_state(conn, "live_since")
    cutoff = (now - timedelta(hours=WINDOW_HOURS, minutes=10)).isoformat()
    rows = conn.execute(
        """SELECT t.mint, t.canonical_pool, t.graduated_at FROM tokens t
           WHERE t.canonical_pool IS NOT NULL AND t.graduated_at <= ?
             AND NOT EXISTS (SELECT 1 FROM candles_5m c WHERE c.pool = t.canonical_pool)
             AND NOT EXISTS (SELECT 1 FROM job_runs j WHERE j.job = 'fr2' AND j.day = t.mint
                             AND (j.status = 'done' OR j.updated_at > ?))  -- failed: retry hourly
           ORDER BY t.graduated_at""", (cutoff, (now - timedelta(hours=1)).isoformat(timespec="seconds"))).fetchall()
    live = [r for r in rows if live_since and r["graduated_at"] >= live_since and sampled(r["mint"], sample)]
    hist = [r for r in rows if not (live_since and r["graduated_at"] >= live_since)
            and sampled(r["mint"], sample * hs)]
    hist.sort(key=lambda r: hashlib.sha256(r["mint"].encode()).digest())
    return live + hist


def fetch_candles(conn: sqlite3.Connection, gecko: Gecko, now: datetime | None = None,
                  sample: float = 1.0, limit: int | None = None, deadline: float | None = None,
                  min_count: int = 0, claim=None, historical_sample: float | None = None) -> int:
    """One OHLCV call per token (1000 x 5m = 83h back from T0+72h covers the window).
    Stops at `deadline` (monotonic) once at least `min_count` tokens were tried.
    `claim(mint) -> bool` lets parallel workers skip tokens another one took.
    job_runs(job='fr2', day=<mint>) marks tokens done, including empty ones."""
    now = now or datetime.now(timezone.utc)
    done = 0
    import time
    tried = 0
    for r in candles_pending(conn, now, sample, historical_sample):
        if limit is not None and tried >= limit:
            break
        if deadline is not None and tried >= min_count and time.monotonic() >= deadline:
            break
        if claim is not None and not claim(r["mint"]):
            continue
        tried += 1
        t0 = datetime.fromisoformat(r["graduated_at"])
        g0, end = grid_t0(t0), t0 + timedelta(hours=WINDOW_HOURS)
        try:
            rows = gecko.ohlcv_5m(r["canonical_pool"], before_ts=int(end.timestamp()) + 300)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                rows = None
                err = e
            else:  # Gecko never indexed the pool: same as no candles
                rows = []
        except Exception as e:
            rows, err = None, e
        if rows is None:
            e = err
            from .http import redact_url
            log.warning("ohlcv %s: %s", r["mint"], e)
            db.upsert(conn, "job_runs", [{"job": "fr2", "day": r["mint"], "status": "failed",
                                          "detail": redact_url(f"{type(e).__name__}: {e}")[:300], "updated_at": db.now_iso()}],
                      key=("job", "day"))
            continue
        rows = [{**c, "pool": r["canonical_pool"], "source": "geckoterminal"} for c in rows
                if g0.isoformat() <= c["ts"] < end.isoformat()]
        db.upsert(conn, "candles_5m", rows, key=("pool", "ts"))
        db.upsert(conn, "job_runs", [{"job": "fr2", "day": r["mint"], "status": "done", "rows": len(rows),
                                      "updated_at": db.now_iso()}], key=("job", "day"))
        done += 1
    return done
