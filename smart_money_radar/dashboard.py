from __future__ import annotations

import csv
import io
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from smart_money_radar.analytics import (
    AnalyticsError,
    prune_analytics_executions,
    run_saved_query,
    run_sql,
)
from smart_money_radar.backtest import run_wallet_walk_forward
from smart_money_radar.config import (
    DEFAULT_DB_PATH,
    api_key_status,
    x_social_gate_enabled,
)
from smart_money_radar.decision import build_capital_readiness
from smart_money_radar.dashboard_dune import (
    DUNE_ANALYTICS_EXECUTION_RETENTION,
    build_dune_overview,
    dashboard_int,
    dune_local_scan_config,
    run_dune_local_scan_job,
)
from smart_money_radar.funding.models import FundingScanConfig
from smart_money_radar.funding.presentation import (
    filter_deactivated_funding_paper_export_rows,
    filter_deactivated_funding_paper_payload,
)
from smart_money_radar.funding.service import (
    run_funding_scan,
)
from smart_money_radar.live_radar import recompute_live_signals, run_base_live_scan
from smart_money_radar.prediction.service import (
    PredictionScanConfig,
    run_prediction_scan,
)
from smart_money_radar.ingestion.dune import DuneAPIError, DuneClient
from smart_money_radar.scoring.wallets import MODEL_VERSION, score_wallet_rows
from smart_money_radar.storage import SQLiteStore, utc_now_iso
from smart_money_radar.wallet_intelligence import rebuild_wallet_clusters


DEFAULT_DASHBOARD_HOST = "127.0.0.1"
DEFAULT_DASHBOARD_PORT = 8787
FUNDING_AUTO_REFRESH_SECONDS = 20
FUNDING_AUTO_REFRESH_ENABLED = False
DASHBOARD_HTML_PATH = Path(__file__).resolve().parent / "web" / "index.html"


def run_dashboard(
    db_path: Path = DEFAULT_DB_PATH,
    host: str = DEFAULT_DASHBOARD_HOST,
    port: int = DEFAULT_DASHBOARD_PORT,
) -> None:
    store = SQLiteStore(db_path)
    store.init_db()
    recover_interrupted_dashboard_work(store)
    handler = build_handler(store)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Smart Money Radar: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard")
    finally:
        server.server_close()


def recover_interrupted_dashboard_work(
    store: SQLiteStore,
    event_writer=print,
) -> None:
    error = "Dashboard restarted before the background job completed"
    failed_jobs = store.fail_running_app_jobs(error)
    if failed_jobs:
        event_writer(
            "Recovered interrupted dashboard work: "
            f"{failed_jobs} app jobs"
        )


