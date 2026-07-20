from __future__ import annotations

from pathlib import Path
from typing import Any


def render_base_pre_listing_buyers_sql(
    targets: list[dict[str, Any]],
    lookback_days: int = 90,
    minimum_total_buy_usd: float = 1000.0,
    max_wallets_per_target: int = 500,
) -> str:
    values = ",\n".join(render_target_row(target) for target in targets)
    if not values:
        values = (
            "        -- No Base-ready targets yet. Add contract mappings first.\n"
            "        ('NO_TARGETS', 0x0000000000000000000000000000000000000000, "
            "TIMESTAMP '1970-01-01 00:00:00')"
        )

    return f"""-- Smart Money Radar generated Dune SQL
-- Purpose: find Base wallets that bought future Binance listing targets before announcement.
-- Generated from local token/listing registry.

WITH targets(symbol, token_address, announced_at) AS (
    VALUES
{values}
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
       AND d.block_time >= t.announced_at - INTERVAL '{lookback_days}' day
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.amount_usd IS NOT NULL
      AND d.tx_from IS NOT NULL
      AND d.tx_from <> 0x0000000000000000000000000000000000000000
),
wallet_buys AS (
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
),
ranked_wallet_buys AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, token_address, announced_at
            ORDER BY gross_buy_usd DESC, wallet_address
        ) AS target_wallet_rank
    FROM wallet_buys
)
SELECT
    symbol,
    token_address,
    announced_at,
    wallet_address,
    first_buy_at,
    last_buy_at,
    buy_trade_count,
    gross_buy_usd
FROM ranked_wallet_buys
WHERE gross_buy_usd >= {float(minimum_total_buy_usd)}
  AND target_wallet_rank <= {int(max_wallets_per_target)}
ORDER BY announced_at DESC, gross_buy_usd DESC;
"""


def render_base_holding_behavior_sql(
    targets: list[dict[str, Any]],
    lookback_days: int = 90,
    post_window_days: int = 30,
    minimum_pre_buy_usd: float = 1000.0,
    max_wallets_per_target: int = 500,
) -> str:
    values = ",\n".join(render_target_row(target) for target in targets)
    if not values:
        values = (
            "        -- No Base-ready targets yet. Add contract mappings first.\n"
            "        ('NO_TARGETS', 0x0000000000000000000000000000000000000000, "
            "TIMESTAMP '1970-01-01 00:00:00')"
        )

    return f"""-- Smart Money Radar generated Dune SQL
-- Purpose: estimate sell-side / holding behavior for Base pre-listing buyers.
-- tx_from is used as the EVM signer. Dune taker may be a contract or router.
-- DEX trade direction and USD flow are a proxy, not exact wallet balances.

WITH targets(symbol, token_address, announced_at) AS (
    VALUES
{values}
),
pre_listing_buys AS (
    SELECT
        t.symbol,
        t.token_address,
        t.announced_at,
        d.tx_from AS wallet_address,
        SUM(d.amount_usd) AS gross_buy_usd
    FROM dex.trades d
    JOIN targets t
      ON d.blockchain = 'base'
     AND d.token_bought_address = t.token_address
     AND d.block_time < t.announced_at
     AND d.block_time >= t.announced_at - INTERVAL '{lookback_days}' DAY
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.amount_usd IS NOT NULL
      AND d.tx_from IS NOT NULL
      AND d.tx_from <> 0x0000000000000000000000000000000000000000
    GROUP BY 1, 2, 3, 4
),
ranked_pre_listing_buys AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, token_address, announced_at
            ORDER BY gross_buy_usd DESC, wallet_address
        ) AS target_wallet_rank
    FROM pre_listing_buys
),
qualified_wallets AS (
    SELECT symbol, token_address, announced_at, wallet_address
    FROM ranked_pre_listing_buys
    WHERE gross_buy_usd >= {float(minimum_pre_buy_usd)}
      AND target_wallet_rank <= {int(max_wallets_per_target)}
),
target_trades AS (
    SELECT
        t.symbol,
        t.token_address,
        t.announced_at,
        d.block_time,
        d.tx_hash,
        d.tx_from AS wallet_address,
        d.taker AS execution_address,
        d.amount_usd,
        CASE
            WHEN d.token_bought_address = t.token_address THEN 'buy'
            WHEN d.token_sold_address = t.token_address THEN 'sell'
        END AS side,
        CASE
            WHEN d.block_time < t.announced_at THEN 'pre'
            ELSE 'post'
        END AS period
    FROM dex.trades d
    JOIN targets t
        ON d.blockchain = 'base'
       AND (
            d.token_bought_address = t.token_address
            OR d.token_sold_address = t.token_address
       )
       AND d.block_time >= t.announced_at - INTERVAL '{lookback_days}' day
       AND d.block_time < t.announced_at + INTERVAL '{post_window_days}' day
    JOIN qualified_wallets q
      ON q.symbol = t.symbol
     AND q.token_address = t.token_address
     AND q.announced_at = t.announced_at
     AND q.wallet_address = d.tx_from
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.amount_usd IS NOT NULL
      AND d.tx_from IS NOT NULL
      AND d.tx_from <> 0x0000000000000000000000000000000000000000
),
wallet_flows AS (
    SELECT
        symbol,
        token_address,
        announced_at,
        wallet_address,
        SUM(CASE WHEN side = 'buy' AND period = 'pre' THEN amount_usd ELSE 0 END) AS pre_buy_usd,
        SUM(CASE WHEN side = 'sell' AND period = 'pre' THEN amount_usd ELSE 0 END) AS pre_sell_usd,
        SUM(CASE WHEN side = 'buy' AND period = 'post' THEN amount_usd ELSE 0 END) AS post_buy_usd,
        SUM(CASE WHEN side = 'sell' AND period = 'post' THEN amount_usd ELSE 0 END) AS post_sell_usd,
        COUNT(DISTINCT CASE WHEN side = 'buy' AND period = 'pre' THEN tx_hash END) AS pre_buy_trades,
        COUNT(DISTINCT CASE WHEN side = 'sell' AND period = 'pre' THEN tx_hash END) AS pre_sell_trades,
        COUNT(DISTINCT CASE WHEN side = 'buy' AND period = 'post' THEN tx_hash END) AS post_buy_trades,
        COUNT(DISTINCT CASE WHEN side = 'sell' AND period = 'post' THEN tx_hash END) AS post_sell_trades,
        MIN(CASE WHEN side = 'buy' THEN block_time END) AS first_buy_at,
        MAX(CASE WHEN side = 'buy' THEN block_time END) AS last_buy_at,
        MIN(CASE WHEN side = 'sell' THEN block_time END) AS first_sell_at,
        MAX(CASE WHEN side = 'sell' THEN block_time END) AS last_sell_at
    FROM target_trades
    GROUP BY 1, 2, 3, 4
)
SELECT
    symbol,
    token_address,
    announced_at,
    wallet_address,
    pre_buy_usd,
    pre_sell_usd,
    post_buy_usd,
    post_sell_usd,
    pre_buy_trades,
    pre_sell_trades,
    post_buy_trades,
    post_sell_trades,
    first_buy_at,
    last_buy_at,
    first_sell_at,
    last_sell_at,
    pre_buy_usd - pre_sell_usd AS net_pre_usd,
    CASE WHEN pre_buy_usd > 0 THEN pre_sell_usd / pre_buy_usd ELSE 0 END AS pre_sell_ratio,
    CASE WHEN pre_buy_usd > 0 THEN post_sell_usd / pre_buy_usd ELSE 0 END AS post_sell_ratio
FROM wallet_flows
WHERE pre_buy_usd >= {float(minimum_pre_buy_usd)}
ORDER BY announced_at DESC, pre_buy_usd DESC;
"""


