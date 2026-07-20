from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from scipy.stats import beta as beta_distribution

from smart_money_radar.config import PROJECT_ROOT
from smart_money_radar.dune_queries import write_wallet_opportunity_sql
from smart_money_radar.ingestion.dune import DuneAPIError, DuneClient, write_json
from smart_money_radar.research_validity import parse_time, positive_float
from smart_money_radar.scoring.wallets import MODEL_VERSION
from smart_money_radar.storage import SQLiteStore


WALLET_RESEARCH_MODEL_VERSION = "wallet_research_v2"
WALLET_OPPORTUNITY_SOURCE = "dune_wallet_opportunities_v2"


def research_wallet_selection(
    store: SQLiteStore,
    chain_id: str = "base",
    max_wallets: int = 200,
) -> list[dict[str, Any]]:
    candidates = store.wallet_score_rows(
        model_version=MODEL_VERSION,
        limit=max(1_000, max_wallets * 5),
        labels=("strong_candidate", "watch_candidate", "weak_candidate"),
    )
    excluded = store.excluded_wallet_addresses(chain_id)
    eligible = [
        row
        for row in candidates
        if row["chain_id"] == chain_id
        and row["wallet_address"].lower() not in excluded
    ]
    eligible.sort(
        key=lambda row: (
            int(row.get("target_count") or 0) >= 2,
            int(row.get("target_count") or 0),
            float(row.get("interest_score") or 0),
            float(row.get("confidence_score") or 0),
        ),
        reverse=True,
    )
    return eligible[:max_wallets]


def run_wallet_opportunity_backfill(
    store: SQLiteStore,
    chain_id: str = "base",
    max_wallets: int = 50,
    client: DuneClient | None = None,
    timeout_seconds: int = 1_800,
) -> dict[str, Any]:
    wallets = research_wallet_selection(store, chain_id, max_wallets)
    if not wallets:
        return {
            "chain_id": chain_id,
            "wallet_count": 0,
            "opportunity_count": 0,
            "score_count": 0,
        }
    sql_path = (
        PROJECT_ROOT
        / "queries"
        / "generated"
        / f"{chain_id}_wallet_opportunities_v2.generated.sql"
    )
    result_path = (
        PROJECT_ROOT
        / "exports"
        / f"{chain_id}_wallet_opportunities_v2.json"
    )
    write_wallet_opportunity_sql(
        sql_path,
        chain_id=chain_id,
        wallets=wallets,
    )
    dune = client or DuneClient()
    dune.require_credit_reserve()
    execution = dune.execute_sql(sql_path.read_text(encoding="utf-8"), performance="medium")
    execution_id = execution["execution_id"]
    store.record_dune_execution(
        execution_id=execution_id,
        source=WALLET_OPPORTUNITY_SOURCE,
        state=execution.get("state", "submitted"),
        sql_file=str(sql_path),
        output_file=str(result_path),
    )
    try:
        status = dune.wait_for_execution(
            execution_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=20,
        )
        export_plan = dune.require_affordable_export(status)
        payload = dune.execution_results_all_cached(
            execution_id,
            cache_root=PROJECT_ROOT / "exports" / "dune_page_cache",
        )
        payload.setdefault("result", {}).setdefault("metadata", {})[
            "export_plan"
        ] = export_plan
    except DuneAPIError as exc:
        store.finish_dune_execution(
            execution_id,
            state="failed",
            error=str(exc),
            output_file=str(result_path),
        )
        raise
    write_json(result_path, payload)
    raw_rows = payload.get("result", {}).get("rows", [])
    store.finish_dune_execution(
        execution_id,
        state=payload.get("state", "QUERY_STATE_COMPLETED"),
        row_count=len(raw_rows),
        output_file=str(result_path),
    )
    annotated = annotate_wallet_opportunities(
        chain_id=chain_id,
        rows=raw_rows,
        universe_rows=store.research_universe_rows(chain_id=chain_id),
        listing_targets=store.chain_backtest_targets(chain_id),
        as_of=datetime.now(UTC),
    )
    opportunity_count = store.replace_wallet_token_opportunities(
        chain_id=chain_id,
        source=WALLET_OPPORTUNITY_SOURCE,
        rows=annotated,
        execution_id=execution_id,
    )
    scores = score_wallet_opportunities(annotated)
    score_count = store.upsert_wallet_research_scores(scores)
    return {
        "chain_id": chain_id,
        "wallet_count": len(wallets),
        "opportunity_count": opportunity_count,
        "score_count": score_count,
        "execution_id": execution_id,
        "sql_path": str(sql_path),
        "result_path": str(result_path),
    }


