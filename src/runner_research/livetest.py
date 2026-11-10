"""Live test of the runner-search rule (strategy.LIVE_RULE) on new graduations.

Per graduation, with data the collector already has:
  T0      pool reserves from the migration tx (first price, in the quote token),
          metadata JSON (X link, launch tool), bonding time, narrative copy
  T0+15m  DexScreener snapshot price / graduation price
  T0+60m  same at 1h = entry price; then one GeckoTerminal OHLCV call for the
          one-candle jump check (only for tokens that passed everything else)
The graduation price is pool_quote / pool_base x the quote's USD price
(priceUsd / priceNative of a snapshot), so a late first candle can't shift it.

A failed check rejects at once (every check is final when it is known).
Signals are scored from their 72h candles with the same exit simulation as
the backtest; rejected tokens get their label too, to see what the rule missed.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

from . import config, db, strategy
from .insights import tool_of, words
from .meta import FIELDS, _get, twitter_kind, uri_from_account

log = logging.getLogger(__name__)

RULE = strategy.LIVE_RULE
SNAP_WAIT = timedelta(minutes=30)       # collector gives up on a snapshot after this
GIVE_UP = timedelta(minutes=120)        # no 1h price by then -> decide with what exists
SCORE_AFTER = timedelta(hours=config.WINDOW_HOURS, minutes=30)
SCORE_GIVE_UP = timedelta(days=7)
HOT_MULT = 3.0
# r2 on Oct 2026 (rule fitted on Aug–Sep), strategy.backtest; see insights page.
BACKTEST = {"period": "Okt 2026", "n": 161, "per_day": 4.9, "runner_label": 0.211, "hit2": 0.286,
            "hit5": 0.081, "rug_after": 0.006, "plan_mean": 0.80, "base_runner": 0.017}


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- inputs

def quote_usd(conn: sqlite3.Connection, quote_mint: str | None, at: datetime) -> float | None:
    """USD price of the pool's quote token near `at`, from any snapshot of a
    pool with that quote (priceUsd / priceNative)."""
    if not quote_mint:
        return None
    r = conn.execute(
        """SELECT s.price / s.price_native FROM snapshots s JOIN tokens t USING (mint)
           JOIN pools p ON p.pool = t.canonical_pool
           WHERE p.quote_mint = ? AND s.price > 0 AND s.price_native > 0
           ORDER BY ABS(julianday(s.taken_at) - julianday(?)) LIMIT 1""", (quote_mint, _iso(at))).fetchone()
    return r[0] if r else None


def grad_native(tok) -> float | None:
    """First pool price in quote units (pool reserves after CreatePool)."""
    return tok["pool_quote"] / tok["pool_base"] if tok["pool_base"] and tok["pool_quote"] is not None else None


def snap(conn: sqlite3.Connection, mint: str, offset: str) -> tuple[float | None, float | None]:
    """(price USD, price in quote) of a snapshot."""
    r = conn.execute("SELECT price, price_native FROM snapshots WHERE mint = ? AND offset = ?", (mint, offset)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def mult(conn, tok, offset: str, gn: float | None, q: float | None) -> float | None:
    """Snapshot price / graduation price, in quote units when possible."""
    usd, nat = snap(conn, tok["mint"], offset)
    if gn and nat:
        return nat / gn
    if gn and q and usd:
        return usd / (gn * q)
    return None


def ensure_meta(conn: sqlite3.Connection, client: httpx.Client, rpc, mint: str) -> dict | None:
    """token_meta row, fetching uri (RPC) and JSON (IPFS gateways) if missing."""
    row = conn.execute("SELECT * FROM token_meta WHERE mint = ?", (mint,)).fetchone()
    if row is None and rpc is not None:
        uri = uri_from_account(rpc.accounts([mint], encoding="jsonParsed")[0])
        db.upsert(conn, "token_meta", [{"mint": mint, "uri": uri, "status": None if uri else "no_uri"}], key=("mint",))
        row = conn.execute("SELECT * FROM token_meta WHERE mint = ?", (mint,)).fetchone()
    stale = row is not None and (row["fetched_at"] or "") < _iso(datetime.now(timezone.utc) - timedelta(minutes=2))
    if row is not None and row["uri"] and (row["status"] is None or (row["status"] in ("http_429", "error") and stale)):
        res = _get(client, mint, row["uri"])
        db.upsert(conn, "token_meta", [{**res, "uri": row["uri"]}], key=("mint",))
        row = conn.execute("SELECT * FROM token_meta WHERE mint = ?", (mint,)).fetchone()
    return dict(row) if row is not None else None


def _words(name: str | None, symbol: str | None) -> set[str]:
    return words(SimpleNamespace(name=name or "", symbol=symbol or ""))


def common_words(conn: sqlite3.Connection, now: datetime) -> set[str]:
    """Words in > 0.5% of last week's graduations (not a narrative)."""
    rows = conn.execute("SELECT name, symbol FROM tokens WHERE graduated_at >= ?",
                        (_iso(now - timedelta(days=7)),)).fetchall()
    df = Counter(w for r in rows for w in _words(r[0], r[1]))
    return {w for w, n in df.items() if n > 0.005 * len(rows) and n >= 5}


