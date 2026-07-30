from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from smart_money_radar.funding.readiness_policy import (
    EvaluationMode,
    RateEstimateKind,
    evaluate_synchronized_route,
    rate_estimate_from_market,
)
from smart_money_radar.funding.strategy_synchronized_funding import (
    FundingSettlementPlanner,
)
from smart_money_radar.funding.synchronized_market_contract import (
    normalize_synchronized_capture_market,
)


OBSERVED_AT = "2026-07-30T12:00:00+00:00"
SETTLEMENT_AT = (
    datetime.fromisoformat(OBSERVED_AT) + timedelta(minutes=30)
).isoformat()


def _fee_evidence(venue: str) -> dict[str, Any]:
    return {
        "source_kind": "official_public_fee_endpoint",
        "source_identifier": f"test:{venue}:fee",
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
    environment: str = "mainnet",
    kind: str = "published_next_estimate",
    mark_price: float | None = 100.0,
    index_price: float | None = 100.0,
    oracle_price: float | None = None,
    collateral: str = "USDT",
    fee: bool = True,
    orderbook: bool = True,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "venue": venue,
        "environment": environment,
        "environment_verified": True,
        "endpoint_base_url": f"https://api.{venue}.test",
        "endpoint_identity_provenance": "test-fixture",
        "endpoint_client_version": "test-client",
        "endpoint_verified_at": OBSERVED_AT,
        "api_product_type": "linear_perpetual",
        "market_type": "linear_perpetual",
        "product_type": "linear_perpetual",
        "symbol": symbol or ("BTCUSDT" if venue != "hyperliquid" else "BTC"),
        "canonical_asset": asset,
        "base_asset": asset,
        "quote_asset": collateral,
        "collateral_asset": collateral,
        "contract_type": "linear_perpetual",
        "contract_kind": "linear_perpetual",
        "supports_perpetuals": True,
        "supports_discrete_funding": True,
        "contract_multiplier": 1.0,
        "funding_rate": rate,
        "normalized_next_funding_rate": rate,
        "funding_interval_hours": 1.0,
        "published_funding_interval_hours": 1.0,
        "hourly_funding_rate": rate,
        "funding_rate_kind": kind,
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "next_funding_at": SETTLEMENT_AT,
        "mark_price": mark_price,
        "index_price": index_price,
        "oracle_price": oracle_price,
        "quantity_step": 0.001,
        "min_quantity": 0.001,
        "min_notional_usd": 5.0,
        "volume_24h_usd": 1_000_000.0,
        "open_interest_usd": 1_000_000.0,
        "server_time": OBSERVED_AT,
        "orderbook_response_received_at": OBSERVED_AT,
        "orderbook_event_time": OBSERVED_AT,
        "paper_enabled": True,
        "shadow_candidate_enabled": True,
        "live_enabled": False,
        "observed_at": OBSERVED_AT,
        "source_event_at": OBSERVED_AT,
        "response_received_at": OBSERVED_AT,
        "entry_safety_buffer_seconds": 2.0,
        "exit_safety_buffer_seconds": 2.0,
        "timing_policy_source": "test",
    }
    if fee:
        row.update(
            {
                "taker_fee_rate": 0.0005,
                "fee_source": "official_public_fee_endpoint",
                "fee_evidence": _fee_evidence(venue),
                "fee_observed_at": OBSERVED_AT,
                "fee_reviewed_at": OBSERVED_AT,
            }
        )
    if orderbook:
        row.update(
            {
                "bids": [[99.99, 100.0]],
                "asks": [[100.01, 100.0]],
                "best_bid": 99.99,
                "best_ask": 100.01,
                "orderbook_depth_available": True,
            }
        )
    return row


def _route(
    *,
    long: dict[str, Any] | None = None,
    short: dict[str, Any] | None = None,
    mode: EvaluationMode = EvaluationMode.DISCOVERY,
) -> dict[str, Any]:
    long_market = long or _market("binance", -0.006)
    short_market = short or _market("bybit", 0.006)
    return evaluate_synchronized_route(
        long_market=long_market,
        short_market=short_market,
        target_notional=500.0,
        mode=mode,
        clients_by_venue={
            str(long_market.get("venue")).lower(): True,
            str(short_market.get("venue")).lower(): True,
        },
    )


def test_current_rate_estimate_is_observable_with_conservative_bounds() -> None:
    market = _market(
        "hyperliquid",
        0.004,
        kind="published_current_fallback",
    )
    market.pop("normalized_next_funding_rate")

    estimate = rate_estimate_from_market(market)

    assert estimate["rate_estimate_kind"] == RateEstimateKind.CURRENT_RATE_FALLBACK.value
    assert estimate["rate_estimate_per_settlement"] == pytest.approx(0.004)
    assert estimate["exact_next_rate_available"] is False
    assert estimate["rate_estimate_lower_bound"] < 0.004
    assert estimate["rate_estimate_upper_bound"] > 0.004
    assert "exact_next_rate_unavailable" in estimate["rate_estimate_risk_flags"]
    assert estimate["rate_estimate_confidence"] < 0.5