def rescore_wallet_opportunities(
    store: SQLiteStore,
    chain_id: str = "base",
) -> dict[str, Any]:
    rows = store.wallet_token_opportunity_rows(chain_id)
    scores = score_wallet_opportunities(rows)
    count = store.upsert_wallet_research_scores(scores)
    return {
        "chain_id": chain_id,
        "opportunity_count": len(rows),
        "score_count": count,
        "model_version": WALLET_RESEARCH_MODEL_VERSION,
    }


def annotate_wallet_opportunities(
    chain_id: str,
    rows: list[dict[str, Any]],
    universe_rows: list[dict[str, Any]],
    listing_targets: list[dict[str, Any]],
    as_of: datetime,
) -> list[dict[str, Any]]:
    series_by_token: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in universe_rows:
        series_by_token[row["token_address"].lower()].append(row)
    for series in series_by_token.values():
        series.sort(key=lambda item: parse_time(item["snapshot_at"]))

    listings_by_token: dict[str, list[datetime]] = defaultdict(list)
    for target in listing_targets:
        listings_by_token[target["contract_address"].lower()].append(
            parse_time(target["announced_at"])
        )
    for dates in listings_by_token.values():
        dates.sort()

    output = []
    for raw in rows:
        row = dict(raw)
        token_address = str(row.get("token_address") or "").lower()
        first_buy_at = parse_time(row["first_buy_at"])
        average_buy = positive_float(row.get("average_buy_price_usd"))
        listings = listings_by_token.get(token_address, [])
        listing_at = next((date for date in listings if date > first_buy_at), None)
        maturity_at = first_buy_at + timedelta(days=90)
        outcome_matured = as_of >= maturity_at
        series = series_by_token.get(token_address, [])
        price_path = [
            (parse_time(item["snapshot_at"]), positive_float(item.get("close_price_usd")))
            for item in series
            if first_buy_at < parse_time(item["snapshot_at"]) <= min(maturity_at, as_of)
        ]
        prices = [price for _, price in price_path if price is not None]
        mark_observed_at = price_path[-1][0] if price_path else None
        mark_fresh = bool(
            mark_observed_at
            and maturity_at - mark_observed_at <= timedelta(days=8)
        )
        mark_price = prices[-1] if prices and (not outcome_matured or mark_fresh) else None
        gross_buy = float(row.get("gross_buy_usd") or 0)
        gross_sell = float(row.get("gross_sell_usd") or 0)
        bought_amount = float(row.get("token_bought_amount") or 0)
        sold_amount = float(row.get("token_sold_amount") or 0)
        remaining_amount = max(0.0, bought_amount - sold_amount)
        last_buy_at = parse_time(row["last_buy_at"])
        last_sell_at = (
            parse_time(row["last_sell_at"])
            if row.get("last_sell_at")
            else None
        )
        cash_flow_horizon_valid = bool(
            last_buy_at <= maturity_at
            and (last_sell_at is None or last_sell_at <= maturity_at)
        )
        inventory_reconciled = bool(
            bought_amount > 0 and sold_amount <= bought_amount * 1.05
        )
        estimated_pnl = (
            gross_sell + remaining_amount * mark_price - gross_buy
            if mark_price is not None
            and cash_flow_horizon_valid
            and inventory_reconciled
            else gross_sell - gross_buy if sold_amount >= bought_amount * 0.95 else None
        )
        if not cash_flow_horizon_valid or not inventory_reconciled:
            estimated_pnl = None
        estimated_return = estimated_pnl / gross_buy if estimated_pnl is not None and gross_buy > 0 else None
        excursions = (
            [price / average_buy - 1 for price in prices]
            if average_buy is not None
            else []
        )
        exited = bought_amount > 0 and sold_amount >= bought_amount * 0.8
        holding_end = last_sell_at if exited and last_sell_at else min(as_of, maturity_at)
        holding_days = max(0.0, (holding_end - first_buy_at).total_seconds() / 86_400)
        average_sell = positive_float(row.get("average_sell_price_usd"))
        exit_quality = compute_exit_quality(
            average_buy=average_buy,
            average_sell=average_sell,
            max_favorable_excursion=max(excursions) if excursions else None,
            exited=exited,
        )
        if not cash_flow_horizon_valid or not inventory_reconciled:
            exit_quality = None
        row.update(
            {
                "chain_id": chain_id,
                "wallet_address": str(row["wallet_address"]).lower(),
                "token_address": token_address,
                "listing_at": listing_at.isoformat() if listing_at else None,
                "listed_within_30d": within_horizon(first_buy_at, listing_at, 30, as_of),
                "listed_within_60d": within_horizon(first_buy_at, listing_at, 60, as_of),
                "listed_within_90d": within_horizon(first_buy_at, listing_at, 90, as_of),
                "outcome_matured": outcome_matured,
                "mark_price_usd": mark_price,
                "estimated_pnl_usd": estimated_pnl,
                "estimated_return": estimated_return,
                "max_favorable_excursion_90d": max(excursions) if excursions else None,
                "max_drawdown_90d": min(excursions) if excursions else None,
                "holding_days": holding_days,
                "exit_quality_score": exit_quality,
                "evidence": {
                    "price_observation_count": len(prices),
                    "outcome_maturity_at": maturity_at.isoformat(),
                    "remaining_token_amount": remaining_amount,
                    "cash_flow_horizon_valid": cash_flow_horizon_valid,
                    "inventory_reconciled": inventory_reconciled,
                    "mark_observed_at": (
                        mark_observed_at.isoformat() if mark_observed_at else None
                    ),
                    "mark_fresh_for_maturity": mark_fresh,
                    "position_size_basis": "share_of_observed_dex_buys",
                    "pnl_method": "episode_cash_flows_capped_at_90d_plus_fresh_mark",
                },
            }
        )
        output.append(row)
    return output


