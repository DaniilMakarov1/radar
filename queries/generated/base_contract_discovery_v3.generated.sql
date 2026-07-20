-- Smart Money Radar generated Dune SQL
-- Purpose: discover Base contracts from DEX activity strictly before each Binance cutoff.
-- This query is an identity-mapping aid, not a trading feature or a post-listing lookup.

WITH targets(symbol, announced_at) AS (
    VALUES
        ('RE', TIMESTAMP '2026-06-18 09:47:21.424000'),
        ('GENIUS', TIMESTAMP '2026-05-22 07:18:35.961000'),
        ('OPG', TIMESTAMP '2026-05-22 07:18:35.961000'),
        ('AIGENSYN', TIMESTAMP '2026-05-14 10:00:03.676000'),
        ('MEGA', TIMESTAMP '2026-04-30 09:07:29.704000'),
        ('CHIP', TIMESTAMP '2026-04-21 12:00:32.132000'),
        ('CFG', TIMESTAMP '2026-03-16 10:22:39.260000'),
        ('KAT', TIMESTAMP '2026-03-13 01:45:36.337000'),
        ('NIGHT', TIMESTAMP '2026-03-11 11:31:21.283000'),
        ('ROBO', TIMESTAMP '2026-03-04 13:13:14.452000'),
        ('OPN', TIMESTAMP '2026-03-02 12:45:51.819000'),
        ('ESP', TIMESTAMP '2026-02-12 07:28:23.176000'),
        ('ZAMA', TIMESTAMP '2026-02-02 04:28:43.224000'),
        ('SENT', TIMESTAMP '2026-01-22 05:25:54.238000'),
        ('FOGO', TIMESTAMP '2026-01-12 12:50:53.294000'),
        ('ZKP', TIMESTAMP '2026-01-07 10:39:06.580000'),
        ('BREV', TIMESTAMP '2026-01-05 10:04:14.724000'),
        ('AT', TIMESTAMP '2025-11-27 08:38:18.399000'),
        ('BANK', TIMESTAMP '2025-11-13 10:00:26.501000'),
        ('MET', TIMESTAMP '2025-11-13 10:00:26.501000'),
        ('ALLO', TIMESTAMP '2025-11-11 04:58:30.748000'),
        ('SAPIEN', TIMESTAMP '2025-11-06 04:45:41.669000'),
        ('MMT', TIMESTAMP '2025-11-03 08:14:11.430000'),
        ('KITE', TIMESTAMP '2025-10-31 08:09:28.970000'),
        ('TURTLE', TIMESTAMP '2025-10-21 07:56:49.816000'),
        ('ZBT', TIMESTAMP '2025-10-17 09:53:33.652000'),
        ('YB', TIMESTAMP '2025-10-14 09:46:27.057000'),
        ('ENSO', TIMESTAMP '2025-10-14 02:58:32.966000'),
        ('EUL', TIMESTAMP '2025-10-13 09:22:48.185000'),
        ('WAL', TIMESTAMP '2025-10-10 02:21:21.098000'),
        ('ASTER', TIMESTAMP '2025-10-06 07:48:49.954000'),
        ('MORPHO', TIMESTAMP '2025-10-03 09:51:47.646000'),
        ('2Z', TIMESTAMP '2025-10-02 03:18:25.856000'),
        ('EDEN', TIMESTAMP '2025-09-29 08:30:37.707000'),
        ('FF', TIMESTAMP '2025-09-26 07:52:42.488000'),
        ('MIRA', TIMESTAMP '2025-09-25 10:00:03.425000'),
        ('XPL', TIMESTAMP '2025-09-24 08:00:04.069000'),
        ('HEMI', TIMESTAMP '2025-09-23 05:09:17.058000'),
        ('0G', TIMESTAMP '2025-09-21 08:58:17.740000'),
        ('BARD', TIMESTAMP '2025-09-17 09:57:50.745000'),
        ('AVNT', TIMESTAMP '2025-09-15 03:47:01.934000'),
        ('ZKC', TIMESTAMP '2025-09-12 09:57:33.626000'),
        ('PUMP', TIMESTAMP '2025-09-11 11:37:53.797000'),
        ('HOLO', TIMESTAMP '2025-09-10 05:11:59.424000'),
        ('USDE', TIMESTAMP '2025-09-09 07:00:01.439000'),
        ('LINEA', TIMESTAMP '2025-09-08 14:26:09.314000'),
        ('OPEN', TIMESTAMP '2025-09-05 07:18:31.526000'),
        ('SOMI', TIMESTAMP '2025-09-01 08:59:32.790000'),
        ('WLFI', TIMESTAMP '2025-09-01 01:49:05.082000'),
        ('MITO', TIMESTAMP '2025-08-29 09:15:57.261000'),
        ('DOLO', TIMESTAMP '2025-08-27 10:43:22.012000'),
        ('PLUME', TIMESTAMP '2025-08-18 09:36:06.933000'),
        ('PROVE', TIMESTAMP '2025-08-05 01:59:17.964000'),
        ('TOWNS', TIMESTAMP '2025-08-04 09:12:55.552000'),
        ('TREE', TIMESTAMP '2025-07-28 13:30:11.683000'),
        ('ERA', TIMESTAMP '2025-07-16 08:58:10.021000'),
        ('LA', TIMESTAMP '2025-07-09 09:58:15.037000'),
        ('SAHARA', TIMESTAMP '2025-06-24 14:29:12.649000'),
        ('NEWT', TIMESTAMP '2025-06-23 14:08:19.878000'),
        ('SPK', TIMESTAMP '2025-06-16 11:33:20.186000'),
        ('HOME', TIMESTAMP '2025-06-12 11:04:44.075000'),
        ('SOPH', TIMESTAMP '2025-05-28 07:16:56.367000'),
        ('HUMA', TIMESTAMP '2025-05-22 10:46:16.085000'),
        ('USD1', TIMESTAMP '2025-05-22 03:30:01.965000'),
        ('HAEDAL', TIMESTAMP '2025-05-21 10:04:15.575000'),
        ('NXPC', TIMESTAMP '2025-05-15 04:36:30.122000'),
        ('KMNO', TIMESTAMP '2025-05-06 11:25:20.814000'),
        ('SYRUP', TIMESTAMP '2025-05-06 11:25:20.814000'),
        ('SXT', TIMESTAMP '2025-05-05 14:14:13.299000'),
        ('STO', TIMESTAMP '2025-05-02 10:21:22.511000'),
        ('SIGN', TIMESTAMP '2025-04-25 09:35:15.532000'),
        ('HYPER', TIMESTAMP '2025-04-21 15:11:51.577000'),
        ('INIT', TIMESTAMP '2025-04-17 08:59:59.889000'),
        ('BIGTIME', TIMESTAMP '2025-04-11 10:08:34.456000'),
        ('ONDO', TIMESTAMP '2025-04-11 10:08:34.456000'),
        ('VIRTUAL', TIMESTAMP '2025-04-11 10:08:34.456000'),
        ('WCT', TIMESTAMP '2025-04-10 10:15:06.069000'),
        ('BABY', TIMESTAMP '2025-04-09 08:42:17.154000'),
        ('KERNEL', TIMESTAMP '2025-04-01 08:42:57.029000'),
        ('BANANAS31', TIMESTAMP '2025-03-27 17:27:12.114000'),
        ('BROCCOLI714', TIMESTAMP '2025-03-27 17:27:12.114000'),
        ('MUBARAK', TIMESTAMP '2025-03-27 17:27:12.114000'),
        ('TUT', TIMESTAMP '2025-03-27 17:27:12.114000'),
        ('GUN', TIMESTAMP '2025-03-27 13:29:21.025000'),
        ('PARTI', TIMESTAMP '2025-03-24 15:56:48.230000'),
        ('NIL', TIMESTAMP '2025-03-20 10:14:39.371000'),
        ('BMT', TIMESTAMP '2025-03-18 09:52:05.581000'),
        ('GPS', TIMESTAMP '2025-03-04 05:18:51.742000'),
        ('SHELL', TIMESTAMP '2025-02-27 05:15:20.602000'),
        ('RED', TIMESTAMP '2025-02-25 09:11:14.392000'),
        ('KAITO', TIMESTAMP '2025-02-19 10:31:06.825000'),
        ('LAYER', TIMESTAMP '2025-02-11 06:43:29.498000'),
        ('1000CHEEMS', TIMESTAMP '2025-02-09 07:10:31.264000'),
        ('TST', TIMESTAMP '2025-02-09 07:10:31.264000'),
        ('BERA', TIMESTAMP '2025-02-05 12:19:06.573000'),
        ('ANIME', TIMESTAMP '2025-01-22 10:02:43.943000'),
        ('TRUMP', TIMESTAMP '2025-01-19 04:05:42.151000'),
        ('AIXBT', TIMESTAMP '2025-01-10 08:55:27.639000'),
        ('CGPT', TIMESTAMP '2025-01-10 08:55:27.639000'),
        ('COOKIE', TIMESTAMP '2025-01-10 08:55:27.639000'),
        ('SOLV', TIMESTAMP '2024-12-30 10:22:57.872000'),
        ('BIO', TIMESTAMP '2024-12-23 10:19:12.975000'),
        ('1000CAT', TIMESTAMP '2024-12-16 12:36:45.627000'),
        ('PENGU', TIMESTAMP '2024-12-16 12:36:45.627000'),
        ('VANA', TIMESTAMP '2024-12-13 08:59:48.395000'),
        ('VELODROME', TIMESTAMP '2024-12-13 08:07:57.826000'),
        ('ME', TIMESTAMP '2024-12-10 07:22:20.546000'),
        ('MOVE', TIMESTAMP '2024-12-09 08:29:16.897000'),
        ('ACX', TIMESTAMP '2024-12-06 08:34:18.953000'),
        ('ORCA', TIMESTAMP '2024-12-06 08:34:18.953000'),
        ('THE', TIMESTAMP '2024-11-26 07:57:32.153000'),
        ('USUAL', TIMESTAMP '2024-11-14 07:32:09.354000'),
        ('ACT', TIMESTAMP '2024-11-11 05:39:29.462000'),
        ('PNUT', TIMESTAMP '2024-11-11 05:39:29.462000'),
        ('CETUS', TIMESTAMP '2024-11-06 05:10:07.602000'),
        ('COW', TIMESTAMP '2024-11-06 05:10:07.602000'),
        ('BNSOL', TIMESTAMP '2024-10-09 07:00:13.876000'),
        ('SCR', TIMESTAMP '2024-10-08 08:15:33'),
        ('EIGEN', TIMESTAMP '2024-09-30 08:19:57.115000'),
        ('1MBABYDOGE', TIMESTAMP '2024-09-16 05:45:02.167000'),
        ('NEIRO', TIMESTAMP '2024-09-16 05:45:02.167000'),
        ('TURBO', TIMESTAMP '2024-09-16 05:45:02.167000'),
        ('CATI', TIMESTAMP '2024-09-13 12:32:48.590000'),
        ('HMSTR', TIMESTAMP '2024-09-12 15:01:39.022000'),
        ('DOGS', TIMESTAMP '2024-08-20 16:42:01.684000'),
        ('TON', TIMESTAMP '2024-08-13 12:12:43.618000')
),
observed_trades AS (
    SELECT
        t.symbol,
        t.announced_at,
        d.token_bought_address AS token_address,
        d.tx_hash,
        d.block_time,
        d.amount_usd
    FROM dex.trades d
    JOIN targets t
      ON UPPER(COALESCE(d.token_bought_symbol, '')) = t.symbol
     AND d.block_time < t.announced_at
     AND d.block_time >= t.announced_at - INTERVAL '120' DAY
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.token_bought_address IS NOT NULL
      AND d.amount_usd >= 25

    UNION ALL

    SELECT
        t.symbol,
        t.announced_at,
        d.token_sold_address AS token_address,
        d.tx_hash,
        d.block_time,
        d.amount_usd
    FROM dex.trades d
    JOIN targets t
      ON UPPER(COALESCE(d.token_sold_symbol, '')) = t.symbol
     AND d.block_time < t.announced_at
     AND d.block_time >= t.announced_at - INTERVAL '120' DAY
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.token_sold_address IS NOT NULL
      AND d.amount_usd >= 25
),
candidate_contracts AS (
    SELECT
        symbol,
        announced_at,
        token_address,
        COUNT(DISTINCT tx_hash) AS trade_count,
        SUM(amount_usd) AS gross_notional_usd,
        MIN(block_time) AS first_trade_at,
        MAX(block_time) AS last_trade_at
    FROM observed_trades
    WHERE token_address <> 0x0000000000000000000000000000000000000000
    GROUP BY 1, 2, 3
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, announced_at
            ORDER BY gross_notional_usd DESC, trade_count DESC, token_address
        ) AS candidate_rank,
        SUM(gross_notional_usd) OVER (
            PARTITION BY symbol, announced_at
        ) AS total_candidate_notional_usd
    FROM candidate_contracts
)
SELECT
    symbol,
    announced_at,
    token_address,
    trade_count,
    gross_notional_usd,
    first_trade_at,
    last_trade_at,
    candidate_rank,
    total_candidate_notional_usd,
    gross_notional_usd / NULLIF(total_candidate_notional_usd, 0) AS notional_share
FROM ranked
WHERE candidate_rank <= 3
  AND gross_notional_usd >= 1000
ORDER BY announced_at, candidate_rank, gross_notional_usd DESC;
