# solana-runner-research

Research pipeline for Solana launchpad graduations (see the PRD). Version 1 covers pump.fun only.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,analysis]'
# no ensurepip on Debian? python3 -m venv --without-pip .venv, then run get-pip.py with .venv/bin/python
cp .env.example .env                      # optional keys; rr loads .env itself
.venv/bin/pytest -q

# history
.venv/bin/rr -v backfill-graduations --start 2026-08-01            # FR1 via Solana RPC, resumable
.venv/bin/rr backfill-candles --sample 0.1                          # FR2 via GeckoTerminal, 1 call/token
# live (keep running, e.g. under systemd/pm2)
.venv/bin/rr collect                                                # FR3 discovery, snapshots, trades, candles
# research
.venv/bin/rr label                         # FR4 features + FR5 labels (tokens past T0+72h)
.venv/bin/rr analyze --split 2026-10-01    # FR7 -> reports/analysis.json
.venv/bin/rr status | check <mint-or-pool> | due
```

## Live system (pm2)

```bash
pm2 start ecosystem.config.cjs && pm2 save   # pm2-mega.service restores it on boot
pm2 ls; pm2 logs rr-collect; .venv/bin/rr status; .venv/bin/rr health
```

| App | What | When |
|---|---|---|
| `rr-collect` | discovery (15 s), snapshots, holders, trade polling, candle backlog in the rest of each loop | always, auto-restart |
| `rr-backfill` | RPC graduations back to 2026-08-01, resumable | until done (exit 0 = stays stopped) |
| `rr-daily` | `rr label` (new windows only) + `rr analyze` → `reports/analysis.json` | 00:20 UTC |
| `rr-watchdog` | restarts `rr-collect` if its heartbeat is > 10 min old | every 10 min |

`.env`: `RR_SOLANA_RPC` (Helius URL; 6 parallel workers, `RR_RPC_WORKERS`), keys are redacted
from every log line. Helius cost: 1 credit per call; full backfill ≈ 1–1.5M credits, live ≈ 5k/day.
Candles are the slow part. Tokens graduated after `live_since` (collector start) all get
candles, first; backfilled tokens get a 30% hash sample (`CANDLE_SAMPLE_HISTORICAL`), in hash
order so a partial backlog covers every month evenly. Two lanes share a claim set: public GeckoTerminal (~5–6 calls/min from this IP, shared
with trades and holders) and the CoinGecko demo key (25/min, stops at 9,500 calls/month,
counted in `state`). Measured: ~1,600 candles/hour while the demo lane has quota, ~150–250/hour
after.

## Data sources

| Need | Source | Cost |
|---|---|---|
| FR1 graduations (mint, canonical pool, T0) | Solana RPC: `getSignaturesForAddress` on the pump.fun migration account `39azUY…`, then `getTransaction`; only the tx whose logs contain `CreatePool` counts | 1 call per tx; each graduation has 2–3 successful Migrate txs (losing keepers are no-ops) |
| creator | bonding-curve account (bytes 49–81), `getMultipleAccounts` ×100 | ~0 |
| name / symbol | Token-2022 metadata extension on the mint, `getMultipleAccounts` ×100 | ~0 |
| creation time / bonding minutes | oldest signature of the bonding curve | 1–3 calls; **live only** |
| FR2 5m candles T0..T0+72h | GeckoTerminal OHLCV, 1000×5m per call | 1 call per token |
| snapshots 15m/1h/6h/24h/25h/72h: price, liquidity, mcap, volume, txns, profile, boosts | DexScreener `/tokens/v1` (30 mints per call, 300/min) | tiny |
| holders, top10 %, dev %, authorities (1h, 24h) | GeckoTerminal token info | 1 call, sampled |
| FR4 first-hour trades | GeckoTerminal pool trades (last 300 only), polled every 10 min + after T0+1h | ~7 calls, sampled |
| FR6 other pools of a mint | DexScreener `/token-pairs/v1` at 24h | 1 call |

Measured on 2026-11-05: ~700 graduations/day. GeckoTerminal's public API starts returning 429
after ~3 quick calls, so the per-token Gecko work (trades + holders) runs on a deterministic hash
sample of mints (`GECKO_DETAIL_SAMPLE = 0.2` in `config.py`): unbiased, and missing stays missing.
The public Solana RPC also rate-limits (429 at ~3 req/s). Set `RR_SOLANA_RPC` to a private RPC for
a full Aug–Oct backfill.

**Not used:** Dune (the provided key belongs to a read-only account: `sql/execute` returns 402, and
query creation returns "account is read-only"; the Dune path is kept as `--source dune`), the
CoinGecko demo key (the pumpswap `new_pools` endpoint needs the Analyst plan, and the 10k
calls/month cap is below the live budget; enable with `RR_GECKO_USE_KEY=1`), and pump.fun's
frontend API (behind Cloudflare).

## Status

| Part | State |
|---|---|
| SQLite schema, idempotent upserts, timestamp normalization, `state` cursors | done, tested |
| FR1 RPC scanner: backward (resumable) and forward (live), creator + name/symbol | done, tested; smoke-run live |
| FR2 candles from GeckoTerminal | done, tested; smoke-run live (Sep 13 pool: 817 candles) |
| FR3 collector loop (`rr collect`): snapshots, Gecko holders, trade polling with coverage check, FR6 pools | done, tested with fakes; one live step run |
| FR4 features at T+1h, no look-ahead | done, tested |
| FR5 labels (`RULE_VERSION = v0.2`) | done, tested |
| FR6 fake/duplicate check | done, tested |
| FR7 analysis: weekly base rates, Wilson buckets, first question (Newcombe), OOT logistic regression | done, tested on synthetic data |
| Dune SQL drafts (FR1/FR2/FR4) | kept, unverified (account cannot run queries) |
| LightGBM, first real analysis run | waiting for ≥ Aug–Oct data |

## Interpretations to confirm

- **Rug:** liquidity below 5% of its running maximum at any point after that maximum. Candles from
  GeckoTerminal have no liquidity, so v0.2 uses a proxy: PumpSwap pools are constant-product and
  the LP is burned at migration, so liquidity ∝ √price, i.e. *rug ⇔ a close below 0.25% (5%²) of the
  running max close*.
- **Dead (no trades in hour 24–25):** trade count from candles if present, else the 25h snapshot
  (DexScreener `txns.h1`), else **< $500 volume in hour 24–25** (new threshold, to tune).
- **T0 on the candle grid:** windows start at the 5m candle that contains T0, so the graduation
  candle is included. Graduation price = open of that candle.
- **Tokens with no GeckoTerminal candles** (Gecko never indexed the pool, usually dead from the
  start) are marked `candles_empty` in `rr status` and are **not labeled**. If this share is large,
  it biases base rates toward survivors.
- **Second wave:** after T0+24h, a close below 2x graduation price, followed by a later close at least 5x that base.
- **Close at 24h:** last 5-minute close before T0+24h.
- **Sniper share:** first-hour buy volume from wallets that bought within 2 minutes of T0, divided by all first-hour buy volume.
  Trade features are only written when polling covered the whole first hour (`trades_complete`).
- **First question "peak":** the 0–24h peak (what was knowable at 24h), not the 72h peak. Only tokens past T0+24h count.
- **Creator history:** the store holds graduated tokens only, so history = the creator's earlier
  *graduations* (`creator_prior_graduated`); rugs/runners count only once their 72h window closed by this token's T0+1h.
- **Same-name copies:** earlier tokens with the same name/symbol within 7 days, by creation time
  when known, else graduation time.
- **Positive class for the model:** `slow_climb` or `runner_other` (PRD: "slow climb or runner").
