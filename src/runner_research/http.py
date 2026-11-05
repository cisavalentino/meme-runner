"""One HTTP client for every API call: per-host rate limits, retry with
exponential backoff on 429/5xx, and secrets kept out of logs."""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

# calls per minute. GeckoTerminal public: 429 after ~3 rapid calls, so stay well
# under its nominal limit. DexScreener /tokens and /token-pairs: 300/min (docs).
# Public Solana RPC: 100 req / 10 s per IP, 40 / 10 s per method.
DEFAULT_LIMITS = {
    "api.geckoterminal.com": 6,             # measured: ~5–6/min sustained from this IP
    "api.coingecko.com": 25,
    "api.dexscreener.com": 240,
    "api.mainnet-beta.solana.com": 120,
    "mainnet.helius-rpc.com": 1500,         # Developer plan (429s above ~25/s); RR_RATE_MAINNET_HELIUS_RPC_COM
    "api.dune.com": 30,
}
SECRET_HEADERS = {"x-dune-api-key", "authorization", "x-cg-pro-api-key", "x-cg-demo-api-key"}


class RateLimiter:
    """Token bucket per host."""

    def __init__(self, per_minute: float, clock=time.monotonic, sleep=time.sleep):
        self.capacity = max(1.0, per_minute / 6)  # allow a short burst
        self.rate = per_minute / 60.0
        self.tokens = self.capacity
        self.clock, self.sleep = clock, sleep
        self.last = clock()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            while True:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                self.sleep((1 - self.tokens) / self.rate)


class ApiClient:
    def __init__(self, limits: dict[str, float] | None = None, max_retries: int = 5,
                 timeout: float = 30.0, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.limits = {**DEFAULT_LIMITS, **(limits or {})}
        self.buckets: dict[str, RateLimiter] = {}
        self._lock = threading.Lock()
        self.max_retries = max_retries
        self.sleep = sleep
        self.http = httpx.Client(timeout=timeout, transport=transport,
                                 headers={"User-Agent": "runner-research/0.1"})

    def _bucket(self, host: str) -> RateLimiter:
        with self._lock:  # called from transaction workers
            if host not in self.buckets:
                env = os.environ.get("RR_RATE_" + host.replace(".", "_").replace("-", "_").upper())
                self.buckets[host] = RateLimiter(float(env) if env else self.limits.get(host, 30), sleep=self.sleep)
            return self.buckets[host]

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        for attempt in range(self.max_retries + 1):
            self._bucket(host).acquire()
            try:
                r = self.http.request(method, url, **kw)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise
                log.warning("%s %s transport error %s (attempt %d)", method, url, type(e).__name__, attempt + 1)
            else:
                if r.status_code < 400 or (r.status_code < 500 and r.status_code != 429):
                    r.raise_for_status()
                    return r
                if attempt == self.max_retries:
                    r.raise_for_status()
                retry_after = r.headers.get("retry-after")
                log.warning("%s %s -> %d (attempt %d)", method, url, r.status_code, attempt + 1)
                if retry_after and retry_after.isdigit():
                    self.sleep(float(retry_after))
                    continue
            self.sleep(min(60.0, 2 ** attempt) + random.random())
        raise RuntimeError("unreachable")

    def get_json(self, url: str, **kw):
        return self.request("GET", url, **kw).json()

    def close(self) -> None:
        self.http.close()


_SECRET_QS = re.compile(r"((?:api[-_]?key|apikey|access[-_]?token|auth[-_]?token)=)[^&\s\"']+", re.I)


def redact_url(text: str) -> str:
    """Hide secrets passed as query parameters (e.g. Helius `?api-key=`)."""
    return _SECRET_QS.sub(r"\1***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if record.exc_info and record.exc_info[1] is not None:
            # format the traceback now so the secret never reaches a handler
            record.exc_text = redact_url(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        record.msg, record.args = redact_url(msg), None
        return True


def install_log_redaction() -> None:
    for h in logging.getLogger().handlers:
        h.addFilter(RedactFilter())


def redact_headers(headers: dict) -> dict:
    return {k: ("***" if k.lower() in SECRET_HEADERS else v) for k, v in headers.items()}
