"""Trend signals (pump.fun graduates in the database), run by `rr trend`.

w1  revival: a token graduated >= 1 day ago suddenly trades again (an
    influencer post, a news hook). Watched tokens = still-liquid graduates of
    the last SWEEP_DAYS (a slow DexScreener sweep keeps the list) plus any
    graduate on GeckoTerminal trending / DexScreener boosts. Fires when the
    last hour's volume is far above its own 24h average and the price runs.
n1  narrative: a word spikes in the names of new graduations (many copies of
    one story). Among every graduate carrying the word, the leader by 1h
    volume is the signal, not the copies. Google News headlines with the
    word are shown as context only (the word often isn't in the news).
Both run the top-holder checks of e1 (top10, creator, cluster) before
signalling, and are scored 72h after detection from GeckoTerminal candles.
No backtest exists for either.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import alerts, config, db, early, fees, livetest, strategy
from .insights import STOPWORDS
from .telegram import Telegram, e
from .webpage import fetch_pairs

log = logging.getLogger(__name__)
_iso = livetest._iso

SWEEP_DAYS = 60
SWEEP_CHUNK = 300            # mints per loop (10 DexScreener calls)
ALIVE_LIQ = 5_000            # stays watched while liquidity >= this
MIN_AGE = timedelta(days=1)
MIN_LIQ = 10_000
MIN_VOL1H = 25_000
VOL_RATIO = 5.0              # 1h volume vs the average hour of the previous 23h
MIN_CHG, MAX_CHG = 50.0, 500.0   # 1h price change %, not too late
NARR_WINDOW = timedelta(hours=3)
NARR_MIN = 4                 # graduations with the word in the window
NARR_SPIKE = 5.0             # vs the word's rate in the previous 7 days
NARR_COOLDOWN = timedelta(hours=24)
LEAD_MARGIN = 2.0            # leader's 1h volume vs the runner-up
DEEP_PER_DAY = 150           # holder checks per day for w1 + n1 (top 15 holders only, ~32 RPC calls each)
RECHECK = timedelta(hours=6)  # a rejected w1 token is checked again after this
NARR_RETRY = timedelta(minutes=30)  # a word whose leader was rejected is looked at again after this
SCORE_AFTER = livetest.SCORE_AFTER
NEWS_URL = "https://news.google.com/rss/search"
RULES = {"w1": "🌊 KEBANGKITAN", "n1": "🔥 NARASI", "c1": "📣 CALL TELEGRAM"}


# ---------------------------------------------------------------- watch list

def _alive(conn) -> dict[str, str]:
    return json.loads(db.get_state(conn, "trend_alive") or "{}")       # mint -> reason


def sweep(conn: sqlite3.Connection, api, now: datetime) -> int:
    """One chunk of the slow pass over graduates of the last SWEEP_DAYS."""
    cursor = db.get_state(conn, "trend_sweep_cursor") or ""
    lo, hi = _iso(now - timedelta(days=SWEEP_DAYS)), _iso(now - MIN_AGE)
    mints = [r[0] for r in conn.execute(
        "SELECT mint FROM tokens WHERE graduated_at >= ? AND graduated_at <= ? AND mint > ? ORDER BY mint LIMIT ?",
        (lo, hi, cursor, SWEEP_CHUNK))]
    db.set_state(conn, "trend_sweep_cursor", mints[-1] if len(mints) == SWEEP_CHUNK else "")
    if not mints:
        return 0
    pairs = fetch_pairs(api, mints)
    alive = _alive(conn)
    for m in mints:
        if _liq(pairs.get(m)) >= ALIVE_LIQ:
            alive.setdefault(m, "sweep")
    db.set_state(conn, "trend_alive", json.dumps(alive))
    return len(mints)


def trending_mints(api) -> list[str]:
    out = []
    try:
        js = api.get_json("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools", params={"page": 1})
        out += [p["relationships"]["base_token"]["data"]["id"].removeprefix("solana_") for p in js.get("data") or []]
    except Exception as ex:
        log.warning("gecko trending: %s", ex)
    for path in ("token-boosts/latest/v1", "token-boosts/top/v1", "token-profiles/latest/v1"):
        try:
            out += [x["tokenAddress"] for x in api.get_json(f"https://api.dexscreener.com/{path}") or []
                    if x.get("chainId") == "solana"]
        except Exception as ex:
            log.warning("dexscreener %s: %s", path, ex)
    return out


def add_ext(conn: sqlite3.Connection, pairs: dict[str, dict], source: str) -> int:
    """Store tokens that are not pump.fun graduates (other launchpads) so the
    trend signals can watch them; pump.fun graduates are already in `tokens`."""
    rows = []
    for m, p in pairs.items():
        if conn.execute("SELECT 1 FROM tokens WHERE mint = ?", (m,)).fetchone():
            continue
        bt = p.get("baseToken") or {}
        ts = p.get("pairCreatedAt")
        rows.append({"mint": m, "name": bt.get("name"), "symbol": bt.get("symbol"), "pool": p.get("pairAddress"),
                     "dex": p.get("dexId"), "source": source, "fetched_at": db.now_iso(),
                     "created_at": _iso(datetime.fromtimestamp(ts / 1000, tz=timezone.utc)) if ts else None})
    if rows:
        db.upsert(conn, "ext_tokens", rows, key=("mint",))
    return len(rows)


def _liq(p) -> float:
    return float(((p or {}).get("liquidity") or {}).get("usd") or 0)


def _num(d, *ks) -> float:
    for k in ks:
        d = (d or {}).get(k)
    return float(d or 0)


# ---------------------------------------------------------------- checks

def revival_reasons(p: dict | None, graduated_at: datetime, now: datetime) -> list[str]:
    if not p:
        return ["tidak ada pair"]
    why = []
    if now - graduated_at < MIN_AGE:
        why.append("umur < 1 hari")
    v1, v24 = _num(p, "volume", "h1"), _num(p, "volume", "h24")
    avg = max(v24 - v1, 0) / 23
    chg = _num(p, "priceChange", "h1")
    b, s = _num(p, "txns", "h1", "buys"), _num(p, "txns", "h1", "sells")
    if _liq(p) < MIN_LIQ:
        why.append(f"likuiditas < ${MIN_LIQ / 1000:.0f}k")
    if v1 < MIN_VOL1H:
        why.append(f"volume 1j < ${MIN_VOL1H / 1000:.0f}k")
    if avg > 0 and v1 < VOL_RATIO * avg:
        why.append(f"volume 1j < {VOL_RATIO:.0f}x rata-rata")
    if not MIN_CHG <= chg <= MAX_CHG:
        why.append(f"harga 1j di luar +{MIN_CHG:.0f}–{MAX_CHG:.0f}%")
    if b <= s:
        why.append("beli ≤ jual (1j)")
    return why


class QuotaExhausted(Exception):
    """No holder checks left today: the token is skipped, not rejected, and tried again later."""


def program_owned(rpc, accs: list[dict], top: int = 30) -> set[str]:
    """Largest holders that are program accounts (pool vaults, lockers, bonding
    curves of other launchpads), not wallets: excluded from holder stats."""
    by = {}
    for a in accs:
        by[a["owner"]] = by.get(a["owner"], 0) + a["amount"]
    owners = [o for o, _ in sorted(by.items(), key=lambda kv: -kv[1])[:top]]
    infos = rpc.accounts(owners) if owners else []
    return {o for o, i in zip(owners, infos) if i is not None and i.get("owner") != early.SYSTEM}


def holder_check(conn, rpc, tok, rng=None) -> tuple[dict, list[str]]:
    """e1's top-holder checks (no holder-count or pro-trader limit). Only the
    top holders are probed: w1/n1 do not use the pro-trader share."""
    if rpc is None:
        return {}, []
    key = f"trend_deep_{datetime.now():%Y-%m-%d}"
    used = int(db.get_state(conn, key) or 0)
    if used >= DEEP_PER_DAY:
        raise QuotaExhausted
    db.set_state(conn, key, str(used + 1))
    accs = early.token_accounts(rpc, tok["mint"])
    exclude = {tok["canonical_pool"], tok["bonding_curve"]} | program_owned(rpc, accs)
    f = early.holder_stats(accs, exclude, tok["creator"])
    f.update(early.deep(rpc, accs, exclude, tok["mint"], rng or random.Random(), sample=0))
    why = []
    if f["top10"] > early.MAX_TOP10:
        why.append(f"top10 > {early.MAX_TOP10:.0f}%")
    if f.get("dev") is not None and f["dev"] > early.MAX_DEV:
        why.append(f"creator pegang > {early.MAX_DEV:.0f}%")
    if f.get("cluster") is not None and f["cluster"] > early.MAX_CLUSTER:
        why.append(f"cluster top holder > {early.MAX_CLUSTER:.0f}%")
    return f, why


def _pair_features(p: dict) -> dict:
    v1, v24 = _num(p, "volume", "h1"), _num(p, "volume", "h24")
    return {"price": float(p.get("priceUsd") or 0) or None, "mcap": p.get("marketCap") or p.get("fdv"),
            "liq": _liq(p), "vol1h": v1, "vol_avg": max(v24 - v1, 0) / 23, "chg1h": _num(p, "priceChange", "h1"),
            "chg5m": _num(p, "priceChange", "m5"), "buys": _num(p, "txns", "h1", "buys"),
            "sells": _num(p, "txns", "h1", "sells"), "pair": p.get("pairAddress")}


def _fees(api, p: dict | None, mint: str) -> dict:
    """Trader fees / volume on the token's most liquid pair (SOL-quoted pairs only)."""
    if not p or (p.get("quoteToken") or {}).get("address") not in (None, fees.WSOL):
        return {}
    try:
        return fees.fee_ratio(api, p.get("pairAddress"), mint)
    except Exception as ex:
        log.warning("fees %s: %s", mint, ex)
        return {}


