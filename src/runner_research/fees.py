"""Fees traders choose to pay vs swap volume (anti fake volume).

Real traders pay priority fees, Jito tips and terminal fees (Axiom, OKX ...,
typically 0.3–1% of the trade); wash-trading bots churn volume while paying
almost nothing. Measured on the pool's recent direct swaps (one Helius
enhanced-transactions call, type=SWAP):
  ratio = sum(network + priority fee + SOL tips/terminal fees) / sum(|pool SOL change|)
Protocol/creator fees are excluded (everyone pays them). Seen 12 Nov 2026:
most tokens 0.3–0.9%; > 10% on sniper tip wars (DihcityCoin 21%, dumped).

MIN_RATIO is a filter (near fee-free volume = suspected wash trading);
HIGH_RATIO only warns. Both are first guesses, judged by the weekly review.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)
WSOL = "So11111111111111111111111111111111111111112"
MIN_RATIO = 0.001        # below: volume is nearly fee-free -> rejected as suspected fake volume
HIGH_RATIO = 0.10        # above: sniper/bot tip war -> warning only


def check(f: dict) -> list[str]:
    r = f.get("fee_ratio")
    return [f"fee trader < {MIN_RATIO * 100:.1f}% volume (curiga volume palsu)"] if r is not None and r < MIN_RATIO else []


def flow_line(f: dict) -> str | None:
    if not f.get("flow_buys"):
        return None
    wave = " · 🧍 <b>gelombang retail</b>" if f.get("retail_share", 0) >= RETAIL_WAVE and f["flow_buys"] >= 8 else ""
    return (f"🛒 {f['flow_buys']} beli terakhir dari {f.get('flow_buyers')} wallet · {f['retail_share'] * 100:.0f}% lewat wallet "
            f"biasa · {f['small_share'] * 100:.0f}% beli &lt;{SMALL_BUY_SOL} SOL{wave}")


def line(f: dict) -> str | None:
    r = f.get("fee_ratio")
    if r is None:
        return None
    tag = " ⚠️ perang tip bot" if r > HIGH_RATIO else " ⚠️ curiga volume palsu" if r < MIN_RATIO else ""
    return f"🧾 Fee trader {r * 100:.2f}% dari volume ({f.get('fee_swaps')} swap){tag}"


def helius_key() -> str | None:
    import os
    import re
    m = re.search(r"api-key=([^&]+)", os.environ.get("RR_SOLANA_RPC", ""))
    return m.group(1) if m else None


def enhanced_fee(t: dict, pool: str, mint: str) -> tuple[float, float] | None:
    """(volume SOL, trader fees SOL) of one direct SOL<->token swap through
    `pool` (Helius enhanced transaction).
    trader fees = network + priority fee + SOL sent to other accounts (Jito
    tips, terminal / bot fees). Protocol and creator fees (WSOL to fee
    accounts) are left out: every trade pays them, wash bots too.
    Multi-hop routes are skipped (SOL moving through another pool)."""
    payer = t.get("feePayer")
    if t.get("transactionError") or not payer or payer == pool:
        return None
    pool_sol, protocol = 0, 0
    own = {payer}
    for a in t.get("accountData") or []:
        for c in a.get("tokenBalanceChanges") or []:
            amt = int(c["rawTokenAmount"]["tokenAmount"])
            if c.get("userAccount") == payer:
                own.add(a["account"])
                if amt and c.get("mint") not in (mint, WSOL):
                    return None                                   # routed through another token
            elif c.get("userAccount") == pool:
                if c.get("mint") == WSOL:
                    pool_sol += amt
            elif c.get("mint") == WSOL and amt:
                if amt < 0:
                    return None                                   # another pool paid out SOL
                protocol += amt
    if not pool_sol or protocol > 0.05 * abs(pool_sol):
        return None                                               # SOL went on to another pool
    wsol_accounts = {a["account"] for a in t.get("accountData") or []
                     if any(c.get("mint") == WSOL for c in a.get("tokenBalanceChanges") or [])}
    tips = sum(a.get("nativeBalanceChange") or 0 for a in t.get("accountData") or []
               if a["account"] not in own and a["account"] not in wsol_accounts and (a.get("nativeBalanceChange") or 0) > 0)
    return abs(pool_sol) / 1e9, ((t.get("fee") or 0) + tips) / 1e9


RETAIL_SOURCES = {"JUPITER", "OKX_DEX_ROUTER", "DFLOW", "PHANTOM", "METAMASK", "COINBASE"}   # wallet swaps
SMALL_BUY_SOL = 0.5
RETAIL_WAVE = 0.4        # share of buys through wallet routes that marks a retail wave (KOL post, news)


def buy_of(t: dict, mint: str) -> tuple[str, float] | None:
    """(buyer, SOL spent) if the fee payer bought `mint` in this swap."""
    payer = t.get("feePayer")
    got = spent = 0
    for a in t.get("accountData") or []:
        tbc = a.get("tokenBalanceChanges") or []
        if a["account"] == payer:
            spent -= a.get("nativeBalanceChange") or 0
        for c in tbc:
            if c.get("userAccount") == payer:
                amt = int(c["rawTokenAmount"]["tokenAmount"])
                if c.get("mint") == mint:
                    got += amt
                elif c.get("mint") == WSOL:
                    spent -= amt
    return (payer, spent / 1e9) if got > 0 and payer else None


def flow(txs: list[dict], mint: str) -> dict:
    """Who is buying right now: unique buyers, share through wallet routes, small buys."""
    buys = [(b, t.get("source")) for t in txs if not t.get("transactionError") and (b := buy_of(t, mint))]
    if not buys:
        return {"flow_buys": 0}
    return {"flow_buys": len(buys), "flow_buyers": len({b[0] for b, _ in buys}),
            "retail_share": sum(src in RETAIL_SOURCES for _, src in buys) / len(buys),
            "small_share": sum(0 < b[1] < SMALL_BUY_SOL for b, _ in buys) / len(buys),
            "flow_span_s": (txs[0].get("timestamp", 0) - txs[-1].get("timestamp", 0)) if txs else None}


def fee_ratio(api, pool: str, mint: str, quote_mint: str | None = WSOL, n: int = 100) -> dict:
    """Fees paid / volume over the pool's recent swaps: one Helius enhanced
    transactions call (type=SWAP drops the many bot transactions that touch a
    busy pool without swapping). Fees paid in other tokens (e.g. USDC terminal
    fees) are not counted, so the ratio is a lower bound."""
    key = helius_key()
    if not key or quote_mint not in (None, WSOL):
        return {}
    js = api.get_json(f"https://api-mainnet.helius-rpc.com/v0/addresses/{pool}/transactions",
                      params={"api-key": key, "type": "SWAP", "limit": n})
    vol = fee = 0.0
    swaps = 0
    for t in js if isinstance(js, list) else []:
        r = enhanced_fee(t, pool, mint)
        if r:
            vol, fee, swaps = vol + r[0], fee + r[1], swaps + 1
    return {"fee_ratio": fee / vol if vol else None, "fee_swaps": swaps, "fee_volume_sol": round(vol, 3),
            "fees_sol": round(fee, 4), **flow(js if isinstance(js, list) else [], mint)}
