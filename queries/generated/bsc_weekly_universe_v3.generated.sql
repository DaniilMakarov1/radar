-- Smart Money Radar Research Validity V3
-- Weekly point-in-time universe for bsc; one row is one token-week.
-- Four explicit follow-up weeks preserve zero activity without treating missing rows as zero.

WITH token_flows AS (
    SELECT
        d.block_time,
        d.tx_hash,
        d.tx_from AS wallet_address,
        d.project,
        d.project_contract_address AS pair_address,
        d.token_bought_address AS token_address,
        d.token_bought_symbol AS token_symbol,
        CAST(d.token_bought_amount AS DOUBLE) AS token_amount,
        d.amount_usd,
        'buy' AS side
    FROM dex.trades d
    WHERE d.blockchain = 'bnb'
      AND d.block_month >= DATE '2020-08-01'
      AND d.block_time >= TIMESTAMP '2020-08-01 00:00:00'
      AND d.block_time < DATE_TRUNC('week', CURRENT_TIMESTAMP)
      AND d.tx_from IS NOT NULL
      AND d.token_bought_address IS NOT NULL
      AND d.amount_usd >= 5.0
      AND CAST(d.token_bought_amount AS DOUBLE) > 0
      AND UPPER(COALESCE(d.token_bought_symbol, '')) NOT IN ('USDC', 'USDT', 'DAI', 'USDBC', 'WETH', 'ETH', 'CBETH', 'EURC', 'CBBTC', 'WBTC', 'BTCB', 'WBNB', 'BNB', 'FDUSD', 'TUSD', 'USDE')

    UNION ALL

    SELECT
        d.block_time,
        d.tx_hash,
        d.tx_from AS wallet_address,
        d.project,
        d.project_contract_address AS pair_address,
        d.token_sold_address AS token_address,
        d.token_sold_symbol AS token_symbol,
        CAST(d.token_sold_amount AS DOUBLE) AS token_amount,
        d.amount_usd,
        'sell' AS side
    FROM dex.trades d
    WHERE d.blockchain = 'bnb'
      AND d.block_month >= DATE '2020-08-01'
      AND d.block_time >= TIMESTAMP '2020-08-01 00:00:00'
      AND d.block_time < DATE_TRUNC('week', CURRENT_TIMESTAMP)
      AND d.tx_from IS NOT NULL
      AND d.token_sold_address IS NOT NULL
      AND d.amount_usd >= 5.0
      AND CAST(d.token_sold_amount AS DOUBLE) > 0
      AND UPPER(COALESCE(d.token_sold_symbol, '')) NOT IN ('USDC', 'USDT', 'DAI', 'USDBC', 'WETH', 'ETH', 'CBETH', 'EURC', 'CBBTC', 'WBTC', 'BTCB', 'WBNB', 'BNB', 'FDUSD', 'TUSD', 'USDE')
),
priced_flows AS (
    SELECT
        *,
        amount_usd / NULLIF(token_amount, 0) AS token_price_usd,
        DATE_TRUNC('week', block_time) AS week_start
    FROM token_flows
    WHERE token_address <> 0x0000000000000000000000000000000000000000
),
weekly AS (
    SELECT
        token_address,
        week_start,
        MAX_BY(token_symbol, block_time) AS token_symbol,
        MIN(block_time) AS first_trade_at,
        MAX(block_time) AS last_trade_at,
        COUNT(DISTINCT pair_address) AS pair_count,
        COUNT(DISTINCT project) AS dex_count,
        COUNT(DISTINCT wallet_address) AS trader_count,
        COUNT(DISTINCT CASE WHEN side = 'buy' THEN wallet_address END) AS buyer_count,
        COUNT(DISTINCT CASE WHEN side = 'sell' THEN wallet_address END) AS seller_count,
        COUNT(DISTINCT tx_hash) AS trade_count,
        COUNT(DISTINCT CASE WHEN side = 'buy' THEN tx_hash END) AS buy_trade_count,
        COUNT(DISTINCT CASE WHEN side = 'sell' THEN tx_hash END) AS sell_trade_count,
        SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE 0 END) AS gross_buy_usd,
        SUM(CASE WHEN side = 'sell' THEN amount_usd ELSE 0 END) AS gross_sell_usd,
        SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE -amount_usd END) AS net_flow_usd,
        SUM(amount_usd) AS volume_usd,
        MAX_BY(token_price_usd, block_time) AS close_price_usd,
        SUM(amount_usd) / NULLIF(SUM(token_amount), 0) AS vwap_price_usd
    FROM priced_flows
    WHERE token_price_usd > 0
      AND token_price_usd < 1e15
    GROUP BY 1, 2
),
eligible_weekly AS (
    SELECT *
    FROM weekly
    WHERE volume_usd >= 1000.0
      AND trade_count >= 3
      AND trader_count >= 1
),
follow_up_grid AS (
    SELECT
        seed.token_address,
        DATE_ADD('week', offset_week, seed.week_start) AS week_start,
        MAX_BY(seed.token_symbol, seed.week_start) AS seed_token_symbol
    FROM eligible_weekly seed
    CROSS JOIN UNNEST(SEQUENCE(0, 4)) AS offsets(offset_week)
    WHERE DATE_ADD('week', offset_week, seed.week_start)
          < DATE_TRUNC('week', CURRENT_TIMESTAMP)
    GROUP BY 1, 2
),
dense_weekly AS (
    SELECT
        grid.token_address,
        grid.week_start,
        COALESCE(actual.token_symbol, grid.seed_token_symbol) AS token_symbol,
        actual.first_trade_at,
        actual.last_trade_at,
        COALESCE(actual.pair_count, 0) AS pair_count,
        COALESCE(actual.dex_count, 0) AS dex_count,
        COALESCE(actual.trader_count, 0) AS trader_count,
        COALESCE(actual.buyer_count, 0) AS buyer_count,
        COALESCE(actual.seller_count, 0) AS seller_count,
        COALESCE(actual.trade_count, 0) AS trade_count,
        COALESCE(actual.buy_trade_count, 0) AS buy_trade_count,
        COALESCE(actual.sell_trade_count, 0) AS sell_trade_count,
        COALESCE(actual.gross_buy_usd, 0) AS gross_buy_usd,
        COALESCE(actual.gross_sell_usd, 0) AS gross_sell_usd,
        COALESCE(actual.net_flow_usd, 0) AS net_flow_usd,
        COALESCE(actual.volume_usd, 0) AS volume_usd,
        actual.close_price_usd,
        actual.vwap_price_usd,
        actual.token_address IS NULL AS is_dense_zero
    FROM follow_up_grid grid
    LEFT JOIN weekly actual
      ON actual.token_address = grid.token_address
     AND actual.week_start = grid.week_start
)
SELECT
    CAST(token_address AS VARCHAR) AS token_address,
    token_symbol,
    DATE_ADD('day', 7, week_start) AS snapshot_at,
    7 AS window_days,
    first_trade_at,
    last_trade_at,
    pair_count,
    dex_count,
    trader_count,
    buyer_count,
    seller_count,
    trade_count,
    buy_trade_count,
    sell_trade_count,
    gross_buy_usd,
    gross_sell_usd,
    net_flow_usd,
    volume_usd,
    close_price_usd,
    vwap_price_usd,
    is_dense_zero,
    CASE
        WHEN volume_usd >= 5000 AND trader_count >= 5 AND trade_count >= 10
        THEN TRUE ELSE FALSE
    END AS is_tradeable
FROM dense_weekly
ORDER BY snapshot_at, volume_usd DESC;
