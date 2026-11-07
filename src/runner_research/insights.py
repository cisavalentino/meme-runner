"""Trader-facing insights for the web page (`rr insights` -> insights.json).

Plain questions, each answered from labeled tokens:
- which tokens become runners (segments, launch tool, socials, narrative, first hour)
- second waves: from the state at T0+24h, how often does a token bounce again
- rugs: what they look like at T0, when they happen, and whether a trailing
  stop on 5-minute closes would have got out first

Everything is a rate with a 95% Wilson interval; n is shown so small groups
are not over-read. Rates are within labeled tokens (30% hash sample before
2026-11-05, all after).
"""
from __future__ import annotations

import itertools
import json
import re
import sqlite3
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config
from .analysis import wilson
from .labels import grid_t0
from .meta import twitter_kind

RUNNER = set(config.POSITIVE_LABELS)
RUG_FRAC = config.DEFAULT_LABEL_RULES.rug_liq_frac ** 2       # same proxy as the rug label
JUMP_FLAG = 50.0
STOPS = (0.3, 0.5, 0.7)

TOOLS = {"ipfs.io": "pump.fun (bawaan)", "metadata.j7tracker.io": "J7 Tracker", "meta.uxento.io": "Uxento",
         "m.rapidlaunch.io": "RapidLaunch", "storage.googleapis.com": "Google Storage",
         "gateway.irys.xyz": "Irys", "pump.mypinata.cloud": "Pinata (pump)", "gateway.pinata.cloud": "Pinata"}

THEMES = {
    "AI / agent": r"\b(ai|agent|agents|gpt|bot|agi|neural|llm|claude|openai|grok)\b",
    "Anjing": r"(dog|doge|shib|inu\b|puppy|pup\b|\bwif\b|corgi|shiba)",
    "Kucing": r"(\bcat\b|cats\b|kitty|kitten|meow|neko|popcat)",
    "Katak / Pepe": r"(pepe|frog|toad|kek\b)",
    "Politik": r"(trump|maga|biden|kamala|vance|obama|president|melania|barron)",
    "Elon / Musk": r"(elon|musk|tesla|spacex|doge ?gov|\bx ai\b)",
    "Saham / ETF": r"(stock|etf|dividend|nasdaq|nyse|tokenized|\binc\b|shares|treasury)",
    "Kripto / uang": r"(bitcoin|\bbtc\b|\bsol\b|solana|\beth\b|\busd|dollar|money|cash|rich|million|billion)",
    "Hewan lain": r"(monkey|\bape\b|penguin|\bbear\b|\bbull\b|hippo|goat|duck|fish|panda|hamster|rat\b|squirrel)",
    "Karakter CJK": r"[぀-ヿ一-鿿가-힯]",
}
STOPWORDS = {"the", "of", "and", "for", "coin", "token", "official", "on", "in", "to", "a", "is", "sol", "solana",
             "meme", "inu"}


@dataclass
class Tok:
    mint: str
    t0: datetime
    label: str
    seg: str
    feats: dict
    name: str = ""
    symbol: str = ""
    tool: str | None = None
    meta: dict | None = None
    path: dict = field(default_factory=dict)
    hot: bool = False            # name shares a word with a recent >= 3x token (narrative momentum)

    @property
    def runner(self) -> bool:
        return self.label in RUNNER

    @property
    def jump(self) -> bool:
        return (self.path.get("max_jump") or 0) >= JUMP_FLAG


def rate(k: int, n: int) -> dict:
    lo, hi = wilson(k, n)
    return {"n": n, "k": k, "rate": k / n if n else None, "lo": lo, "hi": hi}


def segment_of(feats: dict) -> str:
    m = feats.get("grad_mcap")
    if m is not None and m < config.HOLLOW_MCAP_USD:
        return "kosong"
    if feats.get("bonding_minutes") == 0:
        return "instan"
    return "normal"


def tool_of(uri: str | None) -> str:
    if not uri:
        return "tanpa uri"
    host = urllib.parse.urlparse(uri).hostname or ""
    return TOOLS.get(host, "host lain")