def score_wallet_opportunities(
    rows: list[dict[str, Any]],
    prior_strength: float = 20.0,
) -> list[dict[str, Any]]:
    matured_rows = [row for row in rows if bool(row.get("outcome_matured"))]
    total_matured = len(matured_rows)
    total_hits = sum(bool(row.get("listed_within_90d")) for row in matured_rows)
    baseline_rate = total_hits / total_matured if total_matured else 0.01
    baseline_rate = min(0.5, max(0.001, baseline_rate))
    prior_alpha = baseline_rate * prior_strength
    prior_beta = (1 - baseline_rate) * prior_strength

    by_wallet: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_wallet[(row["chain_id"], row["wallet_address"].lower())].append(row)

    scores = []
    for (chain_id, wallet_address), opportunities in by_wallet.items():
        matured = [row for row in opportunities if bool(row.get("outcome_matured"))]
        hits_30 = sum(bool(row.get("listed_within_30d")) for row in matured)
        hits_60 = sum(bool(row.get("listed_within_60d")) for row in matured)
        hits_90 = sum(bool(row.get("listed_within_90d")) for row in matured)
        misses = len(matured) - hits_90
        alpha = prior_alpha + hits_90
        beta = prior_beta + misses
        posterior = alpha / (alpha + beta)
        lower = float(beta_distribution.ppf(0.025, alpha, beta))
        upper = float(beta_distribution.ppf(0.975, alpha, beta))
        lift = posterior / baseline_rate if baseline_rate > 0 else None
        returns = numeric_values(matured, "estimated_return")
        pnl_values = numeric_values(matured, "estimated_pnl_usd")
        drawdowns = numeric_values(matured, "max_drawdown_90d")
        turnovers = numeric_values(matured, "turnover_ratio")
        position_shares = numeric_values(matured, "position_to_observed_flow")
        exits = numeric_values(matured, "exit_quality_score")
        median_return = median(returns) if returns else None
        win_rate = sum(value > 0 for value in returns) / len(returns) if returns else None
        median_drawdown = median(drawdowns) if drawdowns else None
        median_turnover = median(turnovers) if turnovers else None
        median_position = median(position_shares) if position_shares else None
        median_exit = median(exits) if exits else None
        edge_component = clamp(50 + 18 * math.log2(max(0.125, lift or 0.125)))
        pnl_component = 50.0 if median_return is None else clamp(50 + median_return * 35)
        risk_component = 50.0 if median_drawdown is None else clamp(100 + median_drawdown * 100)
        exit_component = median_exit if median_exit is not None else 50.0
        research_score = (
            edge_component * 0.5
            + pnl_component * 0.2
            + risk_component * 0.2
            + exit_component * 0.1
        )
        confidence = clamp(100 * (1 - math.exp(-len(matured) / 25)))
        label = classify_research_wallet(
            matured_count=len(matured),
            posterior=posterior,
            lower=lower,
            baseline=baseline_rate,
            median_return=median_return,
            median_drawdown=median_drawdown,
        )
        rationale = build_wallet_research_rationale(
            matured_count=len(matured),
            hits=hits_90,
            misses=misses,
            posterior=posterior,
            lower=lower,
            upper=upper,
            baseline=baseline_rate,
            median_return=median_return,
        )
        scores.append(
            {
                "chain_id": chain_id,
                "wallet_address": wallet_address,
                "model_version": WALLET_RESEARCH_MODEL_VERSION,
                "opportunity_count": len(opportunities),
                "matured_opportunity_count": len(matured),
                "hit_count_30d": hits_30,
                "hit_count_60d": hits_60,
                "hit_count_90d": hits_90,
                "miss_count_90d": misses,
                "baseline_rate": baseline_rate,
                "posterior_hit_rate": posterior,
                "posterior_lower_95": lower,
                "posterior_upper_95": upper,
                "posterior_lift": lift,
                "estimated_pnl_usd": sum(pnl_values) if pnl_values else None,
                "median_return": median_return,
                "win_rate": win_rate,
                "median_max_drawdown": median_drawdown,
                "median_turnover": median_turnover,
                "median_position_to_flow": median_position,
                "median_exit_quality": median_exit,
                "research_score": research_score,
                "confidence_score": confidence,
                "label": label,
                "rationale": rationale,
                "evidence": {
                    "prior_strength": prior_strength,
                    "prior_alpha": prior_alpha,
                    "prior_beta": prior_beta,
                    "baseline_opportunity_count": total_matured,
                    "score_components": {
                        "edge": edge_component,
                        "pnl": pnl_component,
                        "risk": risk_component,
                        "exit": exit_component,
                    },
                },
            }
        )
    return scores


