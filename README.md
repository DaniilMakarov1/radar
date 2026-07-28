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
- Default paper strategy: `synchronized_funding_capture_v2`.
- Scanner output is not final entry authorization: synchronized routes are
  `watch` until focused observations pass underwriting.
- New synchronized paper state is stored in `funding_capture_positions`,
  `funding_capture_cycles`, `funding_capture_observations`,
  `funding_paper_orders`, `funding_settlement_reconciliations`, and the paper
  event ledger.
- The default profile scans all active registered venues automatically; venues in `DEACTIVATED_FUNDING_VENUES` stay excluded.
- Strategy-incompatible active venues remain diagnostics/research-only until they pass the capability contract.
- Funding candidates are based on the next synchronized settlement economics.
- Spread convergence is not counted as expected profit for the default strategy; executable spread/basis is treated as cost and risk.
- Initial entry never requires 7/30/90-day raw funding history. Long-term history,
  historical persistence, old median PnL, and historical win rate cannot block the
  first synchronized entry.
- Funding estimates are never cashflow. Paper PnL includes funding only after
  public rate plus settlement mark reconciliation.
- 4h/8h projections are informational, not the entry decision gate.
- Focused route checks run in parallel for hot routes.
- Initial entry targets T-30 seconds and is allowed only when both legs are 25-35 seconds from the same settlement.
- Both next settlements must align within 1 second.
- Entry snapshots must be fresh: each route snapshot may be at most 2 seconds old.
- Entry requires two simulated fills before T-20; one filled leg can never become
  an `OPEN` synchronized position.
- The launch defaults do not use the old 15-second final freeze/fallback entry window.
- After settlement, the bot probes the next schedule around T+5 and evaluates hold/close around T+30.
- Aligned next settlements can be held if the next cycle passes underwriting; mismatched schedules close.
- Hold decisions use exact next timestamps and incremental economics; they do
  not wait for funding reconciliation and do not charge opening fees again.
- Hold history is a secondary haircut only. It uses local fully reconciled prior
  hold cycles for the same directed route, collateral, asset, and wait bucket; it
  never uses raw long-term exchange funding history for entry.
- Open v2 positions and critical hot routes have priority over discovery. A
  background full scan must not delay open-position polling, entry rechecks,
  emergency close, or reconciliation.
- A position can capture at most 4 settlements and live about 4 hours 5 minutes.
- A common 10% price move is telemetry and a fresh-risk warning, not an automatic stop-loss.
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
