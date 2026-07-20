-- Smart Money Radar generated Dune SQL
-- Purpose: label historical Base signers and remove CEX/service entities.
-- Wallets originate from dex.trades.tx_from, so they are transaction signers.

WITH wallets(wallet_address) AS (
    VALUES
        (0x9928ea67d8a617ca3d62b9f0856c03975935a296),
        (0xdfd122610a14ac12d934898c02dbec1f72708116),
        (0x07ae8551be970cb1cca11dd7a11f47ae82e70e67),
        (0x2977b8919df6a60e93089e0f4231a28899005302),
        (0x6ffc5848c46319e7c6d48f56ca2152b213d4535f),
        (0x6333196a8590597733f6298361811b3a26bc4434),
        (0xedad7dab5920e7bf1930ca6ba33ff3585a029ea5),
        (0xf70da97812cb96acdf810712aa562db8dfa3dbef),
        (0xc366e0a2ac4e50a84a3c7f7e1f1875e7da34f8d0),
        (0x8a5221f95c8af2d249bc1a7f075b31336ee5032f),
        (0xf28ef72ff457b2ab8463103b57de50910cd02d4f),
        (0xdb61db256a30f3ef46110b8e2520aaec0db08153),
        (0x5ccaf678b74464fac20f6cae8cbaa9b402010c45),
        (0x09e5cdfeeac1866103e17e1debf4aad61c1904ef),
        (0x7a1c8dae76df823da6883fe66ec5dee425848f53),
        (0xe93685f3bba03016f02bd1828badd6195988d950),
        (0xed055568655a1be46a5d5978c507a5ea0dadddd7),
        (0x466b037ace44c0134dcebd965a4a22aed6dea027),
        (0x2b44bcb3ef096a98aa6d2bdf7d12ac76261c3c0e),
        (0x49b5fffed83583562537a1a77ca0419ab0e8f31e),
        (0x09695c5c12481311a24332b5feb0e1c0b13e02ff),
        (0xf94a2fad3ba66196b16dc55a22f11aea4b76dd87),
        (0xa2e28dedaab59d732ae375832fb855510aa7fe57),
        (0x8d47ba07ff9ccccf58c7e8810ee42c0dc8b8b123),
        (0x182da9b68dcab4b50a9705ac9fb701eee62b7b59),
        (0x488922276eabe3d122d3c4f4314555a8be2c7bf9),
        (0x8695330488513a6c3698a2b072ff88aaedbfac3e),
        (0xd2e2356b299ddb6ed1bead94f46246ba23b48571),
        (0xa12fa23701f00ad2663e339386d65cd94a144cbc),
        (0xff330ce76c26b55370c9263b7f6d0297255a6dda)
),
label_summary AS (
    SELECT
        w.wallet_address,
        MAX(CASE WHEN l.model_name = 'trader_age' THEN l.name END) AS trader_age,
        MAX(CASE WHEN l.model_name = 'average_trade_values' THEN l.name END) AS average_trade_value,
        MAX(CASE WHEN l.model_name = 'trader_frequencies' THEN l.name END) AS trader_frequency,
        MAX(CASE WHEN l.model_name = 'trader_dex_diversity' THEN l.name END) AS dex_diversity,
        MAX(CASE WHEN l.model_name = 'dex_aggregator_traders' THEN l.name END) AS aggregator_profile,
        MAX(CASE WHEN l.label_type = 'identifier' THEN l.name END) AS identity_label,
        MAX(CASE WHEN l.label_type = 'identifier' THEN l.category END) AS identity_category,
        MAX(CASE
            WHEN l.label_type = 'identifier'
             AND LOWER(COALESCE(l.category, '')) IN (
                'bridge', 'cex', 'contract', 'infrastructure', 'market maker',
                'market_maker', 'mev'
             )
            THEN l.name END
        ) AS service_label,
        MAX(CASE
            WHEN l.label_type = 'identifier'
             AND LOWER(COALESCE(l.category, '')) IN (
                'bridge', 'cex', 'contract', 'infrastructure', 'market maker',
                'market_maker', 'mev'
             )
            THEN l.category END
        ) AS service_category
    FROM wallets w
    LEFT JOIN labels.addresses l
      ON l.blockchain = 'base'
     AND l.address = w.wallet_address
    GROUP BY 1
),
cex_summary AS (
    SELECT
        w.wallet_address,
        MAX(c.cex_name) AS cex_name,
        MAX(c.distinct_name) AS distinct_name
    FROM wallets w
    LEFT JOIN cex.addresses c
      ON c.blockchain = 'base'
     AND c.address = w.wallet_address
    GROUP BY 1
)
SELECT
    CAST(w.wallet_address AS VARCHAR) AS wallet_address,
    c.cex_name,
    c.distinct_name,
    l.identity_label,
    l.identity_category,
    l.service_label,
    l.service_category,
    l.trader_age,
    l.average_trade_value,
    l.trader_frequency,
    l.dex_diversity,
    l.aggregator_profile,
    FALSE AS is_contract,
    TRUE AS signer_origin_eoa
FROM wallets w
LEFT JOIN label_summary l ON l.wallet_address = w.wallet_address
LEFT JOIN cex_summary c ON c.wallet_address = w.wallet_address
ORDER BY wallet_address;
