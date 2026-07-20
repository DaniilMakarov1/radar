-- Smart Money Radar: Base pre-listing buyer extraction template
--
-- Purpose:
--   For a set of Binance listing targets, find wallets that bought the token
--   on Base before the Binance announcement timestamp.
--
-- How to use:
--   Replace the rows in `targets` with exported/manual token targets.
--   For contract-specific analysis, prefer contract addresses over symbols.
--
-- Notes:
--   This is a template. It should be converted to a parameterized Dune query
--   once we wire the Dune API client.

WITH targets(symbol, token_address, announced_at) AS (
    VALUES
        -- Example:
        -- ('TOKEN', 0x0000000000000000000000000000000000000000, TIMESTAMP '2026-01-01 12:00:00')
        ('REPLACE_ME', 0x0000000000000000000000000000000000000000, TIMESTAMP '2026-01-01 00:00:00')
),
pre_listing_trades AS (
    SELECT
        t.symbol,
        t.token_address,
        t.announced_at,
        d.block_time,
        d.tx_hash,
        d.taker AS wallet_address,
        d.token_bought_address,
        d.token_sold_address,
        d.token_bought_symbol,
        d.token_sold_symbol,
        d.amount_usd
    FROM dex.trades d
    JOIN targets t
        ON d.blockchain = 'base'
       AND d.token_bought_address = t.token_address
       AND d.block_time < t.announced_at
       AND d.block_time >= t.announced_at - INTERVAL '90' day
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.amount_usd IS NOT NULL
)
SELECT
    symbol,
    token_address,
    announced_at,
    wallet_address,
    MIN(block_time) AS first_buy_at,
    MAX(block_time) AS last_buy_at,
    COUNT(*) AS buy_trade_count,
    SUM(amount_usd) AS gross_buy_usd
FROM pre_listing_trades
GROUP BY 1, 2, 3, 4
ORDER BY announced_at DESC, gross_buy_usd DESC;

