from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from smart_money_radar.funding.fees import (
    FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT,
    FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT,
    FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE,
    fee_evidence_status,
    fee_rate_value,
    funding_fee_rate,
)
from smart_money_radar.funding.readiness_policy import modeled_fee_rate
from smart_money_radar.funding.route_identity import (
    ROUTE_IDENTITY_SCHEMA_VERSION,
    canonical_opportunity_key,
    route_identity_summary,
    route_variant_key,
)
from smart_money_radar.paper_bot.helpers import route_entry_key
from smart_money_radar.paper_bot.runtime_v2 import capture_position_id_for_route
from smart_money_radar.storage import SQLiteStore


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _fee_market(
    *,
    kind: str = FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE,
    source_kind: str = "configured_trusted_fee",
    observed_at: str | None = None,
    reviewed_at: str | None = None,
    account_observed_at: str | None = None,
    rate: float = 0.0005,
    fee_scope: str | None = None,
    account_applicability: str | None = None,
    conservative_worst_case: bool = False,
) -> dict:
    evidence = {
        "fee_evidence_kind": kind,
        "source_kind": source_kind,
        "source_identifier": "test-fee-source",
        "trust_status": "CONFIGURED_TRUSTED"
        if kind == FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE
        else "OFFICIAL",
        "venue": "binance",
        "liquidity_role": "taker",
        "market_observed_at": NOW.isoformat(),
        "reviewed_at": reviewed_at or NOW.isoformat(),
        "fee_schedule_reviewed_at": reviewed_at or NOW.isoformat(),
        "environment": "mainnet",
        "product_type": "linear_perpetual",
        "market_type": "linear_perpetual",
        "applicability": "taker",
        "evidence_version": "test-fee-v1",
    }
    if fee_scope is not None:
        evidence["fee_scope"] = fee_scope
    if account_applicability is not None:
        evidence["account_applicability"] = account_applicability
    if conservative_worst_case:
        evidence["conservative_worst_case"] = True
    if observed_at:
        evidence["observed_at"] = observed_at
        evidence["fee_source_observed_at"] = observed_at
    if account_observed_at:
        evidence["account_fee_observed_at"] = account_observed_at
    return {
        "venue": "binance",
        "environment": "mainnet",
        "product_type": "linear_perpetual",
        "market_type": "linear_perpetual",
        "taker_fee_rate": rate,
        "fee_source": source_kind,
        "fee_evidence": evidence,
        "response_received_at": NOW.isoformat(),
    }


def test_static_fee_schedule_ignores_fresh_market_snapshot_after_review_expiration() -> None:
    reviewed = "2026-07-29T00:00:00+00:00"
    market = _fee_market(
        observed_at=NOW.isoformat(),
        reviewed_at=reviewed,
    )

    status = fee_evidence_status(market, "taker", now=NOW)
    modeled = modeled_fee_rate(market, now=NOW)

    assert status["fee_evidence_kind"] == FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE
    assert status["market_observed_at"] == NOW.isoformat()
    assert status["observed_at"] == reviewed
    assert status["fee_schedule_reviewed_at"] == reviewed
    assert status["verified"] is False
    assert status["blocker"] == "taker_fee_evidence_stale"
    assert modeled["verified"] is False
    assert modeled["fee_rate"] > market["taker_fee_rate"]
    assert modeled["uncertainty_reserve_bps"] == pytest.approx(2.0)
    assert "fee_fallback_used" in modeled["risk_flags"]


