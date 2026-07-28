from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol


RECONCILIATION_STATES = (
    "PENDING",
    "PUBLIC_RATE_CONFIRMED",
    "RATE_AND_MARK_RECONCILED",
    "UNRECONCILED",
)

RECONCILIATION_TOLERANCE_SECONDS = 120.0
REALIZED_FUNDING_RATE_SEMANTICS = {"realized_settlement", "account_transaction"}
REALIZED_FUNDING_RATE_SOURCES = {
    ("realized_settlement", "adapter_explicit"),
    ("account_transaction", "account_ledger"),
}


def settlement_reconciliation_key(position_id: Any, venue: str, scheduled_funding_at: str) -> tuple[Any, str, str]:
    return (position_id, str(venue), str(scheduled_funding_at))


def funding_reconciliation_pnl(
    *,
    side: str,
    quantity: float,
    settlement_mark_price: float,
    confirmed_funding_rate: float,
) -> float:
    notional = float(quantity) * float(settlement_mark_price)
    if str(side).lower() == "long":
        return -notional * float(confirmed_funding_rate)
    return notional * float(confirmed_funding_rate)


def cycle_reconciled(leg_rows: list[dict[str, Any]]) -> bool:
    return bool(leg_rows) and all(
        str(row.get("status") or row.get("reconciliation_status"))
        == "RATE_AND_MARK_RECONCILED"
        for row in leg_rows
    )


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _raw_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw")
    if isinstance(raw, dict):
        return raw
    raw_json = row.get("raw_json")
    if not raw_json:
        return {}
    try:
        decoded = json.loads(str(raw_json))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def funding_rate_semantics(event: dict[str, Any] | None) -> str:
    if not event:
        return "unclear"
    raw = _raw_payload(event)
    return str(
        event.get("rate_semantics")
        or event.get("funding_rate_semantics")
        or raw.get("rate_semantics")
        or raw.get("funding_rate_semantics")
        or "unclear"
    )


def funding_rate_semantics_source(event: dict[str, Any] | None) -> str:
    if not event:
        return "unknown"
    raw = _raw_payload(event)
    return str(
        event.get("rate_semantics_source")
        or event.get("funding_rate_semantics_source")
        or raw.get("rate_semantics_source")
        or raw.get("funding_rate_semantics_source")
        or "unknown"
    )


def realized_public_funding_rate(event: dict[str, Any] | None) -> float | None:
    """Return a cashflow-eligible realized funding rate, preserving valid zero."""
    if event is None:
        return None
    semantics = funding_rate_semantics(event)
    source = funding_rate_semantics_source(event)
    if (semantics, source) not in REALIZED_FUNDING_RATE_SOURCES:
        return None
    if "funding_rate" not in event or event.get("funding_rate") is None:
        return None
    return _optional_float(event.get("funding_rate"))


def _event_time_with_quality(row: dict[str, Any], *, fallback_key: str) -> tuple[datetime | None, str, str]:
    raw = _raw_payload(row)
    for key in ("source_event_at", "venue_server_time", "response_received_at"):
        parsed = _parse_time(row.get(key) or raw.get(key))
        if parsed is not None:
            return parsed, key, "PRIMARY"
    parsed = _parse_time(row.get(fallback_key) or raw.get(fallback_key))
    if parsed is not None:
        return parsed, fallback_key, "INGESTION_TIME_FALLBACK"
    parsed = _parse_time(row.get("observed_at") or raw.get("observed_at"))
    if parsed is not None:
        return parsed, "observed_at", "INGESTION_TIME_FALLBACK"
    return None, "missing", "MISSING"


def settlement_schedule_matches(
    public_scheduled_at: Any,
    expected_scheduled_at: Any,
    *,
    tolerance_seconds: float = RECONCILIATION_TOLERANCE_SECONDS,
) -> bool:
    public_time = _parse_time(public_scheduled_at)
    expected_time = _parse_time(expected_scheduled_at)
    if public_time is None or expected_time is None:
        return False
    skew = abs((public_time.astimezone(UTC) - expected_time.astimezone(UTC)).total_seconds())
    return skew <= float(tolerance_seconds)


def reconcile_leg(
    *,
    public_rate: float | None,
    public_mark: float | None,
    side: str,
    quantity: float,
) -> dict[str, Any]:
    """Reconcile a single leg against public data.

    - rate + mark => RATE_AND_MARK_RECONCILED, funding_pnl computed
    - rate only   => PUBLIC_RATE_CONFIRMED, funding_pnl NULL, no balance change
    - timeout     => UNRECONCILED
    """
    if public_rate is None:
        return {
            "status": "UNRECONCILED",
            "rate_status": "MISSING",
            "mark_status": "MISSING",
            "confirmed_funding_rate": None,
            "settlement_mark_price": None,
            "funding_pnl": None,
        }
    if public_mark is None:
        return {
            "status": "PUBLIC_RATE_CONFIRMED",
            "rate_status": "CONFIRMED",
            "mark_status": "MISSING",
            "confirmed_funding_rate": float(public_rate),
            "settlement_mark_price": None,
            "funding_pnl": None,
        }
    pnl = funding_reconciliation_pnl(
        side=side,
        quantity=quantity,
        settlement_mark_price=float(public_mark),
        confirmed_funding_rate=float(public_rate),
    )
    return {
        "status": "RATE_AND_MARK_RECONCILED",
        "rate_status": "CONFIRMED",
        "mark_status": "CONFIRMED",
        "confirmed_funding_rate": float(public_rate),
        "settlement_mark_price": float(public_mark),
        "funding_pnl": pnl,
    }


