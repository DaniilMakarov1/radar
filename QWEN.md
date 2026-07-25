# Smart Money Radar — Qwen Worker Instructions

## Role

Qwen is the implementation worker for this repository. Codex is the architect and reviewer. When Qwen is used from Codex, follow the task exactly and avoid broad redesign unless the prompt explicitly asks for it.

## Active Scope

The current active product is **Funding Radar**:

- multi-venue perpetual funding scanner
- funding/spread paper trader
- local dashboard on port 8787
- Telegram notifications for funding paper events
- CSV export for paper trade reports

Prediction, Dune/local analytics, wallet research, and old on-chain Radar modules are intentionally disabled or archived in the current Funding-first build. Do not reintroduce them unless explicitly requested.

## Commands

```bash
# Full tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q

# Focused funding tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_funding_radar.py tests/test_funding_paper_trader.py -q

# Dashboard
python3 -m smart_money_radar.cli dashboard

# Funding scan
python3 -m smart_money_radar.cli funding-scan --horizon-mode next_settlement

# Paper bot
python3 -m smart_money_radar.cli funding-paper-trader

# Paper report/export
python3 -m smart_money_radar.cli funding-paper-report
python3 -m smart_money_radar.cli funding-paper-export
```

## Hard Rules

- Do not commit secrets. Never write tokens, API keys, Telegram tokens, or `.env` contents into tracked files.
- Do not enable live trading. This repository is paper-only unless Daniil explicitly approves a separate live-execution phase.
- Do not add arbitrary route caps, top-N gates, or hard BPS filters without a risk reason and a test.
- Do not assume all venues report funding in the same units. `funding_rate` means per settlement interval; `hourly_funding_rate` is the normalized comparable value.
- Do not close a paper position merely because the first settlement happened. Keep it open while the arbitrage window remains valid; close when risk or opportunity logic says so.
- Do not show negative-PnL routes as candidates. They may be diagnostics, but not actionable candidates.
- Do not let deleted modules affect Funding UI startup. The Funding dashboard must load even if archived modules are absent.

## Funding Contract

Every market row must obey this contract:

- `funding_rate`: funding cashflow rate for the venue's own settlement interval.
- `funding_interval_hours`: the settlement interval in hours.
- `hourly_funding_rate`: `funding_rate / funding_interval_hours`, used only for cross-venue comparison and projections.
- `next_funding_at`: expected next settlement time.
- `funding_rate_kind`: source/meaning label for audit.

If a venue only publishes an hourly equivalent, set `funding_interval_hours` to the actual cashflow interval if known and keep both published fields for audit. If the source units are uncertain or implausible, fail closed with a risk flag rather than creating a candidate.

## Paper Bot Timing

- No routes: full scan after the previous scan completes, normally every 5 minutes.
- Watch route exists: focused recheck every 120 seconds.
- <= 3 minutes to settlement: focused recheck every 10 seconds.
- Pending/open position: focused recheck every 10 seconds.
- Final fresh API request should be sent no later than 15 seconds before settlement.
- If the final request fails, entry may use the latest successful focused snapshot only if it is <= 30 seconds old.

## Project Layout

- Funding logic: `smart_money_radar/funding/`
- Paper bot lifecycle: `smart_money_radar/paper_bot/`
- CLI: `smart_money_radar/cli.py`
- Dashboard API: `smart_money_radar/dashboard.py`
- Dashboard UI: `smart_money_radar/web/index.html`
- Storage: `smart_money_radar/storage.py`
- Runtime scripts: `scripts/`
- Tests: `tests/test_funding_radar.py`, `tests/test_funding_paper_trader.py`, `tests/test_funding_retention.py`

## Current Venue Notes

- Risky venues disabled by user live in `smart_money_radar/funding/venues.py`.
- `risex_points` is a planned profile shell only. Do not expose or run it until a verified RiseX adapter exists.
- DEX/perp venues with non-standard mechanics must be modeled conservatively. Pool-based venues need a separate pool perturbation model before they can be treated like normal orderbook venues.
- Variational-style API units must be checked carefully. If normalized rates exceed sanity caps, block the candidate and surface the reason.

## Workflow

1. Read the relevant files before editing.
2. Make small, scoped changes.
3. Add or update regression tests for every funding arithmetic or lifecycle change.
4. Run focused tests, then the full test suite.
5. Report exact files changed and tests run.

## Coordination With Codex

When acting as Codex's worker, prefer read-only audits unless Codex explicitly asks for edits. Return concise findings with file paths, line examples, risks, and suggested patch direction. Codex owns final architecture, risk acceptance, and commit boundaries.
