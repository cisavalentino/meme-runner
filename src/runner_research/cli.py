"""rr — command line entry point."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db
from .fakecheck import check_address


def load_dotenv(path: str = ".env") -> None:
    """KEY=VALUE lines into os.environ; never overrides variables already set."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip())


def rpc_urls() -> list[str]:
    """RR_SOLANA_RPC: comma-separated private endpoints (e.g. Helius). Each gets
    RR_RPC_WORKERS parallel transaction workers (default 8). Without one, the
    public endpoint is used alone (its per-IP limit makes extra workers useless).
    The public endpoint is not mixed in: one slow worker would hold up every page."""
    from .solana import RPC_URL
    urls = [u.strip() for u in os.environ.get("RR_SOLANA_RPC", "").split(",") if u.strip()]
    if not urls:
        return [RPC_URL]
    workers = int(os.environ.get("RR_RPC_WORKERS", "6"))
    return [u for _ in range(workers) for u in urls]


def demo_budget_keys() -> list[str]:
    """state keys for the monthly call count of each demo key (first keeps the old name)."""
    from .gecko import demo_keys
    return ["cg_demo_calls" if i == 0 else f"cg_demo_calls_{i + 1}" for i in range(len(demo_keys()))]


def private_rpc() -> bool:
    return bool(os.environ.get("RR_SOLANA_RPC", "").strip())


def clients():
    from .dexscreener import DexScreener
    from .gecko import Gecko
    from .http import ApiClient
    from .solana import SolanaRpc

    api = ApiClient()
    rpc = SolanaRpc(api, rpc_urls())
    return api, rpc, DexScreener(api), Gecko(api)


def run_analysis(conn, split: date, out: str) -> None:
    from . import analysis

    rows = analysis.load_rows(conn)
    if not rows:
        raise SystemExit("no labels yet; run `rr label` first")
    split_dt = datetime.combine(split, datetime.min.time(), tzinfo=timezone.utc)
    numeric = sorted({k for r in rows for k, v in r.features.items()
                      if isinstance(v, (int, float)) and not isinstance(v, bool)})
    report = {
        "meta": {**analysis.meta(conn), "split": split.isoformat(),
                 "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        "first_question": analysis.first_question(conn),
        "base_rates_by_week": analysis.base_rates_by_week(rows),
        "segments": analysis.segment_summary(rows),
        "buckets": [analysis.bucket_rates(rows, f) for f in numeric],
        # core = without hollow graduations (no positives, would dominate low buckets)
        "buckets_core": [analysis.bucket_rates([r for r in rows if analysis.segment(r) != "hollow"], f)
                         for f in numeric],
        "model": analysis.out_of_time_model(rows, split_dt, numeric),
        "model_variants": analysis.model_variants(rows, split_dt, numeric),
    }
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(report, indent=2, default=str))
    print(f"wrote {out}")


