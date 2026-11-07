import json
from datetime import timedelta

import httpx

from runner_research import alerts, db
from runner_research.http import redact_url
from runner_research.telegram import Telegram
from test_livetest import T0, add, run


class FakeTG:
    chat_ids = ["1"]

    def __init__(self):
        self.sent = []

    def send(self, text, photo=None, reply_to=None):
        self.sent.append((text, photo, reply_to))
        return {"1": len(self.sent)}


class FakeApi:
    def get_json(self, url, **kw):
        return [{"baseToken": {"address": "A"}, "pairAddress": "P_A", "priceUsd": "0.0001", "marketCap": 100000,
                 "liquidity": {"usd": 25000}, "volume": {"h1": 30700}, "priceChange": {"m5": 8.6, "h1": 120},
                 "txns": {"h1": {"buys": 216, "sells": 133}}}]


class FakeGeckoInfo:
    def token_info(self, mint):
        return {"holders": 223, "top10_pct": 58.1, "dev_pct": 0.0, "mint_auth": None, "freeze_auth": None}


def test_signal_then_result_alert(conn, monkeypatch):
    pool = add(conn, "A", name="Melon Musk")
    run(conn, T0 + timedelta(minutes=62))
    tg = FakeTG()
    now = T0 + timedelta(minutes=63)
    monkeypatch.setattr(alerts, "fetch_image", lambda client, url: b"img")
    with httpx.Client() as c:
        assert alerts.send_alerts(conn, tg, FakeApi(), FakeGeckoInfo(), c, now) == {"signal": 1, "result": 0}
        assert alerts.send_alerts(conn, tg, FakeApi(), FakeGeckoInfo(), c, now)["signal"] == 0   # once only
    text, photo, _ = tg.sent[0]
    assert "SINYAL RUNNER" in text and "Melon Musk" in text and "MC $100.0K · LP $25.0K" in text
    assert "🅑216 Ⓢ133 · 1.62×" in text and "Top10 58.1%" in text and "60m 2.00x" in text and photo == b"img"
    # scored after 72h -> result as a reply
    conn.execute("UPDATE live_signals SET status = 'scored', outcome = ? WHERE mint = 'A'",
                 (json.dumps({"max_mult": 3.0, "last": 0.5, "rugged": False, "plan": 1.25, "x_tp13_nosl": 1.3}),))
    with httpx.Client() as c:
        assert alerts.send_alerts(conn, tg, FakeApi(), None, c, now + timedelta(hours=72))["result"] == 1
    text, _, reply = tg.sent[1]
    assert "HASIL 72 JAM" in text and "Maks 3.00x" in text and reply == {"1": 1}


def test_old_signals_are_not_alerted(conn):
    add(conn, "A")
    run(conn, T0 + timedelta(minutes=62))
    with httpx.Client() as c:
        assert alerts.send_alerts(conn, FakeTG(), FakeApi(), None, c, T0 + timedelta(hours=5))["signal"] == 0


def test_telegram_send_photo_falls_back_to_text():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("sendPhoto"):
            return httpx.Response(200, json={"ok": False, "description": "bad photo"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    tg = Telegram("123:abc", ["9"], httpx.Client(transport=httpx.MockTransport(handler)))
    assert tg.send("<b>x</b>", b"img") == {"9": 7}
    assert calls == ["/bot123:abc/sendPhoto", "/bot123:abc/sendMessage"]
    assert "123:abc" not in redact_url("https://api.telegram.org/bot123:abc/sendPhoto")
