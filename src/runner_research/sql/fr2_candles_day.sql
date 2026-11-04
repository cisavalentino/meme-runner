-- FR2: 5-minute candles for the canonical pool of every token graduated on {{day}},
-- from T0 to T0+72h. Aggregated inside Dune; only candles leave.
-- UNVERIFIED (M1 task): dex_solana.trades column names (project_main_id as pool id).
-- Liquidity is NOT in this table; rug detection needs a separate reserves query (M1/M2).
WITH grads AS (
    -- inline FR1 result for the same day (kept separate to cache it independently)
    SELECT mint, canonical_pool, graduated_at
    FROM query_{{fr1_query_id}}(day => '{{day}}')
),
t AS (
    SELECT g.canonical_pool AS pool, g.mint, tr.block_time, tr.trader_id, tr.amount_usd,
           CASE WHEN tr.token_bought_mint_address = g.mint THEN 'buy' ELSE 'sell' END AS side,
           tr.amount_usd / NULLIF(
               CASE WHEN tr.token_bought_mint_address = g.mint
                    THEN tr.token_bought_amount ELSE tr.token_sold_amount END, 0) AS price_usd
    FROM dex_solana.trades tr
    JOIN grads g ON tr.project_main_id = g.canonical_pool
    WHERE tr.block_time >= TIMESTAMP '{{day}} 00:00:00'
      AND tr.block_time <  TIMESTAMP '{{day}} 00:00:00' + INTERVAL '4' DAY
      AND tr.block_time <  g.graduated_at + INTERVAL '72' HOUR
      AND tr.block_time >= g.graduated_at
      AND tr.amount_usd > 0
)
SELECT pool,
       from_unixtime(floor(to_unixtime(block_time) / 300) * 300) AS ts,
       min_by(price_usd, block_time) AS open,
       max(price_usd)                AS high,
       min(price_usd)                AS low,
       max_by(price_usd, block_time) AS close,
       sum(amount_usd)               AS volume_usd,
       count(*)                      AS trades,
       count(DISTINCT CASE WHEN side = 'buy'  THEN trader_id END) AS buyers,
       count(DISTINCT CASE WHEN side = 'sell' THEN trader_id END) AS sellers
FROM t
GROUP BY 1, 2
ORDER BY 1, 2
