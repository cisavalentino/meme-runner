import json
from datetime import timedelta

from runner_research import db, trend
from test_livetest import T0, add

NOW = T0 + timedelta(days=5)


def pair(mint, vol1h=60_000, vol24=80_000, chg=120, liq=40_000, buys=500, sells=300):
    return {"baseToken": {"address": mint}, "pairAddress": "PAIR_" + mint, "priceUsd": "0.001", "marketCap": 1_000_000,
            "liquidity": {"usd": liq}, "volume": {"h1": vol1h, "h24": vol24}, "priceChange": {"h1": chg, "m5": 5},
            "txns": {"h1": {"buys": buys, "sells": sells}}}


def test_revival_reasons():
    assert trend.revival_reasons(pair("A"), T0, NOW) == []
    assert "umur < 1 hari" in trend.revival_reasons(pair("A"), NOW - timedelta(hours=2), NOW)
    assert "volume 1j < 5x rata-rata" in trend.revival_reasons(pair("A", vol24=1_000_000), T0, NOW)
    assert "harga 1j di luar +50–500%" in trend.revival_reasons(pair("A", chg=900), T0, NOW)
    assert "beli ≤ jual (1j)" in trend.revival_reasons(pair("A", buys=10, sells=20), T0, NOW)


def test_words_keep_cjk():
    assert trend._words("ni hao cat", "mihao") == {"hao", "cat", "mihao"}
    assert "咪好" in trend._words("咪好", "")


class FakeApi:
    def __init__(self, pairs, trending=()):
        self.pairs, self.trending = pairs, trending

    def get_json(self, url, **kw):
        if "trending_pools" in url:
            return {"data": [{"relationships": {"base_token": {"data": {"id": "solana_" + m}}}} for m in self.trending]}
        if "token-boosts" in url:
            return []
        mints = url.rsplit("/", 1)[1].split(",")
        return [self.pairs[m] for m in mints if m in self.pairs]

    def request(self, method, url, **kw):
        class R:
            text = "<rss><channel></channel></rss>"
        return R()


def test_revival_from_trending_signals_once(conn):
    add(conn, "OLD")
    api = FakeApi({"OLD": pair("OLD")}, trending=["OLD", "UNKNOWN"])
    out = trend.revival_step(conn, api, None, NOW)
    assert out["signal"] == 1 and out["trending"] == 1
    r = conn.execute("SELECT * FROM live_signals WHERE rule = 'w1'").fetchone()
    f = json.loads(r["features"])
    assert r["entry_price"] == 0.001 and f["source"] == "trending" and f["pair"] == "PAIR_OLD"
    assert trend.revival_step(conn, api, None, NOW)["signal"] == 0
    msg = trend.signal_message("w1", {"mint": "OLD", "name": "Cat", "symbol": "CAT"}, f, None)
    assert "KEBANGKITAN" in msg and "Vol1j $60.0K" in msg


def test_narrative_picks_leader_not_copies(conn):
    add(conn, "OG", name="Mihao Cat", t0=T0)
    for i in range(5):
        add(conn, f"C{i}", name=f"mihao copy{i}", t0=NOW - timedelta(minutes=30 + i))
    assert trend.spiking_words(conn, NOW)[0][0] == "mihao"
    pairs = {"OG": pair("OG", vol1h=200_000)} | {f"C{i}": pair(f"C{i}", vol1h=20_000) for i in range(5)}
    out = trend.narrative_step(conn, FakeApi(pairs), None, NOW)
    assert out["signal"] == 1
    r = conn.execute("SELECT mint, features FROM live_signals WHERE rule = 'n1'").fetchone()
    assert r["mint"] == "OG" and json.loads(r["features"])["word"] == "mihao"
    assert trend.narrative_step(conn, FakeApi(pairs), None, NOW + timedelta(minutes=5))["words"] == 0   # cooldown


def test_score_from_ohlcv(conn):
    add(conn, "OLD")
    trend.revival_step(conn, FakeApi({"OLD": pair("OLD")}, trending=["OLD"]), None, NOW)

    class G:
        def ohlcv_5m(self, pool, before_ts=None):
            assert pool == "PAIR_OLD"
            return [{"ts": (NOW + timedelta(minutes=5 * i)).isoformat(), "open": 0.001, "close": 0.001 * (1 + i / 10)}
                    for i in range(30)]
    assert trend.score(conn, G(), NOW + timedelta(hours=73)) == 1
    o = json.loads(conn.execute("SELECT outcome FROM live_signals WHERE rule = 'w1'").fetchone()[0])
    assert o["max_mult"] > 3


def test_quota_skips_without_rejecting(conn, monkeypatch):
    add(conn, "OLD")

    def no_quota(*a, **k):
        raise trend.QuotaExhausted
    monkeypatch.setattr(trend, "holder_check", no_quota)
    api = FakeApi({"OLD": pair("OLD")}, trending=["OLD"])
    out = trend.revival_step(conn, api, object(), NOW)
    assert out["quota"] and conn.execute("SELECT COUNT(*) FROM live_signals").fetchone()[0] == 0
    monkeypatch.setattr(trend, "holder_check", lambda *a, **k: ({"top10": 50}, ["top10 > 30%"]))
    assert trend.revival_step(conn, api, object(), NOW)["signal"] == 0
    calls = []
    monkeypatch.setattr(trend, "holder_check", lambda *a, **k: calls.append(1) or ({}, []))
    trend.revival_step(conn, api, object(), NOW + timedelta(hours=1))          # rejected recently: not rechecked
    assert not calls
    assert trend.revival_step(conn, api, object(), NOW + timedelta(hours=7))["signal"] == 1 and calls
