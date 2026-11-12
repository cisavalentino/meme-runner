from runner_research import fees

POOL, MINT, W = "POOL", "MINT", fees.WSOL


def tx(payer="P", pool_sol=-25_000_000, payer_sol=24_500_000, tip=100_000, fee=10_000, extra=()):
    acc = [{"account": payer, "nativeBalanceChange": payer_sol, "tokenBalanceChanges": []},
           {"account": "vault", "nativeBalanceChange": pool_sol,
            "tokenBalanceChanges": [{"userAccount": POOL, "mint": W, "rawTokenAmount": {"tokenAmount": str(pool_sol)}}]},
           {"account": "ata", "nativeBalanceChange": 0,
            "tokenBalanceChanges": [{"userAccount": payer, "mint": MINT, "rawTokenAmount": {"tokenAmount": "-5"}}]},
           {"account": "AxiomFee", "nativeBalanceChange": tip, "tokenBalanceChanges": []},
           {"account": "protoFee", "nativeBalanceChange": 100_000,
            "tokenBalanceChanges": [{"userAccount": "proto", "mint": W, "rawTokenAmount": {"tokenAmount": "100000"}}]},
           *extra]
    return {"feePayer": payer, "fee": fee, "accountData": acc}


def test_trader_fees_exclude_protocol_fee():
    vol, f = fees.enhanced_fee(tx(), POOL, MINT)
    assert vol == 0.025 and f == (10_000 + 100_000) / 1e9


def test_multi_hop_is_skipped():
    other_pool = {"account": "v2", "nativeBalanceChange": 0,
                  "tokenBalanceChanges": [{"userAccount": "POOL2", "mint": W, "rawTokenAmount": {"tokenAmount": "-9000000"}}]}
    assert fees.enhanced_fee(tx(extra=(other_pool,)), POOL, MINT) is None


def test_check_and_line():
    assert fees.check({"fee_ratio": 0.0005}) and not fees.check({"fee_ratio": 0.005}) and not fees.check({})
    assert "perang tip" in fees.line({"fee_ratio": 0.2, "fee_swaps": 40})
    assert fees.line({}) is None
