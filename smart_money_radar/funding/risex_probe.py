from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from smart_money_radar.funding.adapters import FundingDataError
from smart_money_radar.funding.adapters.risex import RISEX_API_URL, RiseXFundingClient
from smart_money_radar.storage import SQLiteStore


RISEX_TESTNET_CREDENTIAL_ENVS = (
    "RISEX_TESTNET_API_KEY",
    "RISEX_TESTNET_API_SECRET",
)
RISEX_PUBLIC_CHECKPOINT_OFFSETS_SECONDS = (-60, -30, -15, -10, -5, -2, 0, 2, 5, 15, 30)


@dataclass(frozen=True)
class RiseXProbeConfig:
    environment: str = "testnet"
    mode: str = "public"
    db_path: Path = Path("data/risex-funding-probe-testnet.sqlite")
    max_notional_usd: float = 10.0
    entry_lead_seconds: float = 30.0
    max_wait_seconds: float = 120.0
    confirmation_timeout_seconds: float = 60.0
    no_telegram: bool = True
    confirm_testnet_canary: bool = False
    base_url: str = RISEX_API_URL
    symbol: str | None = None

    def validated(self) -> "RiseXProbeConfig":
        environment = str(self.environment or "").strip().lower()
        if environment != "testnet":
            raise ValueError("RiseX funding semantics probe supports only testnet in this pass")
        mode = str(self.mode or "public").strip().lower()
        if mode not in {"public", "testnet-canary"}:
            raise ValueError("mode must be public or testnet-canary")
        base_url = str(self.base_url or RISEX_API_URL).strip()
        if "testnet" not in base_url.lower():
            raise ValueError("RiseX probe base_url must be explicitly testnet")
        max_notional = min(10.0, max(0.0, float(self.max_notional_usd)))
        return RiseXProbeConfig(
            environment=environment,
            mode=mode,
            db_path=Path(self.db_path),
            max_notional_usd=max_notional,
            entry_lead_seconds=max(1.0, float(self.entry_lead_seconds)),
            max_wait_seconds=max(1.0, float(self.max_wait_seconds)),
            confirmation_timeout_seconds=max(1.0, float(self.confirmation_timeout_seconds)),
            no_telegram=True,
            confirm_testnet_canary=bool(self.confirm_testnet_canary),
            base_url=base_url,
            symbol=(str(self.symbol).strip() if self.symbol else None),
        )


def redact_secret_payload(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("secret", "token", "key", "signature", "auth")):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_secret_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_secret_payload(item) for item in value]
    return value


def risex_testnet_credentials_present() -> bool:
    return all(bool(os.environ.get(name)) for name in RISEX_TESTNET_CREDENTIAL_ENVS)


def classify_risex_funding_semantics_observation(
    *,
    position_notional_at_assessment: float,
    rate_per_next_settlement: float,
    hold_duration_seconds: float,
    settlement_interval_seconds: float,
    realized_funding: float,
    balance_delta: float | None = None,
) -> dict[str, Any]:
    notional = max(0.0, float(position_notional_at_assessment))
    rate = abs(float(rate_per_next_settlement))
    interval = max(1e-9, float(settlement_interval_seconds))
    hold = max(0.0, float(hold_duration_seconds))
    expected_full = notional * rate
    expected_prorata = expected_full * min(1.0, hold / interval)
    realized = float(realized_funding)
    full_ratio = realized / expected_full if expected_full > 0 else math.nan
    prorata_ratio = realized / expected_prorata if expected_prorata > 0 else math.nan
    full_error = abs(realized - expected_full)
    prorata_error = abs(realized - expected_prorata)
    relative_full_error = full_error / expected_full if expected_full > 0 else math.inf
    relative_prorata_error = (
        prorata_error / expected_prorata if expected_prorata > 0 else math.inf
    )
    if expected_full > 0 and relative_full_error <= 0.10 and (
        expected_prorata <= 0 or relative_prorata_error > 0.25
    ):
        classification = "snapshot_full_candidate"
        confidence = "medium"
    elif expected_prorata > 0 and relative_prorata_error <= 0.10:
        classification = "continuous_prorata_candidate"
        confidence = "medium"
    else:
        classification = "inconclusive"
        confidence = "low"
    if balance_delta is not None and abs(float(balance_delta) - realized) > max(0.01, abs(realized) * 0.10):
        confidence = "low"
    return {
        "expected_full_payment": expected_full,
        "expected_prorata_payment": expected_prorata,
        "realized_payment": realized,
        "balance_delta": balance_delta,
        "observed_to_full_ratio": full_ratio,
        "observed_to_prorata_ratio": prorata_ratio,
        "absolute_error": full_error,
        "relative_error": relative_full_error,
        "classification": classification,
        "confidence": confidence,
    }


