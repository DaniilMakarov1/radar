#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from smart_money_radar.funding.fees import fee_evidence_status  # noqa: E402
from smart_money_radar.funding.normalization import normalize_orderbook_canonical_units  # noqa: E402
from smart_money_radar.funding.readiness_policy import (  # noqa: E402
    EvaluationMode,
    evaluate_synchronized_route,
    modeled_fee_rate,
)
from smart_money_radar.funding.synchronized_market_contract import (  # noqa: E402
    normalize_synchronized_capture_market,
)
from smart_money_radar.funding.trader import funding_client_for_venue  # noqa: E402
from smart_money_radar.funding.venue_capabilities import (  # noqa: E402
    apply_declared_venue_capability_contract,
)


DEFAULT_VENUES = ("binance", "bybit", "okx")
ROUTE_COMBINATIONS = (
    ("binance", "bybit"),
    ("bybit", "binance"),
    ("binance", "okx"),
    ("okx", "binance"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="exports/public_readonly_smoke/latest.json",
        help="Path for sanitized JSON output.",
    )
    parser.add_argument("--asset", default="BTC")
    parser.add_argument("--target-notional", type=float, default=500.0)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    args = parser.parse_args(argv)

    observed_at = datetime.now(UTC).isoformat()
    markets: dict[str, dict[str, Any]] = {}
    clients: dict[str, Any] = {}
    venue_results: dict[str, Any] = {}
    failures: list[str] = []

    for venue in DEFAULT_VENUES:
        try:
            client = funding_client_for_venue(
                venue,
                fast=True,
                timeout_seconds=float(args.timeout_seconds),
            )
            if client is None:
                raise RuntimeError(f"{venue} client unavailable")
            clients[venue] = client
            catalog, catalog_markets, warnings = client.catalog_and_markets(observed_at)
            selected = _select_market(catalog_markets, args.asset)
            if selected is None:
                raise RuntimeError(f"{venue} {args.asset} market unavailable")
            instrument = _matching_instrument(catalog, selected)
            previous = {**(instrument or {}), **selected}
            snapshot = client.market_snapshot(
                str(selected["symbol"]),
                str(selected["canonical_asset"]),
                observed_at,
                previous,
            )
            merged = {**previous, **snapshot}
            merged = apply_declared_venue_capability_contract(merged)
            merged = normalize_synchronized_capture_market(
                merged,
                observed_at=observed_at,
            )
            book = client.orderbook(str(merged["symbol"]), observed_at, 100)
            book = normalize_orderbook_canonical_units(
                book,
                float(merged.get("canonical_unit_multiplier") or 1.0),
            )
            merged.update(
                {
                    "best_bid": book.get("best_bid"),
                    "best_ask": book.get("best_ask"),
                    "bids": book.get("bids") or [],
                    "asks": book.get("asks") or [],
                    "orderbook_response_received_at": book.get("response_received_at")
                    or observed_at,
                    "orderbook_event_time": book.get("orderbook_event_time")
                    or observed_at,
                    "orderbook_depth_available": bool(book.get("bids") and book.get("asks")),
                    "supports_orderbook_depth": bool(book.get("bids") and book.get("asks")),
                    "response_received_at": snapshot.get("response_received_at")
                    or snapshot.get("observed_at")
                    or observed_at,
                }
            )
            markets[venue] = merged
            fee_status = fee_evidence_status(merged, now=datetime.fromisoformat(observed_at))
            venue_results[venue] = {
                "status": "PASS",
                "catalog_count": len(catalog),
                "market_snapshot": _market_summary(merged),
                "orderbook": {
                    "best_bid": book.get("best_bid"),
                    "best_ask": book.get("best_ask"),
                    "bid_levels": len(book.get("bids") or []),
                    "ask_levels": len(book.get("asks") or []),
                    "response_received_at": book.get("response_received_at"),
                },
                "endpoint_identity": _endpoint_identity(merged),
                "market_normalization": _market_normalization(merged),
                "settlement_contract": _settlement_contract(merged),
                "fee_evidence": _fee_summary(fee_status, merged),
                "modeled_fee_rate": _modeled_fee_summary(merged, observed_at),
                "warnings": warnings,
            }
        except Exception as exc:
            failures.append(f"{venue}: {type(exc).__name__}: {exc}")
            venue_results[venue] = {
                "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
            }

    route_results: dict[str, Any] = {}
    for long_venue, short_venue in ROUTE_COMBINATIONS:
        key = f"{long_venue}->{short_venue}"
        if long_venue not in markets or short_venue not in markets:
            route_results[key] = {
                "status": "NOT_RUN",
                "reason": "one_or_more_venues_failed",
            }
            continue
        readiness = evaluate_synchronized_route(
            long_market={**markets[long_venue], "side": "long"},
            short_market={**markets[short_venue], "side": "short"},
            target_notional=float(args.target_notional),
            mode=EvaluationMode.VERIFIED_PAPER,
            clients_by_venue={
                long_venue: clients[long_venue],
                short_venue: clients[short_venue],
            },
        )
        route_results[key] = {
            "status": "PASS",
            "readiness_level": readiness.get("readiness_level"),
            "verified_paper_ready": readiness.get("verified_paper_ready"),
            "experimental_simulation_ready": readiness.get("experimental_simulation_ready"),
            "mode_blockers": readiness.get("mode_blockers"),
            "verified_blockers": readiness.get("verified_paper_blockers"),
            "hard_blockers": readiness.get("hard_blockers"),
            "risk_flags": readiness.get("risk_flags"),
            "settlement_semantics_status": readiness.get("settlement_semantics_status"),
            "funding_cashflow_status": readiness.get("funding_cashflow_status"),
            "execution_status": readiness.get("execution_status"),
            "fee_evidence": {
                "long": _modeled_status_summary(readiness, "long"),
                "short": _modeled_status_summary(readiness, "short"),
            },
        }

    payload = {
        "status": "FAIL" if failures else "PASS",
        "observed_at": observed_at,
        "asset": str(args.asset).upper(),
        "venues": venue_results,
        "routes": route_results,
        "failures": failures,
        "notes": {
            "credentials_used": False,
            "raw_responses_included": False,
            "real_orders_sent": False,
            "live_trading_enabled": False,
        },
    }
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "output": str(output)}, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 1