def render_target_row(target: dict[str, Any]) -> str:
    symbol = sql_string(target["symbol"])
    address = target["contract_address"]
    announced_at = target["announced_at"].replace("T", " ").split("+")[0]
    return f"        ({symbol}, {address}, TIMESTAMP {sql_string(announced_at)})"


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def write_generated_sql(
    output_path: Path,
    targets: list[dict[str, Any]],
    lookback_days: int = 90,
    minimum_total_buy_usd: float = 1000.0,
    max_wallets_per_target: int = 500,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_base_pre_listing_buyers_sql(
            targets,
            lookback_days=lookback_days,
            minimum_total_buy_usd=minimum_total_buy_usd,
            max_wallets_per_target=max_wallets_per_target,
        ),
        encoding="utf-8",
    )
    return len(targets)


def render_base_pre_listing_threshold_probe_sql(
    targets: list[dict[str, Any]],
    lookback_days: int = 120,
) -> str:
    values = ",\n".join(render_target_row(target) for target in targets)
    if not values:
        values = "        ('NO_TARGETS', 0x0000000000000000000000000000000000000000, TIMESTAMP '1970-01-01 00:00:00')"
    return f"""-- Smart Money Radar generated Dune SQL
-- Purpose: choose a storage threshold before fetching historical Base wallet rows.

WITH targets(symbol, token_address, announced_at) AS (
    VALUES
{values}
),
wallet_buys AS (
    SELECT
        t.symbol,
        t.token_address,
        t.announced_at,
        d.tx_from AS wallet_address,
        SUM(d.amount_usd) AS gross_buy_usd
    FROM dex.trades d
    JOIN targets t
      ON d.blockchain = 'base'
     AND d.token_bought_address = t.token_address
     AND d.block_time < t.announced_at
     AND d.block_time >= t.announced_at - INTERVAL '{int(lookback_days)}' DAY
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.amount_usd IS NOT NULL
      AND d.tx_from IS NOT NULL
      AND d.tx_from <> 0x0000000000000000000000000000000000000000
    GROUP BY 1, 2, 3, 4
)
SELECT
    COUNT(*) AS wallet_target_rows,
    COUNT(*) FILTER (WHERE gross_buy_usd >= 100) AS at_100_usd,
    COUNT(*) FILTER (WHERE gross_buy_usd >= 500) AS at_500_usd,
    COUNT(*) FILTER (WHERE gross_buy_usd >= 1000) AS at_1000_usd,
    COUNT(*) FILTER (WHERE gross_buy_usd >= 2500) AS at_2500_usd,
    COUNT(*) FILTER (WHERE gross_buy_usd >= 5000) AS at_5000_usd,
    COUNT(*) FILTER (WHERE gross_buy_usd >= 10000) AS at_10000_usd,
    COUNT(DISTINCT CASE WHEN gross_buy_usd >= 5000 THEN symbol END) AS targets_at_5000_usd
FROM wallet_buys;
"""


def write_generated_pre_listing_threshold_probe_sql(
    output_path: Path,
    targets: list[dict[str, Any]],
    lookback_days: int = 120,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_base_pre_listing_threshold_probe_sql(
            targets,
            lookback_days=lookback_days,
        ),
        encoding="utf-8",
    )
    return len(targets)


def write_generated_holding_sql(
    output_path: Path,
    targets: list[dict[str, Any]],
    lookback_days: int = 90,
    post_window_days: int = 30,
    minimum_pre_buy_usd: float = 1000.0,
    max_wallets_per_target: int = 500,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_base_holding_behavior_sql(
            targets,
            lookback_days=lookback_days,
            post_window_days=post_window_days,
            minimum_pre_buy_usd=minimum_pre_buy_usd,
            max_wallets_per_target=max_wallets_per_target,
        ),
        encoding="utf-8",
    )
    return len(targets)


def render_base_contract_discovery_sql(
    targets: list[dict[str, Any]],
    lookback_days: int = 120,
) -> str:
    values = ",\n".join(
        "        ("
        f"{sql_string(target['symbol'])}, "
        f"TIMESTAMP {sql_string(target['announced_at'].replace('T', ' ').split('+')[0])}"
        ")"
        for target in targets
    )
    if not values:
        values = "        ('NO_TARGETS', TIMESTAMP '1970-01-01 00:00:00')"
    return f"""-- Smart Money Radar generated Dune SQL
-- Purpose: discover Base contracts from DEX activity strictly before each Binance cutoff.
-- This query is an identity-mapping aid, not a trading feature or a post-listing lookup.

WITH targets(symbol, announced_at) AS (
    VALUES
{values}
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
     AND d.block_time >= t.announced_at - INTERVAL '{int(lookback_days)}' DAY
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
     AND d.block_time >= t.announced_at - INTERVAL '{int(lookback_days)}' DAY
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
"""


def write_generated_contract_discovery_sql(
    output_path: Path,
    targets: list[dict[str, Any]],
    lookback_days: int = 120,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_base_contract_discovery_sql(targets, lookback_days=lookback_days),
        encoding="utf-8",
    )
    return len(targets)


