from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

from smart_money_radar.config import PROJECT_ROOT
from smart_money_radar.dune_queries import (
    render_base_wallet_entities_sql,
    render_base_wallet_funding_sql,
)
from smart_money_radar.ingestion.dune import DuneAPIError, DuneClient, write_json
from smart_money_radar.scoring.wallets import MODEL_VERSION
from smart_money_radar.storage import SQLiteStore
from smart_money_radar.wallet_research import research_wallet_selection


CANDIDATE_LABELS = (
    "strong_candidate",
    "watch_candidate",
)
DEFAULT_ENTITY_SQL_DIR = PROJECT_ROOT / "queries" / "generated" / "wallet_entities"
DEFAULT_ENTITY_RESULT_DIR = PROJECT_ROOT / "exports" / "wallet_entities"
DEFAULT_FUNDING_SQL_DIR = PROJECT_ROOT / "queries" / "generated" / "wallet_funding"
DEFAULT_FUNDING_RESULT_DIR = PROJECT_ROOT / "exports" / "wallet_funding"


def research_candidate_wallets(
    store: SQLiteStore,
    max_wallets: int = 1_500,
) -> list[dict[str, Any]]:
    rows = store.wallet_score_rows(
        model_version=MODEL_VERSION,
        limit=max_wallets,
        labels=CANDIDATE_LABELS,
    )
    return [row for row in rows if int(row.get("target_count") or 0) >= 2]


def enrich_wallet_entities(
    store: SQLiteStore,
    max_wallets: int = 1_500,
    chunk_size: int = 500,
    client: DuneClient | None = None,
) -> dict[str, Any]:
    wallets = research_candidate_wallets(store, max_wallets=max_wallets)
    result = _execute_wallet_chunks(
        store=store,
        wallets=wallets,
        client=client,
        source="dune_base_wallet_entities",
        sql_dir=DEFAULT_ENTITY_SQL_DIR,
        result_dir=DEFAULT_ENTITY_RESULT_DIR,
        chunk_size=chunk_size,
        render_sql=render_base_wallet_entities_sql,
        import_rows=lambda rows: store.import_wallet_entities("base", rows),
    )
    requested = {row["wallet_address"].lower() for row in wallets}
    checked = {
        row["wallet_address"].lower()
        for row in store.wallet_entity_rows("base")
        if row["wallet_address"].lower() in requested
    }
    store.upsert_wallet_identity_coverage(
        "base",
        checked,
        coverage_type="entity_classification",
        status="complete",
        source="dune_base_wallet_entities",
        evidence={"fail_closed": True},
    )
    return {
        **result,
        "entity_summary": store.wallet_entity_summary("base"),
    }


def enrich_wallet_funding(
    store: SQLiteStore,
    max_wallets: int = 1_500,
    chunk_size: int = 400,
    client: DuneClient | None = None,
) -> dict[str, Any]:
    wallets = [
        row
        for row in research_candidate_wallets(store, max_wallets=max_wallets)
        if row.get("earliest_buy_at")
    ]
    result = _execute_wallet_chunks(
        store=store,
        wallets=wallets,
        client=client,
        source="dune_base_wallet_funding",
        sql_dir=DEFAULT_FUNDING_SQL_DIR,
        result_dir=DEFAULT_FUNDING_RESULT_DIR,
        chunk_size=chunk_size,
        render_sql=render_base_wallet_funding_sql,
        import_rows=lambda rows: store.import_wallet_funding("base", rows),
    )
    requested = {row["wallet_address"].lower() for row in wallets}
    checked = {
        row["wallet_address"].lower()
        for row in store.wallet_funding_rows("base")
        if row["wallet_address"].lower() in requested
    }
    store.upsert_wallet_identity_coverage(
        "base",
        checked,
        coverage_type="funding_hop_1",
        status="complete",
        source="dune_base_wallet_funding",
        evidence={"hop_count": 1, "not_sufficient_for_independence": True},
    )
    return result


