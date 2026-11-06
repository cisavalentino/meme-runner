from datetime import datetime, timedelta, timezone

from runner_research import insights, meta

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_meta_helpers():
    assert meta.twitter_kind("") == "none"
    assert meta.twitter_kind("https://x.com/i/communities/123") == "community"
    assert meta.twitter_kind("https://twitter.com/a/status/1") == "tweet"
    assert meta.twitter_kind("https://x.com/abc") == "profile"
    assert meta.twitter_kind("https://t.me/abc") == "other"
    urls = meta.gateway_urls("https://ipfs.io/ipfs/QmNSZuhqS19NdJEpsF2BsHMeAqdJy62GgGziZAxRRFWXdb", start=1)
    assert len(urls) == len(meta.IPFS_GATEWAYS) and urls[0].startswith(meta.IPFS_GATEWAYS[1])
    assert meta.gateway_urls("https://metadata.j7tracker.io/x.json") == ["https://metadata.j7tracker.io/x.json"]
    acc = {"data": {"parsed": {"info": {"extensions": [{"extension": "tokenMetadata", "state": {"uri": "u"}}]}}}}
    assert meta.uri_from_account(acc) == "u" and meta.uri_from_account(None) is None
    m = meta.parse_meta({"twitter": " https://x.com/a ", "website": "", "description": "d"})
    assert m["twitter"] == "https://x.com/a" and m["website"] is None and m["description"] == "d"


def test_segments_and_tools():
    assert insights.segment_of({"grad_mcap": 50}) == "kosong"
    assert insights.segment_of({"grad_mcap": 35000, "bonding_minutes": 0}) == "instan"
    assert insights.segment_of({"grad_mcap": 35000, "bonding_minutes": 12}) == "normal"
    assert insights.tool_of("https://ipfs.io/ipfs/Qm") == "pump.fun (bawaan)"
    assert insights.tool_of("https://metadata.j7tracker.io/a") == "J7 Tracker"
    assert insights.tool_of("https://foo.xyz/a") == "host lain" and insights.tool_of(None) == "tanpa uri"


def rows(closes, opens=None):
    opens = opens or [closes[0]] + closes[:-1]
    return [(T0 + timedelta(minutes=5 * i), o, c, 100.0) for i, (o, c) in enumerate(zip(opens, closes))]


def test_path_stats_rug_and_stops():
    # 1 -> 4 (peak) -> 1.5 (stop 50% hits) -> 0.001 (rug, one candle from 1.5/4 = 37% of max)
    p = insights.path_stats(T0, rows([1.0, 4.0, 1.5, 0.001]))
    assert p["peak_mult"] == 4.0 and abs(p["rug_h"] - 15 / 60) < 1e-9
    assert abs(p["rug_prev_frac"] - 1.5 / 4) < 1e-9
    h, m = p["stops"][0.5]
    assert abs(h - 10 / 60) < 1e-9 and m == 1.5
    # instant crash from the top: one-candle rug
    q = insights.path_stats(T0, rows([1.0, 3.0, 0.001]))
    assert q["rug_prev_frac"] == 1.0 and q["stops"][0.5][0] == q["rug_h"]


def test_jump_only_counts_moves_to_a_new_level():
    dust_bounce = insights.path_stats(T0, rows([1.0, 1e-6, 1e-4], opens=[1.0, 1.0, 1e-6]))
    assert dust_bounce["max_jump"] == 0.0
    injected = insights.path_stats(T0, rows([1.0, 80.0], opens=[1.0, 1.0]))
    assert injected["max_jump"] == 80.0


def test_bucket_labels_use_decimal_comma():
    toks = [insights.Tok("m", T0, "dead", "normal", {"x": 0.2})]
    out = insights.bucketed(toks, lambda t: t.feats["x"], lambda t: 0, [0.1, 0.3], lambda lo, hi: f"{lo}–{hi}x")
    assert out[0]["group"] == "0,1–0,3x"
    assert insights.usd(5000) == "$5 rb" and insights.usd(100) == "$100"
