from __future__ import annotations

import math
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from smart_money_radar.prediction.clients import as_float


PREDICTION_WALLET_MODEL_VERSION = "prediction_wallet_v1_3_event_level"
PREDICTION_EVENT_TOKEN_LINK_SOURCE = "deterministic_direct_mention_v2"


def normalize_closed_positions(
    wallet_address: str,
    rows: list[dict[str, Any]],
    observed_at: str,
    history_complete: bool = False,
) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        asset_id = str(
            row.get("asset")
            or f"{row.get('conditionId', '')}:{row.get('outcome', '')}"
        )
        if not asset_id.strip(":"):
            continue
        title = str(row.get("title") or "")
        output.append(
            {
                "venue": "polymarket",
                "wallet_address": wallet_address.lower(),
                "asset_id": asset_id,
                "condition_id": str(row.get("conditionId") or "") or None,
                "event_slug": str(row.get("eventSlug") or row.get("slug") or "")
                or None,
                "title": title,
                "category": classify_prediction_category(title),
                "outcome": str(row.get("outcome") or "") or None,
                "average_price": optional_float(row.get("avgPrice")),
                "total_bought": optional_float(row.get("totalBought")),
                "realized_pnl": optional_float(row.get("realizedPnl")),
                "resolved_price": optional_float(row.get("curPrice")),
                "closed_at": epoch_to_iso(row.get("timestamp")),
                "raw": row,
                "observed_at": observed_at,
                "history_complete": history_complete,
                "history_sort": "timestamp_desc",
            }
        )
    return output