def run_risex_funding_probe(config: RiseXProbeConfig) -> dict[str, Any]:
    resolved = config.validated()
    store = SQLiteStore(resolved.db_path)
    store.init_db()
    started_dt = datetime.now(UTC).replace(microsecond=0)
    started_at = started_dt.isoformat()
    probe_run_id = hashlib.sha256(
        "|".join(["risex", resolved.environment, resolved.mode, started_at]).encode("utf-8")
    ).hexdigest()[:24]
    run_row = {
        "probe_run_id": probe_run_id,
        "venue": "risex",
        "environment": resolved.environment,
        "mode": resolved.mode,
        "db_path": str(resolved.db_path),
        "started_at": started_at,
        "status": "RUNNING",
        "orders_enabled": False,
        "base_url": resolved.base_url,
        "max_notional_usd": resolved.max_notional_usd,
        "entry_lead_seconds": resolved.entry_lead_seconds,
        "max_wait_seconds": resolved.max_wait_seconds,
        "confirmation_timeout_seconds": resolved.confirmation_timeout_seconds,
        "no_telegram": True,
        "payload": {"credentials_present": False},
    }
    store.insert_funding_semantics_probe_run(run_row)
    try:
        if resolved.mode == "testnet-canary":
            canary_guard = _testnet_canary_guard(resolved)
            if canary_guard:
                completed = {
                    **run_row,
                    "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                    "status": canary_guard["status"],
                    "error": canary_guard["reason"],
                    "payload": canary_guard,
                }
                store.insert_funding_semantics_probe_run(completed)
                return {"probe_run_id": probe_run_id, **canary_guard}

        client = RiseXFundingClient(base_url=resolved.base_url, environment="testnet")
        observed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        instruments, markets, warnings = client.catalog_and_markets(observed_at)
        market = select_risex_probe_market(markets, resolved.symbol)
        observation_count = 0
        boundary_attempts = 0
        boundary_observed = 0
        public_settlements_confirmed = 0
        latest_scheduled_settlement = market.get("next_funding_at") if market else None
        confirmed_settlements: set[str] = set()
        if market is not None:
            interval_seconds = float(market.get("funding_interval_hours") or 0.0) * 3600.0
            store.insert_funding_semantics_probe_observation(
                risex_probe_snapshot_observation(
                    probe_run_id=probe_run_id,
                    market=market,
                    interval_seconds=interval_seconds,
                    warnings=warnings,
                    classification="MARKET_SNAPSHOT",
                    actual_offset_seconds=None,
                )
            )
            observation_count = 1
            scheduled_dt = parse_probe_datetime(latest_scheduled_settlement)
            deadline_dt = started_dt + timedelta(seconds=float(resolved.max_wait_seconds))
            lower_bound_dt = started_dt - timedelta(
                seconds=float(resolved.confirmation_timeout_seconds)
            )
            if scheduled_dt is not None and scheduled_dt >= lower_bound_dt:
                for checkpoint_offset in RISEX_PUBLIC_CHECKPOINT_OFFSETS_SECONDS:
                    checkpoint_dt = scheduled_dt + timedelta(seconds=checkpoint_offset)
                    if checkpoint_dt > deadline_dt:
                        break
                    now_dt = datetime.now(UTC)
                    if checkpoint_dt < now_dt and checkpoint_offset < 0:
                        continue
                    sleep_seconds = (checkpoint_dt - now_dt).total_seconds()
                    if sleep_seconds > 0:
                        time.sleep(min(sleep_seconds, max(0.0, (deadline_dt - now_dt).total_seconds())))
                    actual_dt = datetime.now(UTC)
                    if actual_dt > deadline_dt:
                        break
                    actual_offset = (actual_dt - scheduled_dt).total_seconds()
                    observed_at = actual_dt.isoformat()
                    _checkpoint_instruments, checkpoint_markets, checkpoint_warnings = (
                        client.catalog_and_markets(observed_at)
                    )
                    warnings.extend(checkpoint_warnings)
                    checkpoint_market = select_risex_probe_market(
                        checkpoint_markets,
                        market.get("symbol"),
                    )
                    if checkpoint_market is None:
                        continue
                    latest_scheduled_settlement = checkpoint_market.get("next_funding_at") or latest_scheduled_settlement
                    store.insert_funding_semantics_probe_observation(
                        risex_probe_snapshot_observation(
                            probe_run_id=probe_run_id,
                            market=checkpoint_market,
                            interval_seconds=interval_seconds,
                            warnings=checkpoint_warnings,
                            classification="MARKET_SNAPSHOT",
                            actual_offset_seconds=actual_offset,
                        )
                    )
                    observation_count += 1
                    if actual_offset < 0:
                        continue
                    boundary_attempts += 1
                    settlement_key = scheduled_dt.isoformat()
                    if settlement_key in confirmed_settlements:
                        continue
                    if risex_public_settlement_confirmed(
                        client,
                        symbol=str(checkpoint_market.get("symbol") or ""),
                        scheduled_settlement_at=scheduled_dt,
                        interval_seconds=interval_seconds,
                        observed_at=observed_at,
                    ):
                        confirmed_settlements.add(settlement_key)
                        boundary_observed += 1
                        public_settlements_confirmed += 1
                        store.insert_funding_semantics_probe_observation(
                            risex_probe_snapshot_observation(
                                probe_run_id=probe_run_id,
                                market=checkpoint_market,
                                interval_seconds=interval_seconds,
                                warnings=checkpoint_warnings,
                                classification="PUBLIC_SETTLEMENT_CONFIRMED",
                                actual_offset_seconds=actual_offset,
                            )
                        )
        completed_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        status = (
            "PUBLIC_BOUNDARY_OBSERVED"
            if public_settlements_confirmed
            else ("PARTIAL_NO_BOUNDARY" if observation_count else "NO_PUBLIC_MARKETS")
        )
        completed = {
            **run_row,
            "completed_at": completed_at,
            "status": status,
            "payload": {
                "instrument_count": len(instruments),
                "market_count": len(markets),
                "market_snapshot_count": observation_count,
                "boundary_event_attempt_count": boundary_attempts,
                "boundary_event_observed_count": boundary_observed,
                "public_settlement_confirmed_count": public_settlements_confirmed,
                "latest_scheduled_settlement": latest_scheduled_settlement,
                "symbol": market.get("symbol") if market else resolved.symbol,
                "warnings": warnings,
                "orders_enabled": False,
            },
        }
        store.insert_funding_semantics_probe_run(completed)
        return {
            "probe_run_id": probe_run_id,
            "status": status,
            "instrument_count": len(instruments),
            "market_count": len(markets),
            "market_snapshot_count": observation_count,
            "boundary_event_attempt_count": boundary_attempts,
            "boundary_event_observed_count": boundary_observed,
            "public_settlement_confirmed_count": public_settlements_confirmed,
            "latest_scheduled_settlement": latest_scheduled_settlement,
            "symbol": market.get("symbol") if market else resolved.symbol,
            "warnings": warnings,
            "orders_enabled": False,
        }
    except Exception as exc:
        completed = {
            **run_row,
            "completed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "status": "FAILED",
            "error": str(exc),
            "payload": {"orders_enabled": False},
        }
        store.insert_funding_semantics_probe_run(completed)
        if isinstance(exc, FundingDataError):
            return {
                "probe_run_id": probe_run_id,
                "status": "FAILED",
                "error": str(exc),
                "orders_enabled": False,
            }
        raise