def _select_market(markets: list[dict[str, Any]], asset: str) -> dict[str, Any] | None:
    target = str(asset or "").upper()
    candidates = [
        market
        for market in markets
        if str(market.get("canonical_asset") or "").upper() == target
    ]
    return candidates[0] if candidates else None


def _matching_instrument(
    catalog: list[dict[str, Any]],
    market: dict[str, Any],
) -> dict[str, Any] | None:
    symbol = str(market.get("symbol") or "")
    venue = str(market.get("venue") or "")
    for row in catalog:
        if str(row.get("symbol") or "") == symbol and str(row.get("venue") or "") == venue:
            return row
    return None


def _market_summary(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "venue": market.get("venue"),
        "symbol": market.get("symbol"),
        "canonical_asset": market.get("canonical_asset"),
        "quote_asset": market.get("quote_asset"),
        "collateral_asset": market.get("collateral_asset"),
        "product_type": market.get("product_type"),
        "contract_type": market.get("contract_type") or market.get("contract_kind"),
        "contract_multiplier": market.get("contract_multiplier"),
        "canonical_unit_multiplier": market.get("canonical_unit_multiplier"),
        "funding_rate_kind": market.get("funding_rate_kind"),
        "next_funding_at": market.get("next_funding_at"),
        "mark_price": market.get("mark_price"),
        "index_price": market.get("index_price"),
        "response_received_at": market.get("response_received_at"),
    }