def score_prediction_wallet(
    leaderboard_row: dict[str, Any],
    positions: list[dict[str, Any]],
    observed_at: str,
) -> dict[str, Any]:
    wallet = str(leaderboard_row.get("proxyWallet") or "").lower()
    sample_count = len(positions)
    history_complete = bool(positions) and all(
        bool(row.get("history_complete")) for row in positions
    )
    event_positions = aggregate_event_positions(positions)
    event_count = len(event_positions)
    pnl_values = [as_float(row.get("realized_pnl")) for row in event_positions]
    win_count = sum(value > 0 for value in pnl_values)
    loss_count = sum(value < 0 for value in pnl_values)
    posterior_win_rate = (win_count + 2.0) / (win_count + loss_count + 4.0)
    total_bought_usd = sum(
        as_float(row.get("average_price")) * as_float(row.get("total_bought"))
        for row in event_positions
    )
    total_pnl = sum(pnl_values)
    realized_roi = total_pnl / total_bought_usd if total_bought_usd > 0 else None
    shrunken_roi = (
        realized_roi * event_count / (event_count + 20.0)
        if realized_roi is not None
        else None
    )
    max_drawdown = realized_pnl_drawdown(event_positions)
    positive_pnl = sum(max(0.0, value) for value in pnl_values)
    profit_concentration = (
        max((max(0.0, value) for value in pnl_values), default=0.0) / positive_pnl
        if positive_pnl > 0
        else 1.0
    )
    category_counts = Counter(
        str(row.get("category") or "other") for row in event_positions
    )
    top_category, top_count = category_counts.most_common(1)[0] if category_counts else ("other", 0)
    specialization = top_count / event_count if event_count else 0.0
    confidence = 100.0 * (1.0 - math.exp(-event_count / 25.0))
    positive_scale = max(1.0, positive_pnl)
    drawdown_quality = 1.0 - min(1.0, abs(max_drawdown) / positive_scale)
    roi_quality = 0.5 + 0.5 * math.tanh(2.0 * (shrunken_roi or 0.0))
    quality = 100.0 * (
        0.25 * posterior_win_rate
        + 0.25 * roi_quality
        + 0.15 * drawdown_quality
        + 0.15 * (1.0 - min(1.0, profit_concentration))
        + 0.10 * min(1.0, event_count / 30.0)
        + 0.10 * specialization
    )
    strong_label_allowed = False
    if (
        strong_label_allowed
        and quality >= 72
        and confidence >= 60
        and profit_concentration <= 0.65
        and history_complete
        and total_pnl > 0
        and (shrunken_roi or 0) > 0
    ):
        label = "strong_specialist"
    elif (
        quality >= 58
        and confidence >= 35
        and total_pnl > 0
        and (shrunken_roi or 0) > 0
    ):
        label = "watch"
    elif total_pnl <= 0 or (shrunken_roi or 0) <= 0:
        label = "negative_sample"
    elif profit_concentration > 0.8:
        label = "concentrated_pnl"
    else:
        label = "unproven"
    rationale = [
        f"Bayesian win rate is {posterior_win_rate:.1%} across {event_count} independent event groups.",
        f"Realized PnL is ${total_pnl:,.0f}; shrunk ROI is {(shrunken_roi or 0):.1%}.",
        f"Top category is {top_category} with {specialization:.1%} of observations.",
    ]
    if profit_concentration > 0.65:
        rationale.append(
            f"PnL concentration is high: one position explains {profit_concentration:.1%} of positive PnL."
        )
    if event_count < 20:
        rationale.append("The independent event sample is too small for a high-confidence smart-money claim.")
    rationale.append(
        "Top-PnL leaderboard selection is biased, so institutional/strong labeling is disabled."
    )
    if not history_complete:
        rationale.append(
            "The score uses a capped recent-position window, so strong labeling is disabled."
        )
    if total_pnl <= 0 or (shrunken_roi or 0) <= 0:
        rationale.append("The observed sample is not profitable after ROI shrinkage.")
    return {
        "venue": "polymarket",
        "wallet_address": wallet,
        "model_version": PREDICTION_WALLET_MODEL_VERSION,
        "user_name": str(leaderboard_row.get("userName") or "") or None,
        "leaderboard_rank": optional_int(leaderboard_row.get("rank")),
        "sample_count": sample_count,
        "event_count": event_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "posterior_win_rate": posterior_win_rate,
        "total_bought_usd": total_bought_usd,
        "total_realized_pnl_usd": total_pnl,
        "realized_roi": realized_roi,
        "shrunken_roi": shrunken_roi,
        "max_drawdown_usd": max_drawdown,
        "profit_concentration": profit_concentration,
        "top_category": top_category,
        "specialization_score": specialization,
        "quality_score": max(0.0, min(100.0, quality)),
        "confidence_score": max(0.0, min(100.0, confidence)),
        "label": label,
        "rationale": rationale,
        "evidence": {
            "leaderboard_pnl": as_float(leaderboard_row.get("pnl")),
            "leaderboard_volume": as_float(leaderboard_row.get("vol")),
            "selection_method": "top_all_time_pnl_seed",
            "selection_bias_warning": True,
            "strong_label_allowed": strong_label_allowed,
            "position_count": sample_count,
            "bayesian_denominator": "event_group",
            "closing_line_value_available": False,
            "maker_taker_history_available": False,
            "history_complete": history_complete,
            "history_sort": "timestamp_desc",
            "category_counts": dict(category_counts),
        },
        "observed_at": observed_at,
    }


def score_prediction_wallets(
    leaderboard: list[dict[str, Any]],
    positions_by_wallet: dict[str, list[dict[str, Any]]],
    observed_at: str,
) -> list[dict[str, Any]]:
    scores = []
    for row in leaderboard:
        wallet = str(row.get("proxyWallet") or "").lower()
        if not wallet:
            continue
        scores.append(
            score_prediction_wallet(row, positions_by_wallet.get(wallet, []), observed_at)
        )
    return sorted(
        scores,
        key=lambda row: (row["quality_score"], row["confidence_score"]),
        reverse=True,
    )