def path_stats(t0: datetime, rows: list[tuple]) -> dict:
    """rows: (ts, open, close, volume) in the 72h window, oldest first."""
    if not rows:
        return {}
    p0 = rows[0][1]
    if not p0 or p0 <= 0:
        return {}
    run_max, rug_i, prev_frac = 0.0, None, None
    max_jump = 0.0
    stops = {s: None for s in STOPS}
    day1_peak = c24 = None
    vol_20_24 = 0.0
    after_max = 0.0
    for i, (ts, o, c, v) in enumerate(rows):
        h = (ts - t0).total_seconds() / 3600
        if o and o > 0 and c >= 5 * p0:      # a jump to a new level, not a bounce off dust
            max_jump = max(max_jump, c / o)
        if rug_i is None and run_max > 0 and c < RUG_FRAC * run_max:
            rug_i, prev_frac = i, rows[i - 1][2] / run_max
        run_max = max(run_max, c)
        for s in STOPS:
            if stops[s] is None and c <= (1 - s) * run_max:
                stops[s] = (h, c / p0)
        if h < 24:
            day1_peak = max(day1_peak or 0, c)
            c24 = c
            if h >= 20:
                vol_20_24 += v or 0
        else:
            after_max = max(after_max, c)
    out = {"p0": p0, "peak_mult": run_max / p0, "max_jump": max_jump,
           "rug_h": (rows[rug_i][0] - t0).total_seconds() / 3600 if rug_i is not None else None,
           "rug_prev_frac": prev_frac,
           "stops": {s: v for s, v in stops.items()},
           "day1_peak": day1_peak / p0 if day1_peak else None, "c24": c24 / p0 if c24 else None,
           "vol_20_24": vol_20_24, "after24_max": after_max / p0 if after_max else None}
    return out


def load(conn: sqlite3.Connection) -> list[Tok]:
    q = """SELECT l.mint, t.graduated_at, t.canonical_pool, t.name, t.symbol, l.label, l.features,
                  m.uri, m.status, m.twitter, m.telegram, m.website, m.description
           FROM labels l JOIN tokens t USING (mint) LEFT JOIN token_meta m USING (mint)
           WHERE l.rule_version = ?"""
    toks, by_pool = [], {}
    for r in conn.execute(q, (config.RULE_VERSION,)):
        f = json.loads(r["features"] or "{}")
        meta = ({"twitter": r["twitter"], "telegram": r["telegram"], "website": r["website"],
                 "description": r["description"]} if r["status"] == "ok" else None)
        tk = Tok(r["mint"], grid_t0(datetime.fromisoformat(r["graduated_at"])), r["label"], segment_of(f), f,
                 r["name"] or "", r["symbol"] or "", tool_of(r["uri"]), meta)
        toks.append(tk)
        by_pool[r["canonical_pool"]] = tk
    cur = conn.execute("SELECT pool, ts, open, close, volume_usd FROM candles_5m ORDER BY pool, ts")
    for pool, grp in itertools.groupby(cur, key=lambda r: r[0]):
        tk = by_pool.get(pool)
        if tk is None:
            continue
        end = tk.t0 + timedelta(hours=config.WINDOW_HOURS)
        rows = [(ts, o, c, v) for _, s, o, c, v in grp
                if tk.t0 <= (ts := datetime.fromisoformat(s)) < end and c and c > 0]
        tk.path = path_stats(tk.t0, rows)
    return toks


# ---------------------------------------------------------------- sections

def group_rates(toks, key, outcome, order=None, min_n=1) -> list[dict]:
    by = defaultdict(list)
    for t in toks:
        g = key(t)
        if g is not None:
            by[g].append(outcome(t))
    keys = order or sorted(by, key=lambda g: -len(by[g]))
    return [{"group": g, **rate(sum(by[g]), len(by[g]))} for g in keys if len(by.get(g, [])) >= min_n]


def label_mix(toks, key, order) -> list[dict]:
    out = []
    for g in order:
        ts = [t for t in toks if key(t) == g]
        if not ts:
            continue
        c = Counter("runner" if t.runner or t.label == "second_wave" else
                    "rug" if t.label == "rug" else "dead" if t.label == "dead" else "lain" for t in ts)
        out.append({"group": g, "n": len(ts), **{k: c[k] / len(ts) for k in ("runner", "rug", "dead", "lain")}})
    return out


def bucketed(toks, value, outcome, edges, fmt) -> list[dict]:
    out = []
    for lo, hi in zip(edges, edges[1:]):
        ts = [t for t in toks if (v := value(t)) is not None and lo <= v < hi]
        if ts:
            out.append({"group": re.sub(r"(\d)\.(\d)", r"\1,\2", fmt(lo, hi)),
                        **rate(sum(outcome(t) for t in ts), len(ts))})
    return out


def usd(v: float) -> str:
    return f"${v / 1000:g} rb" if v >= 1000 else f"${v:g}"


def words(t: Tok) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", f"{t.name} {t.symbol}".lower())
            if len(w) >= 3 and w not in STOPWORDS and not w.isdigit()}


