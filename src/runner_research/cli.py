"""rr — command line entry point."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from . import db
from .fakecheck import check_address


def run_analysis(conn, split: date, out: str) -> None:
    from datetime import datetime, timezone
    from pathlib import Path
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rr", description="Solana runner research pipeline")
    p.add_argument("--db", default=None, help="SQLite path (default $RR_DB_PATH)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create tables")

    c = sub.add_parser("check", help="FR6: fake/duplicate check for a mint or pool")
    c.add_argument("address")

    b = sub.add_parser("backfill-graduations", help="FR1: pump.fun graduations from Dune")
    b.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 1))
    b.add_argument("--end", type=date.fromisoformat, default=date.today())

    s = sub.add_parser("status", help="backfill progress per job")

    sub.add_parser("label", help="FR4+FR5: label tokens whose 72h window has closed")

    an = sub.add_parser("analyze", help="FR7: base rates, buckets, first question, OOT model")
    an.add_argument("--split", type=date.fromisoformat, default=date(2026, 10, 1),
                    help="train before, test on/after (default 2026-10-01)")
    an.add_argument("--out", default="reports/analysis.json")

    sub.add_parser("due", help="FR3: snapshots due now")

    a = p.parse_args(argv)
    conn = db.connect(a.db)

    if a.cmd == "init-db":
        print("ok")
    elif a.cmd == "check":
        print(json.dumps(check_address(conn, a.address).as_dict(), indent=2))
    elif a.cmd == "backfill-graduations":
        from .backfill import run_graduations
        run_graduations(conn, a.start, a.end)
    elif a.cmd == "label":
        from .pipeline import label_tokens
        print(f"labeled {label_tokens(conn)} tokens")
    elif a.cmd == "analyze":
        run_analysis(conn, a.split, a.out)
    elif a.cmd == "due":
        from datetime import datetime, timezone
        from .collector import due_snapshots
        for d in due_snapshots(conn, datetime.now(timezone.utc)):
            print(d.mint, d.offset, d.target_at.isoformat())
    elif a.cmd == "status":
        for r in conn.execute("SELECT job, status, COUNT(*) n, SUM(rows) rows FROM job_runs GROUP BY 1, 2"):
            print(dict(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
