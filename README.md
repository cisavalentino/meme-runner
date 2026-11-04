# solana-runner-research

Research pipeline for Solana launchpad graduations (see the PRD). Version 1 covers pump.fun only.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,dune]'
.venv/bin/pytest -q
.venv/bin/rr init-db
.venv/bin/rr check <mint-or-pool>          # FR6
.venv/bin/rr backfill-graduations --start 2026-08-01 --end 2026-08-07   # FR1, needs DUNE_API_KEY
.venv/bin/rr status
```

## Status (M1, offline part)

| Part | State |
|---|---|
| SQLite schema (5 PRD tables + `job_runs` for resume), idempotent upserts | done, tested |
| FR5 label rules (`labels.py`, thresholds in `config.py`, `RULE_VERSION`) | done, tested on synthetic paths |
| FR6 fake/duplicate check + `rr check` | done, tested |
| Rate-limited HTTP client with backoff, header redaction | done, tested |
| FR1/FR2 Dune SQL (`src/runner_research/sql/`) | draft; table/column names **unverified** |
| FR1 backfill driver (one day per query, resumable) | written, never run against Dune |
| FR2 driver, FR3 live collector, FR4 features, FR7 analysis | not started |

## Interpretations to confirm

- **Rug:** liquidity below 5% of its running maximum at any point after that maximum.
- **Second wave:** after T0+24h, a close below 2x graduation price, followed by a later close at least 5x that base.
- **Close at 24h:** last 5-minute close before T0+24h.
- **Liquidity** is not in `dex_solana.trades`; rug detection needs a separate pool-reserves query.
