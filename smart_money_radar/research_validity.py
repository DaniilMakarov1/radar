from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from smart_money_radar.storage import SQLiteStore, normalize_chain_address


OUTCOME_METHODOLOGY_VERSION = "research_outcomes_v3_observation_safe"
WEEKLY_TERMINAL_TOLERANCE_DAYS = 8
MINIMUM_30D_FUTURE_OBSERVATIONS = 3


def run_research_validity(
    store: SQLiteStore,
    chain_id: str = "base",
    as_of: datetime | None = None,
) -> dict[str, Any]:
    now = (as_of or datetime.now(UTC)).astimezone(UTC)
    universe = store.research_universe_rows(chain_id=chain_id)
    targets = store.chain_backtest_targets(chain_id)
    outcomes = build_token_outcomes(universe, targets, now)
    outcome_count = store.replace_research_token_outcomes(
        OUTCOME_METHODOLOGY_VERSION,
        outcomes,
        chain_id=chain_id,
    )
    coverage_rows = build_event_coverage(
        targets=targets,
        universe_rows=universe,
        pre_listing_rows=store.pre_listing_wallet_buy_rows(chain_id=chain_id),
    )
    coverage_count = store.replace_research_event_coverage(
        chain_id,
        coverage_rows,
    )
    return {
        "chain_id": chain_id,
        "universe_snapshot_count": len(universe),
        "outcome_count": outcome_count,
        "event_coverage_count": coverage_count,
        "events_with_pre_announcement_flow": sum(
            int(row["pre_announcement_flow_observed"]) for row in coverage_rows
        ),
        "zero_flow_event_count": sum(
            not row["pre_announcement_flow_observed"] for row in coverage_rows
        ),
        "methodology_version": OUTCOME_METHODOLOGY_VERSION,
    }


def build_token_outcomes(
    universe_rows: list[dict[str, Any]],
    listing_targets: list[dict[str, Any]],
    as_of: datetime,
) -> list[dict[str, Any]]:
    series_by_token: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in universe_rows:
        series_by_token[
            (
                row["chain_id"],
                normalize_chain_address(row["chain_id"], row["token_address"]),
            )
        ].append(row)
    for rows in series_by_token.values():
        rows.sort(key=lambda item: parse_time(item["snapshot_at"]))

    listings_by_token: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for target in listing_targets:
        listings_by_token[
            (
                target["chain_id"],
                normalize_chain_address(
                    target["chain_id"], target["contract_address"]
                ),
            )
        ].append(parse_time(target["announced_at"]))
    for dates in listings_by_token.values():
        dates.sort()

    outcomes = []
    for key, series in series_by_token.items():
        dates = [parse_time(row["snapshot_at"]) for row in series]
        listing_dates = listings_by_token.get(key, [])
        for index, row in enumerate(series):
            snapshot_at = dates[index]
            entry_price = positive_float(row.get("close_price_usd"))
            future_listing = next(
                (date for date in listing_dates if date > snapshot_at),
                None,
            )
            already_listed = any(date <= snapshot_at for date in listing_dates)
            labels = {
                horizon: binary_event_label(
                    snapshot_at=snapshot_at,
                    as_of=as_of,
                    event_at=future_listing,
                    horizon_days=horizon,
                )
                for horizon in (30, 60, 90)
            }
            returns = {
                horizon: forward_return(
                    series=series,
                    dates=dates,
                    start_index=index,
                    entry_price=entry_price,
                    horizon_days=horizon,
                    as_of=as_of,
                )
                for horizon in (7, 30, 60, 90)
            }
            mfe_30, mdd_30 = forward_excursion(
                series, dates, index, entry_price, 30, as_of
            )
            mfe_90, mdd_90 = forward_excursion(
                series, dates, index, entry_price, 90, as_of
            )
            activity_collapse = activity_collapse_label(
                series=series,
                dates=dates,
                start_index=index,
                horizon_days=30,
                as_of=as_of,
            )
            liquidity_collapse, liquidity_is_proxy = liquidity_collapse_label(
                series=series,
                dates=dates,
                start_index=index,
                horizon_days=30,
                as_of=as_of,
                activity_proxy=activity_collapse,
            )
            return_30d = returns[30]
            outcome_coverage = future_observation_coverage(
                dates=dates,
                start_index=index,
                horizon_days=30,
                as_of=as_of,
            )
            rug_proxy = None
            if (
                snapshot_at + timedelta(days=30) <= as_of
                and return_30d is not None
                and activity_collapse is not None
                and liquidity_collapse is not None
            ):
                rug_proxy = bool(
                    activity_collapse
                    and (return_30d <= -0.8 or liquidity_collapse)
                )
            outcomes.append(
                {
                    "chain_id": key[0],
                    "token_address": key[1],
                    "snapshot_at": row["snapshot_at"],
                    "listing_at": future_listing.isoformat() if future_listing else None,
                    "listed_within_30d": labels[30],
                    "listed_within_60d": labels[60],
                    "listed_within_90d": labels[90],
                    "return_7d": returns[7],
                    "return_30d": returns[30],
                    "return_60d": returns[60],
                    "return_90d": returns[90],
                    "max_favorable_excursion_30d": mfe_30,
                    "max_drawdown_30d": mdd_30,
                    "max_favorable_excursion_90d": mfe_90,
                    "max_drawdown_90d": mdd_90,
                    "activity_collapse_30d": activity_collapse,
                    "liquidity_collapse_30d": liquidity_collapse,
                    "rug_proxy_30d": rug_proxy,
                    "labels_matured_through": min(as_of, snapshot_at + timedelta(days=90)).isoformat(),
                    "evidence": {
                        "eligible_for_listing_prediction": not already_listed,
                        "already_listed_at_snapshot": already_listed,
                        "entry_price_usd": entry_price,
                        "liquidity_collapse_is_activity_proxy": liquidity_is_proxy,
                        "future_30d_observation_count": outcome_coverage["observation_count"],
                        "future_30d_terminal_observed": outcome_coverage["terminal_observed"],
                        "missing_future_observations_are_unknown": True,
                        "weekly_sampling": True,
                    },
                }
            )
    return outcomes


