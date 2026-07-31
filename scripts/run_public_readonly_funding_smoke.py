#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
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
from smart_money_radar.funding.trader import PaperBot, PaperBotConfig  # noqa: E402
from smart_money_radar.funding.venue_capabilities import (  # noqa: E402
    apply_declared_venue_capability_contract,
)
from smart_money_radar.storage import SQLiteStore  # noqa: E402


DEFAULT_VENUES = ("binance", "bybit", "okx")


class PublicFocusedSmokeBot(PaperBot):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.telemetry_lock = threading.Lock()
        self.route_telemetry: dict[str, dict[str, Any]] = {}
        self.outer_started = 0

    def _route_key(self, route: dict[str, Any]) -> str:
        return str(route.get("route_key") or "unknown")

    def _update_route_telemetry(self, route_key: str, **fields: Any) -> None:
        with self.telemetry_lock:
            row = self.route_telemetry.setdefault(route_key, {})
            row.update(fields)

    def focused_recheck_route(self, route: dict[str, Any]) -> dict[str, Any] | None:
        route_key = self._route_key(route)
        started = time.perf_counter()
        with self.telemetry_lock:
            self.outer_started += 1
            self.route_telemetry.setdefault(route_key, {})["outer_thread"] = (
                threading.current_thread().name
            )
            self.route_telemetry[route_key]["route_started_at"] = (
                datetime.now(UTC).isoformat()
            )
        try:
            result = super().focused_recheck_route(route)
            self._update_route_telemetry(
                route_key,
                status="success" if result else "no_route",
            )
            return result
        except Exception as exc:
            self._update_route_telemetry(
                route_key,
                status="error",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            self._update_route_telemetry(
                route_key,
                route_latency_seconds=time.perf_counter() - started,
                route_finished_at=datetime.now(UTC).isoformat(),
            )

    def fresh_market_for_route_leg(
        self,
        route: dict[str, Any],
        side: str,
        observed_at: str,
    ):
        route_key = self._route_key(route)
        started = time.perf_counter()
        result = super().fresh_market_for_route_leg(route, side, observed_at)
        with self.telemetry_lock:
            row = self.route_telemetry.setdefault(route_key, {})
            stages = row.setdefault("stages", {})
            stages[f"{side}_market"] = {
                "latency_seconds": time.perf_counter() - started,
                "thread": threading.current_thread().name,
            }
        return result

    def fetch_direct_orderbooks(
        self,
        markets_and_clients: list[tuple[dict[str, Any], Any]],
        observed_at: str,
        **kwargs: Any,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        route_key = str(kwargs.get("route_key") or "unknown")
        started = time.perf_counter()
        result = super().fetch_direct_orderbooks(
            markets_and_clients,
            observed_at,
            **kwargs,
        )
        self._update_route_telemetry(
            route_key,
            orderbook_latency_seconds=time.perf_counter() - started,
            orderbook_count=len(result),
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="exports/public_readonly_smoke/latest.json",
        help="Path for sanitized JSON output.",
    )
    parser.add_argument("--asset", default="BTC")
    parser.add_argument(
        "--venues",
        default=",".join(DEFAULT_VENUES),
        help="Comma-separated public venues to include in route permutations.",
    )
    parser.add_argument("--target-notional", type=float, default=500.0)
    parser.add_argument("--timeout-seconds", type=float, default=8.0)
    parser.add_argument("--focused-route-count", type=int, default=6)
    parser.add_argument("--route-workers", type=int, default=6)
    parser.add_argument("--focused-io-workers", type=int, default=0)
    args = parser.parse_args(argv)

    observed_at = datetime.now(UTC).isoformat()
    venue_names = tuple(
        dict.fromkeys(
            venue.strip().lower()
            for venue in str(args.venues or "").split(",")
            if venue.strip()
        )
    ) or DEFAULT_VENUES
    route_combinations = tuple(
        (long_venue, short_venue)
        for long_venue in venue_names
        for short_venue in venue_names
        if long_venue != short_venue
    )
    markets: dict[str, dict[str, Any]] = {}
    clients: dict[str, Any] = {}
    venue_results: dict[str, Any] = {}
    failures: list[str] = []

    for venue in venue_names:
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
    for long_venue, short_venue in route_combinations:
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

    focused_smoke = _run_focused_concurrency_smoke(
        markets=markets,
        route_combinations=route_combinations,
        asset=str(args.asset).upper(),
        target_notional=float(args.target_notional),
        route_count=int(args.focused_route_count),
        route_workers=int(args.route_workers),
        focused_io_workers=int(args.focused_io_workers),
        timeout_seconds=float(args.timeout_seconds),
    )
    if focused_smoke["status"] != "PASS":
        failures.append(f"focused_concurrency: {focused_smoke.get('reason')}")

    payload = {
        "status": "FAIL" if failures else "PASS",
        "observed_at": observed_at,
        "asset": str(args.asset).upper(),
        "requested_venues": venue_names,
        "venues": venue_results,
        "routes": route_results,
        "focused_concurrency": focused_smoke,
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


def _run_focused_concurrency_smoke(
    *,
    markets: dict[str, dict[str, Any]],
    route_combinations: tuple[tuple[str, str], ...],
    asset: str,
    target_notional: float,
    route_count: int,
    route_workers: int,
    focused_io_workers: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    selected_pairs = [
        pair
        for pair in route_combinations
        if pair[0] in markets and pair[1] in markets
    ][: max(0, route_count)]
    if len(selected_pairs) < route_count:
        return {
            "status": "FAIL",
            "reason": "insufficient_public_markets_for_focused_routes",
            "requested_route_count": route_count,
            "available_route_count": len(selected_pairs),
        }
    start = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="radar-focused-smoke-") as tmp_dir:
        store = SQLiteStore(Path(tmp_dir) / "focused-smoke.sqlite")
        store.init_db()
        bot = PublicFocusedSmokeBot(
            store,
            PaperBotConfig(
                telegram_enabled=False,
                target_notional_per_leg=target_notional,
                hot_route_recheck_workers=route_workers,
                focused_io_workers=focused_io_workers,
                focused_recheck_route_timeout_seconds=timeout_seconds,
                export_dir=Path(tmp_dir) / "exports",
            ),
        )
        for index, (long_venue, short_venue) in enumerate(selected_pairs):
            route = _focused_smoke_route(
                long_market=markets[long_venue],
                short_market=markets[short_venue],
                asset=asset,
                target_notional=target_notional,
                index=index,
            )
            bot.hot_routes[route["route_key"]] = route
        started_perf = time.perf_counter()
        refreshed = bot.refresh_hot_routes()
        elapsed = time.perf_counter() - started_perf
        with store.connect() as connection:
            scan_statuses = [
                dict(row)
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM funding_scans GROUP BY status"
                )
            ]
            running_scan_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM funding_scans WHERE status = 'running'"
                ).fetchone()[0]
            )
            position_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM funding_capture_positions"
                ).fetchone()[0]
            )
            ledger_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM paper_event_ledger"
                ).fetchone()[0]
            )
        bot.shutdown_foreground_executors()
    terminal_count = sum(
        int(row["count"])
        for row in scan_statuses
        if str(row["status"]) in {"success", "failed"}
    )
    status = (
        "PASS"
        if running_scan_count == 0
        and terminal_count == len(selected_pairs)
        and bot.outer_started == len(selected_pairs)
        else "FAIL"
    )
    return {
        "status": status,
        "start_at": start.isoformat(),
        "end_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": elapsed,
        "route_count": len(selected_pairs),
        "route_workers": bot.config.hot_route_recheck_workers,
        "focused_io_workers": bot.config.focused_io_workers,
        "outer_started": bot.outer_started,
        "refreshed_count": len(refreshed),
        "scan_statuses": scan_statuses,
        "remaining_running_scan_count": running_scan_count,
        "position_count": position_count,
        "paper_event_ledger_count": ledger_count,
        "experimental_execution_count": 0,
        "real_orders_sent": False,
        "live_trading_enabled": False,
        "routes": bot.route_telemetry,
    }


