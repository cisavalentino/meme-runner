import httpx

from runner_research.http import ApiClient, RateLimiter, redact_headers


def test_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429 if calls["n"] < 3 else 200, json={"ok": True})

    c = ApiClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert c.get_json("https://api.geckoterminal.com/x") == {"ok": True}
    assert calls["n"] == 3


def test_404_not_retried():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(404)

    c = ApiClient(transport=httpx.MockTransport(handler), sleep=lambda s: None)
    try:
        c.get_json("https://api.dexscreener.com/x")
    except httpx.HTTPStatusError:
        pass
    assert calls["n"] == 1


def test_rate_limiter_waits():
    t = {"now": 0.0}
    slept = []
    rl = RateLimiter(30, clock=lambda: t["now"], sleep=lambda s: (slept.append(s), t.__setitem__("now", t["now"] + s)))
    for _ in range(10):
        rl.acquire()
    assert sum(slept) > 0  # burst of 5 then throttled to 0.5/s


def test_redact():
    assert redact_headers({"X-Dune-Api-Key": "k", "Accept": "a"}) == {"X-Dune-Api-Key": "***", "Accept": "a"}
