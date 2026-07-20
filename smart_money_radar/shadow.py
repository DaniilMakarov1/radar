from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from smart_money_radar.storage import SQLiteStore, normalize_chain_address


SHADOW_SOURCE = "market_snapshot_shadow_v1"
DEFAULT_SHADOW_NOTIONAL_USD = 1_000.0
MAX_LIQUIDITY_SHARE = 0.001


def refresh_signal_shadow_marks(store: SQLiteStore) -> dict[str, Any]:
    with store.connect() as connection:
        signals = connection.execute(
            """
            SELECT s.*
            FROM signals s
            WHERE s.status = 'candidate'
              AND s.signal_id = (
                  SELECT MIN(first_signal.signal_id)
                  FROM signals first_signal
                  WHERE first_signal.chain_id = s.chain_id
                    AND first_signal.contract_address = s.contract_address
                    AND first_signal.signal_type = s.signal_type
                    AND first_signal.status = 'candidate'
              )
            ORDER BY s.detected_at
            """
        ).fetchall()

    marks: list[dict[str, Any]] = []
    skipped = []
    for raw_signal in signals:
        signal = dict(raw_signal)
        chain_id = signal["chain_id"]
        token_address = normalize_chain_address(
            chain_id, signal.get("contract_address")
        )
        if not token_address:
            skipped.append({"signal_id": signal["signal_id"], "reason": "missing_contract"})
            continue
        snapshots = market_snapshots_after(
            store,
            chain_id=chain_id,
            token_address=token_address,
            detected_at=signal["detected_at"],
        )
        if not snapshots:
            skipped.append({"signal_id": signal["signal_id"], "reason": "no_market_snapshot"})
            continue
        entry = snapshots[0]
        entry_price = positive_float(entry.get("price_usd"))
        entry_liquidity = positive_float(entry.get("liquidity_usd"))
        if entry_price is None or entry_liquidity is None:
            skipped.append({"signal_id": signal["signal_id"], "reason": "invalid_entry_market"})
            continue
        notional = min(DEFAULT_SHADOW_NOTIONAL_USD, entry_liquidity * MAX_LIQUIDITY_SHARE)
        for snapshot in snapshots[1:]:
            mark_price = positive_float(snapshot.get("price_usd"))
            mark_liquidity = positive_float(snapshot.get("liquidity_usd"))
            if mark_price is None or mark_liquidity is None:
                continue
            gross_return = mark_price / entry_price - 1.0
            slippage_bps = round_trip_slippage_bps(
                notional,
                entry_liquidity,
                mark_liquidity,
            )
            marks.append(
                {
                    "signal_id": signal["signal_id"],
                    "chain_id": chain_id,
                    "token_address": token_address,
                    "observed_at": snapshot["observed_at"],
                    "horizon_hours": elapsed_hours(
                        signal["detected_at"], snapshot["observed_at"]
                    ),
                    "entry_price_usd": entry_price,
                    "mark_price_usd": mark_price,
                    "entry_liquidity_usd": entry_liquidity,
                    "mark_liquidity_usd": mark_liquidity,
                    "gross_return": gross_return,
                    "estimated_round_trip_slippage_bps": slippage_bps,
                    "net_return_after_slippage": gross_return - slippage_bps / 10_000,
                    "source": SHADOW_SOURCE,
                    "evidence": {
                        "shadow_notional_usd": notional,
                        "slippage_model": "two_sided_notional_to_liquidity_proxy",
                        "entry_snapshot_at": entry["observed_at"],
                        "mark_snapshot_source": snapshot["source"],
                        "research_only": True,
                    },
                }
            )
    stored = store.upsert_signal_shadow_marks(marks)
    return {
        "candidate_episode_count": len(signals),
        "generated_mark_count": len(marks),
        "stored_mark_count": stored,
        "skipped": skipped,
        "summary": store.signal_shadow_summary(),
    }


def market_snapshots_after(
    store: SQLiteStore,
    chain_id: str,
    token_address: str,
    detected_at: str,
) -> list[dict[str, Any]]:
    with store.connect() as connection:
        rows = connection.execute(
            """
            SELECT observed_at, source, price_usd, liquidity_usd
            FROM token_market_snapshots
            WHERE chain_id = ?
              AND token_address = ?
              AND observed_at >= ?
            ORDER BY observed_at
            """,
            (chain_id, token_address, detected_at),
        ).fetchall()
    return [dict(row) for row in rows]


def round_trip_slippage_bps(
    notional_usd: float,
    entry_liquidity_usd: float,
    exit_liquidity_usd: float,
) -> float:
    if notional_usd <= 0 or entry_liquidity_usd <= 0 or exit_liquidity_usd <= 0:
        return 10_000.0
    one_way_entry = notional_usd / entry_liquidity_usd
    one_way_exit = notional_usd / exit_liquidity_usd
    return min(10_000.0, (one_way_entry + one_way_exit) * 10_000)


def elapsed_hours(start: str, end: str) -> float:
    return max(
        0.0,
        (parse_time(end) - parse_time(start)).total_seconds()
        / 3_600,
    )


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