def _store(conn, mint: str, rule: str, now: datetime, status: str, why: list[str], f: dict) -> None:
    db.upsert(conn, "live_signals", [{
        "mint": mint, "rule": rule, "t0": _iso(now), "status": status,
        "reasons": json.dumps(why, ensure_ascii=False), "warnings": "[]",
        "features": json.dumps(f, ensure_ascii=False, default=str),
        "entry_price": f.get("price") if status == "signal" else None,
        "entry_at": _iso(now) if status == "signal" else None, "updated_at": _iso(now)}], key=("mint", "rule"))


def _tok(conn, mint: str):
    return conn.execute("SELECT * FROM all_tokens WHERE mint = ?", (mint,)).fetchone()


def _done(conn, mint: str, rule: str, now: datetime | None = None) -> bool:
    """Signalled already, or rejected less than RECHECK ago (each check costs RPC)."""
    r = conn.execute("SELECT status, updated_at FROM live_signals WHERE mint = ? AND rule = ?", (mint, rule)).fetchone()
    if r is None:
        return False
    return r["status"] != "rejected" or (now is not None and now - datetime.fromisoformat(r["updated_at"]) < RECHECK)


# ---------------------------------------------------------------- w1 revival

def revival_step(conn: sqlite3.Connection, api, rpc, now: datetime) -> dict:
    alive = _alive(conn)
    trending = set(trending_mints(api))
    new = sorted(trending - set(alive))
    tp = fetch_pairs(api, new) if new else {}
    add_ext(conn, tp, "trending")
    known = {m for m in trending if _tok(conn, m) is not None}
    for m in known:
        alive[m] = "trending"
    pairs = {**tp, **(fetch_pairs(api, [m for m in alive if m not in tp]) if alive else {})}
    out = {"watched": len(alive), "trending": len(known), "signal": 0}
    for m in list(alive):
        p = pairs.get(m)
        if _liq(p) < ALIVE_LIQ and m not in known:
            alive.pop(m)
            continue
        tok = _tok(conn, m)
        if tok is None or tok["graduated_at"] is None or _done(conn, m, "w1", now):
            continue
        why = revival_reasons(p, datetime.fromisoformat(tok["graduated_at"]), now)
        if why:
            continue                                    # quiet tokens are not stored
        f = {**_pair_features(p), "source": alive[m],
             "age_days": round((now - datetime.fromisoformat(tok["graduated_at"])).total_seconds() / 86400, 1)}
        f.update(_fees(api, p, m))
        why = fees.check(f)
        try:
            if not why:
                hf, why = holder_check(conn, rpc, tok)
                f.update(hf)
        except QuotaExhausted:
            out["quota"] = True
            continue
        except Exception as ex:
            log.warning("holders %s: %s", m, ex)
            why = [f"error RPC: {type(ex).__name__}"]
        _store(conn, m, "w1", now, "rejected" if why else "signal", why, f)
        out["signal"] += not why
    db.set_state(conn, "trend_alive", json.dumps(alive))
    return out


