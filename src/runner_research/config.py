"""Label and fake-pool thresholds.

These are the PRD starting values. Milestone 3 tunes them on Aug–Sep cohorts
only; bump RULE_VERSION whenever any value changes so stored labels stay
traceable to the rules that produced them.
"""
from dataclasses import dataclass, asdict

RULE_VERSION = "v0.2"   # v0.2: rug price proxy + dead-rule volume fallback (Gecko candles have no trades/liquidity)

# 25h: DexScreener txns.h1 at T0+25h = trades in hour 24–25 (dead rule)
SNAPSHOT_OFFSETS_MIN = {"15m": 15, "1h": 60, "6h": 360, "24h": 1440, "25h": 1500, "72h": 4320}
WINDOW_HOURS = 72
CANDLE_SECONDS = 300


@dataclass(frozen=True)
class LabelRules:
    rug_liq_frac: float = 0.05          # liquidity < 5% of own max (after the max)
    dead_peak_mult: float = 2.0         # peak below 2x graduation price
    dead_min_trades_h24: int = 10       # fewer than 10 trades in hour 24–25
    # used only when no trade count exists for hour 24–25 (Gecko candles, no 25h snapshot)
    dead_min_volume_h24_usd: float = 500.0
    spike_peak_mult: float = 10.0
    spike_max_hours: float = 6.0
    spike_close24_max: float = 0.40     # close@24h < 40% of peak
    slow_peak_mult: float = 5.0
    slow_min_hours: float = 6.0         # peak strictly after 6h
    slow_close24_min: float = 0.60      # close@24h >= 60% of peak
    wave_start_hours: float = 24.0      # move starts after T0+24h
    wave_base_mult: float = 2.0         # from a close below 2x graduation price
    wave_move_mult: float = 5.0         # later close >= 5x that base close
    runner_peak_mult: float = 5.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FakePoolRules:
    vol_to_liq_max: float = 50.0        # 24h volume > 50x liquidity ...
    min_traders: int = 50               # ... with fewer than 50 distinct traders


DEFAULT_LABEL_RULES = LabelRules()
DEFAULT_FAKE_RULES = FakePoolRules()

# FR4
FEATURE_CUTOFF_MIN = 60          # features use data up to T0 + 1h only
SNIPER_WINDOW_SEC = 120          # "bought in the first 2 minutes"
PUMPFUN_SUPPLY = 1_000_000_000   # fixed pump.fun supply, for mcap from price

# FR7 first question
Q1_MIN_MULT_1H = 4.0
Q1_MIN_FRAC_OF_PEAK_24H = 0.5    # peak measured over 0–24h only (as known at 24h)
POSITIVE_LABELS = ("slow_climb", "runner_other")

# Segments (analysis). "hollow": first PumpSwap price implies < $1k market cap,
# ~1000x below a normal graduation (~$35k); there is a clear gap between $1k
# and $10k. "instant": created and graduated in the same second (bundle).
HOLLOW_MCAP_USD = 1000.0
# Features known at T0 (no price path after graduation).
PRICE_PATH_FEATURES = ("mult_15m", "mult_1h")
GRAD_PRICE_FEATURES = ("grad_price", "grad_mcap")

# Live collector / call budget. GeckoTerminal public API is ~10–30 calls/min with
# no monthly cap; per-token trade polling is the expensive part, so it runs on a
# deterministic hash sample of mints (unbiased; missing features stay missing).
GECKO_DETAIL_SAMPLE = 0.2        # share of graduations that get trades + holders
TRADE_POLL_EVERY_MIN = 10        # trades endpoint returns only the last 300 trades
# Candles: tokens graduated after the collector went live (state `live_since`) all get
# candles; older (backfilled) tokens get a hash sample, oldest first.
CANDLE_SAMPLE_HISTORICAL = 0.3
CG_DEMO_MONTHLY_CAP = 9500       # CoinGecko demo key: 10k calls/month; second candle lane
DISCOVERY_POLL_SEC = 15        # one getSignaturesForAddress per loop (1 Helius credit)
