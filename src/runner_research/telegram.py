"""Telegram alerts for the live test (Bot API, HTML parse mode).

.env: TELEGRAM_BOT_TOKEN=123:abc   TELEGRAM_CHAT_ID=-100123,456 (one or more)
The token is part of the API path; logs redact it (http.redact_url).
"""
from __future__ import annotations

import html
import logging
import os

import httpx

log = logging.getLogger(__name__)
API = "https://api.telegram.org"
CAPTION_MAX = 1024


class Telegram:
    def __init__(self, token: str, chat_ids: list[str], client: httpx.Client | None = None):
        self.token, self.chat_ids = token, chat_ids
        self.client = client or httpx.Client(timeout=30)

    def _call(self, method: str, **kw) -> dict:
        r = self.client.post(f"{API}/bot{self.token}/{method}", **kw)
        js = r.json()
        if not js.get("ok"):
            raise RuntimeError(f"telegram {method}: {js.get('description')}")
        return js["result"]

    def send(self, text: str, photo: bytes | None = None, reply_to: dict[str, int] | None = None) -> dict[str, int]:
        """Send to every chat; returns chat id -> message id. A photo is sent
        with the text as caption when it fits, else the text goes alone."""
        out = {}
        for chat in self.chat_ids:
            base = {"chat_id": chat, "parse_mode": "HTML"}
            if reply_to and reply_to.get(chat):
                base.update(reply_to_message_id=reply_to[chat], allow_sending_without_reply="true")
            try:
                if photo and len(text) <= CAPTION_MAX:
                    res = self._call("sendPhoto", data={**base, "caption": text},
                                     files={"photo": ("token.jpg", photo)})
                else:
                    res = self._call("sendMessage", data={**base, "text": text, "disable_web_page_preview": "true"})
            except Exception as e:
                if not photo:
                    raise
                log.warning("sendPhoto failed (%s), sending text only", e)
                res = self._call("sendMessage", data={**base, "text": text, "disable_web_page_preview": "true"})
            out[chat] = res["message_id"]
        return out

    def chats(self) -> list[dict]:
        """Chats that recently messaged the bot (to find TELEGRAM_CHAT_ID)."""
        seen = {}
        for u in self._call("getUpdates"):
            m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            c = m.get("chat") or {}
            if c.get("id") is not None:
                seen[c["id"]] = {"id": c["id"], "type": c.get("type"),
                                 "name": c.get("title") or c.get("username") or c.get("first_name")}
        return list(seen.values())


def from_env(client: httpx.Client | None = None, need_chat: bool = True) -> Telegram | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or (need_chat and not chats):
        return None
    return Telegram(token, chats, client)


def e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)