# ---------------------------------------------------------------- n1 narrative

def _words(name, symbol) -> set[str]:
    """Latin words of >= 3 letters; CJK etc. words of any length (咪好)."""
    return {w for w in re.findall(r"[^\W_]+", f"{name or ''} {symbol or ''}".lower())
            if (len(w) >= 3 or not w.isascii()) and w not in STOPWORDS and not w.isdigit()}


def spiking_words(conn: sqlite3.Connection, now: datetime) -> list[tuple[str, int, float]]:
    """(word, count in window, expected count) for words far above their 7-day rate."""
    lo = now - NARR_WINDOW
    recent, base = {}, {}
    for name, sym, t in conn.execute("SELECT name, symbol, graduated_at FROM tokens WHERE graduated_at >= ?",
                                     (_iso(lo - timedelta(days=7)),)):
        bucket = recent if t >= _iso(lo) else base
        for w in _words(name, sym):
            bucket[w] = bucket.get(w, 0) + 1
    per_window = NARR_WINDOW / timedelta(days=7)
    out = []
    for w, n in recent.items():
        exp = base.get(w, 0) * per_window
        if n >= NARR_MIN and n >= NARR_SPIKE * max(exp, 0.2):
            out.append((w, n, exp))
    return sorted(out, key=lambda x: -x[1] / max(x[2], 0.2))