def test_soft_gaps_do_not_block_discovery_but_block_verified_paper() -> None:
    long_market = _market("lighter", -0.006, fee=False, orderbook=False)
    long_market.pop("quantity_step")
    long_market.pop("min_notional_usd")
    long_market.pop("normalized_next_funding_rate")
    long_market["funding_rate_kind"] = "published_current_interval_rate"

    discovery = _route(long=long_market)
    verified = _route(long=long_market, mode=EvaluationMode.VERIFIED_PAPER)

    assert discovery["hard_blockers"] == []
    assert discovery["structurally_eligible"] is True
    assert discovery["economically_observable"] is True
    assert discovery["experimental_paper_ready"] is True
    assert discovery["funding_cashflow_status"] == "ESTIMATED_ONLY"
    for flag in (
        "long_estimated_rate_used",
        "long_fee_fallback_used",
        "long_fee_unverified",
        "long_quantity_step_missing",
        "long_min_notional_missing",
        "long_synthetic_fill_model_required",
    ):
        assert flag in discovery["risk_flags"]
    assert verified["verified_paper_ready"] is False
    for blocker in (
        "long_exact_next_rate_missing",
        "long_quantity_step_missing",
        "long_min_notional_missing",
        "verified_ioc_execution_evidence_missing",
        "long_fee_evidence_unverified",
    ):
        assert blocker in verified["mode_blockers"]


@pytest.mark.parametrize(
    ("label", "mutate", "expected"),
    [
        (
            "unknown_sign",
            lambda market: market.update({"funding_sign_convention": "unclear"}),
            "long_funding_sign_convention_unknown_or_unsupported",
        ),
        (
            "unknown_unit",
            lambda market: market.update({"funding_rate_unit": "unclear"}),
            "long_funding_rate_unit_or_scale_unknown",
        ),
        (
            "different_canonical_asset",
            lambda market: market.update({"canonical_asset": "ETH"}),
            "canonical_asset_mismatch",
        ),
        (
            "mainnet_testnet_mismatch",
            lambda market: market.update({"environment": "testnet"}),
            "environment_mismatch",
        ),
        (
            "deactivated_venue",
            lambda market: market.update({"venue": "bingx"}),
            "long_venue_deactivated",
        ),
        (
            "variational_quarantine",
            lambda market: market.update({"venue": "variational"}),
            "long_venue_deactivated",
        ),
        (
            "paradex_continuous",
            lambda market: market.update({"venue": "paradex"}),
            "long_funding_continuous_pro_rata",
        ),
    ],
)
def test_hard_blockers_remain_hard(label: str, mutate: Any, expected: str) -> None:
    long_market = _market("binance", -0.006)
    mutate(long_market)

    result = _route(long=long_market)

    assert expected in result["hard_blockers"], label
    assert result["readiness_level"] == "structurally_blocked"
    assert result["experimental_paper_ready"] is False


def test_pacifica_adapter_timing_survives_registry_merge() -> None:
    market = _market(
        "pacifica",
        -0.004,
        kind="published_current_hour_estimate",
        oracle_price=100.0,
    )
    market["entry_safety_buffer_seconds"] = 17.0
    market["exit_safety_buffer_seconds"] = 9.0
    market["timing_policy_source"] = "adapter_pacifica_prices"

    normalized = normalize_synchronized_capture_market(market)
    result = _route(long=normalized)

    assert normalized["entry_safety_buffer_seconds"] == 17.0
    assert normalized["exit_safety_buffer_seconds"] == 9.0
    assert normalized["timing_policy_source"] == "adapter_pacifica_prices"
    assert normalized["timing_policy_provenance"]["entry_source"] == "adapter_pacifica_prices"
    assert normalized["timing_policy_provenance"]["exit_source"] == "adapter_pacifica_prices"
    assert "long_position_inclusion_rule_unverified" in result["risk_flags"]
    assert result["hard_blockers"] == []


def test_hyperliquid_predicted_and_current_rates_are_typed_estimates() -> None:
    predicted = rate_estimate_from_market(
        _market("hyperliquid", 0.003, kind="published_predicted_next")
    )
    current = _market("hyperliquid", 0.003, kind="published_current_fallback")
    current.pop("normalized_next_funding_rate")
    current_estimate = rate_estimate_from_market(current)

    assert predicted["rate_estimate_kind"] == RateEstimateKind.PUBLISHED_PREDICTED.value
    assert current_estimate["rate_estimate_kind"] == RateEstimateKind.CURRENT_RATE_FALLBACK.value
    assert current_estimate["rate_estimate_confidence"] < predicted["rate_estimate_confidence"]
    assert "rate_forecast_or_predicted" in predicted["rate_estimate_risk_flags"]


