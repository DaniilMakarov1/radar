from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.funding.adapters import (
    AevoFundingClient,
    ApexFundingClient,
    AsterFundingClient,
    BackpackFundingClient,
    BinanceFundingClient,
    BingXFundingClient,
    BitMartFundingClient,
    BitgetFundingClient,
    BybitFundingClient,
    CoinExFundingClient,
    DeribitFundingClient,
    DydxFundingClient,
    DriftFundingClient,
    EdgexFundingClient,
    EtherealFundingClient,
    ExtendedFundingClient,
    FundingDataError,
    FundingVenueClient,
    GateFundingClient,
    GrvtFundingClient,
    HTXFundingClient,
    HyperliquidFundingClient,
    KrakenFundingClient,
    KuCoinFundingClient,
    LighterFundingClient,
    MEXCFundingClient,
    OKXFundingClient,
    PacificaFundingClient,
    ParadexFundingClient,
    ReyaFundingClient,
    VariationalFundingClient,
    VertexFundingClient,
    WOOXFundingClient,
)
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.normalization import (
    canonical_asset_symbol,
    normalize_catalog_canonical_units,
    normalize_orderbook_canonical_units,
    normalize_stored_orderbook_units,
)
from smart_money_radar.funding.paper import simulate_paper_revalidations
from smart_money_radar.funding.retention import apply_funding_retention_plan
from smart_money_radar.funding.scanner import (
    execution_shortlist,
    funding_universe_row,
    rank_perp_pairs,
    scan_ranked_pairs,
)
from smart_money_radar.funding.venues import DEACTIVATED_FUNDING_VENUES
from smart_money_radar.storage import SQLiteStore, utc_now_iso


FUNDING_SCAN_RETENTION = 1
FUNDING_HISTORY_RETENTION_PER_MARKET = 24
RETENTION_SCAN_MODES = {"auto", "watch"}


def active_default_funding_clients() -> list[FundingVenueClient]:
    return [
        BinanceFundingClient(),
        BitgetFundingClient(),
        HyperliquidFundingClient(),
        BybitFundingClient(),
        OKXFundingClient(),
        DydxFundingClient(),
        GateFundingClient(),
        GrvtFundingClient(),
        HTXFundingClient(),
        BackpackFundingClient(),
        DriftFundingClient(),
        EdgexFundingClient(),
        EtherealFundingClient(),
        ExtendedFundingClient(),
        AsterFundingClient(),
        LighterFundingClient(),
        KuCoinFundingClient(),
        MEXCFundingClient(),
        ParadexFundingClient(),
        KrakenFundingClient(),
        DeribitFundingClient(),
        VertexFundingClient(),
        WOOXFundingClient(),
        CoinExFundingClient(),
        BitMartFundingClient(),
        AevoFundingClient(),
        ApexFundingClient(),
        PacificaFundingClient(),
        ReyaFundingClient(),
        VariationalFundingClient(),
    ]


def filter_deactivated_funding_clients(
    clients: list[FundingVenueClient],
) -> list[FundingVenueClient]:
    return [
        client
        for client in clients
        if str(client.venue).lower() not in DEACTIVATED_FUNDING_VENUES
    ]