def test_fresh_fee_evidence_distinguishes_public_and_account_applicability() -> None:
    static_status = fee_evidence_status(_fee_market(reviewed_at=NOW.isoformat()), now=NOW)
    website_schedule_status = fee_evidence_status(
        _fee_market(
            kind="",
            source_kind="official_public_fee_endpoint",
            reviewed_at=NOW.isoformat(),
        ),
        now=NOW,
    )
    public_status = fee_evidence_status(
        _fee_market(
            kind=FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT,
            source_kind="official_public_fee_endpoint",
            observed_at=NOW.isoformat(),
        ),
        now=NOW,
    )
    public_worst_case_status = fee_evidence_status(
        _fee_market(
            kind=FEE_EVIDENCE_KIND_PUBLIC_FEE_ENDPOINT,
            source_kind="official_public_fee_endpoint",
            observed_at=NOW.isoformat(),
            fee_scope="public_worst_case_schedule",
            account_applicability="conservative_worst_case_all_accounts",
            conservative_worst_case=True,
        ),
        now=NOW,
    )
    account_status = fee_evidence_status(
        _fee_market(
            kind=FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT,
            source_kind="official_account_fee_endpoint",
            account_observed_at=NOW.isoformat(),
        ),
        now=NOW,
    )

    assert static_status["verified"] is True
    assert static_status["expires_at"] is not None
    assert website_schedule_status["fee_evidence_kind"] == FEE_EVIDENCE_KIND_REVIEWED_STATIC_SCHEDULE
    assert website_schedule_status["fee_source_observed_at"] is None
    assert website_schedule_status["observed_at"] == NOW.isoformat()
    assert public_status["verified"] is False
    assert public_status["blocker"] == "taker_fee_account_applicability_unverified"
    assert public_status["fee_source_observed_at"] == NOW.isoformat()
    assert public_worst_case_status["verified"] is True
    assert public_worst_case_status["conservative_worst_case"] is True
    assert account_status["verified"] is True
    assert account_status["account_fee_observed_at"] == NOW.isoformat()


def test_stale_account_fee_evidence_and_invalid_taker_values_fail_closed() -> None:
    stale_account = _fee_market(
        kind=FEE_EVIDENCE_KIND_ACCOUNT_ENDPOINT,
        source_kind="official_account_fee_endpoint",
        account_observed_at=(NOW - timedelta(days=2)).isoformat(),
    )
    stale_status = fee_evidence_status(stale_account, now=NOW)
    invalid = _fee_market(rate=-0.0001)

    assert stale_status["verified"] is False
    assert stale_status["blocker"] == "taker_fee_evidence_stale"
    assert fee_rate_value(invalid, "taker") is None
    assert math.isnan(funding_fee_rate(invalid, "taker"))


def test_maker_rebate_does_not_make_taker_path_verified() -> None:
    market = _fee_market(rate=0.0005)
    market.pop("taker_fee_rate")
    market["maker_fee_rate"] = -0.0002

    modeled = modeled_fee_rate(market, now=NOW)

    assert modeled["verified"] is False
    assert modeled["fee_rate"] >= 0.0012
    assert modeled["evidence_status"]["blocker"] == "taker_fee_rate_missing"


def _route(
    *,
    multiplier=1,
    long_ts: str = "2026-07-28T16:00:00Z",
    short_ts: str | None = None,
    product_id: str | None = None,
    collateral: str = "USDT",
    environment: str = "mainnet",
    product_type: str = "linear_perpetual",
    symbol: str = "BTCUSDT",
) -> dict:
    legs = []
    for side, venue in (("long", "binance"), ("short", "bybit")):
        leg = {
            "side": side,
            "venue": venue,
            "environment": environment,
            "symbol": symbol,
            "quote_asset": collateral,
            "collateral_asset": collateral,
            "contract_type": "linear_perpetual",
            "product_type": product_type,
            "contract_multiplier": multiplier,
            "canonical_unit_multiplier": 1,
            "next_funding_at": long_ts if side == "long" else short_ts or long_ts,
        }
        if product_id:
            leg["product_id"] = product_id
        legs.append(leg)
    return {
        "canonical_asset": "BTC",
        "long_venue": "binance",
        "short_venue": "bybit",
        "long_symbol": symbol,
        "short_symbol": symbol,
        "legs": legs,
    }


