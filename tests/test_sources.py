"""Parsers against recorded responses (tests/fixtures, recorded 2026-11-05)."""
import json
from datetime import datetime, timezone
from pathlib import Path

from runner_research import dexscreener, gecko, solana

FX = Path(__file__).parent / "fixtures"
MINT = "AgNyxjdT17R78aNCbooraENiJysDLNMZbZnPtRzpump"
POOL = "B9j7ThxPu2Rh9chaKBcSbGm4AzEGeasrqncLPNAtAytx"
CURVE = "9FbX84xkeMnn5scp4EwdEbNTmG6wLwLaD9hxuHZP5hDc"
CREATOR = "CyuYeagveunqGc71hBhjEcQnBMzm1ndecUyrK2YCSZW2"


def load(name):
    return json.loads((FX / name).read_text())


def test_parse_migration():
    m = solana.parse_migration(load("rpc_migrate_tx.json"))
    assert (m.mint, m.pool, m.bonding_curve) == (MINT, POOL, CURVE)
    assert m.graduated_at == datetime(2026, 11, 5, 5, 13, 41, tzinfo=timezone.utc)


def test_non_migration_and_failed_tx_are_ignored():
    tx = load("rpc_migrate_tx.json")
    tx["meta"]["logMessages"] = ["Program log: Instruction: Buy"]
    assert solana.parse_migration(tx) is None
    # losing keeper: Migrate succeeds as a no-op without creating the pool
    tx = load("rpc_migrate_tx.json")
    tx["meta"]["logMessages"] = ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
                                 "Program log: Instruction: MigrateV2"]
    assert solana.parse_migration(tx) is None
    tx = load("rpc_migrate_tx.json")
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert solana.parse_migration(tx) is None
    assert solana.parse_migration(None) is None


def test_bonding_curve_creator_and_mint_metadata():
    acc = load("rpc_bonding_curve.json")["result"]["value"][0]
    assert solana.decode_bonding_curve_creator(acc["data"][0]) == CREATOR
    meta = solana.mint_metadata(load("rpc_mint_parsed.json")["value"][0])
    assert meta["name"] == "Stoa" and meta["symbol"] == "STOA" and meta["mint_auth"] is None


def test_b58_roundtrip_known_key():
    assert solana.b58encode(bytes(32)) == "1" * 32


def test_gecko_ohlcv_sorted_oldest_first():
    rows = gecko.parse_ohlcv(load("gt_ohlcv.json"))
    assert len(rows) == 5 and rows[0]["ts"] < rows[-1]["ts"]
    assert rows[0]["ts"] == "2026-11-05T05:10:00+00:00" and rows[0]["volume_usd"] > 0


def test_gecko_trades_side_from_token_view():
    trades = gecko.parse_trades(load("gt_trades.json"), MINT)
    assert len(trades) == 40
    tid, t = trades[0]
    assert tid.startswith("solana_") and t.side == "buy" and t.usd > 0 and t.ts.tzinfo is not None
    assert {tr.side for _, tr in trades} <= {"buy", "sell"}


def test_gecko_token_info_and_pools():
    info = gecko.parse_token_info(load("gt_token_info.json"))
    assert info["creator"] == CREATOR and info["canonical_pool"] == POOL
    assert info["graduated_at"] == "2026-11-05T05:13:41+00:00"
    assert info["holders"] > 0 and 0 < info["top10_pct"] < 100
    pools = gecko.parse_token_pools(load("gt_token_pools.json"))
    assert {p["dex"] for p in pools} == {"pumpswap", "pump-fun"}


def test_dexscreener_tokens_picks_canonical_pair():
    snaps = dexscreener.parse_tokens(load("dexs_tokens.json"), {MINT: POOL})
    s = snaps[MINT]
    assert s["_canon"] and s["liquidity"] > 0 and s["price"] > 0
    assert s["dexs_profile"] == 1 and s["dexs_boosts"] == 0 and isinstance(s["txns"], int)


def test_dexscreener_token_pairs():
    pairs = dexscreener.parse_token_pairs(load("dexs_token_pairs.json"), MINT)
    assert POOL in {p["pool"] for p in pairs} and len(pairs) >= 2


def test_transactions_spread_over_endpoints_keep_order():
    import httpx
    from runner_research.http import ApiClient

    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(req.url.host)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"sig": body["params"][0]}})

    api = ApiClient(transport=httpx.MockTransport(handler), limits={"a.example": 6000, "b.example": 6000})
    rpc = solana.SolanaRpc(api, ["https://a.example", "https://b.example"])
    sigs = [f"s{i}" for i in range(9)]
    assert [t["sig"] for t in rpc.transactions(sigs)] == sigs
    assert set(seen) == {"a.example", "b.example"}


def test_api_key_is_redacted_from_logs_and_errors(caplog):
    import logging
    from runner_research.http import RedactFilter, redact_url
    url = "https://mainnet.helius-rpc.com/?api-key=abc-123&x=1"
    assert redact_url(url) == "https://mainnet.helius-rpc.com/?api-key=***&x=1"
    rec = logging.LogRecord("t", logging.WARNING, __file__, 1, "POST %s -> 429", (url,), None)
    RedactFilter().filter(rec)
    assert "abc-123" not in rec.getMessage()