def classify_research_wallet(
    matured_count: int,
    posterior: float,
    lower: float,
    baseline: float,
    median_return: float | None,
    median_drawdown: float | None,
) -> str:
    if matured_count < 10:
        return "insufficient_denominator"
    if upper_edge_is_negative(posterior, baseline) or (
        median_return is not None and median_return < -0.35
    ):
        return "negative_edge"
    if (
        matured_count >= 25
        and lower > baseline * 1.5
        and (median_return is None or median_return > 0)
        and (median_drawdown is None or median_drawdown > -0.8)
    ):
        return "institutional_candidate"
    if posterior > baseline * 1.5:
        return "promising"
    return "unproven"


def upper_edge_is_negative(posterior: float, baseline: float) -> bool:
    return posterior < baseline * 0.75


def build_wallet_research_rationale(
    matured_count: int,
    hits: int,
    misses: int,
    posterior: float,
    lower: float,
    upper: float,
    baseline: float,
    median_return: float | None,
) -> list[str]:
    rationale = [
        f"Полный denominator: {matured_count} зрелых token opportunities, {hits} Binance hits и {misses} misses.",
        f"Bayesian 90d hit-rate {posterior:.2%}, 95% interval {lower:.2%}-{upper:.2%}, baseline {baseline:.2%}.",
    ]
    if median_return is not None:
        rationale.append(f"Медианная 90d mark-to-market доходность {median_return:.1%}.")
    else:
        rationale.append("Исторического ценового покрытия пока недостаточно для PnL-вывода.")
    return rationale


def within_horizon(
    start_at: datetime,
    event_at: datetime | None,
    horizon_days: int,
    as_of: datetime,
) -> bool | None:
    end_at = start_at + timedelta(days=horizon_days)
    if event_at is not None and start_at < event_at <= min(end_at, as_of):
        return True
    if as_of < end_at:
        return None
    return False


def compute_exit_quality(
    average_buy: float | None,
    average_sell: float | None,
    max_favorable_excursion: float | None,
    exited: bool,
) -> float | None:
    if average_buy is None:
        return None
    if not exited or average_sell is None:
        return 50.0
    realized_return = average_sell / average_buy - 1
    if max_favorable_excursion is None or max_favorable_excursion <= 0:
        return clamp(50 + realized_return * 50)
    capture = realized_return / max_favorable_excursion
    return clamp(50 + capture * 50)


def numeric_values(rows: list[dict[str, Any]], key: str) -> list[float]:
    values = []
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    return values


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return min(high, max(low, value))