def themes(t: Tok) -> list[str]:
    text = f"{t.name} {t.symbol} {(t.meta or {}).get('description') or ''}".lower()
    return [k for k, rx in THEMES.items() if re.search(rx, text)]


def narrative_momentum(toks: list[Tok], window_h: int = 24, hot_mult: float = 3.0) -> dict:
    """A word is 'hot' at time T if a token whose name contains it was >= 3x at
    its own T0+1h within the previous 24h (knowable at T). Very common words
    (> 0.5% of tokens) are ignored so 'hot' is not just 'any token'."""
    core = sorted((t for t in toks if t.seg != "kosong"), key=lambda t: t.t0)
    df = Counter(w for t in core for w in words(t))
    common = {w for w, n in df.items() if n > 0.005 * len(core)}
    events = defaultdict(list)                               # word -> times it became hot
    for t in core:
        m1 = t.feats.get("mult_1h")
        if m1 is not None and m1 >= hot_mult:
            for w in words(t) - common:
                events[w].append(t.t0 + timedelta(hours=1))
    hot, cold = [], []
    for t in core:
        lo = t.t0 - timedelta(hours=window_h)
        t.hot = any(lo <= e < t.t0 for w in words(t) - common for e in events.get(w, ()))
        (hot if t.hot else cold).append(t)
    org = lambda ts: [t for t in ts if not t.jump]            # runner rates without pool injections
    res = {"window_h": window_h, "hot_mult": hot_mult,
           "hot": rate(sum(t.runner for t in org(hot)), len(org(hot))),
           "cold": rate(sum(t.runner for t in org(cold)), len(org(cold))),
           "hot_rug": rate(sum(t.label == "rug" for t in hot), len(hot)),
           "cold_rug": rate(sum(t.label == "rug" for t in cold), len(cold)),
           "examples": []}
    seen = set()
    for t in reversed(hot):
        if t.runner and not t.jump and t.symbol not in seen:
            seen.add(t.symbol)
            res["examples"].append({"name": t.name, "symbol": t.symbol, "date": t.t0.date().isoformat(),
                                    "peak": round(t.path.get("peak_mult") or 0, 1)})
        if len(res["examples"]) >= 8:
            break
    return res


def top_words(toks, min_n=40, k=12) -> dict:
    core = [t for t in toks if t.seg != "kosong" and not t.jump]
    by = defaultdict(list)
    for t in core:
        for w in words(t):
            by[w].append(t.runner)
    rows = [{"group": w, **rate(sum(v), len(v))} for w, v in by.items() if len(v) >= min_n]
    rows.sort(key=lambda r: -r["lo"])
    best = rows[:k]
    worst = sorted(rows, key=lambda r: r["hi"])[:k]
    return {"min_n": min_n, "best": best, "worst": worst}


def first_hour_ladder(core) -> list[dict]:
    edges = [0, 0.1, 0.3, 0.6, 1.0, 1.5, 2, 3, 5, 1e9]
    lab = lambda lo, hi: f"< {hi}x" if lo == 0 else (f"≥ {lo}x" if hi > 1e8 else f"{lo}–{hi}x")
    return bucketed(core, lambda t: t.feats.get("mult_1h"), lambda t: t.runner, edges, lab)


def second_wave(toks) -> dict:
    """State at T0+24h -> does price make a new move >= 3x the 24h close in
    hours 24–72? Population: core tokens alive at 24h (not rugged before) that
    peaked >= 2x on day 1."""
    pop = [t for t in toks if t.seg != "kosong" and not t.jump and t.path.get("day1_peak")
           and t.path["day1_peak"] >= 2 and (t.path.get("rug_h") is None or t.path["rug_h"] >= 24)
           and t.path.get("c24")]
    bounce = lambda t: (t.path.get("after24_max") or 0) >= 3 * t.path["c24"]
    dd = lambda t: t.path["c24"] / t.path["day1_peak"]
    return {
        "population": len(pop),
        "overall": rate(sum(map(bounce, pop)), len(pop)),
        "label_second_wave": sum(t.label == "second_wave" for t in toks),
        "by_drawdown": bucketed(pop, dd, bounce, [0, 0.1, 0.25, 0.5, 0.75, 1.01],
                                lambda lo, hi: f"{int(lo * 100)}–{min(100, int(hi * 100))}% dari puncak"),
        "by_volume": bucketed(pop, lambda t: t.path["vol_20_24"], bounce, [0, 100, 1000, 5000, 20000, 1e12],
                              lambda lo, hi: f"≥ {usd(lo)}" if hi > 1e11 else f"{usd(lo)}–{usd(hi)}"),
        "by_day1_peak": bucketed(pop, lambda t: t.path["day1_peak"], bounce, [2, 5, 10, 30, 1e9],
                                 lambda lo, hi: f"≥ {lo}x" if hi > 1e8 else f"{lo}–{hi}x"),
    }