def _focused_smoke_route(
    *,
    long_market: dict[str, Any],
    short_market: dict[str, Any],
    asset: str,
    target_notional: float,
    index: int,
) -> dict[str, Any]:
    observed_at = datetime.now(UTC).isoformat()
    route_key = (
        f"public-smoke:{asset}:"
        f"{long_market.get('venue')}:{long_market.get('symbol')}:"
        f"{short_market.get('venue')}:{short_market.get('symbol')}:{index}"
    )
    return {
        "route_key": route_key,
        "route_type": "cex_cex",
        "venue_scope": "cross_venue",
        "status": "watch",
        "canonical_asset": asset,
        "long_venue": long_market.get("venue"),
        "long_symbol": long_market.get("symbol"),
        "short_venue": short_market.get("venue"),
        "short_symbol": short_market.get("symbol"),
        "target_notional": target_notional,
        "observed_at": observed_at,
        "long_next_funding_at": long_market.get("next_funding_at"),
        "short_next_funding_at": short_market.get("next_funding_at"),
        "risk_flags": [],
        "rationale": [],
        "legs": [
            _focused_smoke_leg(long_market, "long", target_notional),
            _focused_smoke_leg(short_market, "short", target_notional),
        ],
        "evidence": {
            "mode": "public_readonly_focused_concurrency_smoke",
            "paper_mode": "VERIFIED_PAPER",
            "experimental_simulation_ready": False,
        },
    }


