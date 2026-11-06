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
    trades: int | None = None
    liquidity_usd: float | None = None
    volume_usd: float | None = None


@dataclass(frozen=True)
class LabelResult:
    label: str
    peak_multiple: float | None
    hours_to_peak: float | None
    close_24h_vs_peak: float | None
    reason: str


def grid_t0(t0: datetime, seconds: int = 300) -> datetime:
    """Start of the 5m candle that contains T0. Candle windows are measured from
    here so the graduation candle (which opens before T0) is included; this only
    ever uses less post-T0 data, never more."""
    return t0 - timedelta(seconds=t0.timestamp() % seconds)


def _hours(t0: datetime, ts: datetime) -> float:
    return (ts - t0).total_seconds() / 3600


def label_path(
    t0: datetime,
    grad_price: float,
    candles: Sequence[Candle],
    rules: LabelRules = DEFAULT_LABEL_RULES,
    trades_h24: int | None = None,
) -> LabelResult:
    """`trades_h24` (trades in hour 24–25, e.g. from the 25h snapshot) is used
    when the candles carry no trade counts."""
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
    liq = [c.liquidity_usd for c in path if c.liquidity_usd is not None]
    if liq:
        running_max = 0.0
        for v in liq:
            running_max = max(running_max, v)
            if running_max > 0 and v < rules.rug_liq_frac * running_max:
                return res("rug", f"liquidity {v:.0f} < {rules.rug_liq_frac:.0%} of max {running_max:.0f}")
    else:
        # No liquidity series: PumpSwap pools are constant-product and the LP is
        # burned at migration, so pool liquidity ~ sqrt(price). Liquidity below
        # X% of its max  <=>  close below X^2 of the running max close.
        frac = rules.rug_liq_frac ** 2
        running_max = 0.0
        for c in path:
            running_max = max(running_max, c.close)
            if c.close < frac * running_max:
                return res("rug", f"close {c.close / running_max:.4%} of max (liquidity ~ sqrt(price) proxy)")

    # 2. Dead
    if peak_mult < rules.dead_peak_mult:
        return res("dead", f"peak {peak_mult:.2f}x < {rules.dead_peak_mult}x")
    h24 = [c for c in path if 24 <= _hours(t0, c.ts) < 25]
    counted = [c.trades for c in h24 if c.trades is not None]
    if counted or (trades_h24 is None and any(c.trades is not None for c in path)):
        trades_h24 = sum(counted)
    if trades_h24 is not None:
        if trades_h24 < rules.dead_min_trades_h24:
            return res("dead", f"{trades_h24} trades in hour 24–25")
    else:
        vol = sum(c.volume_usd or 0.0 for c in h24)
        if vol < rules.dead_min_volume_h24_usd:
            return res("dead", f"${vol:.0f} volume in hour 24–25 (no trade counts)")

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