def build_settlement_crossing_rows(
    *,
    position_id: str,
    cycle_id: str,
    long_venue: str,
    long_symbol: str,
    short_venue: str,
    short_symbol: str,
    scheduled_funding_at: str,
    quantity: float,
) -> list[dict[str, Any]]:
    """Create one PENDING reconciliation row per leg on settlement crossing."""
    return [
        {
            "position_id": position_id,
            "cycle_id": cycle_id,
            "venue": long_venue,
            "symbol": long_symbol,
            "side": "long",
            "scheduled_funding_at": scheduled_funding_at,
            "status": "PENDING",
            "confirmed_funding_rate": None,
            "settlement_mark_price": None,
            "funding_pnl": None,
            "rate_status": None,
            "mark_status": None,
            "evidence": {},
        },
        {
            "position_id": position_id,
            "cycle_id": cycle_id,
            "venue": short_venue,
            "symbol": short_symbol,
            "side": "short",
            "scheduled_funding_at": scheduled_funding_at,
            "status": "PENDING",
            "confirmed_funding_rate": None,
            "settlement_mark_price": None,
            "funding_pnl": None,
            "rate_status": None,
            "mark_status": None,
            "evidence": {},
        },
    ]


# ---------------------------------------------------------------------------
# FundingSettlementDataProvider — injectable interface for reconciliation data
# ---------------------------------------------------------------------------

class FundingSettlementDataProvider(Protocol):
    """Injectable interface for reconciliation data lookups."""

    def get_public_funding_event(
        self,
        venue: str,
        symbol: str,
        scheduled_funding_at: datetime,
        tolerance_seconds: float,
    ) -> dict[str, Any] | None:
        """Return the public funding event for a venue/symbol near the scheduled time.

        Returns dict with at least ``funding_rate`` key, or None if not found.
        """
        ...

    def get_nearest_mark_snapshot(
        self,
        venue: str,
        symbol: str,
        scheduled_funding_at: datetime,
        max_distance_seconds: float,
    ) -> dict[str, Any] | None:
        """Return the nearest mark price snapshot near the scheduled time.

        Returns dict with at least ``mark_price`` key, or None if not found.
        """
        ...


