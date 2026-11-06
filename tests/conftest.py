from datetime import datetime, timedelta, timezone

import pytest

from runner_research import db

T0 = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def seed_token(conn, mint, t0, closes_by_hour, creator="C", name=None, created_offset_min=-45,
               trades=20, liq=None):
    """closes_by_hour: list of (hour, close); step function over 72h of 5m candles."""
    pool = f"P_{mint}"
    db.upsert(conn, "tokens", [{
        "mint": mint, "name": name or mint, "symbol": (name or mint)[:4], "creator": creator,
        "created_at": (t0 + timedelta(minutes=created_offset_min)).isoformat(),
        "graduated_at": t0.isoformat(), "canonical_pool": pool,
        "bonding_minutes": -created_offset_min, "source": "test"}], key=("mint",))
    pts = sorted(closes_by_hour)
    rows, prev = [], pts[0][1]
    for i in range(72 * 12):
        h = i / 12
        c = [v for hh, v in pts if hh <= h][-1]
        rows.append({"pool": pool, "ts": (t0 + timedelta(minutes=5 * i)).isoformat(), "open": prev,
                     "high": max(prev, c), "low": min(prev, c), "close": c, "volume_usd": 1000.0,
                     "trades": trades, "buyers": 5, "sellers": 5,
                     "liquidity_usd": liq(h) if liq else None, "source": "test"})
        prev = c
    db.upsert(conn, "candles_5m", rows, key=("pool", "ts"))
    return pool


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.set_state(c, "live_since", "2000-01-01T00:00:00+00:00")  # every test token counts as live
    return c