def run_funding_scan(
    store: SQLiteStore,
    config: FundingScanConfig | None = None,
    binance: BinanceFundingClient | None = None,
    bitget: BitgetFundingClient | None = None,
    hyperliquid: HyperliquidFundingClient | None = None,
    bybit: BybitFundingClient | None = None,
    deribit: DeribitFundingClient | None = None,
    okx: OKXFundingClient | None = None,
    dydx: DydxFundingClient | None = None,
    gate: GateFundingClient | None = None,
    bingx: BingXFundingClient | None = None,
    htx: HTXFundingClient | None = None,
    backpack: BackpackFundingClient | None = None,
    drift: DriftFundingClient | None = None,
    ethereal: EtherealFundingClient | None = None,
    extended: ExtendedFundingClient | None = None,
    aster: AsterFundingClient | None = None,
    lighter: LighterFundingClient | None = None,
    kraken: KrakenFundingClient | None = None,
    kucoin: KuCoinFundingClient | None = None,
    mexc: MEXCFundingClient | None = None,
    paradex: ParadexFundingClient | None = None,
    vertex_base: VertexFundingClient | None = None,
    venue_clients: list[FundingVenueClient] | None = None,
    scan_mode: str = "manual",
    hydrate_missing_history: bool = True,
) -> dict[str, Any]:
    settings = (config or FundingScanConfig()).validated()
    normalized_scan_mode = scan_mode if scan_mode in {"manual", "auto", "watch"} else "manual"
    store.init_db()
    scan_id = store.start_funding_scan(
        {**asdict(settings), "scan_mode": normalized_scan_mode}
    )
    observed_at = utc_now_iso()
    observed_datetime = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if observed_datetime.tzinfo is None:
        observed_datetime = observed_datetime.replace(tzinfo=UTC)
    if venue_clients is not None:
        configured_clients = filter_deactivated_funding_clients(list(venue_clients))
    elif any(
        client is not None
        for client in (
            binance, bitget, hyperliquid, bybit, okx, dydx, gate, backpack, drift,
            ethereal, extended, bingx, htx, aster, lighter, kucoin, mexc, paradex,
            kraken, deribit, vertex_base,
        )
    ):
        configured_clients = [
            client
            for client in (
                binance, bitget, hyperliquid, bybit, okx, dydx, gate, backpack, drift,
                ethereal, extended, bingx, htx, aster, lighter, kucoin, mexc,
                paradex, kraken, deribit, vertex_base,
            )
            if client is not None
        ]
        configured_clients = filter_deactivated_funding_clients(configured_clients)
    else:
        configured_clients = active_default_funding_clients()
    clients = {str(client.venue): client for client in configured_clients}
    warnings: list[str] = []
    counts = {
        "instrument_count": 0,
        "market_snapshot_count": 0,
        "orderbook_count": 0,
        "history_row_count": 0,
        "route_count": 0,
        "paper_candidate_count": 0,
        "paper_execution_count": 0,
    }
    try:
        previous_routes = store.previous_funding_routes(scan_id)
        instruments: list[dict[str, Any]] = []
        markets: list[dict[str, Any]] = []
        available_venues: set[str] = set()
        catalog_results: dict[
            str,
            tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]],
        ] = {}
        catalog_errors: dict[str, FundingDataError] = {}
        cached_catalog_results: dict[
            str,
            tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]],
        ] = {}
        if clients and settings.market_snapshot_cache_ttl_seconds > 0:
            catalog_cache_floor = (
                observed_datetime
                - timedelta(seconds=settings.market_snapshot_cache_ttl_seconds)
            ).isoformat()
            cached_catalog_results = store.latest_funding_catalog_by_venue(
                list(clients),
                min_observed_at=catalog_cache_floor,
            )
            cached_catalog_results = {
                venue: result
                for venue, result in cached_catalog_results.items()
                if cached_catalog_is_safe(result[1], observed_datetime)
            }
            catalog_results.update(cached_catalog_results)
        clients_to_fetch = {
            venue: client
            for venue, client in clients.items()
            if venue not in catalog_results
        }
        with ThreadPoolExecutor(max_workers=max(1, len(clients))) as executor:
            future_venues = {
                executor.submit(client.catalog_and_markets, observed_at): venue
                for venue, client in clients_to_fetch.items()
            }
            for future in as_completed(future_venues):
                venue = future_venues[future]
                try:
                    catalog_results[venue] = future.result()
                except FundingDataError as exc:
                    catalog_errors[venue] = exc
        if cached_catalog_results:
            warnings.append(
                "Reused "
                f"{len(cached_catalog_results)} venue market snapshots from <= "
                f"{settings.market_snapshot_cache_ttl_seconds}s cache; fetched "
                f"{len(clients_to_fetch)} venues."
            )
        for venue in clients:
            if venue in catalog_errors:
                warnings.append(f"{venue} catalog skipped: {catalog_errors[venue]}")
                continue
            venue_instruments, venue_markets, venue_warnings = catalog_results[venue]
            if venue not in cached_catalog_results:
                venue_instruments, venue_markets = normalize_catalog_canonical_units(
                    venue_instruments,
                    venue_markets,
                )
            instruments.extend(venue_instruments)
            markets.extend(venue_markets)
            warnings.extend(venue_warnings)
            if venue_markets:
                available_venues.add(venue)
        instruments, markets, removed_non_crypto = remove_non_crypto_funding_rows(
            instruments,
            markets,
            explicit_assets=client_non_crypto_assets(clients),
        )
        if removed_non_crypto:
            warnings.append(non_crypto_filter_warning(removed_non_crypto))
        available_venues = {str(row["venue"]) for row in markets if row.get("venue")}
        if len(available_venues) < 2:
            raise FundingDataError(
                "Funding scan requires current market data from at least two venues"
            )
        counts["instrument_count"] = store.upsert_funding_instruments(instruments)
        counts["market_snapshot_count"] = store.insert_funding_market_snapshots(
            scan_id,
            markets,
            include_raw_json=settings.store_diagnostic_raw_json,
        )

        universe_candidates = rank_perp_pairs(
            markets,
            settings.max_candidates,
            pinned_routes=previous_routes,
            config=settings,
            observed_at=observed_at,
        )
        candidates = execution_shortlist(
            universe_candidates,
            settings.near_miss_full_depth_routes,
            config=settings,
        )
        universe_route_count = store.insert_funding_route_universe(
            scan_id,
            [
                funding_universe_row(candidate, observed_at)
                for candidate in universe_candidates
            ],
        )
        books: dict[tuple[str, str], dict[str, Any]] = {}
        history: dict[tuple[str, str], list[dict[str, Any]]] = {}
        history_start = observed_datetime.astimezone(UTC) - timedelta(days=settings.history_days)
        history_start_ms = int(history_start.timestamp() * 1000)
        candidate_markets: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in candidates:
            for market in (candidate["long_market"], candidate["short_market"]):
                venue = str(market["venue"])
                symbol = str(market["symbol"])
                key = (venue, symbol)
                candidate_markets[key] = market

        cached_books: dict[tuple[str, str], dict[str, Any]] = {}
        if candidate_markets and settings.orderbook_cache_ttl_seconds > 0:
            cache_floor = (
                observed_datetime
                - timedelta(seconds=settings.orderbook_cache_ttl_seconds)
            ).isoformat()
            cached_books = store.latest_funding_orderbooks(
                list(candidate_markets),
                min_observed_at=cache_floor,
            )
            for key, book in cached_books.items():
                book_time = datetime.fromisoformat(
                    str(book.get("observed_at") or observed_at).replace("Z", "+00:00")
                )
                if book_time.tzinfo is None:
                    book_time = book_time.replace(tzinfo=UTC)
                cache_age_seconds = max(
                    0.0,
                    (observed_datetime - book_time.astimezone(UTC)).total_seconds(),
                )
                book["_orderbook_cache"] = {
                    "reused": True,
                    "age_seconds": cache_age_seconds,
                    "ttl_seconds": settings.orderbook_cache_ttl_seconds,
                }
            books.update(cached_books)
        markets_to_fetch = {
            key: market
            for key, market in candidate_markets.items()
            if key not in books
        }
        book_errors: dict[tuple[str, str], FundingDataError] = {}
        fresh_books: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(32, len(markets_to_fetch) or 1)) as executor:
            future_books = {
                executor.submit(
                    clients[venue].orderbook,
                    symbol,
                    observed_at,
                    100,
                ): (venue, symbol)
                for venue, symbol in markets_to_fetch
            }
            for future in as_completed(future_books):
                key = future_books[future]
                try:
                    market = candidate_markets[key]
                    books[key] = normalize_orderbook_canonical_units(
                        future.result(),
                        float(market.get("canonical_unit_multiplier") or 1.0),
                    )
                    fresh_books.append(books[key])
                except FundingDataError as exc:
                    book_errors[key] = exc
        for (venue, symbol), exc in sorted(book_errors.items()):
            warnings.append(f"{venue} {symbol} orderbook skipped: {exc}")
        if cached_books:
            warnings.append(
                "Reused "
                f"{len(cached_books)} orderbooks from <= "
                f"{settings.orderbook_cache_ttl_seconds}s cache; fetched "
                f"{len(fresh_books)} fresh orderbooks."
            )

        history_required_markets = executable_history_market_keys(
            candidates,
            books,
            settings.minimum_market_capacity,
        )
        stale_history = {}
        if normalized_scan_mode != "auto" and hydrate_missing_history:
            stale_history = {
                key: market
                for key, market in candidate_markets.items()
                if key in history_required_markets
                and bool(getattr(clients[key[0]], "live_history_enabled", True))
                and not store.funding_history_is_fresh(
                    key[0],
                    key[1],
                    settings.history_refresh_hours,
                )
            }
        stale_history_before_budget = len(stale_history)
        if stale_history and settings.max_live_history_markets == 0:
            warnings.append(
                "Live history hydration disabled for this scan; use "
                "funding-history-backfill for complete funding history."
            )
            stale_history = {}
        elif (
            stale_history
            and settings.max_live_history_markets is not None
            and len(stale_history) > settings.max_live_history_markets
        ):
            selected_history_markets = round_robin_by_venue(
                list(stale_history.values())
            )[: settings.max_live_history_markets]
            selected_history_keys = {
                (str(market["venue"]), str(market["symbol"]))
                for market in selected_history_markets
            }
            stale_history = {
                key: market
                for key, market in stale_history.items()
                if key in selected_history_keys
            }
            warnings.append(
                "Live history hydration limited to "
                f"{len(stale_history)} of {stale_history_before_budget} markets; "
                "run funding-history-backfill for complete funding history."
            )
        latest_history = store.funding_history_latest_at_map(list(stale_history))
        history_request_starts: dict[tuple[str, str], datetime] = {}
        for key, market in stale_history.items():
            latest_at = latest_history.get(key)
            latest = (
                datetime.fromisoformat(latest_at.replace("Z", "+00:00"))
                if latest_at
                else None
            )
            if latest is not None and latest.tzinfo is None:
                latest = latest.replace(tzinfo=UTC)
            if latest is not None:
                overlap_hours = max(
                    2.0,
                    2.0 * float(market.get("funding_interval_hours") or 1.0),
                )
                requested = max(
                    history_start,
                    latest.astimezone(UTC) - timedelta(hours=overlap_hours),
                )
            else:
                # Bootstrap enough data for short-horizon forecasting. The
                # separate backfill pipeline remains responsible for 90d risk.
                requested = max(
                    history_start,
                    observed_datetime - timedelta(days=30),
                )
            effective = client_history_request_start_at(
                clients[key[0]],
                requested,
                observed_datetime,
            )
            parsed_effective = datetime.fromisoformat(effective.replace("Z", "+00:00"))
            history_request_starts[key] = (
                parsed_effective.replace(tzinfo=UTC)
                if parsed_effective.tzinfo is None
                else parsed_effective.astimezone(UTC)
            )
        fresh_history: dict[tuple[str, str], list[dict[str, Any]]] = {}
        history_errors: dict[tuple[str, str], FundingDataError] = {}
        with ThreadPoolExecutor(max_workers=min(12, len(stale_history) or 1)) as executor:
            future_histories = {
                executor.submit(
                    clients[venue].funding_history,
                    symbol,
                    int(history_request_starts[(venue, symbol)].timestamp() * 1000),
                    float(market["funding_interval_hours"]),
                    observed_at,
                ): (venue, symbol)
                for (venue, symbol), market in stale_history.items()
            }
            for future in as_completed(future_histories):
                key = future_histories[future]
                try:
                    fresh_history[key] = future.result()
                except FundingDataError as exc:
                    history_errors[key] = exc
        for key, rows in fresh_history.items():
            counts["history_row_count"] += store.upsert_funding_history(rows)
            store.record_funding_history_sync(
                key[0],
                key[1],
                history_request_starts[key].isoformat(),
                observed_at,
                rows,
            )
        for (venue, symbol), exc in sorted(history_errors.items()):
            warnings.append(f"{venue} {symbol} history skipped: {exc}")
        for venue, symbol in candidate_markets:
            history[(venue, symbol)] = store.funding_history_rows(
                venue,
                symbol,
                since=history_start.isoformat(),
            )
        book_sequences = store.funding_orderbook_sequences(
            list(candidate_markets),
            limit_per_market=settings.liquidity_sequence_limit,
            before_at=observed_at,
        )
        for key, book in books.items():
            market = candidate_markets[key]
            book["_history"] = normalize_stored_orderbook_units(
                book_sequences.get(key, []),
                float(market.get("canonical_unit_multiplier") or 1.0),
                book.get("mid_price"),
            )
        counts["orderbook_count"] = store.insert_funding_orderbooks(
            scan_id,
            fresh_books,
            include_raw_json=settings.store_diagnostic_raw_json,
        )
        counts["orderbook_count"] = len(books)
        routes = scan_ranked_pairs(
            candidates,
            books,
            history,
            observed_at,
            settings,
        )
        stored_routes = store.insert_funding_routes(scan_id, routes)
        paper_rows = simulate_paper_revalidations(
            previous_routes,
            stored_routes,
            settings.paper_latency_ms,
        )
        counts["paper_execution_count"] = store.insert_funding_paper_executions(
            scan_id,
            paper_rows,
        )
        counts["route_count"] = len(routes)
        counts["paper_candidate_count"] = sum(
            row["status"] == "paper_candidate" for row in routes
        )
        store.insert_funding_scan_warnings(scan_id, warnings)
        store.finish_funding_scan(scan_id, "success", **counts)
        if normalized_scan_mode in RETENTION_SCAN_MODES:
            apply_funding_retention_plan(
                store,
                keep_latest_scans=FUNDING_SCAN_RETENTION,
                keep_latest_history_per_market=FUNDING_HISTORY_RETENTION_PER_MARKET,
            )
        return {
            "funding_scan_id": scan_id,
            "status": "success",
            **counts,
            "overlapping_asset_count": len(
                {str(row["canonical_asset"]) for row in universe_candidates}
            ),
            "route_pair_count": universe_route_count,
            "universe_route_count": universe_route_count,
            "execution_shortlist_count": len(candidates),
            "fresh_orderbook_count": len(fresh_books),
            "cached_orderbook_count": len(cached_books),
            "fresh_market_venue_count": len(clients_to_fetch),
            "cached_market_venue_count": len(cached_catalog_results),
            "pre_execution_reject_count": max(
                0,
                universe_route_count - len(candidates),
            ),
            "available_venues": sorted(available_venues),
            "warnings": warnings,
            "execution_mode": "paper_only",
            "scan_mode": normalized_scan_mode,
            "history_hydration_enabled": bool(hydrate_missing_history),
        }
    except Exception as exc:
        store.insert_funding_scan_warnings(scan_id, warnings)
        store.finish_funding_scan(scan_id, "failed", error=str(exc), **counts)
        raise


