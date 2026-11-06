"""Solana RPC source for pump.fun graduations (FR1 without Dune).

Every graduation is a pump.fun `MigrateV2` instruction that touches the
migration account below. Paging `getSignaturesForAddress` over that account
lists graduations (newest first); `getTransaction` gives mint, canonical
PumpSwap pool and bonding curve; the bonding-curve account holds the creator
and the Token-2022 mint holds name/symbol.
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from .http import ApiClient

RPC_URL = "https://api.mainnet-beta.solana.com"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
MIGRATION_ACCOUNT = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
WSOL = "So11111111111111111111111111111111111111112"

# MigrateV2 account order (verified on a Nov 2026 migration, see tests/fixtures)
_IX_MINT, _IX_BONDING_CURVE, _IX_POOL = 2, 4, 10

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, m = divmod(n, 58)
        s = _B58[m] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


@dataclass(frozen=True)
class Migration:
    signature: str
    mint: str
    pool: str
    bonding_curve: str
    graduated_at: datetime


def parse_migration(tx: dict, signature: str | None = None) -> Migration | None:
    """Return the graduation in a transaction, or None if it is not a
    successful pump.fun MigrateV2 into PumpSwap."""
    if not tx or tx.get("meta", {}).get("err") is not None:
        return None
    logs = tx["meta"].get("logMessages") or []
    # Keepers race: a graduation usually has 2–3 successful Migrate txs, but
    # only the winning one creates the PumpSwap pool; the others are no-ops.
    if not any(l.startswith("Program log: Instruction: Migrate") for l in logs) or \
            "Program log: Instruction: CreatePool" not in logs:
        return None
    for ix in tx["transaction"]["message"]["instructions"]:
        acc = ix.get("accounts") or []
        if ix.get("programId") == PUMP_PROGRAM and len(acc) > _IX_POOL and PUMPSWAP_PROGRAM in acc:
            return Migration(
                signature=signature or tx["transaction"]["signatures"][0],
                mint=acc[_IX_MINT], pool=acc[_IX_POOL], bonding_curve=acc[_IX_BONDING_CURVE],
                graduated_at=datetime.fromtimestamp(tx["blockTime"], timezone.utc))
    return None


def decode_bonding_curve_creator(data_b64: str) -> str | None:
    """BondingCurve: 8 discriminator, 5 x u64 reserves/supply, bool complete, creator pubkey."""
    raw = base64.b64decode(data_b64)
    if len(raw) < 81:
        return None
    creator = raw[49:81]
    return None if creator == bytes(32) else b58encode(creator)


def mint_metadata(account: dict | None) -> dict:
    """name/symbol/authorities from a jsonParsed mint (Token-2022 metadata extension)."""
    if not account:
        return {}
    info = (account.get("data") or {}).get("parsed", {}).get("info", {})
    out = {"mint_auth": info.get("mintAuthority"), "freeze_auth": info.get("freezeAuthority")}
    for ext in info.get("extensions") or []:
        if ext.get("extension") == "tokenMetadata":
            st = ext.get("state") or {}
            out["name"], out["symbol"] = st.get("name"), st.get("symbol")
    return out


class SolanaRpc:
    """`urls[0]` serves signature paging and account reads. `transactions()`
    spreads getTransaction over all urls in parallel: rate limits are per
    endpoint (and per IP), so extra workers only help with extra endpoints."""

    def __init__(self, api: ApiClient, url: str | list[str] = RPC_URL):
        self.urls = [url] if isinstance(url, str) else list(url)
        self.api, self.url = api, self.urls[0]

    def call(self, method: str, params: list, url: str | None = None):
        r = self.api.request("POST", url or self.url,
                             json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        d = r.json()
        if "error" in d:
            raise RuntimeError(f"rpc {method}: {d['error']}")
        return d["result"]

    def signatures(self, address: str, before: str | None = None, until: str | None = None,
                   limit: int = 1000) -> list[dict]:
        p: dict = {"limit": limit}
        if before:
            p["before"] = before
        if until:
            p["until"] = until
        return self.call("getSignaturesForAddress", [address, p])

    def iter_signatures(self, address: str, before: str | None = None, until: str | None = None,
                        stop_before_ts: int | None = None) -> Iterator[list[dict]]:
        """Pages newest -> oldest. Stops at `until` or once blockTime < stop_before_ts."""
        while True:
            page = self.signatures(address, before=before, until=until)
            if not page:
                return
            if stop_before_ts is not None:
                keep = [s for s in page if (s.get("blockTime") or 0) >= stop_before_ts]
                yield keep
                if len(keep) < len(page):
                    return
            else:
                yield page
            if len(page) < 1000:
                return
            before = page[-1]["signature"]

    def transaction(self, sig: str, url: str | None = None) -> dict | None:
        return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}],
                         url=url)

    def transactions(self, sigs: list[str]) -> list[dict | None]:
        """Same order as `sigs`; one worker per endpoint, each with its own rate limit."""
        if len(self.urls) == 1 or len(sigs) < 2:
            return [self.transaction(s) for s in sigs]
        from concurrent.futures import ThreadPoolExecutor

        def worker(k: int) -> list[tuple[int, dict | None]]:
            return [(i, self.transaction(sigs[i], self.urls[k])) for i in range(k, len(sigs), len(self.urls))]

        out: list[dict | None] = [None] * len(sigs)
        with ThreadPoolExecutor(len(self.urls)) as ex:
            for part in ex.map(worker, range(len(self.urls))):
                for i, tx in part:
                    out[i] = tx
        return out

    def accounts(self, keys: list[str], encoding: str = "base64") -> list[dict | None]:
        out: list[dict | None] = []
        for i in range(0, len(keys), 100):
            out += self.call("getMultipleAccounts", [keys[i:i + 100], {"encoding": encoding}])["value"]
        return out

    def oldest_signature_time(self, address: str, max_pages: int = 3) -> datetime | None:
        """blockTime of the first transaction touching `address` (bonding-curve
        creation = token creation). None if it needs more than max_pages."""
        before = None
        for _ in range(max_pages):
            page = self.signatures(address, before=before)
            if not page:
                return None
            if len(page) < 1000:
                return datetime.fromtimestamp(page[-1]["blockTime"], timezone.utc)
            before = page[-1]["signature"]
        return None
