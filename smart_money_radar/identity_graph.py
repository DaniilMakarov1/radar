from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import defaultdict
from itertools import combinations
from typing import Any

from smart_money_radar.config import PROJECT_ROOT
from smart_money_radar.dune_queries import (
    write_base_identity_graph_sql,
    write_base_shared_routes_sql,
)
from smart_money_radar.ingestion.dune import DuneAPIError
from smart_money_radar.research_pipeline import execute_dune_artifact
from smart_money_radar.storage import SQLiteStore
from smart_money_radar.wallet_research import research_wallet_selection
from smart_money_radar.wallet_intelligence import (
    rebuild_wallet_clusters,
)


IDENTITY_GRAPH_SOURCE = "identity_graph_v2"
BASE_PUBLIC_RPC = "https://mainnet.base.org"
SERVICE_CATEGORIES = {
    "bridge",
    "cex",
    "contract",
    "infrastructure",
    "market maker",
    "market_maker",
    "mev",
    "bundler",
    "paymaster",
}
TEAM_CATEGORIES = {"team", "founder", "deployer", "treasury"}


def run_identity_graph_backfill(
    store: SQLiteStore,
    max_wallets: int = 200,
    timeout_seconds: int = 1_800,
) -> dict[str, Any]:
    wallets = research_wallet_selection(store, "base", max_wallets=max_wallets)
    sql_path = (
        PROJECT_ROOT
        / "queries"
        / "generated"
        / "base_identity_graph_v2.generated.sql"
    )
    result_path = PROJECT_ROOT / "exports" / "base_identity_graph_v2.json"
    write_base_identity_graph_sql(sql_path, wallets)
    execution_id, payload = execute_dune_artifact(
        store=store,
        source="dune_base_identity_graph_v2",
        sql_path=sql_path,
        result_path=result_path,
        client=None,
        timeout_seconds=timeout_seconds,
        performance="medium",
    )
    rows = payload.get("result", {}).get("rows", [])
    route_rows: list[dict[str, Any]] = []
    route_execution_id = None
    warnings = []
    route_sql_path = (
        PROJECT_ROOT
        / "queries"
        / "generated"
        / "base_identity_routes_v2.generated.sql"
    )
    route_result_path = PROJECT_ROOT / "exports" / "base_identity_routes_v2.json"
    write_base_shared_routes_sql(route_sql_path, wallets)
    try:
        route_execution_id, route_payload = execute_dune_artifact(
            store=store,
            source="dune_base_identity_routes_v2",
            sql_path=route_sql_path,
            result_path=route_result_path,
            client=None,
            timeout_seconds=timeout_seconds,
            performance="medium",
        )
        route_rows = route_payload.get("result", {}).get("rows", [])
    except DuneAPIError as exc:
        warnings.append(f"shared routes unavailable: {exc}")
    edges = build_identity_edges(
        funding_rows=rows,
        weekly_flows=store.wallet_token_weekly_flow_rows("base"),
        candidate_addresses={row["wallet_address"].lower() for row in wallets},
        route_rows=route_rows,
    )
    owner_edges, owner_checked = resolve_standard_account_owner_edges(
        sorted({row["wallet_address"].lower() for row in wallets})
    )
    edges.extend(owner_edges)
    edge_count = store.replace_wallet_identity_edges(
        chain_id="base",
        source=IDENTITY_GRAPH_SOURCE,
        edges=edges,
    )
    wallet_addresses = {row["wallet_address"].lower() for row in wallets}
    funding_checked = {
        str(row.get("wallet_address") or "").lower()
        for row in rows
        if row.get("wallet_address")
    }
    store.upsert_wallet_identity_coverage(
        "base",
        funding_checked,
        coverage_type="funding_hops_3",
        status="complete",
        source=IDENTITY_GRAPH_SOURCE,
        evidence={"hop_count": 3, "execution_id": execution_id},
    )
    if route_execution_id:
        store.upsert_wallet_identity_coverage(
            "base",
            wallet_addresses,
            coverage_type="shared_routes",
            status="complete",
            source=IDENTITY_GRAPH_SOURCE,
            evidence={"route_execution_id": route_execution_id},
        )
    store.upsert_wallet_identity_coverage(
        "base",
        owner_checked,
        coverage_type="owner_resolution",
        status="complete",
        source=IDENTITY_GRAPH_SOURCE,
        evidence={"rpc_url": BASE_PUBLIC_RPC},
    )
    clusters = rebuild_wallet_clusters(store=store, max_wallets=max_wallets)
    return {
        "wallet_count": len(wallets),
        "funding_row_count": len(rows),
        "route_row_count": len(route_rows),
        "identity_edge_count": edge_count,
        "account_owner_edge_count": len(owner_edges),
        "execution_id": execution_id,
        "route_execution_id": route_execution_id,
        "warnings": warnings,
        "clusters": clusters,
    }