def hot_events(conn: sqlite3.Connection, now: datetime, common: set[str]) -> list[tuple[datetime, set[str]]]:
    """(T0+1h, words) of every non-hollow graduation in the last ~26h that was
    >= 3x at its own T0+1h (1h snapshot, quote units)."""
    out = []
    for r in conn.execute(
            """SELECT t.name, t.symbol, t.graduated_at, p.pool_base, p.pool_quote, s.price, s.price_native
               FROM tokens t JOIN pools p ON p.pool = t.canonical_pool
               JOIN snapshots s ON s.mint = t.mint AND s.offset = '1h'
               WHERE t.graduated_at >= ? AND p.pool_base > 0 AND s.price_native > 0
                 AND s.price > 0  -- DexScreener drops priceUsd for dead pools""",
            (_iso(now - timedelta(hours=27)),)):
        gn = r["pool_quote"] / r["pool_base"]
        if gn <= 0 or gn * (r["price"] / r["price_native"]) * config.PUMPFUN_SUPPLY < config.HOLLOW_MCAP_USD:
            continue
        if r["price_native"] / gn >= HOT_MULT:
            out.append((datetime.fromisoformat(r["graduated_at"]) + timedelta(hours=1),
                        _words(r["name"], r["symbol"]) - common))
    return out


def hot_copy(events, t0: datetime, ws: set[str]) -> list[str]:
    lo = t0 - timedelta(hours=24)
    return sorted({w for at, hw in events if lo <= at < t0 for w in hw & ws})


def first_hour_jump(gecko, pool: str, t0: datetime) -> tuple[float | None, int]:
    """Largest close/open of one 5m candle that closed by T0+60m, and the candle count."""
    cs = gecko.ohlcv_5m(pool, limit=100)
    end = t0 + timedelta(minutes=strategy.ENTRY_MIN)
    js = [c["close"] / c["open"] for c in cs
          if c["open"] and c["open"] > 0 and c["close"] and c["ts"] + timedelta(minutes=5) <= end]
    return (max(js) if js else None), len(js)


# ---------------------------------------------------------------- decide

def features(conn, tok, meta, common, events) -> dict:
    t0 = datetime.fromisoformat(tok["graduated_at"])
    gn = grad_native(tok)
    q = quote_usd(conn, tok["quote_mint"], t0)
    gp = gn * q if gn is not None and q else None
    m = meta or {}
    copy = hot_copy(events, t0, _words(tok["name"], tok["symbol"]) - common)
    return {
        "grad_price": gp, "grad_mcap": gp * config.PUMPFUN_SUPPLY if gp is not None else None,
        "grad_source": "pool" if gp is not None else None, "quote_mint": tok["quote_mint"], "quote_usd": q,
        "bonding_minutes": tok["bonding_minutes"],
        "uri": m.get("uri"), "meta_status": m.get("status"),
        "tool": tool_of(m.get("uri")), "x_kind": twitter_kind(m.get("twitter")) if m.get("status") == "ok" else None,
        "twitter": m.get("twitter"), "community": m.get("community"), "telegram": m.get("telegram"),
        "website": m.get("website"),
        "hot_copy": bool(copy), "hot_words": copy,
        "m15": mult(conn, tok, "15m", gn, q), "m60": mult(conn, tok, "1h", gn, q),
        "price_60m": snap(conn, tok["mint"], "1h")[0],
    }