def build_event_token_links(
    events: list[dict[str, Any]],
    tokens: list[dict[str, Any]],
    observed_at: str,
) -> list[dict[str, Any]]:
    links = []
    crypto_context = re.compile(
        r"\b(token|crypto|cryptocurrency|blockchain|on-chain|coin|market cap|"
        r"bitcoin|ethereum|solana|base chain|bnb chain)\b",
        re.I,
    )
    for event in events:
        text = "\n".join(
            str(event.get(key) or "")
            for key in ("title", "description", "resolution_rules")
        )
        for token in tokens:
            symbol = str(token.get("symbol") or "").upper().strip()
            name = str(token.get("name") or "").strip()
            confidence = 0.0
            reason = None
            explicit_symbol = bool(
                len(symbol) >= 2
                and re.search(rf"(?<![A-Z0-9])\${re.escape(symbol)}(?![A-Z0-9])", text)
            )
            contextual_symbol = bool(
                len(symbol) >= 4
                and crypto_context.search(text)
                and re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", text)
            )
            contextual_name = bool(
                name
                and len(name) >= 5
                and crypto_context.search(text)
                and re.search(rf"\b{re.escape(name)}\b", text, re.I)
            )
            if contextual_name:
                confidence = 0.96
                reason = f"Crypto-context event text directly mentions token name {name}."
            elif explicit_symbol:
                confidence = 0.94
                reason = f"Event text explicitly mentions cashtag ${symbol}."
            elif contextual_symbol:
                confidence = 0.86
                reason = f"Crypto-context event text directly mentions ticker {symbol}."
            if not reason:
                continue
            links.append(
                {
                    "venue": event["venue"],
                    "event_id": event["event_id"],
                    "token_id": token["token_id"],
                    "chain_id": token.get("chain_id"),
                    "token_address": token.get("token_address"),
                    "token_symbol": symbol,
                    "relation_type": "direct_mention",
                    "confidence_score": confidence,
                    "source": PREDICTION_EVENT_TOKEN_LINK_SOURCE,
                    "rationale": [reason],
                    "created_at": observed_at,
                }
            )
    return links


def realized_pnl_drawdown(positions: list[dict[str, Any]]) -> float:
    ordered = sorted(positions, key=lambda row: str(row.get("closed_at") or ""))
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in ordered:
        cumulative += as_float(row.get("realized_pnl"))
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return max_drawdown


def aggregate_event_positions(
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in positions:
        key = str(
            row.get("event_slug")
            or row.get("condition_id")
            or row.get("asset_id")
        )
        current = grouped.setdefault(
            key,
            {
                "event_key": key,
                "realized_pnl": 0.0,
                "average_price": 0.0,
                "total_bought": 0.0,
                "closed_at": row.get("closed_at"),
                "category": row.get("category") or "other",
            },
        )
        current["realized_pnl"] += as_float(row.get("realized_pnl"))
        bought = as_float(row.get("total_bought"))
        current["total_bought"] += bought
        current["average_price"] += as_float(row.get("average_price")) * bought
        if str(row.get("closed_at") or "") > str(current.get("closed_at") or ""):
            current["closed_at"] = row.get("closed_at")
    for row in grouped.values():
        bought = as_float(row.get("total_bought"))
        row["average_price"] = row["average_price"] / bought if bought > 0 else 0.0
    return sorted(grouped.values(), key=lambda row: str(row.get("closed_at") or ""))


def classify_prediction_category(title: str) -> str:
    text = title.lower()
    categories = (
        ("sports", ("world cup", "nba", "nfl", "mlb", "match", "game", "champion", "score")),
        ("politics", ("election", "president", "congress", "senate", "prime minister", "vote")),
        ("crypto", ("bitcoin", "btc", "ethereum", "eth", "solana", "token", "crypto")),
        ("macro", ("fed", "inflation", "cpi", "gdp", "interest rate", "unemployment", "treasury")),
        ("technology", ("ai model", "openai", "anthropic", "spacex", "apple", "google", "microsoft")),
        ("culture", ("oscar", "emmy", "grammy", "movie", "album", "celebrity")),
        ("geopolitics", ("war", "ceasefire", "sanction", "iran", "ukraine", "nato")),
    )
    for category, keywords in categories:
        if any(keyword in text for keyword in keywords):
            return category
    return "other"


def epoch_to_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def optional_int(value: Any) -> int | None:
    number = optional_float(value)
    return int(number) if number is not None else None