def test_canonical_identity_normalizes_numbers_timestamps_and_late_product_alias() -> None:
    base = _route(multiplier=1, long_ts="2026-07-28T16:00:00Z")
    same_float = _route(multiplier=1.0, long_ts="2026-07-28T16:00:00+00:00")
    same_text = _route(multiplier="1.000000", long_ts="2026-07-28T19:00:00+03:00")
    enriched = _route(
        multiplier=1,
        long_ts="2026-07-28T16:00:00Z",
        product_id="BTC-USDT-PERP",
    )

    keys = {route_entry_key(base), route_entry_key(same_float), route_entry_key(same_text), route_entry_key(enriched)}
    capture_ids = {
        capture_position_id_for_route(base),
        capture_position_id_for_route(same_float),
        capture_position_id_for_route(same_text),
        capture_position_id_for_route(enriched),
    }

    assert len(keys) == 1
    assert len(capture_ids) == 1
    assert route_identity_summary(base)["route_variant_key"] == route_identity_summary(enriched)["route_variant_key"]
    assert canonical_opportunity_key(base) == canonical_opportunity_key(enriched)


def test_route_variant_separates_collateral_environment_product_type_and_symbol() -> None:
    base = _route()
    variants = {
        "base": route_variant_key(asset="BTC", long_market=base["legs"][0], short_market=base["legs"][1]),
        "collateral": route_variant_key(asset="BTC", long_market=_route(collateral="USDC")["legs"][0], short_market=_route(collateral="USDC")["legs"][1]),
        "environment": route_variant_key(asset="BTC", long_market=_route(environment="testnet")["legs"][0], short_market=_route(environment="testnet")["legs"][1]),
        "product_type": route_variant_key(asset="BTC", long_market=_route(product_type="inverse_perpetual")["legs"][0], short_market=_route(product_type="inverse_perpetual")["legs"][1]),
        "symbol": route_variant_key(asset="BTC", long_market=_route(symbol="BTCUSD")["legs"][0], short_market=_route(symbol="BTCUSD")["legs"][1]),
    }

    assert len(set(variants.values())) == len(variants)


def test_alias_conflict_is_fail_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "radar.sqlite")
    store.init_db()
    first = route_identity_summary(_route(product_id="BTC-USDT-PERP"))
    conflict = route_identity_summary(_route(product_id="BTC-USDT-PERP", symbol="BTCUSDC"))

    assert store.record_product_identity_aliases(first["product_identity_aliases"], canonical_asset="BTC") == []
    blockers = store.record_product_identity_aliases(conflict["product_identity_aliases"], canonical_asset="BTC")

    assert any(blocker.startswith("product_identity_alias_conflict:") for blocker in blockers)
    assert store.funding_capture_active_identity_issues()["alias_conflicts"]


LEGACY_STATES = [
    "DISCOVERED",
    "ARMED",
    "ENTRY_SUBMITTED",
    "OPEN",
    "SETTLEMENT_CROSSED",
    "POST_SETTLEMENT_EVALUATION",
    "HOLDING_NEXT_CYCLE",
    "EXIT_SUBMITTED",
    "CLOSED_PENDING_RECONCILIATION",
    "RECONCILED",
    "UNRECONCILED",
    "CLOSED_REQUIRES_REVIEW",
]


