-- Smart Money Radar generated Dune SQL
-- Purpose: find Base wallets that bought future Binance listing targets before announcement.
-- Generated from local token/listing registry.

WITH targets(symbol, token_address, announced_at) AS (
    VALUES
        ('OPG', 0xfbc2051ae2265686a469421b2c5a2d5462fbf5eb, TIMESTAMP '2026-05-22 07:18:35.961000'),
        ('ZKP', 0xc6c1be6c6d828f9cea70f1b8351879510fbf0065, TIMESTAMP '2026-01-07 10:39:06.580000'),
        ('BREV', 0x086f405146ce90135750bbec9a063a8b20a8bffb, TIMESTAMP '2026-01-05 10:04:14.724000'),
        ('ALLO', 0x032d86656db142138ac97d2c5c4e3766e8c0482d, TIMESTAMP '2025-11-11 04:58:30.748000'),
        ('ZBT', 0xfab99fcf605fd8f4593edb70a43ba56542777777, TIMESTAMP '2025-10-15 09:30:05.182000'),
        ('EUL', 0xa153ad732f831a79b5575fa02e793ec4e99181b0, TIMESTAMP '2025-10-13 09:22:48.185000'),
        ('MORPHO', 0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842, TIMESTAMP '2025-10-03 09:51:47.646000'),
        ('MIRA', 0x7aafd31a321d3627b30a8e2171264b56852187fe, TIMESTAMP '2025-09-25 10:00:03.425000'),
        ('AVNT', 0x696f9436b67233384889472cd7cd58a6fb5df4f1, TIMESTAMP '2025-09-07 13:00:14.505000'),
        ('SOMI', 0x47636b3188774a3e7273d85a537b9ba4ee7b2535, TIMESTAMP '2025-09-01 08:59:32.790000'),
        ('SAPIEN', 0xc729777d0470f30612b1564fd96e8dd26f5814e3, TIMESTAMP '2025-08-20 03:15:06.331000'),
        ('TOWNS', 0x00000000a22c618fd6b4d7e9a335c4b96b189a38, TIMESTAMP '2025-08-04 09:12:55.552000'),
        ('HOME', 0x4bfaa776991e85e5f8b1255461cbbd216cfc714f, TIMESTAMP '2025-06-09 07:01:00.757000'),
        ('SXT', 0xa2c22252cdc8b7cddee1b0b2e242818509fcf7b8, TIMESTAMP '2025-05-05 14:14:13.299000'),
        ('SIGN', 0x868fced65edbf0056c4163515dd840e9f287a4c3, TIMESTAMP '2025-04-25 09:35:15.532000'),
        ('PARTI', 0x59264f02d301281f3393e1385c0aefd446eb0f00, TIMESTAMP '2025-03-24 15:56:48.230000'),
        ('GPS', 0x0c1dc73159e30c4b06170f2593d3118968a0dca5, TIMESTAMP '2025-03-04 05:18:51.742000'),
        ('KAITO', 0x98d0baa52b2d063e780de12f615f963fe8537553, TIMESTAMP '2025-02-19 10:31:06.825000')
),
pre_listing_trades AS (
    SELECT
        t.symbol,
        t.token_address,
        t.announced_at,
        d.block_time,
        d.tx_hash,
        d.tx_from AS wallet_address,
        d.taker AS execution_address,
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
       AND d.block_time >= t.announced_at - INTERVAL '120' day
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.amount_usd IS NOT NULL
      AND d.tx_from IS NOT NULL
      AND d.tx_from <> 0x0000000000000000000000000000000000000000
)
SELECT
    symbol,
    token_address,
    announced_at,
    wallet_address,
    MIN(block_time) AS first_buy_at,
    MAX(block_time) AS last_buy_at,
    COUNT(DISTINCT tx_hash) AS buy_trade_count,
    SUM(amount_usd) AS gross_buy_usd
FROM pre_listing_trades
GROUP BY 1, 2, 3, 4
ORDER BY announced_at DESC, gross_buy_usd DESC;
