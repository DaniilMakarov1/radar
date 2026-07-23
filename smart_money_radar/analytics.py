from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from smart_money_radar.storage import SQLiteStore, utc_now_iso


DEFAULT_ANALYTICS_LIMIT = 100
MAX_ANALYTICS_LIMIT = 5_000


class AnalyticsError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalyticsQuery:
    slug: str
    title: str
    description: str
    query_text: str


ANALYTICS_VIEWS: dict[str, str] = {
    "analytics_pre_listing_wallet_flows": """
CREATE VIEW analytics_pre_listing_wallet_flows AS
WITH latest_scores AS (
    SELECT ws.*
    FROM wallet_scores ws
    JOIN (
        SELECT
            chain_id,
            wallet_address,
            MAX(updated_at) AS latest_updated_at
        FROM wallet_scores
        GROUP BY chain_id, wallet_address
    ) latest
      ON latest.chain_id = ws.chain_id
     AND latest.wallet_address = ws.wallet_address
     AND latest.latest_updated_at = ws.updated_at
)
SELECT
    b.chain_id,
    b.symbol,
    b.token_address,
    b.announced_at,
    b.wallet_address,
    b.first_buy_at,
    b.last_buy_at,
    b.buy_trade_count,
    b.gross_buy_usd,
    COALESCE(h.pre_buy_usd, b.gross_buy_usd) AS pre_buy_usd,
    COALESCE(h.pre_sell_usd, 0) AS pre_sell_usd,
    COALESCE(h.post_buy_usd, 0) AS post_buy_usd,
    COALESCE(h.post_sell_usd, 0) AS post_sell_usd,
    COALESCE(h.net_pre_usd, b.gross_buy_usd) AS net_pre_usd,
    COALESCE(h.pre_sell_ratio, 0) AS pre_sell_ratio,
    COALESCE(h.post_sell_ratio, 0) AS post_sell_ratio,
    COALESCE(h.holding_label, 'unknown') AS holding_label,
    latest_scores.interest_score AS wallet_interest_score,
    latest_scores.noise_score AS wallet_noise_score,
    latest_scores.confidence_score AS wallet_confidence_score,
    latest_scores.label AS wallet_label,
    b.source,
    b.execution_id
FROM pre_listing_wallet_buys b
LEFT JOIN wallet_holding_metrics h
  ON h.chain_id = b.chain_id
 AND h.symbol = b.symbol
 AND h.token_address = b.token_address
 AND h.announced_at = b.announced_at
 AND h.wallet_address = b.wallet_address
LEFT JOIN latest_scores
  ON latest_scores.chain_id = b.chain_id
 AND latest_scores.wallet_address = b.wallet_address
""",
    "analytics_research_token_snapshots": """
CREATE VIEW analytics_research_token_snapshots AS
WITH latest_attention AS (
    SELECT tas.*
    FROM token_attention_snapshots tas
    JOIN (
        SELECT
            chain_id,
            token_address,
            MAX(observed_at) AS latest_observed_at
        FROM token_attention_snapshots
        GROUP BY chain_id, token_address
    ) latest
      ON latest.chain_id = tas.chain_id
     AND latest.token_address = tas.token_address
     AND latest.latest_observed_at = tas.observed_at
)
SELECT
    u.chain_id,
    u.token_address,
    u.token_symbol,
    u.snapshot_at,
    u.window_days,
    u.first_trade_at,
    u.last_trade_at,
    u.trader_count,
    u.buyer_count,
    u.seller_count,
    u.trade_count,
    u.gross_buy_usd,
    u.gross_sell_usd,
    u.net_flow_usd,
    u.volume_usd,
    u.close_price_usd,
    u.liquidity_usd,
    u.market_cap_usd,
    u.fdv_usd,
    u.holder_count,
    u.website_present,
    u.contract_verified,
    u.risk_score,
    u.is_tradeable,
    o.listing_at,
    o.listed_within_30d,
    o.listed_within_60d,
    o.listed_within_90d,
    o.return_7d,
    o.return_30d,
    o.return_60d,
    o.return_90d,
    o.max_favorable_excursion_90d,
    o.max_drawdown_90d,
    o.rug_proxy_30d,
    latest_attention.public_attention_score,
    latest_attention.onchain_attention_score,
    latest_attention.attention_gap_score,
    u.source,
    u.execution_id
FROM research_universe_snapshots u
LEFT JOIN research_token_outcomes o
  ON o.chain_id = u.chain_id
 AND o.token_address = u.token_address
 AND o.snapshot_at = u.snapshot_at
LEFT JOIN latest_attention
  ON latest_attention.chain_id = u.chain_id
 AND latest_attention.token_address = u.token_address
""",
    "analytics_live_signals": """
CREATE VIEW analytics_live_signals AS
WITH latest_observations AS (
    SELECT ro.*
    FROM radar_observations ro
    JOIN (
        SELECT
            chain_id,
            token_address,
            MAX(observed_at) AS latest_observed_at
        FROM radar_observations
        GROUP BY chain_id, token_address
    ) latest
      ON latest.chain_id = ro.chain_id
     AND latest.token_address = ro.token_address
     AND latest.latest_observed_at = ro.observed_at
)
SELECT
    s.signal_id,
    s.token_symbol,
    s.token_name,
    s.chain_id,
    s.contract_address AS token_address,
    s.signal_type,
    s.signal_level,
    s.confidence_score,
    s.status,
    s.detected_at,
    s.strong_wallet_count,
    s.cluster_count,
    s.net_buy_usd,
    s.liquidity_usd,
    s.social_silence_score,
    s.risk_score,
    latest_observations.tracked_wallet_count,
    latest_observations.buy_trade_count,
    latest_observations.sell_trade_count,
    latest_observations.weighted_wallet_score,
    s.risk_flags_json,
    s.evidence_json,
    s.updated_at
FROM signals s
LEFT JOIN latest_observations
  ON latest_observations.chain_id = s.chain_id
 AND latest_observations.token_address = s.contract_address
""",
    "analytics_funding_routes": """
CREATE VIEW analytics_funding_routes AS
WITH latest_scan AS (
    SELECT MAX(funding_scan_id) AS funding_scan_id
    FROM funding_scans
    WHERE status IN ('completed', 'success')
)
SELECT
    r.funding_route_id,
    r.funding_scan_id,
    r.route_key,
    r.route_type,
    r.canonical_asset,
    r.venue_scope,
    r.long_venue,
    r.long_symbol,
    r.short_venue,
    r.short_symbol,
    r.status,
    r.confidence_score,
    r.target_notional,
    r.market_capacity,
    r.capital_required,
    r.horizon_days,
    r.current_hourly_spread,
    r.current_gross_apr,
    r.projected_hourly_spread,
    r.projected_gross_apr,
    r.historical_median_hourly_spread,
    r.positive_spread_fraction,
    r.persistence_score,
    r.history_point_count,
    r.expected_gross_funding,
    r.expected_net_profit,
    r.net_roc_annualized,
    r.total_fees,
    r.slippage_cost,
    r.basis_gap,
    r.basis_reserve,
    r.operations_buffer,
    r.long_next_funding_at,
    r.short_next_funding_at,
    r.observed_at,
    r.risk_flags_json,
    r.evidence_json
FROM funding_routes r
JOIN latest_scan ON latest_scan.funding_scan_id = r.funding_scan_id
""",
    "analytics_prediction_routes": """
CREATE VIEW analytics_prediction_routes AS
WITH latest_scan AS (
    SELECT MAX(prediction_scan_id) AS prediction_scan_id
    FROM prediction_scans
    WHERE status IN ('completed', 'success')
)
SELECT
    r.prediction_route_id,
    r.prediction_scan_id,
    r.route_key,
    r.route_type,
    r.title,
    r.venue_scope,
    r.event_id,
    r.status,
    r.confidence_score,
    r.semantic_match_score,
    r.guaranteed_payout_per_share,
    r.max_executable_size,
    r.optimal_size,
    r.gross_edge_per_share,
    r.net_edge_per_share,
    r.expected_gross_profit,
    r.expected_net_profit,
    r.capital_required,
    r.total_fees,
    r.slippage_cost,
    r.operations_buffer,
    r.capital_lock_days,
    r.annualized_return,
    r.observed_at,
    r.risk_flags_json,
    r.evidence_json
FROM prediction_routes r
JOIN latest_scan ON latest_scan.prediction_scan_id = r.prediction_scan_id
""",
}


