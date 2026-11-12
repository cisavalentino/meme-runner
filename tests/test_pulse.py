from datetime import datetime, timezone

from runner_research import db, pulse


def pair(mint, v1=20_000, v24=40_000, liq=30_000):
    return {"baseToken": {"address": mint}, "pairAddress": "P" + mint, "dexId": "pumpswap", "priceUsd": "0.001",
            "marketCap": 1e6, "liquidity": {"usd": liq}, "volume": {"m5": 5000, "h1": v1, "h6": v1, "h24": v24},
            "txns": {"m5": {"buys": 10, "sells": 5}, "h1": {"buys": 100, "sells": 60}},
            "priceChange": {"m5": 5, "h1": 40}, "pairCreatedAt": 1_700_000_000_000}


def test_step_stores_busy_tokens_and_deep_checks_within_budget(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "t.sqlite"))
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(pulse, "universe", lambda c, n: {"A": "grad24h", "B": "alive", "C": "trending"})
    monkeypatch.setattr(pulse, "fetch_pairs", lambda api, ms: {"A": pair("A"), "B": pair("B", v1=1000),
                                                               "C": pair("C", v1=50_000, v24=60_000)})
    calls = []
    monkeypatch.setattr(pulse, "deep", lambda c, api, rpc, r, p: calls.append(r["mint"]) or {"holders": 321, "top10": 12.0})
    monkeypatch.setattr(pulse, "DEEP_PER_DAY", 1)
    out = pulse.step(conn, None, object(), now)
    assert out["rows"] == 2                        # B is below MIN_VOL1H
    assert calls == ["C"]                          # hottest first, budget 1
    rows = {r["mint"]: r for r in conn.execute("SELECT * FROM pulse")}
    assert rows["C"]["holders"] == 321 and rows["A"]["holders"] is None
    assert pulse.step(conn, None, object(), now.replace(minute=5))["deep"] == 0   # budget used up


def test_heat_prefers_sudden_volume():
    quiet = pulse.row_of("x", "s", pair("x", v1=10_000, v24=240_000), "2026-11-12T00:00:00+00:00")
    sudden = pulse.row_of("y", "s", pair("y", v1=10_000, v24=12_000), "2026-11-12T00:00:00+00:00")
    assert pulse.heat(sudden) > pulse.heat(quiet)