def build_event_coverage(
    targets: list[dict[str, Any]],
    universe_rows: list[dict[str, Any]],
    pre_listing_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    universe_by_token: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in universe_rows:
        universe_by_token[
            (
                row["chain_id"],
                normalize_chain_address(row["chain_id"], row["token_address"]),
            )
        ].append(row)

    buys_by_target: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in pre_listing_rows:
        buys_by_target[
            (
                row["chain_id"],
                normalize_chain_address(row["chain_id"], row["token_address"]),
                row["announced_at"],
            )
        ].append(row)

    output = []
    for target in targets:
        chain_id = target["chain_id"]
        token_address = normalize_chain_address(
            chain_id, target["contract_address"]
        )
        announced_at = parse_time(target["announced_at"])
        buy_rows = buys_by_target.get(
            (chain_id, token_address, normalize_time_identity(target["announced_at"])),
            [],
        )
        if not buy_rows:
            buy_rows = [
                row
                for key, rows in buys_by_target.items()
                if key[0] == chain_id
                and key[1] == token_address
                and parse_time(key[2]) == announced_at
                for row in rows
            ]
        prior_universe = [
            row
            for row in universe_by_token.get((chain_id, token_address), [])
            if parse_time(row["snapshot_at"]) <= announced_at
            and parse_time(row["snapshot_at"]) >= announced_at - timedelta(days=14)
        ]
        output.append(
            {
                "listing_event_id": target["listing_event_id"],
                "token_address": token_address,
                "token_symbol": target["symbol"],
                "announced_at": target["announced_at"],
                "pre_announcement_flow_observed": bool(buy_rows),
                "universe_snapshot_observed": bool(prior_universe),
                "first_pre_announcement_trade_at": min(
                    (row["first_buy_at"] for row in buy_rows),
                    default=None,
                ),
                "last_pre_announcement_trade_at": max(
                    (row["last_buy_at"] for row in buy_rows),
                    default=None,
                ),
                "evidence": {
                    "pre_listing_wallet_count": len(buy_rows),
                    "prior_universe_snapshot_count": len(prior_universe),
                    "zero_flow_event_preserved": not bool(buy_rows),
                },
            }
        )
    return output


def binary_event_label(
    snapshot_at: datetime,
    as_of: datetime,
    event_at: datetime | None,
    horizon_days: int,
) -> bool | None:
    end_at = snapshot_at + timedelta(days=horizon_days)
    if event_at is not None and snapshot_at < event_at <= min(end_at, as_of):
        return True
    if as_of < end_at:
        return None
    return False


def forward_return(
    series: list[dict[str, Any]],
    dates: list[datetime],
    start_index: int,
    entry_price: float | None,
    horizon_days: int,
    as_of: datetime,
) -> float | None:
    snapshot_at = dates[start_index]
    target_at = snapshot_at + timedelta(days=horizon_days)
    if entry_price is None or as_of < target_at:
        return None
    index = bisect_left(dates, target_at, lo=start_index + 1)
    if index >= len(series) or dates[index] > target_at + timedelta(days=8):
        return None
    future_price = positive_float(series[index].get("close_price_usd"))
    if future_price is None:
        return None
    return future_price / entry_price - 1


def forward_excursion(
    series: list[dict[str, Any]],
    dates: list[datetime],
    start_index: int,
    entry_price: float | None,
    horizon_days: int,
    as_of: datetime,
) -> tuple[float | None, float | None]:
    snapshot_at = dates[start_index]
    end_at = snapshot_at + timedelta(days=horizon_days)
    if entry_price is None or as_of < end_at:
        return None, None
    future = [
        (row, date)
        for row, date in zip(series[start_index + 1 :], dates[start_index + 1 :])
        if date <= end_at
    ]
    if not future or future[-1][1] < end_at - timedelta(days=WEEKLY_TERMINAL_TOLERANCE_DAYS):
        return None, None
    prices = [
        price
        for row, _ in future
        for price in [positive_float(row.get("close_price_usd"))]
        if price is not None
    ]
    if not prices:
        return None, None
    returns = [price / entry_price - 1 for price in prices]
    return max(returns), min(returns)


def activity_collapse_label(
    series: list[dict[str, Any]],
    dates: list[datetime],
    start_index: int,
    horizon_days: int,
    as_of: datetime,
) -> bool | None:
    snapshot_at = dates[start_index]
    end_at = snapshot_at + timedelta(days=horizon_days)
    if as_of < end_at:
        return None
    current_volume = float(series[start_index].get("volume_usd") or 0)
    future_rows = [
        (row, date)
        for row, date in zip(series[start_index + 1 :], dates[start_index + 1 :])
        if date <= end_at
    ]
    if (
        len(future_rows) < MINIMUM_30D_FUTURE_OBSERVATIONS
        or future_rows[-1][1]
        < end_at - timedelta(days=WEEKLY_TERMINAL_TOLERANCE_DAYS)
    ):
        return None
    future_volume = sum(float(row.get("volume_usd") or 0) for row, _ in future_rows)
    average_future = future_volume / max(1, len(future_rows))
    future_traders = max(int(row.get("trader_count") or 0) for row, _ in future_rows)
    return current_volume >= 5_000 and average_future <= current_volume * 0.1 and future_traders <= 3


def liquidity_collapse_label(
    series: list[dict[str, Any]],
    dates: list[datetime],
    start_index: int,
    horizon_days: int,
    as_of: datetime,
    activity_proxy: bool | None,
) -> tuple[bool | None, bool]:
    snapshot_at = dates[start_index]
    end_at = snapshot_at + timedelta(days=horizon_days)
    if as_of < end_at:
        return None, False
    current_liquidity = positive_float(series[start_index].get("liquidity_usd"))
    future_liquidity = [
        (value, date)
        for row, date in zip(series[start_index + 1 :], dates[start_index + 1 :])
        if date <= end_at
        for value in [positive_float(row.get("liquidity_usd"))]
        if value is not None
    ]
    if (
        current_liquidity is not None
        and future_liquidity
        and future_liquidity[-1][1]
        >= end_at - timedelta(days=WEEKLY_TERMINAL_TOLERANCE_DAYS)
    ):
        return future_liquidity[-1][0] <= current_liquidity * 0.1, False
    # Missing liquidity cannot be inferred from missing trades or sparse snapshots.
    del activity_proxy
    return None, False


def future_observation_coverage(
    dates: list[datetime],
    start_index: int,
    horizon_days: int,
    as_of: datetime,
) -> dict[str, Any]:
    snapshot_at = dates[start_index]
    end_at = snapshot_at + timedelta(days=horizon_days)
    if as_of < end_at:
        return {"observation_count": 0, "terminal_observed": False, "mature": False}
    future_dates = [
        date for date in dates[start_index + 1 :] if date <= end_at
    ]
    terminal_observed = bool(
        future_dates
        and future_dates[-1]
        >= end_at - timedelta(days=WEEKLY_TERMINAL_TOLERANCE_DAYS)
    )
    return {
        "observation_count": len(future_dates),
        "terminal_observed": terminal_observed,
        "mature": True,
    }


def parse_time(value: Any) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def normalize_time_identity(value: Any) -> str:
    return parse_time(value).isoformat()


def positive_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
