"""GeckoTerminal source: 5m OHLCV (FR2), first-hour trades (FR4), holders (FR3).

Uses the public API (api.geckoterminal.com, no key, no monthly cap). The
CoinGecko demo key (GECKOTERMINAL_API_KEY) has a 10k calls/month cap and
cannot use pumpswap `new_pools`, so it is only used when RR_GECKO_USE_KEY=1.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from .features import Trade
from .http import ApiClient

PUBLIC = "https://api.geckoterminal.com/api/v2/networks/solana"
DEMO = "https://api.coingecko.com/api/v3/onchain/networks/solana"


def _f(v) -> float | None:
    return float(v) if v not in (None, "") else None


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_ohlcv(js: dict) -> list[dict]:
    """[ts, o, h, l, c, volume_usd] newest first -> candle rows oldest first.
    Gecko omits intervals without trades; trades/liquidity are not provided."""
    rows = []
    for ts, o, h, l, c, v in js["data"]["attributes"]["ohlcv_list"]:
        rows.append({"ts": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"),
                     "open": o, "high": h, "low": l, "close": c, "volume_usd": v})
    return sorted(rows, key=lambda r: r["ts"])


def parse_trades(js: dict, mint: str) -> list[tuple[str, Trade]]:
    """(trade id, Trade) for swaps of `mint`. Side is from the token's view."""
    out = []
    for d in js.get("data", []):
        a = d["attributes"]
        if a.get("is_anomalous"):
            continue
        if a["to_token_address"] == mint:
            side = "buy"
        elif a["from_token_address"] == mint:
            side = "sell"
        else:
            continue
        out.append((d["id"], Trade(_ts(a["block_timestamp"]), a["tx_from_address"], side,
                                   _f(a["volume_in_usd"]) or 0.0)))
    return out


def parse_token_info(js: dict) -> dict:
    a = js["data"]["attributes"]
    h = a.get("holders") or {}
    dist = h.get("distribution_percentage") or {}
    lp = a.get("launchpad_details") or {}
    return {
        "name": a.get("name"), "symbol": a.get("symbol"),
        "creator": a.get("developer_address"),
        "holders": h.get("count"), "top10_pct": _f(dist.get("top_10")),
        "dev_pct": _f(a.get("developer_holding_percentage")),
        "mint_auth": a.get("mint_authority"), "freeze_auth": a.get("freeze_authority"),
        "graduated_at": _ts(lp["completed_at"]).isoformat(timespec="seconds") if lp.get("completed_at") else None,
        "canonical_pool": lp.get("migrated_destination_pool_address"),
    }


def parse_token_pools(js: dict) -> list[dict]:
    return [{"pool": d["attributes"]["address"], "dex": d["relationships"]["dex"]["data"]["id"],
             "created_at": _ts(d["attributes"]["pool_created_at"]).isoformat(timespec="seconds")
             if d["attributes"].get("pool_created_at") else None,
             "quote_mint": d["relationships"]["quote_token"]["data"]["id"].removeprefix("solana_"),
             "liquidity_usd": _f(d["attributes"].get("reserve_in_usd"))}
            for d in js.get("data", [])]


def demo_keys() -> list[str]:
    """GECKOTERMINAL_API_KEY: one or more CoinGecko demo keys, comma-separated."""
    return [k.strip() for k in os.environ.get("GECKOTERMINAL_API_KEY", "").split(",") if k.strip()]


class Gecko:
    def __init__(self, api: ApiClient, use_key: bool | None = None, key: str | None = None):
        self.api = api
        if key:
            use_key = True
        key = key or next(iter(demo_keys()), None)
        use_key = os.environ.get("RR_GECKO_USE_KEY") == "1" if use_key is None else use_key
        self.base = DEMO if (use_key and key) else PUBLIC
        self.headers = {"x-cg-demo-api-key": key} if (use_key and key) else {}

    calls = 0

    def get(self, path: str, **params) -> dict:
        self.calls += 1
        return self.api.get_json(f"{self.base}/{path}", params=params or None, headers=self.headers)

    def ohlcv_5m(self, pool: str, before_ts: int | None = None, limit: int = 1000) -> list[dict]:
        p = {"aggregate": 5, "limit": limit, "currency": "usd", "token": "base"}
        if before_ts:
            p["before_timestamp"] = before_ts
        return parse_ohlcv(self.get(f"pools/{pool}/ohlcv/minute", **p))

    def trades(self, pool: str, mint: str) -> list[tuple[str, Trade]]:
        return parse_trades(self.get(f"pools/{pool}/trades"), mint)

    def token_info(self, mint: str) -> dict:
        return parse_token_info(self.get(f"tokens/{mint}/info"))

    def token_pools(self, mint: str) -> list[dict]:
        return parse_token_pools(self.get(f"tokens/{mint}/pools"))
