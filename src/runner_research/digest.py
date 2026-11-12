"""Daily Telegram digest (`rr digest --send`, pm2 rr-digest 00:30 UTC = 07:30 WIB).

e1 no longer alerts one by one (too many, mostly losers); its signals of the
last 24h come here with their price now, plus every rule's 72h results of
the last 24h and a count of the instant alerts.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from . import livetest
from .alerts import mx, usd
from .telegram import e
from .webpage import fetch_pairs

TOP = 10


def report(conn: sqlite3.Connection, api, now: datetime) -> str:
    since = livetest._iso(now - timedelta(hours=24))
    e1 = conn.execute("""SELECT l.mint, l.entry_price, l.features, l.t0, t.symbol FROM live_signals l
                         JOIN all_tokens t USING (mint) WHERE l.rule = 'e1' AND l.status IN ('signal', 'scored')
                         AND l.t0 >= ?""", (since,)).fetchall()
    pairs = fetch_pairs(api, [r["mint"] for r in e1]) if e1 else {}
    rows = []
    for r in e1:
        p = pairs.get(r["mint"]) or {}
        now_px = float(p["priceUsd"]) if p.get("priceUsd") else None
        rows.append((now_px / r["entry_price"] if now_px and r["entry_price"] else None, r, p))
    known = [m for m, _, _ in rows if m is not None]
    lines = [f"🗞 <b>RINGKASAN HARIAN</b> · {now:%d %b %Y}", "",
             f"⚡ <b>e1 (menit 1)</b>: {len(e1)} sinyal dalam 24 jam"]
    if known:
        lines.append(f"Sekarang: {sum(m >= 2 for m in known)} ≥2x · {sum(1 <= m < 2 for m in known)} 1–2x · "
                     f"{sum(0.5 <= m < 1 for m in known)} 0,5–1x · {sum(m < 0.5 for m in known)} &lt;0,5x")
        for m, r, p in sorted((x for x in rows if x[0] is not None), key=lambda x: -x[0])[:TOP]:
            pri = conn.execute("SELECT GROUP_CONCAT(rule) FROM live_signals WHERE mint = ? AND rule != 'e1' "
                               "AND status IN ('signal', 'scored')", (r["mint"],)).fetchone()[0]
            lines.append(f"• <b>${e(r['symbol'])}</b> {mx(m)} · MC {usd(p.get('marketCap'))}"
                         + (f" · juga {e(pri)}" if pri else "")
                         + f'\n  <a href="https://gmgn.ai/sol/token/{r["mint"]}">{e(r["mint"][:6])}…</a>')
    res = conn.execute("""SELECT l.rule, l.outcome, t.symbol FROM live_signals l JOIN all_tokens t USING (mint)
                          WHERE l.status = 'scored' AND l.updated_at >= ? ORDER BY l.rule""", (since,)).fetchall()
    if res:
        lines += ["", f"🏁 <b>Hasil 72 jam yang selesai kemarin</b> ({len(res)})"]
        for r in res[:20]:
            o = json.loads(r["outcome"] or "{}")
            if o.get("max_mult") is not None:
                lines.append(f"• {e(r['rule'])} ${e(r['symbol'])}: maks {mx(o['max_mult'])} · akhir {mx(o.get('last'))}"
                             + (" · ☠️" if o.get("rugged") else ""))
    sent = conn.execute("SELECT kind, COUNT(*) FROM alerts WHERE sent_at >= ? AND kind LIKE '%signal' GROUP BY kind",
                        (since,)).fetchall()
    if sent:
        lines += ["", "📨 Alert langsung kemarin: " + " · ".join(f"{'r2' if k == 'signal' else e(k[:-7])} {n}"
                                                                for k, n in sent)]
    return "\n".join(lines)
