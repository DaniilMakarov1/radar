from __future__ import annotations

import time
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from smart_money_radar.cli import main
from smart_money_radar.funding.adapter_contracts import (
    PRIMARY_SHADOW_VENUES,
    USD_MAJOR_STABLE,
    collateral_family,
    funding_adapter_contract_from_market,
    mandatory_shadow_inventory,
)
from smart_money_radar.funding.adapters.base import build_endpoint_identity
from smart_money_radar.funding.incentives import (
    funding_net_excluding_points,
    rank_opportunities_with_points_tiebreaker,
)
from smart_money_radar.funding.realized_settlements import settlement_cashflow_trusted
from smart_money_radar.funding.shadow_monitor import (
    FundingShadowConfig,
    FundingShadowMonitor,
    adaptive_broad_sweep_interval_seconds,
)
from smart_money_radar.funding.risex_probe import (
    RiseXProbeConfig,
    classify_risex_funding_semantics_observation,
    redact_secret_payload,
    run_risex_funding_probe,
)
from smart_money_radar.funding.settlement_contracts import (
    FundingAccrualModel,
    settlement_contract_from_market,
)
from smart_money_radar.funding.strategy_synchronized_funding import (
    EventWindowPlannerConfig,
    build_settlement_capture_opportunity,
)
from smart_money_radar.funding.stablecoins import (
    PublicStablecoinPriceProvider,
    StablecoinPrice,
    StaticStablecoinPriceProvider,
    evaluate_stablecoin_route,
    stablecoin_basis_bps,
    stablecoin_reserve_bps,
)
from smart_money_radar.funding.venue_capabilities import (
    ExecutionModel,
    apply_declared_venue_capability_contract,
    venue_funding_capabilities,
)
from smart_money_radar.notifications import (
    TelegramNotifier,
    TelegramScope,
    redact_telegram_secret,
    resolve_telegram_credentials,
    telegram_update_chat_ids,
)
from smart_money_radar.paper_bot.clock import FakeClock
from smart_money_radar.storage import SQLiteStore