def news(api, word: str, now: datetime) -> dict:
    try:
        r = api.request("GET", NEWS_URL, params={"q": word, "hl": "en-US", "gl": "US", "ceid": "US:en"})
        items = ET.fromstring(r.text).findall(".//item")
    except Exception as ex:
        log.warning("news %s: %s", word, ex)
        return {}
    fresh = []
    for it in items:
        try:
            if now - parsedate_to_datetime(it.findtext("pubDate")) <= timedelta(hours=48):
                fresh.append(it)
        except Exception:
            continue
    return {"news_48h": len(fresh), "headline": fresh[0].findtext("title") if fresh else None,
            "headline_url": fresh[0].findtext("link") if fresh else None}


def narrative_step(conn: sqlite3.Connection, api, rpc, now: datetime) -> dict:
    out = {"words": 0, "signal": 0}
    for w, n, exp in spiking_words(conn, now)[:5]:
        until = db.get_state(conn, f"narr_until_{w}")
        if until and now < datetime.fromisoformat(until):
            continue
        out["words"] += 1
        cands = [r for r in conn.execute(
            "SELECT * FROM all_tokens WHERE graduated_at >= ? AND (lower(name) LIKE ? OR lower(symbol) LIKE ?)",
            (_iso(now - timedelta(days=SWEEP_DAYS)), f"%{w}%", f"%{w}%")) if w in _words(r["name"], r["symbol"])]
        pairs = fetch_pairs(api, [r["mint"] for r in cands])
        ranked = sorted(cands, key=lambda r: -_num(pairs.get(r["mint"]), "volume", "h1"))
        if not ranked:
            continue
        lead, p = ranked[0], pairs.get(ranked[0]["mint"])
        second = _num(pairs.get(ranked[1]["mint"]), "volume", "h1") if len(ranked) > 1 else 0
        why = []
        if not p or _liq(p) < MIN_LIQ:
            why.append(f"likuiditas < ${MIN_LIQ / 1000:.0f}k")
        if _num(p, "volume", "h1") < MIN_VOL1H:
            why.append(f"volume 1j < ${MIN_VOL1H / 1000:.0f}k")
        if second and _num(p, "volume", "h1") < LEAD_MARGIN * second:
            why.append(f"pemimpin < {LEAD_MARGIN:.0f}x volume token kedua")
        if lead["bonding_minutes"] == 0:
            why.append("graduation instan")
        if _done(conn, lead["mint"], "n1"):
            continue
        f = {**(_pair_features(p) if p else {}), "word": w, "copies_3h": n, "expected_3h": round(exp, 2),
             "candidates": len(cands), "second_vol1h": second,
             "age_days": round((now - datetime.fromisoformat(lead["graduated_at"])).total_seconds() / 86400, 1),
             **news(api, w, now)}
        if not why:
            f.update(_fees(api, p, lead["mint"]))
            why = fees.check(f)
        if not why:
            try:
                hf, why = holder_check(conn, rpc, lead)
                f.update(hf)
            except QuotaExhausted:
                out["quota"] = True
                continue
            except Exception as ex:
                log.warning("holders %s: %s", lead["mint"], ex)
                why = [f"error RPC: {type(ex).__name__}"]
        db.set_state(conn, f"narr_until_{w}", _iso(now + (NARR_RETRY if why else NARR_COOLDOWN)))
        _store(conn, lead["mint"], "n1", now, "rejected" if why else "signal", why, f)
        out["signal"] += not why
    return out


