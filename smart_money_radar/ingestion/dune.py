from __future__ import annotations

import json
import hashlib
import http.client
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from smart_money_radar.config import DUNE_API_BASE_URL, load_env_file


class DuneAPIError(RuntimeError):
    pass


class DuneClient:
    def __init__(self, api_key: str | None = None, timeout_seconds: int = 60):
        load_env_file()
        self.api_key = api_key or os.environ.get("DUNE_API_KEY")
        self.timeout_seconds = timeout_seconds
        if not self.api_key:
            raise DuneAPIError("DUNE_API_KEY is not set")

    def execute_sql(self, sql: str, performance: str = "medium") -> dict[str, Any]:
        return self._request_json(
            method="POST",
            path="/sql/execute",
            payload={"sql": sql, "performance": performance},
        )

    def execution_status(self, execution_id: str) -> dict[str, Any]:
        return self._request_json(
            method="GET",
            path=f"/execution/{execution_id}/status",
        )

    def usage(self) -> dict[str, Any]:
        return self._request_json(
            method="POST",
            path="/usage",
            payload={},
        )

    def remaining_credits(self) -> float | None:
        periods = self.usage().get("billing_periods") or []
        if not periods:
            return None
        period = periods[-1]
        return max(
            0.0,
            float(period.get("credits_included") or 0)
            - float(period.get("credits_used") or 0),
        )

    def require_credit_reserve(self, minimum_credits: float = 25.0) -> None:
        remaining = self.remaining_credits()
        if remaining is not None and remaining < minimum_credits:
            raise DuneAPIError(
                "Dune credit reserve is too low to start a new execution: "
                f"remaining={remaining:.3f}, required={minimum_credits:.3f}"
            )

    def execution_results(
        self,
        execution_id: str,
        limit: int = 1000,
        offset: int = 0,
        filters: str | None = None,
        columns: list[str] | None = None,
        sort_by: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": limit, "offset": offset}
        if filters:
            query["filters"] = filters
        if columns:
            query["columns"] = ",".join(columns)
        if sort_by:
            query["sort_by"] = sort_by
        return self._request_json(
            method="GET",
            path=(
                f"/execution/{execution_id}/results?"
                + urllib.parse.urlencode(query)
            ),
        )

    def execution_results_all(
        self,
        execution_id: str,
        limit: int = 1000,
        filters: str | None = None,
        columns: list[str] | None = None,
        sort_by: str | None = None,
    ) -> dict[str, Any]:
        first_page = self.execution_results(
            execution_id,
            limit=limit,
            offset=0,
            filters=filters,
            columns=columns,
            sort_by=sort_by,
        )
        rows = list(first_page.get("result", {}).get("rows", []))
        metadata = first_page.get("result", {}).get("metadata", {})
        total_count = int(metadata.get("total_row_count") or metadata.get("row_count") or len(rows))
        next_offset = first_page.get("next_offset")

        while next_offset is not None and len(rows) < total_count:
            page = self.execution_results(
                execution_id,
                limit=limit,
                offset=int(next_offset),
                filters=filters,
                columns=columns,
                sort_by=sort_by,
            )
            rows.extend(page.get("result", {}).get("rows", []))
            next_offset = page.get("next_offset")

        combined = dict(first_page)
        combined.setdefault("result", {})
        combined["result"] = dict(combined["result"])
        combined["result"]["rows"] = rows
        combined["result"]["metadata"] = dict(metadata)
        combined["result"]["metadata"]["row_count"] = len(rows)
        combined["result"]["metadata"]["fetched_row_count"] = len(rows)
        return combined

    def execution_results_all_cached(
        self,
        execution_id: str,
        cache_root: Path,
        limit: int = 1000,
        filters: str | None = None,
        columns: list[str] | None = None,
        sort_by: str | None = None,
        max_workers: int = 4,
    ) -> dict[str, Any]:
        signature = hashlib.sha256(
            json.dumps(
                {
                    "execution_id": execution_id,
                    "limit": limit,
                    "filters": filters,
                    "columns": columns,
                    "sort_by": sort_by,
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        cache_dir = cache_root / execution_id / signature
        cache_dir.mkdir(parents=True, exist_ok=True)
        def fetch_page(offset: int) -> dict[str, Any]:
            page_path = cache_dir / f"page-{offset:012d}.json"
            page = read_cached_json(page_path)
            if page is None:
                page = self.execution_results(
                    execution_id,
                    limit=limit,
                    offset=offset,
                    filters=filters,
                    columns=columns,
                    sort_by=sort_by,
                )
                write_json(page_path, page)
            return page

        first_page = fetch_page(0)
        first_rows = list(first_page.get("result", {}).get("rows", []))
        metadata = dict(first_page.get("result", {}).get("metadata") or {})
        reported_total_count = int(
            metadata.get("total_row_count")
            or metadata.get("row_count")
            or len(first_rows)
        )
        pages = [first_page]
        if filters:
            seen_offsets = {0}
            next_offset = first_page.get("next_offset")
            while next_offset is not None:
                offset = int(next_offset)
                if offset in seen_offsets:
                    raise DuneAPIError(
                        f"Dune pagination repeated offset={offset}"
                    )
                seen_offsets.add(offset)
                page = fetch_page(offset)
                pages.append(page)
                next_offset = page.get("next_offset")
        else:
            next_offset = first_page.get("next_offset")
            if next_offset is not None:
                page_stride = int(next_offset)
                if page_stride <= 0:
                    raise DuneAPIError(
                        f"Dune returned an invalid next_offset={next_offset}"
                    )
                remaining_offsets = list(
                    range(page_stride, reported_total_count, page_stride)
                )
                with ThreadPoolExecutor(
                    max_workers=max(1, min(max_workers, len(remaining_offsets)))
                ) as executor:
                    pages.extend(executor.map(fetch_page, remaining_offsets))

        rows = [
            row
            for page in pages
            for row in page.get("result", {}).get("rows", [])
        ]

        combined = dict(first_page)
        result = dict(combined.get("result") or {})
        if not filters and len(rows) != reported_total_count:
            raise DuneAPIError(
                "Dune pagination does not match the advertised result count: "
                f"fetched={len(rows)}, expected={reported_total_count}"
            )
        result["rows"] = rows
        metadata["row_count"] = len(rows)
        metadata["fetched_row_count"] = len(rows)
        if filters:
            metadata["unfiltered_total_row_count"] = reported_total_count
        metadata["page_cache_dir"] = str(cache_dir)
        result["metadata"] = metadata
        combined["result"] = result
        return combined

    def poll_results(
        self,
        execution_id: str,
        timeout_seconds: int = 600,
        poll_interval_seconds: int = 5,
        fetch_all_pages: bool = True,
    ) -> dict[str, Any]:
        self.wait_for_execution(
            execution_id,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        if fetch_all_pages:
            return self.execution_results_all(execution_id)
        return self.execution_results(execution_id)

    def wait_for_execution(
        self,
        execution_id: str,
        timeout_seconds: int = 600,
        poll_interval_seconds: int = 5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last_status = self.execution_status(execution_id)
            state = last_status.get("state")
            if state == "QUERY_STATE_COMPLETED":
                return last_status
            if state in {"QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED"}:
                raise DuneAPIError(f"Dune execution ended with state={state}: {last_status}")
            time.sleep(poll_interval_seconds)
        raise DuneAPIError(f"Timed out waiting for Dune execution: {last_status}")

    def export_cost_plan(
        self,
        execution_status: dict[str, Any],
        columns: list[str] | None = None,
    ) -> dict[str, Any]:
        metadata = execution_status.get("result_metadata") or {}
        row_count = int(metadata.get("total_row_count") or metadata.get("row_count") or 0)
        available_columns = list(metadata.get("column_names") or [])
        selected_column_count = len(columns or available_columns)
        available_column_count = max(1, len(available_columns))
        total_bytes = int(
            metadata.get("total_result_set_bytes")
            or metadata.get("result_set_bytes")
            or 0
        )
        projected_bytes = int(
            total_bytes * selected_column_count / available_column_count
        )
        projected_datapoints = row_count * selected_column_count
        # Free-tier export is the conservative bound; higher plans are cheaper.
        credits_by_bytes = projected_bytes / 1_000_000 * 20.0
        credits_by_datapoints = projected_datapoints / 5_000
        estimated_credits = max(credits_by_bytes, credits_by_datapoints)
        return {
            "row_count": row_count,
            "selected_column_count": selected_column_count,
            "available_column_count": available_column_count,
            "projected_bytes": projected_bytes,
            "projected_datapoints": projected_datapoints,
            "estimated_export_credits": estimated_credits,
        }

    def require_affordable_export(
        self,
        execution_status: dict[str, Any],
        columns: list[str] | None = None,
        reserve_fraction: float = 0.1,
    ) -> dict[str, Any]:
        plan = self.export_cost_plan(execution_status, columns=columns)
        remaining = self.remaining_credits()
        plan["remaining_credits"] = remaining
        required = float(plan["estimated_export_credits"]) * 1.1
        if remaining is not None:
            spendable = remaining * max(0.0, 1.0 - reserve_fraction)
            if required > spendable:
                raise DuneAPIError(
                    "Dune export blocked before data transfer: "
                    f"estimated={required:.3f} credits with safety margin, "
                    f"spendable={spendable:.3f}. Reduce rows/columns or raise the limit."
                )
        return plan

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        max_attempts = 2 if "/results" in path else 5
        for attempt in range(max_attempts):
            request = urllib.request.Request(
                DUNE_API_BASE_URL + path,
                data=body,
                method=method,
                headers={
                    "Content-Type": "application/json",
                    "X-Dune-Api-Key": self.api_key or "",
                    "User-Agent": "SmartMoneyRadar/0.1",
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt < max_attempts - 1:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 30.0
                    except ValueError:
                        delay = 30.0
                    time.sleep(max(5.0, delay))
                    continue
                raise DuneAPIError(f"Dune API HTTP {exc.code}: {detail}") from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                ConnectionResetError,
                http.client.IncompleteRead,
                http.client.RemoteDisconnected,
                json.JSONDecodeError,
            ) as exc:
                if attempt < max_attempts - 1:
                    time.sleep(2**attempt)
                    continue
                raise DuneAPIError(
                    f"Dune API request failed after {attempt + 1} attempts: {exc}"
                ) from exc
        raise DuneAPIError("Dune API retries exhausted")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_cached_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        path.unlink(missing_ok=True)
        return None
    return payload if isinstance(payload, dict) else None
