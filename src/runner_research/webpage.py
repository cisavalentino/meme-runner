"""Runner list for the web: labeled runners + a live DexScreener snapshot.

`rr runners-page --out web/meme-research` writes data.json and index.html; a
static server (pm2 `rr-web`) serves `web/` so the page lives at /meme-research/.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .dexscreener import BASE, BATCH
from .http import ApiClient

RUNNER_LABELS = ("runner_other", "slow_climb", "second_wave")
SOL = "So11111111111111111111111111111111111111112"

COLUMNS = ["snapshot_utc", "name", "symbol", "token_address", "pair_address", "dex", "quote", "price_usd",
           "liquidity_usd", "mcap_usd", "vol_24h", "vol_1h", "txns_1h_buys", "txns_1h_sells", "chg_1h", "chg_6h",
           "chg_24h", "note"]
RESEARCH = ["label", "graduated_utc", "grad_mcap_usd", "peak_mult_72h", "hours_to_peak", "close24h_vs_peak",
            "max_5m_jump"]
# A >= 50x move inside one 5m candle that then holds flat is a one-sided pool
# injection (often ETF/stock-named tokens), not an organic run.
JUMP_FLAG = 50.0


def runners(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    q = f"""SELECT t.mint, t.name, t.symbol, t.canonical_pool, t.graduated_at, l.label, l.peak_multiple,
                   l.hours_to_peak, l.close_24h_vs_peak, json_extract(l.features, '$.grad_mcap') grad_mcap,
                   (SELECT MAX(c.close / c.open) FROM candles_5m c WHERE c.pool = t.canonical_pool AND c.open > 0)
                   AS max_jump
            FROM labels l JOIN tokens t USING (mint)
            WHERE l.rule_version = ? AND l.label IN ({','.join('?' * len(RUNNER_LABELS))})
            ORDER BY l.peak_multiple DESC"""
    return conn.execute(q, (config.RULE_VERSION, *RUNNER_LABELS)).fetchall()


def best_pairs(js: list[dict]) -> dict[str, dict]:
    """mint -> its most liquid pair with the mint as base (what DexScreener shows)."""
    out: dict[str, dict] = {}
    for p in js or []:
        mint = (p.get("baseToken") or {}).get("address")
        liq = (p.get("liquidity") or {}).get("usd") or 0
        if mint and (mint not in out or liq > ((out[mint].get("liquidity") or {}).get("usd") or 0)):
            out[mint] = p
    return out


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def note_for(p: dict | None, row: dict, now: datetime) -> str:
    notes = []
    if (row.get("max_5m_jump") or 0) >= JUMP_FLAG:
        notes.append(f"{row['max_5m_jump']:.0f}x inside one 5m candle (pool injection, not organic)")
    if p is None:
        return "; ".join(notes + ["not listed on DexScreener (pool closed or never indexed)"])
    q = p.get("quoteToken") or {}
    if q.get("address") and q["address"] != SOL:
        notes.append(f"quote is {q.get('name') or q.get('symbol')}")
    if (row["liquidity_usd"] or 0) < 1:
        notes.append("pool emptied; mcap is stale")
    n1 = (row["txns_1h_buys"] or 0) + (row["txns_1h_sells"] or 0)
    if n1 < 5 and (row["liquidity_usd"] or 0) >= 1:
        notes.append(f"only {n1} txns in last 1h")
    if (row["chg_24h"] or 0) <= -50 or (row["chg_1h"] or 0) <= -40:
        notes.append("dumping")
    created = p.get("pairCreatedAt")
    if created:
        age_h = (now.timestamp() - created / 1000) / 3600
        if age_h < 24:
            notes.append(f"~{max(1, round(age_h))}h old")
    gm = row.get("grad_mcap_usd")
    if gm is not None and (gm < config.HOLLOW_MCAP_USD or gm > 1e6):
        notes.append("graduation price looks off (check candles)")
    return "; ".join(notes)


def build_rows(toks: list, pairs: dict[str, dict], now: datetime) -> list[dict]:
    snap = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for t in toks:
        p = pairs.get(t["mint"])
        tx = ((p or {}).get("txns") or {}).get("h1") or {}
        vol = (p or {}).get("volume") or {}
        chg = (p or {}).get("priceChange") or {}
        q = (p or {}).get("quoteToken") or {}
        r = {
            "snapshot_utc": snap,
            "name": (p or {}).get("baseToken", {}).get("name") or t["name"],
            "symbol": (p or {}).get("baseToken", {}).get("symbol") or t["symbol"],
            "token_address": t["mint"],
            "pair_address": (p or {}).get("pairAddress") or t["canonical_pool"],
            "dex": (p or {}).get("dexId") or "pumpswap",
            "quote": q.get("symbol") if q.get("address") == SOL or not q else f"{q.get('symbol')} ({q.get('name')})",
            "price_usd": _f((p or {}).get("priceUsd")),
            "liquidity_usd": _f(((p or {}).get("liquidity") or {}).get("usd")),
            "mcap_usd": _f((p or {}).get("marketCap") or (p or {}).get("fdv")),
            "vol_24h": _f(vol.get("h24")), "vol_1h": _f(vol.get("h1")),
            "txns_1h_buys": tx.get("buys"), "txns_1h_sells": tx.get("sells"),
            "chg_1h": _f(chg.get("h1")), "chg_6h": _f(chg.get("h6")), "chg_24h": _f(chg.get("h24")),
            "label": t["label"], "graduated_utc": t["graduated_at"], "grad_mcap_usd": _f(t["grad_mcap"]),
            "peak_mult_72h": t["peak_multiple"], "hours_to_peak": t["hours_to_peak"],
            "close24h_vs_peak": t["close_24h_vs_peak"], "max_5m_jump": t["max_jump"],
        }
        r["note"] = note_for(p, r, now)
        rows.append(r)
    return rows


def fetch_pairs(api: ApiClient, mints: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(mints), BATCH):
        out.update(best_pairs(api.get_json(f"{BASE}/tokens/v1/solana/{','.join(mints[i:i + BATCH])}")))
    return out


def write_page(conn: sqlite3.Connection, api: ApiClient, out: str | Path, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    toks = runners(conn)
    rows = build_rows(toks, fetch_pairs(api, [t["mint"] for t in toks]), now)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    data = {"generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "rule_version": config.RULE_VERSION,
            "labels": list(RUNNER_LABELS), "jump_flag": JUMP_FLAG, "columns": COLUMNS + RESEARCH, "rows": rows}
    tmp = out / "data.json.tmp"
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(out / "data.json")
    (out / "index.html").write_text((Path(__file__).parent / "web" / "index.html").read_text())
    return len(rows)
