"""DexScreener source (https://docs.dexscreener.com/api/reference).

`/tokens/v1/solana/{up to 30 mints}` (300 req/min) gives price, liquidity,
mcap, volume, txns, profile (`info`) and active boosts per pair, so one call
covers 30 snapshot targets. `/token-pairs/v1` lists every pool of a mint (FR6).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .http import ApiClient

BASE = "https://api.dexscreener.com"
BATCH = 30


def _txns(p: dict, window: str) -> int | None:
    t = (p.get("txns") or {}).get(window)
    return (t.get("buys", 0) + t.get("sells", 0)) if t else None


def snapshot_from_pair(p: dict) -> dict:
    """Snapshot columns from one pair. `txns` is the last hour (txns.h1, used
    for the dead rule at 25h); `volume_usd` is the last 24h."""
    return {
        "price": float(p["priceUsd"]) if p.get("priceUsd") else None,
        "price_native": float(p["priceNative"]) if p.get("priceNative") else None,
        "liquidity": (p.get("liquidity") or {}).get("usd"),
        "mcap": p.get("marketCap") or p.get("fdv"),
        "volume_usd": (p.get("volume") or {}).get("h24"),
        "txns": _txns(p, "h1"),
        "dexs_profile": int(bool(p.get("info"))),
        "dexs_boosts": int((p.get("boosts") or {}).get("active") or 0),
    }


def parse_tokens(js: list[dict], canonical: dict[str, str]) -> dict[str, dict]:
    """mint -> snapshot for its canonical pool; falls back to the most liquid
    pair with the mint as base token if the canonical pool is not listed."""
    by_mint: dict[str, dict] = {}
    for p in js or []:
        mint = p.get("baseToken", {}).get("address")
        if mint not in canonical:
            continue
        cur = by_mint.get(mint)
        is_canon = p.get("pairAddress") == canonical[mint]
        if cur is None or is_canon or (not cur["_canon"] and
                                       ((p.get("liquidity") or {}).get("usd") or 0) > (cur["liquidity"] or 0)):
            by_mint[mint] = {**snapshot_from_pair(p), "_canon": is_canon}
    return by_mint


def parse_token_pairs(js: list[dict], mint: str) -> list[dict]:
    out = []
    for p in js or []:
        if p.get("baseToken", {}).get("address") != mint:
            continue
        created = p.get("pairCreatedAt")
        out.append({"pool": p["pairAddress"], "dex": p.get("dexId"),
                    "quote_mint": p.get("quoteToken", {}).get("address"),
                    "created_at": datetime.fromtimestamp(created / 1000, timezone.utc).isoformat(timespec="seconds")
                    if created else None,
                    "liquidity_usd": (p.get("liquidity") or {}).get("usd"),
                    "volume_24h_usd": (p.get("volume") or {}).get("h24"),
                    "txns_24h": _txns(p, "h24")})
    return out


class DexScreener:
    def __init__(self, api: ApiClient):
        self.api = api

    def tokens(self, canonical: dict[str, str]) -> dict[str, dict]:
        """canonical: mint -> canonical pool. Batches of 30."""
        mints, out = list(canonical), {}
        for i in range(0, len(mints), BATCH):
            js = self.api.get_json(f"{BASE}/tokens/v1/solana/{','.join(mints[i:i + BATCH])}")
            out.update(parse_tokens(js, canonical))
        return out

    def token_pairs(self, mint: str) -> list[dict]:
        return parse_token_pairs(self.api.get_json(f"{BASE}/token-pairs/v1/solana/{mint}"), mint)