class _FakeTelegramResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeTelegramResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class _ShadowClient:
    def __init__(
        self,
        venue: str,
        *,
        asset: str = "BTC",
        collateral: str = "USDT",
        funding_rate: float = 0.002,
        settlement: datetime,
        environment: str = "mainnet",
        delay_seconds: float = 0.0,
        quantity_step: float | None = 0.001,
    ) -> None:
        self.venue = venue
        self.asset = asset
        self.symbol = f"{asset}{collateral}"
        self.collateral = collateral
        self.funding_rate = funding_rate
        self.settlement = settlement
        self.environment = environment
        self.delay_seconds = delay_seconds
        self.quantity_step = quantity_step
        self.catalog_calls = 0
        self.sweep_calls = 0
        self.orderbook_calls = 0
        self.history_calls = 0

    def catalog_metadata(self, observed_at: str) -> list[dict[str, Any]]:
        self.catalog_calls += 1
        return [
            {
                "venue": self.venue,
                "symbol": self.symbol,
                "canonical_asset": self.asset,
                "base_asset": self.asset,
                "quote_asset": self.collateral,
                "collateral_asset": self.collateral,
                "environment": self.environment,
                "contract_type": "linear_perpetual",
                "contract_kind": "linear_perpetual",
                "contract_multiplier": 1.0,
                "status": "active",
                "observed_at": observed_at,
            }
        ]

    def funding_sweep(
        self,
        observed_at: str,
        _catalog: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        self.sweep_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return [self.market(observed_at)], []

    def catalog_and_markets(self, observed_at: str):
        self.catalog_calls += 1
        return self.catalog_metadata(observed_at), [self.market(observed_at)], []

    def orderbook(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.orderbook_calls += 1
        raise AssertionError("shadow monitor must not fetch orderbooks")

    def funding_history(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        self.history_calls += 1
        raise AssertionError("shadow monitor must not fetch long-term history")

    def market(self, observed_at: str) -> dict[str, Any]:
        return complete_market(
            venue=self.venue,
            symbol=self.symbol,
            asset=self.asset,
            collateral=self.collateral,
            funding_rate=self.funding_rate,
            settlement=self.settlement.isoformat(),
            observed_at=observed_at,
            environment=self.environment,
            quantity_step=self.quantity_step,
        )


class _FailingShadowClient:
    def __init__(
        self,
        venue: str,
        *,
        environment: str = "mainnet",
        error: str = "fixture_unavailable",
    ) -> None:
        self.venue = venue
        self.environment = environment
        self.error = error

    def catalog_metadata(self, _observed_at: str) -> list[dict[str, Any]]:
        return []

    def funding_sweep(
        self,
        _observed_at: str,
        _catalog: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        raise RuntimeError(self.error)


def complete_market(
    *,
    venue: str,
    symbol: str = "BTCUSDT",
    asset: str = "BTC",
    collateral: str = "USDT",
    funding_rate: float = 0.002,
    settlement: str = "2026-07-28T12:01:30+00:00",
    observed_at: str = "2026-07-28T12:00:00+00:00",
    environment: str = "mainnet",
    quantity_step: float | None = 0.001,
    funding_interval_hours: float = 1.0,
    funding_accrual_model: str = FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
    position_inclusion_rule_verified: bool = True,
    assessment_jitter_seconds: float = 0.0,
    settlement_semantics_status: str = "VERIFIED",
) -> dict[str, Any]:
    row = {
        "venue": venue,
        "symbol": symbol,
        "canonical_asset": asset,
        "base_asset": asset,
        "quote_asset": collateral,
        "collateral_asset": collateral,
        "price_quote_currency": collateral,
        "settlement_collateral": collateral,
        "collateral_family": USD_MAJOR_STABLE,
        "environment": environment,
        "environment_verified": True,
        "endpoint_base_url": f"https://api.{venue}.test",
        "endpoint_identity_provenance": f"test-fixture:{venue}:endpoint:v1",
        "endpoint_client_version": "test-client-v1",
        "endpoint_verified_at": observed_at,
        "api_product_type": "linear_perpetual",
        "market_type": "linear_perpetual",
        "product_type": "linear_perpetual",
        "data_enabled": True,
        "strategy_observation_enabled": True,
        "shadow_candidate_enabled": True,
        "paper_enabled": True,
        "live_enabled": False,
        "execution_model": "CLOB",
        "settlement_verification_level": "test_fixture",
        "funding_rate": funding_rate,
        "normalized_next_funding_rate": funding_rate,
        "funding_interval_hours": funding_interval_hours,
        "hourly_funding_rate": funding_rate / funding_interval_hours,
        "funding_rate_kind": "published_next_estimate",
        "funding_rate_semantics": "next_settlement",
        "funding_rate_unit": "fraction_of_notional_per_settlement",
        "funding_sign_convention": "positive_long_pays",
        "next_funding_at": settlement,
        "mark_price": 100_000.0,
        "index_price": 100_000.0,
        "volume_24h_usd": 100_000_000.0,
        "open_interest_usd": 50_000_000.0,
        "contract_type": "linear_perpetual",
        "contract_kind": "linear_perpetual",
        "contract_multiplier": 1.0,
        "min_quantity": 0.001,
        "min_notional_usd": 5.0,
        "taker_fee_rate": 0.0005,
        "fee_source": "configured_trusted_fee",
        "fee_evidence": {
            "source_kind": "configured_trusted_fee",
            "source_identifier": f"test-fixture:{venue}:fees:v1",
            "trust_status": "CONFIGURED_TRUSTED",
            "venue": venue,
            "liquidity_role": "taker",
            "observed_at": observed_at,
            "reviewed_at": observed_at,
            "environment": "mainnet",
            "market_type": "linear_perpetual",
            "product_type": "linear_perpetual",
            "applicability": "taker",
            "evidence_version": "test-fee-evidence-v1",
        },
        "fee_observed_at": observed_at,
        "fee_reviewed_at": observed_at,
        "supports_perpetuals": True,
        "is_linear_contract": True,
        "supports_discrete_funding": True,
        "supports_public_shadow_mode": True,
        "position_inclusion_rule": "perp_position_at_settlement",
        "position_inclusion_rule_verified": position_inclusion_rule_verified,
        "funding_accrual_model": funding_accrual_model,
        "settlement_interval_seconds": funding_interval_hours * 3600.0,
        "displayed_rate_period_seconds": funding_interval_hours * 3600.0,
        "next_settlement_source": f"adapter_{venue}_fixture",
        "rate_per_settlement_derivation": "fixture_rate_is_per_settlement",
        "funding_notional_price_source": "fixture_mark_price",
        "assessment_jitter_before_seconds": assessment_jitter_seconds,
        "assessment_jitter_after_seconds": assessment_jitter_seconds,
        "settlement_confirmation_source": "fixture_public_history",
        "realized_payment_source": "fixture_account_ledger",
        "settlement_semantics_status": settlement_semantics_status,
        "entry_safety_buffer_seconds": 20,
        "exit_safety_buffer_seconds": 20,
        "timing_policy_source": f"adapter_{venue}_fixture",
        "realized_history_semantics": "generic_history_unverified",
        "response_received_at": observed_at,
        "source_event_at": observed_at,
        "observed_at": observed_at,
    }
    if quantity_step is not None:
        row["quantity_step"] = quantity_step
    return row


def price_provider(observed_at: str) -> StaticStablecoinPriceProvider:
    return StaticStablecoinPriceProvider(
        {
            "USDC": [
                StablecoinPrice("USDC", 1.0000, "source_a", observed_at, observed_at),
                StablecoinPrice("USDC", 1.0001, "source_b", observed_at, observed_at),
            ],
            "USDT": [
                StablecoinPrice("USDT", 0.9999, "source_a", observed_at, observed_at),
                StablecoinPrice("USDT", 1.0000, "source_b", observed_at, observed_at),
            ],
        }
    )


class _CountingStablecoinPriceProvider:
    def __init__(self, observed_at: str) -> None:
        self.observed_at = observed_at
        self.calls: list[tuple[str, str]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def prices(self, asset: str, observed_at: str) -> list[StablecoinPrice]:
        symbol = str(asset).upper()
        self.calls.append((symbol, observed_at))
        if symbol == "USDT":
            return [
                StablecoinPrice("USDT", 0.9999, "source_a", self.observed_at, self.observed_at),
                StablecoinPrice("USDT", 1.0000, "source_b", self.observed_at, self.observed_at),
            ]
        if symbol == "USDC":
            return [
                StablecoinPrice("USDC", 1.0000, "source_a", self.observed_at, self.observed_at),
                StablecoinPrice("USDC", 1.0001, "source_b", self.observed_at, self.observed_at),
            ]
        return []


def test_shadow_scope_uses_funding_vars_when_shadow_absent(monkeypatch) -> None:
    monkeypatch.delenv("FUNDING_SHADOW_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("FUNDING_SHADOW_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("FUNDING_TELEGRAM_BOT_TOKEN", "funding-token")
    monkeypatch.setenv("FUNDING_TELEGRAM_CHAT_ID", "funding-chat")

    credentials = resolve_telegram_credentials(TelegramScope.SHADOW)

    assert credentials.token_source_env_var == "FUNDING_TELEGRAM_BOT_TOKEN"
    assert credentials.chat_id_source_env_var == "FUNDING_TELEGRAM_CHAT_ID"


def test_shadow_scope_overrides_funding_vars(monkeypatch) -> None:
    monkeypatch.setenv("FUNDING_TELEGRAM_BOT_TOKEN", "funding-token")
    monkeypatch.setenv("FUNDING_TELEGRAM_CHAT_ID", "funding-chat")
    monkeypatch.setenv("FUNDING_SHADOW_TELEGRAM_BOT_TOKEN", "shadow-token")
    monkeypatch.setenv("FUNDING_SHADOW_TELEGRAM_CHAT_ID", "shadow-chat")

    credentials = resolve_telegram_credentials("shadow")

    assert credentials.token_source_env_var == "FUNDING_SHADOW_TELEGRAM_BOT_TOKEN"
    assert credentials.chat_id_source_env_var == "FUNDING_SHADOW_TELEGRAM_CHAT_ID"


def test_telegram_token_is_redacted_from_errors(monkeypatch) -> None:
    monkeypatch.setenv("FUNDING_SHADOW_TELEGRAM_BOT_TOKEN", "SECRET")
    monkeypatch.setenv("FUNDING_SHADOW_TELEGRAM_CHAT_ID", "123")

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("https://api.telegram.org/botSECRET/sendMessage failed")

    monkeypatch.setattr("smart_money_radar.notifications.urllib.request.urlopen", fail)
    result = TelegramNotifier(scope="shadow").send("<b>SHADOW FUNDING</b>")

    assert result.status == "failed"
    assert "SECRET" not in str(result.error)
    assert "/bot<redacted>/" in str(result.error)
    assert redact_telegram_secret("/botSECRET/getUpdates") == "/bot<redacted>/getUpdates"


def test_telegram_chat_id_shadow_uses_resolved_token(monkeypatch) -> None:
    captured: list[str] = []
    monkeypatch.delenv("FUNDING_SHADOW_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("FUNDING_TELEGRAM_BOT_TOKEN", "FUNDINGSECRET")

    def ok(request: Any, timeout: int):
        captured.append(request.full_url)
        return _FakeTelegramResponse(
            b'{"ok": true, "result": [{"message": {"chat": {"id": 42, "type": "private", "first_name": "D"}}}]}'
        )

    monkeypatch.setattr("smart_money_radar.notifications.urllib.request.urlopen", ok)
    chats = telegram_update_chat_ids(scope="shadow")

    assert chats[0]["chat_id"] == "42"
    assert "/botFUNDINGSECRET/getUpdates" in captured[0]


def test_telegram_test_scope_shadow_does_not_print_token(monkeypatch, capsys) -> None:
    monkeypatch.setenv("FUNDING_SHADOW_TELEGRAM_BOT_TOKEN", "SHADOWSECRET")
    monkeypatch.setenv("FUNDING_SHADOW_TELEGRAM_CHAT_ID", "123")

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("https://api.telegram.org/botSHADOWSECRET/sendMessage failed")

    monkeypatch.setattr("smart_money_radar.notifications.urllib.request.urlopen", fail)
    code = main(["--db", ":memory:", "telegram-test", "--scope", "shadow"])
    captured = capsys.readouterr()

    assert code == 1
    assert "SHADOWSECRET" not in captured.out
    assert "/bot<redacted>/" in captured.out


def test_shadow_monitor_writes_no_paper_state(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    monitor = FundingShadowMonitor(
        store,
        [
            _ShadowClient("risex", collateral="USDC", funding_rate=-0.002, settlement=settlement),
            _ShadowClient("binance", collateral="USDT", funding_rate=0.004, settlement=settlement),
        ],
        config=FundingShadowConfig(telegram_enabled=False).validated(),
        stablecoin_price_provider=price_provider(now.isoformat()),
        clock=FakeClock(now),
    )

    result = monitor.run_once()
    counts = store.funding_shadow_counts()

    assert result["shadow_candidates"] == 0
    assert result["research_only_routes"] >= 1
    assert counts["opportunities"] >= 1
    assert counts["observations"] >= 2
    assert counts["paper_positions"] == 0
    assert counts["paper_orders"] == 0
    assert counts["paper_accounts"] == 0


def test_one_settlement_uses_second_venue_as_short_hold_hedge() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    first = now + timedelta(seconds=20)
    far = now + timedelta(hours=4)
    long_market = complete_market(
        venue="binance",
        collateral="USDT",
        funding_rate=0.0,
        settlement=far.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="okx",
        collateral="USDT",
        funding_rate=0.008,
        settlement=first.isoformat(),
        observed_at=now.isoformat(),
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert opportunity["opportunity_shape"] == "ONE_SETTLEMENT"
    assert [event["venue"] for event in opportunity["included_settlement_events"]] == ["okx"]
    assert [event["venue"] for event in opportunity["excluded_settlement_events"]] == ["binance"]
    assert opportunity["conservative_net_usd"] > 0


def test_simultaneous_positive_settlements_are_summed() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.004,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=0.005,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert opportunity["opportunity_shape"] == "MULTIPLE_SETTLEMENTS"
    assert len(opportunity["included_settlement_events"]) == 2
    assert opportunity["expected_funding_cashflow_usd"] == pytest.approx(4.5)


def test_close_after_second_near_settlement_when_it_improves_net() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    first = now + timedelta(seconds=30)
    second = first + timedelta(seconds=20)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.004,
        settlement=first.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=0.004,
        settlement=second.isoformat(),
        observed_at=now.isoformat(),
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert opportunity["planner"]["selected_plan"] == "exit_after_second_settlement"
    assert opportunity["opportunity_shape"] == "MULTIPLE_SETTLEMENTS"
    assert len(opportunity["included_settlement_events"]) == 2


def test_negative_second_settlement_is_excluded_when_exit_is_guaranteed() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    first = now + timedelta(seconds=30)
    second = first + timedelta(seconds=20)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.014,
        settlement=first.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=-0.004,
        settlement=second.isoformat(),
        observed_at=now.isoformat(),
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert opportunity["planner"]["selected_plan"] == "exit_after_first_settlement"
    assert [event["venue"] for event in opportunity["included_settlement_events"]] == ["binance"]
    assert [event["venue"] for event in opportunity["excluded_settlement_events"]] == ["bybit"]


def test_unavoidable_negative_second_settlement_is_deducted() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    first = now + timedelta(seconds=30)
    second = first + timedelta(seconds=4)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.014,
        settlement=first.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=-0.004,
        settlement=second.isoformat(),
        observed_at=now.isoformat(),
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert len(opportunity["included_settlement_events"]) == 2
    assert opportunity["expected_funding_cashflow_usd"] == pytest.approx(5.0)


def test_three_settlement_events_are_considered_as_timeline() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    first = now + timedelta(seconds=30)
    second = first + timedelta(seconds=20)
    third = first + timedelta(seconds=60)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.004,
        settlement=first.isoformat(),
        observed_at=now.isoformat(),
    )
    long_market["funding_settlement_events"] = [
        {"scheduled_at": first.isoformat(), "funding_rate": -0.004},
        {"scheduled_at": third.isoformat(), "funding_rate": -0.003},
    ]
    short_market = complete_market(
        venue="bybit",
        funding_rate=0.002,
        settlement=second.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market["funding_settlement_events"] = [
        {"scheduled_at": second.isoformat(), "funding_rate": 0.002},
    ]

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert opportunity["planner"]["selected_plan"] == "exit_after_third_settlement"
    assert opportunity["opportunity_shape"] == "MULTIPLE_SETTLEMENTS"
    assert len(opportunity["included_settlement_events"]) == 3
    assert [
        (event["venue"], event["scheduled_at"])
        for event in opportunity["included_settlement_events"]
    ] == [
        ("binance", first.isoformat()),
        ("bybit", second.isoformat()),
        ("binance", third.isoformat()),
    ]
    assert opportunity["expected_funding_cashflow_usd"] == pytest.approx(4.5)


def test_second_settlement_too_far_does_not_extend_hold() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    first = now + timedelta(seconds=30)
    second = first + timedelta(minutes=10)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.006,
        settlement=first.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=0.010,
        settlement=second.isoformat(),
        observed_at=now.isoformat(),
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert opportunity["planner"]["selected_plan"] == "exit_after_first_settlement"
    assert opportunity["opportunity_shape"] == "ONE_SETTLEMENT"


def test_assessment_jitter_ambiguous_event_is_not_candidate() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    first = now + timedelta(seconds=30)
    second = first + timedelta(seconds=8)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.006,
        settlement=first.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=-0.004,
        settlement=second.isoformat(),
        observed_at=now.isoformat(),
        assessment_jitter_seconds=4.0,
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert opportunity["ambiguous_settlement_events"]
    assert "settlement_timing_ambiguous" in opportunity["blockers"]


def test_dynamic_intervals_and_displayed_rate_period_are_not_hardcoded() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    market = complete_market(
        venue="bybit",
        funding_rate=0.001,
        funding_interval_hours=0.5,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    contract = settlement_contract_from_market(market)

    assert contract.settlement_interval_seconds == pytest.approx(1800.0)
    assert contract.displayed_rate_period_seconds == pytest.approx(1800.0)


def test_market_row_cannot_self_promote_settlement_semantics() -> None:
    market = complete_market(
        venue="risex",
        collateral="USDC",
        environment="testnet",
        funding_accrual_model=FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
        position_inclusion_rule_verified=True,
        settlement_semantics_status="VERIFIED",
    )
    market["verification_level"] = "VERIFIED"
    market["official_evidence_urls"] = ["https://attacker.invalid/fake"]
    market["evidence_checked_at"] = "2099-01-01"
    market["assessment_jitter_before_seconds"] = 0
    market["assessment_jitter_after_seconds"] = 0
    market["settlement_confirmation_source"] = "payload_claimed_history"
    market["realized_payment_source"] = "payload_claimed_ledger"

    contract = settlement_contract_from_market(market)

    assert contract.accrual_model == FundingAccrualModel.UNKNOWN.value
    assert contract.position_inclusion_rule_verified is False
    assert contract.verification_level == "PUBLIC_OBSERVED"
    assert "attacker.invalid" not in ",".join(contract.official_evidence_urls)
    assert contract.evidence_checked_at != "2099-01-01"
    assert contract.assessment_jitter_before_seconds is None
    assert contract.assessment_jitter_after_seconds is None
    assert contract.settlement_confirmation_source is None
    assert contract.realized_payment_source is None


def test_unknown_adapter_payload_cannot_self_promote_settlement_contract() -> None:
    market = complete_market(
        venue="unknownvenue",
        funding_accrual_model=FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
        position_inclusion_rule_verified=True,
        settlement_semantics_status="VERIFIED",
    )
    market["official_evidence_urls"] = ["https://attacker.invalid/fake"]
    market["assessment_jitter_before_seconds"] = 0
    market["assessment_jitter_after_seconds"] = 0
    market["settlement_confirmation_source"] = "payload_claimed_history"

    contract = settlement_contract_from_market(market)

    assert contract.accrual_model == FundingAccrualModel.UNKNOWN.value
    assert contract.position_inclusion_rule_verified is False
    assert contract.verification_level == "UNVERIFIED"
    assert contract.official_evidence_urls == ()
    assert contract.assessment_jitter_before_seconds is None
    assert contract.assessment_jitter_after_seconds is None
    assert contract.settlement_confirmation_source is None


def test_displayed_8h_rate_is_not_used_as_next_settlement_cashflow() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    long_market = complete_market(
        venue="binance",
        funding_rate=0.0,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="lighter",
        funding_rate=0.001,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
        funding_interval_hours=1.0,
    )
    short_market["published_funding_rate"] = 0.008
    short_market["published_funding_interval_hours"] = 8.0
    long_market["taker_fee_rate"] = 0.0
    short_market["taker_fee_rate"] = 0.0

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert opportunity["expected_funding_cashflow_usd"] == pytest.approx(0.5)


def test_positive_gross_negative_conservative_net_is_not_candidate() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    long_market = complete_market(
        venue="binance",
        funding_rate=0.0,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=0.002,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    long_market["taker_fee_rate"] = 0.01
    short_market["taker_fee_rate"] = 0.01

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert opportunity["expected_funding_cashflow_usd"] > 0
    assert opportunity["conservative_net_usd"] < 0
    assert "conservative_net_not_positive" in opportunity["blockers"]


def test_missing_fee_uses_conservative_fallback_without_blocking_discovery() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.010,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=0.010,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    long_market.pop("taker_fee_rate", None)
    long_market.pop("fee_rate", None)
    long_market.pop("fee_evidence", None)
    long_market.pop("fee_source", None)

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert "long_fee_unknown" not in opportunity["blockers"]
    costs = {
        row["component"]: row
        for row in opportunity["cost_estimates"]
    }
    assert costs["entry_fee_leg_a"]["status"] == "UNKNOWN"
    assert costs["entry_fee_leg_a"]["blocker_if_missing"] == "taker_fee_rate_missing"
    assert float(costs["entry_fee_leg_a"]["value"]) > 0.0
    assert costs["entry_slippage_leg_a"]["status"] == "CONSERVATIVE_CONFIGURED"


def test_explicit_zero_fee_still_uses_configured_conservative_floor() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.010,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=0.010,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    long_market["taker_fee_rate"] = 0.0
    short_market["taker_fee_rate"] = 0.0

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
    )

    assert "long_fee_unknown" not in opportunity["blockers"]
    assert "short_fee_unknown" not in opportunity["blockers"]
    assert opportunity["modeled_costs"]["entry_fees_usd"] > 0.0
    costs = {
        row["component"]: row
        for row in opportunity["cost_estimates"]
    }
    assert costs["entry_fee_leg_a"]["status"] == "CONFIGURED_TRUSTED"
    assert float(costs["entry_fee_leg_a"]["value"]) > 0.0


def test_paradex_is_not_built_as_shadow_route(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [
            _ShadowClient("paradex", funding_rate=0.010, settlement=settlement),
            _ShadowClient("binance", funding_rate=-0.002, settlement=settlement),
        ],
        config=FundingShadowConfig(telegram_enabled=False),
        clock=FakeClock(now),
    )

    result = monitor.run_once()

    assert result["opportunities"] == 0
    assert result["rejection_reasons"]["funding_continuous_pro_rata"] >= 1


def test_risex_unverified_opportunity_is_research_only(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    risex = complete_market(
        venue="risex",
        collateral="USDC",
        funding_rate=0.006,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
        environment="testnet",
        funding_accrual_model=FundingAccrualModel.UNKNOWN.value,
        position_inclusion_rule_verified=False,
        settlement_semantics_status="PUBLIC_OBSERVED",
    )
    hedge = complete_market(
        venue="binance",
        collateral="USDC",
        funding_rate=0.0,
        settlement=(now + timedelta(hours=4)).isoformat(),
        observed_at=now.isoformat(),
        environment="testnet",
    )
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [],
        config=FundingShadowConfig(environment="testnet", telegram_enabled=False),
        clock=FakeClock(now),
    )

    opportunity = monitor._shadow_opportunity_for_pair("BTC", hedge, risex, now)

    assert opportunity["status"] == "RESEARCH_ONLY"
    assert "short_position_inclusion_rule_unverified" not in opportunity["blockers"]
    assert opportunity["planner"]["route_readiness"]["not_verified_alpha"] is True
    assert "short_position_inclusion_rule_unverified" in opportunity["planner"]["route_readiness"]["risk_flags"]


def test_risex_payload_promotion_remains_research_only(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    first = now + timedelta(seconds=30)
    second = first + timedelta(seconds=20)
    risex = complete_market(
        venue="risex",
        collateral="USDC",
        funding_rate=0.008,
        settlement=first.isoformat(),
        observed_at=now.isoformat(),
        environment="testnet",
        funding_accrual_model=FundingAccrualModel.POSITION_AT_EVENT_FULL.value,
        position_inclusion_rule_verified=True,
        settlement_semantics_status="TESTNET_SNAPSHOT_SUPPORTED",
    )
    hedge = complete_market(
        venue="fixturehedge",
        collateral="USDC",
        funding_rate=0.0,
        settlement=(now + timedelta(hours=4)).isoformat(),
        observed_at=now.isoformat(),
        environment="testnet",
    )
    near = complete_market(
        venue="fixturehedge",
        collateral="USDC",
        funding_rate=-0.004,
        settlement=second.isoformat(),
        observed_at=now.isoformat(),
        environment="testnet",
    )
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [],
        config=FundingShadowConfig(environment="testnet", telegram_enabled=False),
        clock=FakeClock(now),
    )

    one = monitor._shadow_opportunity_for_pair("BTC", hedge, risex, now)
    multi = monitor._shadow_opportunity_for_pair("BTC", near, risex, now)

    assert one["status"] == "RESEARCH_ONLY"
    assert one["planner"]["route_readiness"]["not_verified_alpha"] is True
    assert "short_position_inclusion_rule_unverified" in one["planner"]["route_readiness"]["risk_flags"]
    assert multi["status"] == "SHADOW_CANDIDATE"
    assert multi["planner"]["route_readiness"]["not_verified_alpha"] is True
    assert "short_position_inclusion_rule_unverified" in multi["planner"]["route_readiness"]["risk_flags"]


def test_venue_capability_registry_separates_experimental_from_verified() -> None:
    risex = venue_funding_capabilities("risex")
    pacifica = venue_funding_capabilities("pacifica")
    variational = venue_funding_capabilities("variational")
    paradex = venue_funding_capabilities("paradex")

    assert risex.data_enabled is True
    assert risex.strategy_observation_enabled is True
    assert risex.shadow_candidate_enabled is True
    assert risex.paper_enabled is False
    assert risex.live_enabled is False
    assert risex.readiness_status == "EXPERIMENTAL"
    assert risex.mandatory is True
    assert pacifica.data_enabled is True
    assert pacifica.strategy_observation_enabled is True
    assert pacifica.shadow_candidate_enabled is True
    assert pacifica.paper_enabled is False
    assert pacifica.readiness_status == "EXPERIMENTAL"
    assert variational.execution_model == ExecutionModel.RFQ
    assert variational.strategy_observation_enabled is False
    assert variational.readiness_status == "QUARANTINED"
    assert paradex.strategy_observation_enabled is False
    assert paradex.readiness_status == "STRUCTURALLY_INCOMPATIBLE"
    assert "funding_continuous_pro_rata" in paradex.blockers


def test_venue_capability_payload_cannot_promote_risex_to_paper() -> None:
    row = complete_market(
        venue="risex",
        collateral="USDC",
        environment="testnet",
    )
    row.update(
        {
            "shadow_candidate_enabled": True,
            "paper_enabled": True,
            "live_enabled": True,
            "execution_model": "CLOB",
            "settlement_verification_level": "VERIFIED",
            "venue_capability_blockers": [],
        }
    )

    hardened = apply_declared_venue_capability_contract(row)

    assert hardened["shadow_candidate_enabled"] is True
    assert hardened["paper_enabled"] is False
    assert hardened["live_enabled"] is False
    assert hardened["settlement_verification_level"] == "PUBLIC_OBSERVED"
    assert "mainnet_canary_required" in hardened["venue_capability_blockers"]


def test_nado_periodic_unverified_route_is_research_only() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    nado = apply_declared_venue_capability_contract(
        complete_market(
            venue="nado",
            symbol="BTC-PERP_USDT0",
            collateral="USDT0",
            funding_rate=0.024 / 24,
            settlement=(now + timedelta(seconds=30)).isoformat(),
            observed_at=now.isoformat(),
            environment="mainnet",
        )
    )
    hedge = apply_declared_venue_capability_contract(
        complete_market(
            venue="binance",
            collateral="USDT0",
            funding_rate=-0.0001,
            settlement=(now + timedelta(hours=4)).isoformat(),
            observed_at=now.isoformat(),
            environment="mainnet",
        )
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=nado,
        short_market=hedge,
        now=now,
        target_notional=1_000,
    )

    assert opportunity["eligibility_status"] == "RESEARCH_ONLY"
    assert "long_shadow_candidate_disabled" not in opportunity["blockers"]
    assert opportunity["planner"]["route_readiness"]["not_verified_alpha"] is True
    assert nado["paper_enabled"] is False
    assert nado["live_enabled"] is False


def test_variational_strategy_route_is_capability_blocked() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    variational = apply_declared_venue_capability_contract(
        complete_market(
            venue="variational",
            collateral="USDC",
            funding_rate=0.001,
            settlement=(now + timedelta(seconds=30)).isoformat(),
            observed_at=now.isoformat(),
            environment="mainnet",
        )
    )
    hedge = apply_declared_venue_capability_contract(
        complete_market(
            venue="binance",
            collateral="USDC",
            funding_rate=-0.0001,
            settlement=(now + timedelta(hours=4)).isoformat(),
            observed_at=now.isoformat(),
            environment="mainnet",
        )
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=variational,
        short_market=hedge,
        now=now,
        target_notional=1_000,
    )

    assert opportunity["eligibility_status"] in {"CAPABILITY_BLOCKED", "RESEARCH_ONLY"}
    assert "long_venue_deactivated" in opportunity["planner"]["route_readiness"]["hard_blockers"]
    assert variational["execution_model"] == "RFQ"
    assert variational["paper_enabled"] is False


def test_risex_is_in_primary_inventory_and_missing_step_is_research_only() -> None:
    market = complete_market(venue="risex", collateral="USDC", quantity_step=None)
    contract = funding_adapter_contract_from_market(market)
    inventory = mandatory_shadow_inventory([market], registered_venues=["risex"])

    assert "risex" in {row["venue"] for row in inventory}
    assert contract.status == "RESEARCH_ONLY"
    assert "quantity_step_missing" in contract.reasons
    assert "contract_multiplier_is_not_quantity_step" in contract.reasons


def test_risex_valid_fixture_is_shadow_eligible() -> None:
    market = complete_market(venue="risex", collateral="USDC")
    contract = funding_adapter_contract_from_market(market)

    assert contract.status == "SHADOW_ELIGIBLE"
    assert contract.settlement_collateral == "USDC"
    assert contract.collateral_family == USD_MAJOR_STABLE


def test_risex_testnet_base_url_cannot_be_marked_mainnet() -> None:
    from smart_money_radar.funding.adapters.risex import RiseXFundingClient

    with pytest.raises(Exception, match="mainnet cannot use a testnet base_url"):
        RiseXFundingClient(environment="mainnet")


def test_risex_testnet_cannot_match_mainnet() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    testnet = complete_market(venue="risex", collateral="USDC", environment="testnet")
    mainnet = complete_market(venue="binance", collateral="USDT", environment="mainnet")
    monitor = FundingShadowMonitor(
        SQLiteStore(Path(":memory:")),
        [],
        config=FundingShadowConfig(environment="mainnet", telegram_enabled=False),
        clock=FakeClock(now),
    )

    opportunity = monitor._shadow_opportunity_for_pair("BTC", testnet, mainnet, now)

    assert opportunity["status"] in {"CAPABILITY_BLOCKED", "RESEARCH_ONLY"}
    assert "environment_mismatch" in opportunity["blockers"]


def test_unverified_endpoint_identity_blocks_candidate() -> None:
    first = complete_market(venue="binance", environment="mainnet")
    second = complete_market(
        venue="bybit",
        environment="mainnet",
        funding_rate=-0.002,
    )
    first["environment_verified"] = False
    second["environment_verified"] = True

    opportunity = build_settlement_capture_opportunity(
        long_market=first,
        short_market=second,
        now=datetime(2026, 7, 28, 12, 1, tzinfo=UTC),
        target_notional=1_000,
        max_response_age_seconds=300,
    )

    readiness = opportunity["planner"]["route_readiness"]
    assert "long_environment_unverified" in readiness["risk_flags"]
    assert "long_environment_unverified" in readiness["verified_paper_blockers"]
    assert opportunity["eligibility_status"] != "CAPABILITY_BLOCKED"


def test_cross_usdc_usdt_route_is_allowed_with_reserve() -> None:
    observed_at = "2026-07-28T12:00:00+00:00"
    result = evaluate_stablecoin_route(
        long_collateral="USDC",
        short_collateral="USDT",
        provider=price_provider(observed_at),
        observed_at=observed_at,
        reference_notional=1_000.0,
        funding_net_before_stablecoin_reserve=12.0,
        adverse_stablecoin_change_1m_bps=[0.0] * 10,
    )

    assert result["compatible"]
    assert result["numeraire"] == "USD"
    assert result["status"] == "PASS"
    assert result["stablecoin_reserve_bps"] >= 10.0
    assert result["funding_net_after_stablecoin_reserve"] < 12.0


def test_shadow_monitor_attaches_stablecoin_snapshot_before_planner(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    monitor = FundingShadowMonitor(
        store,
        [
            _ShadowClient("binance", collateral="USDT", funding_rate=-0.006, settlement=settlement),
            _ShadowClient("bybit", collateral="USDC", funding_rate=0.006, settlement=settlement),
        ],
        config=FundingShadowConfig(telegram_enabled=False).validated(),
        stablecoin_price_provider=price_provider(now.isoformat()),
        clock=FakeClock(now),
    )

    monitor.run_once()
    opportunity = next(
        row for row in monitor._last_opportunities if row["long_venue"] == "binance"
    )
    stablecoin = opportunity["stablecoin_risk"]

    assert opportunity["status"] == "SHADOW_CANDIDATE"
    assert stablecoin["status"] == "PASS"
    assert stablecoin["stablecoin_reserve_usd"] > 0
    assert stablecoin["funding_net_after_stablecoin_reserve"] == pytest.approx(
        opportunity["conservative_net_usd"]
    )
    assert stablecoin["funding_net_before_stablecoin_reserve"] == pytest.approx(
        opportunity["conservative_net_usd"] + stablecoin["stablecoin_reserve_usd"]
    )
    for blocker in (
        "stablecoin_snapshot_missing",
        "stablecoin_snapshot_pair_mismatch",
        "stablecoin_snapshot_source_missing",
    ):
        assert blocker not in opportunity["blockers"]
    for side in ("long", "short"):
        snapshot = opportunity[f"{side}_market"]["stablecoin_route_evaluation"]
        assert snapshot["status"] == "PASS"
        assert snapshot["stablecoin_pair"] == "USDT/USDC"
        assert snapshot["source_identity"]["provider"] == "StaticStablecoinPriceProvider"
        assert snapshot["source_identity"]["sources"] == ["source_a", "source_b"]

    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            """
            SELECT stablecoin_risk_json, payload_json
              FROM funding_shadow_opportunities
             WHERE opportunity_key = ?
            """,
            (opportunity["opportunity_key"],),
        ).fetchone()
    stored_stablecoin = json.loads(row[0])
    stored_payload = json.loads(row[1])
    assert stored_stablecoin["status"] == "PASS"
    assert stored_payload["long_market"]["stablecoin_route_evaluation"]["stablecoin_pair"] == "USDT/USDC"
    assert stored_payload["short_market"]["stablecoin_route_evaluation"]["stablecoin_pair"] == "USDT/USDC"


def test_stablecoin_provider_used_by_broad_not_focused_and_expiry_blocks(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    provider = _CountingStablecoinPriceProvider(now.isoformat())
    clock = FakeClock(now)
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [
            _ShadowClient("binance", collateral="USDT", funding_rate=-0.006, settlement=settlement),
            _ShadowClient("bybit", collateral="USDC", funding_rate=0.006, settlement=settlement),
        ],
        config=FundingShadowConfig(telegram_enabled=False).validated(),
        stablecoin_price_provider=provider,
        clock=clock,
    )

    monitor.run_once()
    broad_calls = provider.call_count
    assert broad_calls > 0
    assert monitor._last_opportunities

    for _ in range(3):
        clock.advance(1.0)
        monitor.run_focused_once()
        assert provider.call_count == broad_calls
        assert any(row["stablecoin_risk"]["status"] == "PASS" for row in monitor._last_opportunities)

    clock.advance(4.0)
    monitor.run_focused_once()
    assert provider.call_count == broad_calls
    assert any(
        "stablecoin_snapshot_expired" in row["blockers"]
        or "stablecoin_snapshot_stale" in row["blockers"]
        for row in monitor._last_opportunities
    )


def test_public_stablecoin_provider_uses_two_sources_and_caches() -> None:
    calls: list[str] = []

    def fetch_json(url: str, _timeout: float) -> Any:
        calls.append(url)
        if "coingecko" in url and "usd-coin" in url:
            return {"usd-coin": {"usd": 1.0001}}
        if "coinbase" in url and "USDC" in url:
            return {"data": {"rates": {"USD": "1.0000"}}}
        raise AssertionError(url)

    provider = PublicStablecoinPriceProvider(fetch_json=fetch_json)
    first = provider.prices("USDC", "2026-07-28T12:00:00+00:00")
    second = provider.prices("USDC", "2026-07-28T12:00:03+00:00")

    assert {row.source for row in first} == {"coingecko", "coinbase"}
    assert all(row.source_event_at == "" for row in first)
    assert all(row.response_received_at != "2026-07-28T12:00:00+00:00" for row in first)
    assert second == first
    assert len(calls) == 2

    stale = (datetime.now(UTC) - timedelta(seconds=6)).isoformat()
    provider._cache[("USDC", ("coinbase", "coingecko"))] = [
        StablecoinPrice("USDC", 1.0001, "coingecko", "", stale),
        StablecoinPrice("USDC", 1.0000, "coinbase", "", stale),
    ]
    third = provider.prices("USDC", "2026-07-28T12:00:04+00:00")

    assert {row.source for row in third} == {"coingecko", "coinbase"}
    assert len(calls) == 4


def test_cross_stable_without_price_data_remains_research_only() -> None:
    result = evaluate_stablecoin_route(
        long_collateral="USDC",
        short_collateral="USDT",
        provider=None,
        observed_at="2026-07-28T12:00:00+00:00",
        reference_notional=1_000.0,
        funding_net_before_stablecoin_reserve=12.0,
    )

    assert result["status"] == "RESEARCH_ONLY"
    assert "stablecoin_price_provider_unavailable" in result["blockers"]


def test_same_stable_route_does_not_receive_cross_reserve_twice() -> None:
    result = evaluate_stablecoin_route(
        long_collateral="USDT",
        short_collateral="USDT",
        provider=None,
        observed_at="2026-07-28T12:00:00+00:00",
        reference_notional=1_000.0,
        funding_net_before_stablecoin_reserve=12.0,
    )

    assert not result["cross_stable"]
    assert result["stablecoin_reserve_bps"] == 0.0
    assert result["funding_net_after_stablecoin_reserve"] == pytest.approx(12.0)


@pytest.mark.parametrize("asset", ["USDe", "DAI", "USDT0", "UNKNOWN"])
def test_untrusted_same_asset_stable_route_is_blocked(asset: str) -> None:
    result = evaluate_stablecoin_route(
        long_collateral=asset,
        short_collateral=asset,
        provider=None,
        observed_at="2026-07-28T12:00:00+00:00",
        reference_notional=1_000.0,
        funding_net_before_stablecoin_reserve=12.0,
    )

    assert result["status"] == "RESEARCH_ONLY"
    assert result["compatible"] is False
    assert "same_asset_collateral_not_trusted" in result["blockers"]


@pytest.mark.parametrize("asset", ["USD", "USDT", "USDC"])
def test_trusted_same_asset_stable_route_is_allowed(asset: str) -> None:
    result = evaluate_stablecoin_route(
        long_collateral=asset,
        short_collateral=asset,
        provider=None,
        observed_at="2026-07-28T12:00:00+00:00",
        reference_notional=1_000.0,
        funding_net_before_stablecoin_reserve=12.0,
    )

    assert result["status"] == "PASS"
    assert result["funding_net_after_stablecoin_reserve"] == pytest.approx(12.0)


def test_usde_is_not_usd_major_stable() -> None:
    assert collateral_family("USDC") == USD_MAJOR_STABLE
    assert collateral_family("USDT") == USD_MAJOR_STABLE
    assert collateral_family("USDe") != USD_MAJOR_STABLE


def test_stablecoin_basis_and_reserve_boundaries() -> None:
    assert stablecoin_basis_bps(1.0015, 0.9985) == pytest.approx(30.0)
    assert stablecoin_reserve_bps(20.0, [10.0] * 10) == pytest.approx(50.0)


def test_points_never_change_trading_net_and_only_tie_break_positive_routes() -> None:
    negative_with_points = {
        "funding_net_after_stablecoin_reserve": -1.0,
        "points_metadata": {"points_per_1000_volume": 1_000.0},
    }
    positive_low_points = {
        "funding_net_after_stablecoin_reserve": 2.0,
        "points_metadata": {"points_per_1000_volume": 1.0},
    }
    positive_high_points = {
        "funding_net_after_stablecoin_reserve": 2.0,
        "points_metadata": {"points_per_1000_volume": 2.0},
    }

    assert funding_net_excluding_points(negative_with_points) == pytest.approx(-1.0)
    ranked = rank_opportunities_with_points_tiebreaker(
        [negative_with_points, positive_low_points, positive_high_points]
    )
    assert ranked[0] is positive_high_points
    assert ranked[1] is positive_low_points
    assert ranked[2] is negative_with_points


def test_unknown_points_metadata_is_typed_low_confidence(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [],
        config=FundingShadowConfig(telegram_enabled=False),
        clock=FakeClock(now),
    )
    opportunity = monitor._shadow_opportunity_for_pair(
        "BTC",
        complete_market(
            venue="binance",
            funding_rate=-0.006,
            settlement=settlement.isoformat(),
            observed_at=now.isoformat(),
        ),
        complete_market(
            venue="bybit",
            funding_rate=0.006,
            settlement=settlement.isoformat(),
            observed_at=now.isoformat(),
        ),
        now,
    )

    metadata = opportunity["points_metadata"]
    assert metadata["status"] == "UNKNOWN"
    assert metadata["source"] is None
    assert metadata["program_name"] is None
    assert metadata["season"] is None
    assert metadata["multiplier"] is None
    assert metadata["eligibility"] is None
    assert metadata["confidence"] == "LOW"
    assert metadata["incentive_program_status"] == "UNKNOWN"


def test_hanging_broad_sweep_venue_returns_partial_results(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    monitor = FundingShadowMonitor(
        store,
        [
            _ShadowClient("risex", collateral="USDC", funding_rate=-0.002, settlement=settlement),
            _ShadowClient("binance", collateral="USDT", funding_rate=0.004, settlement=settlement),
            _ShadowClient("okx", collateral="USDT", funding_rate=0.003, settlement=settlement, delay_seconds=0.2),
        ],
        config=FundingShadowConfig(
            telegram_enabled=False,
            venue_deadline_seconds=0.05,
        ).validated(),
        stablecoin_price_provider=price_provider(now.isoformat()),
        clock=FakeClock(now),
    )
    started = time.monotonic()
    result = monitor.run_once()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert result["shadow_candidates"] == 0
    assert result["research_only_routes"] >= 1
    assert any("timed out" in warning for warning in result["warnings"])


def test_pending_venue_request_prevents_overlapping_second_request(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    slow = _ShadowClient(
        "okx",
        collateral="USDT",
        funding_rate=0.003,
        settlement=settlement,
        delay_seconds=0.3,
    )
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [slow],
        config=FundingShadowConfig(
            telegram_enabled=False,
            venue_deadline_seconds=0.01,
        ).validated(),
        clock=FakeClock(now),
    )

    monitor.run_once()
    second = monitor.run_once()

    assert second["overlapping_call_prevention_count"] >= 1
    assert slow.sweep_calls == 1


def test_broad_sweep_not_repeated_on_each_focused_tick(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    binance = _ShadowClient("binance", funding_rate=-0.006, settlement=settlement)
    bybit = _ShadowClient("bybit", funding_rate=0.006, settlement=settlement)
    okx = _ShadowClient("okx", asset="ETH", funding_rate=0.003, settlement=settlement)
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [binance, bybit, okx],
        config=FundingShadowConfig(
            telegram_enabled=False,
            broad_near_interval_seconds=10.0,
            focused_refresh_cadence_seconds=1.0,
        ).validated(),
        clock=FakeClock(now),
    )

    result = monitor.run(duration_seconds=3.2)

    assert result["broad_sweep_count"] == 1
    assert result["focused_refresh_count"] >= 2
    assert result["request_counts_by_venue"]["okx"] == 1
    assert result["request_counts_by_venue"]["binance"] > 1
    assert result["request_counts_by_endpoint_class"]["funding_sweep"] >= 5


def test_worker_latency_uses_completion_timestamp(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    monitor = FundingShadowMonitor(
        store,
        [_ShadowClient("binance", funding_rate=0.003, settlement=settlement, delay_seconds=0.02)],
        config=FundingShadowConfig(
            telegram_enabled=False,
            venue_deadline_seconds=1.0,
        ).validated(),
        clock=FakeClock(now),
    )

    monitor.run_once()

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            """
            SELECT request_started_at, response_received_at, parsing_completed_at,
                   total_latency_ms, endpoint_class
              FROM funding_shadow_venue_health
             WHERE venue = 'binance'
            """
        ).fetchone()
    assert row[0] is not None
    assert row[1] is not None
    assert row[2] is not None
    assert row[3] >= 10.0
    assert row[4] == "funding_sweep"


def test_adaptive_cadence_boundaries() -> None:
    assert adaptive_broad_sweep_interval_seconds(601) == 30.0
    assert adaptive_broad_sweep_interval_seconds(600) == 10.0
    assert adaptive_broad_sweep_interval_seconds(120) == 5.0
    assert adaptive_broad_sweep_interval_seconds(59) == 5.0


def test_catalog_metadata_is_not_called_on_each_broad_sweep(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    client_a = _ShadowClient("risex", collateral="USDC", funding_rate=-0.002, settlement=settlement)
    client_b = _ShadowClient("binance", collateral="USDT", funding_rate=0.004, settlement=settlement)
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [client_a, client_b],
        config=FundingShadowConfig(telegram_enabled=False),
        stablecoin_price_provider=price_provider(now.isoformat()),
        clock=FakeClock(now),
    )

    monitor.run_once()
    monitor.run_once()

    assert client_a.catalog_calls == 1
    assert client_b.catalog_calls == 1
    assert client_a.sweep_calls == 2
    assert client_b.sweep_calls == 2


def test_one_watch_route_does_not_suppress_another_opportunity(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [
            _ShadowClient("risex", collateral="USDC", funding_rate=-0.002, settlement=settlement),
            _ShadowClient("binance", collateral="USDT", funding_rate=0.004, settlement=settlement),
            _ShadowClient("bybit", collateral="USDT", funding_rate=0.003, settlement=settlement),
        ],
        config=FundingShadowConfig(telegram_enabled=False),
        stablecoin_price_provider=price_provider(now.isoformat()),
        clock=FakeClock(now),
    )

    result = monitor.run_once()

    assert result["opportunities"] >= 2


def test_inventory_covers_all_twelve_primary_venues() -> None:
    fixture = json.loads(
        Path("tests/fixtures/funding_contracts/dex_shadow_primary.json").read_text()
    )
    inventory = mandatory_shadow_inventory(
        fixture["markets"],
        registered_venues=fixture["primary_venues"],
    )

    assert {row["venue"] for row in inventory} == set(PRIMARY_SHADOW_VENUES)
    risex_assets = {
        row["canonical_asset"]
        for row in fixture["markets"]
        if row["venue"] == "risex"
    }
    assert {"BTC", "ETH", "SOL"}.issubset(risex_assets)


def test_dex_profile_records_mandatory_risex_health_on_fetch_failure(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    monitor = FundingShadowMonitor(
        store,
        [
            _FailingShadowClient("risex", environment="mainnet"),
            _ShadowClient(
                "binance",
                funding_rate=0.002,
                settlement=now + timedelta(seconds=30),
            ),
        ],
        config=FundingShadowConfig(telegram_enabled=False),
        clock=FakeClock(now),
    )

    result = monitor.run_once()

    assert result["mandatory_unavailable"] == 1
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            """
            SELECT status, environment
              FROM funding_shadow_venue_health
             WHERE venue = 'risex'
            """
        ).fetchone()
    assert row == ("MANDATORY_UNAVAILABLE", "mainnet")


def test_shadow_monitor_does_not_use_monitor_environment_as_client_identity(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    monitor = FundingShadowMonitor(
        store,
        [_FailingShadowClient("risex", environment="")],
        config=FundingShadowConfig(environment="mainnet", telegram_enabled=False),
        clock=FakeClock(now),
    )

    monitor.run_once()

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            """
            SELECT status, environment
              FROM funding_shadow_venue_health
             WHERE venue = 'risex'
            """
        ).fetchone()
    assert row == ("MANDATORY_UNAVAILABLE", "unknown")


def test_shadow_monitor_endpoint_identity_overrides_payload_environment(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    client = _ShadowClient(
        "binance",
        environment="testnet",
        settlement=now + timedelta(seconds=30),
    )
    client.endpoint_identity = build_endpoint_identity(
        venue="binance",
        base_url="https://fapi.binance.com",
        requested_environment="mainnet",
    )
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [client],
        config=FundingShadowConfig(environment="testnet", telegram_enabled=False),
        clock=FakeClock(now),
    )

    markets, health_rows, warnings = monitor.broad_funding_sweep(now.isoformat())

    assert warnings == []
    assert markets[0]["environment"] == "mainnet"
    assert markets[0]["environment_verified"] is True
    assert health_rows[0]["environment"] == "mainnet"


def test_strict_required_venue_validation_returns_nonzero(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "shadow.sqlite"
    monkeypatch.setattr(
        "smart_money_radar.cli.shadow_monitor_clients",
        lambda _args: [_FailingShadowClient("risex", environment="mainnet")],
    )
    code = main(
        [
            "--db",
            str(db_path),
            "funding-shadow-monitor",
            "--duration-seconds",
            "0.1",
            "--no-telegram",
            "--strict-required-venues",
        ]
    )

    assert code == 1


def test_missing_timestamps_fail_closed_instead_of_becoming_now(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.006,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=0.006,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )
    long_market.pop("source_event_at")
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [],
        config=FundingShadowConfig(telegram_enabled=False),
        clock=FakeClock(now),
    )

    opportunity = monitor._shadow_opportunity_for_pair("BTC", long_market, short_market, now)

    assert opportunity["status"] == "DATA_STALE"
    assert "long_source_event_timestamp_missing" in opportunity["blockers"]


def test_stale_response_blocks_candidate() -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    stale = (now - timedelta(seconds=10)).isoformat()
    settlement = now + timedelta(seconds=30)
    long_market = complete_market(
        venue="binance",
        funding_rate=-0.006,
        settlement=settlement.isoformat(),
        observed_at=stale,
    )
    short_market = complete_market(
        venue="bybit",
        funding_rate=0.006,
        settlement=settlement.isoformat(),
        observed_at=now.isoformat(),
    )

    opportunity = build_settlement_capture_opportunity(
        long_market=long_market,
        short_market=short_market,
        now=now,
        target_notional=500.0,
        max_response_age_seconds=5.0,
        max_source_age_seconds=60.0,
    )

    assert "long_response_timestamp_stale" in opportunity["blockers"]


def test_stale_shadow_result_does_not_overwrite_fresh_opportunity(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    fresh = {
        "opportunity_key": "opp",
        "environment": "mainnet",
        "profile": "dex_shadow",
        "canonical_asset": "BTC",
        "status": "SHADOW_CANDIDATE",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "settlement_at": "2026-07-28T12:01:00+00:00",
        "settlement_skew_seconds": 0.0,
        "seconds_until_settlement": 60.0,
        "preliminary_gross_funding": 10.0,
        "funding_net_excluding_points": 9.0,
        "observed_at": "2026-07-28T12:00:30+00:00",
    }
    stale = {**fresh, "status": "DATA_STALE", "observed_at": "2026-07-28T12:00:00+00:00"}

    store.upsert_funding_shadow_opportunity(fresh)
    store.upsert_funding_shadow_opportunity(stale)

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT status, last_observed_at FROM funding_shadow_opportunities WHERE opportunity_key='opp'"
        ).fetchone()
    assert row == ("SHADOW_CANDIDATE", "2026-07-28T12:00:30+00:00")


def test_research_only_routes_do_not_create_individual_alerts(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    routes = [
        _ShadowClient(
            f"venue{i}",
            funding_rate=-0.001,
            settlement=settlement,
            quantity_step=None,
        )
        for i in range(20)
    ]
    store = SQLiteStore(tmp_path / "radar.sqlite")
    monitor = FundingShadowMonitor(
        store,
        routes,
        config=FundingShadowConfig(telegram_enabled=False),
        clock=FakeClock(now),
    )

    result = monitor.run_once()
    counts = store.funding_shadow_counts()

    assert result["research_only_routes"] >= 1
    assert result["telegram_individual_attempted"] == 0
    assert counts["alerts"] == 0


def test_sent_alert_is_not_repeated_after_restart(tmp_path) -> None:
    class FakeNotifier:
        def __init__(self) -> None:
            self.sent = 0
            self.messages: list[str] = []

        def send(self, message: str):
            self.sent += 1
            self.messages.append(message)
            return type("Result", (), {"status": "sent", "error": None})()

    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=30)
    db_path = tmp_path / "radar.sqlite"
    clients = [
        _ShadowClient("binance", funding_rate=-0.006, settlement=settlement),
        _ShadowClient("bybit", funding_rate=0.006, settlement=settlement),
    ]
    notifier = FakeNotifier()
    first = FundingShadowMonitor(
        SQLiteStore(db_path),
        clients,
        config=FundingShadowConfig(telegram_enabled=True),
        notifier=notifier,
        clock=FakeClock(now),
    )
    second = FundingShadowMonitor(
        SQLiteStore(db_path),
        clients,
        config=FundingShadowConfig(telegram_enabled=True),
        notifier=notifier,
        clock=FakeClock(now),
    )

    first.run_once()
    second.run_once()

    individual_alerts = [
        message for message in notifier.messages if "SHADOW FUNDING WINDOW" in message
    ]
    assert len(individual_alerts) == 1


def test_retryable_shadow_alert_retries_only_after_next_retry(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    opportunity = {
        "opportunity_key": "alert-opp",
        "environment": "mainnet",
        "profile": "dex_shadow",
        "canonical_asset": "BTC",
        "status": "SHADOW_CANDIDATE",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "settlement_at": "2026-07-28T12:01:00+00:00",
        "settlement_skew_seconds": 0.0,
        "seconds_until_settlement": 60.0,
        "preliminary_gross_funding": 10.0,
        "funding_net_excluding_points": 9.0,
        "observed_at": "2026-07-28T12:00:00+00:00",
    }
    store.upsert_funding_shadow_opportunity(opportunity)
    row = {
        "alert_key": "retry-alert",
        "opportunity_key": "alert-opp",
        "environment": "mainnet",
        "status": "SHADOW_CANDIDATE",
        "message": "SHADOW FUNDING WINDOW",
        "telegram_status": "queued",
        "payload": {},
    }
    started = datetime(2026, 7, 28, 12, tzinfo=UTC)

    first = store.claim_funding_shadow_alert(row, now=started)
    store.update_funding_shadow_alert_status(
        "retry-alert",
        "failed",
        "https://api.telegram.org/bot123:ABC/sendMessage api_key=SECRET",
        retry_delay_seconds=60.0,
        now=started,
    )
    blocked = store.claim_funding_shadow_alert(
        row,
        now=started + timedelta(seconds=30),
    )
    retried = store.claim_funding_shadow_alert(
        row,
        now=started + timedelta(seconds=61),
    )

    with sqlite3.connect(store.db_path) as conn:
        state, attempts, error = conn.execute(
            """
            SELECT state, attempt_count, last_error_redacted
              FROM funding_shadow_alerts
             WHERE alert_key = 'retry-alert'
            """
        ).fetchone()
    assert first is not None
    assert blocked is None
    assert retried == first
    assert state == "CLAIMED"
    assert attempts == 2
    assert "123:ABC" not in error
    assert "SECRET" not in error


def test_active_claimed_shadow_alert_prevents_duplicate_claim(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    opportunity = {
        "opportunity_key": "concurrent-opp",
        "environment": "mainnet",
        "profile": "dex_shadow",
        "canonical_asset": "BTC",
        "status": "SHADOW_CANDIDATE",
        "long_venue": "binance",
        "long_symbol": "BTCUSDT",
        "short_venue": "bybit",
        "short_symbol": "BTCUSDT",
        "settlement_at": "2026-07-28T12:01:00+00:00",
        "settlement_skew_seconds": 0.0,
        "seconds_until_settlement": 60.0,
        "preliminary_gross_funding": 10.0,
        "funding_net_excluding_points": 9.0,
        "observed_at": "2026-07-28T12:00:00+00:00",
    }
    store.upsert_funding_shadow_opportunity(opportunity)
    row = {
        "alert_key": "concurrent-alert",
        "opportunity_key": "concurrent-opp",
        "environment": "mainnet",
        "status": "SHADOW_CANDIDATE",
        "message": "SHADOW FUNDING WINDOW",
        "telegram_status": "queued",
        "payload": {},
    }
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)

    first = store.claim_funding_shadow_alert(row, now=now)
    second = store.claim_funding_shadow_alert(row, now=now)

    with sqlite3.connect(store.db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM funding_shadow_alerts").fetchone()[0]
    assert first is not None
    assert second is None
    assert count == 1


def test_fake_paper_delta_causes_safety_violation(tmp_path, monkeypatch) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "radar.sqlite")
    original = store.funding_shadow_paper_safety_snapshot
    calls = {"count": 0}

    def fake_snapshot() -> dict[str, float]:
        calls["count"] += 1
        snapshot = original()
        if calls["count"] >= 2:
            snapshot["funding_paper_orders"] += 1
        return snapshot

    monkeypatch.setattr(store, "funding_shadow_paper_safety_snapshot", fake_snapshot)
    monitor = FundingShadowMonitor(
        store,
        [],
        config=FundingShadowConfig(telegram_enabled=False),
        clock=FakeClock(now),
    )

    result = monitor.run_once()

    assert result["status"] == "safety_violation"
    assert result["paper_safety_deltas"]["funding_paper_orders"] == pytest.approx(1.0)


def test_bare_shadow_cli_uses_isolated_default_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("smart_money_radar.cli.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("smart_money_radar.cli.shadow_monitor_clients", lambda _args: [])

    code = main(["funding-shadow-monitor", "--duration-seconds", "0.1", "--no-telegram"])

    assert code == 0
    assert (tmp_path / "data" / "radar-shadow.sqlite").exists()
    assert not (tmp_path / "data" / "radar.sqlite").exists()


def test_risex_probe_default_public_places_no_orders(tmp_path, monkeypatch) -> None:
    class FakeRiseXClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.venue = "risex"

        def catalog_and_markets(self, observed_at: str):
            market = complete_market(
                venue="risex",
                collateral="USDC",
                environment="testnet",
                observed_at=observed_at,
            )
            return [market], [market], []

    monkeypatch.setattr("smart_money_radar.funding.risex_probe.RiseXFundingClient", FakeRiseXClient)
    code = main(
        [
            "--db",
            str(tmp_path / "probe.sqlite"),
            "funding-risex-probe",
            "--mode",
            "public",
            "--no-telegram",
        ]
    )

    assert code == 0
    with sqlite3.connect(tmp_path / "probe.sqlite") as conn:
        orders_enabled = conn.execute(
            "SELECT orders_enabled FROM funding_semantics_probe_runs"
        ).fetchone()[0]
        payload_json = conn.execute(
            "SELECT payload_json FROM funding_semantics_probe_runs"
        ).fetchone()[0]
        observations = conn.execute(
            "SELECT COUNT(*) FROM funding_semantics_probe_observations"
        ).fetchone()[0]
    assert orders_enabled == 0
    assert observations == 1
    payload = json.loads(payload_json)
    assert payload["market_snapshot_count"] == 1
    assert payload["boundary_event_attempt_count"] == 0
    assert payload["boundary_event_observed_count"] == 0
    assert payload["public_settlement_confirmed_count"] == 0


def test_risex_public_probe_confirms_boundary_only_from_history(
    tmp_path,
    monkeypatch,
) -> None:
    started = datetime(2026, 7, 28, 12, 0, 0, 900_000, tzinfo=UTC)
    scheduled = started + timedelta(seconds=0.25)

    class ProbeClock:
        def __init__(self, current: datetime) -> None:
            self.current = current

        def now(self) -> datetime:
            return self.current

        def sleep(self, seconds: float) -> None:
            self.current += timedelta(seconds=max(0.0, float(seconds)))

    clock = ProbeClock(started)

    class FakeRiseXClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.venue = "risex"
            self.scheduled = scheduled

        def catalog_and_markets(self, observed_at: str):
            market = complete_market(
                venue="risex",
                collateral="USDC",
                environment="testnet",
                settlement=self.scheduled.isoformat(),
                observed_at=observed_at,
            )
            return [market], [market], []

        def funding_history(
            self,
            _symbol: str,
            *,
            start_time_ms: int,
            interval_hours: float,
            observed_at: str,
        ) -> list[dict[str, Any]]:
            del start_time_ms, interval_hours, observed_at
            return [{"funding_at": self.scheduled.isoformat(), "funding_rate": 0.001}]

    monkeypatch.setattr("smart_money_radar.funding.risex_probe.RiseXFundingClient", FakeRiseXClient)

    result = run_risex_funding_probe(
        RiseXProbeConfig(
            db_path=tmp_path / "probe.sqlite",
            mode="public",
            max_wait_seconds=1.0,
        ),
        now_provider=clock.now,
        sleep_func=clock.sleep,
    )

    assert result["status"] == "PUBLIC_BOUNDARY_OBSERVED"
    assert result["market_snapshot_count"] >= 2
    assert result["boundary_event_attempt_count"] == 1
    assert result["boundary_event_observed_count"] == 1
    assert result["public_settlement_confirmed_count"] == 1


def test_testnet_canary_requires_explicit_flag(tmp_path) -> None:
    code = main(
        [
            "--db",
            str(tmp_path / "probe.sqlite"),
            "funding-risex-probe",
            "--mode",
            "testnet-canary",
            "--no-telegram",
        ]
    )

    assert code == 1


def test_testnet_canary_returns_honest_unsupported_without_placeholder(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("RISEX_TESTNET_API_KEY", "test-api-key")
    monkeypatch.setenv("RISEX_TESTNET_API_SECRET", "test-api-secret")

    code = main(
        [
            "--db",
            str(tmp_path / "probe.sqlite"),
            "funding-risex-probe",
            "--mode",
            "testnet-canary",
            "--confirm-testnet-canary",
            "--no-telegram",
        ]
    )

    assert code == 1
    with sqlite3.connect(tmp_path / "probe.sqlite") as conn:
        status, error, payload_json = conn.execute(
            """
            SELECT status, error, payload_json
              FROM funding_semantics_probe_runs
            """
        ).fetchone()
    payload = json.loads(payload_json)
    assert status == "CANARY_UNSUPPORTED"
    assert error == "risex_eip712_session_key_order_and_ledger_flow_not_implemented"
    assert payload["reason"] == error
    assert "private_testnet_order_client_not_implemented" not in json.dumps(payload)


def test_secret_redaction_removes_auth_material() -> None:
    payload = {
        "api_key": "secret",
        "nested": {"signature": "sig", "safe": "value"},
        "items": [{"auth_header": "token"}],
    }

    redacted = redact_secret_payload(payload)

    assert redacted["api_key"] == "<redacted>"
    assert redacted["nested"]["signature"] == "<redacted>"
    assert redacted["nested"]["safe"] == "value"
    assert redacted["items"][0]["auth_header"] == "<redacted>"


def test_fake_full_snapshot_payout_classifies_as_snapshot_candidate() -> None:
    result = classify_risex_funding_semantics_observation(
        position_notional_at_assessment=10.0,
        rate_per_next_settlement=0.01,
        hold_duration_seconds=30.0,
        settlement_interval_seconds=3600.0,
        realized_funding=0.10,
        balance_delta=0.10,
    )

    assert result["classification"] == "snapshot_full_candidate"


def test_fake_prorata_payout_does_not_classify_as_snapshot_candidate() -> None:
    result = classify_risex_funding_semantics_observation(
        position_notional_at_assessment=10.0,
        rate_per_next_settlement=0.01,
        hold_duration_seconds=1800.0,
        settlement_interval_seconds=3600.0,
        realized_funding=0.05,
        balance_delta=0.05,
    )

    assert result["classification"] == "continuous_prorata_candidate"


def test_unknown_adapter_contract_is_research_only() -> None:
    contract = funding_adapter_contract_from_market({"venue": "unknownx"})

    assert contract.status == "RESEARCH_ONLY"
    assert "contract_kind_missing" in contract.reasons


def test_contract_multiplier_cannot_satisfy_quantity_step() -> None:
    market = complete_market(venue="paperx", quantity_step=None)
    contract = funding_adapter_contract_from_market(market)

    assert contract.market_rules.contract_multiplier == pytest.approx(1.0)
    assert contract.market_rules.quantity_step is None
    assert "contract_multiplier_is_not_quantity_step" in contract.reasons


def test_generic_history_cannot_become_realized_cashflow() -> None:
    assert not settlement_cashflow_trusted(
        {"source_type": "funding_history", "source_trust": "legacy_default"}
    )
    assert not settlement_cashflow_trusted(
        {"source_type": "realized_settlement", "source_trust": "unknown"}
    )


def test_official_realized_fixture_has_explicit_trusted_semantics() -> None:
    assert settlement_cashflow_trusted(
        {"source_type": "realized_settlement", "source_trust": "adapter_explicit"}
    )
    assert settlement_cashflow_trusted(
        {"source_type": "account_transaction", "source_trust": "account_ledger"}
    )


def test_shadow_mode_does_not_query_history_or_orderbooks(tmp_path) -> None:
    now = datetime(2026, 7, 28, 12, tzinfo=UTC)
    settlement = now + timedelta(seconds=90)
    client_a = _ShadowClient("risex", collateral="USDC", funding_rate=-0.002, settlement=settlement)
    client_b = _ShadowClient("binance", collateral="USDT", funding_rate=0.004, settlement=settlement)
    monitor = FundingShadowMonitor(
        SQLiteStore(tmp_path / "radar.sqlite"),
        [client_a, client_b],
        config=FundingShadowConfig(telegram_enabled=False),
        stablecoin_price_provider=price_provider(now.isoformat()),
        clock=FakeClock(now),
    )

    monitor.run_once()

    assert client_a.orderbook_calls == 0
    assert client_b.orderbook_calls == 0
    assert client_a.history_calls == 0
    assert client_b.history_calls == 0
