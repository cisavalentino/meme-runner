"""Runner-search rule: one definition used by the backtest and the live test.

The rule looks only at what is knowable at T0+60m:
  T0 (graduation): segment, launch tool, X link in metadata, narrative copy
  T0+15m / T0+60m: price vs graduation price, largest one-candle jump
Entry = last 5m close at T0+60m. Outcomes are measured from that entry.

`evaluate(f, rule)` -> (passed, reasons). `f` is a dict of features; the same
keys are produced by `features_from_candles` (history and live).
"""
from __future__ import annotations

import itertools
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from . import config
from .labels import grid_t0
from .meta import twitter_kind

ENTRY_MIN = 60
PLAN = "half2_half5_sl06_be"
RUG_FRAC = config.DEFAULT_LABEL_RULES.rug_liq_frac ** 2


@dataclass(frozen=True)
class Rule:
    version: str = "r1"
    min_grad_mcap: float = config.HOLLOW_MCAP_USD     # skip hollow graduations
    skip_instant: bool = True                          # skip create+graduate in the same second
    require_x: bool = True                             # metadata has an X link
    skip_tools: tuple = ("Irys",)
    skip_hot_copy: bool = True                         # name rides a word that ran >= 3x in the last 24h
    min_m15: float = 0.8                               # price at 15m vs graduation
    min_m60: float = 1.5                               # price at 60m vs graduation (entry zone)
    max_m60: float = 5.0
    max_jump: float = 20.0                             # largest close/open of one 5m candle, first hour

    def as_dict(self) -> dict:
        return asdict(self)


R1 = Rule()


def evaluate(f: dict, rule: Rule = R1) -> tuple[bool, list[str]]:
    """Fails list the first reasons; an unknown value fails its check."""
    why = []
    gm = f.get("grad_mcap")
    if gm is None or gm < rule.min_grad_mcap:
        why.append("graduation kosong")
    if rule.skip_instant and f.get("bonding_minutes") == 0:
        why.append("graduation instan")
    if rule.require_x and f.get("x_kind") in (None, "none", "other"):
        why.append("tanpa link X")
    if f.get("tool") in rule.skip_tools:
        why.append(f"metadata di {f.get('tool')}")
    if rule.skip_hot_copy and f.get("hot_copy"):
        why.append("meniru narasi panas")
    m15, m60, jump = f.get("m15"), f.get("m60"), f.get("max_jump_1h")
    if m15 is None or m15 < rule.min_m15:
        why.append(f"15m < {rule.min_m15}x")
    if m60 is None or not (rule.min_m60 <= m60 < rule.max_m60):
        why.append(f"60m di luar {rule.min_m60}–{rule.max_m60}x")
    if jump is not None and jump >= rule.max_jump:
        why.append(f"lonjakan {jump:.0f}x dalam 1 candle")
    return (not why, why)


def first_hour(t0: datetime, rows: list[tuple]) -> dict:
    """rows: (ts, open, close) oldest first, ts >= T0. Features at T0+60m."""
    if not rows or not rows[0][1] or rows[0][1] <= 0:
        return {}
    p0 = rows[0][1]
    close_at = lambda m: next((c for ts, o, c in reversed(rows) if ts + timedelta(minutes=5) <= t0 + timedelta(minutes=m)), None)
    c15, c60 = close_at(15), close_at(ENTRY_MIN)
    jump = max((c / o for ts, o, c in rows if o and o > 0 and ts + timedelta(minutes=5) <= t0 + timedelta(minutes=ENTRY_MIN)),
               default=None)
    return {"grad_price": p0, "grad_mcap": p0 * config.PUMPFUN_SUPPLY,
            "m15": c15 / p0 if c15 else None, "m60": c60 / p0 if c60 else None,
            "entry": c60, "max_jump_1h": jump}


EXITS = {
    # name: (take-profit levels as (multiple, fraction)), stop-loss multiple, move stop to entry after first TP
    "tp2_sl06": (((2.0, 1.0),), 0.6, False),
    "tp15_sl07": (((1.5, 1.0),), 0.7, False),
    "half2_half5_sl06_be": (((2.0, 0.5), (5.0, 0.5)), 0.6, True),
    "third2_third5_runner_sl06_be": (((2.0, 1 / 3), (5.0, 1 / 3)), 0.6, True),
    "hold72": ((), 0.0, False),
    "tp3_sl05": (((3.0, 1.0),), 0.5, False),
    "tp5_sl05": (((5.0, 1.0),), 0.5, False),
    "tp10_sl03": (((10.0, 1.0),), 0.3, False),
    "ladder_2_4_8_16_sl05_be": (((2.0, .25), (4.0, .25), (8.0, .25), (16.0, .25)), 0.5, True),
    "tp13_nosl": (((1.3, 1.0),), 0.0, False),
    "tp2_nosl": (((2.0, 1.0),), 0.0, False),
}


