from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from smart_money_radar.funding.readiness_policy import (
    EvaluationMode,
    evaluate_synchronized_route,
    modeled_fee_rate,
)
from smart_money_radar.funding.route_identity import (
    FocusedSelectionConfig,
    best_route_variant,
    route_family_key,
    route_variant_rank_sort_key,
    select_focused_routes,
)
from smart_money_radar.funding.trader import PaperBot, PaperBotConfig, route_summary
from smart_money_radar.paper_bot.clock import FakeClock
from smart_money_radar.storage import SQLiteStore


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
OBSERVED_AT = NOW.isoformat()
SETTLEMENT_AT = (NOW + timedelta(seconds=60)).isoformat()


def _fee_evidence(venue: str, *, rate: float = 0.0005) -> dict[str, Any]:
    return {
        "source_kind": "official_public_fee_endpoint",
        "source_identifier": f"test:{venue}:taker:{rate}",
        "trust_status": "OFFICIAL",
        "venue": venue,
        "liquidity_role": "taker",
        "observed_at": OBSERVED_AT,
        "reviewed_at": OBSERVED_AT,
        "environment": "mainnet",
        "market_type": "linear_perpetual",
        "product_type": "linear_perpetual",
        "applicability": "taker",
        "evidence_version": "test-fee-v1",
    }


def _market(
    venue: str,
    rate: float,
    *,
    asset: str = "BTC",
    symbol: str | None = None,
    quote: str = "USDT",
    collateral: str | None = None,
    environment: str = "mainnet",
    product_id: str | None = None,
    next_funding_at: str = SETTLEMENT_AT,
    taker_fee_rate: float | None = 0.0005,
    verified_fee: bool = True,
    include_orderbook: bool = True,
    mark_price: float = 100.0,
    kind: str = "published_next_estimate",
) -> dict[str, Any]:
    collateral_asset = collateral or quote
    market = {
        "venue": venue,
        "environment": environment,
        "environment_verified": True,
        "endpoint_base_url": f"https://api.{venue}.test",
        "endpoint_identity_provenance": f"test:{venue}:endpoint",
        "endpoint_client_version": "test-client",
        "endpoint_verified_at": OBSERVED_AT,
        "api_product_type": "linear_perpetual",
        "market_type": "linear_perpetual",
        "product_type": "linear_perpetual",
        "product_id": product_id or symbol or f"{asset}-{quote}-PERP",
        "symbol": symbol or f"{asset}{quote}",
        "canonical_asset": asset,
        "base_asset": asset,
        "quote_asset": quote,
        "collateral_asset": collateral_asset,
        "contract_type": "linear_perpetual",
        "contract_kind": "linear_perpetual",
        "contract_multiplier": 1.0,
        "canonical_unit_multiplier": 1.0,
        "supports_perpetuals": True,
        "is_linear_contract": True,
        "supports_discrete_funding": True,
        "data_enabled": True,
        "strategy_observation_enabled": True,
        "shadow_candidate_enabled": True,
        "execution_model": "CLOB",
        "settlement_verification_level": "adapter_contract",
        "funding_rate": rate,
        "normalized_next_funding_rate": rate,
        "funding_rate_kind": kind,
        "funding_rate_semantics": "next_settlement",
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "funding_interval_hours": 1.0,
        "hourly_funding_rate": rate,
        "next_funding_at": next_funding_at,
        "mark_price": mark_price,
        "index_price": mark_price,
        "oracle_price": mark_price,
        "quantity_step": 0.001,
        "min_quantity": 0.001,
        "min_notional_usd": 5.0,
        "volume_24h_usd": 1_000_000.0,
        "open_interest_usd": 1_000_000.0,
        "paper_enabled": True,
        "live_enabled": False,
        "position_inclusion_rule": "perp_position_at_settlement",
        "entry_safety_buffer_seconds": 2.0,
        "exit_safety_buffer_seconds": 2.0,
        "timing_policy_source": f"adapter_{venue}_test",
        "observed_at": OBSERVED_AT,
        "response_received_at": OBSERVED_AT,
        "source_event_at": OBSERVED_AT,
        "server_time": OBSERVED_AT,
        "venue_server_time": OBSERVED_AT,
    }
    if taker_fee_rate is not None:
        market["taker_fee_rate"] = taker_fee_rate
        if verified_fee:
            market["fee_source"] = "official_public_fee_endpoint"
            market["fee_evidence"] = _fee_evidence(venue, rate=taker_fee_rate)
            market["fee_observed_at"] = OBSERVED_AT
            market["fee_reviewed_at"] = OBSERVED_AT
    if include_orderbook:
        market.update(
            {
                "bids": [[mark_price - 0.01, 100.0]],
                "asks": [[mark_price + 0.01, 100.0]],
                "best_bid": mark_price - 0.01,
                "best_ask": mark_price + 0.01,
                "orderbook_depth_available": True,
            }
        )
    return market


