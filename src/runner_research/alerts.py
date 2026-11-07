"""Telegram alerts for live-test signals (livetest.py).

signal: sent once when a graduation passes the rule at T0+60m, with live
        DexScreener market data, GeckoTerminal holders and the token image.
result: sent once the signal is scored after 72h, as a reply to the signal.
Only data this pipeline has is shown (no smart-money / KOL / bundle data).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

import httpx

from . import db, livetest
from .meta import gateway_urls
from .telegram import Telegram, e

log = logging.getLogger(__name__)
PAGE = os.environ.get("RR_PUBLIC_URL", "https://dashboard.cisavalentino.digital/meme-research/live.html")
FRESH = timedelta(hours=3)            # never alert on signals older than this (e.g. after downtime)
MAX_IMAGE = 5_000_000


def usd(v) -> str:
    if v is None:
        return "–"
    v = float(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:.1f}{suf}"
    return f"${v:.0f}"


def mx(v) -> str:
    return "–" if v is None else (f"{v:.0f}x" if v >= 10 else f"{v:.2f}x")


def pct(v) -> str:
    return "–" if v is None else f"{v:+.1f}%"


def age(t0: datetime, now: datetime) -> str:
    m = int((now - t0).total_seconds() // 60)
    return f"{m}m" if m < 60 else f"{m // 60}j{m % 60:02d}m"


def link(url: str | None, text: str) -> str | None:
    if not url or not str(url).startswith(("http://", "https://")):
        return None
    return f'<a href="{e(url).replace(chr(34), "&quot;")}">{e(text)}</a>'


def signal_message(r: dict, f: dict, pair: dict | None, info: dict | None, creator_grads: int | None,
                   warnings: list[str], now: datetime, rule: str) -> str:
    t0 = datetime.fromisoformat(r["t0"])
    p = pair or {}
    vol, chg = p.get("volume") or {}, p.get("priceChange") or {}
    tx = (p.get("txns") or {}).get("h1") or {}
    b, s = tx.get("buys"), tx.get("sells")
    i = info or {}
    bt = livetest.BACKTEST
    tool = f.get("tool") or "–"
    bond = f.get("bonding_minutes")
    lines = [
        f"🟢 <b>SINYAL RUNNER</b> · {e(rule)} · SOL",
        f"<b>{e(r['name'])}</b> (${e(r['symbol'])}) · ⏱{age(t0, now)} · Graduated",
        f"<code>{e(r['mint'])}</code>",
        "",
        f"💸 MC {usd(p.get('marketCap') or p.get('fdv'))} · LP {usd((p.get('liquidity') or {}).get('usd'))}",
        f"📈 Grad {usd(f.get('grad_mcap'))} → 15m {mx(f.get('m15'))} → 60m {mx(f.get('m60'))}",
        f"🔥 Vol1j {usd(vol.get('h1'))} · 5m {pct(chg.get('m5'))} · 1j {pct(chg.get('h1'))}",
    ]
    if b is not None:
        lines.append(f"🅑{b} Ⓢ{s} · {b / s:.2f}× (1 jam)" if s else f"🅑{b} Ⓢ{s}")
    if i:
        lines.append(f"👥 {i.get('holders') or '–'} · Top10 {i['top10_pct']:.1f}%" if i.get("top10_pct") is not None
                     else f"👥 {i.get('holders') or '–'}")
        lines.append(f"🔐 Mint {'❌' if i.get('mint_auth') else '✅'} · Freeze {'❌' if i.get('freeze_auth') else '✅'}"
                     + (f" · Dev {i['dev_pct']:.1f}%" if i.get("dev_pct") is not None else ""))
    jump = f.get("max_jump_1h")
    lines.append(f"🕯 Lonjakan 1 candle maks {mx(jump)}" + (f" · ⏳ Bonding {bond * 60:.0f} dtk" if bond is not None and bond < 1
                                                      else f" · ⏳ Bonding {bond:.0f} mnt" if bond else ""))
    if creator_grads is not None:
        lines.append(f"👤 Creator graduate sebelumnya: {creator_grads}")
    lines.append(f"🚀 {e(tool)}")
    socials = [x for x in (link(f.get("twitter"), "𝕏"), link(f.get("telegram"), "TG"), link(f.get("website"), "Web"),
                           link(f.get("community"), "Komunitas")) if x]
    lines.append("🔗 " + (" · ".join(socials) if socials else "tanpa sosial"))
    for w in warnings:
        lines.append(f"⚠️ {e(w)}")
    lines += [
        "",
        f"📊 Backtest {e(bt['period'])}: {bt['runner_label'] * 100:.0f}% jadi runner · "
        f"{bt['hit2'] * 100:.0f}% ≥2x dari masuk (semua token {bt['base_runner'] * 100:.1f}%)",
        " · ".join(x for x in (link(f"https://gmgn.ai/sol/token/{r['mint']}", "GMGN"),
                                link(f"https://dexscreener.com/solana/{r['mint']}", "Dex"),
                                link(PAGE, "Live test")) if x),
        "<i>Paper trading, bukan saran beli.</i>",
    ]
    return "\n".join(lines)


def result_message(r: dict, o: dict) -> str:
    if not o or o.get("missing"):
        return f"🏁 <b>HASIL 72 JAM</b> · ${e(r['symbol'])}\nCandle 72 jam tidak tersedia, tidak bisa dinilai."
    up = o.get("max_mult") or 0
    head = "🚀" if up >= 5 else "✅" if up >= 2 else "🔻"
    lab = r.get("label") or "menunggu label harian"
    return "\n".join([
        f"🏁 <b>HASIL 72 JAM</b> · {e(r['name'])} (${e(r['symbol'])})",
        f"{head} Maks {mx(o.get('max_mult'))} dari harga masuk · akhir {mx(o.get('last'))}"
        + (" · ☠️ rug" if o.get("rugged") else ""),
        f"🎯 Rencana ½@2x ½@5x SL0,6: {mx(o.get('plan'))} · jual semua @1,3x: {mx(o.get('x_tp13_nosl'))}",
        f"🏷 Label: {e(lab)}",
    ])


def fetch_image(client: httpx.Client, url: str | None) -> bytes | None:
    if not url:
        return None
    for u in gateway_urls(url):
        try:
            r = client.get(u, timeout=10)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") \
                    and len(r.content) <= MAX_IMAGE:
                return r.content
        except httpx.HTTPError:
            continue
    return None


def _sent(conn, mint: str, kind: str) -> dict | None:
    r = conn.execute("SELECT messages FROM alerts WHERE mint = ? AND kind = ?", (mint, kind)).fetchone()
    return json.loads(r[0] or "{}") if r else None


def _mark(conn, mint: str, kind: str, msgs: dict) -> None:
    db.upsert(conn, "alerts", [{"mint": mint, "kind": kind, "sent_at": db.now_iso(),
                                "messages": json.dumps({str(k): v for k, v in msgs.items()})}], key=("mint", "kind"))


def send_alerts(conn: sqlite3.Connection, tg: Telegram, api, gecko, client: httpx.Client, now: datetime) -> dict:
    from .webpage import fetch_pairs
    out = {"signal": 0, "result": 0}
    rule = livetest.RULE.version
    rows = conn.execute(
        """SELECT l.*, t.name, t.symbol, t.creator, t.graduated_at, m.image FROM live_signals l
           JOIN tokens t USING (mint) LEFT JOIN token_meta m USING (mint)
           WHERE l.rule = ? AND l.status = 'signal' AND l.t0 >= ?
             AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.mint = l.mint AND a.kind = 'signal')""",
        (rule, livetest._iso(now - FRESH))).fetchall()
    pairs = fetch_pairs(api, [r["mint"] for r in rows]) if rows else {}
    for r in rows:
        f = json.loads(r["features"] or "{}")
        info = None
        if gecko is not None:
            try:
                info = gecko.token_info(r["mint"])
            except Exception as ex:
                log.warning("token_info %s: %s", r["mint"], ex)
        grads = conn.execute("SELECT COUNT(*) FROM tokens WHERE creator = ? AND graduated_at < ?",
                             (r["creator"], r["graduated_at"])).fetchone()[0] if r["creator"] else None
        text = signal_message(dict(r), f, pairs.get(r["mint"]), info, grads, json.loads(r["warnings"] or "[]"),
                              now, rule)
        _mark(conn, r["mint"], "signal", tg.send(text, fetch_image(client, r["image"])))
        out["signal"] += 1
    for r in conn.execute(
            """SELECT l.*, t.name, t.symbol FROM live_signals l JOIN tokens t USING (mint)
               WHERE l.rule = ? AND l.status = 'scored'
                 AND EXISTS (SELECT 1 FROM alerts a WHERE a.mint = l.mint AND a.kind = 'signal')
                 AND NOT EXISTS (SELECT 1 FROM alerts a WHERE a.mint = l.mint AND a.kind = 'result')""",
            (rule,)).fetchall():
        reply = {k: v for k, v in (_sent(conn, r["mint"], "signal") or {}).items()}
        _mark(conn, r["mint"], "result", tg.send(result_message(dict(r), json.loads(r["outcome"] or "{}")),
                                                 reply_to=reply))
        out["result"] += 1
    return out