def executable_history_market_keys(
    candidates: list[dict[str, Any]],
    books: dict[tuple[str, str], dict[str, Any]],
    minimum_capacity: float,
) -> set[tuple[str, str]]:
    """Select markets in routes that can clear minimum notional on all four sides."""
    required: set[tuple[str, str]] = set()
    minimum = max(0.0, float(minimum_capacity))
    for candidate in candidates:
        keys = [
            (str(market["venue"]), str(market["symbol"]))
            for market in (candidate["long_market"], candidate["short_market"])
        ]
        route_books = [books.get(key) for key in keys]
        if not all(
            book
            and float(book.get("bid_depth_usd") or 0.0) >= minimum
            and float(book.get("ask_depth_usd") or 0.0) >= minimum
            for book in route_books
        ):
            continue
        required.update(keys)
    return required


def cached_catalog_is_safe(
    markets: list[dict[str, Any]],
    observed_at: datetime,
    settlement_buffer_seconds: int = 60,
) -> bool:
    if not markets:
        return False
    cache_boundary = observed_at.astimezone(UTC) + timedelta(
        seconds=max(0, int(settlement_buffer_seconds))
    )
    for market in markets:
        next_funding_at = market.get("next_funding_at")
        if not next_funding_at:
            continue
        try:
            funding_time = datetime.fromisoformat(
                str(next_funding_at).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if funding_time.tzinfo is None:
            funding_time = funding_time.replace(tzinfo=UTC)
        if funding_time.astimezone(UTC) <= cache_boundary:
            return False
    return True


def client_history_request_start_at(
    client: FundingVenueClient,
    requested_start: datetime,
    observed_at: datetime,
) -> str:
    lookback = getattr(client, "incremental_history_lookback_hours", None)
    if lookback is None:
        return requested_start.isoformat()
    try:
        effective = observed_at - timedelta(hours=max(0.25, float(lookback)))
    except (TypeError, ValueError):
        return requested_start.isoformat()
    return max(requested_start, effective).isoformat()


def watch_funding_markets(
    store: SQLiteStore,
    interval_seconds: int = 60,
    iterations: int | None = None,
    config: FundingScanConfig | None = None,
) -> None:
    completed = 0
    while iterations is None or completed < iterations:
        result = run_funding_scan(store, config=config, scan_mode="watch")
        completed += 1
        print(
            f"Funding scan {result['funding_scan_id']}: "
            f"candidates={result['paper_candidate_count']} routes={result['route_count']}"
        )
        if iterations is not None and completed >= iterations:
            return
        time.sleep(max(5, interval_seconds))


def backfill_funding_history(
    store: SQLiteStore,
    days: int = 90,
    limit: int = 200,
    venue_clients: list[FundingVenueClient] | None = None,
    target_venues: set[str] | None = None,
) -> dict[str, Any]:
    store.init_db()
    observed_at = utc_now_iso()
    observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    history_start = observed.astimezone(UTC) - timedelta(days=max(30, min(days, 90)))
    history_start_ms = int(history_start.timestamp() * 1000)
    clients_list = filter_deactivated_funding_clients(
        list(venue_clients) if venue_clients is not None else active_default_funding_clients()
    )
    clients = {str(client.venue): client for client in clients_list}
    catalog_results: list[tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]] = []
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, len(clients))) as executor:
        futures = {
            executor.submit(client.catalog_and_markets, observed_at): venue
            for venue, client in clients.items()
        }
        for future in as_completed(futures):
            venue = futures[future]
            try:
                catalog_results.append(future.result())
            except FundingDataError as exc:
                warnings.append(f"{venue} catalog skipped: {exc}")

    instruments = [row for result in catalog_results for row in result[0]]
    markets = [row for result in catalog_results for row in result[1]]
    warnings.extend(warning for result in catalog_results for warning in result[2])
    instruments, markets, removed_non_crypto = remove_non_crypto_funding_rows(
        instruments,
        markets,
        explicit_assets=client_non_crypto_assets(clients),
    )
    if removed_non_crypto:
        warnings.append(non_crypto_filter_warning(removed_non_crypto))
    store.upsert_funding_instruments(instruments)
    venue_count_by_asset: dict[str, set[str]] = {}
    for market in markets:
        venue_count_by_asset.setdefault(str(market["canonical_asset"]), set()).add(
            str(market["venue"])
        )
    overlapping_assets = {
        asset for asset, venues in venue_count_by_asset.items() if len(venues) >= 2
    }
    eligible = [
        market
        for market in markets
        if str(market["canonical_asset"]) in overlapping_assets
        and (
            not target_venues
            or str(market["venue"]) in target_venues
        )
    ]
    pending = [
        market
        for market in eligible
        if not store.funding_history_is_fresh(
            str(market["venue"]),
            str(market["symbol"]),
            24.0 * 365.0,
            required_start_at=history_start.isoformat(),
        )
    ]
    pending = round_robin_by_venue(pending)
    selected = pending if limit <= 0 else pending[: max(1, int(limit))]
    histories: dict[tuple[str, str], list[dict[str, Any]]] = {}
    failures: dict[tuple[str, str], str] = {}
    with ThreadPoolExecutor(max_workers=min(12, len(selected) or 1)) as executor:
        futures = {
            executor.submit(
                clients[str(market["venue"])].funding_history,
                str(market["symbol"]),
                history_start_ms,
                float(market["funding_interval_hours"]),
                observed_at,
            ): (str(market["venue"]), str(market["symbol"]))
            for market in selected
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                histories[key] = future.result()
            except FundingDataError as exc:
                failures[key] = str(exc)

    imported_rows = 0
    for key, rows in histories.items():
        imported_rows += store.upsert_funding_history(rows)
        store.record_funding_history_sync(
            key[0],
            key[1],
            client_history_request_start_at(
                clients[key[0]],
                history_start,
                observed,
            ),
            observed_at,
            rows,
        )
    retention = apply_funding_retention_plan(
        store,
        keep_latest_scans=FUNDING_SCAN_RETENTION,
        keep_latest_history_per_market=FUNDING_HISTORY_RETENTION_PER_MARKET,
    )
    warnings.extend(
        f"{venue} {symbol} history skipped: {error}"
        for (venue, symbol), error in sorted(failures.items())
    )
    return {
        "status": "success",
        "history_days": max(30, min(days, 90)),
        "target_venues": sorted(target_venues or []),
        "overlapping_asset_count": len(overlapping_assets),
        "eligible_market_count": len(eligible),
        "pending_before_count": len(pending),
        "attempted_market_count": len(selected),
        "synced_market_count": len(histories),
        "remaining_market_count": max(0, len(pending) - len(histories)),
        "imported_row_count": imported_rows,
        "retention": retention,
        "warnings": warnings,
    }


def round_robin_by_venue(
    markets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for market in markets:
        grouped.setdefault(str(market["venue"]), []).append(market)
    for rows in grouped.values():
        rows.sort(
            key=lambda row: (
                -market_volume(row),
                str(row["canonical_asset"]),
                str(row["symbol"]),
            )
        )
    output: list[dict[str, Any]] = []
    venues = sorted(grouped)
    while any(grouped.values()):
        for venue in venues:
            rows = grouped[venue]
            if rows:
                output.append(rows.pop(0))
    return output


def market_volume(market: dict[str, Any]) -> float:
    try:
        return max(0.0, float(market.get("volume_24h_usd") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def remove_non_crypto_funding_rows(
    instruments: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    explicit_assets: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Drop assets that any venue explicitly marks as stock/RWA/non-crypto.

    Some venues expose stock or RWA perpetuals through the same public perp APIs as
    crypto contracts. Cross-venue funding comparisons must stay crypto-only:
    otherwise a zero or equity-style funding estimate can create fake spreads.
    """
    non_crypto_assets = funding_non_crypto_assets(instruments, markets)
    non_crypto_assets.update(
        asset
        for asset in (
            canonical_asset_symbol(value)
            for value in (explicit_assets or set())
        )
        if asset
    )
    if not non_crypto_assets:
        return instruments, markets, set()
    filtered_instruments = [
        row
        for row in instruments
        if canonical_asset_symbol(row.get("canonical_asset")) not in non_crypto_assets
    ]
    filtered_markets = [
        row
        for row in markets
        if canonical_asset_symbol(row.get("canonical_asset")) not in non_crypto_assets
    ]
    return filtered_instruments, filtered_markets, non_crypto_assets


def funding_non_crypto_assets(
    instruments: list[dict[str, Any]],
    markets: list[dict[str, Any]],
) -> set[str]:
    assets: set[str] = set()
    for row in [*instruments, *markets]:
        if not funding_row_is_non_crypto(row):
            continue
        asset = canonical_asset_symbol(row.get("canonical_asset"))
        if asset:
            assets.add(asset)
    return assets


def client_non_crypto_assets(
    clients: dict[str, FundingVenueClient],
) -> set[str]:
    assets: set[str] = set()
    for client in clients.values():
        for value in getattr(client, "non_crypto_assets", set()) or set():
            asset = canonical_asset_symbol(value)
            if asset:
                assets.add(asset)
    return assets


def funding_row_is_non_crypto(row: dict[str, Any]) -> bool:
    raw = row.get("raw")
    candidates = [raw] if isinstance(raw, dict) else []
    if isinstance(raw, dict):
        for key in ("instrument", "market", "contract", "spec", "detail"):
            nested = raw.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
    for item in candidates:
        if field_is_non_crypto(item.get("symbolType")):
            return True
        if field_is_non_crypto(item.get("asset_class")):
            return True
        if field_is_non_crypto(item.get("marketType")):
            return True
        if str(item.get("instCategory") or "").strip() == "3":
            return True
        if str(item.get("isRwa") or "").strip().lower() in {"true", "1", "yes"}:
            return True
        if any(field_is_non_crypto(value) for value in item.get("tags") or []):
            return True
        if any(field_is_non_crypto(value) for value in item.get("underlyingSubType") or []):
            return True
    return False


def field_is_non_crypto(value: Any) -> bool:
    normalized = str(value or "").strip().lower().replace("_", "-")
    return normalized in {
        "stock",
        "stocks",
        "equity",
        "equities",
        "rwa",
        "real-world-asset",
        "real-world-assets",
        "forex",
        "fx",
        "commodity",
        "commodities",
        "index",
        "indices",
        "etf",
        "etfs",
    }


def non_crypto_filter_warning(assets: set[str]) -> str:
    examples = ", ".join(sorted(assets)[:12])
    suffix = "" if len(assets) <= 12 else ", ..."
    return (
        "Filtered "
        f"{len(assets)} non-crypto funding assets from scan/backfill "
        f"({examples}{suffix})."
    )