def _focused_smoke_leg(
    market: dict[str, Any],
    side: str,
    target_notional: float,
) -> dict[str, Any]:
    price = float(market.get("mark_price") or market.get("index_price") or 0.0)
    quantity = target_notional / price if price > 0 else 0.0
    return {
        "side": side,
        "venue": market.get("venue"),
        "symbol": market.get("symbol"),
        "canonical_asset": market.get("canonical_asset"),
        "notional": target_notional,
        "base_quantity": quantity,
        "funding_rate": market.get("funding_rate"),
        "normalized_next_funding_rate": market.get("normalized_next_funding_rate"),
        "funding_interval_hours": market.get("funding_interval_hours"),
        "hourly_funding_rate": market.get("hourly_funding_rate"),
        "funding_rate_kind": market.get("funding_rate_kind"),
        "funding_rate_semantics": market.get("funding_rate_semantics"),
        "funding_rate_unit": market.get("funding_rate_unit"),
        "funding_sign_convention": market.get("funding_sign_convention"),
        "next_funding_at": market.get("next_funding_at"),
        "mark_price": market.get("mark_price"),
        "index_price": market.get("index_price"),
        "quote_asset": market.get("quote_asset"),
        "collateral_asset": market.get("collateral_asset"),
        "contract_type": market.get("contract_type"),
        "contract_kind": market.get("contract_kind"),
        "contract_multiplier": market.get("contract_multiplier"),
        "canonical_unit_multiplier": market.get("canonical_unit_multiplier"),
        "quantity_step": market.get("quantity_step"),
        "min_notional": market.get("min_notional"),
        "min_notional_usd": market.get("min_notional_usd"),
        "fee_rate": market.get("fee_rate") or market.get("taker_fee_rate"),
        "taker_fee_rate": market.get("taker_fee_rate") or market.get("fee_rate"),
        "fee_source": market.get("fee_source"),
        "fee_evidence": market.get("fee_evidence"),
        "fee_observed_at": market.get("fee_observed_at"),
        "fee_reviewed_at": market.get("fee_reviewed_at"),
        "environment_verified": market.get("environment_verified"),
        "endpoint_base_url": market.get("endpoint_base_url"),
        "endpoint_identity_provenance": market.get("endpoint_identity_provenance"),
        "endpoint_client_version": market.get("endpoint_client_version"),
        "endpoint_verified_at": market.get("endpoint_verified_at"),
        "api_product_type": market.get("api_product_type"),
        "market_type": market.get("market_type"),
        "product_type": market.get("product_type"),
        "data_enabled": market.get("data_enabled"),
        "strategy_observation_enabled": market.get("strategy_observation_enabled"),
        "paper_enabled": market.get("paper_enabled"),
        "live_enabled": market.get("live_enabled"),
        "execution_model": market.get("execution_model"),
        "settlement_verification_level": market.get("settlement_verification_level"),
        "position_inclusion_rule": market.get("position_inclusion_rule"),
        "entry_safety_buffer_seconds": market.get("entry_safety_buffer_seconds"),
        "exit_safety_buffer_seconds": market.get("exit_safety_buffer_seconds"),
        "timing_policy_source": market.get("timing_policy_source"),
    }


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
        "quantity_step": market.get("quantity_step"),
        "min_quantity": market.get("min_quantity"),
        "min_notional_usd": market.get("min_notional_usd") or market.get("min_notional"),
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