BUILTIN_QUERIES: tuple[AnalyticsQuery, ...] = (
    AnalyticsQuery(
        slug="pre-listing-wallet-leaders",
        title="Pre-listing Wallet Leaders",
        description="Wallets ranked by imported historical pre-listing flow and holding quality.",
        query_text="""
SELECT
    chain_id,
    wallet_address,
    COUNT(DISTINCT token_address) AS token_count,
    COUNT(DISTINCT symbol) AS symbol_count,
    SUM(gross_buy_usd) AS gross_buy_usd,
    SUM(net_pre_usd) AS net_pre_usd,
    AVG(pre_sell_ratio) AS avg_pre_sell_ratio,
    AVG(post_sell_ratio) AS avg_post_sell_ratio,
    MAX(wallet_interest_score) AS wallet_interest_score,
    MAX(wallet_confidence_score) AS wallet_confidence_score,
    MAX(wallet_label) AS wallet_label,
    MIN(first_buy_at) AS first_observed_buy_at,
    MAX(last_buy_at) AS last_observed_buy_at
FROM analytics_pre_listing_wallet_flows
GROUP BY chain_id, wallet_address
ORDER BY gross_buy_usd DESC, token_count DESC
""",
    ),
    AnalyticsQuery(
        slug="research-universe-readiness",
        title="Research Universe Readiness",
        description="Point-in-time token universe coverage and outcome maturity by chain.",
        query_text="""
SELECT
    chain_id,
    COUNT(*) AS snapshot_count,
    COUNT(DISTINCT token_address) AS token_count,
    MIN(snapshot_at) AS first_snapshot_at,
    MAX(snapshot_at) AS last_snapshot_at,
    SUM(CASE WHEN is_tradeable = 1 THEN 1 ELSE 0 END) AS tradeable_snapshot_count,
    SUM(CASE WHEN listed_within_90d = 1 THEN 1 ELSE 0 END) AS positive_90d_count,
    AVG(volume_usd) AS avg_volume_usd,
    AVG(trade_count) AS avg_trade_count,
    AVG(attention_gap_score) AS avg_attention_gap_score
FROM analytics_research_token_snapshots
GROUP BY chain_id
ORDER BY snapshot_count DESC
""",
    ),
    AnalyticsQuery(
        slug="live-signal-funnel",
        title="Live Signal Funnel",
        description="Current live signal distribution by status, level, and type.",
        query_text="""
SELECT
    chain_id,
    status,
    signal_level,
    signal_type,
    COUNT(*) AS signal_count,
    SUM(COALESCE(net_buy_usd, 0)) AS net_buy_usd,
    AVG(confidence_score) AS avg_confidence_score,
    AVG(COALESCE(liquidity_usd, 0)) AS avg_liquidity_usd,
    AVG(COALESCE(risk_score, 0)) AS avg_risk_score
FROM analytics_live_signals
GROUP BY chain_id, status, signal_level, signal_type
ORDER BY signal_count DESC, net_buy_usd DESC
""",
    ),
    AnalyticsQuery(
        slug="funding-route-leaders",
        title="Funding Route Leaders",
        description="Latest funding routes ranked by net profit and annualized return.",
        query_text="""
SELECT
    canonical_asset,
    long_venue,
    short_venue,
    status,
    target_notional,
    market_capacity,
    expected_net_profit,
    net_roc_annualized,
    positive_spread_fraction,
    persistence_score,
    observed_at
FROM analytics_funding_routes
ORDER BY expected_net_profit DESC, net_roc_annualized DESC
""",
    ),
    AnalyticsQuery(
        slug="prediction-route-leaders",
        title="Prediction Route Leaders",
        description="Latest prediction-market routes ranked by expected net profit.",
        query_text="""
SELECT
    route_type,
    venue_scope,
    status,
    title,
    optimal_size,
    expected_net_profit,
    annualized_return,
    confidence_score,
    observed_at
FROM analytics_prediction_routes
ORDER BY expected_net_profit DESC, annualized_return DESC
""",
    ),
)


