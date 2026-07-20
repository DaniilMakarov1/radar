from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime
from statistics import median
from typing import Any


MODEL_VERSION = "wallet_score_v1_2"

MIN_MEANINGFUL_BUY_USD = 1000.0
MIN_MEANINGFUL_NET_USD = 500.0
MAX_MEANINGFUL_PRE_SELL_RATIO = 0.35
MIN_DATASET_TARGETS_FOR_STRONG_CONFIDENCE = 10


def score_wallet_rows(
    rows: list[dict[str, Any]],
    holding_rows: list[dict[str, Any]] | None = None,
    dataset_target_count: int | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["chain_id"], row["wallet_address"].lower())].append(row)

    holding_by_wallet: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in holding_rows or []:
        holding_by_wallet[(row["chain_id"], row["wallet_address"].lower())].append(row)

    observed_target_count = len(
        {
            target_key(row)
            for row in rows
        }
    )
    effective_dataset_target_count = (
        observed_target_count
        if dataset_target_count is None
        else max(0, int(dataset_target_count))
    )

    return [
        score_one_wallet(
            chain_id=chain_id,
            wallet_address=wallet_address,
            rows=wallet_rows,
            holding_rows=holding_by_wallet.get((chain_id, wallet_address), []),
            dataset_target_count=effective_dataset_target_count,
        )
        for (chain_id, wallet_address), wallet_rows in grouped.items()
    ]


