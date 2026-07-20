from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from smart_money_radar.prediction.clients import (
    KalshiClient,
    PolymarketClient,
    PredictionDataError,
)
from smart_money_radar.prediction.intelligence import (
    build_event_token_links,
    normalize_closed_positions,
    score_prediction_wallets,
)
from smart_money_radar.prediction.normalization import (
    normalize_kalshi_catalog,
    normalize_kalshi_orderbook,
    normalize_polymarket_catalog,
    normalize_polymarket_orderbook,
    enrich_polymarket_market_info,
)
from smart_money_radar.prediction.paper import PaperConfig, simulate_paper_routes
from smart_money_radar.prediction.scanner import ScannerConfig, scan_prediction_routes
from smart_money_radar.storage import SQLiteStore, utc_now_iso


@dataclass(frozen=True)
class PredictionScanConfig:
    events_per_venue: int = 24
    max_markets_per_venue: int = 500
    kalshi_market_pages: int = 5
    wallet_limit: int = 10
    wallet_position_limit: int = 60
    wallet_refresh_hours: float = 24.0
    paper_size: float = 100.0
    paper_latency_ms: int = 750
    paper_depth_haircut: float = 0.8
    retention_days: int = 7
    collect_wallets: bool = True


def run_prediction_scan(
    store: SQLiteStore,
    config: PredictionScanConfig | None = None,
    polymarket: PolymarketClient | None = None,
    kalshi: KalshiClient | None = None,
) -> dict[str, Any]:
    settings = config or PredictionScanConfig()
    store.init_db()
    scan_id = store.start_prediction_scan(asdict(settings))
    observed_at = utc_now_iso()
    poly_client = polymarket or PolymarketClient()
    kalshi_client = kalshi or KalshiClient()
    warnings: list[str] = []
    counts = {
        "polymarket_event_count": 0,
        "kalshi_event_count": 0,
        "market_count": 0,
        "orderbook_count": 0,
        "route_count": 0,
        "executable_route_count": 0,
    }
    try:
        raw_poly_events = poly_client.events(settings.events_per_venue)
        raw_kalshi_events = kalshi_client.top_events(
            settings.events_per_venue,
            market_pages=settings.kalshi_market_pages,
        )
        poly_events, poly_markets = normalize_polymarket_catalog(
            raw_poly_events,
            observed_at,
            settings.max_markets_per_venue,
        )
        kalshi_events, kalshi_markets = normalize_kalshi_catalog(
            raw_kalshi_events,
            observed_at,
            settings.max_markets_per_venue,
        )
        missing_fee_conditions = [
            str(market.get("condition_id"))
            for market in poly_markets
            if market.get("fees_enabled")
            and not market.get("fee_verified")
            and market.get("condition_id")
        ]
        if missing_fee_conditions:
            try:
                market_info = poly_client.clob_market_info(missing_fee_conditions)
                enrich_polymarket_market_info(poly_markets, market_info)
            except PredictionDataError as exc:
                warnings.append(f"Polymarket fee metadata incomplete: {exc}")
        events = poly_events + kalshi_events
        markets = poly_markets + kalshi_markets
        counts["polymarket_event_count"] = len(poly_events)
        counts["kalshi_event_count"] = len(kalshi_events)
        counts["market_count"] = len(markets)
        store.upsert_prediction_catalog(events, markets)

        poly_token_ids = [
            token_id
            for market in poly_markets
            if market.get("status") != "closed" and market.get("accepting_orders")
            for token_id in (market.get("yes_token_id"), market.get("no_token_id"))
            if token_id
        ]
        raw_poly_books = poly_client.orderbooks(poly_token_ids)
        poly_books = [
            book
            for market in poly_markets
            if market.get("status") != "closed" and market.get("accepting_orders")
            for book in [
                normalize_polymarket_orderbook(market, raw_poly_books, observed_at)
            ]
            if book
        ]
        open_kalshi_ids = [
            market["market_id"]
            for market in kalshi_markets
            if market.get("status") == "open" and market.get("accepting_orders")
        ]
        raw_kalshi_books = kalshi_client.orderbooks(open_kalshi_ids)
        kalshi_books = [
            book
            for market in kalshi_markets
            if market.get("status") == "open" and market.get("accepting_orders")
            for book in [
                normalize_kalshi_orderbook(
                    market,
                    raw_kalshi_books.get(market["market_id"]),
                    observed_at,
                )
            ]
            if book
        ]
        books = poly_books + kalshi_books
        counts["orderbook_count"] = store.insert_prediction_orderbooks(
            scan_id, books
        )

        constraints, matches, routes = scan_prediction_routes(
            events,
            markets,
            books,
            observed_at,
            ScannerConfig(),
        )
        store.insert_prediction_constraints(scan_id, constraints)
        store.insert_prediction_contract_matches(scan_id, matches)
        store.insert_prediction_routes(scan_id, routes)
        previous_routes = store.previous_prediction_routes(scan_id)
        paper_rows = simulate_paper_routes(
            previous_routes,
            books,
            observed_at,
            PaperConfig(
                target_size=settings.paper_size,
                minimum_latency_ms=settings.paper_latency_ms,
                depth_haircut=settings.paper_depth_haircut,
            ),
        )
        store.insert_prediction_paper_executions(scan_id, paper_rows)
        counts["route_count"] = len(routes)
        counts["executable_route_count"] = sum(
            route["status"] == "executable" for route in routes
        )
        paper_candidate_count = sum(
            route["status"] == "paper_candidate" for route in routes
        )

        wallet_score_count = 0
        wallet_position_count = 0
        wallet_cache_fresh = store.prediction_wallet_scores_are_fresh(
            settings.wallet_refresh_hours
        )
        if settings.collect_wallets and settings.wallet_limit > 0 and not wallet_cache_fresh:
            try:
                leaderboard = poly_client.leaderboard(settings.wallet_limit)
                positions_by_wallet: dict[str, list[dict[str, Any]]] = {}
                all_positions = []
                for leaderboard_row in leaderboard:
                    wallet = str(leaderboard_row.get("proxyWallet") or "").lower()
                    if not wallet:
                        continue
                    try:
                        raw_positions = poly_client.closed_positions(
                            wallet,
                            settings.wallet_position_limit,
                        )
                    except PredictionDataError as exc:
                        warnings.append(f"Wallet {wallet} history skipped: {exc}")
                        raw_positions = []
                    positions = normalize_closed_positions(
                        wallet,
                        raw_positions,
                        observed_at,
                        history_complete=len(raw_positions)
                        < settings.wallet_position_limit,
                    )
                    positions_by_wallet[wallet] = positions
                    all_positions.extend(positions)
                wallet_position_count = store.upsert_prediction_wallet_positions(
                    all_positions
                )
                scores = score_prediction_wallets(
                    leaderboard,
                    positions_by_wallet,
                    observed_at,
                )
                wallet_score_count = store.upsert_prediction_wallet_scores(scores)
            except PredictionDataError as exc:
                warnings.append(f"Polymarket wallet intelligence skipped: {exc}")
        elif settings.collect_wallets and wallet_cache_fresh:
            warnings.append("Polymarket wallet scores reused from the fresh cache.")

        token_links = build_event_token_links(
            events,
            store.prediction_token_candidates(),
            observed_at,
        )
        token_link_count = store.replace_prediction_event_token_links(token_links)
        store.finish_prediction_scan(scan_id, "success", **counts)
        retention = store.prune_prediction_history(
            keep_scans=max(2, settings.retention_days * 24 * 60)
        )
        return {
            "prediction_scan_id": scan_id,
            "status": "success",
            **counts,
            "constraint_count": len(constraints),
            "contract_match_count": len(matches),
            "paper_execution_count": len(paper_rows),
            "paper_candidate_count": paper_candidate_count,
            "wallet_score_count": wallet_score_count,
            "wallet_position_count": wallet_position_count,
            "token_link_count": token_link_count,
            "retention": retention,
            "warnings": warnings,
        }
    except Exception as exc:
        store.finish_prediction_scan(
            scan_id,
            "failed",
            error=str(exc),
            **counts,
        )
        raise


def watch_prediction_markets(
    store: SQLiteStore,
    interval_seconds: int = 60,
    iterations: int | None = None,
    config: PredictionScanConfig | None = None,
) -> None:
    completed = 0
    while iterations is None or completed < iterations:
        run_prediction_scan(store, config=config)
        completed += 1
        if iterations is None or completed < iterations:
            time.sleep(max(5, interval_seconds))