def render_base_live_radar_sql(
    wallets: list[dict[str, Any]],
    excluded_token_addresses: list[str] | None = None,
    window_hours: int = 336,
    min_trade_usd: float = 25.0,
) -> str:
    wallet_values = ",\n".join(render_wallet_row(wallet) for wallet in wallets)
    if not wallet_values:
        wallet_values = (
            "        (0x0000000000000000000000000000000000000000, "
            "'ignore', 0.0, 0.0)"
        )
    exclusions = [address.lower() for address in excluded_token_addresses or []]
    exclusion_sql = ""
    if exclusions:
        exclusion_sql = (
            "\n      AND token_address NOT IN ("
            + ", ".join(exclusions)
            + ")"
        )

    return f"""-- Smart Money Radar generated Dune SQL
-- Purpose: near-live Base token accumulation by qualified historical wallets.
-- Uses tx_from as the EVM signer and excludes known training target contracts.

WITH tracked_wallets(wallet_address, wallet_label, interest_score, confidence_score) AS (
    VALUES
{wallet_values}
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
      AND d.block_time >= CURRENT_TIMESTAMP - INTERVAL '{int(window_hours)}' HOUR
      AND d.block_month >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '30' DAY)
      AND d.amount_usd >= {float(min_trade_usd)}
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
      AND d.block_time >= CURRENT_TIMESTAMP - INTERVAL '{int(window_hours)}' HOUR
      AND d.block_month >= DATE_TRUNC('month', CURRENT_DATE - INTERVAL '30' DAY)
      AND d.amount_usd >= {float(min_trade_usd)}
      AND d.token_sold_address IS NOT NULL
      AND UPPER(COALESCE(d.token_sold_symbol, '')) NOT IN (
          'USDC', 'USDT', 'DAI', 'USDBC', 'WETH', 'ETH', 'CBETH', 'EURC', 'CBBTC'
      )
),
filtered_trades AS (
    SELECT *
    FROM wallet_trades
    WHERE token_address <> 0x0000000000000000000000000000000000000000{exclusion_sql}
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
    {int(window_hours)} AS window_hours,
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
"""


def write_generated_live_radar_sql(
    output_path: Path,
    wallets: list[dict[str, Any]],
    excluded_token_addresses: list[str] | None = None,
    window_hours: int = 336,
    min_trade_usd: float = 25.0,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_base_live_radar_sql(
            wallets=wallets,
            excluded_token_addresses=excluded_token_addresses,
            window_hours=window_hours,
            min_trade_usd=min_trade_usd,
        ),
        encoding="utf-8",
    )
    return len(wallets)


def render_wallet_row(wallet: dict[str, Any]) -> str:
    return (
        "        ("
        f"{wallet['wallet_address'].lower()}, "
        f"{sql_string(wallet['label'])}, "
        f"{float(wallet['interest_score'])}, "
        f"{float(wallet['confidence_score'])}"
        ")"
    )


def render_base_wallet_entities_sql(wallets: list[dict[str, Any]]) -> str:
    values = ",\n".join(
        f"        ({row['wallet_address'].lower()})" for row in wallets
    )
    if not values:
        values = "        (0x0000000000000000000000000000000000000000)"
    return f"""-- Smart Money Radar generated Dune SQL
-- Purpose: label historical Base signers and remove CEX/service entities.
-- Wallets originate from dex.trades.tx_from, so they are transaction signers.

WITH wallets(wallet_address) AS (
    VALUES
{values}
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
"""


def write_generated_wallet_entities_sql(
    output_path: Path,
    wallets: list[dict[str, Any]],
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_base_wallet_entities_sql(wallets), encoding="utf-8")
    return len(wallets)


def render_base_wallet_funding_sql(wallets: list[dict[str, Any]]) -> str:
    values = ",\n".join(
        "        ("
        f"{row['wallet_address'].lower()}, "
        f"TIMESTAMP {sql_string(row['earliest_buy_at'].replace('T', ' ').split('+')[0])}"
        ")"
        for row in wallets
    )
    if not values:
        values = (
            "        (0x0000000000000000000000000000000000000000, "
            "TIMESTAMP '1970-01-01 00:00:00')"
        )
    return f"""-- Smart Money Radar generated Dune SQL
-- Purpose: find the first meaningful native-ETH funder for Base wallet clustering.

WITH wallets(wallet_address, first_activity_at) AS (
    VALUES
{values}
),
incoming AS (
    SELECT
        w.wallet_address,
        tr.\"from\" AS funder_address,
        tr.block_time,
        CAST(tr.value AS DOUBLE) / 1e18 AS amount_eth,
        ROW_NUMBER() OVER (
            PARTITION BY w.wallet_address
            ORDER BY tr.block_time, tr.tx_hash
        ) AS row_number
    FROM wallets w
    JOIN base.traces tr ON tr.\"to\" = w.wallet_address
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
"""


