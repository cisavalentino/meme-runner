"""Regression tests for the review fixes, label fallbacks, the RPC scanner
and the live collector (with fake clients, no network)."""
import json
from datetime import date, datetime, timedelta, timezone

from conftest import T0, seed_token
from runner_research import analysis, collector, db, ingest
from runner_research.backfill import pending_days
from runner_research.features import Trade, context_features
from runner_research.labels import Candle, grid_t0, label_path
from runner_research.solana import Migration


# ---------------------------------------------------------------- fixes

def test_day_grad_count_has_no_lookahead(conn):
    seed_token(conn, "EARLY", T0, [(0, 1)])
    seed_token(conn, "LATE", T0 + timedelta(hours=5), [(0, 1)])
    assert context_features(conn, "EARLY")["day_grad_count"] == 1
    assert context_features(conn, "LATE")["day_grad_count"] == 2


def test_pending_days_skips_unfinished_day(conn):
    days = pending_days(conn, "fr1", date(2026, 11, 1), date(2026, 11, 5), today=date(2026, 11, 5))
    assert days[-1] == date(2026, 11, 4)


def test_first_question_ignores_tokens_younger_than_24h(conn):
    seed_token(conn, "OLD", T0, [(0, 1), (0.5, 5), (23, 6)])
    seed_token(conn, "YOUNG", T0 + timedelta(days=3), [(0, 1), (0.5, 5)])
    q = analysis.first_question(conn, now=T0 + timedelta(days=3, hours=10))
    assert q["all"]["n"] == 1


def test_model_skips_single_class_training():
    rows = [analysis.Row(f"m{i}", T0 + timedelta(hours=i), "dead", {"x": float(i)}) for i in range(20)]
    assert "skipped" in analysis.out_of_time_model(rows, split=T0 + timedelta(hours=10))


# ---------------------------------------------------------------- labels

def path(points, trades=None, vol=1000.0, start=T0):
    """5m candles from (hour, close) steps; trades None = Gecko-style candles."""
    out = []
    for i in range(72 * 12):
        h = i / 12
        out.append(Candle(start + timedelta(minutes=5 * i), [v for hh, v in points if hh <= h][-1],
                          trades, None, vol))
    return out


def test_rug_from_price_proxy_without_liquidity():
    # 20x then -99.9%: liquidity ~ sqrt(price) falls below 5% of its max
    r = label_path(T0, 1.0, path([(0, 1), (2, 20), (5, 0.01)]))
    assert r.label == "rug" and "proxy" in r.reason
    # -90% is a dump, not a rug under the proxy (sqrt(0.1) = 32% of max liquidity)
    assert label_path(T0, 1.0, path([(0, 1), (2, 20), (5, 2)])).label != "rug"


def test_dead_rule_volume_fallback_and_snapshot_override():
    quiet = path([(0, 1), (8, 6), (23, 5)], trades=None, vol=10.0)  # $120 in hour 24–25
    assert label_path(T0, 1.0, quiet).label == "dead"
    assert label_path(T0, 1.0, quiet, trades_h24=50).label == "slow_climb"
    busy = path([(0, 1), (8, 6), (23, 5)], trades=None, vol=1000.0)
    assert label_path(T0, 1.0, busy).label == "slow_climb"


def test_grid_t0_keeps_graduation_candle():
    t0 = T0 + timedelta(minutes=2, seconds=30)
    assert grid_t0(t0) == T0
    c = [Candle(T0, 1.0)]
    assert label_path(grid_t0(t0), 1.0, c).reason != "no trades in window"


# ---------------------------------------------------------------- RPC scanner (fake)

class FakeRpc:
    """Pretends the migration account has N graduations, one per minute."""

    def __init__(self, n=2500, t_end=T0):
        self.sigs = [{"signature": f"s{i:05d}", "err": None if i % 10 else {"x": 1},
                      "blockTime": int((t_end - timedelta(minutes=i)).timestamp())} for i in range(n)]
        self.calls = {"tx": 0}

    def iter_signatures(self, address, before=None, until=None, stop_before_ts=None):
        i = 0 if before is None else [s["signature"] for s in self.sigs].index(before) + 1
        while i < len(self.sigs):
            page = self.sigs[i:i + 1000]
            if until in [s["signature"] for s in page]:
                yield page[:[s["signature"] for s in page].index(until)]
                return
            if stop_before_ts is not None:
                keep = [s for s in page if s["blockTime"] >= stop_before_ts]
                yield keep
                if len(keep) < len(page):
                    return
            else:
                yield page
            i += 1000

    def signatures(self, address, before=None, until=None, limit=1000):
        return next(self.iter_signatures(address, before, until))

    def transaction(self, sig):
        self.calls["tx"] += 1
        return sig

    def accounts(self, keys, encoding="base64"):
        return [None] * len(keys)

    def oldest_signature_time(self, address, max_pages=3):
        return T0 - timedelta(hours=1)


