"""Off-chain token metadata (pump.fun JSON at the mint's metadata uri).

Two steps: `fetch_uris` reads the uri from the Token-2022 metadata extension
(getMultipleAccounts, 100 mints per call); `fetch_json` downloads the JSON.
ipfs.io / dweb.link only serve a service-worker page now, so IPFS uris are
rewritten to another public gateway.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import db

log = logging.getLogger(__name__)
IPFS_GATEWAYS = ("https://4everland.io/ipfs/", "https://ipfs.filebase.io/ipfs/", "https://gw.ipfs-lens.dev/ipfs/",
                 "https://gateway.pinata.cloud/ipfs/")
_IPFS = re.compile(r"^(?:https?://[^/]+/ipfs/|ipfs://)([A-Za-z0-9]{40,}(?:/[^?#]*)?)")


def gateway_urls(uri: str, start: int = 0) -> list[str]:
    """IPFS uris -> every gateway, rotated by `start`; other uris as they are."""
    m = _IPFS.match(uri or "")
    if not m:
        return [uri]
    gws = IPFS_GATEWAYS[start % len(IPFS_GATEWAYS):] + IPFS_GATEWAYS[:start % len(IPFS_GATEWAYS)]
    return [g + m.group(1) for g in gws]


def uri_from_account(account: dict | None) -> str | None:
    info = ((account or {}).get("data") or {}).get("parsed", {}).get("info", {})
    for ext in info.get("extensions") or []:
        if ext.get("extension") == "tokenMetadata":
            return (ext.get("state") or {}).get("uri") or None
    return None


def twitter_kind(url: str | None) -> str:
    """none | profile | tweet | community | other: what the X link points to."""
    u = (url or "").strip().lower()
    if not u:
        return "none"
    if "x.com" not in u and "twitter.com" not in u:
        return "other"
    if "/communities/" in u:
        return "community"
    if "/status/" in u:
        return "tweet"
    return "profile"


def parse_meta(js: dict) -> dict:
    s = lambda k: (str(js.get(k)).strip() or None) if js.get(k) not in (None, "") else None
    return {"description": (s("description") or "")[:1000] or None, "twitter": s("twitter"),
            "telegram": s("telegram"), "website": s("website"), "image": s("image"),
            "community": s("coin_community")}


def fetch_uris(conn: sqlite3.Connection, rpc, mints: list[str]) -> int:
    rows = []
    for i in range(0, len(mints), 100):
        chunk = mints[i:i + 100]
        for m, acc in zip(chunk, rpc.accounts(chunk, encoding="jsonParsed")):
            uri = uri_from_account(acc)
            rows.append({"mint": m, "uri": uri, "status": None if uri else "no_uri", "fetched_at": db.now_iso()})
        db.upsert(conn, "token_meta", rows, key=("mint",))
        rows = []
    return len(mints)


FIELDS = ("status", "description", "twitter", "telegram", "website", "image", "community")


def _get(client: httpx.Client, mint: str, uri: str) -> dict:
    return {"mint": mint, **dict.fromkeys(FIELDS), **_get1(client, mint, uri)}


def _get1(client: httpx.Client, mint: str, uri: str) -> dict:
    """Try each gateway once (rate limits are per gateway); 429 everywhere
    stays retryable."""
    res = {"status": "error"}
    for url in gateway_urls(uri, start=sum(map(ord, mint))):
        try:
            r = client.get(url)
            if r.status_code == 200:
                return {"status": "ok", **parse_meta(r.json())}
            res = {"status": f"http_{r.status_code}"}
            if r.status_code not in (429, 403, 502, 503, 504):
                return res
        except (httpx.HTTPError, json.JSONDecodeError, ValueError, AttributeError) as e:
            res = {"status": "error", "description": type(e).__name__}
    return res


def fetch_json(conn: sqlite3.Connection, workers: int = 8, limit: int | None = None) -> int:
    todo = conn.execute("SELECT mint, uri FROM token_meta WHERE uri IS NOT NULL AND (status IS NULL OR status = 'http_429')"
                        " ORDER BY substr(mint, 3, 8)"
                        + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    done = 0
    with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "runner-research"}) as client, \
            ThreadPoolExecutor(workers) as ex:
        batch = []
        for res in ex.map(lambda r: _get(client, r["mint"], r["uri"]), todo):
            batch.append({**res, "fetched_at": db.now_iso()})
            if len(batch) >= 200:
                db.upsert(conn, "token_meta", batch, key=("mint",))
                done += len(batch); batch = []
                log.info("metadata %d/%d", done, len(todo))
        db.upsert(conn, "token_meta", batch, key=("mint",))
        done += len(batch)
    return done