def score_one_wallet(
    chain_id: str,
    wallet_address: str,
    rows: list[dict[str, Any]],
    holding_rows: list[dict[str, Any]],
    dataset_target_count: int,
) -> dict[str, Any]:
    evidence_by_target = build_target_evidence(rows, holding_rows)
    meaningful_targets = [row for row in evidence_by_target if row["is_meaningful"]]
    meaningful_symbols = {row["symbol"] for row in meaningful_targets}

    total_buy_trades = sum(int(row["buy_trade_count"]) for row in rows)
    total_gross_buy_usd = sum(float(row["gross_buy_usd"]) for row in rows)
    avg_trade_usd = total_gross_buy_usd / total_buy_trades if total_buy_trades else 0.0
    meaningful_net_pre_usd = sum(float(row["net_pre_usd"]) for row in meaningful_targets)
    meaningful_pre_buy_usd = sum(float(row["pre_buy_usd"]) for row in meaningful_targets)
    median_pre_sell_ratio = median(
        [float(row["pre_sell_ratio"]) for row in meaningful_targets] or [0.0]
    )
    median_post_sell_ratio = median(
        [float(row["post_sell_ratio"]) for row in meaningful_targets] or [0.0]
    )

    first_buy_times = [parse_time(row["first_buy_at"]) for row in rows]
    last_buy_times = [parse_time(row["last_buy_at"]) for row in rows]
    earliest_buy = min(first_buy_times)
    latest_buy = max(last_buy_times)

    lead_days = [float(row["first_lead_days"]) for row in evidence_by_target]
    last_lead_hours = [float(row["last_lead_hours"]) for row in evidence_by_target]
    meaningful_lead_days = [float(row["first_lead_days"]) for row in meaningful_targets]
    score_lead_days = median(meaningful_lead_days or lead_days)
    min_last_lead_hours = min(last_lead_hours)
    max_first_lead_days = max(lead_days)
    active_span_days = days_between(latest_buy, earliest_buy)
    holding_coverage = (
        sum(1 for row in evidence_by_target if row["holding_available"])
        / len(evidence_by_target)
    )
    target_concentration = compute_target_concentration(meaningful_targets)

    metrics = {
        "raw_target_count": len(evidence_by_target),
        "meaningful_target_count": len(meaningful_targets),
        "dataset_target_count": dataset_target_count,
        "total_buy_trades": total_buy_trades,
        "total_gross_buy_usd": total_gross_buy_usd,
        "meaningful_pre_buy_usd": meaningful_pre_buy_usd,
        "meaningful_net_pre_usd": meaningful_net_pre_usd,
        "avg_trade_usd": avg_trade_usd,
        "score_lead_days": score_lead_days,
        "min_last_lead_hours": min_last_lead_hours,
        "holding_coverage": holding_coverage,
        "target_concentration": target_concentration,
        "median_pre_sell_ratio": median_pre_sell_ratio,
        "median_post_sell_ratio": median_post_sell_ratio,
    }
    flags = build_flags(metrics, evidence_by_target)
    interest_score = compute_interest_score(metrics, flags)
    noise_score = compute_noise_score(metrics, flags)
    confidence_score = compute_confidence_score(metrics, flags)
    label = classify_wallet(
        interest_score=interest_score,
        noise_score=noise_score,
        confidence_score=confidence_score,
        meaningful_target_count=len(meaningful_targets),
    )
    rationale = build_wallet_rationale(
        metrics=metrics,
        meaningful_targets=meaningful_targets,
        flags=flags,
        label=label,
    )

    evidence = {
        "model_note": (
            "V1.2 keeps only fund-sized target entries and caps "
            "confidence when the historical target sample is small."
        ),
        "raw_target_count": len(evidence_by_target),
        "meaningful_target_count": len(meaningful_targets),
        "dataset_target_count": dataset_target_count,
        "symbols": sorted({row["symbol"] for row in evidence_by_target}),
        "meaningful_symbols": sorted(meaningful_symbols),
        "meaningful_pre_buy_usd": round(meaningful_pre_buy_usd, 6),
        "meaningful_net_pre_usd": round(meaningful_net_pre_usd, 6),
        "holding_coverage": round(holding_coverage, 6),
        "target_concentration": round(target_concentration, 6),
        "median_pre_sell_ratio": round(median_pre_sell_ratio, 6),
        "median_post_sell_ratio": round(median_post_sell_ratio, 6),
        "repeatability_status": repeatability_status(
            meaningful_target_count=len(meaningful_targets),
            dataset_target_count=dataset_target_count,
        ),
        "targets": evidence_by_target,
        **rationale,
    }

    return {
        "wallet_address": wallet_address,
        "chain_id": chain_id,
        "model_version": MODEL_VERSION,
        "interest_score": round(interest_score, 2),
        "noise_score": round(noise_score, 2),
        "confidence_score": round(confidence_score, 2),
        "label": label,
        # Existing storage columns keep working, but now target_count is meaningful.
        "target_count": len(meaningful_targets),
        "symbol_count": len(meaningful_symbols),
        "total_buy_trades": total_buy_trades,
        "total_gross_buy_usd": round(total_gross_buy_usd, 6),
        "avg_trade_usd": round(avg_trade_usd, 6),
        "earliest_buy_at": earliest_buy.isoformat(),
        "latest_buy_at": latest_buy.isoformat(),
        "max_first_lead_days": round(max_first_lead_days, 4),
        "min_last_lead_hours": round(min_last_lead_hours, 4),
        "active_span_days": round(active_span_days, 4),
        "flags": flags,
        "evidence": evidence,
    }