def fake_parse(tx, sig):
    s = next(x for x in RPC.sigs if x["signature"] == sig)
    return Migration(sig, f"M{sig}", f"P{sig}", f"C{sig}", datetime.fromtimestamp(s["blockTime"], timezone.utc))


RPC = FakeRpc()


def test_scan_backward_resumes_and_respects_start(conn, monkeypatch):
    monkeypatch.setattr(ingest, "parse_migration", fake_parse)
    start = T0 - timedelta(minutes=1999)
    n1 = ingest.scan_backward(conn, RPC, start, max_pages=1)
    assert n1 == 900  # 1000 sigs, 10% failed
    n2 = ingest.scan_backward(conn, RPC, start)
    assert n1 + n2 == conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 1800
    assert ingest.scan_backward(conn, RPC, start) == 0


def test_sampling_is_deterministic():
    keys = [f"k{i}" for i in range(10000)]
    picked = [k for k in keys if ingest.sampled(k, 0.2)]
    assert 1800 < len(picked) < 2200 and picked == [k for k in keys if ingest.sampled(k, 0.2)]


def test_scan_forward_only_new(conn, monkeypatch):
    monkeypatch.setattr(ingest, "parse_migration", fake_parse)
    rpc = FakeRpc(n=50)
    db.set_state(conn, "rpc_newest_sig", "s00020")
    globals()["RPC"] = rpc
    try:
        assert ingest.scan_forward(conn, rpc) == 18  # s00000..s00019, minus 2 failed
    finally:
        globals()["RPC"] = FakeRpc()
    assert db.get_state(conn, "rpc_newest_sig") == "s00000"
    row = conn.execute("SELECT bonding_minutes, source FROM tokens LIMIT 1").fetchone()
    assert row["source"] == "rpc" and row["bonding_minutes"] is not None


# ---------------------------------------------------------------- collector (fake clients)

class FakeDexs:
    def tokens(self, canonical):
        return {m: {"price": 1.0, "liquidity": 5e4, "mcap": 1e6, "volume_usd": 1e5, "txns": 42,
                    "dexs_profile": 1, "dexs_boosts": 0, "_canon": True} for m in canonical}

    def token_pairs(self, mint):
        return [{"pool": f"P_{mint}", "dex": "pumpswap", "quote_mint": "SOL", "created_at": None,
                 "liquidity_usd": 5e4, "volume_24h_usd": 1e5, "txns_24h": 900},
                {"pool": "FAKE", "dex": "meteora", "quote_mint": "SOL", "created_at": None,
                 "liquidity_usd": 100.0, "volume_24h_usd": 1e6, "txns_24h": 20}]


class FakeGecko:
    def __init__(self):
        self.calls = 0

    def token_info(self, mint):
        self.calls += 1
        return {"holders": 300, "top10_pct": 25.0, "dev_pct": 1.0, "mint_auth": "no", "freeze_auth": "no"}

    def trades(self, pool, mint):
        self.calls += 1
        return [(f"{mint}-{i}", Trade(T0 + timedelta(minutes=i), f"w{i % 7}", "buy" if i % 3 else "sell", 10.0))
                for i in range(0, 70, 2)]

    def ohlcv_5m(self, pool, before_ts=None, limit=1000):
        self.calls += 1
        return [{"ts": (T0 + timedelta(minutes=5 * i)).isoformat(timespec="seconds"), "open": 1.0, "high": 1.0,
                 "low": 1.0, "close": 1.0 + i / 100, "volume_usd": 100.0} for i in range(900)]


def test_snapshots_with_holders_and_fake_pools(conn):
    seed_token(conn, "A", T0, [(0, 1)])
    g = FakeGecko()
    assert collector.take_snapshots(conn, FakeDexs(), g, T0 + timedelta(hours=1, minutes=1), detail_sample=1) == 1
    s = conn.execute("SELECT * FROM snapshots WHERE mint='A'").fetchone()
    assert s["offset"] == "1h" and s["holders"] == 300 and s["txns"] == 42
    assert context_features(conn, "A")["top10_pct_1h"] == 25.0
    collector.take_snapshots(conn, FakeDexs(), g, T0 + timedelta(hours=24, minutes=1), detail_sample=1)
    fake = conn.execute("SELECT * FROM pools WHERE pool='FAKE'").fetchone()
    assert fake["fake_flag"] == 1 and "not_canonical_pool" in fake["fake_reason"] and "wash_volume" in fake["fake_reason"]


