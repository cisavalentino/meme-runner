"""Early signal e1: holders one minute after graduation (separate from r2).

At T0+60s (evaluated until T0+3m, later = too late), from the Solana RPC:
  holders    every token account of the mint (Helius DAS getTokenAccounts),
             pool and bonding curve excluded           -> holders, top10 %, creator %
  buyers     first transaction of a sample of holder token accounts:
             "pro" = bought through a trading terminal / bot (router program
             of Axiom, Photon, GMGN, Trojan, Maestro, ... or a fee/tip SOL
             transfer added by the client); wallets/aggregators (Jupiter,
             DFlow, OKX) and the pump.fun site are not pro
  cluster    top holders whose first buy is in the same slot, or signed by
             the same other wallet (bundle / one operator), as % of supply
  price      pool reserves now (entry) vs the graduation reserves
Cheap checks run first; the deep check (~2 RPC calls per probed holder) only
for tokens that pass them, at most DEEP_PER_DAY per UTC day.
No backtest exists: holder state at T0+60s is not recoverable from history,
so every evaluated token is stored to judge the thresholds later.
"""
from __future__ import annotations

import json
import logging
import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

from . import alerts, config, db, livetest, strategy
from .telegram import Telegram, e

log = logging.getLogger(__name__)

RULE = "e1"
EVAL_AT = timedelta(seconds=60)
LATE = timedelta(minutes=3)
MIN_HOLDERS = 100
MIN_PRO = 0.5            # share of probed holders that bought via a terminal
MAX_TOP10 = 30.0         # % of supply, pool excluded
MAX_DEV = 5.0            # % of supply still held by the creator
MAX_CLUSTER = 15.0       # % of supply held by same-slot / same-signer groups in the top holders
TOP_PROBE = 15           # top holders probed (cluster + pro)
SAMPLE_PROBE = 20        # random other holders probed (pro)
DEEP_PER_DAY = 150       # RPC budget: ~70 calls per deep check

