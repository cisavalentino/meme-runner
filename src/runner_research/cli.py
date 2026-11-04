"""rr — command line entry point."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from . import db
from .fakecheck import check_address


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

    a = p.parse_args(argv)
    conn = db.connect(a.db)

    if a.cmd == "init-db":
        print("ok")
    elif a.cmd == "check":
        print(json.dumps(check_address(conn, a.address).as_dict(), indent=2))
    elif a.cmd == "backfill-graduations":
        from .backfill import run_graduations
        run_graduations(conn, a.start, a.end)
    elif a.cmd == "status":
        for r in conn.execute("SELECT job, status, COUNT(*) n, SUM(rows) rows FROM job_runs GROUP BY 1, 2"):
            print(dict(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