def _create_legacy_capture_schema(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE funding_capture_positions (
                position_id TEXT PRIMARY KEY,
                funding_paper_position_id INTEGER,
                strategy_name TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                canonical_asset TEXT NOT NULL,
                long_venue TEXT NOT NULL,
                long_symbol TEXT NOT NULL,
                short_venue TEXT NOT NULL,
                short_symbol TEXT NOT NULL,
                quantity REAL NOT NULL,
                target_notional REAL NOT NULL,
                state TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                settlements_captured_count INTEGER NOT NULL DEFAULT 0,
                max_settlements INTEGER NOT NULL DEFAULT 4,
                original_entry_spread REAL,
                paper_open_fees REAL NOT NULL DEFAULT 0,
                paper_close_fees REAL NOT NULL DEFAULT 0,
                paper_emergency_unwind_cost REAL NOT NULL DEFAULT 0,
                paper_net_pnl_estimated REAL,
                paper_net_pnl_reconciled REAL,
                config_json TEXT NOT NULL DEFAULT '{}',
                config_hash TEXT,
                code_commit TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE funding_capture_cycles (
                cycle_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL,
                cycle_number INTEGER NOT NULL,
                scheduled_funding_at TEXT NOT NULL,
                long_next_funding_rate_at_decision REAL,
                short_next_funding_rate_at_decision REAL,
                conservative_funding_gross REAL,
                conservative_funding_edge_bps REAL,
                hold_basis_reserve_bps REAL,
                hold_legging_reserve_bps REAL,
                hold_time_reserve_bps REAL,
                hold_liquidity_reserve_bps REAL,
                incremental_hold_cost REAL,
                incremental_hold_net_pnl REAL,
                hold_cost_coverage_ratio REAL,
                paper_net_if_exit_at_decision REAL,
                decision TEXT,
                decision_reason TEXT,
                state TEXT NOT NULL,
                settlement_crossed_at TEXT,
                reconciliation_status TEXT NOT NULL DEFAULT 'PENDING',
                reconciled_funding_pnl REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (position_id, cycle_number),
                UNIQUE (position_id, scheduled_funding_at)
            );
            """
        )


def test_funding_capture_identity_migration_is_backward_compatible_and_idempotent(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    _create_legacy_capture_schema(db_path)
    opened = NOW.isoformat()
    with sqlite3.connect(db_path) as connection:
        for index, state in enumerate(LEGACY_STATES, start=1):
            scheduled_at = f"2026-07-28T16:{index:02d}:00Z"
            route = _route(
                product_id=f"BTC-USDT-PERP-{index}",
                long_ts=scheduled_at,
                symbol=f"BTCUSDT{index}",
            )
            config = {
                "route_key": f"legacy-route-{index}",
                "route_entry_key": f"legacy-entry-{index}",
                "entry_legs": route["legs"],
            }
            position_id = f"legacy-{index:02d}"
            connection.execute(
                """
                INSERT INTO funding_capture_positions (
                    position_id, strategy_name, strategy_version, canonical_asset,
                    long_venue, long_symbol, short_venue, short_symbol, quantity,
                    target_notional, state, opened_at, config_json, created_at, updated_at
                )
                VALUES (?, 'synchronized_funding_capture', 'synchronized_funding_capture_v2',
                        'BTC', 'binance', 'BTCUSDT', 'bybit', 'BTCUSDT',
                        5.0, 500.0, ?, ?, ?, ?, ?)
                """,
                (position_id, state, opened, json.dumps(config, sort_keys=True), opened, opened),
            )
            connection.execute(
                """
                INSERT INTO funding_capture_cycles (
                    cycle_id, position_id, cycle_number, scheduled_funding_at,
                    state, created_at, updated_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (f"{position_id}:1", position_id, scheduled_at, state, opened, opened),
            )

    store = SQLiteStore(db_path)
    store.init_db()
    first = sqlite3.connect(db_path).execute(
        """
        SELECT position_id, route_family_key, route_variant_key,
               canonical_opportunity_key, identity_schema_version,
               product_identity_aliases_json, legacy_route_key,
               legacy_route_entry_key, config_json
        FROM funding_capture_positions
        ORDER BY position_id
        """
    ).fetchall()
    store.init_db()
    second = sqlite3.connect(db_path).execute(
        """
        SELECT position_id, route_family_key, route_variant_key,
               canonical_opportunity_key, identity_schema_version,
               product_identity_aliases_json, legacy_route_key,
               legacy_route_entry_key, config_json
        FROM funding_capture_positions
        ORDER BY position_id
        """
    ).fetchall()

    assert first == second
    assert len(first) == len(LEGACY_STATES)
    expected_family = route_identity_summary(_route())["route_family_key"]
    for row in first:
        assert row[1] == expected_family
        assert row[2]
        assert row[3]
        assert row[4] == ROUTE_IDENTITY_SCHEMA_VERSION
        assert row[6].startswith("legacy-route-")
        assert row[7].startswith("legacy-entry-")
        assert json.loads(row[8])["identity_schema_version"] == ROUTE_IDENTITY_SCHEMA_VERSION
    assert store.funding_capture_active_identity_issues()["ok"] is True