def status(conn) -> None:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    print(json.dumps({
        "tokens": q("SELECT COUNT(*) FROM tokens"),
        "graduated_from": q("SELECT MIN(graduated_at) FROM tokens"),
        "graduated_to": q("SELECT MAX(graduated_at) FROM tokens"),
        "with_creator": q("SELECT COUNT(*) FROM tokens WHERE creator IS NOT NULL"),
        "rpc_backfill_reached": db.get_state(conn, "rpc_oldest_ts"),
        "collector_heartbeat": json.loads(db.get_state(conn, "collector_heartbeat") or "null"),
        "candles_pending_sampled": len(__import__("runner_research.ingest", fromlist=["x"]).candles_pending(
            conn, datetime.now(timezone.utc))),
        "live_since": db.get_state(conn, "live_since"),
        "cg_demo_calls_this_month": {k: int(db.get_state(conn, f"{k}:{datetime.now(timezone.utc):%Y-%m}") or 0)
                                     for k in demo_budget_keys()},
        "candles_done": q("SELECT COUNT(*) FROM job_runs WHERE job='fr2' AND status='done'"),
        "candles_empty": q("SELECT COUNT(*) FROM job_runs WHERE job='fr2' AND status='done' AND rows=0"),
        "candles_failed": q("SELECT COUNT(*) FROM job_runs WHERE job='fr2' AND status='failed'"),
        "snapshots": dict(conn.execute("SELECT offset, COUNT(*) FROM snapshots GROUP BY 1").fetchall()),
        "trade_features": q("SELECT COUNT(*) FROM features_1h"),
        "labels": dict(conn.execute("SELECT label, COUNT(*) FROM labels GROUP BY 1").fetchall()),
        "jobs": [dict(r) for r in conn.execute(
            "SELECT job, status, COUNT(*) n, SUM(rows) rows FROM job_runs WHERE job != 'fr2' GROUP BY 1, 2")],
    }, indent=2))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="rr", description="Solana runner research pipeline")
    p.add_argument("--db", default=None, help="SQLite path (default $RR_DB_PATH)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create tables")

    c = sub.add_parser("check", help="FR6: fake/duplicate check for a mint or pool")
    c.add_argument("address")

    b = sub.add_parser("backfill-graduations", help="FR1: pump.fun graduations (Solana RPC, or Dune)")
    b.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 1))
    b.add_argument("--end", type=date.fromisoformat, default=None, help="exclusive; default now")
    b.add_argument("--source", choices=("rpc", "dune"), default="rpc")
    b.add_argument("--sample", type=float, default=1.0, help="share of graduations to keep (by signature hash)")
    b.add_argument("--max-pages", type=int, default=None, help="stop after N signature pages (resumable)")
    b.add_argument("--restart", action="store_true", help="forget the resume cursor and rescan from now")
    b.add_argument("--created-at", choices=("auto", "yes", "no"), default="auto",
                   help="fetch creation time (1–3 RPC calls/token); auto = only with a private RPC")

    bc = sub.add_parser("backfill-candles", help="FR2: 5m candles from GeckoTerminal for closed 72h windows")
    bc.add_argument("--sample", type=float, default=1.0, help="multiplies the live/historical samples")
    bc.add_argument("--limit", type=int, default=None)

    col = sub.add_parser("collect", help="FR3: live collector loop (discovery, snapshots, trades, candles)")
    col.add_argument("--once", action="store_true")

    sub.add_parser("status", help="store and backfill progress")
    lb = sub.add_parser("label", help="FR4+FR5: label tokens whose 72h window has closed")
    lb.add_argument("--all", action="store_true", help="relabel every token (default: only unlabeled)")
    em = sub.add_parser("enrich-meta", help="metadata JSON (socials, description) for labeled tokens")
    em.add_argument("--workers", type=int, default=8)
    em.add_argument("--limit", type=int, default=None)
    ins = sub.add_parser("insights", help="trader-facing insights.json for the web page")
    ins.add_argument("--out", default="web/meme-research")
    wp = sub.add_parser("runners-page", help="write the runner list (data.json + index.html) for the web")
    wp.add_argument("--out", default="web/meme-research")
    lt = sub.add_parser("live-test", help="live test of the runner rule: decide at T0+60m, score after 72h")
    lt.add_argument("--once", action="store_true")
    lt.add_argument("--every", type=int, default=30, help="seconds between steps")
    lt.add_argument("--out", default="web/meme-research")
    h = sub.add_parser("health", help="exit 1 if the collector heartbeat is older than --max-age seconds")
    h.add_argument("--max-age", type=int, default=300)

    an = sub.add_parser("analyze", help="FR7: base rates, buckets, first question, OOT model")
    an.add_argument("--split", type=date.fromisoformat, default=date(2026, 10, 1),
                    help="train before, test on/after (default 2026-10-01)")
    an.add_argument("--out", default="reports/analysis.json")

    sub.add_parser("due", help="FR3: snapshots due now")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request is noise
    from .http import install_log_redaction
    install_log_redaction()
    conn = db.connect(a.db)
    utc = lambda d: datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)

    if a.cmd == "init-db":
        print("ok")
    elif a.cmd == "check":
        print(json.dumps(check_address(conn, a.address).as_dict(), indent=2))
    elif a.cmd == "backfill-graduations":
        if a.source == "dune":
            from .backfill import run_graduations
            run_graduations(conn, a.start, (a.end - timedelta(days=1)) if a.end else date.today())
        else:
            from .ingest import scan_backward
            _, rpc, _, _ = clients()
            if a.restart:
                db.set_state(conn, "rpc_oldest_sig", None)
                db.set_state(conn, "rpc_oldest_ts", None)
            created = a.created_at == "yes" or (a.created_at == "auto" and private_rpc())
            n = scan_backward(conn, rpc, utc(a.start), utc(a.end) if a.end else None, a.sample, a.max_pages,
                              with_created_at=created)
            print(f"stored {n} graduations; reached {db.get_state(conn, 'rpc_oldest_ts')}")
    elif a.cmd == "backfill-candles":
        from .ingest import fetch_candles
        _, _, _, gecko = clients()
        print(f"candles for {fetch_candles(conn, gecko, sample=a.sample, limit=a.limit)} tokens")
    elif a.cmd == "collect":
        from .collector import next_wakeup, run_once
        from .config import DISCOVERY_POLL_SEC
        _, rpc, dexs, gecko = clients()

        import threading
        from .config import CG_DEMO_MONTHLY_CAP
        from .gecko import Gecko
        if db.get_state(conn, "live_since") is None:
            db.set_state(conn, "live_since", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        claimed: set[str] = set()
        claim_lock = threading.Lock()

        def claim(mint: str) -> bool:
            with claim_lock:
                if mint in claimed:
                    return False
                claimed.add(mint)
                return True

        def candle_worker(g, budget_key: str | None) -> None:
            """Candle backlog lanes. The public lane shares the Gecko limiter with
            the live steps; the CoinGecko demo lane stops at its monthly cap."""
            from .ingest import fetch_candles
            c2 = db.connect(a.db)
            log = logging.getLogger(__name__)
            while True:
                month_key = f"{budget_key}:{datetime.now(timezone.utc):%Y-%m}" if budget_key else None
                used = int(db.get_state(c2, month_key) or 0) if month_key else 0
                if month_key and used >= CG_DEMO_MONTHLY_CAP:
                    time.sleep(3600)
                    continue
                before = g.calls
                try:
                    n = fetch_candles(c2, g, limit=5, claim=claim)
                except Exception:
                    log.exception("candle worker %s", budget_key or "public")
                    n = 0
                if month_key:
                    db.set_state(c2, month_key, str(int(db.get_state(c2, month_key) or 0) + g.calls - before))
                if n == 0:
                    time.sleep(30)

        threading.Thread(target=candle_worker, args=(gecko, None), name="candles", daemon=True).start()
        # One lane per CoinGecko demo key, each with its own client so the
        # per-key 30/min limit is not shared through the per-host bucket.
        from .gecko import demo_keys
        from .http import ApiClient
        for key, budget in zip(demo_keys(), demo_budget_keys()):
            threading.Thread(target=candle_worker, args=(Gecko(ApiClient(), key=key), budget),
                             name=f"candles-{budget}", daemon=True).start()
        while True:
            now = datetime.now(timezone.utc)
            from .http import redact_url
            print(redact_url(json.dumps({"at": now.isoformat(timespec="seconds"),
                              **run_once(conn, rpc, dexs, gecko, now, candles_per_step=0)})),
                  flush=True)
            if a.once:
                break
            # discovery needs a short poll; snapshot targets can't be missed by more than this
            wake = next_wakeup(conn, now)
            gap = DISCOVERY_POLL_SEC if wake is None else min(DISCOVERY_POLL_SEC, max(1.0, (wake - now).total_seconds()))
            time.sleep(max(0.0, gap - (datetime.now(timezone.utc) - now).total_seconds()))
    elif a.cmd == "label":
        from .pipeline import label_tokens
        print(f"labeled {label_tokens(conn, only_new=not a.all)} tokens")
    elif a.cmd == "analyze":
        run_analysis(conn, a.split, a.out)
    elif a.cmd == "due":
        from .collector import due_snapshots
        for d in due_snapshots(conn, datetime.now(timezone.utc)):
            print(d.mint, d.offset, d.target_at.isoformat())
    elif a.cmd == "status":
        status(conn)
    elif a.cmd == "enrich-meta":
        from .meta import fetch_json, fetch_uris
        _, rpc, _, _ = clients()
        todo = [r[0] for r in conn.execute(
            "SELECT l.mint FROM labels l LEFT JOIN token_meta m USING (mint) WHERE m.mint IS NULL")]
        print(f"uris: {fetch_uris(conn, rpc, todo)}")
        print(f"json: {fetch_json(conn, a.workers, a.limit)}")
    elif a.cmd == "insights":
        from .insights import write
        write(conn, a.out, datetime.now(timezone.utc))
        print(f"wrote {a.out}/insights.json")
    elif a.cmd == "runners-page":
        from .http import ApiClient
        from .webpage import write_page
        print(f"wrote {write_page(conn, ApiClient(), a.out)} runners to {a.out}")
    elif a.cmd == "live-test":
        import httpx
        from . import livetest
        from .http import ApiClient, redact_url
        _, rpc, _, gecko = clients()
        api = ApiClient()
        now = datetime.now(timezone.utc)
        print(f"reserves backfilled: {livetest.backfill_reserves(conn, rpc, now - timedelta(hours=27))}", flush=True)
        prices, priced_at = {}, None
        with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "runner-research"}) as client:
            while True:
                now = datetime.now(timezone.utc)
                out = {"at": now.isoformat(timespec="seconds")}
                for name, fn in (("step", lambda: livetest.step(conn, gecko, rpc, client, now)),
                                 ("scored", lambda: livetest.score(conn, now))):
                    try:
                        out[name] = fn()
                    except Exception as e:
                        logging.getLogger(__name__).exception("live-test %s", name)
                        out[name] = redact_url(f"error: {type(e).__name__}: {e}")[:200]
                if priced_at is None or now - priced_at >= timedelta(minutes=5):
                    try:
                        prices, priced_at = livetest.open_prices(conn, api), now
                    except Exception as e:
                        out["prices"] = redact_url(f"error: {e}")[:200]
                out["signals"] = livetest.write(conn, a.out, now, prices)
                print(json.dumps(out), flush=True)
                if a.once:
                    break
                time.sleep(max(1.0, a.every - (datetime.now(timezone.utc) - now).total_seconds()))
    elif a.cmd == "health":
        hb = db.get_state(conn, "collector_heartbeat")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(json.loads(hb)["at"])).total_seconds() if hb else None
        ok = age is not None and age <= a.max_age
        print(json.dumps({"ok": ok, "heartbeat_age_s": age, "last": json.loads(hb) if hb else None}))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