def _route(
    index: int,
    *,
    verified: bool = False,
    experimental: bool = True,
    lead_seconds: float = 60.0,
    net: float = 1.0,
    funding: float | None = None,
    long_venue: str = "binance",
    short_venue: str = "bybit",
) -> dict[str, Any]:
    key = f"variant-{index:03d}"
    family = route_family_key("BTC", long_venue, short_venue)
    settlement = NOW + timedelta(seconds=lead_seconds)
    return {
        "route_key": key,
        "route_variant_key": key,
        "route_family_key": family,
        "canonical_asset": "BTC",
        "long_venue": long_venue,
        "long_symbol": f"BTCUSDT-{index}",
        "short_venue": short_venue,
        "short_symbol": f"BTCUSDT-{index}",
        "next_funding_at": settlement.isoformat(),
        "observed_at": (NOW + timedelta(seconds=index)).isoformat(),
        "risk_flags": [] if verified else ["estimated_rate_used"],
        "legs": [
            {
                "side": "long",
                "venue": long_venue,
                "symbol": f"BTCUSDT-{index}",
                "next_funding_at": settlement.isoformat(),
            },
            {
                "side": "short",
                "venue": short_venue,
                "symbol": f"BTCUSDT-{index}",
                "next_funding_at": settlement.isoformat(),
            },
        ],
        "evidence": {
            "route_family_key": family,
            "route_variant_key": key,
            "verified_paper_ready": verified,
            "experimental_simulation_ready": experimental,
            "experimental_paper_ready": experimental,
            "paper_mode": "VERIFIED_PAPER" if verified else "PAPER",
            "readiness_policy": {
                "hard_blockers": [],
                "verified_paper_ready": verified,
                "experimental_simulation_ready": experimental,
                "experimental_paper_ready": experimental,
                "rate_confidence": 0.95 if verified else 0.45,
                "execution_confidence": 0.95 if verified else 0.35,
                "settlement_confidence": 0.95 if verified else 0.40,
                "economics": {
                    "conservative_expected_net_usd": net,
                    "conservative_expected_funding_usd": funding if funding is not None else net + 1.0,
                },
            },
            "rate_confidence": 0.95 if verified else 0.45,
            "execution_confidence": 0.95 if verified else 0.35,
            "settlement_confidence": 0.95 if verified else 0.40,
            "conservative_expected_net": net,
            "conservative_expected_funding": funding if funding is not None else net + 1.0,
            "current_nowcast_net": net,
            "current_nowcast_gross": funding if funding is not None else net + 1.0,
        },
    }