def build_target_evidence(
    rows: list[dict[str, Any]],
    holding_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    holding_by_target = {target_key(row): row for row in holding_rows}
    evidence = []
    for row in rows:
        holding = holding_by_target.get(target_key(row))
        gross_buy_usd = float(row["gross_buy_usd"] or 0)
        pre_buy_usd = float(holding["pre_buy_usd"] or 0) if holding else gross_buy_usd
        pre_sell_usd = float(holding["pre_sell_usd"] or 0) if holding else 0.0
        post_sell_usd = float(holding["post_sell_usd"] or 0) if holding else 0.0
        net_pre_usd = float(holding["net_pre_usd"] or 0) if holding else pre_buy_usd
        pre_sell_ratio = safe_ratio(pre_sell_usd, pre_buy_usd)
        post_sell_ratio = safe_ratio(post_sell_usd, pre_buy_usd)
        holding_label = holding["holding_label"] if holding else "holding_unknown"
        announced_at = parse_time(row["announced_at"])
        first_buy_at = parse_time(row["first_buy_at"])
        last_buy_at = parse_time(row["last_buy_at"])

        meaningful = (
            pre_buy_usd >= MIN_MEANINGFUL_BUY_USD
            and net_pre_usd >= MIN_MEANINGFUL_NET_USD
            and pre_sell_ratio < MAX_MEANINGFUL_PRE_SELL_RATIO
            and holding_label not in {
                "dust_accumulator",
                "dust_or_prior_holder",
                "pre_listing_flipper",
                "partial_pre_listing_seller",
            }
        )
        if meaningful and post_sell_ratio >= 0.8:
            behavior = "post_listing_exit"
        elif meaningful and post_sell_ratio >= 0.35:
            behavior = "partial_post_listing_exit"
        elif meaningful:
            behavior = "accumulator"
        elif pre_sell_ratio >= MAX_MEANINGFUL_PRE_SELL_RATIO:
            behavior = "pre_listing_trader"
        elif pre_buy_usd < MIN_MEANINGFUL_BUY_USD:
            behavior = "dust"
        else:
            behavior = "weak_net_accumulation"

        evidence.append(
            {
                "chain_id": row["chain_id"],
                "symbol": row["symbol"],
                "token_address": row["token_address"].lower(),
                "announced_at": announced_at.isoformat(),
                "first_buy_at": first_buy_at.isoformat(),
                "last_buy_at": last_buy_at.isoformat(),
                "gross_buy_usd": round(gross_buy_usd, 6),
                "pre_buy_usd": round(pre_buy_usd, 6),
                "pre_sell_usd": round(pre_sell_usd, 6),
                "post_sell_usd": round(post_sell_usd, 6),
                "net_pre_usd": round(net_pre_usd, 6),
                "pre_sell_ratio": round(pre_sell_ratio, 6),
                "post_sell_ratio": round(post_sell_ratio, 6),
                "buy_trade_count": int(row["buy_trade_count"]),
                "first_lead_days": round(days_between(announced_at, first_buy_at), 6),
                "last_lead_hours": round(hours_between(announced_at, last_buy_at), 6),
                "holding_label": holding_label,
                "holding_available": holding is not None,
                "behavior": behavior,
                "is_meaningful": meaningful,
            }
        )
    return sorted(evidence, key=lambda item: (item["announced_at"], item["symbol"]))


def build_flags(
    metrics: dict[str, Any],
    target_evidence: list[dict[str, Any]],
) -> list[str]:
    flags = []
    meaningful_count = int(metrics["meaningful_target_count"])
    dataset_target_count = int(metrics["dataset_target_count"])

    if dataset_target_count < MIN_DATASET_TARGETS_FOR_STRONG_CONFIDENCE:
        flags.append("insufficient_dataset_targets")
    if meaningful_count == 0:
        flags.append("no_meaningful_targets")
    elif meaningful_count == 1:
        flags.append("one_meaningful_target")
    else:
        flags.append(f"meaningful_repeat_{meaningful_count}")

    if any(row["behavior"] == "dust" for row in target_evidence):
        flags.append("dust_target_present")
    if any(row["behavior"] == "pre_listing_trader" for row in target_evidence):
        flags.append("pre_listing_trading_present")
    if any(
        row["behavior"] in {"post_listing_exit", "partial_post_listing_exit"}
        for row in target_evidence
    ):
        flags.append("post_listing_realization")
    if metrics["target_concentration"] >= 0.95 and meaningful_count >= 2:
        flags.append("dominant_single_target")

    if metrics["score_lead_days"] >= 30:
        flags.append("median_entry_30d")
    elif metrics["score_lead_days"] >= 14:
        flags.append("median_entry_14d")
    elif metrics["score_lead_days"] >= 7:
        flags.append("median_entry_7d")
    if metrics["min_last_lead_hours"] <= 6:
        flags.append("continued_buying_until_announcement")

    if metrics["meaningful_net_pre_usd"] >= 500_000:
        flags.append("very_high_meaningful_notional")
    elif metrics["meaningful_net_pre_usd"] >= 100_000:
        flags.append("high_meaningful_notional")
    if metrics["total_buy_trades"] >= 3_000:
        flags.append("extreme_trade_count")
    elif metrics["total_buy_trades"] >= 1_000:
        flags.append("very_high_trade_count")
    elif metrics["total_buy_trades"] >= 250:
        flags.append("high_trade_count")
    if metrics["avg_trade_usd"] < 100 and metrics["total_buy_trades"] >= 100:
        flags.append("small_average_trade_size")
    if metrics["holding_coverage"] < 1:
        flags.append("partial_holding_coverage")
    return flags


def compute_interest_score(metrics: dict[str, Any], flags: list[str]) -> float:
    meaningful_count = int(metrics["meaningful_target_count"])
    if meaningful_count == 0:
        return clamp(10.0 + min(20.0, math.log10(max(metrics["total_gross_buy_usd"], 1.0)) * 3.0))

    score = 24.0
    score += min(
        20.0,
        math.log10(max(metrics["meaningful_net_pre_usd"], 1.0)) * 3.5,
    )
    if metrics["score_lead_days"] >= 30:
        score += 15.0
    elif metrics["score_lead_days"] >= 14:
        score += 11.0
    elif metrics["score_lead_days"] >= 7:
        score += 7.0
    elif metrics["score_lead_days"] >= 1:
        score += 3.0

    if metrics["avg_trade_usd"] >= 5_000:
        score += 9.0
    elif metrics["avg_trade_usd"] >= 1_000:
        score += 7.0
    elif metrics["avg_trade_usd"] >= 250:
        score += 4.0

    if meaningful_count >= 5:
        score += 36.0
    elif meaningful_count >= 3:
        score += 28.0
    elif meaningful_count == 2:
        score += 20.0

    if "dominant_single_target" in flags:
        score -= 10.0
    if "pre_listing_trading_present" in flags:
        score -= 6.0
    if "extreme_trade_count" in flags:
        score -= 14.0
    elif "very_high_trade_count" in flags:
        score -= 8.0
    if "small_average_trade_size" in flags:
        score -= 7.0

    if meaningful_count == 1:
        score = min(score, 68.0)
    return clamp(score)


def compute_noise_score(metrics: dict[str, Any], flags: list[str]) -> float:
    score = 8.0
    trades = int(metrics["total_buy_trades"])
    if trades >= 8_000:
        score += 60.0
    elif trades >= 3_000:
        score += 48.0
    elif trades >= 1_000:
        score += 34.0
    elif trades >= 250:
        score += 18.0
    elif trades >= 100:
        score += 10.0

    if "small_average_trade_size" in flags:
        score += 18.0
    if metrics["min_last_lead_hours"] <= 1:
        score += 8.0
    elif metrics["min_last_lead_hours"] <= 6:
        score += 4.0
    if "pre_listing_trading_present" in flags:
        score += 16.0
    if "no_meaningful_targets" in flags:
        score += 12.0
    if "dominant_single_target" in flags:
        score += 8.0
    if metrics["meaningful_target_count"] >= 2 and metrics["target_concentration"] < 0.9:
        score -= 6.0
    return clamp(score)


def compute_confidence_score(metrics: dict[str, Any], flags: list[str]) -> float:
    meaningful_count = int(metrics["meaningful_target_count"])
    dataset_target_count = int(metrics["dataset_target_count"])
    score = 10.0
    score += min(45.0, meaningful_count * 18.0)
    score += min(20.0, dataset_target_count * 2.0)
    score += metrics["holding_coverage"] * 8.0
    if meaningful_count >= 2 and metrics["target_concentration"] < 0.9:
        score += 5.0
    if "pre_listing_trading_present" in flags:
        score -= 6.0
    if "extreme_trade_count" in flags:
        score -= 8.0

    if meaningful_count == 0:
        score = min(score, 25.0)
    elif meaningful_count == 1:
        score = min(score, 45.0)
    if dataset_target_count < MIN_DATASET_TARGETS_FOR_STRONG_CONFIDENCE:
        score = min(score, 55.0)
    return clamp(score)


def classify_wallet(
    interest_score: float,
    noise_score: float,
    confidence_score: float,
    meaningful_target_count: int,
) -> str:
    if noise_score >= 70:
        return "likely_noise"
    if (
        meaningful_target_count >= 2
        and interest_score >= 72
        and confidence_score >= 65
        and noise_score < 50
    ):
        return "strong_candidate"
    if (
        meaningful_target_count >= 1
        and interest_score >= 52
        and confidence_score >= 30
        and noise_score < 65
    ):
        return "watch_candidate"
    if meaningful_target_count >= 1 or interest_score >= 35:
        return "weak_candidate"
    return "ignore"


def repeatability_status(
    meaningful_target_count: int,
    dataset_target_count: int,
) -> str:
    if meaningful_target_count == 0:
        return "no_meaningful_history"
    if meaningful_target_count == 1:
        return "single_meaningful_target"
    if dataset_target_count < MIN_DATASET_TARGETS_FOR_STRONG_CONFIDENCE:
        return "repeat_observed_dataset_insufficient"
    return "meaningful_repeat"


def build_wallet_rationale(
    metrics: dict[str, Any],
    meaningful_targets: list[dict[str, Any]],
    flags: list[str],
    label: str,
) -> dict[str, Any]:
    meaningful_count = int(metrics["meaningful_target_count"])
    symbols = sorted({row["symbol"] for row in meaningful_targets})
    why = []
    counter = []

    if meaningful_count:
        why.append(
            f"{meaningful_count} экономически значимых накоплений до публичного "
            f"Binance cutoff: {', '.join(symbols)}."
        )
        why.append(
            f"Чистое накопление до cutoff: ${metrics['meaningful_net_pre_usd']:,.0f}; "
            f"медианная доля ранних продаж {metrics['median_pre_sell_ratio']:.1%}."
        )
        why.append(
            f"Медианный вход за {metrics['score_lead_days']:.1f} дн. до объявления."
        )
        if metrics["median_post_sell_ratio"] >= 0.35:
            why.append(
                "Основная реализация происходила после события, а не до него."
            )
        if meaningful_count >= 2 and metrics["target_concentration"] < 0.9:
            why.append("Результат повторялся и не сводится к одной позиции.")
    else:
        counter.append("Нет ни одного накопления, прошедшего meaningful-фильтры.")

    if meaningful_count == 1:
        counter.append("Пока это один удачный target, repeatability не доказана.")
    if metrics["dataset_target_count"] < MIN_DATASET_TARGETS_FOR_STRONG_CONFIDENCE:
        counter.append(
            f"Историческая выборка мала: {metrics['dataset_target_count']} целей из "
            f"необходимых {MIN_DATASET_TARGETS_FOR_STRONG_CONFIDENCE}."
        )
    if "dominant_single_target" in flags:
        counter.append("Более 95% meaningful notional приходится на один токен.")
    if "pre_listing_trading_present" in flags:
        counter.append("Есть заметные продажи до cutoff, возможен обычный трейдинг.")
    if "continued_buying_until_announcement" in flags:
        counter.append("Покупки продолжались почти до объявления; lead time неоднороден.")
    if "high_trade_count" in flags or "very_high_trade_count" in flags or "extreme_trade_count" in flags:
        counter.append("Высокая частота сделок похожа на бот/маркет-мейкинг и требует проверки.")
    if "partial_holding_coverage" in flags:
        counter.append("Sell-side история покрыта не для всех целей.")

    verdict = {
        "strong_candidate": "Повторяемый smart-money кандидат.",
        "watch_candidate": "Наблюдаемый кандидат; нужны новые независимые подтверждения.",
        "weak_candidate": "Слабая гипотеза, данных для smart-money статуса недостаточно.",
        "likely_noise": "Поведение больше похоже на шум или систематический трейдинг.",
        "ignore": "Оснований считать адрес smart money сейчас нет.",
    }[label]
    return {
        "why_smart_money": why,
        "counter_evidence": counter,
        "verdict": verdict,
    }


def compute_target_concentration(targets: list[dict[str, Any]]) -> float:
    notionals = [max(0.0, float(row["net_pre_usd"])) for row in targets]
    total = sum(notionals)
    if total <= 0:
        return 1.0
    return max(notionals) / total


def target_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        row["chain_id"],
        row["symbol"],
        row["token_address"].lower(),
        normalize_time_text(row["announced_at"]),
    )


def normalize_time_text(value: str) -> str:
    return parse_time(value).isoformat()


def safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def parse_time(value: str) -> datetime:
    text = str(value).strip().replace(" UTC", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def days_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 86400


def hours_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))