class StoredFundingSettlementDataProvider:
    """Production provider using stored funding history and market snapshots.

    Uses only locally stored data — no real API calls.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    def get_public_funding_event(
        self,
        venue: str,
        symbol: str,
        scheduled_funding_at: datetime,
        tolerance_seconds: float,
    ) -> dict[str, Any] | None:
        since = (
            scheduled_funding_at.astimezone(UTC)
            - timedelta(seconds=float(tolerance_seconds))
        ).isoformat()
        rows = self.store.funding_history_rows(
            venue=venue,
            symbol=symbol,
            since=since,
        )
        best: dict[str, Any] | None = None
        best_skew: float = tolerance_seconds + 1.0
        target_ts = scheduled_funding_at.astimezone(UTC).timestamp()
        for row in rows:
            semantics = funding_rate_semantics(row)
            semantics_source = funding_rate_semantics_source(row)
            if (semantics, semantics_source) not in REALIZED_FUNDING_RATE_SOURCES:
                continue
            rate = _optional_float(row.get("funding_rate"))
            if rate is None:
                continue
            published_at = _parse_time(
                row.get("funding_at")
                or row.get("published_at")
                or row.get("observed_at")
            )
            if published_at is None:
                continue
            skew = abs(published_at.astimezone(UTC).timestamp() - target_ts)
            if skew > tolerance_seconds:
                continue
            if skew < best_skew:
                best_skew = skew
                best = {
                    "funding_rate": rate,
                    "rate_semantics": semantics,
                    "rate_semantics_source": semantics_source,
                    "published_at": published_at.isoformat(),
                    "skew_seconds": skew,
                    "source": "funding_rate_history",
                }
        return best

    def get_nearest_mark_snapshot(
        self,
        venue: str,
        symbol: str,
        scheduled_funding_at: datetime,
        max_distance_seconds: float,
    ) -> dict[str, Any] | None:
        rows = self.store.funding_market_snapshot_rows(
            venue=venue,
            symbol=symbol,
            limit=200,
        )
        best: dict[str, Any] | None = None
        best_skew: float = max_distance_seconds + 1.0
        target_ts = scheduled_funding_at.astimezone(UTC).timestamp()
        for row in rows:
            event_at, timestamp_source, quality = _event_time_with_quality(
                row,
                fallback_key="observed_at",
            )
            if event_at is None:
                continue
            skew = abs(event_at.astimezone(UTC).timestamp() - target_ts)
            if skew > max_distance_seconds:
                continue
            mark = _optional_float(row.get("mark_price"))
            if mark is None:
                continue
            if mark <= 0:
                continue
            if skew < best_skew:
                best_skew = skew
                best = {
                    "mark_price": mark,
                    "observed_at": event_at.isoformat(),
                    "skew_seconds": skew,
                    "timestamp_source": timestamp_source,
                    "reconciliation_quality": quality,
                }
        if best is not None:
            return best

        since = (
            scheduled_funding_at.astimezone(UTC)
            - timedelta(seconds=float(max_distance_seconds))
        ).isoformat()
        history_rows = self.store.funding_history_rows(
            venue=venue,
            symbol=symbol,
            since=since,
        )
        for row in history_rows:
            event_at, timestamp_source, quality = _event_time_with_quality(
                row,
                fallback_key="funding_at",
            )
            if event_at is None:
                continue
            skew = abs(event_at.astimezone(UTC).timestamp() - target_ts)
            if skew > max_distance_seconds:
                continue
            mark = _optional_float(row.get("mark_price"))
            if mark is None:
                continue
            if mark <= 0:
                continue
            if skew < best_skew:
                best_skew = skew
                best = {
                    "mark_price": mark,
                    "observed_at": event_at.isoformat(),
                    "skew_seconds": skew,
                    "source": "funding_rate_history",
                    "timestamp_source": timestamp_source,
                    "reconciliation_quality": quality,
                }
        return best


class FakeFundingSettlementDataProvider:
    """Test provider with preloaded public events and mark snapshots."""

    def __init__(
        self,
        public_events: list[dict[str, Any]] | None = None,
        mark_snapshots: list[dict[str, Any]] | None = None,
    ) -> None:
        self.public_events = list(public_events or [])
        self.mark_snapshots = list(mark_snapshots or [])

    def get_public_funding_event(
        self,
        venue: str,
        symbol: str,
        scheduled_funding_at: datetime,
        tolerance_seconds: float,
    ) -> dict[str, Any] | None:
        target_ts = scheduled_funding_at.astimezone(UTC).timestamp()
        best: dict[str, Any] | None = None
        best_skew: float = tolerance_seconds + 1.0
        for event in self.public_events:
            if str(event.get("venue") or "") != venue:
                continue
            if str(event.get("symbol") or "") != symbol:
                continue
            event_time = _parse_time(event.get("scheduled_at") or event.get("published_at"))
            if event_time is None:
                continue
            skew = abs(event_time.astimezone(UTC).timestamp() - target_ts)
            if skew > tolerance_seconds:
                continue
            if skew < best_skew:
                best_skew = skew
                rate = _optional_float(event.get("funding_rate"))
                semantics = str(event.get("rate_semantics") or "realized_settlement")
                semantics_source = str(
                    event.get("rate_semantics_source") or "adapter_explicit"
                )
                best = {
                    "funding_rate": rate,
                    "rate_semantics": semantics,
                    "rate_semantics_source": semantics_source,
                    "published_at": event_time.isoformat(),
                    "skew_seconds": skew,
                }
        return best

    def get_nearest_mark_snapshot(
        self,
        venue: str,
        symbol: str,
        scheduled_funding_at: datetime,
        max_distance_seconds: float,
    ) -> dict[str, Any] | None:
        target_ts = scheduled_funding_at.astimezone(UTC).timestamp()
        best: dict[str, Any] | None = None
        best_skew: float = max_distance_seconds + 1.0
        for snap in self.mark_snapshots:
            if str(snap.get("venue") or "") != venue:
                continue
            if str(snap.get("symbol") or "") != symbol:
                continue
            snap_time = _parse_time(snap.get("observed_at"))
            if snap_time is None:
                continue
            skew = abs(snap_time.astimezone(UTC).timestamp() - target_ts)
            if skew > max_distance_seconds:
                continue
            mark = _optional_float(snap.get("mark_price"))
            if mark is None:
                continue
            if mark <= 0:
                continue
            if skew < best_skew:
                best_skew = skew
                best = {
                    "mark_price": mark,
                    "observed_at": snap_time.isoformat(),
                    "skew_seconds": skew,
                    "timestamp_source": str(snap.get("timestamp_source") or "observed_at"),
                    "reconciliation_quality": str(
                        snap.get("reconciliation_quality") or "INGESTION_TIME_FALLBACK"
                    ),
                }
        return best