def render_base_identity_graph_sql(wallets: list[dict[str, Any]]) -> str:
    values = ",\n".join(
        "        ("
        f"{row['wallet_address'].lower()}, "
        f"TIMESTAMP {sql_string(row['earliest_buy_at'].replace('T', ' ').split('+')[0])}"
        ")"
        for row in wallets
        if row.get("earliest_buy_at")
    )
    if not values:
        values = (
            "        (0x0000000000000000000000000000000000000000, "
            "TIMESTAMP '1970-01-01 00:00:00')"
        )
    return f"""-- Smart Money Radar identity graph V2
-- Two-hop native funding provenance with CEX/service labels.

WITH wallets(wallet_address, first_activity_at) AS (
    VALUES
{values}
),
hop1_candidates AS (
    SELECT
        w.wallet_address,
        tr.\"from\" AS hop1_address,
        tr.block_time AS hop1_funded_at,
        CAST(tr.value AS DOUBLE) / 1e18 AS hop1_amount_eth,
        ROW_NUMBER() OVER (
            PARTITION BY w.wallet_address
            ORDER BY tr.block_time, tr.tx_hash
        ) AS row_number
    FROM wallets w
    JOIN base.traces tr ON tr.\"to\" = w.wallet_address
    WHERE tr.success = TRUE
      AND tr.value >= UINT256 '100000000000000'
      AND tr.block_time < w.first_activity_at
      AND tr.block_time >= TIMESTAMP '2023-06-01 00:00:00'
),
hop1 AS (
    SELECT * FROM hop1_candidates WHERE row_number = 1
),
hop2_candidates AS (
    SELECT
        h.wallet_address,
        h.hop1_address,
        h.hop1_funded_at,
        h.hop1_amount_eth,
        tr.\"from\" AS hop2_address,
        tr.block_time AS hop2_funded_at,
        CAST(tr.value AS DOUBLE) / 1e18 AS hop2_amount_eth,
        ROW_NUMBER() OVER (
            PARTITION BY h.wallet_address
            ORDER BY tr.block_time, tr.tx_hash
        ) AS row_number
    FROM hop1 h
    JOIN base.traces tr ON tr.\"to\" = h.hop1_address
    WHERE tr.success = TRUE
      AND tr.value >= UINT256 '100000000000000'
      AND tr.block_time < h.hop1_funded_at
      AND tr.block_time >= TIMESTAMP '2023-06-01 00:00:00'
),
hop2 AS (
    SELECT * FROM hop2_candidates WHERE row_number = 1
),
hop3_candidates AS (
    SELECT
        h.wallet_address,
        h.hop1_address,
        h.hop1_funded_at,
        h.hop1_amount_eth,
        h.hop2_address,
        h.hop2_funded_at,
        h.hop2_amount_eth,
        tr."from" AS hop3_address,
        tr.block_time AS hop3_funded_at,
        CAST(tr.value AS DOUBLE) / 1e18 AS hop3_amount_eth,
        ROW_NUMBER() OVER (
            PARTITION BY h.wallet_address
            ORDER BY tr.block_time, tr.tx_hash
        ) AS row_number
    FROM hop2 h
    JOIN base.traces tr ON tr."to" = h.hop2_address
    WHERE tr.success = TRUE
      AND tr.value >= UINT256 '100000000000000'
      AND tr.block_time < h.hop2_funded_at
      AND tr.block_time >= TIMESTAMP '2023-06-01 00:00:00'
),
hop3 AS (
    SELECT * FROM hop3_candidates WHERE row_number = 1
),
addresses AS (
    SELECT hop1_address AS address FROM hop1
    UNION
    SELECT hop2_address AS address FROM hop2 WHERE hop2_address IS NOT NULL
    UNION
    SELECT hop3_address AS address FROM hop3 WHERE hop3_address IS NOT NULL
),
labels AS (
    SELECT
        a.address,
        MAX(CASE WHEN l.label_type = 'identifier' THEN l.name END) AS identity_label,
        MAX(CASE WHEN l.label_type = 'identifier' THEN l.category END) AS identity_category,
        MAX(c.cex_name) AS cex_name
    FROM addresses a
    LEFT JOIN labels.addresses l
      ON l.blockchain = 'base'
     AND l.address = a.address
    LEFT JOIN cex.addresses c
      ON c.blockchain = 'base'
     AND c.address = a.address
    GROUP BY 1
)
SELECT
    CAST(w.wallet_address AS VARCHAR) AS wallet_address,
    CAST(h1.hop1_address AS VARCHAR) AS hop1_address,
    h1.hop1_funded_at,
    h1.hop1_amount_eth,
    l1.identity_label AS hop1_label,
    l1.identity_category AS hop1_category,
    l1.cex_name AS hop1_cex,
    CAST(h2.hop2_address AS VARCHAR) AS hop2_address,
    h2.hop2_funded_at,
    h2.hop2_amount_eth,
    l2.identity_label AS hop2_label,
    l2.identity_category AS hop2_category,
    l2.cex_name AS hop2_cex,
    CAST(h3.hop3_address AS VARCHAR) AS hop3_address,
    h3.hop3_funded_at,
    h3.hop3_amount_eth,
    l3.identity_label AS hop3_label,
    l3.identity_category AS hop3_category,
    l3.cex_name AS hop3_cex
FROM wallets w
LEFT JOIN hop1 h1 ON h1.wallet_address = w.wallet_address
LEFT JOIN hop2 h2 ON h2.wallet_address = w.wallet_address
LEFT JOIN hop3 h3 ON h3.wallet_address = w.wallet_address
LEFT JOIN labels l1 ON l1.address = h1.hop1_address
LEFT JOIN labels l2 ON l2.address = h2.hop2_address
LEFT JOIN labels l3 ON l3.address = h3.hop3_address
ORDER BY wallet_address;
"""


def write_generated_wallet_funding_sql(
    output_path: Path,
    wallets: list[dict[str, Any]],
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_base_wallet_funding_sql(wallets), encoding="utf-8")
    return len(wallets)


def write_base_identity_graph_sql(
    output_path: Path,
    wallets: list[dict[str, Any]],
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_base_identity_graph_sql(wallets), encoding="utf-8")
    return len(wallets)


def render_base_shared_routes_sql(wallets: list[dict[str, Any]]) -> str:
    values = ",\n".join(
        f"        ({row['wallet_address'].lower()})" for row in wallets
    )
    if not values:
        values = "        (0x0000000000000000000000000000000000000000)"
    return f"""-- Smart Money Radar identity graph V2
-- Repeated rare execution routes across several pools/tokens.

WITH wallets(wallet_address) AS (
    VALUES
{values}
),
wallet_routes AS (
    SELECT
        d.tx_from AS wallet_address,
        d.project,
        d.project_contract_address AS route_address,
        COUNT(DISTINCT d.tx_hash) AS route_trade_count,
        MIN(d.block_time) AS first_route_at,
        MAX(d.block_time) AS last_route_at
    FROM dex.trades d
    JOIN wallets w ON w.wallet_address = d.tx_from
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2023-08-01'
      AND d.block_time < CURRENT_TIMESTAMP
      AND d.amount_usd >= 100
      AND d.project_contract_address IS NOT NULL
    GROUP BY 1, 2, 3
    HAVING COUNT(DISTINCT d.tx_hash) >= 2
),
route_population AS (
    SELECT
        project,
        route_address,
        COUNT(DISTINCT wallet_address) AS route_wallet_count
    FROM wallet_routes
    GROUP BY 1, 2
),
rare_routes AS (
    SELECT wr.*
    FROM wallet_routes wr
    JOIN route_population rp
      ON rp.project = wr.project
     AND rp.route_address = wr.route_address
    WHERE rp.route_wallet_count BETWEEN 2 AND 12
),
wallet_pairs AS (
    SELECT
        a.wallet_address AS wallet_address_a,
        b.wallet_address AS wallet_address_b,
        COUNT(DISTINCT a.route_address) AS shared_route_count,
        SUM(LEAST(a.route_trade_count, b.route_trade_count)) AS shared_route_trades,
        MIN(LEAST(a.first_route_at, b.first_route_at)) AS first_observed_at,
        MAX(GREATEST(a.last_route_at, b.last_route_at)) AS last_observed_at,
        ARRAY_JOIN(
            SLICE(
                ARRAY_AGG(DISTINCT CONCAT(
                    a.project, ':', CAST(a.route_address AS VARCHAR)
                )),
                1,
                10
            ),
            ','
        ) AS route_examples
    FROM rare_routes a
    JOIN rare_routes b
      ON b.project = a.project
     AND b.route_address = a.route_address
     AND b.wallet_address > a.wallet_address
    GROUP BY 1, 2
)
SELECT
    CAST(wallet_address_a AS VARCHAR) AS wallet_address_a,
    CAST(wallet_address_b AS VARCHAR) AS wallet_address_b,
    shared_route_count,
    shared_route_trades,
    first_observed_at,
    last_observed_at,
    route_examples
FROM wallet_pairs
WHERE shared_route_count >= 3
  AND shared_route_trades >= 6
ORDER BY shared_route_count DESC, shared_route_trades DESC;
"""


def write_base_shared_routes_sql(
    output_path: Path,
    wallets: list[dict[str, Any]],
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_base_shared_routes_sql(wallets),
        encoding="utf-8",
    )
    return len(wallets)