def parse_probe_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def risex_public_settlement_confirmed(
    client: RiseXFundingClient,
    *,
    symbol: str,
    scheduled_settlement_at: datetime,
    interval_seconds: float,
    observed_at: str,
) -> bool:
    if not symbol:
        return False
    funding_history = getattr(client, "funding_history", None)
    if not callable(funding_history):
        return False
    interval = max(1.0, float(interval_seconds or 0.0))
    start_time_ms = int(
        (scheduled_settlement_at - timedelta(seconds=interval * 2)).timestamp() * 1_000
    )
    try:
        rows = funding_history(
            symbol,
            start_time_ms=start_time_ms,
            interval_hours=interval / 3600.0,
            observed_at=observed_at,
        )
    except Exception:
        return False
    tolerance_seconds = max(2.0, min(90.0, interval * 0.05))
    for row in rows:
        funding_at = parse_probe_datetime(row.get("funding_at"))
        if funding_at is None:
            continue
        if abs((funding_at - scheduled_settlement_at).total_seconds()) <= tolerance_seconds:
            return True
    return False


def _testnet_canary_guard(config: RiseXProbeConfig) -> dict[str, Any] | None:
    if config.environment != "testnet":
        return {
            "status": "CANARY_BLOCKED",
            "reason": "mainnet_canary_forbidden",
            "orders_enabled": False,
        }
    if not config.confirm_testnet_canary:
        return {
            "status": "CANARY_BLOCKED",
            "reason": "confirm_testnet_canary_flag_missing",
            "orders_enabled": False,
        }
    if config.max_notional_usd > 10.0:
        return {
            "status": "CANARY_BLOCKED",
            "reason": "max_notional_exceeds_10_usd",
            "orders_enabled": False,
        }
    if not risex_testnet_credentials_present():
        return {
            "status": "CANARY_UNSUPPORTED",
            "reason": "risex_testnet_credentials_missing",
            "orders_enabled": False,
        }
    return {
        "status": "CANARY_UNSUPPORTED",
        "reason": "risex_eip712_session_key_order_and_ledger_flow_not_implemented",
        "orders_enabled": False,
    }