def run_wallet_intelligence(
    store: SQLiteStore,
    max_wallets: int = 1_500,
    entity_chunk_size: int = 500,
    funding_chunk_size: int = 400,
    client: DuneClient | None = None,
) -> dict[str, Any]:
    entities = enrich_wallet_entities(
        store=store,
        max_wallets=max_wallets,
        chunk_size=entity_chunk_size,
        client=client,
    )
    funding = enrich_wallet_funding(
        store=store,
        max_wallets=max_wallets,
        chunk_size=funding_chunk_size,
        client=client,
    )
    clusters = rebuild_wallet_clusters(store=store, max_wallets=max_wallets)
    return {
        "entities": entities,
        "funding": funding,
        "clusters": clusters,
    }


def _execute_wallet_chunks(
    store: SQLiteStore,
    wallets: list[dict[str, Any]],
    client: DuneClient | None,
    source: str,
    sql_dir: Path,
    result_dir: Path,
    chunk_size: int,
    render_sql: Callable[[list[dict[str, Any]]], str],
    import_rows: Callable[[list[dict[str, Any]]], int],
) -> dict[str, Any]:
    if not wallets:
        return {
            "wallet_count": 0,
            "chunk_count": 0,
            "imported_row_count": 0,
            "execution_ids": [],
        }

    dune = client or DuneClient()
    sql_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    execution_ids = []
    imported = 0
    chunks = list(chunked(wallets, max(1, chunk_size)))

    for index, wallet_chunk in enumerate(chunks, start=1):
        sql_path = sql_dir / f"{source}_{index:03d}.generated.sql"
        result_path = result_dir / f"{source}_{index:03d}.json"
        sql_path.write_text(render_sql(wallet_chunk), encoding="utf-8")
        execution = dune.execute_sql(sql_path.read_text(encoding="utf-8"), performance="medium")
        execution_id = execution["execution_id"]
        execution_ids.append(execution_id)
        store.record_dune_execution(
            execution_id=execution_id,
            source=source,
            state=execution.get("state", "submitted"),
            sql_file=str(sql_path),
            output_file=str(result_path),
        )
        try:
            payload = dune.poll_results(execution_id, timeout_seconds=600, poll_interval_seconds=30)
            write_json(result_path, payload)
            rows = payload.get("result", {}).get("rows", [])
            store.finish_dune_execution(
                execution_id=execution_id,
                state=payload.get("state", "QUERY_STATE_COMPLETED"),
                row_count=len(rows),
                output_file=str(result_path),
            )
            imported += import_rows(rows)
        except DuneAPIError as exc:
            store.finish_dune_execution(
                execution_id=execution_id,
                state="failed",
                error=str(exc),
                output_file=str(result_path),
            )
            raise

    return {
        "wallet_count": len(wallets),
        "chunk_count": len(chunks),
        "imported_row_count": imported,
        "execution_ids": execution_ids,
    }


