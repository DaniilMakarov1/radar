-- Smart Money Radar generated Dune SQL
-- Purpose: near-live Base token accumulation by qualified historical wallets.
-- Uses tx_from as the EVM signer and excludes known training target contracts.

WITH tracked_wallets(wallet_address, wallet_label, interest_score, confidence_score) AS (
    VALUES
        (0x9928ea67d8a617ca3d62b9f0856c03975935a296, 'strong_candidate', 100.0, 88.0),
        (0xdfd122610a14ac12d934898c02dbec1f72708116, 'strong_candidate', 90.31, 88.0),
        (0x07ae8551be970cb1cca11dd7a11f47ae82e70e67, 'strong_candidate', 89.74, 88.0),
        (0x2977b8919df6a60e93089e0f4231a28899005302, 'strong_candidate', 89.58, 88.0),
        (0x6ffc5848c46319e7c6d48f56ca2152b213d4535f, 'strong_candidate', 89.14, 88.0),
        (0x6333196a8590597733f6298361811b3a26bc4434, 'strong_candidate', 87.52, 88.0),
        (0xedad7dab5920e7bf1930ca6ba33ff3585a029ea5, 'strong_candidate', 86.7, 79.0),
        (0xf70da97812cb96acdf810712aa562db8dfa3dbef, 'strong_candidate', 86.62, 88.0),
        (0xc366e0a2ac4e50a84a3c7f7e1f1875e7da34f8d0, 'strong_candidate', 86.23, 79.0),
        (0x8a5221f95c8af2d249bc1a7f075b31336ee5032f, 'strong_candidate', 85.16, 79.0),
        (0xf28ef72ff457b2ab8463103b57de50910cd02d4f, 'strong_candidate', 84.44, 83.0),
        (0xdb61db256a30f3ef46110b8e2520aaec0db08153, 'strong_candidate', 81.33, 74.0),
        (0x5ccaf678b74464fac20f6cae8cbaa9b402010c45, 'strong_candidate', 81.1, 73.0),
        (0x09e5cdfeeac1866103e17e1debf4aad61c1904ef, 'strong_candidate', 80.94, 82.0),
        (0x7a1c8dae76df823da6883fe66ec5dee425848f53, 'strong_candidate', 80.6, 79.0),
        (0xe93685f3bba03016f02bd1828badd6195988d950, 'watch_candidate', 80.0, 80.0),
        (0xed055568655a1be46a5d5978c507a5ea0dadddd7, 'watch_candidate', 78.85, 88.0),
        (0x466b037ace44c0134dcebd965a4a22aed6dea027, 'strong_candidate', 77.73, 74.0),
        (0x2b44bcb3ef096a98aa6d2bdf7d12ac76261c3c0e, 'strong_candidate', 77.55, 73.0),
        (0x49b5fffed83583562537a1a77ca0419ab0e8f31e, 'strong_candidate', 75.95, 73.0),
        (0x09695c5c12481311a24332b5feb0e1c0b13e02ff, 'strong_candidate', 74.87, 74.0),
        (0xf94a2fad3ba66196b16dc55a22f11aea4b76dd87, 'strong_candidate', 73.28, 79.0),
        (0xa2e28dedaab59d732ae375832fb855510aa7fe57, 'watch_candidate', 71.49, 68.0),
        (0x8d47ba07ff9ccccf58c7e8810ee42c0dc8b8b123, 'watch_candidate', 70.09, 82.0),
        (0x182da9b68dcab4b50a9705ac9fb701eee62b7b59, 'watch_candidate', 69.0, 71.0),
        (0x488922276eabe3d122d3c4f4314555a8be2c7bf9, 'watch_candidate', 67.54, 79.0),
        (0x8695330488513a6c3698a2b072ff88aaedbfac3e, 'watch_candidate', 65.85, 74.0),
        (0xd2e2356b299ddb6ed1bead94f46246ba23b48571, 'watch_candidate', 65.0, 82.0),
        (0xa12fa23701f00ad2663e339386d65cd94a144cbc, 'watch_candidate', 64.83, 73.0),
        (0xff330ce76c26b55370c9263b7f6d0297255a6dda, 'watch_candidate', 64.04, 74.0),
        (0x0a1ac7d31142760c430d0bb7c801c3c727a81d69, 'watch_candidate', 62.73, 82.0),
        (0xeff023bb006da503414507062bb4b86291e8707f, 'watch_candidate', 62.35, 73.0),
        (0xe42524376600b15ed437b8d5f1195e8138989c2f, 'watch_candidate', 62.24, 74.0)
),
wallet_trades AS (
    SELECT
        d.block_time,
        d.tx_hash,
        d.tx_from AS wallet_address,
        w.wallet_label,
        w.interest_score,
        w.confidence_score,
        d.token_bought_address AS token_address,
        d.token_bought_symbol AS token_symbol,
        d.amount_usd,
        'buy' AS side
    FROM dex.trades d
    JOIN tracked_wallets w ON d.tx_from = w.wallet_address
    WHERE d.blockchain = 'base'
      AND d.block_time >= CURRENT_TIMESTAMP - INTERVAL '336' HOUR
      AND d.block_month >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '30' DAY)
      AND d.amount_usd >= 25.0
      AND d.token_bought_address IS NOT NULL
      AND UPPER(COALESCE(d.token_bought_symbol, '')) NOT IN (
          'USDC', 'USDT', 'DAI', 'USDBC', 'WETH', 'ETH', 'CBETH', 'EURC', 'CBBTC'
      )

    UNION ALL

    SELECT
        d.block_time,
        d.tx_hash,
        d.tx_from AS wallet_address,
        w.wallet_label,
        w.interest_score,
        w.confidence_score,
        d.token_sold_address AS token_address,
        d.token_sold_symbol AS token_symbol,
        d.amount_usd,
        'sell' AS side
    FROM dex.trades d
    JOIN tracked_wallets w ON d.tx_from = w.wallet_address
    WHERE d.blockchain = 'base'
      AND d.block_time >= CURRENT_TIMESTAMP - INTERVAL '336' HOUR
      AND d.block_month >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '30' DAY)
      AND d.amount_usd >= 25.0
      AND d.token_sold_address IS NOT NULL
      AND UPPER(COALESCE(d.token_sold_symbol, '')) NOT IN (
          'USDC', 'USDT', 'DAI', 'USDBC', 'WETH', 'ETH', 'CBETH', 'EURC', 'CBBTC'
      )
),
filtered_trades AS (
    SELECT *
    FROM wallet_trades
    WHERE token_address <> 0x0000000000000000000000000000000000000000
      AND token_address NOT IN (0x00000000a22c618fd6b4d7e9a335c4b96b189a38, 0x032d86656db142138ac97d2c5c4e3766e8c0482d, 0x086f405146ce90135750bbec9a063a8b20a8bffb, 0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b, 0x0c1dc73159e30c4b06170f2593d3118968a0dca5, 0x2ad3d80c917ddbf08acc04277f379e00e4d75395, 0x47636b3188774a3e7273d85a537b9ba4ee7b2535, 0x4bfaa776991e85e5f8b1255461cbbd216cfc714f, 0x4f9fd6be4a90f2620860d680c0d4d5fb53d1a825, 0x59264f02d301281f3393e1385c0aefd446eb0f00, 0x61cfc8dded821e1cf81cdb055a8789660e4c24ab, 0x688aee022aa544f150678b8e5720b6b96a9e9a2f, 0x696f9436b67233384889472cd7cd58a6fb5df4f1, 0x6a030f778a56aa31451044c673595413480ff4e3, 0x7aafd31a321d3627b30a8e2171264b56852187fe, 0x868fced65edbf0056c4163515dd840e9f287a4c3, 0x98d0baa52b2d063e780de12f615f963fe8537553, 0xa153ad732f831a79b5575fa02e793ec4e99181b0, 0xa2c22252cdc8b7cddee1b0b2e242818509fcf7b8, 0xac225b5611be42c10db0f7732bbee918ee79e85c, 0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842, 0xc0041ef357b183448b235a8ea73ce4e4ec8c265f, 0xc5ce8f730d540d4dbc210235933c51c0e5917ce5, 0xc6c1be6c6d828f9cea70f1b8351879510fbf0065, 0xc729777d0470f30612b1564fd96e8dd26f5814e3, 0xeb618089e20ee1a053f7c7cf38e193d3133cee02, 0xfab99fcf605fd8f4593edb70a43ba56542777777, 0xfbc2051ae2265686a469421b2c5a2d5462fbf5eb)
),
wallet_token_flows AS (
    SELECT
        token_address,
        MAX(token_symbol) AS token_symbol,
        wallet_address,
        MAX(wallet_label) AS wallet_label,
        MAX(interest_score) AS interest_score,
        MAX(confidence_score) AS confidence_score,
        SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE 0 END) AS gross_buy_usd,
        SUM(CASE WHEN side = 'sell' THEN amount_usd ELSE 0 END) AS gross_sell_usd,
        SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE -amount_usd END) AS net_buy_usd,
        COUNT(DISTINCT CASE WHEN side = 'buy' THEN tx_hash END) AS buy_trade_count,
        COUNT(DISTINCT CASE WHEN side = 'sell' THEN tx_hash END) AS sell_trade_count,
        MIN(block_time) AS first_trade_at,
        MAX(block_time) AS last_trade_at
    FROM filtered_trades
    GROUP BY 1, 3
)
SELECT
    CURRENT_TIMESTAMP AS observed_at,
    336 AS window_hours,
    token_address,
    MAX(token_symbol) AS token_symbol,
    COUNT(*) FILTER (WHERE net_buy_usd > 0) AS tracked_wallet_count,
    COUNT(*) FILTER (
        WHERE net_buy_usd > 0 AND wallet_label = 'strong_candidate'
    ) AS strong_wallet_count,
    COUNT(*) FILTER (
        WHERE net_buy_usd > 0 AND wallet_label = 'watch_candidate'
    ) AS watch_wallet_count,
    SUM(gross_buy_usd) AS gross_buy_usd,
    SUM(gross_sell_usd) AS gross_sell_usd,
    SUM(net_buy_usd) AS net_buy_usd,
    SUM(buy_trade_count) AS buy_trade_count,
    SUM(sell_trade_count) AS sell_trade_count,
    MIN(first_trade_at) AS first_trade_at,
    MAX(last_trade_at) AS last_trade_at,
    AVG(interest_score) FILTER (WHERE net_buy_usd > 0) AS weighted_wallet_score,
    AVG(confidence_score) FILTER (WHERE net_buy_usd > 0) AS average_wallet_confidence,
    ARRAY_AGG(CAST(wallet_address AS VARCHAR))
        FILTER (WHERE net_buy_usd > 0) AS accumulating_wallet_addresses,
    ARRAY_AGG(net_buy_usd)
        FILTER (WHERE net_buy_usd > 0) AS accumulating_wallet_net_buy_usd
FROM wallet_token_flows
GROUP BY token_address
HAVING SUM(net_buy_usd) >= 100
ORDER BY net_buy_usd DESC;
