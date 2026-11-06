"""FR1 backfill through Dune: one UTC day per query, resumable via job_runs.

Needs a Dune plan that can run queries (a read-only/free account gets 402/403).
The default FR1 path is the Solana RPC scanner in `ingest.py`.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from importlib.resources import files

from .db import norm_ts, now_iso, upsert


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def load_sql(name: str, **params) -> str:
    sql = (files(__package__) / "sql" / name).read_text()
    for k, v in params.items():
        sql = sql.replace("{{" + k + "}}", str(v))
    return sql


def pending_days(conn: sqlite3.Connection, job: str, start: date, end: date,
                 today: date | None = None) -> list[date]:
    """Days not yet done. Days that have not fully elapsed (UTC) are never
    returned: a partial day marked 'done' would never be fetched again."""
    today = today or datetime.now(timezone.utc).date()
    done = {r[0] for r in conn.execute(
        "SELECT day FROM job_runs WHERE job = ? AND status = 'done'", (job,))}
    return [d for d in daterange(start, min(end, today - timedelta(days=1))) if d.isoformat() not in done]


def mark(conn, job: str, day: date, status: str, rows: int | None = None,
         credits: float | None = None, detail: str | None = None) -> None:
    upsert(conn, "job_runs", [{
        "job": job, "day": day.isoformat(), "status": status, "rows": rows,
        "credits": credits, "detail": detail, "updated_at": now_iso(),
    }], key=("job", "day"))


def require_dune_key() -> str:
    key = os.environ.get("DUNE_API_KEY")
    if not key:
        raise SystemExit("DUNE_API_KEY is not set in the environment.")
    return key


def run_graduations(conn: sqlite3.Connection, start: date, end: date) -> None:
    key = require_dune_key()
    from dune_client.client import DuneClient  # optional dependency: pip install -e '.[dune]'

    client = DuneClient(key)
    for day in pending_days(conn, "fr1", start, end):
        mark(conn, "fr1", day, "running")
        try:
            sql = load_sql("fr1_graduations_day.sql", day=day.isoformat())
            res = client.run_sql(query_sql=sql)
            rows = res.result.rows if res.result else []
            upsert(conn, "tokens", [{
                "mint": r["mint"], "name": r.get("name"), "symbol": r.get("symbol"),
                "creator": r.get("creator"), "created_at": norm_ts(r.get("created_at")),
                "graduated_at": norm_ts(r["graduated_at"]), "launchpad": "pumpfun",
                "canonical_pool": r["canonical_pool"], "bonding_minutes": r.get("bonding_minutes"),
                "source": "dune",
            } for r in rows], key=("mint",))
            upsert(conn, "pools", [{
                "pool": r["canonical_pool"], "mint": r["mint"], "dex": "pumpswap",
                "created_at": norm_ts(r["graduated_at"]), "is_canonical": 1, "source": "dune",
            } for r in rows if r.get("canonical_pool")], key=("pool",))
            mark(conn, "fr1", day, "done", rows=len(rows))
        except Exception as e:  # keep going; failed days stay pending for the next run
            mark(conn, "fr1", day, "failed", detail=f"{type(e).__name__}: {e}"[:500])