def decide(f: dict, t0: datetime, now: datetime) -> tuple[str, list[str]]:
    """'pending' while a check still waits for data; else 'signal' or 'rejected'."""
    ok, why = strategy.evaluate(f, RULE)
    late = now >= t0 + GIVE_UP
    waiting = []
    if f.get("grad_price") is None and not late:
        waiting = ["graduation kosong", "15m <", "60m di luar"]
    if f.get("m15") is None and now < t0 + timedelta(minutes=15) + SNAP_WAIT and not late:
        waiting.append("15m <")
    if f.get("m60") is None and now < t0 + timedelta(minutes=60) + SNAP_WAIT and not late:
        waiting.append("60m di luar")
    if f.get("meta_status") is None and not late:
        waiting.append("tanpa link X")                      # metadata not read yet
    final = [r for r in why if not any(r.startswith(w) for w in waiting)]
    if final:
        return "rejected", final
    if why or f.get("m60") is None:
        return "pending", []
    return ("signal" if ok else "rejected"), why


def step(conn: sqlite3.Connection, gecko, rpc, client: httpx.Client, now: datetime) -> dict:
    """Evaluate every graduation of the last 2h that is not decided yet."""
    since = db.get_state(conn, "livetest_since")
    if since is None:
        since = _iso(now)
        db.set_state(conn, "livetest_since", since)
    lo = max(since, _iso(now - GIVE_UP - timedelta(minutes=5)))
    toks = conn.execute(
        """SELECT t.*, p.quote_mint, p.pool_base, p.pool_quote FROM tokens t
           LEFT JOIN pools p ON p.pool = t.canonical_pool
           WHERE t.graduated_at >= ? AND t.graduated_at <= ?
             AND NOT EXISTS (SELECT 1 FROM live_signals l WHERE l.mint = t.mint AND l.rule = ?
                             AND l.status != 'pending')
           ORDER BY t.graduated_at""", (lo, _iso(now), RULE.version)).fetchall()
    common = common_words(conn, now) if toks else set()
    events = hot_events(conn, now, common) if toks else []
    out = Counter()
    for tok in toks:
        t0 = datetime.fromisoformat(tok["graduated_at"])
        try:
            meta = ensure_meta(conn, client, rpc, tok["mint"])
        except Exception as e:
            log.warning("meta %s: %s", tok["mint"], e)
            meta = None
        f = features(conn, tok, meta, common, events)
        status, why = decide(f, t0, now)
        if status == "signal" and gecko is not None and tok["canonical_pool"]:
            try:
                f["max_jump_1h"], f["candles_1h"] = first_hour_jump(gecko, tok["canonical_pool"], t0)
            except Exception as e:
                log.warning("ohlcv %s: %s", tok["mint"], e)
                if now < t0 + GIVE_UP:
                    status = "pending"                  # retry next loop
            if status == "signal":
                status, why = decide(f, t0, now)
        row = {"mint": tok["mint"], "rule": RULE.version, "t0": tok["graduated_at"], "status": status,
               "reasons": json.dumps(why, ensure_ascii=False),
               "warnings": json.dumps(strategy.warnings(f), ensure_ascii=False),
               "features": json.dumps(f, ensure_ascii=False, default=str),
               "entry_price": f.get("price_60m") if status == "signal" else None,
               "entry_at": _iso(t0 + timedelta(minutes=strategy.ENTRY_MIN)) if status == "signal" else None,
               "updated_at": _iso(now)}
        db.upsert(conn, "live_signals", [row], key=("mint", "rule"))
        out[status] += 1
    return dict(out)


# ---------------------------------------------------------------- score

