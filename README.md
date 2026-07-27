# Smart Money Radar

Current focus: **Funding Radar**.

The project is a deterministic local system for scanning perpetual funding arbitrage, running a paper trader, monitoring funding/spread risk, and showing only decision-relevant results in a local dashboard.

Older Prediction, Dune/local analytics, wallet research, and on-chain Radar modules were removed or disabled so the project can concentrate on one working edge first.

## Active Modules

- `smart_money_radar/funding/` — venue adapters, route construction, funding economics, liquidity, forecasts.
- `smart_money_radar/paper_bot/` — paper entry, hold, settlement, close, spread monitoring, Telegram messages.
- `smart_money_radar/dashboard.py` and `smart_money_radar/web/index.html` — local Funding dashboard.
- `smart_money_radar/cli.py` — commands for scan, paper bot, reports, export, dashboard.
- `scripts/` — local launch helpers for dashboard and paper bot.

## Agent Workflow

Codex is the orchestrator/architect/reviewer. Qwen/QN is the full-access implementation worker for scoped coding tasks. See `docs/agent_operating_model.md` and the detailed repository contract in `docs/MODEL_INSTRUCTIONS.md`.

## Core Funding Contract

Every venue adapter must normalize funding fields consistently:

- `funding_rate`: rate for the venue's own settlement interval.
- `funding_interval_hours`: actual settlement interval.
- `hourly_funding_rate`: normalized rate for cross-venue comparison.
- `next_funding_at`: expected next settlement.
- `funding_rate_kind`: audit label describing the source.

If a source uses unclear units, fail closed. Do not let suspiciously large rates become candidates.

## Quick Start

```bash
python3 -m smart_money_radar.cli init-db
python3 -m smart_money_radar.cli dashboard
```

Open:

```text
http://127.0.0.1:8787
```

Run a manual funding scan:

```bash
python3 -m smart_money_radar.cli funding-scan --horizon-mode next_settlement
```

Run the paper bot:

```bash
python3 -m smart_money_radar.cli funding-paper-trader
```

Export paper trades:

```bash
python3 -m smart_money_radar.cli funding-paper-export
```

## Paper Bot Profiles

Profiles let us reuse one bot engine for different venue/strategy modes:

```bash
python3 -m smart_money_radar.cli funding-paper-trader --profile default
python3 -m smart_money_radar.cli funding-paper-trader --profile core_cex
```

`risex_points` exists in code as a planned profile shell for future points-farming, but it is intentionally unavailable in the CLI until a verified RiseX adapter exists.

## Runtime Rules

- Paper-only. No live execution.
- Funding candidates are based on next settlement economics.
- 4h/8h projections are informational, not the entry decision gate.
- Focused route checks run in parallel for hot routes.
- Final fresh recheck should happen no later than 15 seconds before settlement.
- If the final recheck fails, the bot may use the latest successful focused snapshot only if it is no older than 30 seconds.
- Negative-PnL routes are not shown as candidates.
- Risky venues disabled by user live in `smart_money_radar/funding/venues.py`.
- Variational is quarantined because its public funding units produced implausible PnL; it must be rebuilt and re-tested before reactivation.

## Testing

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

Focused funding suite:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_funding_radar.py tests/test_funding_paper_trader.py -q
```

## Data Retention

The system keeps paper trade history for analysis, but scan diagnostics are pruned aggressively. The active policy is to retain only the latest scan snapshots needed by the dashboard plus compact funding history for forecasts.

## Telegram

Telegram is configured through environment variables. Do not commit tokens or `.env` files.

```bash
python3 -m smart_money_radar.cli telegram-chat-id
python3 -m smart_money_radar.cli telegram-test
```
