from __future__ import annotations

import hashlib
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.adapter_contracts import (
    PRIMARY_SHADOW_VENUES,
    funding_adapter_contract_from_market,
    mandatory_shadow_inventory,
)
from smart_money_radar.funding.adapters import FundingDataError, FundingVenueClient
from smart_money_radar.funding.normalization import normalize_catalog_canonical_units
from smart_money_radar.funding.stablecoins import (
    StablecoinPriceProvider,
    evaluate_stablecoin_route,
)
from smart_money_radar.funding.strategy_synchronized_funding import (
    gross_funding_pnl,
    settlement_skew_seconds,
)
from smart_money_radar.paper_bot.clock import SystemClock
from smart_money_radar.paper_bot.helpers import parse_iso
from smart_money_radar.storage import SQLiteStore


SHADOW_STATUSES = {
    "DISCOVERED",
    "WATCH",
    "SHADOW_CANDIDATE",
    "RESEARCH_ONLY",
    "EXPIRED",
    "DATA_STALE",
    "CAPABILITY_BLOCKED",
}


@dataclass(frozen=True)
class FundingShadowConfig:
    profile: str = "dex_shadow"
    environment: str = "mainnet"
    target_notional: float = 500.0
    catalog_refresh_seconds: float = 1_800.0
    venue_deadline_seconds: float = 2.0
    periodic_summary_seconds: float = 900.0
    unchanged_opportunity_cooldown_seconds: float = 900.0
    telegram_enabled: bool = True
    max_workers: int = 12

    def validated(self) -> "FundingShadowConfig":
        environment = str(self.environment or "mainnet").strip().lower()
        if environment not in {"mainnet", "testnet"}:
            environment = "mainnet"
        return FundingShadowConfig(
            profile=str(self.profile or "dex_shadow"),
            environment=environment,
            target_notional=max(50.0, float(self.target_notional)),
            catalog_refresh_seconds=max(60.0, float(self.catalog_refresh_seconds)),
            venue_deadline_seconds=max(0.1, min(float(self.venue_deadline_seconds), 10.0)),
            periodic_summary_seconds=max(30.0, float(self.periodic_summary_seconds)),
            unchanged_opportunity_cooldown_seconds=max(
                60.0,
                float(self.unchanged_opportunity_cooldown_seconds),
            ),
            telegram_enabled=bool(self.telegram_enabled),
            max_workers=max(1, min(int(self.max_workers), 32)),
        )


