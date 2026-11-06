"""FR6 — fake pool and duplicate check. Reads only from the local store."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

from .config import DEFAULT_FAKE_RULES, FakePoolRules


def fake_pool_reasons(
    *,
    is_canonical: bool,
    has_launchpad_record: bool,
    volume_24h_usd: float | None,
    liquidity_usd: float | None,
    distinct_traders_24h: int | None,
    rules: FakePoolRules = DEFAULT_FAKE_RULES,
) -> list[str]:
    reasons = []
    if not has_launchpad_record:
        reasons.append("no_launchpad_record")
    elif not is_canonical:
        reasons.append("not_canonical_pool")
    if (
        volume_24h_usd is not None
        and liquidity_usd is not None
        and distinct_traders_24h is not None
        and liquidity_usd > 0
        and volume_24h_usd > rules.vol_to_liq_max * liquidity_usd
        and distinct_traders_24h < rules.min_traders
    ):
        reasons.append("wash_volume")
    return reasons


@dataclass
class CheckResult:
    address: str
    mint: str | None
    pool: str | None
    launchpad_record: bool
    canonical: bool | None
    canonical_pool: str | None
    same_name_copies_7d: int | None
    fake_flag: bool
    fake_reasons: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def same_name_copies(conn: sqlite3.Connection, mint: str, days: int = 7) -> int | None:
    """Earlier tokens with the same name or symbol in the prior `days`. Uses
    creation time when known, else graduation time (RPC backfill has no
    creation time). The store holds graduated tokens only."""
    row = conn.execute("SELECT name, symbol, coalesce(created_at, graduated_at) AS t FROM tokens WHERE mint = ?",
                       (mint,)).fetchone()
    if row is None or row["t"] is None or (row["name"] is None and row["symbol"] is None):
        return None
    start = (datetime.fromisoformat(row["t"]) - timedelta(days=days)).isoformat()
    n = conn.execute(
        """SELECT COUNT(*) FROM tokens
           WHERE mint != ? AND coalesce(created_at, graduated_at) >= ? AND coalesce(created_at, graduated_at) < ?
             AND (lower(name) = lower(?) OR upper(symbol) = upper(?))""",
        (mint, start, row["t"], row["name"], row["symbol"]),
    ).fetchone()[0]
    return n


def check_address(conn: sqlite3.Connection, address: str) -> CheckResult:
    """Accepts a mint or a pool address."""
    pool_row = conn.execute("SELECT * FROM pools WHERE pool = ?", (address,)).fetchone()
    mint = pool_row["mint"] if pool_row else address
    tok = conn.execute("SELECT * FROM tokens WHERE mint = ?", (mint,)).fetchone()

    has_record = tok is not None and tok["graduated_at"] is not None
    canonical_pool = tok["canonical_pool"] if tok else None
    canonical = None
    if pool_row is not None:
        canonical = has_record and canonical_pool == address

    reasons = []
    if pool_row is not None or tok is None:
        reasons = fake_pool_reasons(
            is_canonical=bool(canonical),
            has_launchpad_record=has_record,
            volume_24h_usd=None, liquidity_usd=None, distinct_traders_24h=None,
        )
        if pool_row is not None and pool_row["fake_reason"]:
            reasons += [r for r in pool_row["fake_reason"].split(",") if r and r not in reasons]

    return CheckResult(
        address=address,
        mint=mint if (tok or pool_row) else None,
        pool=address if pool_row else None,
        launchpad_record=has_record,
        canonical=canonical,
        canonical_pool=canonical_pool,
        same_name_copies_7d=same_name_copies(conn, mint) if tok else None,
        fake_flag=bool(reasons),
        fake_reasons=reasons,
    )
