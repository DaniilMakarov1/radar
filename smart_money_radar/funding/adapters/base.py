from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from smart_money_radar.http import RateLimitedHttpClient


class FundingDataError(RuntimeError):
    pass


class FundingVenueClient(Protocol):
    venue: str

    def catalog_and_markets(
        self,
        observed_at: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]: ...

    def orderbook(
        self,
        symbol: str,
        observed_at: str,
        limit: int = 100,
    ) -> dict[str, Any]: ...

    def funding_history(
        self,
        symbol: str,
        start_time_ms: int,
        interval_hours: float,
        observed_at: str,
    ) -> list[dict[str, Any]]: ...


class FundingHttpClient(RateLimitedHttpClient):
    error_class = FundingDataError
    user_agent = "SmartMoneyRadar-Funding/0.1"


@dataclass(frozen=True)
class VenueEndpointIdentity:
    venue: str
    environment: str
    base_url: str
    environment_verified: bool
    provenance: str
    client_version: str
    verified_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_KNOWN_ENDPOINTS: dict[tuple[str, str], tuple[str, str]] = {
    ("binance", "https://fapi.binance.com"): ("mainnet", "official_public_rest"),
    ("bybit", "https://api.bybit.com"): ("mainnet", "official_public_rest"),
    ("okx", "https://app.okx.com"): ("mainnet", "official_public_rest"),
    ("okx", "https://www.okx.com"): ("mainnet", "official_public_rest"),
    ("pacifica", "https://api.pacifica.fi/api/v1"): (
        "mainnet",
        "official_public_rest",
    ),
    ("nado", "https://gateway.prod.nado.xyz/v2"): (
        "mainnet",
        "official_public_rest",
    ),
    ("nado", "https://gateway.test.nado.xyz/v2"): (
        "testnet",
        "official_public_rest",
    ),
    (
        "variational",
        "https://omni-client-api.prod.ap-northeast-1.variational.io",
    ): (
        "mainnet",
        "official_public_rest",
    ),
    ("risex", "https://api.testnet.rise.trade"): (
        "testnet",
        "official_public_rest",
    ),
}


def build_endpoint_identity(
    *,
    venue: str,
    base_url: str,
    requested_environment: str | None = None,
    client_version: str = "radar-funding-public-v1",
) -> VenueEndpointIdentity:
    venue_key = str(venue or "").strip().lower()
    normalized_base = str(base_url or "").strip().rstrip("/")
    known = _KNOWN_ENDPOINTS.get((venue_key, normalized_base))
    verified_at = datetime.now(UTC).isoformat()
    if known is not None:
        environment, provenance = known
        requested = str(requested_environment or "").strip().lower()
        if requested in {"mainnet", "testnet"} and requested != environment:
            raise FundingDataError(
                f"{venue_key} {requested} cannot use {normalized_base}"
            )
        return VenueEndpointIdentity(
            venue=venue_key,
            environment=environment,
            base_url=normalized_base,
            environment_verified=True,
            provenance=provenance,
            client_version=client_version,
            verified_at=verified_at,
        )
    requested = str(requested_environment or "").strip().lower()
    environment = requested if requested in {"mainnet", "testnet"} else "unknown"
    return VenueEndpointIdentity(
        venue=venue_key,
        environment=environment,
        base_url=normalized_base,
        environment_verified=False,
        provenance="unverified_client_endpoint",
        client_version=client_version,
        verified_at=verified_at,
    )


def client_endpoint_identity(client: Any) -> VenueEndpointIdentity:
    identity = getattr(client, "endpoint_identity", None)
    if isinstance(identity, VenueEndpointIdentity):
        return identity
    venue = str(getattr(client, "venue", "") or "").strip().lower()
    base_url = str(getattr(client, "base_url", "") or "").strip()
    environment = str(getattr(client, "environment", "") or "").strip().lower()
    return build_endpoint_identity(
        venue=venue,
        base_url=base_url,
        requested_environment=environment if environment in {"mainnet", "testnet"} else None,
    )


def endpoint_identity_fields(identity: VenueEndpointIdentity) -> dict[str, Any]:
    return {
        "environment": identity.environment,
        "environment_verified": identity.environment_verified,
        "endpoint_base_url": identity.base_url,
        "endpoint_identity_provenance": identity.provenance,
        "endpoint_client_version": identity.client_version,
        "endpoint_verified_at": identity.verified_at,
    }


def apply_endpoint_identity(
    rows: list[dict[str, Any]],
    identity: VenueEndpointIdentity,
) -> list[dict[str, Any]]:
    fields = endpoint_identity_fields(identity)
    for row in rows:
        row.update(fields)
    return rows


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