def test_route_identity_and_route_dedup_keeps_exact_variants_deterministic(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    bot = PaperBot(
        store,
        PaperBotConfig(telegram_enabled=False, target_notional_per_leg=500.0).validated(),
        clock=FakeClock(NOW, monotonic_start=100.0),
    )
    variants = [
        (
            _market("binance", -0.00045, symbol="BTCUSDT", quote="USDT", product_id="BTC-USDT-PERP"),
            _market("bybit", 0.00045, symbol="BTCUSDT", quote="USDT", product_id="BTC-USDT-PERP"),
        ),
        (
            _market("binance", -0.00080, symbol="BTCPERPUSDC", quote="USDC", product_id="BTC-USDC-PERP"),
            _market("bybit", 0.00080, symbol="BTCPERPUSDC", quote="USDC", product_id="BTC-USDC-PERP"),
        ),
        (
            _market(
                "binance",
                -0.00055,
                symbol="BTCUSDT-TEST",
                quote="USDT",
                environment="testnet",
                product_id="BTC-USDT-TEST-PERP",
            ),
            _market(
                "bybit",
                0.00055,
                symbol="BTCUSDT-TEST",
                quote="USDT",
                environment="testnet",
                product_id="BTC-USDT-TEST-PERP",
            ),
        ),
    ]
    duplicate_exact_long = _market(
        "binance",
        -0.00040,
        symbol="BTCUSDT",
        quote="USDT",
        product_id="BTC-USDT-PERP",
    )
    markets = [row for pair in variants for row in pair] + [duplicate_exact_long]

    routes_a, summary_a = bot._build_lightweight_watch_routes(markets, {"binance": True, "bybit": True}, NOW)
    routes_b, summary_b = bot._build_lightweight_watch_routes(list(reversed(markets)), {"binance": True, "bybit": True}, NOW)

    assert summary_a["routes_detected"] >= 3
    assert summary_a["funnel"]["unique_route_variants"]["count"] == len(routes_a)
    assert summary_a["funnel"]["unique_route_families"]["count"] == 1
    assert summary_a["unique_route_variants_soft_flagged"] >= 0
    assert sorted(route["route_key"] for route in routes_a) == sorted(route["route_key"] for route in routes_b)
    assert {route["route_family_key"] for route in routes_a} == {
        route_family_key("BTC", "binance", "bybit")
    }
    assert len({route["route_variant_key"] for route in routes_a}) == len(routes_a)
    assert all(route["route_key"] == route["route_variant_key"] for route in routes_a)
    assert all(route["legacy_route_key"] == route["route_family_key"] for route in routes_a)
    assert any(
        route["long_symbol"] == "BTCUSDT-TEST"
        and route["evidence"]["route_identity"]["long"]["environment"] == "testnet"
        for route in routes_a
    )
    best_a = best_route_variant(routes_a)
    best_b = best_route_variant(routes_b)
    assert best_a is not None
    assert best_b is not None
    assert best_a["route_key"] == best_b["route_key"]
    assert best_a["long_symbol"] == "BTCPERPUSDC"
    assert best_a["short_symbol"] == "BTCPERPUSDC"
    assert route_variant_rank_sort_key(best_a) == route_variant_rank_sort_key(best_b)


def test_focused_capacity_100_watch_routes_stays_visible_without_deferral() -> None:
    routes = [
        _route(index, verified=index < 5, experimental=True, net=20.0 - index * 0.1)
        for index in range(100)
    ]
    selection = select_focused_routes(
        routes,
        now=NOW,
        config=FocusedSelectionConfig(
            max_focused_routes=12,
            max_experimental_focused_routes=7,
            max_focused_routes_per_venue=100,
        ),
    )

    assert len(selection["routes"]) == 100
    assert len(selection["focused_routes"]) == 100
    assert len(selection["deferred_routes"]) == 0
    selected_keys = set(selection["selected_route_keys"])
    assert {f"variant-{index:03d}" for index in range(5)} <= selected_keys
    assert all(
        route["evidence"]["focused_selection"]["state"] == "selected"
        for route in selection["focused_routes"]
    )
    assert all(
        "focused_capacity_deferred" not in route["risk_flags"]
        for route in selection["routes"]
    )
    assert len(selection["focused_routes"]) == len(routes)
    assert "focused_capacity_soft_limit_exceeded" in selection["capacity_warnings"]
    assert selection["config"]["capacity_limits_are_advisory"] is True
    assert all(
        "focused_capacity_soft_limit_exceeded"
        in route["evidence"]["focused_selection"]["capacity_warnings"]
        for route in selection["focused_routes"]
    )

    open_route = _route(500, verified=True, experimental=True, net=-100.0)
    open_selection = select_focused_routes(
        [open_route, *routes[:5]],
        now=NOW,
        config=FocusedSelectionConfig(max_focused_routes=1, max_experimental_focused_routes=1),
        open_position_route_keys={open_route["route_key"]},
    )
    assert open_selection["selected_route_keys"][0] == open_route["route_key"]
    assert len(open_selection["focused_routes"]) == 6
    assert all(
        "focused_capacity_deferred" not in route["risk_flags"]
        for route in open_selection["focused_routes"]
    )


def test_focused_shortlist_verified_route_preempts_sticky_experimental() -> None:
    experimental = [_route(index, verified=False, experimental=True, net=10 - index) for index in range(3)]
    config = FocusedSelectionConfig(
        max_focused_routes=3,
        max_experimental_focused_routes=3,
        focused_selection_min_ttl_seconds=60.0,
        focused_selection_hysteresis=3,
    )
    first = select_focused_routes(experimental, now=NOW, config=config, now_monotonic=100.0)
    previous_selected = {key: 100.0 for key in first["selected_route_keys"]}
    verified = _route(99, verified=True, experimental=True, lead_seconds=30.0, net=1.0)

    second = select_focused_routes(
        [*experimental, verified],
        now=NOW,
        config=config,
        previous_selected_at=previous_selected,
        now_monotonic=110.0,
    )

    assert "variant-099" in second["selected_route_keys"]
    assert second["selected_route_keys"][0] == "variant-099"
    assert len(second["focused_routes"]) == 4
    assert second["deferred_route_keys"] == []
    assert any(route["route_key"] == "variant-099" for route in second["focused_routes"])
    assert all(route["focused_state"] == "selected" for route in second["routes"])


def test_focused_capacity_venue_concentration_is_advisory_not_blocking() -> None:
    concentrated = [
        _route(index, long_venue="binance", short_venue=f"venue{index}", net=100 - index)
        for index in range(30)
    ]
    diversified = [
        _route(
            100 + index,
            long_venue=f"long{index}",
            short_venue=f"short{index}",
            net=50 - index,
        )
        for index in range(30)
    ]
    selection = select_focused_routes(
        [*concentrated, *diversified],
        now=NOW,
        config=FocusedSelectionConfig(
            max_focused_routes=10,
            max_experimental_focused_routes=10,
            max_focused_routes_per_venue=3,
        ),
    )

    assert len(selection["focused_routes"]) == 60
    assert len(selection["deferred_routes"]) == 0
    assert "focused_per_venue_soft_limit_exceeded" in selection["capacity_warnings"]
    assert sum(1 for route in selection["focused_routes"] if route["long_venue"] == "binance") == 30
    assert any(route["long_venue"] != "binance" for route in selection["focused_routes"])


def test_open_position_plus_100_watch_routes_scheduler_keeps_p0_first(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    bot = PaperBot(
        store,
        PaperBotConfig(telegram_enabled=False, max_focused_routes=8).validated(),
        clock=FakeClock(NOW, monotonic_start=100.0),
    )
    monkeypatch.setattr(
        bot,
        "_with_focused_selection_capability",
        lambda route, _clients_by_venue: route,
    )
    bot.discovered_routes = {route["route_key"]: route for route in (_route(index) for index in range(100))}
    bot.apply_focused_selection(NOW)
    assert len(bot.discovered_routes) == 100
    assert len(bot.hot_routes) == 100
    assert all(
        "focused_capacity_deferred" not in route["risk_flags"]
        for route in bot.hot_routes.values()
    )
    calls: list[str] = []
    monkeypatch.setattr(bot, "has_open_exposure", lambda: True)
    monkeypatch.setattr(bot, "run_open_position_iteration", lambda: calls.append("open_poll") or {"mode": "open_positions", "open_position_count": 1})
    monkeypatch.setattr(bot, "_maybe_run_reconciliation", lambda: calls.append("reconciliation") or {"status": "checked"})
    monkeypatch.setattr(bot.synchronized_runtime, "recover_runtime_state", lambda now: calls.append("recovery") or {"status": "checked"})
    monkeypatch.setattr(bot, "_run_lightweight_discovery", lambda: (_ for _ in ()).throw(AssertionError("discovery must not run before open polling")))
    monkeypatch.setattr(bot, "run_hot_iteration", lambda: (_ for _ in ()).throw(AssertionError("focused refresh must not run before open polling")))

    result = bot.run_iteration()

    assert calls[:1] == ["open_poll"]
    assert calls == ["open_poll", "recovery", "reconciliation"]
    assert result["mode"] == "open_positions"
    assert result["reconciliation"]["status"] == "checked"


def test_fee_fallback_policy_verified_unverified_missing_and_invalid() -> None:
    verified_low = modeled_fee_rate(_market("binance", -0.0001, taker_fee_rate=0.0005), now=NOW)
    verified_high = modeled_fee_rate(_market("binance", -0.0001, taker_fee_rate=0.0012), now=NOW)
    unverified_low = modeled_fee_rate(
        _market("unknownvenue", -0.0001, taker_fee_rate=0.0005, verified_fee=False),
        now=NOW,
    )
    missing = modeled_fee_rate(_market("unknownvenue", -0.0001, taker_fee_rate=None), now=NOW)
    maker_rebate = _market("binance", -0.0001, taker_fee_rate=None)
    maker_rebate["maker_fee_rate"] = -0.0002
    maker_only = modeled_fee_rate(maker_rebate, now=NOW)
    invalid = modeled_fee_rate(_market("binance", -0.0001, taker_fee_rate=0.5), now=NOW)

    assert verified_low["verified"] is True
    assert verified_low["fee_rate"] == pytest.approx(0.0005)
    assert verified_low["uncertainty_reserve_bps"] == 0.0
    assert verified_high["fee_rate"] == pytest.approx(0.0012)
    assert unverified_low["verified"] is False
    assert unverified_low["fee_rate"] >= 0.0012
    assert "fee_fallback_used" in unverified_low["risk_flags"]
    assert missing["fee_rate"] >= 0.0012
    assert "account_fee_unknown" in missing["risk_flags"]
    assert maker_only["fee_rate"] >= 0.0012
    assert maker_only["source_rate"] is None
    assert invalid["verified"] is False
    assert invalid["source_rate"] is None
    assert invalid["fee_rate"] >= 0.0012


def _evaluate(
    long_rate: float,
    short_rate: float,
    *,
    long_kind: str = "published_next_estimate",
    short_kind: str = "published_next_estimate",
    fee: float | None = 0.0005,
    verified_fee: bool = True,
    orderbook: bool = True,
    mode: EvaluationMode = EvaluationMode.DISCOVERY,
) -> dict[str, Any]:
    long = _market(
        "binance",
        long_rate,
        taker_fee_rate=fee,
        verified_fee=verified_fee,
        include_orderbook=orderbook,
        kind=long_kind,
    )
    short = _market(
        "bybit",
        short_rate,
        taker_fee_rate=fee,
        verified_fee=verified_fee,
        include_orderbook=orderbook,
        kind=short_kind,
    )
    if long_kind != "published_next_estimate":
        long.pop("normalized_next_funding_rate", None)
    if short_kind != "published_next_estimate":
        short.pop("normalized_next_funding_rate", None)
    return evaluate_synchronized_route(
        long_market=long,
        short_market=short,
        target_notional=500.0,
        mode=mode,
        clients_by_venue={"binance": True, "bybit": True},
    )


def test_realistic_economics_boundary_cases() -> None:
    raw_positive_conservative_negative = _evaluate(-0.0012, 0.0012)
    zero_fee_trap = _evaluate(-0.0004, 0.0004, fee=None)
    verified_fee_pass = _evaluate(-0.0020, 0.0020, fee=0.0005, verified_fee=True)
    fallback_fee_blocks = _evaluate(-0.0020, 0.0020, fee=0.0005, verified_fee=False)
    current_crosses_zero = _evaluate(
        -0.00005,
        0.00005,
        long_kind="published_current_fallback",
        short_kind="published_current_fallback",
    )
    estimated_worst_direction_negative = _evaluate(
        -0.00020,
        0.00010,
        long_kind="published_current_fallback",
        short_kind="published_current_fallback",
    )
    exact_full_execution = _evaluate(-0.0030, 0.0030, mode=EvaluationMode.VERIFIED_PAPER)
    current_synthetic_fill = _evaluate(
        -0.0020,
        0.0020,
        long_kind="published_current_fallback",
        short_kind="published_current_fallback",
        orderbook=False,
    )
    few_bps_boundary = _evaluate(-0.0009, 0.0009)

    econ = raw_positive_conservative_negative["economics"]
    assert econ["raw_expected_funding_usd"] > 0
    assert econ["raw_expected_net_usd"] > 0
    assert econ["conservative_expected_costs_usd"] > econ["raw_expected_costs_usd"]
    assert econ["conservative_expected_net_usd"] < 0
    assert raw_positive_conservative_negative["verified_paper_ready"] is False

    zero_econ = zero_fee_trap["economics"]
    assert zero_econ["raw_expected_net_usd"] <= 0
    assert zero_econ["conservative_expected_net_usd"] < 0
    assert "long_fee_fallback_used" in zero_fee_trap["risk_flags"]

    assert verified_fee_pass["economics"]["conservative_expected_net_usd"] > 0
    assert verified_fee_pass["experimental_simulation_ready"] is True
    assert fallback_fee_blocks["economics"]["conservative_expected_net_usd"] < 0
    assert "long_fee_unverified" in fallback_fee_blocks["risk_flags"]

    assert current_crosses_zero["economics"]["raw_expected_funding_usd"] > 0
    assert current_crosses_zero["economics"]["conservative_expected_funding_usd"] < 0
    assert "long_estimated_rate_used" in current_crosses_zero["risk_flags"]
    assert estimated_worst_direction_negative["economics"]["conservative_expected_funding_usd"] < 0

    assert exact_full_execution["verified_paper_ready"] is True
    assert exact_full_execution["verified_paper_blockers"] == []
    assert exact_full_execution["economics"]["raw_expected_costs_usd"] == pytest.approx(1.0)
    assert exact_full_execution["economics"]["conservative_expected_costs_usd"] > 1.0

    assert current_synthetic_fill["economically_observable"] is True
    assert current_synthetic_fill["experimental_simulation_ready"] is False
    assert current_synthetic_fill["verified_paper_ready"] is False
    assert "synthetic_fill" in current_synthetic_fill["risk_flags"]
    assert "verified_ioc_execution_evidence_missing" in current_synthetic_fill["verified_paper_blockers"]

    assert few_bps_boundary["economics"]["raw_expected_funding_usd"] == pytest.approx(0.9)
    assert few_bps_boundary["economics"]["raw_expected_costs_usd"] == pytest.approx(1.0)
    assert few_bps_boundary["economics"]["raw_expected_net_usd"] < 0