# ---------------------------------------------------------------- scoring

def score(conn: sqlite3.Connection, gecko, now: datetime) -> int:
    n = 0
    for s in conn.execute(
            """SELECT l.mint, l.rule, l.t0, l.entry_price, l.features, t.canonical_pool FROM live_signals l
               JOIN all_tokens t USING (mint) WHERE l.rule IN ('w1', 'n1', 'c1') AND l.status = 'signal' AND l.t0 <= ?""",
            (_iso(now - SCORE_AFTER),)).fetchall():
        t0 = datetime.fromisoformat(s["t0"])
        pool = json.loads(s["features"] or "{}").get("pair") or s["canonical_pool"]
        end = t0 + timedelta(hours=config.WINDOW_HOURS)
        try:
            cs = gecko.ohlcv_5m(pool, before_ts=int(end.timestamp()))
        except Exception as ex:
            log.warning("ohlcv %s: %s", s["mint"], ex)
            if now - t0 < livetest.SCORE_GIVE_UP:
                continue
            cs = []
        rows = [(ts, c["open"], c["close"]) for c in cs
                if t0 - timedelta(minutes=5) <= (ts := datetime.fromisoformat(c["ts"])) < end and c["close"]]
        res = strategy.outcome(t0, rows, s["entry_price"], entry_min=0) if rows else {}
        conn.execute("UPDATE live_signals SET status = 'scored', outcome = ?, updated_at = ? WHERE mint = ? AND rule = ?",
                     (json.dumps(res or {"missing": "candles"}), _iso(now), s["mint"], s["rule"]))
        n += 1
    conn.commit()
    return n


# ---------------------------------------------------------------- alerts