def score(conn: sqlite3.Connection, now: datetime) -> int:
    """Signals whose 72h window has candles: outcome from the entry price.
    Labels (written daily by `rr label`) are copied for signals and rejects."""
    n = 0
    for s in conn.execute(
            """SELECT l.mint, l.t0, l.entry_price, t.canonical_pool FROM live_signals l JOIN tokens t USING (mint)
               WHERE l.rule = ? AND l.status = 'signal' AND l.t0 <= ?""",
            (RULE.version, _iso(now - SCORE_AFTER))).fetchall():
        t0 = datetime.fromisoformat(s["t0"])
        rows = [(datetime.fromisoformat(r[0]), r[1], r[2]) for r in conn.execute(
            "SELECT ts, open, close FROM candles_5m WHERE pool = ? AND ts >= ? AND ts < ? AND close > 0 ORDER BY ts",
            (s["canonical_pool"], _iso(t0 - timedelta(minutes=5)),
             _iso(t0 + timedelta(hours=config.WINDOW_HOURS))))]
        if not rows and now - t0 < SCORE_GIVE_UP:
            continue
        res = strategy.outcome(t0, rows, s["entry_price"]) if rows else {}
        conn.execute("UPDATE live_signals SET status = 'scored', outcome = ?, updated_at = ? WHERE mint = ? AND rule = ?",
                     (json.dumps(res or {"missing": "candles"}), _iso(now), s["mint"], RULE.version))
        n += 1
    conn.execute(
        """UPDATE live_signals SET label = (SELECT b.label FROM labels b WHERE b.mint = live_signals.mint
                                           AND b.rule_version = ?)
           WHERE rule = ? AND label IS NULL AND status IN ('scored', 'signal', 'rejected')""",
        (config.RULE_VERSION, RULE.version))
    conn.commit()
    return n


# ---------------------------------------------------------------- holder metrics

def detail(conn: sqlite3.Connection, mint: str) -> dict:
    """1h holder snapshot (GeckoTerminal), DexScreener paid profile and
    first-hour trade features of one token; missing values stay None."""
    s = conn.execute("SELECT holders, top10_pct, dev_pct, dexs_profile, dexs_boosts FROM snapshots "
                     "WHERE mint = ? AND offset = '1h'", (mint,)).fetchone()
    f = conn.execute("SELECT features FROM features_1h WHERE mint = ?", (mint,)).fetchone()
    tf = json.loads(f[0]) if f else {}
    ok = tf.get("trades_complete")
    return {"holders": s["holders"] if s else None,
            "top10": s["top10_pct"] if s and s["top10_pct"] is not None and s["top10_pct"] <= 100 else None,
            "dev": s["dev_pct"] if s else None,
            "dex_paid": bool(s["dexs_profile"]) if s and s["dexs_profile"] is not None else None,
            "boosts": s["dexs_boosts"] if s else None,
            "sniper": tf.get("sniper_share") if ok else None,
            "creator_sold": (tf.get("creator_sell_usd_1h") or 0) > 0 if ok else None,
            "traders_1h": tf.get("traders_1h") if ok else None}


METRICS = [
    # key, title, buckets as (label, test)
    ("top10", "Top 10 holder (jam 1)", [("< 30%", lambda v: v < 30), ("30–50%", lambda v: 30 <= v < 50),
                                        ("50–70%", lambda v: 50 <= v < 70), ("≥ 70%", lambda v: v >= 70)]),
    ("dev", "Dev holding (jam 1)", [("0%", lambda v: v == 0), ("0–5%", lambda v: 0 < v < 5),
                                     ("5–20%", lambda v: 5 <= v < 20), ("≥ 20%", lambda v: v >= 20)]),
    ("holders", "Jumlah holder (jam 1)", [("< 100", lambda v: v < 100), ("100–300", lambda v: 100 <= v < 300),
                                           ("300–1.000", lambda v: 300 <= v < 1000), ("≥ 1.000", lambda v: v >= 1000)]),
    ("sniper", "Sniper: porsi volume beli jam 1 di 2 menit pertama", [("< 10%", lambda v: v < .1), ("10–25%", lambda v: .1 <= v < .25),
                                                        ("25–50%", lambda v: .25 <= v < .5), ("≥ 50%", lambda v: v >= .5)]),
    ("dex_paid", "Dex paid (profil DexScreener)", [("ya", lambda v: v is True), ("tidak", lambda v: v is False)]),
    ("creator_sold", "Creator menjual di jam 1", [("ya", lambda v: v is True), ("tidak", lambda v: v is False)]),
]
MIN_N = 30          # a bucket below this is shown as "belum cukup"


