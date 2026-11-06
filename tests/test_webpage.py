import json
from datetime import datetime, timezone

from runner_research import db, webpage

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
SOL = webpage.SOL


def pair(mint, pool, liq, quote=SOL, qname="Wrapped SOL", created=None, h1=(10, 8)):
    return {"baseToken": {"address": mint, "name": "Dog", "symbol": "DOG"}, "pairAddress": pool, "dexId": "pumpswap",
            "quoteToken": {"address": quote, "symbol": "SOL" if quote == SOL else "AMD", "name": qname},
            "priceUsd": "0.001", "liquidity": {"usd": liq}, "marketCap": 1_000_000,
            "volume": {"h24": 5000, "h1": 300}, "txns": {"h1": {"buys": h1[0], "sells": h1[1]}},
            "priceChange": {"h1": -45, "h6": 3, "h24": 20},
            "pairCreatedAt": int(created.timestamp() * 1000) if created else None}


def test_best_pair_is_most_liquid():
    js = [pair("M", "P1", 100), pair("M", "P2", 5000, quote="AMDx", qname="AMD stock"), pair("X", "P3", 1)]
    best = webpage.best_pairs(js)
    assert best["M"]["pairAddress"] == "P2" and best["X"]["pairAddress"] == "P3"


def test_page_rows_and_notes(conn, tmp_path):
    db.upsert(conn, "tokens", [
        {"mint": "M", "name": "Dog", "symbol": "DOG", "graduated_at": "2026-10-01T00:00:00+00:00",
         "canonical_pool": "P1", "source": "t"},
        {"mint": "J", "name": "Etf", "symbol": "ETF", "graduated_at": "2026-10-02T00:00:00+00:00",
         "canonical_pool": "PJ", "source": "t"},
        {"mint": "D", "name": "Dud", "symbol": "DUD", "graduated_at": "2026-10-02T00:00:00+00:00",
         "canonical_pool": "PD", "source": "t"}], key=("mint",))
    lab = lambda m, l, pk: {"mint": m, "rule_version": webpage.config.RULE_VERSION, "label": l, "peak_multiple": pk,
                            "hours_to_peak": 3.0, "close_24h_vs_peak": 0.5,
                            "features": json.dumps({"grad_mcap": 35000.0, "grad_price": 1e-5}), "source": "t"}
    db.upsert(conn, "labels", [lab("M", "runner_other", 9.0), lab("J", "slow_climb", 900.0), lab("D", "dead", 1.0)],
              key=("mint", "rule_version"))
    db.upsert(conn, "candles_5m", [
        {"pool": "PJ", "ts": "2026-10-02T00:00:00+00:00", "open": 1e-5, "high": 1e-2, "low": 1e-5, "close": 9e-3,
         "source": "t"},
        {"pool": "P1", "ts": "2026-10-01T00:00:00+00:00", "open": 1e-5, "high": 3e-5, "low": 1e-5, "close": 2e-5,
         "source": "t"}], key=("pool", "ts"))
    toks = webpage.runners(conn)
    assert [t["mint"] for t in toks] == ["J", "M"]                       # runners only, by peak
    pairs = webpage.best_pairs([pair("M", "P2", 5000, quote="AMDx", qname="AMD stock", created=NOW, h1=(1, 1))])
    rows = {r["token_address"]: r for r in webpage.build_rows(toks, pairs, NOW)}
    m, j = rows["M"], rows["J"]
    assert m["pair_address"] == "P2" and m["quote"] == "AMD (AMD stock)" and m["txns_1h_buys"] == 1
    assert "quote is AMD stock" in m["note"] and "only 2 txns" in m["note"] and "dumping" in m["note"]
    assert "~1h old" in m["note"] and m["peak_mult_72h"] == 9.0 and m["label"] == "runner_other"
    assert j["pair_address"] == "PJ" and "not listed" in j["note"] and "900x inside one 5m candle" in j["note"]
    assert set(webpage.COLUMNS) <= set(m)