def rebuild_wallet_clusters(
    store: SQLiteStore,
    max_wallets: int = 1_500,
) -> dict[str, Any]:
    wallets = research_wallet_selection(store, "base", max_wallets=max_wallets)
    by_address = {row["wallet_address"].lower(): row for row in wallets}
    addresses = set(by_address)
    funding_rows = [
        row for row in store.wallet_funding_rows("base") if row["wallet_address"] in addresses
    ]
    identity_edges = store.wallet_identity_edge_rows("base")
    required_coverage = {
        "entity_classification",
        "funding_hops_3",
        "shared_routes",
        "owner_resolution",
    }
    coverage_by_address: dict[str, set[str]] = defaultdict(set)
    for row in store.wallet_identity_coverage_rows("base"):
        if row.get("status") == "complete":
            coverage_by_address[str(row["wallet_address"]).lower()].add(
                str(row["coverage_type"])
            )
    graph = UnionFind(addresses)
    edge_evidence: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    def add_edge(
        first: str,
        second: str,
        method: str,
        confidence: float,
        evidence: dict[str, Any],
    ) -> None:
        if first == second:
            return
        pair = tuple(sorted((first, second)))
        graph.union(*pair)
        edge_evidence[pair].append(
            {"method": method, "confidence": confidence, **evidence}
        )

    for row in identity_edges:
        first = str(row["wallet_address_a"]).lower()
        second = str(row["wallet_address_b"]).lower()
        confidence = float(row.get("confidence") or 0)
        if first not in addresses or second not in addresses or confidence < 0.6:
            continue
        add_edge(
            first,
            second,
            str(row["edge_type"]),
            confidence,
            {
                "hop_count": int(row.get("hop_count") or 0),
                "source": row.get("source"),
                "identity_evidence": row.get("evidence", {}),
            },
        )

    funding_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in funding_rows:
        funder = str(row.get("funder_address") or "").lower()
        if funder and not row.get("is_shared_service"):
            funding_groups[funder].append(row)
    for funder, members in funding_groups.items():
        if not 2 <= len(members) <= 12:
            continue
        for first, second in combinations(sorted(row["wallet_address"] for row in members), 2):
            sample = members[0]
            add_edge(
                first,
                second,
                "shared_first_funder",
                0.75,
                {
                    "funder_address": funder,
                    "funder_label": sample.get("funder_label"),
                    "funder_category": sample.get("funder_category"),
                },
            )

    pair_targets: dict[tuple[str, str], set[tuple[str, str, str]]] = defaultdict(set)
    for key, members in synchronous_target_groups(wallets).items():
        if not 2 <= len(members) <= 6:
            continue
        target_key = key[:3]
        for first, second in combinations(sorted(members), 2):
            pair_targets[(first, second)].add(target_key)
    for (first, second), targets in pair_targets.items():
        if len(targets) < 2:
            continue
        add_edge(
            first,
            second,
            "repeated_synchronous_entry",
            0.65,
            {
                "target_count": len(targets),
                "targets": [target[0] for target in sorted(targets)],
                "bucket_minutes": 15,
            },
        )

    clusters = []
    components = graph.components()
    for members in sorted(components.values(), key=lambda group: (len(group), group), reverse=True):
        members = sorted(members)
        member_set = set(members)
        links = [
            item
            for pair, evidence_rows in edge_evidence.items()
            if set(pair).issubset(member_set)
            for item in evidence_rows
        ]
        coverage_complete = all(
            required_coverage.issubset(coverage_by_address.get(member, set()))
            for member in members
        )
        independence_status = "supported" if coverage_complete else "unknown"
        methods = sorted({item["method"] for item in links})
        if not methods:
            methods = [
                "independence_supported"
                if coverage_complete
                else "identity_unknown"
            ]
        rationale = cluster_rationale(members, links)
        if coverage_complete:
            rationale.append(
                "Полное identity coverage не нашло внешних связей с другими исследуемыми кластерами."
            )
        else:
            rationale.append(
                "Независимость не подтверждена: отсутствие найденной связи не считается доказательством отдельного владельца."
            )
        cluster_id = "base-" + hashlib.sha256("|".join(members).encode("utf-8")).hexdigest()[:16]
        member_rows = []
        for wallet_address in members:
            member_links = [
                item
                for pair, evidence_rows in edge_evidence.items()
                if wallet_address in pair
                for item in evidence_rows
            ]
            member_rows.append(
                {
                    "wallet_address": wallet_address,
                    "role": (
                        "independence_supported"
                        if len(members) == 1 and coverage_complete
                        else "identity_unknown"
                        if len(members) == 1
                        else "linked_member"
                    ),
                    "link_confidence": max(
                        [float(item["confidence"]) for item in member_links] or [0.0]
                    ),
                    "evidence": {
                        "methods": sorted({item["method"] for item in member_links}),
                        "peer_count": len(members) - 1,
                        "identity_coverage": sorted(
                            coverage_by_address.get(wallet_address, set())
                        ),
                        "independence_status": independence_status,
                    },
                }
            )
        clusters.append(
            {
                "cluster_id": cluster_id,
                "members": member_rows,
                "independence_score": (
                    round(1 / math.sqrt(len(members)), 4)
                    if coverage_complete
                    else 0.0
                ),
                "methods": methods,
                "rationale": rationale,
                "evidence": {
                    "member_count": len(members),
                    "link_count": len(links),
                    "independence_status": independence_status,
                    "coverage_complete": coverage_complete,
                    "required_coverage_types": sorted(required_coverage),
                    "coverage_by_wallet": {
                        member: sorted(coverage_by_address.get(member, set()))
                        for member in members
                    },
                    "rules": [
                        "Общий funder учитывается только без CEX/bridge/service метки и в группе до 12 адресов.",
                        "Синхронный вход требует совпадения в 15-минутном окне минимум по двум разным meaningful targets.",
                        "Identity Graph V2 добавляет двухшаговый funding, team/deployer связи, повторную синхронность и стандартных owner-ов контрактных аккаунтов.",
                        "Повтор редких execution routes учитывается только минимум по трём маршрутам и не более чем у 12 исследуемых адресов.",
                    ],
                },
            }
        )
    store.replace_wallet_clusters(clusters, model_version=MODEL_VERSION, chain_id="base")
    linked = sum(1 for cluster in clusters if len(cluster["members"]) > 1)
    return {
        "wallet_count": len(wallets),
        "cluster_count": len(clusters),
        "linked_cluster_count": linked,
        "linked_wallet_count": sum(
            len(cluster["members"]) for cluster in clusters if len(cluster["members"]) > 1
        ),
    }