def build_identity_edges(
    funding_rows: list[dict[str, Any]],
    weekly_flows: list[dict[str, Any]],
    candidate_addresses: set[str],
    route_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for hop_key, hop_count, confidence in (
        ("hop1_address", 1, 0.8),
        ("hop2_address", 2, 0.65),
        ("hop3_address", 3, 0.6),
    ):
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in funding_rows:
            funder = str(row.get(hop_key) or "").lower()
            category = str(row.get(hop_key.replace("address", "category")) or "").lower()
            cex = row.get(hop_key.replace("address", "cex"))
            if not funder or cex or category in SERVICE_CATEGORIES:
                continue
            grouped[funder].append(row)
        for funder, members in grouped.items():
            addresses = sorted(
                {
                    str(row["wallet_address"]).lower()
                    for row in members
                    if str(row["wallet_address"]).lower() in candidate_addresses
                }
            )
            if not 2 <= len(addresses) <= 20:
                continue
            sample = members[0]
            category = str(
                sample.get(hop_key.replace("address", "category")) or ""
            ).lower()
            edge_type = (
                f"shared_team_funder_hop_{hop_count}"
                if category in TEAM_CATEGORIES
                else f"shared_funder_hop_{hop_count}"
            )
            edge_confidence = 0.9 if category in TEAM_CATEGORIES else confidence
            for first, second in combinations(addresses, 2):
                edges.append(
                    {
                        "wallet_address_a": first,
                        "wallet_address_b": second,
                        "edge_type": edge_type,
                        "hop_count": hop_count,
                        "confidence": edge_confidence,
                        "first_observed_at": sample.get(
                            hop_key.replace("address", "funded_at")
                        ),
                        "last_observed_at": sample.get(
                            hop_key.replace("address", "funded_at")
                        ),
                        "evidence": {
                            "shared_funder": funder,
                            "funder_label": sample.get(
                                hop_key.replace("address", "label")
                            ),
                            "funder_category": category or None,
                            "member_count": len(addresses),
                        },
                    }
                )

    for row in funding_rows:
        wallet = str(row.get("wallet_address") or "").lower()
        hop1 = str(row.get("hop1_address") or "").lower()
        hop2 = str(row.get("hop2_address") or "").lower()
        hop3 = str(row.get("hop3_address") or "").lower()
        for funder, hop in ((hop1, 1), (hop2, 2), (hop3, 3)):
            if wallet in candidate_addresses and funder in candidate_addresses:
                edges.append(
                    {
                        "wallet_address_a": wallet,
                        "wallet_address_b": funder,
                        "edge_type": "direct_candidate_funding",
                        "hop_count": hop,
                        "confidence": 0.9 if hop == 1 else 0.7 if hop == 2 else 0.6,
                        "evidence": {"funding_direction": f"{funder}->{wallet}"},
                    }
                )

    pair_events: dict[tuple[str, str], set[str]] = defaultdict(set)
    synchronous_groups: dict[tuple[str, int], set[str]] = defaultdict(set)
    event_times: dict[tuple[str, int], list[str]] = defaultdict(list)
    for row in weekly_flows:
        if float(row.get("net_buy_usd") or 0) <= 0 or not row.get("first_trade_at"):
            continue
        wallet = str(row["wallet_address"]).lower()
        if wallet not in candidate_addresses:
            continue
        timestamp = parse_timestamp(row["first_trade_at"])
        bucket = int(timestamp // (15 * 60))
        token = str(row["token_address"]).lower()
        synchronous_groups[(token, bucket)].add(wallet)
        event_times[(token, bucket)].append(str(row["first_trade_at"]))
    for (token, bucket), members in synchronous_groups.items():
        if not 2 <= len(members) <= 12:
            continue
        for first, second in combinations(sorted(members), 2):
            pair_events[(first, second)].add(token)
    for (first, second), tokens in pair_events.items():
        if len(tokens) < 2:
            continue
        edges.append(
            {
                "wallet_address_a": first,
                "wallet_address_b": second,
                "edge_type": "repeated_synchronous_flow",
                "hop_count": 0,
                "confidence": min(0.9, 0.55 + len(tokens) * 0.08),
                "evidence": {
                    "token_count": len(tokens),
                    "tokens": sorted(tokens)[:20],
                    "bucket_minutes": 15,
                },
            }
        )
    for row in route_rows or []:
        first = str(row.get("wallet_address_a") or "").lower()
        second = str(row.get("wallet_address_b") or "").lower()
        shared_routes = int(row.get("shared_route_count") or 0)
        shared_trades = int(row.get("shared_route_trades") or 0)
        if (
            first not in candidate_addresses
            or second not in candidate_addresses
            or shared_routes < 3
            or shared_trades < 6
        ):
            continue
        edges.append(
            {
                "wallet_address_a": first,
                "wallet_address_b": second,
                "edge_type": "repeated_rare_execution_routes",
                "hop_count": 0,
                "confidence": min(0.82, 0.5 + shared_routes * 0.06),
                "first_observed_at": row.get("first_observed_at"),
                "last_observed_at": row.get("last_observed_at"),
                "evidence": {
                    "shared_route_count": shared_routes,
                    "shared_route_trades": shared_trades,
                    "route_examples": str(row.get("route_examples") or "").split(",")[:10],
                    "rare_route_wallet_cap": 12,
                },
            }
        )
    return deduplicate_edges(edges)


def resolve_standard_account_owner_edges(
    addresses: list[str],
    rpc_url: str = BASE_PUBLIC_RPC,
    max_contracts: int = 50,
) -> tuple[list[dict[str, Any]], set[str]]:
    client = JsonRpcClient(rpc_url)
    edges = []
    checked_addresses: set[str] = set()
    checked_contracts = 0
    for address in addresses[: max_contracts * 5]:
        if checked_contracts >= max_contracts:
            break
        try:
            code = client.call("eth_getCode", [address, "latest"])
        except IdentityRpcError:
            break
        checked_addresses.add(address)
        if not isinstance(code, str) or code in {"0x", "0x0"}:
            continue
        checked_contracts += 1
        owners = []
        for selector, decoder in (
            ("0x8da5cb5b", decode_single_address),
            ("0xa0e67e2b", decode_address_array),
        ):
            try:
                result = client.call(
                    "eth_call",
                    [{"to": address, "data": selector}, "latest"],
                )
            except IdentityRpcError:
                continue
            owners.extend(decoder(result))
        for owner in sorted(set(owners)):
            if owner == address:
                continue
            edges.append(
                {
                    "wallet_address_a": address,
                    "wallet_address_b": owner,
                    "edge_type": "standard_account_owner",
                    "hop_count": 1,
                    "confidence": 0.85,
                    "evidence": {
                        "owner_resolved_by": "owner_or_getOwners_eth_call",
                        "contract_wallet": address,
                    },
                }
            )
    return edges, checked_addresses


class IdentityRpcError(RuntimeError):
    pass


class JsonRpcClient:
    def __init__(self, rpc_url: str, timeout_seconds: int = 15) -> None:
        self.rpc_url = rpc_url
        self.timeout_seconds = timeout_seconds
        self.request_id = 0

    def call(self, method: str, params: list[Any]) -> Any:
        self.request_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": params,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.rpc_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "SmartMoneyRadar/0.3",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise IdentityRpcError(str(exc)) from exc
        if payload.get("error"):
            raise IdentityRpcError(str(payload["error"]))
        return payload.get("result")


def decode_single_address(value: Any) -> list[str]:
    text = str(value or "")
    if not text.startswith("0x") or len(text) < 66:
        return []
    address = "0x" + text[-40:]
    return [] if int(address, 16) == 0 else [address.lower()]


def decode_address_array(value: Any) -> list[str]:
    text = str(value or "")
    if not text.startswith("0x"):
        return []
    data = text[2:]
    if len(data) < 128:
        return []
    try:
        offset = int(data[:64], 16) * 2
        length = int(data[offset : offset + 64], 16)
    except (ValueError, IndexError):
        return []
    owners = []
    start = offset + 64
    for index in range(min(length, 100)):
        word = data[start + index * 64 : start + (index + 1) * 64]
        if len(word) != 64:
            break
        address = "0x" + word[-40:]
        if int(address, 16):
            owners.append(address.lower())
    return owners


def deduplicate_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        first, second = sorted(
            (edge["wallet_address_a"].lower(), edge["wallet_address_b"].lower())
        )
        key = (first, second, edge["edge_type"])
        normalized = {**edge, "wallet_address_a": first, "wallet_address_b": second}
        current = unique.get(key)
        if current is None or float(normalized["confidence"]) > float(current["confidence"]):
            unique[key] = normalized
    return list(unique.values())


def parse_timestamp(value: Any) -> float:
    text = str(value).replace("Z", "+00:00")
    from datetime import UTC, datetime

    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()