def simulate(after: list[tuple], entry: float, tps, sl: float, breakeven: bool) -> float:
    """5m closes after entry; fills at the close that crosses a level (no
    slippage). Returns the multiple of the stake at the end."""
    left, got, stop, i = 1.0, 0.0, sl, 0
    for row in after:
        c = row[1]
        hi = row[2] if len(row) > 2 and row[2] else c       # limit sells fill at the level when the high touches it
        m, mh = c / entry, max(c, hi) / entry
        while i < len(tps) and mh >= tps[i][0] and left > 1e-9:
            frac = min(tps[i][1], left)
            got += frac * tps[i][0]
            left -= frac
            i += 1
            if breakeven:
                stop = max(stop, 1.0)
        if left <= 1e-9:
            return got
        if stop and m <= stop:
            return got + left * m
    return got + left * (after[-1][1] / entry)


def outcome(t0: datetime, rows: list[tuple], entry: float) -> dict:
    """From the entry close at T0+60m to T0+72h: best multiple, rug after
    entry (close below 0.25% of the running max), and each exit plan in EXITS."""
    after = [(ts, c) for ts, o, c in rows if ts + timedelta(minutes=5) > t0 + timedelta(minutes=ENTRY_MIN)]
    if not entry or not after:
        return {}
    best = run = entry
    rugged = False
    for ts, c in after:
        if run > 0 and c < RUG_FRAC * run:
            rugged = True
        run = max(run, c)
        best = max(best, c)
    res = {"max_mult": best / entry, "rugged": rugged, "last": after[-1][1] / entry}
    for name, (tps, sl, be) in EXITS.items():
        res["x_" + name] = simulate(after, entry, tps, sl, be)
    res["plan"] = res["x_" + PLAN]
    return res


def load_history(conn: sqlite3.Connection) -> list[dict]:
    """Per labeled token: first-hour features, T0 features and outcome."""
    from .insights import load as load_toks, narrative_momentum
    toks = {t.mint: t for t in load_toks(conn)}
    narrative_momentum(list(toks.values()))                  # sets t.hot (knowable at T0)
    pool_mint = {r[0]: r[1] for r in conn.execute("SELECT canonical_pool, mint FROM tokens")}
    out = []
    cur = conn.execute("SELECT pool, ts, open, close FROM candles_5m ORDER BY pool, ts")
    for pool, grp in itertools.groupby(cur, key=lambda r: r[0]):
        t = toks.get(pool_mint.get(pool))
        if t is None:
            continue
        end = t.t0 + timedelta(hours=config.WINDOW_HOURS)
        rows = [(ts, o, c) for _, s, o, c in grp if t.t0 <= (ts := datetime.fromisoformat(s)) < end and c and c > 0]
        f = first_hour(t.t0, rows)
        if not f:
            continue
        f.update({"mint": t.mint, "t0": t.t0, "label": t.label, "symbol": t.symbol,
                  "bonding_minutes": t.feats.get("bonding_minutes"), "tool": t.tool, "hot_copy": t.hot,
                  "x_kind": twitter_kind(t.meta.get("twitter")) if t.meta is not None else None})
        f.update(outcome(t.t0, rows, f["entry"]))
        out.append(f)
    return out


def summarize(rows: list[dict]) -> dict:
    rs = [r for r in rows if r.get("max_mult") is not None]
    n = len(rs)
    if not n:
        return {"n": 0}
    med = lambda xs: sorted(xs)[len(xs) // 2]
    plan = [r["plan"] for r in rs]
    return {"n": n,
            "hit2": sum(r["max_mult"] >= 2 for r in rs) / n,
            "hit5": sum(r["max_mult"] >= 5 for r in rs) / n,
            "rug_after": sum(r["rugged"] for r in rs) / n,
            "runner_label": sum(r["label"] in config.POSITIVE_LABELS for r in rs) / n,
            "plan_mean": sum(plan) / n, "plan_median": med(plan),
            "plan_win": sum(p > 1 for p in plan) / n}


def backtest(rows: list[dict], rule: Rule, split: datetime) -> dict:
    sel = [r for r in rows if evaluate(r, rule)[0]]
    days = lambda rs: max(1, (max(r["t0"] for r in rs) - min(r["t0"] for r in rs)).days) if rs else 1
    tr = [r for r in sel if r["t0"] < split]
    te = [r for r in sel if r["t0"] >= split]
    base_te = [r for r in rows if r["t0"] >= split and r.get("entry")]
    return {"rule": rule.as_dict(), "train": summarize(tr), "test": summarize(te),
            "test_all_tokens": summarize(base_te),
            "signals_per_day_test": len(te) / days([r for r in rows if r["t0"] >= split])}
