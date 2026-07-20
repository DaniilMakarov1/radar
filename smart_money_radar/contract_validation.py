from __future__ import annotations

from typing import Any

from smart_money_radar.ingestion.evm import (
    EvmRpcClient,
    EvmRpcError,
    normalize_token_symbol,
)
from smart_money_radar.storage import SQLiteStore


def validate_evm_contract_mappings(
    store: SQLiteStore,
    chain_id: str,
    client: EvmRpcClient | None = None,
) -> dict[str, Any]:
    normalized_chain = chain_id.lower()
    if normalized_chain not in {"base", "bsc", "ethereum"}:
        raise ValueError(f"Unsupported EVM validation chain: {chain_id}")
    rpc = client or EvmRpcClient(chain_id=normalized_chain)
    rows = store.contract_mapping_rows(normalized_chain)
    results = []
    for row in rows:
        expected = normalize_token_symbol(row["symbol"])
        try:
            observed_symbol = rpc.erc20_symbol(row["contract_address"])
            observed = normalize_token_symbol(observed_symbol)
            verified = bool(expected and observed and expected == observed)
            status = "rpc_symbol_verified" if verified else "rpc_symbol_mismatch"
            error = None
        except EvmRpcError as exc:
            observed_symbol = None
            verified = False
            status = "rpc_validation_failed"
            error = str(exc)
        store.update_contract_mapping_validation(
            token_contract_id=row["token_contract_id"],
            mapping_status=status,
            confidence_score=0.98 if verified else 0.1,
        )
        results.append(
            {
                **row,
                "observed_symbol": observed_symbol,
                "verified": verified,
                "validation_status": status,
                "error": error,
            }
        )
    return {
        "chain_id": normalized_chain,
        "checked": len(results),
        "verified": sum(row["verified"] for row in results),
        "mismatched": sum(
            row["validation_status"] == "rpc_symbol_mismatch" for row in results
        ),
        "failed": sum(
            row["validation_status"] == "rpc_validation_failed" for row in results
        ),
        "results": results,
    }


def validate_base_contract_mappings(
    store: SQLiteStore,
    client: EvmRpcClient | None = None,
) -> dict[str, Any]:
    return validate_evm_contract_mappings(store, "base", client=client)
