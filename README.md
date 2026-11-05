# solana-runner-research

Research pipeline for Solana launchpad graduations (see the PRD). Version 1 covers pump.fun only.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,dune]'
.venv/bin/pytest -q
.venv/bin/rr init-db
.venv/bin/rr check <mint-or-pool>          # FR6
.venv/bin/rr backfill-graduations --start 2026-08-01 --end 2026-08-07   # FR1, needs DUNE_API_KEY
.venv/bin/rr status
.venv/bin/rr label                         # FR4 features + FR5 labels (tokens past T0+72h)
.venv/bin/rr analyze --split 2026-10-01    # FR7 -> reports/analysis.json
.venv/bin/rr due                           # FR3 snapshots due now
```

## Status

| Part | State |
|---|---|
| SQLite schema (5 PRD tables + `job_runs` for resume, `features_1h` for Dune trade features), idempotent upserts, timestamp normalization | done, tested |
| FR5 labels (`labels.py`, thresholds in `config.py`, `RULE_VERSION`) + labeling job | done, tested on synthetic paths |
| FR4 features at T+1h (`features.py`): price, trade, creator history, same-name, snapshot, calendar | done, tested incl. no-look-ahead checks |
| FR6 fake/duplicate check + `rr check` | done, tested |
| FR7 analysis: weekly base rates, feature buckets with Wilson CI, first question with Newcombe CI, out-of-time logistic regression | done, tested on synthetic data |
| FR3 scheduler (due snapshots, lateness, next wake-up) | done, tested; API fetchers not written |
| Rate-limited HTTP client with backoff, header redaction | done, tested |
| Dune SQL: FR1 graduations, FR2 candles, FR4 trade features | draft; table/column names **unverified** |
| FR1 backfill driver | written, never run against Dune |
| FR2/FR4 drivers, DexScreener/GeckoTerminal parsers, LightGBM, 4 Nov fixtures | blocked on network + data |

## Interpretations to confirm

- **Rug:** liquidity below 5% of its running maximum at any point after that maximum.
- **Second wave:** after T0+24h, a close below 2x graduation price, followed by a later close at least 5x that base.
- **Close at 24h:** last 5-minute close before T0+24h.
- **Liquidity** is not in `dex_solana.trades`; rug detection needs a separate pool-reserves query.
- **Sniper share:** first-hour buy volume from wallets that bought within 2 minutes of T0, divided by all first-hour buy volume.
- **First question "peak":** the 0–24h peak (what was knowable at 24h), not the 72h peak.
- **Creator history:** only earlier tokens whose 72h window had closed by this token's T0+1h count toward prior rugs/runners.
- **Positive class for the model:** `slow_climb` or `runner_other` (PRD: "slow climb or runner").
