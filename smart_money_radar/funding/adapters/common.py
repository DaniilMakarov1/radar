from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from smart_money_radar.funding.adapters.base import FundingDataError, as_float
from smart_money_radar.funding.normalization import parse_timestamp


def rows_by_key(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {
        str(row.get(key)): row
        for row in rows
        if isinstance(row, dict) and row.get(key) not in {None, ""}
    }


def list_rows(payload: Any, label: str, *, data_key: str = "data") -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise FundingDataError(f"Invalid {label} response")
    rows = payload.get(data_key)
    if isinstance(rows, dict):
        for nested_key in ("rows", "list", "symbols"):
            nested = rows.get(nested_key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    raise FundingDataError(f"Invalid {label} rows")


def first_data_object(payload: Any, label: str) -> dict[str, Any]:
    rows = list_rows(payload, label)
    if not rows:
        raise FundingDataError(f"{label} returned no rows")
    return rows[0]


def success_payload(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FundingDataError(f"Invalid {label} response")
    code = payload.get("code")
    success = payload.get("success")
    if success is False:
        raise FundingDataError(f"{label} unavailable: {payload.get('message')}")
    if code not in {None, 0, "0", 1000, "1000"}:
        raise FundingDataError(
            f"{label} unavailable: {payload.get('msg') or payload.get('message')}"
        )
    return payload


def dict_price_size_levels(
    levels: Any,
    *,
    price_key: str = "price",
    size_key: str = "quantity",
    size_multiplier: float = 1.0,
) -> list[list[float]]:
    output: list[list[float]] = []
    if not isinstance(levels, list):
        return output
    for row in levels:
        if isinstance(row, dict):
            price = as_float(row.get(price_key))
            size = as_float(row.get(size_key))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price = as_float(row[0])
            size = as_float(row[1])
        else:
            continue
        if price > 0 and size > 0:
            output.append([price, size * max(float(size_multiplier or 1.0), 0.0)])
    return output


def iso_from_nanoseconds(value: Any) -> str | None:
    try:
        timestamp = float(value) / 1_000_000_000.0
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, UTC).replace(microsecond=0).isoformat()


def next_interval_boundary_iso(observed_at: str, interval_seconds: int) -> str | None:
    observed = parse_timestamp(observed_at) or datetime.now(UTC)
    interval = max(60, int(interval_seconds or 0))
    current = int(observed.timestamp())
    next_boundary = ((current // interval) + 1) * interval
    return datetime.fromtimestamp(next_boundary, UTC).replace(microsecond=0).isoformat()
