"""Telegram commands for the live test, answered by `rr live-test`.

The "/" menu is set from COMMANDS on start (setMyCommands), so it shows only
commands this bot answers. Only chats in TELEGRAM_CHAT_ID get replies.
Messages are read by long-polling getUpdates between live-test steps.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from . import db, early, livetest
from .alerts import PAGE, age, mx, usd
from .telegram import Telegram, e

log = logging.getLogger(__name__)
OFFSET_KEY = "telegram_update_offset"
WIB = timezone(timedelta(hours=7))

COMMANDS = [
    ("status", "Ringkasan live test: sinyal, pending, hasil"),
    ("sinyal", "Sinyal aktif semua aturan + harga sekarang"),
    ("hasil", "Hasil 72 jam sinyal terakhir"),
    ("cek", "Status satu token: /cek <mint>"),
    ("kanal", "Rapor channel call Telegram"),
    ("help", "Daftar command"),
]


def register(tg: Telegram) -> None:
    """Replace every old menu (default scope and private chats) with COMMANDS."""
    for scope in ({"type": "default"}, {"type": "all_private_chats"}):
        tg._call("deleteMyCommands", json={"scope": scope})
    tg._call("setMyCommands", json={"commands": [{"command": c, "description": d} for c, d in COMMANDS]})


def _rows(conn, sql: str, *args, rule: str | None = None):
    return conn.execute(sql, (rule or livetest.RULE.version, *args)).fetchall()


EXTRA = [("e1", "⚡ Sinyal awal e1 (menit 1: holder, pro trader)"), ("w1", "🌊 Kebangkitan w1 (token lama ramai lagi)"),
         ("n1", "🔥 Narasi n1 (pemimpin kata yang sedang ramai)"), ("c1", "📣 Call c1 (disebut ≥2 channel Telegram)")]
ICON = {"r2": "🟢", "e1": "⚡", "w1": "🌊", "n1": "🔥", "c1": "📣"}
SIG, OPEN, PENDING = "status IN ('signal', 'scored')", "status = 'signal'", "status = 'pending'"


def cmd_status(conn: sqlite3.Connection, now: datetime, prices: dict) -> str:
    since = livetest._iso(now - timedelta(hours=24))
    cnt = lambda where, *a: _rows(conn, f"SELECT COUNT(*) FROM live_signals WHERE rule = ? AND {where}", *a)[0][0]
    scored = [json.loads(r[0] or "{}") for r in _rows(conn, "SELECT outcome FROM live_signals WHERE rule = ? AND status = 'scored'")]
    scored = [o for o in scored if o.get("max_mult") is not None]
    started = _rows(conn, "SELECT MIN(t0) FROM live_signals WHERE rule = ?")[0][0]
    bt = livetest.BACKTEST
    lines = [f"📋 <b>LIVE TEST</b> · aturan {e(livetest.RULE.version)}",
             f"Mulai: {datetime.fromisoformat(started).astimezone(WIB):%d %b %H:%M} WIB" if started else "Belum ada token.",
             "",
             f"24 jam terakhir: {cnt('t0 >= ?', since)} token dinilai · {cnt(SIG + ' AND t0 >= ?', since)} sinyal · "
             f"{cnt(PENDING)} pending",
             f"Total: {cnt(SIG)} sinyal · {cnt(OPEN)} masih jalan (< 72 jam) · {len(scored)} sudah dinilai"]
    if scored:
        n = len(scored)
        lines.append(f"Hasil: {sum(o['max_mult'] >= 2 for o in scored) / n * 100:.0f}% ≥2x · "
                     f"{sum(o['max_mult'] >= 5 for o in scored) / n * 100:.0f}% ≥5x · "
                     f"rencana rata-rata {mx(sum(o.get('plan') or 0 for o in scored) / n)}")
    lines.append(f"Backtest {e(bt['period'])}: {bt['hit2'] * 100:.0f}% ≥2x · {bt['hit5'] * 100:.0f}% ≥5x · "
                 f"{bt['per_day']} sinyal/hari")
    for rule, title in EXTRA:
        cnt_r = lambda where, *a: _rows(conn, f"SELECT COUNT(*) FROM live_signals WHERE rule = ? AND {where}", *a,
                                        rule=rule)[0][0]
        ro = [json.loads(r[0] or "{}") for r in _rows(conn, "SELECT outcome FROM live_signals WHERE rule = ? AND "
                                                      "status = 'scored'", rule=rule)]
        ro = [o for o in ro if o.get("max_mult") is not None]
        lines += ["", f"<b>{title}</b>",
                  f"24 jam: {cnt_r(SIG + ' AND t0 >= ?', since)} sinyal · total {cnt_r(SIG)} · {len(ro)} sudah dinilai"]
        if ro:
            lines.append(f"Hasil: {sum(o['max_mult'] >= 2 for o in ro) / len(ro) * 100:.0f}% ≥2x · "
                         f"rencana rata-rata {mx(sum(o.get('plan') or 0 for o in ro) / len(ro))}")
    lines.append(f'<a href="{e(PAGE)}">Halaman live test</a>')
    return "\n".join(lines)


def cmd_sinyal(conn: sqlite3.Connection, now: datetime, prices: dict) -> str:
    rows = conn.execute("""SELECT l.mint, l.t0, l.entry_price, l.rule, t.symbol FROM live_signals l JOIN all_tokens t USING (mint)
                           WHERE l.rule IN (?, 'e1', 'w1', 'n1', 'c1') AND l.status = 'signal' ORDER BY l.t0 DESC LIMIT 20""",
                        (livetest.RULE.version,)).fetchall()
    if not rows:
        return "Belum ada sinyal aktif (sinyal < 72 jam)."
    lines = ["🟢 <b>SINYAL AKTIF</b> (harga vs masuk)"]
    for r in rows:
        p = prices.get(r["mint"])
        m = p / r["entry_price"] if p and r["entry_price"] else None
        lines.append(f"• {ICON.get(r['rule'], '')} <b>${e(r['symbol'])}</b> · "
                     f"{age(datetime.fromisoformat(r['t0']), now)} · {mx(m)}\n"
                     f"  <code>{e(r['mint'])}</code>")
    return "\n".join(lines)


def cmd_hasil(conn: sqlite3.Connection, now: datetime, prices: dict) -> str:
    rows = conn.execute("""SELECT l.outcome, l.label, l.rule, t.symbol FROM live_signals l JOIN all_tokens t USING (mint)
                           WHERE l.rule IN (?, 'e1', 'w1', 'n1', 'c1') AND l.status = 'scored' ORDER BY l.t0 DESC LIMIT 15""",
                        (livetest.RULE.version,)).fetchall()
    if not rows:
        return "Belum ada sinyal yang selesai 72 jam."
    lines = ["🏁 <b>HASIL 72 JAM</b> (maks · akhir · rencana)"]
    for r in rows:
        o = json.loads(r["outcome"] or "{}")
        if o.get("max_mult") is None:
            lines.append(f"• ${e(r['symbol'])}: candle tidak tersedia")
            continue
        lines.append(f"• {e(r['rule'])} <b>${e(r['symbol'])}</b> {mx(o['max_mult'])} · {mx(o.get('last'))} · {mx(o.get('plan'))}"
                     + (" · ☠️" if o.get("rugged") else "") + (f" · {e(r['label'])}" if r["label"] else ""))
    return "\n".join(lines)


def cmd_cek(conn: sqlite3.Connection, now: datetime, prices: dict, arg: str = "") -> str:
    mint = arg.strip().split()[0] if arg.strip() else ""
    if not mint:
        return "Pakai: <code>/cek &lt;mint&gt;</code>"
    tok = conn.execute("SELECT name, symbol, graduated_at FROM all_tokens WHERE mint = ?", (mint,)).fetchone()
    if tok is None:
        return "Token ini tidak ada di data graduation (belum graduate, bukan pump.fun, atau alamat salah)."
    r = conn.execute("SELECT * FROM live_signals WHERE mint = ? AND rule = ?", (mint, livetest.RULE.version)).fetchone()
    head = f"<b>{e(tok['name'])}</b> (${e(tok['symbol'])}) · graduate {age(datetime.fromisoformat(tok['graduated_at']), now)} lalu"
    if r is None:
        return "\n".join([head + "\nr2: belum dinilai (graduate sebelum live test mulai, atau belum diproses)."]
                         + _cek_early(conn, mint))
    f = json.loads(r["features"] or "{}")
    status = {"signal": "🟢 SINYAL", "scored": "🏁 sinyal, sudah dinilai", "rejected": "❌ ditolak",
              "pending": "⏳ menunggu data"}.get(r["status"], r["status"])
    lines = [head, f"Status: {status}",
             f"Grad {usd(f.get('grad_mcap'))} → 15m {mx(f.get('m15'))} → 60m {mx(f.get('m60'))}"
             + (f" · lonjakan maks {mx(f['max_jump_1h'])}" if f.get("max_jump_1h") else "")]
    for why in json.loads(r["reasons"] or "[]"):
        lines.append(f"✖️ {e(why)}")
    for w in json.loads(r["warnings"] or "[]"):
        lines.append(f"⚠️ {e(w)}")
    if r["status"] == "signal" and prices.get(mint) and r["entry_price"]:
        lines.append(f"Sekarang {mx(prices[mint] / r['entry_price'])} dari harga masuk")
    o = json.loads(r["outcome"] or "{}")
    if o.get("max_mult") is not None:
        lines.append(f"72 jam: maks {mx(o['max_mult'])} · akhir {mx(o.get('last'))} · rencana {mx(o.get('plan'))}")
    if r["label"]:
        lines.append(f"Label: {e(r['label'])}")
    return "\n".join(lines + _cek_early(conn, mint))


def _cek_early(conn, mint: str) -> list[str]:
    out = []
    for r in conn.execute("SELECT * FROM live_signals WHERE mint = ? AND rule IN ('w1', 'n1', 'c1')", (mint,)):
        f = json.loads(r["features"] or "{}")
        st = "SINYAL" if r["status"] in ("signal", "scored") else "ditolak"
        out += ["", f"{ICON[r['rule']]} <b>{r['rule']}</b>: {st} · "
                    f"{datetime.fromisoformat(r['t0']).astimezone(WIB):%d %b %H:%M} WIB"
                + (f" · kata {e(f['word'])}" if f.get("word") else "")]
        out += [f"✖️ {e(w)}" for w in json.loads(r["reasons"] or "[]")]
    r = conn.execute("SELECT * FROM live_signals WHERE mint = ? AND rule = ?", (mint, early.RULE)).fetchone()
    if r is None:
        return out
    f = json.loads(r["features"] or "{}")
    st = "⚡ SINYAL" if r["status"] in ("signal", "scored") else "❌ ditolak"
    parts = [f"{k} {f[k]:.0f}%" if isinstance(f.get(k), float) else None for k in ("top10", "dev", "cluster")]
    out += ["", f"<b>{early.RULE}</b> (menit 1): {st}",
           f"holder {f.get('holders', '–')} · pro {f['pro'] * 100:.0f}%" if f.get("pro") is not None
           else f"holder {f.get('holders', '–')}"]
    if any(parts):
        out.append(" · ".join(p for p in parts if p))
    out += [f"✖️ {e(w)}" for w in json.loads(r["reasons"] or "[]")]
    return out


def cmd_help(conn, now, prices) -> str:
    return "\n".join(["🤖 <b>Command</b>"] + [f"/{c} — {e(d)}" for c, d in COMMANDS] +
                     ["", "Alert sinyal dan hasil 72 jam dikirim otomatis."])


def cmd_kanal(conn: sqlite3.Connection, now: datetime, prices: dict) -> str:
    from .calls import channel_stats
    st = channel_stats(conn)
    if not st:
        n = conn.execute("SELECT COUNT(*) FROM tg_calls").fetchone()[0]
        return f"Belum ada channel dengan ≥3 call ({n} call tercatat). Rapor muncul setelah call berumur 24 jam."
    lines = ["📣 <b>RAPOR CHANNEL CALL</b> (harga 24 jam setelah call)"]
    for c in st[:20]:
        lines.append(f"• <b>{e(c['chat'][:40])}</b>: {c['calls']} call"
                     + (f" · median {mx(c['x24_median'])} · {c['hit2'] * 100:.0f}% sempat ≥2x · "
                        f"{c['dump'] * 100:.0f}% turun &gt;50%" if c["graded"] else " · belum ada yang 24 jam"))
    return "\n".join(lines)


HANDLERS = {"kanal": cmd_kanal, "status": cmd_status, "sinyal": cmd_sinyal, "hasil": cmd_hasil, "cek": cmd_cek,
            "help": cmd_help, "start": cmd_help}


def answer(conn: sqlite3.Connection, text: str, now: datetime, prices: dict) -> str | None:
    """Reply text for one message, or None if it is not a command."""
    if not text.startswith("/"):
        return None
    head, _, arg = text.partition(" ")
    name = head[1:].split("@")[0].lower()
    fn = HANDLERS.get(name)
    if fn is None:
        return f"Command /{e(name)} tidak ada. Ketik /help."
    return fn(conn, now, prices, arg) if name == "cek" else fn(conn, now, prices)


def poll(conn: sqlite3.Connection, tg: Telegram, wait: float, prices: dict) -> int:
    """Long-poll for up to `wait` seconds, answering commands. Returns replies sent."""
    sent, end = 0, time.monotonic() + wait
    offset = int(db.get_state(conn, OFFSET_KEY) or 0)
    while (left := end - time.monotonic()) > 1:
        try:
            ups = tg._call("getUpdates", data={"offset": offset, "timeout": int(min(left, 25)),
                                               "allowed_updates": '["message"]'}, timeout=35)
        except Exception as ex:
            log.warning("getUpdates: %s", ex)
            time.sleep(min(left, 10))
            continue
        for u in ups:
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            chat = str((m.get("chat") or {}).get("id"))
            if chat not in tg.chat_ids or not m.get("text"):
                continue
            try:
                reply = answer(conn, m["text"].strip(), datetime.now(timezone.utc), prices)
            except Exception as ex:
                log.exception("command %r", m["text"])
                reply = f"Error: {e(type(ex).__name__)}"
            if reply:
                try:
                    tg._call("sendMessage", data={"chat_id": chat, "text": reply, "parse_mode": "HTML",
                                                  "reply_to_message_id": m["message_id"],
                                                  "disable_web_page_preview": "true"})
                    sent += 1
                except Exception as ex:
                    log.warning("reply: %s", ex)
        if ups:
            db.set_state(conn, OFFSET_KEY, str(offset))
    return sent
