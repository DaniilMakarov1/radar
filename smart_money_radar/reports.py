from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from smart_money_radar.config import (
    DEFAULT_REPORT_HTML_PATH,
    DEFAULT_REPORT_PATH,
    api_key_status,
)
from smart_money_radar.scoring.wallets import MODEL_VERSION
from smart_money_radar.storage import SQLiteStore, utc_now_iso


def build_project_report(store: SQLiteStore) -> str:
    summary = store.dashboard_summary()
    registry = store.registry_summary()
    base_targets = store.chain_backtest_targets("base")
    buyer_summary = store.pre_listing_buy_summary(limit=10)
    holding_summary = store.holding_behavior_summary(limit=10)
    wallet_summary = store.wallet_score_summary(model_version=MODEL_VERSION, limit=10)
    repeatability_summary = store.wallet_repeatability_summary(
        model_version=MODEL_VERSION,
        limit=10,
    )
    api_status = api_key_status()
    recent_events = store.registry_events(limit=12)
    runs = store.dashboard_ingestion_runs(limit=8)

    lines = [
        "# Smart Money Radar Report",
        "",
        f"Generated at: `{utc_now_iso()}`",
        "",
        "## Current State",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Binance announcements | {summary['announcement_count']} |",
        f"| Registry tokens | {registry['tokens']} |",
        f"| Listing events | {registry['listing_events']} |",
        f"| Spot backtest targets | {registry['backtest_targets']} |",
        f"| Token contracts | {registry['token_contracts']} |",
        f"| Base-ready backtest targets | {len(base_targets)} |",
        f"| Pre-listing wallet buy rows | {buyer_summary['total_rows']} |",
        f"| Distinct pre-listing wallets | {buyer_summary['total_wallets']} |",
        f"| Wallet holding metric rows | {holding_summary['total_rows']} |",
        f"| Wallet scores ({MODEL_VERSION}) | {wallet_summary['total_wallets']} |",
        f"| Repeated wallets | {repeatability_summary['repeat_wallet_count']} |",
        f"| Live signals | {summary['signal_count']} |",
        "",
        "## Data Readiness",
        "",
        readiness_line(registry["backtest_targets"] > 0, "Binance spot targets exist"),
        readiness_line(len(base_targets) > 0, "At least one Base target has a contract"),
        readiness_line(api_status["DUNE_API_KEY"], "DUNE_API_KEY is configured"),
        readiness_line(buyer_summary["total_rows"] > 0, "Base DEX trade history imported"),
        readiness_line(
            holding_summary["total_rows"] > 0,
            "Sell-side / holding behavior imported",
        ),
        readiness_line(wallet_summary["total_wallets"] > 0, "Wallet diagnostics V1 computed"),
        readiness_line(
            repeatability_summary["repeat_wallet_count"] > 0,
            "Multi-target wallet repeatability observed",
        ),
        "",
        "## API Keys",
        "",
        "| Key | Status |",
        "|---|---|",
    ]

    for key, present in api_status.items():
        lines.append(f"| `{key}` | {'present' if present else 'missing'} |")

    lines.extend(
        [
            "",
            "## Base-Ready Targets",
            "",
            "| Symbol | Announced At | Contract | Mapping |",
            "|---|---|---|---|",
        ]
    )
    if base_targets:
        for target in base_targets:
            lines.append(
                "| "
                f"{target['symbol']} | "
                f"{target['announced_at']} | "
                f"`{target['contract_address']}` | "
                f"{target['mapping_status']} |"
            )
    else:
        lines.append("| none | - | - | - |")

    lines.extend(
        [
            "",
            "## Pre-Listing Buyer Summary",
            "",
            "| Wallet | Symbols | Trades | Gross Buy USD |",
            "|---|---:|---:|---:|",
        ]
    )
    if buyer_summary["top_wallets"]:
        for row in buyer_summary["top_wallets"]:
            lines.append(
                "| "
                f"`{row['wallet_address']}` | "
                f"{row['symbol_count']} | "
                f"{row['buy_trade_count']} | "
                f"{row['gross_buy_usd']:.2f} |"
            )
    else:
        lines.append("| none | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "## Holding Behavior Summary",
            "",
            "| Label | Count | Pre Buy USD | Pre Sell USD | Post Sell USD | Avg Pre Sell Ratio | Avg Post Sell Ratio |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    if holding_summary["by_label"]:
        for row in holding_summary["by_label"]:
            lines.append(
                "| "
                f"{row['holding_label']} | "
                f"{row['count']} | "
                f"{row['pre_buy_usd']:.2f} | "
                f"{row['pre_sell_usd']:.2f} | "
                f"{row['post_sell_usd']:.2f} | "
                f"{row['avg_pre_sell_ratio']:.3f} | "
                f"{row['avg_post_sell_ratio']:.3f} |"
            )
    else:
        lines.append("| none | 0 | 0 | 0 | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "### Top Accumulators",
            "",
            "| Wallet | Symbol | Label | Net Pre USD | Pre Sell Ratio | Post Sell Ratio |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    if holding_summary["top_holders"]:
        for row in holding_summary["top_holders"]:
            lines.append(
                "| "
                f"`{row['wallet_address']}` | "
                f"{row['symbol']} | "
                f"{row['holding_label']} | "
                f"{row['net_pre_usd']:.2f} | "
                f"{row['pre_sell_ratio']:.3f} | "
                f"{row['post_sell_ratio']:.3f} |"
            )
    else:
        lines.append("| none | - | - | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "### Top Sellers / Flippers",
            "",
            "| Wallet | Symbol | Label | Pre Buy USD | Pre Sell Ratio | Post Sell Ratio |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    if holding_summary["top_sellers"]:
        for row in holding_summary["top_sellers"]:
            lines.append(
                "| "
                f"`{row['wallet_address']}` | "
                f"{row['symbol']} | "
                f"{row['holding_label']} | "
                f"{row['pre_buy_usd']:.2f} | "
                f"{row['pre_sell_ratio']:.3f} | "
                f"{row['post_sell_ratio']:.3f} |"
            )
    else:
        lines.append("| none | - | - | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "## Repeatability",
            "",
            "| Target Count | Wallets |",
            "|---:|---:|",
        ]
    )
    if repeatability_summary["by_target_count"]:
        for row in repeatability_summary["by_target_count"]:
            lines.append(f"| {row['target_count']} | {row['wallet_count']} |")
    else:
        lines.append("| 0 | 0 |")

    lines.extend(
        [
            "",
            "### Repeated Wallets",
            "",
            "| Wallet | Label | Symbols | Interest | Noise | Confidence | Gross Buy USD | Flags |",
            "|---|---|---|---:|---:|---:|---:|---|",
        ]
    )
    if repeatability_summary["repeated_wallets"]:
        for row in repeatability_summary["repeated_wallets"]:
            lines.append(
                "| "
                f"`{row['wallet_address']}` | "
                f"{row['label']} | "
                f"{', '.join(row['evidence'].get('symbols', []))} | "
                f"{row['interest_score']:.1f} | "
                f"{row['noise_score']:.1f} | "
                f"{row['confidence_score']:.1f} | "
                f"{row['total_gross_buy_usd']:.2f} | "
                f"{', '.join(row['flags'][:5])} |"
            )
    else:
        lines.append("| none | - | - | 0 | 0 | 0 | 0 | - |")

    lines.extend(
        [
            "",
            "## Wallet Score Summary",
            "",
            "| Label | Count |",
            "|---|---:|",
        ]
    )
    if wallet_summary["by_label"]:
        for row in wallet_summary["by_label"]:
            lines.append(f"| {row['label']} | {row['count']} |")
    else:
        lines.append("| none | 0 |")

    lines.extend(
        [
            "",
            "### Top Wallet Candidates",
            "",
            "| Wallet | Label | Interest | Noise | Confidence | Gross Buy USD | Flags |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    if wallet_summary["top_candidates"]:
        for row in wallet_summary["top_candidates"]:
            lines.append(
                "| "
                f"`{row['wallet_address']}` | "
                f"{row['label']} | "
                f"{row['interest_score']:.1f} | "
                f"{row['noise_score']:.1f} | "
                f"{row['confidence_score']:.1f} | "
                f"{row['total_gross_buy_usd']:.2f} | "
                f"{', '.join(row['flags'][:4])} |"
            )
    else:
        lines.append("| none | - | 0 | 0 | 0 | 0 | - |")

    lines.extend(
        [
            "",
            "## Recent Registry Events",
            "",
            "| Time | Type | Symbol | Target | Mapping |",
            "|---|---|---|---|---|",
        ]
    )
    for event in recent_events:
        lines.append(
            "| "
            f"{event['announced_at']} | "
            f"{event['event_type']} | "
            f"{event['symbol']} | "
            f"{'yes' if event['is_backtest_target'] else 'no'} | "
            f"{event['mapping_status']} |"
        )

    lines.extend(
        [
            "",
            "## Recent Ingestion Runs",
            "",
            "| Run | Source | Status | Seen | Written | Finished |",
            "|---:|---|---|---:|---:|---|",
        ]
    )
    for run in runs:
        lines.append(
            "| "
            f"{run['run_id']} | "
            f"{run['source']} | "
            f"{run['status']} | "
            f"{run['records_seen']} | "
            f"{run['records_written']} | "
            f"{run['finished_at'] or '-'} |"
        )

    lines.extend(["", "## Next Practical Step", ""])
    if buyer_summary["total_rows"] > 0:
        if holding_summary["total_rows"] == 0:
            lines.extend(
                [
                    "1. Generate holding SQL with `render-dune-base-holding`.",
                    "2. Execute it through `dune-execute-sql` and import with `import-base-holding`.",
                    "3. Re-run `score-wallets` so V1 can separate accumulators from flippers.",
                ]
            )
        elif wallet_summary["total_wallets"] > 0:
            lines.extend(
                [
                    "1. Expand Binance backfill and Base-ready target count.",
                    "2. Re-run Dune extraction across more targets.",
                    "3. Upgrade from one-target diagnostics to repeatability scoring.",
                ]
            )
        else:
            lines.extend(
                [
                    "1. Run `score-wallets` to compute wallet diagnostics V1.",
                    "2. Inspect `wallet-report` for candidates and likely noise.",
                    "3. Expand Binance backfill and Base-ready target count.",
                ]
            )
    elif api_status["DUNE_API_KEY"] and base_targets:
        lines.extend(
            [
                "1. Generate Base buyers SQL with `render-dune-base-buyers`.",
                "2. Execute the generated SQL with `dune-execute-sql`.",
                "3. Import result rows into the wallet-scoring dataset.",
            ]
        )
    else:
        lines.extend(
            [
                "1. Add `DUNE_API_KEY` to `.env`.",
                "2. Generate Base buyers SQL with `render-dune-base-buyers`.",
                "3. Execute the generated SQL with `dune-execute-sql`.",
            ]
        )
    return "\n".join(lines) + "\n"


def readiness_line(ok: bool, text: str) -> str:
    mark = "OK" if ok else "TODO"
    return f"- `{mark}` {text}"


def write_report(
    store: SQLiteStore,
    markdown_path: Path = DEFAULT_REPORT_PATH,
    html_path: Path = DEFAULT_REPORT_HTML_PATH,
) -> tuple[Path, Path]:
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    report = build_project_report(store)
    markdown_path.write_text(report, encoding="utf-8")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(markdown_to_simple_html(report), encoding="utf-8")
    return markdown_path, html_path


def markdown_to_simple_html(markdown: str) -> str:
    escaped = html.escape(markdown)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Smart Money Radar Report</title>
  <style>
    body {{
      margin: 0;
      background: #f6f7f3;
      color: #171b1c;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 28px;
    }}
    pre {{
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: #fff;
      border: 1px solid #d9dfd8;
      border-radius: 8px;
      padding: 20px;
      line-height: 1.55;
      box-shadow: 0 12px 30px rgba(27, 35, 31, 0.08);
    }}
  </style>
</head>
<body>
  <main><pre>{escaped}</pre></main>
</body>
</html>
"""
