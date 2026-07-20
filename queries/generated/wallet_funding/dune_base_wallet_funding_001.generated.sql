-- Smart Money Radar generated Dune SQL
-- Purpose: find the first meaningful native-ETH funder for Base wallet clustering.

WITH wallets(wallet_address, first_activity_at) AS (
    VALUES
        (0x9928ea67d8a617ca3d62b9f0856c03975935a296, TIMESTAMP '2024-11-27 10:38:47'),
        (0xdfd122610a14ac12d934898c02dbec1f72708116, TIMESTAMP '2025-06-07 16:57:57'),
        (0x07ae8551be970cb1cca11dd7a11f47ae82e70e67, TIMESTAMP '2025-06-10 17:11:11'),
        (0x2977b8919df6a60e93089e0f4231a28899005302, TIMESTAMP '2025-08-19 03:51:45'),
        (0x6ffc5848c46319e7c6d48f56ca2152b213d4535f, TIMESTAMP '2025-06-17 19:56:53'),
        (0x6333196a8590597733f6298361811b3a26bc4434, TIMESTAMP '2025-01-16 10:01:07'),
        (0xedad7dab5920e7bf1930ca6ba33ff3585a029ea5, TIMESTAMP '2024-11-26 09:58:25'),
        (0xf70da97812cb96acdf810712aa562db8dfa3dbef, TIMESTAMP '2025-06-06 15:07:07'),
        (0xc366e0a2ac4e50a84a3c7f7e1f1875e7da34f8d0, TIMESTAMP '2025-09-06 11:50:37'),
        (0x8a5221f95c8af2d249bc1a7f075b31336ee5032f, TIMESTAMP '2025-06-10 19:31:13'),
        (0xf28ef72ff457b2ab8463103b57de50910cd02d4f, TIMESTAMP '2024-12-30 14:05:27'),
        (0xdb61db256a30f3ef46110b8e2520aaec0db08153, TIMESTAMP '2025-07-13 02:30:01'),
        (0x5ccaf678b74464fac20f6cae8cbaa9b402010c45, TIMESTAMP '2024-11-15 16:01:27'),
        (0x09e5cdfeeac1866103e17e1debf4aad61c1904ef, TIMESTAMP '2025-06-10 16:59:43'),
        (0x7a1c8dae76df823da6883fe66ec5dee425848f53, TIMESTAMP '2024-08-13 18:53:15'),
        (0xe93685f3bba03016f02bd1828badd6195988d950, TIMESTAMP '2024-11-30 08:26:41'),
        (0xed055568655a1be46a5d5978c507a5ea0dadddd7, TIMESTAMP '2024-12-01 12:21:01'),
        (0x466b037ace44c0134dcebd965a4a22aed6dea027, TIMESTAMP '2025-02-08 09:31:05'),
        (0x2b44bcb3ef096a98aa6d2bdf7d12ac76261c3c0e, TIMESTAMP '2024-11-29 03:39:55'),
        (0x49b5fffed83583562537a1a77ca0419ab0e8f31e, TIMESTAMP '2024-12-20 03:55:35'),
        (0x09695c5c12481311a24332b5feb0e1c0b13e02ff, TIMESTAMP '2025-01-26 00:33:01'),
        (0xf94a2fad3ba66196b16dc55a22f11aea4b76dd87, TIMESTAMP '2025-01-16 07:54:17'),
        (0xa2e28dedaab59d732ae375832fb855510aa7fe57, TIMESTAMP '2025-08-04 22:12:43'),
        (0x8d47ba07ff9ccccf58c7e8810ee42c0dc8b8b123, TIMESTAMP '2025-02-06 18:28:27'),
        (0x182da9b68dcab4b50a9705ac9fb701eee62b7b59, TIMESTAMP '2024-11-23 00:58:57'),
        (0x488922276eabe3d122d3c4f4314555a8be2c7bf9, TIMESTAMP '2025-07-15 11:11:13'),
        (0x8695330488513a6c3698a2b072ff88aaedbfac3e, TIMESTAMP '2024-12-19 20:16:05'),
        (0xd2e2356b299ddb6ed1bead94f46246ba23b48571, TIMESTAMP '2024-12-24 13:59:57'),
        (0xa12fa23701f00ad2663e339386d65cd94a144cbc, TIMESTAMP '2024-11-17 07:52:31'),
        (0xff330ce76c26b55370c9263b7f6d0297255a6dda, TIMESTAMP '2024-12-03 17:10:57')
),
incoming AS (
    SELECT
        w.wallet_address,
        tr."from" AS funder_address,
        tr.block_time,
        CAST(tr.value AS DOUBLE) / 1e18 AS amount_eth,
        ROW_NUMBER() OVER (
            PARTITION BY w.wallet_address
            ORDER BY tr.block_time, tr.tx_hash
        ) AS row_number
    FROM wallets w
    JOIN base.traces tr ON tr."to" = w.wallet_address
    WHERE tr.success = TRUE
      AND tr.value >= UINT256 '100000000000000'
      AND tr.block_time < w.first_activity_at
      AND tr.block_time >= TIMESTAMP '2023-06-01 00:00:00'
),
first_funder AS (
    SELECT * FROM incoming WHERE row_number = 1
),
funder_labels AS (
    SELECT
        f.funder_address,
        MAX(CASE WHEN l.label_type = 'identifier' THEN l.name END) AS funder_label,
        MAX(CASE WHEN l.label_type = 'identifier' THEN l.category END) AS funder_category,
        MAX(c.cex_name) AS funder_cex
    FROM first_funder f
    LEFT JOIN labels.addresses l
      ON l.blockchain = 'base'
     AND l.address = f.funder_address
    LEFT JOIN cex.addresses c
      ON c.blockchain = 'base'
     AND c.address = f.funder_address
    GROUP BY 1
)
SELECT
    CAST(f.wallet_address AS VARCHAR) AS wallet_address,
    CAST(f.funder_address AS VARCHAR) AS funder_address,
    f.block_time AS funded_at,
    f.amount_eth,
    l.funder_label,
    l.funder_category,
    l.funder_cex
FROM first_funder f
LEFT JOIN funder_labels l ON l.funder_address = f.funder_address
ORDER BY wallet_address;