class ShadowAlertDeduper:
    def __init__(self, cooldown_seconds: float = 900.0) -> None:
        self.cooldown_seconds = max(60.0, float(cooldown_seconds))
        self.last_sent_monotonic_by_key: dict[str, float] = {}

    def alert_key(self, opportunity: dict[str, Any]) -> str:
        economics = opportunity.get("stablecoin_risk") or {}
        try:
            bucket_value = float(
                economics.get("funding_net_after_stablecoin_reserve")
                or opportunity.get("preliminary_gross_funding")
                or 0.0
            )
        except (TypeError, ValueError):
            bucket_value = 0.0
        bucket = round(bucket_value, 1)
        blockers = ",".join(sorted(opportunity.get("blockers") or []))
        raw = "|".join(
            [
                str(opportunity.get("opportunity_key") or ""),
                str(opportunity.get("status") or ""),
                str(bucket),
                blockers,
                str(opportunity.get("settlement_at") or ""),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def should_send(
        self,
        opportunity: dict[str, Any],
        *,
        now_monotonic: float,
    ) -> tuple[bool, str]:
        key = self.alert_key(opportunity)
        last = self.last_sent_monotonic_by_key.get(key)
        if last is not None and now_monotonic - last < self.cooldown_seconds:
            return False, key
        self.last_sent_monotonic_by_key[key] = now_monotonic
        return True, key


class FundingShadowMonitor:
    def __init__(
        self,
        store: SQLiteStore,
        clients: list[FundingVenueClient],
        *,
        config: FundingShadowConfig | None = None,
        stablecoin_price_provider: StablecoinPriceProvider | None = None,
        notifier: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self.store = store
        self.clients = list(clients)
        self.config = (config or FundingShadowConfig()).validated()
        self.stablecoin_price_provider = stablecoin_price_provider
        self.notifier = notifier
        self.clock = clock or SystemClock()
        self.catalog_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
        self.alert_deduper = ShadowAlertDeduper(
            self.config.unchanged_opportunity_cooldown_seconds
        )
        self.last_summary_monotonic = 0.0

    def run(self, *, duration_seconds: float = 180.0) -> dict[str, Any]:
        self.store.init_db()
        started = self.clock.monotonic()
        iterations = 0
        self._notify(
            "<b>SHADOW FUNDING</b>\n\nSHADOW STARTED\nNO POSITION OPENED"
        )
        try:
            while self.clock.monotonic() - started < max(1.0, float(duration_seconds)):
                result = self.run_once()
                iterations += 1
                interval = adaptive_broad_sweep_interval_seconds(
                    result.get("nearest_settlement_seconds")
                )
                remaining = max(
                    0.0,
                    float(duration_seconds) - (self.clock.monotonic() - started),
                )
                if remaining <= 0:
                    break
                self.clock.sleep(min(interval, remaining))
        except Exception as exc:
            self._notify(
                "<b>SHADOW FUNDING</b>\n\n"
                "monitor crashed\n"
                f"Error: {type(exc).__name__}: {exc}\n"
                "NO POSITION OPENED"
            )
            raise
        finally:
            self._notify(
                "<b>SHADOW FUNDING</b>\n\nSHADOW STOPPED\nNO POSITION OPENED"
            )
        return {"iterations": iterations, "duration_seconds": duration_seconds}

    def run_once(self) -> dict[str, Any]:
        self.store.init_db()
        observed = self.clock.now().astimezone(UTC)
        observed_at = observed.isoformat()
        markets, venue_health, warnings = self.broad_funding_sweep(observed_at)
        for health in venue_health:
            self.store.upsert_funding_shadow_venue_health(health)
        opportunities, summary = self.build_shadow_opportunities(markets, observed)
        for opportunity in opportunities:
            self.store.upsert_funding_shadow_opportunity(opportunity)
            for observation in shadow_observations_for_opportunity(opportunity):
                self.store.insert_funding_shadow_observation(observation)
            if opportunity.get("status") in {"SHADOW_CANDIDATE", "RESEARCH_ONLY"}:
                should_send, alert_key = self.alert_deduper.should_send(
                    opportunity,
                    now_monotonic=self.clock.monotonic(),
                )
                if should_send:
                    message = shadow_opportunity_message(opportunity)
                    result = self._notify(message)
                    self.store.insert_funding_shadow_alert(
                        {
                            "alert_key": alert_key,
                            "opportunity_key": opportunity["opportunity_key"],
                            "environment": opportunity["environment"],
                            "status": opportunity["status"],
                            "message": message,
                            "telegram_status": result.get("status"),
                            "telegram_error": result.get("error"),
                            "payload": {
                                "opportunity": opportunity,
                                "material_bucket": alert_key,
                            },
                        }
                    )
        if self._summary_due():
            self._notify(shadow_summary_message(summary, opportunities, warnings))
        return {
            "status": "success",
            "warnings": warnings,
            "opportunities": len(opportunities),
            **summary,
        }

    def broad_funding_sweep(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        clients = {
            str(getattr(client, "venue", "")).lower(): client
            for client in self.clients
            if getattr(client, "venue", None)
        }
        if not clients:
            return [], [], ["shadow sweep skipped: no venues configured"]
        worker_count = min(self.config.max_workers, len(clients))
        markets: list[dict[str, Any]] = []
        warnings: list[str] = []
        health_rows: list[dict[str, Any]] = []
        started_monotonic = self.clock.monotonic()
        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="funding-shadow-sweep",
        )
        try:
            futures: dict[Future[tuple[list[dict[str, Any]], list[str]]], tuple[str, float]] = {}
            for venue, client in clients.items():
                request_started = self.clock.monotonic()
                futures[
                    executor.submit(
                        self._fetch_public_market_snapshot,
                        venue,
                        client,
                        observed_at,
                    )
                ] = (venue, request_started)
            done, pending = wait(
                futures,
                timeout=float(self.config.venue_deadline_seconds),
            )
            for future in pending:
                venue, request_started = futures[future]
                future.cancel()
                latency_ms = (self.clock.monotonic() - request_started) * 1_000.0
                warnings.append(f"{venue} shadow sweep timed out")
                health_rows.append(
                    {
                        "venue": venue,
                        "environment": self.config.environment,
                        "status": "degraded",
                        "latency_ms": latency_ms,
                        "last_error": "venue_deadline_exceeded",
                        "observed_at": observed_at,
                    }
                )
            for future in done:
                venue, request_started = futures[future]
                latency_ms = (self.clock.monotonic() - request_started) * 1_000.0
                try:
                    venue_markets, venue_warnings = future.result()
                except Exception as exc:
                    warnings.append(f"{venue} shadow sweep failed: {exc}")
                    health_rows.append(
                        {
                            "venue": venue,
                            "environment": self.config.environment,
                            "status": "degraded",
                            "latency_ms": latency_ms,
                            "last_error": str(exc),
                            "observed_at": observed_at,
                        }
                    )
                    continue
                markets.extend(venue_markets)
                warnings.extend(venue_warnings)
                health_rows.append(
                    {
                        "venue": venue,
                        "environment": self.config.environment,
                        "status": "healthy",
                        "latency_ms": latency_ms,
                        "last_error": None,
                        "observed_at": observed_at,
                    }
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        elapsed = self.clock.monotonic() - started_monotonic
        if elapsed > float(self.config.venue_deadline_seconds) + 0.25:
            warnings.append("shadow sweep exceeded venue deadline budget")
        return markets, health_rows, warnings

    def _fetch_public_market_snapshot(
        self,
        venue: str,
        client: FundingVenueClient,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        cached_instruments = self._catalog_instruments(venue, client, observed_at)
        funding_sweep = getattr(client, "funding_sweep", None)
        if callable(funding_sweep):
            raw_markets, warnings = funding_sweep(observed_at, cached_instruments)
            markets = list(raw_markets)
        else:
            instruments, markets, warnings = client.catalog_and_markets(observed_at)
            cached_instruments, markets = normalize_catalog_canonical_units(
                instruments,
                markets,
            )
            self.catalog_cache[venue] = (
                cached_instruments,
                self.clock.monotonic(),
            )
        instrument_by_key = {
            (str(row.get("venue") or venue).lower(), str(row.get("symbol") or "")): row
            for row in cached_instruments
        }
        enriched: list[dict[str, Any]] = []
        for market in markets:
            row = dict(market)
            key = (str(row.get("venue") or venue).lower(), str(row.get("symbol") or ""))
            instrument = instrument_by_key.get(key, {})
            row = {**instrument, **row}
            row["venue"] = str(row.get("venue") or venue).lower()
            row.setdefault("environment", self.config.environment)
            row.setdefault("response_received_at", observed_at)
            row.setdefault("source_event_at", row.get("observed_at") or observed_at)
            row.setdefault("observed_at", observed_at)
            enriched.append(row)
        return enriched, list(warnings or [])

    def _catalog_instruments(
        self,
        venue: str,
        client: FundingVenueClient,
        observed_at: str,
    ) -> list[dict[str, Any]]:
        cached = self.catalog_cache.get(venue)
        now_monotonic = self.clock.monotonic()
        if cached and now_monotonic - cached[1] < self.config.catalog_refresh_seconds:
            return cached[0]
        catalog_method = getattr(client, "catalog_metadata", None)
        if callable(catalog_method):
            instruments = list(catalog_method(observed_at))
            self.catalog_cache[venue] = (instruments, now_monotonic)
            return instruments
        return cached[0] if cached else []

    def build_shadow_opportunities(
        self,
        markets: list[dict[str, Any]],
        now: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rejection_reasons: dict[str, int] = {}

        def reject(reason: str) -> None:
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        eligible_by_asset: dict[str, list[dict[str, Any]]] = {}
        inventory = mandatory_shadow_inventory(
            markets,
            registered_venues=[
                str(getattr(client, "venue", "")).lower()
                for client in self.clients
                if getattr(client, "venue", None)
            ],
        )
        for market in markets:
            env = str(market.get("environment") or self.config.environment).lower()
            if env != self.config.environment:
                reject("environment_mismatch")
                continue
            asset = str(market.get("canonical_asset") or "").upper()
            if not asset:
                reject("canonical_asset_missing")
                continue
            eligible_by_asset.setdefault(asset, []).append(market)

        opportunities_by_key: dict[str, dict[str, Any]] = {}
        structurally_matched = 0
        nearest_settlement_seconds: float | None = None
        for asset, rows in eligible_by_asset.items():
            for long_market in rows:
                for short_market in rows:
                    if long_market is short_market:
                        continue
                    if str(long_market.get("venue")) == str(short_market.get("venue")):
                        continue
                    structurally_matched += 1
                    opportunity = self._shadow_opportunity_for_pair(
                        asset,
                        long_market,
                        short_market,
                        now,
                    )
                    for reason in opportunity.get("blockers") or []:
                        reject(str(reason))
                    settlement_at = parse_iso(opportunity.get("settlement_at"))
                    if settlement_at is not None:
                        lead = max(0.0, (settlement_at - now).total_seconds())
                        nearest_settlement_seconds = (
                            lead
                            if nearest_settlement_seconds is None
                            else min(nearest_settlement_seconds, lead)
                        )
                    if opportunity["status"] in {
                        "SHADOW_CANDIDATE",
                        "WATCH",
                        "RESEARCH_ONLY",
                        "CAPABILITY_BLOCKED",
                    }:
                        key = opportunity["opportunity_key"]
                        existing = opportunities_by_key.get(key)
                        if existing is None or opportunity_sort_value(opportunity) > opportunity_sort_value(existing):
                            opportunities_by_key[key] = opportunity

        opportunities = sorted(
            opportunities_by_key.values(),
            key=opportunity_sort_value,
            reverse=True,
        )
        return opportunities, {
            "venues_checked": len({str(row.get("venue")) for row in markets}),
            "markets_checked": len(markets),
            "inventory": inventory,
            "primary_inventory_venue_count": len(PRIMARY_SHADOW_VENUES),
            "routes_structurally_matched": structurally_matched,
            "aligned_routes": sum(
                1
                for row in opportunities
                if "settlement_alignment_mismatch" not in (row.get("blockers") or [])
            ),
            "shadow_candidates": sum(
                1 for row in opportunities if row["status"] == "SHADOW_CANDIDATE"
            ),
            "research_only_routes": sum(
                1 for row in opportunities if row["status"] == "RESEARCH_ONLY"
            ),
            "nearest_settlement_seconds": nearest_settlement_seconds,
            "rejection_reasons": dict(sorted(rejection_reasons.items())),
        }

    def _shadow_opportunity_for_pair(
        self,
        asset: str,
        long_market: dict[str, Any],
        short_market: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        observed_at = now.astimezone(UTC).isoformat()
        long_venue = str(long_market.get("venue") or "").lower()
        short_venue = str(short_market.get("venue") or "").lower()
        long_contract = funding_adapter_contract_from_market(long_market)
        short_contract = funding_adapter_contract_from_market(short_market)
        blockers: list[str] = []
        if long_contract.environment != short_contract.environment:
            blockers.append("environment_mismatch")
        skew = settlement_skew_seconds(
            long_market.get("next_funding_at"),
            short_market.get("next_funding_at"),
        )
        if skew is None:
            blockers.append("settlement_timestamp_missing")
        elif skew > 1.0:
            blockers.append("settlement_alignment_mismatch")
        long_mark = optional_float(long_market.get("mark_price"))
        short_mark = optional_float(short_market.get("mark_price"))
        if long_mark is None or short_mark is None:
            blockers.append("mark_price_missing")
        if optional_float(long_market.get("index_price")) is None or optional_float(
            short_market.get("index_price")
        ) is None:
            blockers.append("index_price_missing")
        long_rate = optional_float(long_market.get("normalized_next_funding_rate"))
        short_rate = optional_float(short_market.get("normalized_next_funding_rate"))
        if long_rate is None or short_rate is None:
            blockers.append("normalized_next_funding_rate_missing")
        if not long_market.get("response_received_at") or not short_market.get(
            "response_received_at"
        ):
            blockers.append("response_timestamp_missing")
        if long_contract.status not in {"PAPER_ELIGIBLE", "SHADOW_ELIGIBLE"}:
            blockers.extend(f"long_{reason}" for reason in long_contract.reasons)
        if short_contract.status not in {"PAPER_ELIGIBLE", "SHADOW_ELIGIBLE"}:
            blockers.extend(f"short_{reason}" for reason in short_contract.reasons)

        preliminary_gross = 0.0
        if long_mark and short_mark and long_rate is not None and short_rate is not None:
            quantity = min(
                float(self.config.target_notional) / long_mark,
                float(self.config.target_notional) / short_mark,
            )
            preliminary_gross = gross_funding_pnl(
                quantity=quantity,
                long_mark=long_mark,
                short_mark=short_mark,
                long_funding_rate=long_rate,
                short_funding_rate=short_rate,
            )
            if preliminary_gross <= 0:
                blockers.append("preliminary_gross_funding_not_positive")
        stablecoin = evaluate_stablecoin_route(
            long_collateral=str(
                long_contract.settlement_collateral
                or long_market.get("collateral_asset")
                or ""
            ),
            short_collateral=str(
                short_contract.settlement_collateral
                or short_market.get("collateral_asset")
                or ""
            ),
            provider=self.stablecoin_price_provider,
            observed_at=observed_at,
            reference_notional=float(self.config.target_notional),
            funding_net_before_stablecoin_reserve=preliminary_gross,
        )
        if stablecoin.get("status") == "RESEARCH_ONLY":
            blockers.extend(str(reason) for reason in stablecoin.get("blockers") or [])
        blockers = list(dict.fromkeys(blockers))
        status = shadow_status_from_blockers(blockers, preliminary_gross)
        settlement_at = (
            long_market.get("next_funding_at")
            if skew is not None and skew <= 1.0
            else None
        )
        key = shadow_opportunity_key(
            environment=self.config.environment,
            asset=asset,
            long_venue=long_venue,
            short_venue=short_venue,
            long_collateral=stablecoin.get("stablecoin_pair", "").split("/", 1)[0],
            short_collateral=stablecoin.get("stablecoin_pair", "/").split("/", 1)[-1],
            settlement_at=settlement_at,
        )
        return {
            "opportunity_key": key,
            "environment": self.config.environment,
            "profile": self.config.profile,
            "canonical_asset": asset,
            "status": status,
            "long_venue": long_venue,
            "long_symbol": long_market.get("symbol"),
            "short_venue": short_venue,
            "short_symbol": short_market.get("symbol"),
            "settlement_at": settlement_at,
            "settlement_skew_seconds": skew,
            "seconds_until_settlement": (
                (parse_iso(settlement_at) - now).total_seconds()
                if parse_iso(settlement_at) is not None
                else None
            ),
            "long_next_funding_rate": long_rate,
            "short_next_funding_rate": short_rate,
            "preliminary_gross_funding": preliminary_gross,
            "stablecoin_risk": stablecoin,
            "funding_net_excluding_points": stablecoin.get(
                "funding_net_after_stablecoin_reserve",
                preliminary_gross,
            ),
            "capability_status": {
                "long": long_contract.as_dict(),
                "short": short_contract.as_dict(),
            },
            "blockers": blockers,
            "points_metadata": {"incentive_program_status": "UNKNOWN"},
            "observed_at": observed_at,
            "long_market": compact_market_shadow_payload(long_market),
            "short_market": compact_market_shadow_payload(short_market),
        }

    def _notify(self, message: str) -> dict[str, str | None]:
        print(message, flush=True)
        if not self.config.telegram_enabled or self.notifier is None:
            return {"status": "disabled", "error": None}
        result = self.notifier.send(message)
        return {"status": result.status, "error": result.error}

    def _summary_due(self) -> bool:
        now = self.clock.monotonic()
        if self.last_summary_monotonic <= 0:
            self.last_summary_monotonic = now
            return True
        if now - self.last_summary_monotonic >= self.config.periodic_summary_seconds:
            self.last_summary_monotonic = now
            return True
        return False


def adaptive_broad_sweep_interval_seconds(
    nearest_settlement_seconds: Any,
) -> float:
    if nearest_settlement_seconds is None:
        return 30.0
    try:
        lead = float(nearest_settlement_seconds)
    except (TypeError, ValueError):
        return 30.0
    if lead < 60.0:
        return 1.0
    if lead <= 120.0:
        return 5.0
    if lead <= 600.0:
        return 10.0
    return 30.0


def optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def shadow_status_from_blockers(blockers: list[str], preliminary_gross: float) -> str:
    if not blockers and preliminary_gross > 0:
        return "SHADOW_CANDIDATE"
    research_blockers = {
        "insufficient_stablecoin_price_sources",
        "stablecoin_price_provider_unavailable",
        "stablecoin_basis_above_30bps",
        "stablecoin_reserve_above_50bps",
        "stablecoin_family_not_compatible",
        "preliminary_gross_funding_not_positive",
    }
    if any(reason in research_blockers for reason in blockers):
        return "RESEARCH_ONLY"
    if any("timestamp" in reason or "stale" in reason for reason in blockers):
        return "DATA_STALE"
    if blockers:
        return "CAPABILITY_BLOCKED"
    return "WATCH"


def opportunity_sort_value(opportunity: dict[str, Any]) -> float:
    stablecoin = opportunity.get("stablecoin_risk") or {}
    for key in ("funding_net_after_stablecoin_reserve", "preliminary_gross_funding"):
        try:
            return float(stablecoin.get(key, opportunity.get(key)))
        except (TypeError, ValueError):
            continue
    return 0.0


def shadow_opportunity_key(
    *,
    environment: str,
    asset: str,
    long_venue: str,
    short_venue: str,
    long_collateral: str,
    short_collateral: str,
    settlement_at: Any,
) -> str:
    raw = "|".join(
        [
            str(environment),
            str(asset),
            str(long_venue),
            str(short_venue),
            str(long_collateral),
            str(short_collateral),
            str(settlement_at or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def compact_market_shadow_payload(market: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "venue",
        "symbol",
        "canonical_asset",
        "environment",
        "normalized_next_funding_rate",
        "next_funding_at",
        "mark_price",
        "index_price",
        "volume_24h_usd",
        "open_interest_usd",
        "contract_status",
        "status",
        "response_received_at",
        "source_event_at",
        "collateral_asset",
        "quote_asset",
        "price_quote_currency",
        "settlement_collateral",
        "collateral_family",
    )
    return {field: market.get(field) for field in fields if field in market}


def shadow_observations_for_opportunity(
    opportunity: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = []
    for side in ("long", "short"):
        market = opportunity.get(f"{side}_market") or {}
        rows.append(
            {
                "opportunity_key": opportunity["opportunity_key"],
                "environment": opportunity["environment"],
                "venue": market.get("venue"),
                "symbol": market.get("symbol"),
                "canonical_asset": opportunity["canonical_asset"],
                "side": side,
                "observed_at": opportunity["observed_at"],
                "next_funding_at": market.get("next_funding_at"),
                "normalized_next_funding_rate": market.get(
                    "normalized_next_funding_rate"
                ),
                "mark_price": market.get("mark_price"),
                "index_price": market.get("index_price"),
                "volume_24h_usd": market.get("volume_24h_usd"),
                "open_interest_usd": market.get("open_interest_usd"),
                "response_received_at": market.get("response_received_at"),
                "payload": market,
            }
        )
    return rows


def shadow_opportunity_message(opportunity: dict[str, Any]) -> str:
    stablecoin = opportunity.get("stablecoin_risk") or {}
    blockers = opportunity.get("blockers") or []
    return (
        "<b>SHADOW FUNDING</b>\n\n"
        "SHADOW FUNDING WINDOW\n\n"
        f"Asset: {opportunity.get('canonical_asset')}\n"
        f"LONG venue / collateral: {opportunity.get('long_venue')} / "
        f"{stablecoin.get('stablecoin_pair', '/').split('/', 1)[0]}\n"
        f"SHORT venue / collateral: {opportunity.get('short_venue')} / "
        f"{stablecoin.get('stablecoin_pair', '/').split('/', 1)[-1]}\n\n"
        f"Environment: {opportunity.get('environment')}\n"
        f"Settlement time: {opportunity.get('settlement_at') or '-'}\n"
        f"Settlement skew: {opportunity.get('settlement_skew_seconds')}\n"
        f"Seconds until settlement: {opportunity.get('seconds_until_settlement')}\n\n"
        f"Long next funding: {opportunity.get('long_next_funding_rate')}\n"
        f"Short next funding: {opportunity.get('short_next_funding_rate')}\n"
        f"Preliminary gross funding: {opportunity.get('preliminary_gross_funding')}\n\n"
        f"Stablecoin pair: {stablecoin.get('stablecoin_pair')}\n"
        f"Stablecoin basis: {stablecoin.get('current_stablecoin_basis_bps')}\n"
        f"Stablecoin reserve: {stablecoin.get('stablecoin_reserve_bps')}\n"
        f"Funding net after stablecoin reserve: "
        f"{stablecoin.get('funding_net_after_stablecoin_reserve')}\n\n"
        f"Capability status: {opportunity.get('status')}\n"
        "Points programs: UNKNOWN\n"
        f"Status: {opportunity.get('status')}\n"
        f"Exact blockers: {', '.join(blockers) if blockers else '-'}\n\n"
        "NO POSITION OPENED"
    )


def shadow_summary_message(
    summary: dict[str, Any],
    opportunities: list[dict[str, Any]],
    warnings: list[str],
) -> str:
    top = opportunities[:5]
    top_lines = [
        f"{row.get('canonical_asset')} {row.get('long_venue')}/{row.get('short_venue')} "
        f"{row.get('status')} net={opportunity_sort_value(row):.4f}"
        for row in top
    ]
    blockers = summary.get("rejection_reasons") or {}
    common_blockers = ", ".join(
        f"{key}:{value}" for key, value in list(blockers.items())[:8]
    ) or "-"
    return (
        "<b>SHADOW FUNDING</b>\n\n"
        "periodic summary\n"
        f"venues checked: {summary.get('venues_checked', 0)}\n"
        f"markets checked: {summary.get('markets_checked', 0)}\n"
        f"aligned routes: {summary.get('aligned_routes', 0)}\n"
        f"shadow candidates: {summary.get('shadow_candidates', 0)}\n"
        f"research-only routes: {summary.get('research_only_routes', 0)}\n"
        f"top 5 opportunities: {'; '.join(top_lines) or '-'}\n"
        f"exact common blockers: {common_blockers}\n"
        f"warnings: {', '.join(warnings[:5]) or '-'}\n\n"
        "NO POSITION OPENED"
    )