def test_trade_polling_coverage_and_features(conn):
    seed_token(conn, "A", T0, [(0, 1)], creator="w1")
    g = FakeGecko()
    for m in range(1, 70, 1):
        collector.poll_trades(conn, g, T0 + timedelta(minutes=m), sample=1)
    polls = conn.execute("SELECT * FROM trade_polls WHERE mint='A' ORDER BY polled_at").fetchall()
    assert len(polls) == 7  # 1, 11, ..., 51 and the final poll at 61
    assert collector.finalize_trade_features(conn, T0 + timedelta(minutes=70)) == 1
    f = json.loads(conn.execute("SELECT features FROM features_1h WHERE mint='A'").fetchone()[0])
    assert f["trades_complete"] is True and f["trades_1h"] == 30  # trades at 0..58 min only


def test_first_hour_gap_is_flagged():
    row = lambda at, n, oldest: {"polled_at": (T0 + timedelta(minutes=at)).isoformat(), "n": n,
                                 "oldest": (T0 + timedelta(minutes=oldest)).isoformat()}
    assert collector.first_hour_complete(T0, [row(10, 50, 0), row(61, 200, 5)])
    assert not collector.first_hour_complete(T0, [row(10, 300, 4), row(61, 200, 5)])   # first 4 min lost
    assert not collector.first_hour_complete(T0, [row(10, 50, 0), row(61, 300, 30)])   # 10–30 min lost
    assert not collector.first_hour_complete(T0, [row(10, 50, 0)])                     # no final poll


def test_run_once_end_to_end(conn, monkeypatch):
    monkeypatch.setattr(ingest, "parse_migration", fake_parse)
    rpc = FakeRpc(n=5, t_end=T0)
    globals()["RPC"] = rpc
    try:
        out = collector.run_once(conn, rpc, FakeDexs(), FakeGecko(), T0 + timedelta(seconds=30))
    finally:
        globals()["RPC"] = FakeRpc()
    assert out["discovered"] == 4
    later = T0 + timedelta(hours=73)
    out = collector.run_once(conn, rpc, FakeDexs(), FakeGecko(), later, candles_per_step=10)
    assert out["candles"] == 4
    assert conn.execute("SELECT COUNT(*) FROM candles_5m").fetchone()[0] > 0


def test_label_only_new_skips_labeled(conn):
    from runner_research.pipeline import label_tokens
    seed_token(conn, "A", T0, [(0, 1), (2, 3)])
    now = T0 + timedelta(days=4)
    assert label_tokens(conn, now=now, only_new=True) == 1
    assert label_tokens(conn, now=now, only_new=True) == 0
    assert label_tokens(conn, now=now) == 1


def test_fetch_candles_stops_at_deadline(conn):
    import time
    for i in range(3):
        db.upsert(conn, "tokens", [{"mint": f"M{i}", "graduated_at": T0.isoformat(), "canonical_pool": f"P{i}",
                                    "source": "test"}], key=("mint",))
    later = T0 + timedelta(days=4)
    assert ingest.fetch_candles(conn, FakeGecko(), later, deadline=time.monotonic() - 1) == 0
    assert ingest.fetch_candles(conn, FakeGecko(), later, deadline=time.monotonic() + 60) == 3
    assert ingest.candles_pending(conn, later) == []


def test_rpc_urls(monkeypatch):
    from runner_research import cli
    from runner_research.solana import RPC_URL
    monkeypatch.delenv("RR_SOLANA_RPC", raising=False)
    assert cli.rpc_urls() == [RPC_URL]
    monkeypatch.setenv("RR_SOLANA_RPC", "https://h.example/?api-key=x")
    monkeypatch.setenv("RR_RPC_WORKERS", "4")
    assert cli.rpc_urls() == ["https://h.example/?api-key=x"] * 4


def test_gecko_404_is_empty_and_other_errors_retry_hourly(conn):
    import httpx

    class G404(FakeGecko):
        def __init__(self, code):
            super().__init__()
            self.code = code

        def ohlcv_5m(self, pool, before_ts=None, limit=1000):
            req = httpx.Request("GET", "https://x")
            raise httpx.HTTPStatusError("err", request=req, response=httpx.Response(self.code, request=req))

    db.upsert(conn, "tokens", [{"mint": "N", "graduated_at": T0.isoformat(), "canonical_pool": "PN", "source": "t"},
                               {"mint": "E", "graduated_at": T0.isoformat(), "canonical_pool": "PE", "source": "t"}],
              key=("mint",))
    later = T0 + timedelta(days=4)
    ingest.fetch_candles(conn, G404(404), later, limit=1)   # newest first; both same T0 -> one of them
    st = dict(conn.execute("SELECT day, status FROM job_runs WHERE job='fr2'").fetchall())
    assert list(st.values()) == ["done"]
    wall = datetime.now(timezone.utc)  # job_runs.updated_at is wall-clock time
    ingest.fetch_candles(conn, G404(500), wall)
    assert ingest.candles_pending(conn, wall) == []                        # failed: not retried within the hour
    assert len(ingest.candles_pending(conn, wall + timedelta(hours=2))) == 1
