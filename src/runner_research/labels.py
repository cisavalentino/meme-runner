"""FR5 — label a token from its first 72 hours. First matching rule wins.

All multiples are relative to the graduation price (first trade in the
canonical pool). Peak uses 5-minute closes, not wicks.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from .config import DEFAULT_LABEL_RULES, WINDOW_HOURS, LabelRules

LABELS = ("rug", "dead", "spike_and_fade", "slow_climb", "second_wave", "runner_other", "flat")


@dataclass(frozen=True)
class Candle:
    ts: datetime          # candle open time, UTC
    close: float
    trades: int = 0
    liquidity_usd: float | None = None


@dataclass(frozen=True)
class LabelResult:
    label: str
    peak_multiple: float | None
    hours_to_peak: float | None
    close_24h_vs_peak: float | None
    reason: str


def _hours(t0: datetime, ts: datetime) -> float:
    return (ts - t0).total_seconds() / 3600


def label_path(
    t0: datetime,
    grad_price: float,
    candles: Sequence[Candle],
    rules: LabelRules = DEFAULT_LABEL_RULES,
) -> LabelResult:
    if grad_price <= 0:
        raise ValueError("graduation price must be positive")
    end = t0 + timedelta(hours=WINDOW_HOURS)
    path = sorted((c for c in candles if t0 <= c.ts < end and c.close > 0), key=lambda c: c.ts)
    if not path:
        return LabelResult("dead", None, None, None, "no trades in window")

    peak = max(path, key=lambda c: c.close)
    peak_mult = peak.close / grad_price
    h_peak = _hours(t0, peak.ts)
    before_24 = [c for c in path if _hours(t0, c.ts) < 24]
    close_24 = before_24[-1].close if before_24 else None
    c24_vs_peak = close_24 / peak.close if close_24 is not None else None

    def res(label: str, reason: str) -> LabelResult:
        return LabelResult(label, peak_mult, h_peak, c24_vs_peak, reason)

    # 1. Rug: liquidity drops below X% of its own max at some point after that max.
    liq = [(c.ts, c.liquidity_usd) for c in path if c.liquidity_usd is not None]
    if liq:
        running_max = 0.0
        for _, v in liq:
            running_max = max(running_max, v)
            if running_max > 0 and v < rules.rug_liq_frac * running_max:
                return res("rug", f"liquidity {v:.0f} < {rules.rug_liq_frac:.0%} of max {running_max:.0f}")

    # 2. Dead
    if peak_mult < rules.dead_peak_mult:
        return res("dead", f"peak {peak_mult:.2f}x < {rules.dead_peak_mult}x")
    trades_h24 = sum(c.trades for c in path if 24 <= _hours(t0, c.ts) < 25)
    if trades_h24 < rules.dead_min_trades_h24:
        return res("dead", f"{trades_h24} trades in hour 24–25")

    # 3. Spike and fade
    if (
        peak_mult >= rules.spike_peak_mult
        and h_peak <= rules.spike_max_hours
        and c24_vs_peak is not None
        and c24_vs_peak < rules.spike_close24_max
    ):
        return res("spike_and_fade", "fast peak, faded by 24h")

    # 4. Slow climb
    if (
        peak_mult >= rules.slow_peak_mult
        and h_peak > rules.slow_min_hours
        and c24_vs_peak is not None
        and c24_vs_peak >= rules.slow_close24_min
    ):
        return res("slow_climb", "late peak, held at 24h")

    # 5. Second wave: after T0+24h, from a base close < 2x, a later close >= 5x that base.
    late = [c for c in path if _hours(t0, c.ts) > rules.wave_start_hours]
    base = None
    for c in late:
        if c.close / grad_price < rules.wave_base_mult and (base is None or c.close < base):
            base = c.close
        elif base is not None and c.close >= rules.wave_move_mult * base:
            return res("second_wave", f"{c.close / base:.1f}x from base after 24h")

    # 6. Runner (other)
    if peak_mult >= rules.runner_peak_mult:
        return res("runner_other", "peak >= 5x, no specific path")

    return res("flat", "no rule matched")
