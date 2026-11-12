"""Backtest: LP into a runner's PumpSwap pool after its ATH (`rr lp-backtest`).

Question: when a graduate has already run (peak >= MIN_PEAK x its first
price) and then fallen DROP below its running high, does providing
liquidity earn more in fees than it loses to the price falling?

Model (full-range constant-product LP, 50/50 at entry, small stake):
  pool TVL     PumpSwap after graduation is x*y=k with the LP burned, so
               TVL(t) = 2 * BASE0 * sqrt(p0 * p(t))  (USD, SOL taken as flat)
               BASE0 = token reserve at graduation (~206.9M of 1B).
  fees         stake * sum(volume_5m * FEE / TVL)  (our share of LP fees)
  LP value     stake * sqrt(p_exit / p_entry)  (vs holding SOL = 1.0)
  result       LP value + fees, as a multiple of the stake
Volume is GeckoTerminal 5m volume of the pool. Other LPs joining (lower
share), slippage and gas are ignored, so fees are an upper bound.
Concentrated (Meteora DLMM) positions earn more per $ but lose more when the
price leaves the range; this backtest is the plain full-range baseline.
"""
from __future__ import annotations

import itertools
import math
import sqlite3
from datetime import datetime, timedelta
from statistics import mean, median

from . import config

BASE0 = 206_900_000
MIN_PEAK = 5.0
DROPS = (0.3, 0.5, 0.7)
HOLDS_H = (6, 24, 48)
FEES = (0.002, 0.01)          # PumpSwap LP share 0.20%; 1% as a high-fee pool (e.g. Meteora)
MIN_VOL_TVL = (0.0, 1.0)      # trailing 1h volume / TVL at entry


def load(conn: sqlite3.Connection) -> list[dict]:
    """Per token with peak >= MIN_PEAK: candles (ts, close, volume) of its 72h window."""
    info = {r["canonical_pool"]: r for r in conn.execute(
        """SELECT t.mint, t.symbol, t.graduated_at, t.canonical_pool, l.label, l.peak_multiple FROM tokens t
           JOIN labels l ON l.mint = t.mint AND l.rule_version = ? WHERE l.peak_multiple >= ?""",
        (config.RULE_VERSION, MIN_PEAK))}
    out = []
    cur = conn.execute("SELECT pool, ts, open, close, volume_usd FROM candles_5m ORDER BY pool, ts")
    for pool, grp in itertools.groupby(cur, key=lambda r: r[0]):
        t = info.get(pool)
        if t is None:
            continue
        t0 = datetime.fromisoformat(t["graduated_at"])
        end = t0 + timedelta(hours=config.WINDOW_HOURS)
        rows = [(ts, o, c, v or 0.0) for _, s, o, c, v in grp
                if t0 <= (ts := datetime.fromisoformat(s)) < end and c and c > 0]
        if len(rows) < 12 or not rows[0][1] or rows[0][1] * config.PUMPFUN_SUPPLY < config.HOLLOW_MCAP_USD:
            continue
        out.append({"mint": t["mint"], "symbol": t["symbol"], "label": t["label"], "t0": t0,
                    "p0": rows[0][1], "rows": [(ts, c, v) for ts, o, c, v in rows]})
    return out


def simulate(tok: dict, drop: float, hold_h: float, fee: float, min_vol_tvl: float) -> dict | None:
    """First entry after the price ran >= MIN_PEAK and fell `drop` below its running high."""
    p0, rows = tok["p0"], tok["rows"]
    tvl = lambda p: 2 * BASE0 * math.sqrt(p0 * p)
    high, i0 = 0.0, None
    for i, (ts, c, v) in enumerate(rows):
        high = max(high, c)
        if high >= MIN_PEAK * p0 and c <= (1 - drop) * high:
            hour = [r for r in rows[max(0, i - 11):i + 1]]
            if sum(r[2] for r in hour) >= min_vol_tvl * tvl(c):
                i0 = i
                break
    if i0 is None:
        return None
    entry_ts, pe = rows[i0][0], rows[i0][1]
    stop = entry_ts + timedelta(hours=hold_h)
    seg = [r for r in rows[i0 + 1:] if r[0] <= stop]
    if not seg:
        return None
    fees = sum(v * fee / tvl(c) for ts, c, v in seg)
    px = seg[-1][1] / pe
    return {"fees": fees, "price": px, "lp": math.sqrt(px) + fees, "hold_token": px,
            "full": seg[-1][0] >= stop - timedelta(minutes=10)}


def run(conn: sqlite3.Connection) -> dict:
    toks = load(conn)
    res = []
    for drop, hold, fee, mv in itertools.product(DROPS, HOLDS_H, FEES, MIN_VOL_TVL):
        sims = [s for t in toks if (s := simulate(t, drop, hold, fee, mv))]
        if not sims:
            continue
        lp = [s["lp"] for s in sims]
        res.append({"drop": drop, "hold_h": hold, "fee": fee, "min_vol_tvl": mv, "n": len(sims),
                    "lp_mean": mean(lp), "lp_median": median(lp), "lp_win": sum(x > 1 for x in lp) / len(lp),
                    "fees_median": median(s["fees"] for s in sims),
                    "price_median": median(s["price"] for s in sims),
                    "token_mean": mean(s["hold_token"] for s in sims)})
    return {"tokens": len(toks), "results": res}


def text(r: dict) -> str:
    lines = [f"Token dengan puncak ≥{MIN_PEAK:.0f}x dan candle: {r['tokens']}",
             "turun | tahan | fee  | vol/TVL | n    | LP rata2 | LP median | menang | fee med | harga med | pegang token rata2"]
    for x in sorted(r["results"], key=lambda x: (x["fee"], x["min_vol_tvl"], x["drop"], x["hold_h"])):
        lines.append(f"{x['drop']:.0%}   | {x['hold_h']:>3}j  | {x['fee']:.1%} | {x['min_vol_tvl']:>4.1f}    | "
                     f"{x['n']:>4} | {x['lp_mean']:.3f}    | {x['lp_median']:.3f}     | {x['lp_win']:.0%}    | "
                     f"{x['fees_median']:.3f}   | {x['price_median']:.3f}     | {x['token_mean']:.3f}")
    return "\n".join(lines)
