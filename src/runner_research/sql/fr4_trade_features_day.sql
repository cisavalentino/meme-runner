-- FR4 trade features for tokens graduated on {{day}}, first hour only.
-- Mirrors features.trade_features(). UNVERIFIED column names (see fr2).
WITH grads AS (
    SELECT mint, canonical_pool, graduated_at, creator
    FROM query_{{fr1_query_id}}(day => '{{day}}')
),
t AS (
    SELECT g.mint, g.graduated_at, g.creator, tr.block_time, tr.trader_id, tr.amount_usd AS usd,
           CASE WHEN tr.token_bought_mint_address = g.mint THEN 'buy' ELSE 'sell' END AS side
    FROM dex_solana.trades tr
    JOIN grads g ON tr.project_main_id = g.canonical_pool
    WHERE tr.block_time >= TIMESTAMP '{{day}} 00:00:00'
      AND tr.block_time <  TIMESTAMP '{{day}} 00:00:00' + INTERVAL '2' DAY
      AND tr.block_time >= g.graduated_at
      AND tr.block_time <  g.graduated_at + INTERVAL '60' MINUTE
      AND tr.amount_usd > 0
),
snipers AS (
    SELECT DISTINCT mint, trader_id FROM t
    WHERE side = 'buy' AND block_time < graduated_at + INTERVAL '120' SECOND
)
SELECT t.mint,
       sum(usd)                                         AS vol_1h,
       count(*)                                         AS trades_1h,
       count(DISTINCT t.trader_id)                      AS traders_1h,
       sum(usd) / count(DISTINCT t.trader_id)           AS vol_per_trader,
       count(*) * 1.0 / count(DISTINCT t.trader_id)     AS trades_per_trader,
       sum(CASE WHEN side='buy' THEN usd END) / NULLIF(sum(CASE WHEN side='sell' THEN usd END), 0) AS buy_sell_ratio,
       sum(CASE WHEN side='buy' AND s.trader_id IS NOT NULL THEN usd END)
         / NULLIF(sum(CASE WHEN side='buy' THEN usd END), 0) AS sniper_share,
       coalesce(sum(CASE WHEN side='buy'  AND t.trader_id = t.creator THEN usd END), 0) AS creator_buy_usd_1h,
       coalesce(sum(CASE WHEN side='sell' AND t.trader_id = t.creator THEN usd END), 0) AS creator_sell_usd_1h
FROM t
LEFT JOIN snipers s ON s.mint = t.mint AND s.trader_id = t.trader_id
GROUP BY t.mint
