-- Smart Money Radar generated Dune SQL
-- Purpose: find Base wallets that bought future Binance listing targets before announcement.
-- Generated from local token/listing registry.

WITH targets(symbol, token_address, announced_at) AS (
    VALUES
        ('OPG', 0xfbc2051ae2265686a469421b2c5a2d5462fbf5eb, TIMESTAMP '2026-05-22 07:18:35.961000'),
        ('ESP', 0xc5ce8f730d540d4dbc210235933c51c0e5917ce5, TIMESTAMP '2026-02-12 07:28:23.176000'),
        ('FOGO', 0x61cfc8dded821e1cf81cdb055a8789660e4c24ab, TIMESTAMP '2026-01-12 12:50:53.294000'),
        ('ZKP', 0xc6c1be6c6d828f9cea70f1b8351879510fbf0065, TIMESTAMP '2026-01-07 10:39:06.580000'),
        ('BREV', 0x086f405146ce90135750bbec9a063a8b20a8bffb, TIMESTAMP '2026-01-05 10:04:14.724000'),
        ('ALLO', 0x032d86656db142138ac97d2c5c4e3766e8c0482d, TIMESTAMP '2025-11-11 04:58:30.748000'),
        ('SAPIEN', 0xc729777d0470f30612b1564fd96e8dd26f5814e3, TIMESTAMP '2025-11-06 04:45:41.669000'),
        ('ZBT', 0xfab99fcf605fd8f4593edb70a43ba56542777777, TIMESTAMP '2025-10-17 09:53:33.652000'),
        ('EUL', 0xa153ad732f831a79b5575fa02e793ec4e99181b0, TIMESTAMP '2025-10-13 09:22:48.185000'),
        ('MORPHO', 0xbaa5cc21fd487b8fcc2f632f3f4e8d37262a0842, TIMESTAMP '2025-10-03 09:51:47.646000'),
        ('MIRA', 0x7aafd31a321d3627b30a8e2171264b56852187fe, TIMESTAMP '2025-09-25 10:00:03.425000'),
        ('AVNT', 0x696f9436b67233384889472cd7cd58a6fb5df4f1, TIMESTAMP '2025-09-15 03:47:01.934000'),
        ('SOMI', 0x47636b3188774a3e7273d85a537b9ba4ee7b2535, TIMESTAMP '2025-09-01 08:59:32.790000'),
        ('TOWNS', 0x00000000a22c618fd6b4d7e9a335c4b96b189a38, TIMESTAMP '2025-08-04 09:12:55.552000'),
        ('ERA', 0xeb618089e20ee1a053f7c7cf38e193d3133cee02, TIMESTAMP '2025-07-16 08:58:10.021000'),
        ('HOME', 0x4bfaa776991e85e5f8b1255461cbbd216cfc714f, TIMESTAMP '2025-06-12 11:04:44.075000'),
        ('SYRUP', 0x688aee022aa544f150678b8e5720b6b96a9e9a2f, TIMESTAMP '2025-05-06 11:25:20.814000'),
        ('SXT', 0xa2c22252cdc8b7cddee1b0b2e242818509fcf7b8, TIMESTAMP '2025-05-05 14:14:13.299000'),
        ('SIGN', 0x868fced65edbf0056c4163515dd840e9f287a4c3, TIMESTAMP '2025-04-25 09:35:15.532000'),
        ('VIRTUAL', 0x0b3e328455c4059eeb9e3f84b5543f74e24e7e1b, TIMESTAMP '2025-04-11 10:08:34.456000'),
        ('PARTI', 0x59264f02d301281f3393e1385c0aefd446eb0f00, TIMESTAMP '2025-03-24 15:56:48.230000'),
        ('GPS', 0x0c1dc73159e30c4b06170f2593d3118968a0dca5, TIMESTAMP '2025-03-04 05:18:51.742000'),
        ('KAITO', 0x98d0baa52b2d063e780de12f615f963fe8537553, TIMESTAMP '2025-02-19 10:31:06.825000'),
        ('AIXBT', 0x4f9fd6be4a90f2620860d680c0d4d5fb53d1a825, TIMESTAMP '2025-01-10 08:55:27.639000'),
        ('COOKIE', 0xc0041ef357b183448b235a8ea73ce4e4ec8c265f, TIMESTAMP '2025-01-10 08:55:27.639000'),
        ('COW', 0x2ad3d80c917ddbf08acc04277f379e00e4d75395, TIMESTAMP '2024-11-06 05:10:07.602000'),
        ('EIGEN', 0x6a030f778a56aa31451044c673595413480ff4e3, TIMESTAMP '2024-09-30 08:19:57.115000'),
        ('TON', 0xac225b5611be42c10db0f7732bbee918ee79e85c, TIMESTAMP '2024-08-13 12:12:43.618000')
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
HAVING SUM(amount_usd) >= 100.0
ORDER BY announced_at DESC, gross_buy_usd DESC;
