"""FR3 — live collector scheduling. Decides which snapshots are due and
records lateness. API fetchers/parsers are added once recorded responses
exist (network to DexScreener/GeckoTerminal is required for that)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import SNAPSHOT_OFFSETS_MIN

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