LOAD_EXTENSION_PATTERN = re.compile(r"\bload_extension\s*\(", flags=re.IGNORECASE)
SQLITE_WRITE_ACTIONS = {
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_PRAGMA,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_UPDATE,
}


def initialize_analytics(store: SQLiteStore) -> dict[str, Any]:
    store.init_db()
    with store.connect() as connection:
        ensure_analytics_views(connection)
        upsert_builtin_queries(connection)
        return analytics_catalog(connection)


def ensure_analytics_views(connection: sqlite3.Connection) -> None:
    for view_name, create_sql in ANALYTICS_VIEWS.items():
        row = connection.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'view' AND name = ?
            """,
            (view_name,),
        ).fetchone()
        if row and normalize_sql_definition(row["sql"]) == normalize_sql_definition(
            create_sql
        ):
            continue
        connection.execute(f"DROP VIEW IF EXISTS {view_name}")
        connection.execute(create_sql)


def upsert_builtin_queries(connection: sqlite3.Connection) -> None:
    now = utc_now_iso()
    connection.executemany(
        """
        INSERT INTO analytics_queries (
            query_slug,
            title,
            description,
            query_text,
            source,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, 'builtin', ?, ?)
        ON CONFLICT(query_slug) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            query_text = excluded.query_text,
            source = excluded.source,
            updated_at = CASE
                WHEN analytics_queries.title <> excluded.title
                  OR analytics_queries.description <> excluded.description
                  OR analytics_queries.query_text <> excluded.query_text
                  OR analytics_queries.source <> excluded.source
                THEN excluded.updated_at
                ELSE analytics_queries.updated_at
            END
        """,
        [
            (
                query.slug,
                query.title,
                query.description,
                query.query_text.strip(),
                now,
                now,
            )
            for query in BUILTIN_QUERIES
        ],
    )


def analytics_catalog(connection: sqlite3.Connection) -> dict[str, Any]:
    views = [
        {
            "name": row["name"],
            "kind": "view",
        }
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'view' AND name LIKE 'analytics_%'
            ORDER BY name
            """
        )
    ]
    queries = [
        dict(row)
        for row in connection.execute(
            """
            SELECT query_slug, title, description, source, updated_at
            FROM analytics_queries
            ORDER BY source, query_slug
            """
        )
    ]
    return {
        "engine": "sqlite",
        "views": views,
        "queries": queries,
    }


