import json
from datetime import timedelta
from urllib.parse import parse_qs

import httpx

from runner_research import bot
from runner_research.telegram import Telegram
from test_livetest import T0, add, run


def test_commands_answer_from_live_signals(conn):
    add(conn, "A", name="Melon Musk")
    add(conn, "B", name="Dog", t0=T0 + timedelta(minutes=1), bonding=0)        # instant -> rejected
    run(conn, T0 + timedelta(minutes=63))
    now = T0 + timedelta(minutes=70)
    st = bot.answer(conn, "/status", now, {})
    assert "1 sinyal" in st and "LIVE TEST" in st
    entry = conn.execute("SELECT entry_price FROM live_signals WHERE mint = 'A'").fetchone()[0]
    sig = bot.answer(conn, "/sinyal@mybot", now, {"A": entry * 2})
    assert "SINYAL AKTIF" in sig and "2.00x" in sig and "<code>A</code>" in sig
    assert "SINYAL" in bot.answer(conn, "/cek A", now, {})
    assert "graduation instan" in bot.answer(conn, "/cek B", now, {})
    assert "tidak ada di data" in bot.answer(conn, "/cek ZZZ", now, {})
    assert "Pakai" in bot.answer(conn, "/cek", now, {})
    assert "Belum ada sinyal yang selesai" in bot.answer(conn, "/hasil", now, {})
    assert "/status" in bot.answer(conn, "/help", now, {})
    assert "tidak ada" in bot.answer(conn, "/wallet", now, {})
    assert bot.answer(conn, "halo", now, {}) is None


def test_register_and_poll_only_answer_own_chat(conn):
    calls = []

    def handler(req):
        method = req.url.path.rsplit("/", 1)[1]
        body = json.loads(req.content) if req.headers.get("content-type", "").startswith("application/json") \
            else {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        calls.append((method, body))
        if method == "getUpdates":
            res = [] if int(body["offset"]) else [
                {"update_id": 5, "message": {"message_id": 1, "chat": {"id": 1}, "text": "/help"}},
                {"update_id": 6, "message": {"message_id": 2, "chat": {"id": 99}, "text": "/help"}}]
            return httpx.Response(200, json={"ok": True, "result": res})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 3} if method == "sendMessage" else True})

    tg = Telegram("123:abc", ["1"], httpx.Client(transport=httpx.MockTransport(handler)))
    bot.register(tg)
    assert [c["command"] for c in calls[-1][1]["commands"]] == [c for c, _ in bot.COMMANDS]
    assert bot.poll(conn, tg, 3, {}) == 1
    sends = [b for m, b in calls if m == "sendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == "1"
    from runner_research import db
    assert db.get_state(conn, bot.OFFSET_KEY) == "7"