def warning_signs(toks) -> list[dict]:
    """Each sign: rug rate (and runner rate) of tokens that show it vs the rest."""
    signs = [
        ("Graduation instan (dibuat & lulus di detik yang sama)", "t0", lambda t: t.seg == "instan"),
        ("Creator baru pertama kali graduate", "t0", lambda t: t.feats.get("creator_prior_graduated") == 0),
        ("Nama meniru narasi yang sedang panas (24 jam terakhir)", "t0", lambda t: t.hot),
        ("Metadata di Irys", "t0", lambda t: t.tool == "Irys"),
        ("Tanpa link X di metadata", "t0", lambda t: t.meta is not None and not t.meta.get("twitter")),
        ("Lonjakan ≥ 50x dalam satu candle 5 menit", "live", lambda t: t.jump),
        ("Sudah turun ke < 0,3x dalam 15 menit", "live", lambda t: (t.feats.get("mult_15m") or 9) < 0.3),
    ]
    out = []
    for name, when, f in signs:
        pool = [t for t in toks if t.seg != "kosong" and (t.meta is not None or "X" not in name)]
        yes = [t for t in pool if f(t)]
        no = [t for t in pool if not f(t)]
        if len(yes) < 30:
            continue
        out.append({"sign": name, "when": when, "share": len(yes) / len(pool),
                    "rug_yes": rate(sum(t.label == "rug" for t in yes), len(yes)),
                    "rug_no": rate(sum(t.label == "rug" for t in no), len(no)),
                    "runner_yes": rate(sum(t.runner for t in yes), len(yes))})
    return out


