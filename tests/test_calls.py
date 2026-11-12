from datetime import timedelta

from runner_research import bot, calls, db
from test_livetest import T0
from test_trend import FakeApi, pair

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


def test_addresses_from_text_and_links():
    text = f"🚀 ape {MINT} https://pump.fun/coin/{MINT} dexscreener.com/solana/Fwnzg9 {calls.fees.WSOL}"
    assert calls.addresses(text) == [MINT]


def test_record_grade_and_c1(conn):
    p = pair(MINT) | {"baseToken": {"address": MINT, "name": "Cat", "symbol": "CAT"}, "dexId": "pumpswap",
                      "pairCreatedAt": int((T0 - timedelta(days=3)).timestamp() * 1000)}
    api = FakeApi({MINT: p})
    assert calls.record(conn, api, -1001, "Alpha Calls", 10, T0, f"buy {MINT}") == [MINT]
    assert calls.signal_step(conn, api, None, T0 + timedelta(minutes=5))["candidates"] == 0      # one channel only
    calls.record(conn, api, -1002, "Beta Calls", 11, T0 + timedelta(minutes=10), f"{MINT} gem")
    assert calls.signal_step(conn, api, None, T0 + timedelta(minutes=12))["signal"] == 1
    r = conn.execute("SELECT * FROM live_signals WHERE rule = 'c1'").fetchone()
    assert r["entry_price"] == 0.001
    api.pairs[MINT] = p | {"priceUsd": "0.003"}
    for _ in range(3):
        calls.record(conn, api, -1001, "Alpha Calls", 20 + _, T0 + timedelta(minutes=_), f"{MINT}")
    assert calls.update_prices(conn, api, T0 + timedelta(hours=24, minutes=11)) > 0
    st = calls.channel_stats(conn)
    assert st[0]["chat"] == "Alpha Calls" and st[0]["graded"] >= 1
    assert "Alpha Calls" in bot.answer(conn, "/kanal", T0, {})