def signal_message(rule: str, r: dict, f: dict, meta: dict | None) -> str:
    m = meta or {}
    socials = [x for x in (alerts.link(m.get("twitter"), "𝕏"), alerts.link(m.get("telegram"), "TG"),
                           alerts.link(m.get("website"), "Web"), alerts.link(m.get("community"), "Komunitas")) if x]
    ratio = f["vol1h"] / f["vol_avg"] if f.get("vol_avg") else None
    lines = [f"{RULES[rule]} · {rule} · SOL",
             f"<b>{e(r['name'])}</b> (${e(r['symbol'])})"
             + (f" · umur {f['age_days']} hari" if f.get("age_days") is not None else ""),
             f"<code>{e(r['mint'])}</code>", ""]
    if rule == "c1":
        lines.append(f"📣 Disebut {f['channels']} channel dalam 30 mnt: " + e(", ".join(f.get("chats") or [])[:200]))
    if rule == "n1":
        lines.append(f"🗣 Kata <b>{e(f['word'])}</b>: {f['copies_3h']} token graduate dalam 3 jam "
                     f"(normal {f['expected_3h']:.1f}) · ini pemimpin volume dari {f['candidates']} token")
        if f.get("headline"):
            lines.append(f"📰 {f['news_48h']} berita 48 jam: "
                         + (alerts.link(f.get("headline_url"), f["headline"][:120]) or e(f["headline"][:120])))
        else:
            lines.append("📰 Tidak ada berita dengan kata ini (narasi dari dalam crypto, atau kata tidak muncul di berita)")
    lines += [
        f"💸 MC {alerts.usd(f.get('mcap'))} · LP {alerts.usd(f.get('liq'))}",
        f"🔥 Vol1j {alerts.usd(f.get('vol1h'))}" + (f" · {ratio:.0f}x rata-rata per jam" if ratio else " · sebelumnya sepi")
        + f" · 1j {alerts.pct(f.get('chg1h'))} · 5m {alerts.pct(f.get('chg5m'))}",
        f"🅑{f.get('buys', 0):.0f} Ⓢ{f.get('sells', 0):.0f} (1 jam)",
    ]
    for extra in (fees.line(f), fees.flow_line(f)):
        if extra:
            lines.append(extra)
    if f.get("holders") is not None:
        lines.append(f"👥 {f['holders']} holder · Top10 {f['top10']:.1f}% · cluster {f.get('cluster', 0):.1f}%"
                     + (f" · pro trader {f['pro'] * 100:.0f}%" if f.get("pro") is not None else ""))
    if rule == "w1":
        lines.append(f"📡 Ditemukan dari: {'trending GeckoTerminal/DexScreener' if f.get('source') == 'trending' else 'pantauan likuiditas'}")
    lines.append("🔗 " + (" · ".join(socials) if socials else "tanpa sosial"))
    lines += ["", "⚠️ Aturan baru, belum ada backtest. Cek X: siapa yang memposting?",
              " · ".join(x for x in (alerts.link(f"https://gmgn.ai/sol/token/{r['mint']}", "GMGN"),
                                      alerts.link(f"https://dexscreener.com/solana/{r['mint']}", "Dex"),
                                      alerts.link(f"https://x.com/search?q={r['mint']}&f=live", "Cari di X")) if x),
              "<i>Paper trading, bukan saran beli.</i>"]
    return "\n".join(lines)


def send_alerts(conn: sqlite3.Connection, tg: Telegram, client, rpc, now: datetime,
                rules: tuple[str, ...] = ("w1", "n1")) -> dict:
    """Alerts of `rules` (each process sends only its own rules, so none is sent twice)."""
    rin = ",".join(f"'{r}'" for r in rules if r in RULES)
    out = {"signal": 0, "result": 0}
    for r in conn.execute(
            f"""SELECT l.*, t.name, t.symbol FROM live_signals l JOIN all_tokens t USING (mint)
               WHERE l.rule IN ({rin}) AND l.status = 'signal' AND l.t0 >= ?
                 AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.mint = l.mint AND a.kind = l.rule || '_signal')""",
            (_iso(now - timedelta(minutes=30)),)).fetchall():
        try:
            meta = livetest.ensure_meta(conn, client, rpc, r["mint"])
        except Exception as ex:
            log.warning("meta %s: %s", r["mint"], ex)
            meta = None
        text = alerts.priority_head(conn, r["mint"], r["rule"]) + signal_message(r["rule"], dict(r),
                                                                                 json.loads(r["features"]), meta)
        alerts._mark(conn, r["mint"], f"{r['rule']}_signal",
                     tg.send(text, alerts.fetch_image(client, (meta or {}).get("image"))))
        out["signal"] += 1
    for r in conn.execute(
            f"""SELECT l.*, t.name, t.symbol FROM live_signals l JOIN all_tokens t USING (mint)
               WHERE l.rule IN ({rin}) AND l.status = 'scored'
                 AND EXISTS (SELECT 1 FROM alerts a WHERE a.mint = l.mint AND a.kind = l.rule || '_signal')
                 AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.mint = l.mint AND a.kind = l.rule || '_result')""").fetchall():
        alerts._mark(conn, r["mint"], f"{r['rule']}_result",
                     tg.send(alerts.result_message(dict(r), json.loads(r["outcome"] or "{}")),
                             reply_to=alerts._sent(conn, r["mint"], f"{r['rule']}_signal") or {}))
        out["result"] += 1
    return out