def build_handler(store: SQLiteStore) -> type[BaseHTTPRequestHandler]:
    job_lock = threading.Lock()
    active_job: dict[str, int | None] = {"job_id": None}
    funding_state: dict[str, object] = {
        "running": False,
        "request_id": 0,
        "mode": None,
        "started_at": None,
        "last_status": None,
        "last_error": None,
        "last_finished_at": None,
        "latest_scan_id": None,
    }

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == "/":
                self.send_html(DASHBOARD_HTML_PATH.read_text(encoding="utf-8"))
                return
            if path == "/api/overview":
                self.send_json(build_app_overview(store))
                return
            if path == "/api/wallets":
                self.send_json(
                    store.wallet_score_rows(
                        model_version=MODEL_VERSION,
                        limit=query_limit(query, 300, 2000),
                    )
                )
                return
            if path == "/api/clusters":
                self.send_json(
                    store.dashboard_clusters(
                        model_version=MODEL_VERSION,
                        limit=query_limit(query, 100, 200),
                    )
                )
                return
            if path == "/api/backtest":
                self.send_json(
                    {
                        "wallet": store.latest_backtest("wallet_walk_forward") or {},
                        "negative": store.latest_backtest(
                            "token_candidate_negative_control"
                        )
                        or {},
                        "negative_candidates": store.latest_negative_control_candidates(
                            limit=100
                        ),
                    }
                )
                return
            if path == "/api/coverage":
                self.send_json(store.dashboard_coverage())
                return
            if path == "/api/radar":
                self.send_json(
                    store.latest_radar_observations(
                        limit=query_limit(query, 100, 500)
                    )
                )
                return
            if path == "/api/signals":
                self.send_json(
                    store.dashboard_signals(limit=query_limit(query, 100, 500))
                )
                return
            if path == "/api/jobs/latest":
                self.send_json(store.latest_app_job() or {})
                return
            if path.startswith("/api/jobs/"):
                raw_job_id = path.rsplit("/", 1)[-1]
                try:
                    job_id = int(raw_job_id)
                except ValueError:
                    self.send_json(
                        {"status": "invalid_request", "error": "Invalid job id"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                job = store.app_job(job_id)
                if not job:
                    self.send_json(
                        {"status": "not_found", "error": "Job not found"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                self.send_json(job)
                return
            if path == "/api/summary":
                self.send_json(store.dashboard_summary())
                return
            if path == "/api/research":
                self.send_json(build_research_overview(store))
                return
            if path == "/api/prediction":
                self.send_json(store.prediction_dashboard(route_limit=100))
                return
            if path == "/api/funding":
                horizon_mode, horizon_hours = funding_query_horizon(query)
                payload = store.funding_dashboard(
                    horizon_mode=horizon_mode,
                    horizon_hours=horizon_hours,
                    include_watch_scans=True,
                )
                payload["selected_horizon"] = {
                    "horizon_mode": horizon_mode,
                    "horizon_hours": horizon_hours,
                }
                with job_lock:
                    payload["refresh_state"] = dict(funding_state)
                payload["refresh_policy"] = {
                    "auto_refresh_enabled": FUNDING_AUTO_REFRESH_ENABLED,
                    "auto_refresh_seconds": FUNDING_AUTO_REFRESH_SECONDS,
                    "non_overlapping": True,
                    "scan_snapshot_retention": 1,
                    "funding_history_retention_per_market": 24,
                }
                self.send_json(payload)
                return
            if path == "/api/funding/status":
                with job_lock:
                    payload = dict(funding_state)
                self.send_json(payload)
                return
            if path == "/api/funding-paper/export.csv":
                self.send_csv(
                    "funding-paper-trades.csv",
                    filter_deactivated_funding_paper_export_rows(
                        store.funding_paper_trade_report_rows(
                            refresh_estimates=False
                        )
                    ),
                )
                return
            if path == "/api/funding-paper":
                self.send_json(filter_deactivated_funding_paper_payload(
                    store.funding_paper_dashboard(refresh_estimates=False)
                ))
                return
            if path == "/api/dune":
                try:
                    self.send_json(build_dune_overview(store))
                except Exception as exc:
                    self.send_json(
                        {"status": "failed", "error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return
            if path == "/health":
                self.send_json({"status": "ok", "time": utc_now_iso()})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/actions/recompute":
                try:
                    result = recompute_research(store)
                except Exception as exc:
                    self.send_json(
                        {"status": "failed", "error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                self.send_json({"status": "success", **result})
                return

            if path == "/api/actions/live-scan":
                with job_lock:
                    if active_job["job_id"] is not None or funding_state["running"]:
                        self.send_json(
                            {
                                "status": "already_running",
                                "job_id": active_job["job_id"],
                            },
                            status=HTTPStatus.CONFLICT,
                        )
                        return
                    job_id = store.create_app_job(
                        "base_live_scan",
                        "Preparing qualified wallet scan",
                    )
                    active_job["job_id"] = job_id
                    thread = threading.Thread(
                        target=run_live_scan_job,
                        args=(store, job_id, active_job, job_lock),
                        daemon=True,
                    )
                    thread.start()
                self.send_json(
                    {"status": "queued", "job_id": job_id},
                    status=HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/actions/prediction-scan":
                with job_lock:
                    if active_job["job_id"] is not None or funding_state["running"]:
                        self.send_json(
                            {
                                "status": "already_running",
                                "job_id": active_job["job_id"],
                            },
                            status=HTTPStatus.CONFLICT,
                        )
                        return
                    job_id = store.create_app_job(
                        "prediction_scan",
                        "Preparing public prediction-market collectors",
                    )
                    active_job["job_id"] = job_id
                    thread = threading.Thread(
                        target=run_prediction_scan_job,
                        args=(store, job_id, active_job, job_lock),
                        daemon=True,
                    )
                    thread.start()
                self.send_json(
                    {"status": "queued", "job_id": job_id},
                    status=HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/dune/saved-query":
                try:
                    payload = self.read_json_body()
                    slug = str(payload.get("slug") or "").strip()
                    if not slug:
                        raise ValueError("slug is required")
                    result = run_saved_query(
                        store,
                        slug,
                        limit=dashboard_int(payload, "limit", 100, 1, 5_000),
                    )
                    pruned = prune_analytics_executions(
                        store,
                        keep_latest=DUNE_ANALYTICS_EXECUTION_RETENTION,
                    )
                    self.send_json(
                        {
                            "status": "success",
                            "result": result,
                            "pruned_execution_count": pruned,
                        }
                    )
                except (AnalyticsError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    prune_analytics_executions(
                        store,
                        keep_latest=DUNE_ANALYTICS_EXECUTION_RETENTION,
                    )
                    self.send_json(
                        {"status": "invalid_request", "error": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except Exception as exc:
                    self.send_json(
                        {"status": "failed", "error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if path == "/api/dune/query":
                try:
                    payload = self.read_json_body()
                    sql = str(payload.get("sql") or "")
                    result = run_sql(
                        store,
                        sql,
                        limit=dashboard_int(payload, "limit", 100, 1, 5_000),
                    )
                    pruned = prune_analytics_executions(
                        store,
                        keep_latest=DUNE_ANALYTICS_EXECUTION_RETENTION,
                    )
                    self.send_json(
                        {
                            "status": "success",
                            "result": result,
                            "pruned_execution_count": pruned,
                        }
                    )
                except (AnalyticsError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    prune_analytics_executions(
                        store,
                        keep_latest=DUNE_ANALYTICS_EXECUTION_RETENTION,
                    )
                    self.send_json(
                        {"status": "invalid_request", "error": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except Exception as exc:
                    self.send_json(
                        {"status": "failed", "error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return

            if path == "/api/actions/funding-scan":
                try:
                    payload = self.read_json_body()
                    scan_mode = str(payload.get("scan_mode") or "manual")
                    if scan_mode not in {"manual", "auto"}:
                        raise ValueError("scan_mode must be manual or auto")
                    if scan_mode == "auto" and not FUNDING_AUTO_REFRESH_ENABLED:
                        with job_lock:
                            request_id = int(funding_state.get("request_id") or 0)
                        self.send_json(
                            {
                                "status": "auto_refresh_disabled",
                                "funding_request_id": request_id,
                                "scan_mode": scan_mode,
                            }
                        )
                        return
                    config = funding_request_config(store, payload, scan_mode)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self.send_json(
                        {"status": "invalid_request", "error": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                with job_lock:
                    if active_job["job_id"] is not None or funding_state["running"]:
                        self.send_json(
                            {
                                "status": "already_running",
                                "job_id": active_job["job_id"],
                                "funding_request_id": funding_state["request_id"],
                                "funding_running": bool(funding_state["running"]),
                            },
                            status=(
                                HTTPStatus.OK
                                if scan_mode == "auto"
                                else HTTPStatus.CONFLICT
                            ),
                        )
                        return
                    funding_state["request_id"] = int(funding_state["request_id"]) + 1
                    funding_state["running"] = True
                    funding_state["mode"] = scan_mode
                    funding_state["started_at"] = utc_now_iso()
                    funding_state["last_error"] = None
                    request_id = int(funding_state["request_id"])
                    if scan_mode == "auto":
                        thread = threading.Thread(
                            target=run_funding_auto_scan_job,
                            args=(
                                store,
                                funding_state,
                                job_lock,
                                config,
                                request_id,
                            ),
                            daemon=True,
                        )
                        thread.start()
                        self.send_json(
                            {
                                "status": "queued",
                                "funding_request_id": request_id,
                                "scan_mode": scan_mode,
                            },
                            status=HTTPStatus.ACCEPTED,
                        )
                        return
                    job_id = store.create_app_job(
                        "funding_scan",
                        "Preparing multi-venue funding scan",
                    )
                    active_job["job_id"] = job_id
                    thread = threading.Thread(
                        target=run_funding_scan_job,
                        args=(
                            store,
                            job_id,
                            active_job,
                            funding_state,
                            job_lock,
                            config,
                            request_id,
                        ),
                        daemon=True,
                    )
                    thread.start()
                self.send_json(
                    {
                        "status": "queued",
                        "job_id": job_id,
                        "funding_request_id": request_id,
                        "scan_mode": scan_mode,
                    },
                    status=HTTPStatus.ACCEPTED,
                )
                return

            if path == "/api/actions/dune-local-scan":
                try:
                    payload = self.read_json_body()
                    config = dune_local_scan_config(payload)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    self.send_json(
                        {"status": "invalid_request", "error": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                with job_lock:
                    if active_job["job_id"] is not None or funding_state["running"]:
                        self.send_json(
                            {
                                "status": "already_running",
                                "job_id": active_job["job_id"],
                                "funding_running": bool(funding_state["running"]),
                            },
                            status=HTTPStatus.CONFLICT,
                        )
                        return
                    job_id = store.create_app_job(
                        "dune_local_base_scan",
                        "Preparing local Base analytics ingestion",
                    )
                    active_job["job_id"] = job_id
                    thread = threading.Thread(
                        target=run_dune_local_scan_job,
                        args=(store, job_id, active_job, job_lock, config),
                        daemon=True,
                    )
                    thread.start()
                self.send_json(
                    {"status": "queued", "job_id": job_id},
                    status=HTTPStatus.ACCEPTED,
                )
                return

            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def send_html(self, html_body: str) -> None:
            body = html_body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.write_response_body(body)

        def send_json(
            self,
            payload: object,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.write_response_body(body)

        def send_csv(
            self,
            filename: str,
            rows: list[dict[str, object]],
        ) -> None:
            fieldnames = list(rows[0].keys()) if rows else funding_paper_csv_fields()
            buffer = io.StringIO(newline="")
            writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            body = ("\ufeff" + buffer.getvalue()).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"',
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.write_response_body(body)

        def write_response_body(self, body: bytes) -> None:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def read_json_body(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > 250_000:
                raise ValueError("Request body is too large")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def log_message(self, format: str, *args: object) -> None:
            return

    return DashboardHandler


def funding_paper_csv_fields() -> list[str]:
    return [
        "ID",
        "Статус",
        "Открыто",
        "Закрыто",
        "Актив",
        "Маршрут",
        "Long",
        "Short",
        "Объем $",
        "Long notional $",
        "Short notional $",
        "Base qty",
        "Expected net $",
        "Funding PnL $",
        "Costs $",
        "Net PnL $",
        "Причина закрытия",
        "Состояние окна при закрытии",
        "Качество PnL",
    ]




def funding_request_config(
    store: SQLiteStore,
    payload: dict[str, object],
    scan_mode: str,
) -> FundingScanConfig:
    if scan_mode == "auto":
        saved = store.latest_funding_scan_config()
        source: dict[str, object] = saved or {
            "target_notional": 500.0,
            "horizon_mode": "next_settlement",
            "horizon_hours": None,
        }
    else:
        source = payload
    horizon_hours = source.get("horizon_hours")
    raw_near_miss_routes = source.get("near_miss_full_depth_routes")
    if raw_near_miss_routes is None:
        raw_near_miss_routes = 0
    raw_max_full_depth_orderbook_markets = (
        None
        if scan_mode == "auto"
        else source.get("max_full_depth_orderbook_markets")
    )
    return FundingScanConfig(
        target_notional=float(source.get("target_notional", 500.0)),
        horizon_mode=str(source.get("horizon_mode") or "next_settlement"),
        horizon_hours=(
            float(horizon_hours) if horizon_hours is not None else None
        ),
        near_miss_full_depth_routes=(
            int(raw_near_miss_routes)
            if raw_near_miss_routes is not None
            else None
        ),
        history_refresh_hours=float(source.get("history_refresh_hours", 48.0)),
        max_live_history_markets=int(source.get("max_live_history_markets", 24)),
        max_full_depth_orderbook_markets=(
            int(raw_max_full_depth_orderbook_markets)
            if raw_max_full_depth_orderbook_markets is not None
            else None
        ),
        orderbook_cache_ttl_seconds=0,
        market_snapshot_cache_ttl_seconds=0,
        adaptive_near_miss_min_score=float(
            source.get("adaptive_near_miss_min_score", 55.0)
        ),
        adaptive_near_miss_min_cost_coverage=float(
            source.get("adaptive_near_miss_min_cost_coverage", 0.35)
        ),
        adaptive_near_miss_emergency_floor_bps=float(
            source.get("adaptive_near_miss_emergency_floor_bps", 2.0)
        ),
        adaptive_near_miss_emergency_cost_multiplier=float(
            source.get("adaptive_near_miss_emergency_cost_multiplier", 0.75)
        ),
        adaptive_near_miss_near_break_even_coverage=float(
            source.get("adaptive_near_miss_near_break_even_coverage", 0.65)
        ),
        adaptive_near_miss_liquidity_score=float(
            source.get("adaptive_near_miss_liquidity_score", 0.70)
        ),
        adaptive_near_miss_urgency_score=float(
            source.get("adaptive_near_miss_urgency_score", 0.35)
        ),
    ).validated()


def funding_query_horizon(
    query: dict[str, list[str]],
) -> tuple[str, float | None]:
    raw_mode = (query.get("horizon_mode") or ["next_settlement"])[0]
    mode = str(raw_mode or "next_settlement").strip().lower()
    if mode in {"next", "next_settlement"}:
        return "next_settlement", None
    if mode != "fixed":
        mode = "fixed"
    raw_hours = (query.get("horizon_hours") or ["24"])[0]
    try:
        hours = float(raw_hours)
    except (TypeError, ValueError):
        hours = 24.0
    if hours not in {4.0, 8.0, 24.0}:
        hours = 24.0
    return mode, hours


def build_app_overview(store: SQLiteStore) -> dict[str, object]:
    summary = store.dashboard_summary()
    registry = store.registry_summary()
    wallet_summary = store.wallet_score_summary(
        model_version=MODEL_VERSION,
        limit=10,
    )
    latest_backtest = store.latest_backtest("wallet_walk_forward")
    latest_negative_backtest = store.latest_backtest(
        "token_candidate_negative_control"
    )
    latest_job = store.latest_app_job()
    api_status = api_key_status()
    research = store.research_dashboard()
    capital_readiness = build_capital_readiness(
        research,
        identity=store.identity_coverage_summary(),
        shadow=store.signal_shadow_summary(),
    )
    return {
        **summary,
        "model_version": MODEL_VERSION,
        "spot_backtest_target_count": registry["backtest_targets"],
        "token_contract_count": registry["token_contracts"],
        "wallet_labels": wallet_summary["by_label"],
        "wallet_entities": store.wallet_entity_summary("base"),
        "latest_backtest": latest_backtest,
        "latest_negative_backtest": latest_negative_backtest,
        "latest_job": latest_job,
        "dune_ready": api_status["DUNE_API_KEY"],
        "moralis_ready": api_status["MORALIS_API_KEY"],
        "hypersync_ready": api_status["HYPERSYNC_API_TOKEN"],
        "blockscout_mode": "public_base_instance",
        "x_social_gate_enabled": x_social_gate_enabled(),
        "capital_readiness": capital_readiness,
    }


def build_research_overview(store: SQLiteStore) -> dict[str, object]:
    result: dict[str, object] = store.research_dashboard()
    result["dune_executions"] = store.dashboard_dune_executions(limit=5)
    result["identity"] = store.identity_coverage_summary()
    result["shadow"] = store.signal_shadow_summary()
    result["capital_readiness"] = build_capital_readiness(
        result,
        identity=result["identity"],
        shadow=result["shadow"],
    )
    try:
        usage = DuneClient(timeout_seconds=8).usage()
        periods = usage.get("billing_periods") or []
        current = periods[-1] if periods else {}
        credits_used = float(current.get("credits_used") or 0)
        credits_included = float(current.get("credits_included") or 0)
        credits_remaining = max(0.0, credits_included - credits_used)
        result["dune_usage"] = {
            "available": True,
            "credits_used": credits_used,
            "credits_included": credits_included,
            "credits_remaining": credits_remaining,
            "included_credits_exhausted": bool(
                credits_included > 0 and credits_used >= credits_included
            ),
            "execution_blocked": credits_remaining < 25,
            "minimum_execution_reserve": 25,
            "period_start": current.get("start_date"),
            "period_end": current.get("end_date"),
        }
    except DuneAPIError as exc:
        result["dune_usage"] = {
            "available": False,
            "error": str(exc),
        }
    return result


def recompute_research(store: SQLiteStore) -> dict[str, object]:
    rows = store.pre_listing_wallet_buy_rows()
    holding_rows = store.wallet_holding_metric_rows()
    scores = score_wallet_rows(
        rows,
        holding_rows=holding_rows,
        dataset_target_count=store.observed_target_count(),
    )
    wallet_count = store.upsert_wallet_scores(scores)
    cluster_summary = rebuild_wallet_clusters(store)
    backtest = run_wallet_walk_forward(store)
    signal_count = 0
    if store.latest_radar_observations(limit=1):
        signal_count = len(recompute_live_signals(store))
    return {
        "wallet_count": wallet_count,
        "backtest_run_id": backtest["backtest_run_id"],
        "signal_count": signal_count,
        "cluster_summary": cluster_summary,
    }


def run_live_scan_job(
    store: SQLiteStore,
    job_id: int,
    active_job: dict[str, int | None],
    job_lock: threading.Lock,
) -> None:
    try:
        store.update_app_job(
            job_id,
            status="running",
            progress=0.1,
            message="Scanning recent Base DEX activity",
        )
        result = run_base_live_scan(store)
        store.update_app_job(
            job_id,
            status="success",
            progress=1.0,
            message="Live Radar scan completed",
            result=result,
        )
    except Exception as exc:
        store.update_app_job(
            job_id,
            status="failed",
            progress=1.0,
            message="Live Radar scan failed",
            error=str(exc),
        )
    finally:
        with job_lock:
            active_job["job_id"] = None


def run_prediction_scan_job(
    store: SQLiteStore,
    job_id: int,
    active_job: dict[str, int | None],
    job_lock: threading.Lock,
) -> None:
    try:
        store.update_app_job(
            job_id,
            status="running",
            progress=0.12,
            message="Collecting Polymarket and Kalshi orderbooks",
        )
        result = run_prediction_scan(
            store,
            PredictionScanConfig(collect_wallets=False),
        )
        store.update_app_job(
            job_id,
            status="success",
            progress=1.0,
            message="Prediction Radar scan completed",
            result=result,
        )
    except Exception as exc:
        store.update_app_job(
            job_id,
            status="failed",
            progress=1.0,
            message="Prediction Radar scan failed",
            error=str(exc),
        )
    finally:
        with job_lock:
            active_job["job_id"] = None


def run_funding_scan_job(
    store: SQLiteStore,
    job_id: int,
    active_job: dict[str, int | None],
    funding_state: dict[str, object],
    job_lock: threading.Lock,
    config: FundingScanConfig,
    request_id: int,
) -> None:
    result: dict[str, object] | None = None
    error: str | None = None
    try:
        store.update_app_job(
            job_id,
            status="running",
            progress=0.12,
            message="Collecting cross-venue funding and orderbook data",
        )
        result = run_funding_scan(
            store,
            config=config,
            scan_mode="manual",
            hydrate_missing_history=False,
        )
        store.update_app_job(
            job_id,
            status="success",
            progress=1.0,
            message="Funding Radar scan completed",
            result=result,
        )
    except Exception as exc:
        error = str(exc)
        store.update_app_job(
            job_id,
            status="failed",
            progress=1.0,
            message="Funding Radar scan failed",
            error=str(exc),
        )
    finally:
        with job_lock:
            active_job["job_id"] = None
            finish_funding_state(
                funding_state,
                request_id,
                result,
                error,
            )


def run_funding_auto_scan_job(
    store: SQLiteStore,
    funding_state: dict[str, object],
    job_lock: threading.Lock,
    config: FundingScanConfig,
    request_id: int,
) -> None:
    result: dict[str, object] | None = None
    error: str | None = None
    try:
        result = run_funding_scan(store, config=config, scan_mode="auto")
    except Exception as exc:
        error = str(exc)
    finally:
        with job_lock:
            finish_funding_state(
                funding_state,
                request_id,
                result,
                error,
            )


def finish_funding_state(
    funding_state: dict[str, object],
    request_id: int,
    result: dict[str, object] | None,
    error: str | None,
) -> None:
    if int(funding_state.get("request_id") or 0) != request_id:
        return
    funding_state["running"] = False
    funding_state["last_status"] = "failed" if error else "success"
    funding_state["last_error"] = error
    funding_state["last_finished_at"] = utc_now_iso()
    funding_state["latest_scan_id"] = (
        result.get("funding_scan_id") if result else None
    )


def query_limit(
    query: dict[str, list[str]],
    default: int,
    maximum: int,
) -> int:
    value = query.get("limit", [str(default)])[0]
    try:
        limit = int(value)
    except ValueError:
        return default
    return max(1, min(limit, maximum))