TERMINALS = {            # top-level router programs of trading terminals / bots
    "6Vo3245eszAb5wuqEMw8mGdbfRUdKbHhDHP5LcaGuTAB", "FLASHX8DrLbgeR8FcfNV1F5krxYcYMUdBkrP1EPBtxB9",
    "term9YPb9mzAsABaqN71A4xdbxHmpBNZavpBiQKZzN3", "MaestroAAe9ge5HTc64VbBQZ6fP77pwvrhM8i1XWSAx",
    "troyXT7Ty3s2rjJe4bqWaroUrS4Fjd8rbHHNHxcACF4", "BSfD6SHZigAfDWSjzD5Q41jw8LmKwtmjskPH9XW1mrRW",
}
AGGREGATORS = {          # wallet swaps (Phantom, Jupiter, OKX ...): not pro
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",
    "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH", "proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u",
    "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma",
}
PUMP = {"6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}
SYSTEM = "11111111111111111111111111111111"


# ---------------------------------------------------------------- RPC reads

def token_accounts(rpc, mint: str, max_pages: int = 5) -> list[dict]:
    """Non-empty token accounts of a mint: {owner, address, amount}."""
    out = []
    for page in range(1, max_pages + 1):
        res = rpc.call("getTokenAccounts", {"mint": mint, "limit": 1000, "page": page})
        accs = res.get("token_accounts") or []
        out += [{"owner": a["owner"], "address": a["address"], "amount": int(a["amount"])}
                for a in accs if int(a.get("amount") or 0) > 0]
        if len(accs) < 1000:
            break
    return out


def pool_price(rpc, pool: str, base_mint: str, quote_mint: str) -> float | None:
    """Pool reserves now: quote per base token (ui amounts)."""
    amt = {}
    for m in (base_mint, quote_mint):
        r = rpc.call("getTokenAccountsByOwner", [pool, {"mint": m}, {"encoding": "jsonParsed"}])["value"]
        amt[m] = max((float(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"] or 0) for a in r),
                     default=0.0)
    return amt[quote_mint] / amt[base_mint] if amt[base_mint] > 0 else None


def first_buy(rpc, account: str) -> dict | None:
    """Oldest transaction of a token account (its creation = the first buy, for a fresh account)."""
    sigs = rpc.call("getSignaturesForAddress", [account, {"limit": 100}])
    if not sigs:
        return None
    return rpc.transaction(sigs[-1]["signature"])


def classify(tx: dict | None, owner: str, mint: str) -> dict | None:
    """{'pro', 'slot', 'signer'} for a buy, None if the tx is not a buy of `mint` by `owner`."""
    if not tx or not tx.get("meta"):
        return None
    meta, msg = tx["meta"], tx["transaction"]["message"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
    keys += [*(meta.get("loadedAddresses") or {}).get("writable", []), *(meta.get("loadedAddresses") or {}).get("readonly", [])]
    bal = lambda xs: sum(float(b["uiTokenAmount"]["uiAmount"] or 0) for b in xs
                         if b.get("mint") == mint and b.get("owner") == owner)
    if bal(meta.get("postTokenBalances") or []) <= bal(meta.get("preTokenBalances") or []):
        return None
    top = [i.get("programId") for i in msg["instructions"]]
    inner = [i.get("programId") for g in (meta.get("innerInstructions") or []) for i in g["instructions"]]
    if not PUMP & set(top + inner) and not AGGREGATORS & set(top) and not TERMINALS & set(top):
        return None                                           # received by transfer, not bought
    token_accs = {keys[b["accountIndex"]] for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or [])
                  if b["accountIndex"] < len(keys)}
    fee = any(i.get("programId") == SYSTEM and isinstance(i.get("parsed"), dict) and i["parsed"].get("type") == "transfer"
              and i["parsed"]["info"].get("destination") not in token_accs | {owner}
              and int(i["parsed"]["info"].get("lamports") or 0) > 0
              for i in msg["instructions"])
    pro = bool(TERMINALS & set(top)) or (fee and not AGGREGATORS & set(top))
    return {"pro": pro, "slot": tx.get("slot"), "signer": keys[0]}


# ---------------------------------------------------------------- rule

def holder_stats(accs: list[dict], exclude: set[str], creator: str | None) -> dict:
    total = sum(a["amount"] for a in accs) or 1
    by_owner = defaultdict(int)
    for a in accs:
        by_owner[a["owner"]] += a["amount"]
    hold = {o: v for o, v in by_owner.items() if o not in exclude}
    top = sorted(hold.items(), key=lambda kv: -kv[1])[:10]
    return {"holders": len(hold), "top10": sum(v for _, v in top) / total * 100,
            "dev": by_owner.get(creator, 0) / total * 100 if creator else None}


def cluster_pct(probes: list[dict], total: int) -> tuple[float, int]:
    """% of supply in groups of >= 2 top holders sharing a buy slot or an outside signer."""
    groups = defaultdict(set)
    for p in probes:
        if p.get("slot") is not None:
            groups[("slot", p["slot"])].add(p["owner"])
        if p.get("signer") and p["signer"] != p["owner"]:
            groups[("signer", p["signer"])].add(p["owner"])
    linked = set().union(*[g for g in groups.values() if len(g) >= 2] or [set()])
    amt = {p["owner"]: p["amount"] for p in probes}
    return sum(amt[o] for o in linked) / (total or 1) * 100, len(linked)


def deep(rpc, accs: list[dict], exclude: set[str], mint: str, rng: random.Random) -> dict:
    total = sum(a["amount"] for a in accs)
    by_owner = {}
    for a in sorted(accs, key=lambda a: -a["amount"]):
        if a["owner"] not in exclude:
            by_owner.setdefault(a["owner"], a)
    ranked = list(by_owner.values())
    top, rest = ranked[:TOP_PROBE], ranked[TOP_PROBE:]
    sample = rng.sample(rest, min(SAMPLE_PROBE, len(rest)))
    probes = []
    for a in top + sample:
        try:
            c = classify(first_buy(rpc, a["address"]), a["owner"], mint)
        except Exception as ex:
            log.warning("probe %s: %s", a["address"], ex)
            c = None
        probes.append({**a, **(c or {}), "top": a in top, "buy": c is not None})
    buys = [p for p in probes if p["buy"]]
    cl, n_linked = cluster_pct([p for p in probes if p["top"] and p["buy"]], total)
    return {"probed": len(probes), "buys": len(buys), "pro_n": sum(p["pro"] for p in buys),
            "pro": sum(p["pro"] for p in buys) / len(buys) if buys else None,
            "cluster": cl, "cluster_wallets": n_linked,
            "transfer_in": sum(1 for p in probes if p["top"] and not p["buy"])}


def evaluate(f: dict) -> list[str]:
    why = []
    if f.get("grad_mcap") is None or f["grad_mcap"] < config.HOLLOW_MCAP_USD:
        why.append("graduation kosong")
    if f.get("bonding_minutes") == 0:
        why.append("graduation instan")
    if f.get("holders") is not None and f["holders"] <= MIN_HOLDERS:
        why.append(f"holder ≤ {MIN_HOLDERS}")
    if f.get("top10") is not None and f["top10"] > MAX_TOP10:
        why.append(f"top10 > {MAX_TOP10:.0f}%")
    if f.get("dev") is not None and f["dev"] > MAX_DEV:
        why.append(f"creator pegang > {MAX_DEV:.0f}%")
    if "pro" in f and (f["pro"] is None or f["pro"] < MIN_PRO):
        why.append(f"pro trader < {MIN_PRO * 100:.0f}%")
    if f.get("cluster") is not None and f["cluster"] > MAX_CLUSTER:
        why.append(f"cluster top holder > {MAX_CLUSTER:.0f}%")
    return why


def step(conn: sqlite3.Connection, rpc, now: datetime, rng: random.Random | None = None) -> dict:
    rng = rng or random.Random()
    since = db.get_state(conn, "early_since")
    if since is None:
        since = livetest._iso(now)
        db.set_state(conn, "early_since", since)
    lo = max(since, livetest._iso(now - LATE - timedelta(minutes=2)))
    toks = conn.execute(
        """SELECT t.*, p.quote_mint, p.pool_base, p.pool_quote FROM tokens t
           LEFT JOIN pools p ON p.pool = t.canonical_pool
           WHERE t.graduated_at >= ? AND t.graduated_at <= ?
             AND NOT EXISTS (SELECT 1 FROM live_signals l WHERE l.mint = t.mint AND l.rule = ?)
           ORDER BY t.graduated_at""", (lo, livetest._iso(now - EVAL_AT), RULE)).fetchall()
    out = defaultdict(int)
    day_key = f"early_deep_{now:%Y-%m-%d}"
    for tok in toks:
        t0 = datetime.fromisoformat(tok["graduated_at"])
        gn = livetest.grad_native(tok)
        q = livetest.quote_usd(conn, tok["quote_mint"], t0)
        f = {"grad_mcap": gn * q * config.PUMPFUN_SUPPLY if gn and q else None,
             "bonding_minutes": tok["bonding_minutes"], "checked_at": livetest._iso(now),
             "delay_s": round((now - t0).total_seconds())}
        why = evaluate(f)
        if now - t0 > LATE:
            why.append("terlambat dicek (> 3 mnt)")
        try:
            if not why:
                exclude = {tok["canonical_pool"], tok["bonding_curve"]}
                accs = token_accounts(rpc, tok["mint"])
                f.update(holder_stats(accs, exclude, tok["creator"]))
                why = evaluate(f)
                if not why:
                    used = int(db.get_state(conn, day_key) or 0)
                    if used >= DEEP_PER_DAY:
                        why = ["kuota cek harian habis"]
                    else:
                        db.set_state(conn, day_key, str(used + 1))
                        f.update(deep(rpc, accs, exclude, tok["mint"], rng))
                        why = evaluate(f)
                if not why:
                    p = pool_price(rpc, tok["canonical_pool"], tok["mint"], tok["quote_mint"])
                    f["price_native"] = p
                    f["m_now"] = p / gn if p and gn else None
                    f["entry_mcap"] = p * q * config.PUMPFUN_SUPPLY if p and q else None
                    if not p or not q:
                        why = ["harga pool tidak terbaca"]
        except Exception as ex:
            log.warning("early %s: %s", tok["mint"], ex)
            why = [f"error RPC: {type(ex).__name__}"]
        status = "rejected" if why else "signal"
        entry = f["price_native"] * q if status == "signal" else None
        db.upsert(conn, "live_signals", [{
            "mint": tok["mint"], "rule": RULE, "t0": tok["graduated_at"], "status": status,
            "reasons": json.dumps(why, ensure_ascii=False), "warnings": "[]",
            "features": json.dumps(f, ensure_ascii=False, default=str),
            "entry_price": entry, "entry_at": livetest._iso(now) if entry else None,
            "updated_at": livetest._iso(now)}], key=("mint", "rule"))
        out[status] += 1
    return dict(out)


def score(conn: sqlite3.Connection, now: datetime) -> int:
    """Outcome from the entry (T0+~1m) to T0+72h, same exit simulation as r2."""
    n = 0
    for s in conn.execute(
            """SELECT l.mint, l.t0, l.entry_price, l.entry_at, t.canonical_pool FROM live_signals l
               JOIN tokens t USING (mint) WHERE l.rule = ? AND l.status = 'signal' AND l.t0 <= ?""",
            (RULE, livetest._iso(now - livetest.SCORE_AFTER))).fetchall():
        t0 = datetime.fromisoformat(s["t0"])
        rows = [(datetime.fromisoformat(r[0]), r[1], r[2]) for r in conn.execute(
            "SELECT ts, open, close FROM candles_5m WHERE pool = ? AND ts >= ? AND ts < ? AND close > 0 ORDER BY ts",
            (s["canonical_pool"], livetest._iso(t0 - timedelta(minutes=5)),
             livetest._iso(t0 + timedelta(hours=config.WINDOW_HOURS))))]
        if not rows and now - t0 < livetest.SCORE_GIVE_UP:
            continue
        mins = (datetime.fromisoformat(s["entry_at"]) - t0).total_seconds() / 60 if s["entry_at"] else 1
        res = strategy.outcome(t0, rows, s["entry_price"], entry_min=mins) if rows else {}
        conn.execute("UPDATE live_signals SET status = 'scored', outcome = ?, updated_at = ? WHERE mint = ? AND rule = ?",
                     (json.dumps(res or {"missing": "candles"}), livetest._iso(now), s["mint"], RULE))
        n += 1
    conn.execute(
        """UPDATE live_signals SET label = (SELECT b.label FROM labels b WHERE b.mint = live_signals.mint
                                           AND b.rule_version = ?)
           WHERE rule = ? AND label IS NULL AND status IN ('scored', 'signal', 'rejected')""",
        (config.RULE_VERSION, RULE))
    conn.commit()
    return n


# ---------------------------------------------------------------- alerts

def signal_message(r: dict, f: dict, meta: dict | None, now: datetime) -> str:
    t0 = datetime.fromisoformat(r["t0"])
    m = meta or {}
    socials = [x for x in (alerts.link(m.get("twitter"), "𝕏"), alerts.link(m.get("telegram"), "TG"),
                           alerts.link(m.get("website"), "Web"), alerts.link(m.get("community"), "Komunitas")) if x]
    bond = f.get("bonding_minutes")
    lines = [
        f"⚡ <b>SINYAL AWAL</b> · {RULE} · SOL",
        f"<b>{e(r['name'])}</b> (${e(r['symbol'])}) · ⏱{alerts.age(t0, now)} setelah graduate",
        f"<code>{e(r['mint'])}</code>",
        "",
        f"💸 MC {alerts.usd(f.get('entry_mcap'))} · Grad {alerts.usd(f.get('grad_mcap'))} · {alerts.mx(f.get('m_now'))}",
        f"👥 {f.get('holders')} holder di menit 1 · Top10 {f['top10']:.1f}%"
        + (f" · Dev {f['dev']:.1f}%" if f.get("dev") is not None else ""),
        f"🧠 Pro trader {f['pro'] * 100:.0f}% ({f['pro_n']}/{f['buys']} pembeli dicek)",
        f"🕸 Cluster top holder {f['cluster']:.1f}% ({f['cluster_wallets']} wallet)"
        + (f" · {f['transfer_in']} top holder dapat token via transfer" if f.get("transfer_in") else ""),
    ]
    if bond is not None:
        lines.append(f"⏳ Bonding {bond * 60:.0f} dtk" if bond < 1 else f"⏳ Bonding {bond:.0f} mnt")
    lines.append("🔗 " + (" · ".join(socials) if socials else "tanpa sosial"))
    lines += ["", "⚠️ Aturan baru, belum ada backtest. Hasil 72 jam dicatat untuk menilai.",
              " · ".join(x for x in (alerts.link(f"https://gmgn.ai/sol/token/{r['mint']}", "GMGN"),
                                      alerts.link(f"https://dexscreener.com/solana/{r['mint']}", "Dex")) if x),
              "<i>Paper trading, bukan saran beli.</i>"]
    return "\n".join(lines)


def send_alerts(conn: sqlite3.Connection, tg: Telegram, client, rpc, now: datetime) -> dict:
    out = {"signal": 0, "result": 0}
    for r in conn.execute(
            """SELECT l.*, t.name, t.symbol FROM live_signals l JOIN tokens t USING (mint)
               WHERE l.rule = ? AND l.status = 'signal' AND l.t0 >= ?
                 AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.mint = l.mint AND a.kind = 'e1_signal')""",
            (RULE, livetest._iso(now - timedelta(minutes=30)))).fetchall():
        try:
            meta = livetest.ensure_meta(conn, client, rpc, r["mint"])
        except Exception as ex:
            log.warning("meta %s: %s", r["mint"], ex)
            meta = None
        text = signal_message(dict(r), json.loads(r["features"]), meta, now)
        alerts._mark(conn, r["mint"], "e1_signal", tg.send(text, alerts.fetch_image(client, (meta or {}).get("image"))))
        out["signal"] += 1
    for r in conn.execute(
            """SELECT l.*, t.name, t.symbol FROM live_signals l JOIN tokens t USING (mint)
               WHERE l.rule = ? AND l.status = 'scored'
                 AND EXISTS (SELECT 1 FROM alerts a WHERE a.mint = l.mint AND a.kind = 'e1_signal')
                 AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.mint = l.mint AND a.kind = 'e1_result')""",
            (RULE,)).fetchall():
        alerts._mark(conn, r["mint"], "e1_result",
                     tg.send(alerts.result_message(dict(r), json.loads(r["outcome"] or "{}")),
                             reply_to=alerts._sent(conn, r["mint"], "e1_signal") or {}))
        out["result"] += 1
    return out
