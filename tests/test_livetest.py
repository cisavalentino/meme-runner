import json
from datetime import datetime, timedelta, timezone

import httpx

from runner_research import db, livetest as L, strategy as S
from runner_research.solana import WSOL

T0 = datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc)
SOL = 150.0
GN = 67.4 / 206_900_000            # first pool price in SOL -> ~$48.9k mcap


def add(conn, mint, name="Cat Coin", t0=T0, bonding=12.0, base=206_900_000, quote=67.4,
        m15=1.1, m60=2.0, twitter="https://x.com/i/status/1", uri="https://ipfs.io/ipfs/bafkx"):
    pool = f"P_{mint}"
    db.upsert(conn, "tokens", [{"mint": mint, "name": name, "symbol": name.split()[0].upper(),
                                "graduated_at": t0.isoformat(), "canonical_pool": pool, "bonding_minutes": bonding,
                                "source": "test"}], key=("mint",))
    db.upsert(conn, "pools", [{"pool": pool, "mint": mint, "quote_mint": WSOL, "pool_base": base,
                               "pool_quote": quote, "is_canonical": 1, "source": "test"}], key=("pool",))
    db.upsert(conn, "token_meta", [{"mint": mint, "uri": uri, "status": "ok", "twitter": twitter,
                                    "community": None, "fetched_at": t0.isoformat()}], key=("mint",))
    for off, m, mins in (("15m", m15, 15), ("1h", m60, 60)):
        if m is None:
            continue
        nat = GN * m
        db.upsert(conn, "snapshots", [{"mint": mint, "offset": off, "price": nat * SOL, "price_native": nat,
                                       "target_at": (t0 + timedelta(minutes=mins)).isoformat(),
                                       "taken_at": (t0 + timedelta(minutes=mins)).isoformat(), "source": "test"}],
                  key=("mint", "offset"))
    return pool


class FakeGecko:
    def __init__(self, jump=1.3):
        self.jump, self.calls = jump, 0

    def ohlcv_5m(self, pool, limit=100):
        self.calls += 1
        return [{"ts": T0 + timedelta(minutes=5 * i), "open": 1.0, "close": self.jump if i == 3 else 1.0}
                for i in range(14)]


def run(conn, now, gecko=None):
    db.set_state(conn, "livetest_since", (T0 - timedelta(hours=1)).isoformat())
    with httpx.Client() as client:
        return L.step(conn, gecko or FakeGecko(), None, client, now)


def row(conn, mint):
    r = conn.execute("SELECT * FROM live_signals WHERE mint = ?", (mint,)).fetchone()
    return r and {**dict(r), "reasons": json.loads(r["reasons"]), "warnings": json.loads(r["warnings"]),
                  "features": json.loads(r["features"])}


def test_signal_uses_pool_price_not_first_candle(conn):
    add(conn, "A")
    g = FakeGecko()
    run(conn, T0 + timedelta(minutes=62), g)
    r = row(conn, "A")
    assert r["status"] == "signal" and r["reasons"] == [] and g.calls == 1
    f = r["features"]
    assert abs(f["grad_mcap"] - GN * SOL * 1e9) < 1 and abs(f["m15"] - 1.1) < 1e-9 and abs(f["m60"] - 2.0) < 1e-9
    assert abs(r["entry_price"] - GN * 2.0 * SOL) < 1e-12


def test_missing_x_is_a_warning_not_a_rejection(conn):
    add(conn, "B", twitter=None)
    run(conn, T0 + timedelta(minutes=62))
    r = row(conn, "B")
    assert r["status"] == "signal" and r["warnings"] == ["tanpa link X"]
    assert S.evaluate({**r["features"], "max_jump_1h": 1.0}, S.R1)[1] == ["tanpa link X"]   # r1 would reject


def test_pending_then_final(conn):
    add(conn, "C", m15=None, m60=None)
    run(conn, T0 + timedelta(minutes=5))
    assert row(conn, "C")["status"] == "pending"
    add(conn, "C", m15=0.5, m60=None)                        # 15m snapshot lands: fails at once
    run(conn, T0 + timedelta(minutes=16))
    r = row(conn, "C")
    assert r["status"] == "rejected" and r["reasons"] == ["15m < 0.8x"]


def test_rejections(conn):
    add(conn, "H", quote=0.01)                                # hollow: dust in the pool
    add(conn, "I", bonding=0.0)
    add(conn, "J", m60=7.0)
    add(conn, "K", uri="https://gateway.irys.xyz/abc")
    g = FakeGecko(jump=30)
    add(conn, "L")
    run(conn, T0 + timedelta(minutes=62), g)
    assert "graduation kosong" in row(conn, "H")["reasons"]
    assert row(conn, "I")["reasons"] == ["graduation instan"]
    assert row(conn, "J")["reasons"] == ["60m di luar 1.5–5.0x"]
    assert row(conn, "K")["reasons"] == ["metadata di Irys"]
    assert row(conn, "L")["status"] == "rejected" and row(conn, "L")["reasons"][0].startswith("lonjakan 30x")


def test_hot_copy_from_recent_tokens(conn):
    add(conn, "OLD", name="Moonfrog", t0=T0 - timedelta(hours=5), m60=4.0)   # ran 3x+ at its own 1h
    add(conn, "NEW", name="Moonfrog Jr")
    run(conn, T0 + timedelta(minutes=62))
    r = row(conn, "NEW")
    assert r["status"] == "rejected" and r["reasons"] == ["meniru narasi panas"]
    assert r["features"]["hot_words"] == ["moonfrog"]


def test_score_and_page(conn, tmp_path):
    pool = add(conn, "A")
    run(conn, T0 + timedelta(minutes=62))
    entry = row(conn, "A")["entry_price"]
    rows = [{"pool": pool, "ts": (T0 + timedelta(minutes=5 * i)).isoformat(), "open": entry, "high": entry,
             "low": entry, "close": entry * (3.0 if i == 100 else 1.0), "source": "test"} for i in range(864)]
    db.upsert(conn, "candles_5m", rows, key=("pool", "ts"))
    assert L.score(conn, T0 + timedelta(hours=72, minutes=10)) == 0          # too early
    assert L.score(conn, T0 + timedelta(hours=73)) == 1
    db.upsert(conn, "labels", [{"mint": "A", "rule_version": "v0.2", "label": "runner_other", "source": "t"}],
              key=("mint", "rule_version"))
    L.score(conn, T0 + timedelta(hours=74))
    r = row(conn, "A")
    o = json.loads(r["outcome"])
    assert r["status"] == "scored" and abs(o["max_mult"] - 3.0) < 1e-9 and r["label"] == "runner_other"
    L.write(conn, tmp_path, T0 + timedelta(hours=74), {})
    js = json.loads((tmp_path / "live.json").read_text())
    assert js["summary"]["signals"] == 1 and js["summary"]["hit2"] == 1.0 and js["summary"]["runner_label"] == 1.0
    assert (tmp_path / "live.html").exists()