def run_saved_query(
    store: SQLiteStore,
    slug: str,
    limit: int = DEFAULT_ANALYTICS_LIMIT,
) -> dict[str, Any]:
    initialize_analytics(store)
    with store.connect() as connection:
        row = connection.execute(
            """
            SELECT query_text
            FROM analytics_queries
            WHERE query_slug = ?
            """,
            (slug,),
        ).fetchone()
        if row is None:
            raise AnalyticsError(f"Unknown analytics query: {slug}")
        return run_sql(
            store,
            row["query_text"],
            limit=limit,
            query_slug=slug,
            initialize=False,
        )


def run_sql(
    store: SQLiteStore,
    sql: str,
    limit: int = DEFAULT_ANALYTICS_LIMIT,
    query_slug: str | None = None,
    initialize: bool = True,
) -> dict[str, Any]:
    if initialize:
        initialize_analytics(store)
    bounded_limit = bounded_query_limit(limit)
    query_text = sanitize_select_sql(sql)
    query_hash = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
    started = utc_now_iso()
    started_monotonic = time.monotonic()
    with store.connect() as connection:
        execution_id = start_execution(
            connection,
            query_slug=query_slug,
            query_hash=query_hash,
            started_at=started,
            limit_rows=bounded_limit,
        )
        try:
            connection.set_authorizer(read_only_authorizer)
            cursor = connection.execute(
                f"SELECT * FROM ({query_text}) AS radar_analytics_query LIMIT ?",
                (bounded_limit,),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            columns = [description[0] for description in cursor.description or []]
        except Exception as exc:
            connection.set_authorizer(None)
            finish_execution(
                connection,
                execution_id=execution_id,
                status="failed",
                elapsed_ms=elapsed_ms(started_monotonic),
                row_count=0,
                error=str(exc),
            )
            connection.commit()
            raise AnalyticsError(str(exc)) from exc
        finally:
            connection.set_authorizer(None)
        finish_execution(
            connection,
            execution_id=execution_id,
            status="completed",
            elapsed_ms=elapsed_ms(started_monotonic),
            row_count=len(rows),
            error=None,
        )
    return {
        "analytics_execution_id": execution_id,
        "engine": "sqlite",
        "query_slug": query_slug,
        "query_hash": query_hash,
        "limit": bounded_limit,
        "row_count": len(rows),
        "columns": columns,
        "rows": rows,
    }


def prune_analytics_executions(
    store: SQLiteStore,
    keep_latest: int = 5,
) -> int:
    retained = max(0, int(keep_latest))
    with store.connect() as connection:
        if retained == 0:
            cursor = connection.execute("DELETE FROM analytics_executions")
            return int(cursor.rowcount if cursor.rowcount is not None else 0)
        retained_ids = [
            int(row["analytics_execution_id"])
            for row in connection.execute(
                """
                SELECT analytics_execution_id
                FROM analytics_executions
                ORDER BY started_at DESC, analytics_execution_id DESC
                LIMIT ?
                """,
                (retained,),
            )
        ]
        if not retained_ids:
            return 0
        placeholders = ",".join("?" for _ in retained_ids)
        cursor = connection.execute(
            f"""
            DELETE FROM analytics_executions
            WHERE analytics_execution_id NOT IN ({placeholders})
            """,
            retained_ids,
        )
        return int(cursor.rowcount if cursor.rowcount is not None else 0)


def start_execution(
    connection: sqlite3.Connection,
    query_slug: str | None,
    query_hash: str,
    started_at: str,
    limit_rows: int,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO analytics_executions (
            query_slug,
            query_hash,
            engine,
            status,
            started_at,
            limit_rows
        )
        VALUES (?, ?, 'sqlite', 'running', ?, ?)
        """,
        (query_slug, query_hash, started_at, limit_rows),
    )
    return int(cursor.lastrowid)


def finish_execution(
    connection: sqlite3.Connection,
    execution_id: int,
    status: str,
    elapsed_ms: int,
    row_count: int,
    error: str | None,
) -> None:
    connection.execute(
        """
        UPDATE analytics_executions
        SET status = ?,
            finished_at = ?,
            elapsed_ms = ?,
            row_count = ?,
            error = ?
        WHERE analytics_execution_id = ?
        """,
        (status, utc_now_iso(), elapsed_ms, row_count, error, execution_id),
    )


def sanitize_select_sql(sql: str) -> str:
    cleaned = strip_sql_comments(sql).strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if not cleaned:
        raise AnalyticsError("Analytics SQL is empty")
    if ";" in cleaned:
        raise AnalyticsError("Analytics SQL must contain a single SELECT statement")
    first_token = cleaned.split(None, 1)[0].lower()
    if first_token not in {"select", "with"}:
        raise AnalyticsError("Analytics SQL must start with SELECT or WITH")
    if LOAD_EXTENSION_PATTERN.search(cleaned):
        raise AnalyticsError("Analytics SQL is read-only; load_extension is blocked")
    return cleaned


def read_only_authorizer(
    action_code: int,
    arg1: str | None,
    arg2: str | None,
    db_name: str | None,
    trigger_name: str | None,
) -> int:
    if action_code in SQLITE_WRITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action_code == sqlite3.SQLITE_FUNCTION:
        function_name = str(arg2 or arg1 or "").lower()
        if function_name == "load_extension":
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def normalize_sql_definition(sql: str | None) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip()).lower()


def strip_sql_comments(sql: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    lines = []
    for line in without_blocks.splitlines():
        if "--" in line:
            line = line.split("--", 1)[0]
        lines.append(line)
    return "\n".join(lines)


def bounded_query_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_ANALYTICS_LIMIT))


def elapsed_ms(started_monotonic: float) -> int:
    return max(0, int((time.monotonic() - started_monotonic) * 1000))