def holder_metrics(conn: sqlite3.Connection) -> dict:
    """Runner / rug rate by holder metric, for labeled graduations that have
    the 1h holder snapshot (collected for every graduation since Nov 2026).
    Hollow and instant graduations are left out (they are rejected anyway)."""
    pos = config.POSITIVE_LABELS
    toks = []
    for mint, lab, feats in conn.execute(
            """SELECT l.mint, l.label, l.features FROM labels l
               WHERE l.rule_version = ? AND EXISTS (SELECT 1 FROM snapshots s WHERE s.mint = l.mint
                     AND s.offset = '1h' AND (s.holders IS NOT NULL OR s.dexs_profile IS NOT NULL))""",
            (config.RULE_VERSION,)):
        f = json.loads(feats or "{}")
        if (f.get("grad_mcap") or 0) < config.HOLLOW_MCAP_USD or f.get("bonding_minutes") == 0:
            continue
        toks.append((lab, detail(conn, mint)))
    out = []
    for key, title, buckets in METRICS:
        rows = []
        for name, test in buckets:
            sel = [lab for lab, d in toks if d[key] is not None and test(d[key])]
            n = len(sel)
            rows.append({"bucket": name, "n": n,
                         "runner": round(sum(l in pos for l in sel) / n, 3) if n else None,
                         "rug": round(sum(l == "rug" for l in sel) / n, 3) if n else None})
        out.append({"key": key, "title": title, "rows": rows})
    n_all = len(toks)
    return {"n": n_all, "min_n": MIN_N, "metrics": out,
            "base_runner": round(sum(l in pos for l, _ in toks) / n_all, 3) if n_all else None,
            "base_rug": round(sum(l == "rug" for l, _ in toks) / n_all, 3) if n_all else None}


# ---------------------------------------------------------------- page

def _r(v, k=2):
    return round(v, k) if isinstance(v, (int, float)) else v


