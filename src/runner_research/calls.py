"""Telegram call channels -> signal c1 (`rr calls`, pm2 rr-calls).

A Telegram *user* account (Telethon; TG_API_ID / TG_API_HASH from
my.telegram.org, session in data/tg_user.session, chmod 600) reads every
channel/group it has joined. Each Solana token address in a message
(plain, or in pump.fun / dexscreener / gmgn / birdeye links) that has a
DexScreener pair is stored in tg_calls with the price at the call; prices
at +1h/+6h/+24h and the highest price seen grade each channel (/kanal).

c1 fires when CALL_MIN_CHANNELS different chats call the same token within
CALL_WINDOW and it passes: liquidity, trader fees (fees.check) and the top
holder checks (trend.holder_check). Scored 72h later like w1/n1.
Login without a terminal prompt: `rr tg-login --phone +62...`, then
`rr tg-login --code 12345 [--password ...]`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median

from . import db, fees, trend
from .webpage import fetch_pairs

log = logging.getLogger(__name__)
SESSION = os.environ.get("TG_SESSION", "data/tg_user")
CALL_MIN_CHANNELS = int(os.environ.get("CALL_MIN_CHANNELS", "2"))
CALL_WINDOW = timedelta(minutes=30)
CHECKPOINTS = {"price_1h": timedelta(hours=1), "price_6h": timedelta(hours=6), "price_24h": timedelta(hours=24)}
MIN_LIQ = 10_000
ADDR = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
NOT_TOKENS = {fees.WSOL, "11111111111111111111111111111111", "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
              "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}
_iso = trend._iso


def addresses(text: str) -> list[str]:
    seen = []
    for a in ADDR.findall(text or ""):
        if a not in NOT_TOKENS and a not in seen:
            seen.append(a)
    return seen[:5]


def record(conn: sqlite3.Connection, api, chat_id: int, chat_title: str, msg_id: int, posted: datetime,
           text: str) -> list[str]:
    """Store the tokens a message calls; returns the mints stored."""
    cands = addresses(text)
    if not cands:
        return []
    pairs = fetch_pairs(api, cands)
    trend.add_ext(conn, pairs, "telegram")
    rows = []
    for m in cands:
        p = pairs.get(m)
        if not p or not p.get("priceUsd"):
            continue                                   # wallet / pair address / not a traded token
        rows.append({"chat_id": chat_id, "msg_id": msg_id, "mint": m, "chat_title": chat_title,
                     "posted_at": _iso(posted), "text": (text or "")[:500], "price0": float(p["priceUsd"]),
                     "mcap0": p.get("marketCap") or p.get("fdv"), "max_seen": float(p["priceUsd"]),
                     "checked_at": _iso(posted)})
    if rows:
        db.upsert(conn, "tg_calls", rows, key=("chat_id", "msg_id", "mint"))
    return [r["mint"] for r in rows]


def update_prices(conn: sqlite3.Connection, api, now: datetime) -> int:
    """Checkpoint prices and the running max for calls of the last 25h."""
    rows = conn.execute("SELECT * FROM tg_calls WHERE posted_at >= ?", (_iso(now - timedelta(hours=25)),)).fetchall()
    if not rows:
        return 0
    pairs = fetch_pairs(api, sorted({r["mint"] for r in rows}))
    for r in rows:
        p = pairs.get(r["mint"])
        if not p or not p.get("priceUsd"):
            continue
        px, posted = float(p["priceUsd"]), datetime.fromisoformat(r["posted_at"])
        upd = {"max_seen": max(r["max_seen"] or 0, px), "checked_at": _iso(now)}
        for col, dt in CHECKPOINTS.items():
            if r[col] is None and now >= posted + dt:
                upd[col] = px
        conn.execute(f"UPDATE tg_calls SET {', '.join(f'{k} = ?' for k in upd)} "
                     "WHERE chat_id = ? AND msg_id = ? AND mint = ?", (*upd.values(), r["chat_id"], r["msg_id"], r["mint"]))
    conn.commit()
    return len(rows)


def signal_step(conn: sqlite3.Connection, api, rpc, now: datetime) -> dict:
    """c1: a token called by >= CALL_MIN_CHANNELS chats within CALL_WINDOW."""
    out = {"candidates": 0, "signal": 0}
    for r in conn.execute(
            """SELECT mint, COUNT(DISTINCT chat_id) n, GROUP_CONCAT(DISTINCT chat_title) chats, MIN(posted_at) first
               FROM tg_calls WHERE posted_at >= ? GROUP BY mint HAVING n >= ?""",
            (_iso(now - CALL_WINDOW), CALL_MIN_CHANNELS)).fetchall():
        if trend._done(conn, r["mint"], "c1", now):
            continue
        tok = trend._tok(conn, r["mint"])
        if tok is None:
            continue
        out["candidates"] += 1
        p = fetch_pairs(api, [r["mint"]]).get(r["mint"])
        f = {**(trend._pair_features(p) if p else {}), "channels": r["n"], "chats": (r["chats"] or "").split(","),
             "first_call": r["first"],
             "age_days": round((now - datetime.fromisoformat(tok["graduated_at"])).total_seconds() / 86400, 1)
             if tok["graduated_at"] else None}
        why = [] if p and trend._liq(p) >= MIN_LIQ else [f"likuiditas < ${MIN_LIQ / 1000:.0f}k"]
        if not why:
            f.update(trend._fees(api, p, r["mint"]))
            why = fees.check(f)
        if not why:
            try:
                hf, why = trend.holder_check(conn, rpc, tok)
                f.update(hf)
            except trend.QuotaExhausted:
                out["quota"] = True
                continue
            except Exception as ex:
                log.warning("holders %s: %s", r["mint"], ex)
                why = [f"error RPC: {type(ex).__name__}"]
        trend._store(conn, r["mint"], "c1", now, "rejected" if why else "signal", why, f)
        out["signal"] += not why
    return out


def channel_stats(conn: sqlite3.Connection, min_calls: int = 3) -> list[dict]:
    """Per chat: how its calls did after 24h (price vs call price) and the best move seen."""
    by = {}
    for r in conn.execute("SELECT * FROM tg_calls WHERE price0 > 0"):
        by.setdefault(r["chat_id"], []).append(r)
    out = []
    for cid, rs in by.items():
        done = [r for r in rs if r["price_24h"] is not None]
        out.append({"chat": rs[-1]["chat_title"], "calls": len(rs), "graded": len(done),
                    "x24_median": median(r["price_24h"] / r["price0"] for r in done) if done else None,
                    "hit2": sum((r["max_seen"] or 0) >= 2 * r["price0"] for r in done) / len(done) if done else None,
                    "dump": sum(r["price_24h"] < 0.5 * r["price0"] for r in done) / len(done) if done else None})
    return sorted([o for o in out if o["calls"] >= min_calls], key=lambda o: -(o["x24_median"] or 0))


# ---------------------------------------------------------------- Telethon

def _client():
    from telethon import TelegramClient
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("set TG_API_ID and TG_API_HASH in .env (my.telegram.org -> API development tools)")
    return TelegramClient(SESSION, int(api_id), api_hash)


def _secure_session() -> None:
    path = SESSION + ".session"
    if os.path.exists(path):
        os.chmod(path, 0o600)


async def login(conn: sqlite3.Connection, phone: str | None, code: str | None, password: str | None) -> str:
    client = _client()
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            return f"sudah login sebagai {me.first_name} (id {me.id})"
        if phone:
            sent = await client.send_code_request(phone)
            db.set_state(conn, "tg_login", json.dumps({"phone": phone, "hash": sent.phone_code_hash}))
            return "kode dikirim ke Telegram Anda; lanjut: rr tg-login --code <kode> [--password <2FA>]"
        st = json.loads(db.get_state(conn, "tg_login") or "{}")
        if not code or not st:
            return "jalankan dulu: rr tg-login --phone +62..."
        from telethon.errors import SessionPasswordNeededError
        try:
            await client.sign_in(st["phone"], code, phone_code_hash=st["hash"])
        except SessionPasswordNeededError:
            if not password:
                return "akun memakai verifikasi 2 langkah: ulangi dengan --code <kode> --password <sandi>"
            await client.sign_in(password=password)
        db.set_state(conn, "tg_login", None)
        me = await client.get_me()
        return f"login berhasil: {me.first_name} (id {me.id})"
    finally:
        _secure_session()
        await client.disconnect()


async def run(db_path: str | None, api, rpc, tg_bot, http_client) -> None:
    """Listen to every joined channel/group; grade calls and check c1 every minute.
    All database work runs on one worker thread with its own connection."""
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from telethon import events
    local = threading.local()
    pool = ThreadPoolExecutor(1, initializer=lambda: setattr(local, "conn", db.connect(db_path)))

    async def on_db(fn, *args):
        return await asyncio.get_running_loop().run_in_executor(pool, lambda: fn(local.conn, *args))

    client = _client()
    await client.start()
    _secure_session()
    me = await client.get_me()
    bot_ids = {int(c) for c in (tg_bot.chat_ids if tg_bot else []) if c.lstrip("-").isdigit()}
    print(f"telegram calls: listening as {me.first_name}", flush=True)

    @client.on(events.NewMessage(incoming=True))
    async def on_msg(ev):
        if ev.is_private or ev.chat_id in bot_ids:
            return
        try:
            chat = await ev.get_chat()
            title = getattr(chat, "title", None) or str(ev.chat_id)
            got = await on_db(record, api, ev.chat_id, title, ev.id, ev.date.astimezone(timezone.utc),
                              ev.raw_text or "")
            if got:
                print(json.dumps({"call": title, "mints": got}, ensure_ascii=False), flush=True)
        except Exception:
            log.exception("call message")

    async def loop():
        while True:
            now = datetime.now(timezone.utc)
            out = {"at": _iso(now)}
            for name, fn in (("prices", lambda c: update_prices(c, api, now)),
                             ("c1", lambda c: signal_step(c, api, rpc, now)),
                             ("alerts", lambda c: trend.send_alerts(c, tg_bot, http_client, rpc, now, rules=("c1",))
                              if tg_bot else None)):
                try:
                    out[name] = await on_db(fn)
                except Exception as ex:
                    log.exception("calls %s", name)
                    out[name] = f"error: {type(ex).__name__}"
            print(json.dumps(out, ensure_ascii=False), flush=True)
            await asyncio.sleep(60)

    asyncio.create_task(loop())
    await client.run_until_disconnected()