def synchronous_target_groups(
    wallets: list[dict[str, Any]],
) -> dict[tuple[str, str, str, int], set[str]]:
    groups: dict[tuple[str, str, str, int], set[str]] = defaultdict(set)
    for wallet in wallets:
        address = wallet["wallet_address"].lower()
        for target in wallet.get("evidence", {}).get("targets", []):
            if not target.get("is_meaningful") or not target.get("first_buy_at"):
                continue
            try:
                timestamp = parse_utc(target["first_buy_at"]).timestamp()
            except (TypeError, ValueError):
                continue
            bucket = int(timestamp // (15 * 60))
            groups[
                (
                    str(target["symbol"]),
                    str(target["token_address"]).lower(),
                    str(target["announced_at"]),
                    bucket,
                )
            ].add(address)
    return groups


def cluster_rationale(members: list[str], links: list[dict[str, Any]]) -> list[str]:
    if len(members) == 1:
        return [
            "Связей по текущим правилам не найдено, но адрес остаётся identity-unknown до полного coverage.",
            "Отсутствие найденной связи не является доказательством уникального владельца.",
        ]
    messages = []
    funders = sorted(
        {str(item["funder_address"]) for item in links if item["method"] == "shared_first_funder"}
    )
    if funders:
        messages.append(
            "Есть общий первый non-service funder: "
            + ", ".join(funders[:3])
            + ". Это признак возможной связности, но не доказательство общего владельца."
        )
    sync_counts = [
        int(item.get("target_count") or 0)
        for item in links
        if item["method"] == "repeated_synchronous_entry"
    ]
    if sync_counts:
        messages.append(
            "Есть повторяющиеся синхронные meaningful-входы в 15-минутных окнах "
            f"минимум по {max(sync_counts)} разным Binance targets."
        )
    identity_methods = sorted(
        {
            item["method"]
            for item in links
            if item["method"]
            not in {"shared_first_funder", "repeated_synchronous_entry"}
        }
    )
    if identity_methods:
        readable = {
            "shared_funder_hop_1": "общий funding source на первом hop",
            "shared_funder_hop_2": "общий funding source на втором hop",
            "shared_funder_hop_3": "общий funding source на третьем hop",
            "shared_team_funder_hop_1": "общий team/deployer funder",
            "shared_team_funder_hop_2": "общий team/deployer funder через второй hop",
            "shared_team_funder_hop_3": "общий team/deployer funder через третий hop",
            "direct_candidate_funding": "прямое финансирование между кандидатами",
            "standard_account_owner": "общий стандартный owner контрактного аккаунта",
            "repeated_synchronous_flow": "повторный синхронный поток по разным токенам",
            "repeated_rare_execution_routes": "повтор минимум трёх редких execution routes",
        }
        messages.append(
            "Identity Graph: "
            + "; ".join(readable.get(method, method) for method in identity_methods)
            + "."
        )
    messages.append(
        "Кластер снижает независимый вес группы в сигнале; он не устанавливает юридическую или фактическую принадлежность адресов одному лицу."
    )
    return messages


def parse_utc(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def chunked(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[start : start + size] for start in range(0, len(rows), size)]


class UnionFind:
    def __init__(self, values: set[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, first: str, second: str) -> None:
        root_first = self.find(first)
        root_second = self.find(second)
        if root_first != root_second:
            self.parent[root_second] = root_first

    def components(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = defaultdict(list)
        for value in self.parent:
            result[self.find(value)].append(value)
        return result
