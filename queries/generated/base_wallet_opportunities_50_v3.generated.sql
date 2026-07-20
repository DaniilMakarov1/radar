-- Smart Money Radar Research Validity V2
-- Complete DEX opportunity denominator for selected base wallets.

WITH wallets(wallet_address) AS (
    VALUES
        (0x9928ea67d8a617ca3d62b9f0856c03975935a296),
        (0xf70da97812cb96acdf810712aa562db8dfa3dbef),
        (0xed055568655a1be46a5d5978c507a5ea0dadddd7),
        (0xdfd122610a14ac12d934898c02dbec1f72708116),
        (0x2977b8919df6a60e93089e0f4231a28899005302),
        (0x6ffc5848c46319e7c6d48f56ca2152b213d4535f),
        (0xe93685f3bba03016f02bd1828badd6195988d950),
        (0x07ae8551be970cb1cca11dd7a11f47ae82e70e67),
        (0x6333196a8590597733f6298361811b3a26bc4434),
        (0xf28ef72ff457b2ab8463103b57de50910cd02d4f),
        (0x09e5cdfeeac1866103e17e1debf4aad61c1904ef),
        (0x8d47ba07ff9ccccf58c7e8810ee42c0dc8b8b123),
        (0xd2e2356b299ddb6ed1bead94f46246ba23b48571),
        (0x0a1ac7d31142760c430d0bb7c801c3c727a81d69),
        (0xedad7dab5920e7bf1930ca6ba33ff3585a029ea5),
        (0xc366e0a2ac4e50a84a3c7f7e1f1875e7da34f8d0),
        (0x8a5221f95c8af2d249bc1a7f075b31336ee5032f),
        (0xdb61db256a30f3ef46110b8e2520aaec0db08153),
        (0x5ccaf678b74464fac20f6cae8cbaa9b402010c45),
        (0x7a1c8dae76df823da6883fe66ec5dee425848f53),
        (0x466b037ace44c0134dcebd965a4a22aed6dea027),
        (0x2b44bcb3ef096a98aa6d2bdf7d12ac76261c3c0e),
        (0x49b5fffed83583562537a1a77ca0419ab0e8f31e),
        (0x09695c5c12481311a24332b5feb0e1c0b13e02ff),
        (0xf94a2fad3ba66196b16dc55a22f11aea4b76dd87),
        (0xa2e28dedaab59d732ae375832fb855510aa7fe57),
        (0x182da9b68dcab4b50a9705ac9fb701eee62b7b59),
        (0x488922276eabe3d122d3c4f4314555a8be2c7bf9),
        (0x8695330488513a6c3698a2b072ff88aaedbfac3e),
        (0xa12fa23701f00ad2663e339386d65cd94a144cbc),
        (0xff330ce76c26b55370c9263b7f6d0297255a6dda),
        (0xeff023bb006da503414507062bb4b86291e8707f),
        (0xe42524376600b15ed437b8d5f1195e8138989c2f),
        (0x531a87a869d9dfc4c79216bbf393e28722fce021),
        (0x1b1aa8f5830db779e905872d75ea13e688c897ef),
        (0x25dde1cf4da8c34dd2d2abb3fee937b7ba4a5d63),
        (0x41dda7be30130cebd867f439a759b9e7ab2569e9),
        (0x4b3f1048c55faa0c0873e249e541139360501f2a),
        (0x4fba13ba676bca8bbba5e539d0480d5756472f9a),
        (0x9390e7d8d72a5f5d280d63ad35aff943cc98b01a),
        (0xa8b087fc770a634dd2d7fe71eb1fa898af3c7a49),
        (0xb1a5b42808c2140804f5ce2e2dd2be0cee828513),
        (0xcf16fdf71110115c87a45fec1447d9f3518245d6),
        (0xcf4dfc7786b07ae22088cf12f026b39e469cb6bc),
        (0xeea279620216dbccc0f1b7fa91c3bb78b02fbe75),
        (0x30f89f5f4cf25f457b4d9cbfb64a64a97f00bed0),
        (0xb3aa47edbc9a1178b56bb55d1a9e3821845870e8),
        (0x2466a85341e3a7f24bde0d7e1e055b15617fef91),
        (0x66beda7a27e8fc78d29ec7871328251fcbe217ea),
        (0x7aff8cded4aaea38d45ff7fb89668920ac113dd8)
),
token_flows AS (
    SELECT
        d.tx_from AS wallet_address,
        d.token_bought_address AS token_address,
        d.token_bought_symbol AS token_symbol,
        d.block_time,
        d.tx_hash,
        d.amount_usd,
        CAST(d.token_bought_amount AS DOUBLE) AS token_amount,
        'buy' AS side
    FROM dex.trades d
    JOIN wallets w ON w.wallet_address = d.tx_from
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2023-08-01'
      AND d.block_time >= TIMESTAMP '2023-08-01 00:00:00'
      AND d.block_time < CURRENT_TIMESTAMP
      AND d.amount_usd >= 10.0
      AND d.token_bought_address IS NOT NULL
      AND UPPER(COALESCE(d.token_bought_symbol, '')) NOT IN ('USDC', 'USDT', 'DAI', 'USDBC', 'WETH', 'ETH', 'CBETH', 'EURC', 'CBBTC', 'WBTC', 'BTCB', 'WBNB', 'BNB', 'FDUSD', 'TUSD', 'USDE')

    UNION ALL

    SELECT
        d.tx_from AS wallet_address,
        d.token_sold_address AS token_address,
        d.token_sold_symbol AS token_symbol,
        d.block_time,
        d.tx_hash,
        d.amount_usd,
        CAST(d.token_sold_amount AS DOUBLE) AS token_amount,
        'sell' AS side
    FROM dex.trades d
    JOIN wallets w ON w.wallet_address = d.tx_from
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2023-08-01'
      AND d.block_time >= TIMESTAMP '2023-08-01 00:00:00'
      AND d.block_time < CURRENT_TIMESTAMP
      AND d.amount_usd >= 10.0
      AND d.token_sold_address IS NOT NULL
      AND UPPER(COALESCE(d.token_sold_symbol, '')) NOT IN ('USDC', 'USDT', 'DAI', 'USDBC', 'WETH', 'ETH', 'CBETH', 'EURC', 'CBBTC', 'WBTC', 'BTCB', 'WBNB', 'BNB', 'FDUSD', 'TUSD', 'USDE')
),
ordered_buys AS (
    SELECT
        wallet_address,
        token_address,
        block_time,
        tx_hash,
        LAG(block_time) OVER (
            PARTITION BY wallet_address, token_address
            ORDER BY block_time, tx_hash
        ) AS previous_buy_at
    FROM token_flows
    WHERE side = 'buy'
),
segmented_buys AS (
    SELECT
        *,
        SUM(
            CASE
                WHEN previous_buy_at IS NULL
                  OR block_time > previous_buy_at + INTERVAL '90' DAY
                THEN 1 ELSE 0
            END
        ) OVER (
            PARTITION BY wallet_address, token_address
            ORDER BY block_time, tx_hash
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS episode_number
    FROM ordered_buys
),
episode_anchors AS (
    SELECT
        wallet_address,
        token_address,
        episode_number,
        MIN(block_time) AS episode_start_at,
        MIN_BY(tx_hash, block_time) AS entry_tx_hash
    FROM segmented_buys
    GROUP BY 1, 2, 3
),
episode_flows AS (
    SELECT
        anchor.wallet_address,
        anchor.token_address,
        anchor.episode_number,
        anchor.episode_start_at,
        flow.token_symbol,
        flow.block_time,
        flow.tx_hash,
        flow.amount_usd,
        flow.token_amount,
        flow.side
    FROM episode_anchors anchor
    JOIN token_flows flow
      ON flow.wallet_address = anchor.wallet_address
     AND flow.token_address = anchor.token_address
     AND flow.block_time >= anchor.episode_start_at
     AND flow.block_time < anchor.episode_start_at + INTERVAL '90' DAY
),
wallet_prior_flows AS (
    SELECT
        anchor.wallet_address,
        anchor.token_address,
        anchor.episode_number,
        SUM(flow.amount_usd) AS wallet_prior_buy_usd
    FROM episode_anchors anchor
    LEFT JOIN token_flows flow
      ON flow.wallet_address = anchor.wallet_address
     AND flow.side = 'buy'
     AND flow.block_time < anchor.episode_start_at
     AND flow.block_time >= anchor.episode_start_at - INTERVAL '365' DAY
    GROUP BY 1, 2, 3
),
positions AS (
    SELECT
        wallet_address,
        token_address,
        episode_number,
        episode_start_at,
        MAX_BY(token_symbol, block_time) AS token_symbol,
        MIN(CASE WHEN side = 'buy' THEN block_time END) AS first_buy_at,
        MAX(CASE WHEN side = 'buy' THEN block_time END) AS last_buy_at,
        MIN(CASE WHEN side = 'sell' THEN block_time END) AS first_sell_at,
        MAX(CASE WHEN side = 'sell' THEN block_time END) AS last_sell_at,
        COUNT(DISTINCT CASE WHEN side = 'buy' THEN tx_hash END) AS buy_trade_count,
        COUNT(DISTINCT CASE WHEN side = 'sell' THEN tx_hash END) AS sell_trade_count,
        COUNT(DISTINCT DATE(block_time)) AS active_day_count,
        SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE 0 END) AS gross_buy_usd,
        SUM(CASE WHEN side = 'sell' THEN amount_usd ELSE 0 END) AS gross_sell_usd,
        SUM(CASE WHEN side = 'buy' THEN token_amount ELSE 0 END) AS token_bought_amount,
        SUM(CASE WHEN side = 'sell' THEN token_amount ELSE 0 END) AS token_sold_amount
    FROM episode_flows
    WHERE token_address <> 0x0000000000000000000000000000000000000000
    GROUP BY 1, 2, 3, 4
)
SELECT
    CAST(p.wallet_address AS VARCHAR) AS wallet_address,
    CAST(p.token_address AS VARCHAR) AS token_address,
    p.token_symbol,
    p.episode_number,
    p.episode_start_at + INTERVAL '90' DAY AS episode_end_at,
    p.first_buy_at,
    p.last_buy_at,
    p.first_sell_at,
    p.last_sell_at,
    p.buy_trade_count,
    p.sell_trade_count,
    p.active_day_count,
    p.gross_buy_usd,
    p.gross_sell_usd,
    p.gross_buy_usd - p.gross_sell_usd AS net_cash_flow_usd,
    p.token_bought_amount,
    p.token_sold_amount,
    p.gross_buy_usd / NULLIF(p.token_bought_amount, 0) AS average_buy_price_usd,
    p.gross_sell_usd / NULLIF(p.token_sold_amount, 0) AS average_sell_price_usd,
    COALESCE(prior.wallet_prior_buy_usd, 0) + p.gross_buy_usd AS wallet_observed_buy_usd,
    p.gross_buy_usd / NULLIF(
        COALESCE(prior.wallet_prior_buy_usd, 0) + p.gross_buy_usd,
        0
    ) AS position_to_observed_flow,
    (p.gross_buy_usd + p.gross_sell_usd) / NULLIF(p.gross_buy_usd, 0) AS turnover_ratio
FROM positions p
LEFT JOIN wallet_prior_flows prior
  ON prior.wallet_address = p.wallet_address
 AND prior.token_address = p.token_address
 AND prior.episode_number = p.episode_number
WHERE p.first_buy_at IS NOT NULL
  AND p.gross_buy_usd >= 100.0
ORDER BY wallet_address, first_buy_at, gross_buy_usd DESC;
