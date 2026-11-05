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


def clients():
    from .dexscreener import DexScreener
    from .gecko import Gecko
    from .http import ApiClient
    from .solana import SolanaRpc

    api = ApiClient()
    rpc_url = os.environ.get("RR_SOLANA_RPC")
    rpc = SolanaRpc(api, rpc_url) if rpc_url else SolanaRpc(api)
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
        "buckets": [analysis.bucket_rates(rows, f) for f in numeric],
        "model": analysis.out_of_time_model(rows, split_dt, numeric),
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

    bc = sub.add_parser("backfill-candles", help="FR2: 5m candles from GeckoTerminal for closed 72h windows")
    bc.add_argument("--sample", type=float, default=1.0, help="share of tokens (by mint hash)")
    bc.add_argument("--limit", type=int, default=None)

    col = sub.add_parser("collect", help="FR3: live collector loop (discovery, snapshots, trades, candles)")
    col.add_argument("--once", action="store_true")

    sub.add_parser("status", help="store and backfill progress")
    sub.add_parser("label", help="FR4+FR5: label tokens whose 72h window has closed")

    an = sub.add_parser("analyze", help="FR7: base rates, buckets, first question, OOT model")
    an.add_argument("--split", type=date.fromisoformat, default=date(2026, 10, 1),
                    help="train before, test on/after (default 2026-10-01)")
    an.add_argument("--out", default="reports/analysis.json")

    sub.add_parser("due", help="FR3: snapshots due now")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
            n = scan_backward(conn, rpc, utc(a.start), utc(a.end) if a.end else None, a.sample, a.max_pages)
            print(f"stored {n} graduations; reached {db.get_state(conn, 'rpc_oldest_ts')}")
    elif a.cmd == "backfill-candles":
        from .ingest import fetch_candles
        _, _, _, gecko = clients()
        print(f"candles for {fetch_candles(conn, gecko, sample=a.sample, limit=a.limit)} tokens")
    elif a.cmd == "collect":
        from .collector import next_wakeup, run_once
        from .config import DISCOVERY_POLL_SEC
        _, rpc, dexs, gecko = clients()
        while True:
            now = datetime.now(timezone.utc)
            print(json.dumps({"at": now.isoformat(timespec="seconds"), **run_once(conn, rpc, dexs, gecko, now)}),
                  flush=True)
            if a.once:
                break
            # discovery needs a short poll; snapshot targets can't be missed by more than this
            wake = next_wakeup(conn, now)
            gap = DISCOVERY_POLL_SEC if wake is None else min(DISCOVERY_POLL_SEC, max(1.0, (wake - now).total_seconds()))
            time.sleep(max(0.0, gap - (datetime.now(timezone.utc) - now).total_seconds()))
    elif a.cmd == "label":
        from .pipeline import label_tokens
        print(f"labeled {label_tokens(conn)} tokens")
    elif a.cmd == "analyze":
        run_analysis(conn, a.split, a.out)
    elif a.cmd == "due":
        from .collector import due_snapshots
        for d in due_snapshots(conn, datetime.now(timezone.utc)):
            print(d.mint, d.offset, d.target_at.isoformat())
    elif a.cmd == "status":
        status(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
