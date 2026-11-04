-- FR1: pump.fun graduations for one UTC day ({{day}}), joined to their creation record.
-- UNVERIFIED (M1 task): table and column names below must be checked against the
-- live Dune catalog before first run. `pump_call_migrate` is the expected PumpSwap-era
-- migrate instruction; `pump_call_withdraw` is the older Raydium-era path.
WITH grads AS (
    SELECT
        account_mint            AS mint,
        account_pool            AS canonical_pool,
        call_block_time         AS graduated_at,
        call_tx_id              AS migrate_tx
    FROM pumpdotfun_solana.pump_call_migrate
    WHERE call_block_time >= TIMESTAMP '{{day}} 00:00:00'
      AND call_block_time <  TIMESTAMP '{{day}} 00:00:00' + INTERVAL '1' DAY
),
creates AS (
    SELECT account_mint AS mint, name, symbol, account_user AS creator,
           call_block_time AS created_at
    FROM pumpdotfun_solana.pump_call_create
    WHERE call_block_time >= TIMESTAMP '{{day}} 00:00:00' - INTERVAL '30' DAY
      AND call_block_time <  TIMESTAMP '{{day}} 00:00:00' + INTERVAL '1' DAY
)
SELECT g.mint, c.name, c.symbol, c.creator, c.created_at, g.graduated_at,
       g.canonical_pool, g.migrate_tx,
       date_diff('second', c.created_at, g.graduated_at) / 60.0 AS bonding_minutes
FROM grads g
LEFT JOIN creates c ON c.mint = g.mint
ORDER BY g.graduated_at