def render_base_negative_control_sql(
    evaluations: list[dict[str, Any]],
    window_days: int = 14,
    min_trade_usd: float = 25.0,
) -> str:
    cohort_rows = []
    seen = set()
    for evaluation in evaluations:
        if evaluation.get("chain_id") != "base":
            continue
        for wallet_address in evaluation.get("evidence", {}).get("predicted_wallets", []):
            key = (
                evaluation["snapshot_at"],
                evaluation["symbol"],
                evaluation["token_address"].lower(),
                wallet_address.lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            snapshot = key[0].replace("T", " ").split("+")[0]
            cohort_rows.append(
                "        ("
                f"TIMESTAMP {sql_string(snapshot)}, "
                f"{sql_string(key[1])}, {key[2]}, {key[3]}"
                ")"
            )
    values = ",\n".join(cohort_rows)
    if not values:
        values = (
            "        (TIMESTAMP '1970-01-01 00:00:00', 'NO_COHORT', "
            "0x0000000000000000000000000000000000000000, "
            "0x0000000000000000000000000000000000000000)"
        )

    return f"""-- Smart Money Radar generated Dune SQL
-- Purpose: point-in-time negative controls for wallet-driven Base token discovery.
-- Wallet cohorts are built only from targets mature before each snapshot.

WITH cohorts(snapshot_at, positive_symbol, positive_token_address, wallet_address) AS (
    VALUES
{values}
),
cohort_sizes AS (
    SELECT
        snapshot_at,
        positive_symbol,
        positive_token_address,
        COUNT(DISTINCT wallet_address) AS cohort_wallet_count
    FROM cohorts
    WHERE positive_symbol <> 'NO_COHORT'
    GROUP BY 1, 2, 3
),
wallet_trades AS (
    SELECT
        c.snapshot_at,
        c.positive_symbol,
        c.positive_token_address,
        d.block_time,
        d.tx_hash,
        d.tx_from AS wallet_address,
        d.token_bought_address AS token_address,
        d.token_bought_symbol AS token_symbol,
        d.amount_usd,
        'buy' AS side
    FROM dex.trades d
    JOIN cohorts c
      ON d.tx_from = c.wallet_address
     AND d.block_time < c.snapshot_at
     AND d.block_time >= c.snapshot_at - INTERVAL '{int(window_days)}' DAY
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.amount_usd >= {float(min_trade_usd)}
      AND d.token_bought_address IS NOT NULL
      AND UPPER(COALESCE(d.token_bought_symbol, '')) NOT IN (
          'USDC', 'USDT', 'DAI', 'USDBC', 'WETH', 'ETH', 'CBETH', 'EURC', 'CBBTC'
      )

    UNION ALL

    SELECT
        c.snapshot_at,
        c.positive_symbol,
        c.positive_token_address,
        d.block_time,
        d.tx_hash,
        d.tx_from AS wallet_address,
        d.token_sold_address AS token_address,
        d.token_sold_symbol AS token_symbol,
        d.amount_usd,
        'sell' AS side
    FROM dex.trades d
    JOIN cohorts c
      ON d.tx_from = c.wallet_address
     AND d.block_time < c.snapshot_at
     AND d.block_time >= c.snapshot_at - INTERVAL '{int(window_days)}' DAY
    WHERE d.blockchain = 'base'
      AND d.block_month >= DATE '2024-01-01'
      AND d.amount_usd >= {float(min_trade_usd)}
      AND d.token_sold_address IS NOT NULL
      AND UPPER(COALESCE(d.token_sold_symbol, '')) NOT IN (
          'USDC', 'USDT', 'DAI', 'USDBC', 'WETH', 'ETH', 'CBETH', 'EURC', 'CBBTC'
      )
)
SELECT
    wt.snapshot_at,
    wt.positive_symbol,
    wt.positive_token_address,
    wt.token_address,
    MAX(wt.token_symbol) AS token_symbol,
    CASE WHEN wt.token_address = wt.positive_token_address THEN 1 ELSE 0 END AS is_positive,
    cs.cohort_wallet_count,
    COUNT(DISTINCT wt.wallet_address) AS tracked_wallet_count,
    SUM(CASE WHEN wt.side = 'buy' THEN wt.amount_usd ELSE 0 END) AS gross_buy_usd,
    SUM(CASE WHEN wt.side = 'sell' THEN wt.amount_usd ELSE 0 END) AS gross_sell_usd,
    SUM(CASE WHEN wt.side = 'buy' THEN wt.amount_usd ELSE -wt.amount_usd END) AS net_buy_usd,
    COUNT(DISTINCT CASE WHEN wt.side = 'buy' THEN wt.tx_hash END) AS buy_trade_count,
    COUNT(DISTINCT CASE WHEN wt.side = 'sell' THEN wt.tx_hash END) AS sell_trade_count,
    MIN(wt.block_time) AS first_trade_at,
    MAX(wt.block_time) AS last_trade_at
FROM wallet_trades wt
JOIN cohort_sizes cs
  ON cs.snapshot_at = wt.snapshot_at
 AND cs.positive_symbol = wt.positive_symbol
 AND cs.positive_token_address = wt.positive_token_address
WHERE wt.token_address <> 0x0000000000000000000000000000000000000000
GROUP BY 1, 2, 3, 4, 6, 7
HAVING SUM(CASE WHEN wt.side = 'buy' THEN wt.amount_usd ELSE 0 END) >= 100
ORDER BY snapshot_at, net_buy_usd DESC;
"""


def write_generated_negative_control_sql(
    output_path: Path,
    evaluations: list[dict[str, Any]],
    window_days: int = 14,
    min_trade_usd: float = 25.0,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_base_negative_control_sql(
            evaluations=evaluations,
            window_days=window_days,
            min_trade_usd=min_trade_usd,
        ),
        encoding="utf-8",
    )
    return sum(
        len(row.get("evidence", {}).get("predicted_wallets", []))
        for row in evaluations
        if row.get("chain_id") == "base"
    )


DUNE_EVM_BLOCKCHAINS = {
    "base": ("base", "2023-08-01"),
    "bsc": ("bnb", "2020-08-01"),
    "ethereum": ("ethereum", "2018-01-01"),
}

EXCLUDED_RESEARCH_SYMBOLS = (
    "USDC", "USDT", "DAI", "USDBC", "WETH", "ETH", "CBETH", "EURC",
    "CBBTC", "WBTC", "BTCB", "WBNB", "BNB", "FDUSD", "TUSD", "USDE",
)


def render_weekly_evm_universe_sql(
    chain_id: str,
    start_date: str | None = None,
    min_trade_usd: float = 5.0,
    min_weekly_volume_usd: float = 200_000.0,
    min_weekly_trades: int = 20,
    min_weekly_traders: int = 10,
) -> str:
    dune_chain, default_start = dune_evm_chain(chain_id)
    start = start_date or default_start
    excluded = ", ".join(sql_string(symbol) for symbol in EXCLUDED_RESEARCH_SYMBOLS)
    return f"""-- Smart Money Radar Research Validity V4
-- Weekly point-in-time universe for {chain_id}; one row is one token-week.
-- Thirteen explicit follow-up weeks cover 30/60/90-day outcomes without inferring zeros.

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
    WHERE d.blockchain = {sql_string(dune_chain)}
      AND d.block_month >= DATE {sql_string(start)}
      AND d.block_time >= TIMESTAMP {sql_string(start + ' 00:00:00')}
      AND d.block_time < DATE_TRUNC('week', CURRENT_TIMESTAMP)
      AND d.tx_from IS NOT NULL
      AND d.token_bought_address IS NOT NULL
      AND d.amount_usd >= {float(min_trade_usd)}
      AND CAST(d.token_bought_amount AS DOUBLE) > 0
      AND UPPER(COALESCE(d.token_bought_symbol, '')) NOT IN ({excluded})

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
    WHERE d.blockchain = {sql_string(dune_chain)}
      AND d.block_month >= DATE {sql_string(start)}
      AND d.block_time >= TIMESTAMP {sql_string(start + ' 00:00:00')}
      AND d.block_time < DATE_TRUNC('week', CURRENT_TIMESTAMP)
      AND d.tx_from IS NOT NULL
      AND d.token_sold_address IS NOT NULL
      AND d.amount_usd >= {float(min_trade_usd)}
      AND CAST(d.token_sold_amount AS DOUBLE) > 0
      AND UPPER(COALESCE(d.token_sold_symbol, '')) NOT IN ({excluded})
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
    WHERE volume_usd >= {float(min_weekly_volume_usd)}
      AND trade_count >= {int(min_weekly_trades)}
      AND trader_count >= {int(min_weekly_traders)}
),
follow_up_grid AS (
    SELECT
        seed.token_address,
        DATE_ADD('week', offset_week, seed.week_start) AS week_start,
        MAX_BY(seed.token_symbol, seed.week_start) AS seed_token_symbol
    FROM eligible_weekly seed
    CROSS JOIN UNNEST(SEQUENCE(0, 13)) AS offsets(offset_week)
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
"""


def write_weekly_evm_universe_sql(
    output_path: Path,
    chain_id: str,
    **kwargs: Any,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_weekly_evm_universe_sql(chain_id=chain_id, **kwargs),
        encoding="utf-8",
    )


def render_weekly_evm_universe_probe_sql(
    chain_id: str,
    start_date: str | None = None,
) -> str:
    sql = render_weekly_evm_universe_sql(
        chain_id=chain_id,
        start_date=start_date,
    )
    marker = "\nSELECT\n    CAST(token_address AS VARCHAR) AS token_address,"
    prefix, separator, _ = sql.partition(marker)
    if not separator:
        raise ValueError("Weekly universe SQL marker not found")
    return prefix + """
SELECT
    COUNT(*) FILTER (
        WHERE volume_usd >= 1000 AND trade_count >= 3
    ) AS snapshots_at_1k_3,
    COUNT(*) FILTER (
        WHERE volume_usd >= 5000 AND trade_count >= 10 AND trader_count >= 5
    ) AS snapshots_at_5k_10,
    COUNT(*) FILTER (
        WHERE volume_usd >= 10000 AND trade_count >= 10 AND trader_count >= 5
    ) AS snapshots_at_10k_10,
    COUNT(*) FILTER (
        WHERE volume_usd >= 25000 AND trade_count >= 10 AND trader_count >= 5
    ) AS snapshots_at_25k_10,
    COUNT(*) FILTER (
        WHERE volume_usd >= 50000 AND trade_count >= 20 AND trader_count >= 10
    ) AS snapshots_at_50k_20,
    COUNT(*) FILTER (
        WHERE volume_usd >= 100000 AND trade_count >= 20 AND trader_count >= 10
    ) AS snapshots_at_100k_20,
    COUNT(DISTINCT CASE
        WHEN volume_usd >= 25000 AND trade_count >= 10 AND trader_count >= 5
        THEN token_address END
    ) AS tokens_at_25k_10,
    MIN(week_start) AS first_week,
    MAX(week_start) AS last_week
FROM weekly;
"""


def write_weekly_evm_universe_probe_sql(
    output_path: Path,
    chain_id: str,
    start_date: str | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_weekly_evm_universe_probe_sql(chain_id, start_date=start_date),
        encoding="utf-8",
    )


def render_wallet_opportunity_sql(
    chain_id: str,
    wallets: list[dict[str, Any]],
    start_date: str | None = None,
    min_trade_usd: float = 10.0,
    min_token_buy_usd: float = 100.0,
) -> str:
    dune_chain, default_start = dune_evm_chain(chain_id)
    start = start_date or default_start
    values = ",\n".join(
        f"        ({row['wallet_address'].lower()})" for row in wallets
    )
    if not values:
        values = "        (0x0000000000000000000000000000000000000000)"
    excluded = ", ".join(sql_string(symbol) for symbol in EXCLUDED_RESEARCH_SYMBOLS)
    return f"""-- Smart Money Radar Research Validity V2
-- Complete DEX opportunity denominator for selected {chain_id} wallets.

WITH wallets(wallet_address) AS (
    VALUES
{values}
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
    WHERE d.blockchain = {sql_string(dune_chain)}
      AND d.block_month >= DATE {sql_string(start)}
      AND d.block_time >= TIMESTAMP {sql_string(start + ' 00:00:00')}
      AND d.block_time < CURRENT_TIMESTAMP
      AND d.amount_usd >= {float(min_trade_usd)}
      AND d.token_bought_address IS NOT NULL
      AND UPPER(COALESCE(d.token_bought_symbol, '')) NOT IN ({excluded})

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
    WHERE d.blockchain = {sql_string(dune_chain)}
      AND d.block_month >= DATE {sql_string(start)}
      AND d.block_time >= TIMESTAMP {sql_string(start + ' 00:00:00')}
      AND d.block_time < CURRENT_TIMESTAMP
      AND d.amount_usd >= {float(min_trade_usd)}
      AND d.token_sold_address IS NOT NULL
      AND UPPER(COALESCE(d.token_sold_symbol, '')) NOT IN ({excluded})
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
  AND p.gross_buy_usd >= {float(min_token_buy_usd)}
ORDER BY wallet_address, first_buy_at, gross_buy_usd DESC;
"""


def write_wallet_opportunity_sql(
    output_path: Path,
    chain_id: str,
    wallets: list[dict[str, Any]],
    **kwargs: Any,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_wallet_opportunity_sql(
            chain_id=chain_id,
            wallets=wallets,
            **kwargs,
        ),
        encoding="utf-8",
    )
    return len(wallets)


def render_wallet_weekly_flows_sql(
    chain_id: str,
    wallets: list[dict[str, Any]],
    start_date: str | None = None,
    min_trade_usd: float = 10.0,
) -> str:
    dune_chain, default_start = dune_evm_chain(chain_id)
    start = start_date or default_start
    values = ",\n".join(
        f"        ({row['wallet_address'].lower()})" for row in wallets
    )
    if not values:
        values = "        (0x0000000000000000000000000000000000000000)"
    excluded = ", ".join(sql_string(symbol) for symbol in EXCLUDED_RESEARCH_SYMBOLS)
    return f"""-- Smart Money Radar point-in-time wallet-token weekly flows
WITH wallets(wallet_address) AS (
    VALUES
{values}
),
flows AS (
    SELECT
        d.tx_from AS wallet_address,
        d.token_bought_address AS token_address,
        d.token_bought_symbol AS token_symbol,
        d.block_time,
        d.tx_hash,
        d.amount_usd,
        'buy' AS side
    FROM dex.trades d
    JOIN wallets w ON w.wallet_address = d.tx_from
    WHERE d.blockchain = {sql_string(dune_chain)}
      AND d.block_month >= DATE {sql_string(start)}
      AND d.block_time >= TIMESTAMP {sql_string(start + ' 00:00:00')}
      AND d.block_time < DATE_TRUNC('week', CURRENT_TIMESTAMP)
      AND d.amount_usd >= {float(min_trade_usd)}
      AND d.token_bought_address IS NOT NULL
      AND UPPER(COALESCE(d.token_bought_symbol, '')) NOT IN ({excluded})

    UNION ALL

    SELECT
        d.tx_from AS wallet_address,
        d.token_sold_address AS token_address,
        d.token_sold_symbol AS token_symbol,
        d.block_time,
        d.tx_hash,
        d.amount_usd,
        'sell' AS side
    FROM dex.trades d
    JOIN wallets w ON w.wallet_address = d.tx_from
    WHERE d.blockchain = {sql_string(dune_chain)}
      AND d.block_month >= DATE {sql_string(start)}
      AND d.block_time >= TIMESTAMP {sql_string(start + ' 00:00:00')}
      AND d.block_time < DATE_TRUNC('week', CURRENT_TIMESTAMP)
      AND d.amount_usd >= {float(min_trade_usd)}
      AND d.token_sold_address IS NOT NULL
      AND UPPER(COALESCE(d.token_sold_symbol, '')) NOT IN ({excluded})
)
SELECT
    CAST(wallet_address AS VARCHAR) AS wallet_address,
    CAST(token_address AS VARCHAR) AS token_address,
    MAX_BY(token_symbol, block_time) AS token_symbol,
    DATE_ADD('day', 7, DATE_TRUNC('week', block_time)) AS snapshot_at,
    SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE 0 END) AS gross_buy_usd,
    SUM(CASE WHEN side = 'sell' THEN amount_usd ELSE 0 END) AS gross_sell_usd,
    SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE -amount_usd END) AS net_buy_usd,
    COUNT(DISTINCT CASE WHEN side = 'buy' THEN tx_hash END) AS buy_trade_count,
    COUNT(DISTINCT CASE WHEN side = 'sell' THEN tx_hash END) AS sell_trade_count,
    MIN(block_time) AS first_trade_at,
    MAX(block_time) AS last_trade_at
FROM flows
WHERE token_address <> 0x0000000000000000000000000000000000000000
GROUP BY 1, 2, 4
HAVING SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE 0 END) >= 25
    OR SUM(CASE WHEN side = 'sell' THEN amount_usd ELSE 0 END) >= 25
ORDER BY snapshot_at, wallet_address, net_buy_usd DESC;
"""


def write_wallet_weekly_flows_sql(
    output_path: Path,
    chain_id: str,
    wallets: list[dict[str, Any]],
    **kwargs: Any,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_wallet_weekly_flows_sql(
            chain_id=chain_id,
            wallets=wallets,
            **kwargs,
        ),
        encoding="utf-8",
    )
    return len(wallets)


def render_evm_pre_listing_buyers_sql(
    chain_id: str,
    targets: list[dict[str, Any]],
    lookback_days: int = 180,
    minimum_total_buy_usd: float = 500.0,
    max_wallets_per_target: int = 2_000,
) -> str:
    dune_chain, start_date = dune_evm_chain(chain_id)
    values = ",\n".join(render_target_row(target) for target in targets)
    if not values:
        values = (
            "        ('NO_TARGETS', 0x0000000000000000000000000000000000000000, "
            "TIMESTAMP '1970-01-01 00:00:00')"
        )
    return f"""-- Smart Money Radar cross-chain Binance history: {chain_id}
WITH targets(symbol, token_address, announced_at) AS (
    VALUES
{values}
),
wallet_buys AS (
    SELECT
        t.symbol,
        t.token_address,
        t.announced_at,
        d.tx_from AS wallet_address,
        MIN(d.block_time) AS first_buy_at,
        MAX(d.block_time) AS last_buy_at,
        COUNT(DISTINCT d.tx_hash) AS buy_trade_count,
        SUM(d.amount_usd) AS gross_buy_usd
    FROM dex.trades d
    JOIN targets t
      ON d.token_bought_address = t.token_address
     AND d.block_time < t.announced_at
     AND d.block_time >= t.announced_at - INTERVAL '{int(lookback_days)}' DAY
    WHERE d.blockchain = {sql_string(dune_chain)}
      AND d.block_month >= DATE {sql_string(start_date)}
      AND d.tx_from IS NOT NULL
      AND d.amount_usd IS NOT NULL
    GROUP BY 1, 2, 3, 4
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, token_address, announced_at
            ORDER BY gross_buy_usd DESC, wallet_address
        ) AS wallet_rank
    FROM wallet_buys
)
SELECT
    symbol,
    CAST(token_address AS VARCHAR) AS token_address,
    announced_at,
    CAST(wallet_address AS VARCHAR) AS wallet_address,
    first_buy_at,
    last_buy_at,
    buy_trade_count,
    gross_buy_usd
FROM ranked
WHERE gross_buy_usd >= {float(minimum_total_buy_usd)}
  AND wallet_rank <= {int(max_wallets_per_target)}
ORDER BY announced_at, gross_buy_usd DESC;
"""


def write_evm_pre_listing_buyers_sql(
    output_path: Path,
    chain_id: str,
    targets: list[dict[str, Any]],
    **kwargs: Any,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_evm_pre_listing_buyers_sql(
            chain_id=chain_id,
            targets=targets,
            **kwargs,
        ),
        encoding="utf-8",
    )
    return len(targets)


def render_solana_pre_listing_buyers_sql(
    targets: list[dict[str, Any]],
    lookback_days: int = 365,
    minimum_total_buy_usd: float = 500.0,
    max_wallets_per_target: int = 2_000,
) -> str:
    values = ",\n".join(render_solana_target_row(target) for target in targets)
    if not values:
        values = "        ('NO_TARGETS', 'NO_MINT', TIMESTAMP '1970-01-01 00:00:00')"
    return f"""-- Smart Money Radar Solana historical Binance buyers
WITH targets(symbol, token_address, announced_at) AS (
    VALUES
{values}
),
wallet_buys AS (
    SELECT
        t.symbol,
        t.token_address,
        t.announced_at,
        d.trader_id AS wallet_address,
        MIN(d.block_time) AS first_buy_at,
        MAX(d.block_time) AS last_buy_at,
        COUNT(DISTINCT d.tx_id) AS buy_trade_count,
        SUM(d.amount_usd) AS gross_buy_usd
    FROM dex_solana.trades d
    JOIN targets t
      ON d.token_bought_mint_address = t.token_address
     AND d.block_time < t.announced_at
     AND d.block_time >= t.announced_at - INTERVAL '{int(lookback_days)}' DAY
    WHERE d.block_month >= DATE '2020-01-01'
      AND d.trader_id IS NOT NULL
      AND d.amount_usd IS NOT NULL
    GROUP BY 1, 2, 3, 4
),
ranked AS (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY symbol, token_address, announced_at
            ORDER BY gross_buy_usd DESC, wallet_address
        ) AS wallet_rank
    FROM wallet_buys
)
SELECT
    symbol,
    token_address,
    announced_at,
    wallet_address,
    first_buy_at,
    last_buy_at,
    buy_trade_count,
    gross_buy_usd
FROM ranked
WHERE gross_buy_usd >= {float(minimum_total_buy_usd)}
  AND wallet_rank <= {int(max_wallets_per_target)}
ORDER BY announced_at, gross_buy_usd DESC;
"""


def render_weekly_solana_universe_sql(
    start_date: str = "2020-01-01",
    min_trade_usd: float = 5.0,
    min_weekly_volume_usd: float = 1_000.0,
    min_weekly_trades: int = 3,
    min_weekly_traders: int = 1,
) -> str:
    excluded = ", ".join(sql_string(symbol) for symbol in EXCLUDED_RESEARCH_SYMBOLS)
    return f"""-- Smart Money Radar Solana weekly point-in-time universe
WITH token_flows AS (
    SELECT
        d.block_time,
        d.tx_id,
        d.trader_id AS wallet_address,
        d.project,
        d.project_main_id AS pair_address,
        d.token_bought_mint_address AS token_address,
        d.token_bought_symbol AS token_symbol,
        d.token_bought_amount AS token_amount,
        d.amount_usd,
        'buy' AS side
    FROM dex_solana.trades d
    WHERE d.block_month >= DATE {sql_string(start_date)}
      AND d.block_time >= TIMESTAMP {sql_string(start_date + ' 00:00:00')}
      AND d.block_time < DATE_TRUNC('week', CURRENT_TIMESTAMP)
      AND d.trader_id IS NOT NULL
      AND d.amount_usd >= {float(min_trade_usd)}
      AND d.token_bought_amount > 0
      AND UPPER(COALESCE(d.token_bought_symbol, '')) NOT IN ({excluded})

    UNION ALL

    SELECT
        d.block_time,
        d.tx_id,
        d.trader_id AS wallet_address,
        d.project,
        d.project_main_id AS pair_address,
        d.token_sold_mint_address AS token_address,
        d.token_sold_symbol AS token_symbol,
        d.token_sold_amount AS token_amount,
        d.amount_usd,
        'sell' AS side
    FROM dex_solana.trades d
    WHERE d.block_month >= DATE {sql_string(start_date)}
      AND d.block_time >= TIMESTAMP {sql_string(start_date + ' 00:00:00')}
      AND d.block_time < DATE_TRUNC('week', CURRENT_TIMESTAMP)
      AND d.trader_id IS NOT NULL
      AND d.amount_usd >= {float(min_trade_usd)}
      AND d.token_sold_amount > 0
      AND UPPER(COALESCE(d.token_sold_symbol, '')) NOT IN ({excluded})
),
priced AS (
    SELECT
        *,
        amount_usd / NULLIF(token_amount, 0) AS token_price_usd,
        DATE_TRUNC('week', block_time) AS week_start
    FROM token_flows
    WHERE token_address IS NOT NULL
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
        COUNT(DISTINCT tx_id) AS trade_count,
        COUNT(DISTINCT CASE WHEN side = 'buy' THEN tx_id END) AS buy_trade_count,
        COUNT(DISTINCT CASE WHEN side = 'sell' THEN tx_id END) AS sell_trade_count,
        SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE 0 END) AS gross_buy_usd,
        SUM(CASE WHEN side = 'sell' THEN amount_usd ELSE 0 END) AS gross_sell_usd,
        SUM(CASE WHEN side = 'buy' THEN amount_usd ELSE -amount_usd END) AS net_flow_usd,
        SUM(amount_usd) AS volume_usd,
        MAX_BY(token_price_usd, block_time) AS close_price_usd,
        SUM(amount_usd) / NULLIF(SUM(token_amount), 0) AS vwap_price_usd
    FROM priced
    WHERE token_price_usd > 0 AND token_price_usd < 1e15
    GROUP BY 1, 2
)
SELECT
    token_address,
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
    CASE
        WHEN volume_usd >= 5000 AND trader_count >= 5 AND trade_count >= 10
        THEN TRUE ELSE FALSE
    END AS is_tradeable
FROM weekly
WHERE volume_usd >= {float(min_weekly_volume_usd)}
  AND trade_count >= {int(min_weekly_trades)}
  AND trader_count >= {int(min_weekly_traders)}
ORDER BY snapshot_at, volume_usd DESC;
"""


def write_solana_pre_listing_buyers_sql(
    output_path: Path,
    targets: list[dict[str, Any]],
    **kwargs: Any,
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_solana_pre_listing_buyers_sql(targets=targets, **kwargs),
        encoding="utf-8",
    )
    return len(targets)


def write_weekly_solana_universe_sql(
    output_path: Path,
    **kwargs: Any,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_weekly_solana_universe_sql(**kwargs),
        encoding="utf-8",
    )


def render_solana_target_row(target: dict[str, Any]) -> str:
    announced_at = target["announced_at"].replace("T", " ").split("+")[0]
    return (
        "        ("
        f"{sql_string(target['symbol'])}, "
        f"{sql_string(target['contract_address'])}, "
        f"TIMESTAMP {sql_string(announced_at)}"
        ")"
    )


def dune_evm_chain(chain_id: str) -> tuple[str, str]:
    try:
        return DUNE_EVM_BLOCKCHAINS[chain_id]
    except KeyError as exc:
        raise ValueError(f"Unsupported EVM research chain: {chain_id}") from exc