def select_risex_probe_market(
    markets: list[dict[str, Any]],
    symbol: str | None,
) -> dict[str, Any] | None:
    if symbol:
        requested = str(symbol).strip().upper()
        for market in markets:
            if str(market.get("symbol") or "").upper() == requested:
                return market
    candidates = [market for market in markets if market.get("symbol")]
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda row: (
            -float(row.get("volume_24h_usd") or 0.0),
            str(row.get("symbol") or ""),
        ),
    )[0]


def risex_probe_snapshot_observation(
    *,
    probe_run_id: str,
    market: dict[str, Any],
    interval_seconds: float,
    warnings: list[str],
    classification: str,
    actual_offset_seconds: float | None,
) -> dict[str, Any]:
    return {
        "probe_run_id": probe_run_id,
        "venue": "risex",
        "environment": "testnet",
        "symbol": market.get("symbol"),
        "scheduled_settlement_at": market.get("next_funding_at"),
        "actual_assessment_at": None,
        "actual_confirmation_at": None,
        "entry_lead_seconds": actual_offset_seconds,
        "hold_duration_seconds": None,
        "size": None,
        "predicted_rate": market.get("normalized_next_funding_rate"),
        "rate_period_seconds": interval_seconds,
        "expected_full_payment": None,
        "expected_prorata_payment": None,
        "realized_payment": None,
        "balance_delta": None,
        "classification": classification,
        "confidence": "low",
        "errors": None,
        "raw_evidence_metadata": redact_secret_payload(
            {
                "market": {
                    key: market.get(key)
                    for key in (
                        "venue",
                        "symbol",
                        "environment",
                        "next_funding_at",
                        "funding_rate",
                        "normalized_next_funding_rate",
                        "funding_interval_hours",
                        "published_funding_rate",
                        "published_funding_interval_hours",
                        "mark_price",
                        "index_price",
                    )
                },
                "actual_offset_seconds": actual_offset_seconds,
                "warnings": warnings,
            }
        ),
    }