def test_dydx_oracle_price_is_discovery_reference_when_mark_missing() -> None:
    dydx = _market(
        "dydx",
        -0.006,
        kind="published_next_hour",
        mark_price=None,
        index_price=100.0,
        oracle_price=100.0,
    )

    result = _route(long=dydx)

    assert result["hard_blockers"] == []
    assert "long_mark_price_missing_reference_price_used" in result["risk_flags"]
    assert result["economically_observable"] is True


def test_nado_usdt0_is_collateral_risk_not_discovery_blocker() -> None:
    nado = _market(
        "nado",
        -0.006,
        kind="published_latest_24h_x18",
        collateral="USDT0",
        fee=False,
    )
    nado["paper_enabled"] = False

    result = _route(long=nado)

    assert result["hard_blockers"] == []
    assert "collateral_other_dollar_stable" in result["risk_flags"]
    assert "collateral_usdt0_risk" in result["risk_flags"]
    assert "long_fee_fallback_used" in result["risk_flags"]
    assert result["experimental_paper_ready"] is True
    assert result["verified_paper_ready"] is False
    assert "collateral_accounting_contract_unverified" in result["verified_paper_blockers"]


def test_risex_testnet_mainnet_mismatch_is_hard_and_same_env_is_watchable() -> None:
    risex = _market(
        "risex",
        -0.006,
        environment="testnet",
        kind="published_current_interval_rate",
    )
    binance_mainnet = _market("binance", 0.006)

    mismatch = _route(long=risex, short=binance_mainnet)
    same_env = _route(
        long=risex,
        short=_market("binance", 0.006, environment="testnet"),
    )

    assert "environment_mismatch" in mismatch["hard_blockers"]
    assert same_env["hard_blockers"] == []
    assert same_env["economically_observable"] is True
    assert same_env["not_verified_alpha"] is True


@pytest.mark.parametrize(
    ("label", "mutate", "risk_flag", "verified_blocker"),
    [
        (
            "exact_next_rate",
            lambda market: (
                market.pop("normalized_next_funding_rate", None),
                market.update({"funding_rate_kind": "published_current_interval_rate"}),
            ),
            "long_estimated_rate_used",
            "long_exact_next_rate_missing",
        ),
        (
            "fee_evidence",
            lambda market: market.pop("fee_evidence", None),
            "long_fee_fallback_used",
            "long_fee_evidence_unverified",
        ),
        (
            "quantity_step",
            lambda market: market.pop("quantity_step", None),
            "long_quantity_step_missing",
            "long_quantity_step_missing",
        ),
        (
            "min_notional",
            lambda market: market.pop("min_notional_usd", None),
            "long_min_notional_missing",
            "long_min_notional_missing",
        ),
        (
            "orderbook_depth",
            lambda market: (
                market.pop("bids", None),
                market.pop("asks", None),
                market.pop("best_bid", None),
                market.pop("best_ask", None),
                market.pop("orderbook_response_received_at", None),
                market.pop("orderbook_event_time", None),
                market.pop("orderbook_depth_available", None),
            ),
            "long_synthetic_fill_model_required",
            "verified_ioc_execution_evidence_missing",
        ),
    ],
)
def test_capability_field_classification_table(
    label: str,
    mutate: Any,
    risk_flag: str,
    verified_blocker: str,
) -> None:
    long_market = _market("binance", -0.006)
    mutate(long_market)

    discovery = _route(long=long_market)
    verified = _route(long=long_market, mode=EvaluationMode.VERIFIED_PAPER)

    assert discovery["hard_blockers"] == [], label
    assert risk_flag in discovery["risk_flags"], label
    assert verified_blocker in verified["mode_blockers"], label


def test_verified_planner_uses_late_gates_but_discovery_planner_does_not() -> None:
    long_market = _market("binance", -0.006)
    long_market.pop("quantity_step")

    discovery_plan = FundingSettlementPlanner(
        evaluation_mode=EvaluationMode.DISCOVERY,
    ).plan(
        long_market=long_market,
        short_market=_market("bybit", 0.006),
        now=datetime.fromisoformat(OBSERVED_AT),
        target_notional=500.0,
    )
    verified_plan = FundingSettlementPlanner(
        evaluation_mode=EvaluationMode.VERIFIED_PAPER,
    ).plan(
        long_market=long_market,
        short_market=_market("bybit", 0.006),
        now=datetime.fromisoformat(OBSERVED_AT),
        target_notional=500.0,
    )

    assert "long_quantity_step_missing" not in discovery_plan.blockers
    assert "long_quantity_step_missing" in verified_plan.blockers
    assert discovery_plan.planner["route_readiness"]["economically_observable"] is True