def build(conn: sqlite3.Connection, now: datetime, prices: dict[str, float] | None = None) -> dict:
    prices = prices or {}
    rows = conn.execute(
        """SELECT l.*, t.name, t.symbol FROM live_signals l JOIN tokens t USING (mint)
           WHERE l.rule = ? ORDER BY l.t0 DESC""", (RULE.version,)).fetchall()

    def base(r):
        f = json.loads(r["features"] or "{}")
        return {"mint": r["mint"], "name": r["name"], "symbol": r["symbol"], "t0": r["t0"],
                "grad_mcap": _r(f.get("grad_mcap"), 0), "m15": _r(f.get("m15")), "m60": _r(f.get("m60")),
                "jump": _r(f.get("max_jump_1h"), 1), "tool": f.get("tool"), "x_kind": f.get("x_kind"),
                "twitter": f.get("twitter"), "community": f.get("community"),
                "warnings": json.loads(r["warnings"] or "[]"), "reasons": json.loads(r["reasons"] or "[]"),
                "label": r["label"], "status": r["status"]}

    def with_detail(d):
        d.update(detail(conn, d["mint"]))
        return d

    signals, watching, rejected = [], [], []
    for r in rows:
        d = base(r)
        if r["status"] in ("signal", "scored"):
            o = json.loads(r["outcome"] or "{}")
            e = r["entry_price"]
            d.update({"entry_mcap": _r(e * config.PUMPFUN_SUPPLY, 0) if e else None, "entry_at": r["entry_at"],
                      "now_mult": _r(prices[r["mint"]] / e) if e and prices.get(r["mint"]) else None,
                      "max_mult": _r(o.get("max_mult")), "last": _r(o.get("last")), "rugged": o.get("rugged"),
                      "plan": _r(o.get("plan")), "tp13": _r(o.get("x_tp13_nosl")), "tp2": _r(o.get("x_tp2_sl06"))})
            signals.append(with_detail(d))
        elif r["status"] == "pending":
            watching.append(with_detail(d) if len(watching) < 80 else d)
        else:
            rejected.append(d)

    scored = [s for s in signals if s["max_mult"] is not None]
    labeled = [s for s in signals if s["label"]]
    rate = lambda xs, p: (sum(map(p, xs)) / len(xs)) if xs else None
    since = db.get_state(conn, "livetest_since")
    days = max(1 / 24, (now - datetime.fromisoformat(since)).total_seconds() / 86400) if since else None
    day_ago = _iso(now - timedelta(hours=24))
    rej24 = [r for r in rejected if r["t0"] >= day_ago]
    reasons = Counter(re.sub(r"\d+x dalam", "≥20x dalam", x) if x.startswith("lonjakan") else x
                      for r in rej24 for x in r["reasons"])
    rej_lab = [r for r in rejected if r["label"]]
    near = [r for r in rejected if len(r["reasons"]) == 1 and not r["reasons"][0].startswith("graduation")][:60]
    pos = config.POSITIVE_LABELS
    return {
        "generated_utc": _iso(now), "since": since, "rule": RULE.as_dict(), "backtest": BACKTEST,
        "summary": {
            "signals": len(signals), "per_day": _r(len(signals) / days) if days else None,
            "scored": len(scored), "labeled": len(labeled), "watching": len(watching),
            "evaluated": len(rows),
            "runner_label": _r(rate(labeled, lambda s: s["label"] in pos), 3),
            "hit2": _r(rate(scored, lambda s: s["max_mult"] >= 2), 3),
            "hit5": _r(rate(scored, lambda s: s["max_mult"] >= 5), 3),
            "rug_after": _r(rate(scored, lambda s: bool(s["rugged"])), 3),
            "plan_mean": _r(rate(scored, lambda s: s["plan"] or 0), 3),
            "rejected_labeled": len(rej_lab),
            "rejected_runner": _r(rate(rej_lab, lambda s: s["label"] in pos), 3),
            "rejected_runners": [{"mint": r["mint"], "name": r["name"], "symbol": r["symbol"], "t0": r["t0"],
                                  "label": r["label"], "reasons": r["reasons"]}
                                 for r in rej_lab if r["label"] in pos][:30],
        },
        "signals": signals, "watching": watching[:80],
        "holder_metrics": holder_metrics(conn),
        "reasons_24h": reasons.most_common(), "rejected_24h": len(rej24), "near_misses": near,
    }


def write(conn: sqlite3.Connection, out: str | Path, now: datetime, prices: dict[str, float] | None = None) -> int:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    js = build(conn, now, prices)
    tmp = out / "live.json.tmp"
    tmp.write_text(json.dumps(js, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(out / "live.json")
    (out / "live.html").write_text((Path(__file__).parent / "web" / "live.html").read_text())
    return len(js["signals"])


def open_prices(conn: sqlite3.Connection, api) -> dict[str, float]:
    """Current DexScreener price of signals still inside their 72h window."""
    from .webpage import fetch_pairs
    mints = [r[0] for r in conn.execute(
        "SELECT mint FROM live_signals WHERE rule = ? AND status = 'signal'", (RULE.version,))]
    if not mints:
        return {}
    pairs = fetch_pairs(api, mints)
    return {m: float(p["priceUsd"]) for m, p in pairs.items() if p.get("priceUsd")}


def backfill_reserves(conn: sqlite3.Connection, rpc, since: datetime) -> int:
    """Pool reserves (and the right pool) for recent graduations stored without them."""
    from .ingest import repair_pools
    mints = [r[0] for r in conn.execute(
        """SELECT t.mint FROM tokens t LEFT JOIN pools p ON p.pool = t.canonical_pool
           WHERE t.graduated_at >= ? AND p.pool_base IS NULL AND t.migrate_tx IS NOT NULL""", (_iso(since),))]
    return repair_pools(conn, rpc, mints)["reserves"] if mints else 0
