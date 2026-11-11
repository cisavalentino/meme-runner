import json
import random
from datetime import timedelta

from runner_research import db, early
from test_livetest import T0, add

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
AXIOM = "6Vo3245eszAb5wuqEMw8mGdbfRUdKbHhDHP5LcaGuTAB"
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"


def tx(owner, mint="M", top=(PUMP,), fee_to=None, slot=1, signer=None, got=100.0):
    ins = [{"programId": p} for p in top]
    if fee_to:
        ins.append({"programId": early.SYSTEM, "parsed": {"type": "transfer",
                                                          "info": {"destination": fee_to, "lamports": 10000}}})
    return {"slot": slot, "meta": {
        "preTokenBalances": [], "innerInstructions": [],
        "postTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": owner, "uiTokenAmount": {"uiAmount": got}}]},
        "transaction": {"message": {"accountKeys": [signer or owner, "ata_" + owner], "instructions": ins}}}


def test_classify_terminal_vs_retail():
    assert early.classify(tx("w", top=(AXIOM,)), "w", "M")["pro"]
    assert early.classify(tx("w", fee_to="FeeWallet"), "w", "M")["pro"]           # pump buy + tip/fee
    assert not early.classify(tx("w"), "w", "M")["pro"]                            # pump.fun site
    assert not early.classify(tx("w", top=(JUP,), fee_to="X"), "w", "M")["pro"]   # wallet swap
    assert not early.classify(tx("w", fee_to="ata_w"), "w", "M")["pro"]           # transfer into own token account
    assert early.classify(tx("w", top=(early.SYSTEM,)), "w", "M") is None          # received by transfer
    assert early.classify(tx("w", got=0.0), "w", "M") is None


def test_holder_stats_and_cluster():
    accs = [{"owner": "POOL", "address": "a0", "amount": 500}] + \
           [{"owner": f"h{i}", "address": f"a{i}", "amount": 50 if i < 3 else 1} for i in range(1, 103)]
    s = early.holder_stats(accs, {"POOL"}, "h1")
    assert s["holders"] == 102 and round(s["dev"], 1) == round(50 / 700 * 100, 1)
    pct, n = early.cluster_pct([{"owner": "h1", "amount": 50, "slot": 7, "signer": "h1"},
                                {"owner": "h2", "amount": 50, "slot": 7, "signer": "h2"},
                                {"owner": "h3", "amount": 1, "slot": 9, "signer": "boss"},
                                {"owner": "h4", "amount": 1, "slot": 10, "signer": "boss"},
                                {"owner": "h5", "amount": 1, "slot": 11, "signer": "h5"}], 1000)
    assert n == 4 and pct == 10.2


class FakeRpc:
    def __init__(self, n_holders=150, pro_every=2):
        self.n, self.pro_every, self.calls = n_holders, pro_every, 0

    def call(self, method, params):
        self.calls += 1
        if method == "getTokenAccounts":
            accs = [{"owner": "P_A", "address": "pool_ata", "amount": 200_000_000}] + \
                   [{"owner": f"h{i}", "address": f"ata{i}", "amount": 1_000_000} for i in range(self.n)]
            return {"token_accounts": accs}
        if method == "getSignaturesForAddress":
            return [{"signature": params[0]}]
        if method == "getTokenAccountsByOwner":
            ui = 100.0 if params[1]["mint"] != "A" else 100.0 / (2 * 67.4 / 206_900_000)
            return {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": ui}}}}}}]}
        raise AssertionError(method)

    def transaction(self, sig):
        i = int(sig[3:])
        return tx(f"h{i}", mint="A", top=(AXIOM,) if i % self.pro_every == 0 else (PUMP,), slot=1000 + i)


def test_step_signal_and_reject(conn):
    add(conn, "A")
    db.set_state(conn, "early_since", (T0 - timedelta(hours=1)).isoformat())
    now = T0 + timedelta(seconds=70)
    assert early.step(conn, FakeRpc(pro_every=1), now, random.Random(1)) == {"signal": 1}
    r = conn.execute("SELECT * FROM live_signals WHERE mint = 'A' AND rule = 'e1'").fetchone()
    f = json.loads(r["features"])
    assert f["holders"] == 150 and f["pro"] == 1.0 and round(f["m_now"], 2) == 2.0 and r["entry_price"] > 0
    assert "SINYAL AWAL" in early.signal_message({"mint": "A", "name": "Cat", "symbol": "CAT", "t0": r["t0"]},
                                                 f, None, now)
    conn.execute("DELETE FROM live_signals")
    assert early.step(conn, FakeRpc(pro_every=3), now, random.Random(1)) == {"rejected": 1}
    assert "pro trader < 50%" in conn.execute("SELECT reasons FROM live_signals WHERE rule = 'e1'").fetchone()[0]
    conn.execute("DELETE FROM live_signals")
    assert early.step(conn, FakeRpc(n_holders=80), now, random.Random(1)) == {"rejected": 1}
    conn.execute("DELETE FROM live_signals")
    rpc = FakeRpc()
    assert early.step(conn, rpc, T0 + timedelta(minutes=4), random.Random(1)) == {"rejected": 1} and rpc.calls == 0