def rugs(toks) -> dict:
    core = list(toks)
    rg = [t for t in core if t.label == "rug" and t.path.get("rug_h") is not None]
    hist_edges = [0, 15 / 60, 1, 6, 24, 72.01]
    names = ["≤ 15 menit", "15–60 menit", "1–6 jam", "6–24 jam", "24–72 jam"]
    timing = [{"group": n, "n": sum(lo <= t.path["rug_h"] < hi for t in rg)}
              for n, lo, hi in zip(names, hist_edges, hist_edges[1:])]
    instant = [t for t in rg if (t.path.get("rug_prev_frac") or 0) >= 0.5]
    stops = []
    for s in STOPS:
        before = [t for t in rg if t.path["stops"].get(s) and t.path["stops"][s][0] < t.path["rug_h"]]
        exit_mults = sorted(t.path["stops"][s][1] for t in before)
        run = [t for t in core if t.runner and not t.jump and t.path.get("stops")]
        cut = [t for t in run if t.path["stops"].get(s) and t.path["stops"][s][0] < (t.path.get("rug_h") or 1e9)]
        run_exit = sorted(t.path["stops"][s][1] for t in cut)
        stops.append({"stop": s, "rugs_exited_before": rate(len(before), len(rg)),
                      "rug_exit_median_mult": exit_mults[len(exit_mults) // 2] if exit_mults else None,
                      "runners_stopped": rate(len(cut), len(run)),
                      "runner_exit_median_mult": run_exit[len(run_exit) // 2] if run_exit else None,
                      "runner_peak_median": sorted(t.path["peak_mult"] for t in run)[len(run) // 2] if run else None})
    return {"n": len(rg), "timing": timing, "one_candle": rate(len(instant), len(rg)), "stops": stops,
            "signs": warning_signs(toks),
            "by_segment": group_rates(core, lambda t: t.seg, lambda t: t.label == "rug",
                                      order=["normal", "instan", "kosong"]),
            "by_bonding": bucketed([t for t in core if t.seg != "kosong"], lambda t: t.feats.get("bonding_minutes"),
                                   lambda t: t.label == "rug", [0, 0.001, 1, 5, 30, 240, 1e12],
                                   lambda lo, hi: "0 (instan)" if hi <= 0.001 else
                                   (f"≥ {lo:g} mnt" if hi > 1e11 else f"{lo:g}–{hi:g} mnt")),
            "by_tool": group_rates(core, lambda t: t.tool, lambda t: t.label == "rug", min_n=100),
            "by_creator": bucketed(core, lambda t: t.feats.get("creator_prior_graduated"), lambda t: t.label == "rug",
                                   [0, 1, 3, 10, 1e9], lambda lo, hi: "pertama kali" if lo == 0 else
                                   (f"≥ {lo:g} kali" if hi > 1e8 else f"{lo:g}–{hi - 1:g} kali sebelumnya"))}


def socials(toks) -> dict:
    have = [t for t in toks if t.meta is not None and t.seg != "kosong" and not t.jump]
    kind = lambda t: {"none": "tanpa X", "profile": "akun X", "tweet": "link tweet", "community": "X community",
                      "other": "link lain"}[twitter_kind(t.meta.get("twitter"))]
    return {
        "n": len(have),
        "twitter_runner": group_rates(have, kind, lambda t: t.runner, min_n=50),
        "twitter_rug": group_rates(have, kind, lambda t: t.label == "rug", min_n=50),
        "website_runner": group_rates(have, lambda t: "ada website" if t.meta.get("website") else "tanpa website",
                                      lambda t: t.runner),
        "telegram_runner": group_rates(have, lambda t: "ada telegram" if t.meta.get("telegram") else "tanpa telegram",
                                       lambda t: t.runner),
        "n_socials_runner": group_rates(have, lambda t: f"{sum(bool(t.meta.get(k)) for k in ('twitter', 'telegram', 'website'))} sosial",
                                        lambda t: t.runner, order=["0 sosial", "1 sosial", "2 sosial", "3 sosial"]),
    }


def build(conn: sqlite3.Connection, now: datetime) -> dict:
    toks = load(conn)
    core = [t for t in toks if t.seg != "kosong" and not t.jump]
    seg_order = ["normal", "instan", "kosong"]
    th = defaultdict(list)
    for t in core:
        for name in themes(t) or ["(tanpa tema)"]:
            th[name].append(t)
    theme_rows = [{"group": k, **rate(sum(t.runner for t in v), len(v)),
                   "rug": rate(sum(t.label == "rug" for t in v), len(v))["rate"]}
                  for k, v in th.items() if len(v) >= 50]
    theme_rows.sort(key=lambda r: -r["rate"])
    n_runner = sum(t.runner for t in toks)
    momentum = narrative_momentum(toks)
    return {
        "generated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rule_version": config.RULE_VERSION,
        "overview": {"tokens": len(toks), "from": min(t.t0 for t in toks).date().isoformat(),
                     "to": max(t.t0 for t in toks).date().isoformat(),
                     "runner": rate(n_runner, len(toks)),
                     "runner_core": rate(sum(t.runner for t in core), len(core)),
                     "rug": rate(sum(t.label == "rug" for t in toks), len(toks)),
                     "dead": rate(sum(t.label == "dead" for t in toks), len(toks)),
                     "jumps": sum(t.jump for t in toks),
                     "meta_coverage": sum(t.meta is not None for t in toks)},
        "segments": {"mix": label_mix([t for t in toks if not t.jump], lambda t: t.seg, seg_order),
                     "runner": group_rates([t for t in toks if not t.jump], lambda t: t.seg, lambda t: t.runner,
                                           order=seg_order)},
        "tools": {"mix": label_mix([t for t in toks if not t.jump], lambda t: t.tool,
                                   [g["group"] for g in group_rates(toks, lambda t: t.tool, lambda t: 0, min_n=100)]),
                  "runner": group_rates(core, lambda t: t.tool, lambda t: t.runner, min_n=100)},
        "first_hour": {"ladder": first_hour_ladder(core),
                       "m15": bucketed(core, lambda t: t.feats.get("mult_15m"), lambda t: t.runner,
                                       [0, 0.3, 0.8, 1.2, 2, 1e9],
                                       lambda lo, hi: f"< {hi}x" if lo == 0 else (f"≥ {lo}x" if hi > 1e8 else f"{lo}–{hi}x"))},
        "timing": {"hour": bucketed(core, lambda t: t.feats.get("hour_utc"), lambda t: t.runner,
                                    [0, 4, 8, 12, 16, 20, 24], lambda lo, hi: f"{(lo + 7) % 24:02d}–{(hi + 7) % 24:02d} WIB")},
        "narrative": {"themes": theme_rows, "words": top_words(toks), "momentum": momentum},
        "socials": socials(toks),
        "second_wave": second_wave(toks),
        "rugs": rugs(toks),
    }


def write(conn: sqlite3.Connection, out: str, now: datetime) -> None:
    from pathlib import Path
    data = build(conn, now)
    p = Path(out)
    p.mkdir(parents=True, exist_ok=True)
    tmp = p / "insights.json.tmp"
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=str))
    tmp.replace(p / "insights.json")