def _endpoint_identity(market: dict[str, Any]) -> dict[str, Any]:
    return {
        "venue": market.get("venue"),
        "environment": market.get("environment"),
        "environment_verified": market.get("environment_verified"),
        "endpoint_base_url": market.get("endpoint_base_url"),
        "endpoint_identity_provenance": market.get("endpoint_identity_provenance"),
        "endpoint_client_version": market.get("endpoint_client_version"),
        "endpoint_verified_at": market.get("endpoint_verified_at"),
    }


def _market_normalization(market: dict[str, Any]) -> dict[str, Any]:
    evidence = market.get("normalization_evidence") or {}
    return {
        "normalized_next_funding_rate": market.get("normalized_next_funding_rate"),
        "funding_rate_unit": market.get("funding_rate_unit"),
        "funding_sign_convention": market.get("funding_sign_convention"),
        "normalization_source_kind": evidence.get("source_kind"),
        "normalization_source_identifier": evidence.get("source_identifier"),
        "normalization_reviewed_at": evidence.get("reviewed_at"),
        "normalization_observed_at": evidence.get("observed_at"),
    }


def _settlement_contract(market: dict[str, Any]) -> dict[str, Any]:
    evidence = market.get("settlement_contract_evidence") or {}
    return {
        "verification_level": market.get("settlement_verification_level"),
        "position_inclusion_rule": market.get("position_inclusion_rule"),
        "settlement_accrual_model": market.get("settlement_accrual_model"),
        "blockers": market.get("settlement_contract_blockers") or [],
        "evidence_checked_at": evidence.get("evidence_checked_at"),
        "next_settlement_source": evidence.get("next_settlement_source"),
    }


def _fee_summary(status: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    return {
        "fee_evidence_kind": status.get("fee_evidence_kind"),
        "source_kind": status.get("source_kind"),
        "source_identifier": status.get("source_identifier"),
        "market_observed_at": status.get("market_observed_at"),
        "fee_source_observed_at": status.get("fee_source_observed_at"),
        "fee_schedule_reviewed_at": status.get("fee_schedule_reviewed_at"),
        "account_fee_observed_at": status.get("account_fee_observed_at"),
        "observed_at": status.get("observed_at"),
        "reviewed_at": status.get("reviewed_at"),
        "age_seconds": status.get("age_seconds"),
        "expires_at": status.get("expires_at"),
        "verified": status.get("verified"),
        "fallback_required": status.get("fallback_required"),
        "uncertainty_reserve_required": status.get("uncertainty_reserve_required"),
        "blocker": status.get("blocker"),
        "market_snapshot_response_received_at": market.get("response_received_at"),
    }


def _modeled_fee_summary(market: dict[str, Any], observed_at: str) -> dict[str, Any]:
    modeled = modeled_fee_rate(market, now=datetime.fromisoformat(observed_at))
    return {
        "fee_rate": modeled.get("fee_rate"),
        "verified": modeled.get("verified"),
        "risk_flags": modeled.get("risk_flags"),
        "uncertainty_reserve_bps": modeled.get("uncertainty_reserve_bps"),
        "evidence_status": _fee_summary(modeled.get("evidence_status") or {}, market),
    }


def _modeled_status_summary(readiness: dict[str, Any], side: str) -> dict[str, Any]:
    modeled = (
        ((readiness.get("economics") or {}).get("modeled_fee_rates") or {}).get(side)
        or {}
    )
    status = modeled.get("evidence_status") or {}
    return {
        "fee_evidence_kind": status.get("fee_evidence_kind"),
        "source_identifier": status.get("source_identifier"),
        "observed_at": status.get("observed_at"),
        "fee_schedule_reviewed_at": status.get("fee_schedule_reviewed_at"),
        "fee_source_observed_at": status.get("fee_source_observed_at"),
        "account_fee_observed_at": status.get("account_fee_observed_at"),
        "age_seconds": status.get("age_seconds"),
        "expires_at": status.get("expires_at"),
        "verified": status.get("verified"),
        "fallback_required": status.get("fallback_required"),
        "uncertainty_reserve_required": status.get("uncertainty_reserve_required"),
        "blocker": status.get("blocker"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
