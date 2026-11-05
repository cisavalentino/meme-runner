"""FR4 — features known at T+1h. Nothing after T0 + 1h may enter here.

Trade-level features are computed by `trade_features()` (reference semantics,
used by tests and the live path); `sql/fr4_trade_features_day.sql` mirrors it
inside Dune for the backfill so raw trades never leave Dune.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from .config import FEATURE_CUTOFF_MIN, PUMPFUN_SUPPLY, SNIPER_WINDOW_SEC, WINDOW_HOURS
from .fakecheck import same_name_copies
from .labels import Candle


@dataclass(frozen=True)
class Trade:
    ts: datetime
    trader: str
    side: str        # 'buy' | 'sell' (of the token)
    usd: float


def close_at(t0: datetime, candles: Sequence[Candle], minutes: float) -> float | None:
    """Last 5m close of a candle that has fully closed by T0+minutes."""
    cutoff = t0 + timedelta(minutes=minutes)
    done = [c for c in candles if t0 <= c.ts and c.ts + timedelta(minutes=5) <= cutoff]
    return max(done, key=lambda c: c.ts).close if done else None


def price_features(t0: datetime, grad_price: float, candles: Sequence[Candle]) -> dict:
    c15, c60 = close_at(t0, candles, 15), close_at(t0, candles, 60)
    return {
        "grad_price": grad_price,
        "grad_mcap": grad_price * PUMPFUN_SUPPLY,
        "mult_15m": c15 / grad_price if c15 else None,
        "mult_1h": c60 / grad_price if c60 else None,
    }


def trade_features(t0: datetime, trades: Iterable[Trade], creator: str | None) -> dict:
    end = t0 + timedelta(minutes=FEATURE_CUTOFF_MIN)
    tr = [t for t in trades if t0 <= t.ts < end and t.usd > 0]
    buys = [t for t in tr if t.side == "buy"]
    sells = [t for t in tr if t.side == "sell"]
    vol = sum(t.usd for t in tr)
    buy_vol, sell_vol = sum(t.usd for t in buys), sum(t.usd for t in sells)
    traders = {t.trader for t in tr}
    snipe_cut = t0 + timedelta(seconds=SNIPER_WINDOW_SEC)
    snipers = {t.trader for t in buys if t.ts < snipe_cut}
    sniper_buy = sum(t.usd for t in buys if t.trader in snipers)
    return {
        "vol_1h": vol,
        "trades_1h": len(tr),
        "traders_1h": len(traders),
        "vol_per_trader": vol / len(traders) if traders else None,
        "trades_per_trader": len(tr) / len(traders) if traders else None,
        "buy_sell_ratio": buy_vol / sell_vol if sell_vol else None,
        # share of first-hour BUY volume from wallets that bought in the first 2 min
        "sniper_share": sniper_buy / buy_vol if buy_vol else None,
        "creator_buy_usd_1h": sum(t.usd for t in buys if t.trader == creator),
        "creator_sell_usd_1h": sum(t.usd for t in sells if t.trader == creator),
    }


def context_features(conn: sqlite3.Connection, mint: str) -> dict:
    """Features from the store. Creator history only counts tokens whose 72h
    window had closed by this token's T0+1h, so no outcome leaks backwards."""
    tok = conn.execute("SELECT * FROM tokens WHERE mint = ?", (mint,)).fetchone()
    t0 = datetime.fromisoformat(tok["graduated_at"])
    asof = t0 + timedelta(minutes=FEATURE_CUTOFF_MIN)
    out: dict = {
        "bonding_minutes": tok["bonding_minutes"],
        "hour_utc": t0.hour,
        "same_name_7d": same_name_copies(conn, mint),
    }
    # graduations so far on this UTC day, up to T0+1h only (no look-ahead)
    day = t0.date().isoformat()
    out["day_grad_count"] = conn.execute(
        "SELECT COUNT(*) FROM tokens WHERE substr(graduated_at, 1, 10) = ? AND graduated_at <= ?",
        (day, asof.isoformat())).fetchone()[0]

    if tok["creator"]:
        # The store only holds graduated tokens, so history = earlier graduations.
        prior = conn.execute(
            "SELECT mint, graduated_at FROM tokens WHERE creator = ? AND graduated_at < ? AND mint != ?",
            (tok["creator"], tok["graduated_at"], mint)).fetchall()
        out["creator_prior_graduated"] = len(prior)
        closed = [p["mint"] for p in prior
                  if datetime.fromisoformat(p["graduated_at"]) + timedelta(hours=WINDOW_HOURS) <= asof]
        labs = [r[0] for r in conn.execute(
            f"SELECT label FROM labels WHERE mint IN ({','.join('?' * len(closed))})", closed)] if closed else []
        out["creator_prior_rugs"] = labs.count("rug")
        out["creator_prior_runners"] = sum(l in ("slow_climb", "runner_other", "second_wave") for l in labs)

    snap = conn.execute(
        "SELECT * FROM snapshots WHERE mint = ? AND offset = '1h' AND taken_at <= ?",
        (mint, (asof + timedelta(minutes=2)).isoformat())).fetchone()
    out["dexs_profile_1h"] = snap["dexs_profile"] if snap else None
    out["dexs_boosts_1h"] = snap["dexs_boosts"] if snap else None
    out["top10_pct_1h"] = snap["top10_pct"] if snap else None
    out["holders_1h"] = snap["holders"] if snap else None
    return out
