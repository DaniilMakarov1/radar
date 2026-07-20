-- Smart Money Radar: Base target liquidity context template
--
-- Purpose:
--   Estimate DEX activity around Binance listing targets on Base.
--
-- Use this after token contract mapping is reliable.

WITH targets(symbol, token_address, announced_at) AS (
    VALUES
        -- ('TOKEN', 0x0000000000000000000000000000000000000000, TIMESTAMP '2026-01-01 12:00:00')
        ('REPLACE_ME', 0x0000000000000000000000000000000000000000, TIMESTAMP '2026-01-01 00:00:00')
),
daily_trades AS (
    SELECT
        t.symbol,
        t.token_address,
        DATE_TRUNC('day', d.block_time) AS day,
        SUM(d.amount_usd) AS volume_usd,
        COUNT(*) AS trade_count,
        COUNT(DISTINCT d.taker) AS active_traders
    FROM dex.trades d
    JOIN targets t
        ON d.blockchain = 'base'
       AND (
            d.token_bought_address = t.token_address
            OR d.token_sold_address = t.token_address
       )
       AND d.block_time >= t.announced_at - INTERVAL '90' day
       AND d.block_time < t.announced_at + INTERVAL '30' day
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.amount_usd IS NOT NULL
    GROUP BY 1, 2, 3
)
SELECT *
FROM daily_trades
ORDER BY symbol, day;

