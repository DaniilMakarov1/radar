from __future__ import annotations

import time
import json
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
from smart_money_radar.funding.stablecoins import (
    StablecoinPrice,
    StaticStablecoinPriceProvider,
    evaluate_stablecoin_route,
    stablecoin_basis_bps,
    stablecoin_reserve_bps,
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
        "funding_rate": funding_rate,
        "normalized_next_funding_rate": funding_rate,
        "funding_interval_hours": 1.0,
        "hourly_funding_rate": funding_rate,
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
        "supports_perpetuals": True,
        "is_linear_contract": True,
        "supports_discrete_funding": True,
        "supports_public_shadow_mode": True,
        "position_inclusion_rule": "perp_position_at_settlement",
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

    assert result["shadow_candidates"] >= 1
    assert counts["opportunities"] >= 1
    assert counts["observations"] >= 2
    assert counts["paper_positions"] == 0
    assert counts["paper_orders"] == 0
    assert counts["paper_accounts"] == 0


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
    assert result["shadow_candidates"] >= 1
    assert any("timed out" in warning for warning in result["warnings"])


def test_adaptive_cadence_boundaries() -> None:
    assert adaptive_broad_sweep_interval_seconds(601) == 30.0
    assert adaptive_broad_sweep_interval_seconds(600) == 10.0
    assert adaptive_broad_sweep_interval_seconds(120) == 5.0
    assert adaptive_broad_sweep_interval_seconds(59) == 1.0


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
