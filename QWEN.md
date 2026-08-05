# Smart Money Radar — Qwen Worker Instructions

## Role

Codex is the orchestrator, architect, and final reviewer for this repository. Qwen/QN is the scoped write-capable implementation worker: Codex decides what should be changed, Qwen/QN can edit code and run commands inside the assigned task, then Codex reviews the result before anything is accepted.

When Qwen is used from Codex, follow the task exactly and avoid broad redesign unless the prompt explicitly asks for it. Qwen should not change product direction, risk gates, venue eligibility, trading assumptions, or architecture boundaries on its own.

## Active Scope

The current active product is **Funding Radar**:

- multi-venue perpetual funding scanner
- funding/spread paper trader
- local dashboard on port 8787
- Telegram notifications for funding paper events
- CSV export for paper trade reports

Prediction, Dune/local analytics, wallet research, and old on-chain Radar modules are intentionally disabled or archived in the current Funding-first build. Do not reintroduce them unless explicitly requested.

## Required Reading

Before changing repository logic, read:

1. `docs/MODEL_INSTRUCTIONS.md` — detailed operating contract, entry/mandatory-exit/close rules, funding-unit invariants, and testing requirements.
2. `docs/AGENT_HANDOFF.md` — current handoff context and where large raw chat transcripts live.
3. The specific source file and matching tests for the task.

## Commands

```bash
# Full tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q

# Focused funding tests
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_funding_radar.py tests/test_funding_paper_trader.py tests/test_synchronized_funding_v2.py -q

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
- Do not keep a production paper position for another funding cycle after the first captured settlement. Exit at the first normal-safe opportunity after T+20, except hard-risk exits may happen earlier.
- Do not show negative-PnL routes as candidates. They may be diagnostics, but not actionable candidates.
- Do not let deleted modules affect Funding UI startup. The Funding dashboard must load even if archived modules are absent.

## History-Learned Worker Rules

- Implement only the scoped task. Do not expand product scope, add research modules, or promote a venue because it appears promising.
- Do not convert zero candidates into candidates by weakening gates. Add or preserve funnel diagnostics and fail closed.
- Before touching a venue adapter or funding formula, prove the rate unit, interval, next settlement timestamp, public/final source, mark source, symbol identity, and collateral assumptions.
- Do not treat estimates, spread convergence, or long raw history as realized paper PnL.
- Keep scanner output separate from paper eligibility: routes remain `watch` or diagnostics until focused observations and executable books satisfy the v2 rules.
- Preserve route identity across symbol, collateral, market type, environment/profile, and settlement timestamps.
- Do not block hot focused checks with full scans or slow discovery work.
- Return exact changed files, tests run, and risks. Codex must verify the production call graph and final diff before accepting the worker result.

## Funding Contract

Every market row must obey this contract:

- `funding_rate`: funding cashflow rate for the venue's own settlement interval.
- `funding_interval_hours`: the settlement interval in hours.
- `hourly_funding_rate`: `funding_rate / funding_interval_hours`, used only for cross-venue comparison and projections.
- `next_funding_at`: expected next settlement time.
- `funding_rate_kind`: source/meaning label for audit.

If a venue only publishes an hourly equivalent, set `funding_interval_hours` to the actual cashflow interval if known and keep both published fields for audit. If the source units are uncertain or implausible, fail closed with a risk flag rather than creating a candidate.

## Default Strategy

The production paper strategy is `FUNDING_SETTLEMENT_CAPTURE`:

- long one perpetual venue and short another with the same canonical base quantity;
- enter for the nearest synchronized funding settlement;
- use plan shape `ONE_SETTLEMENT` or `MULTIPLE_SETTLEMENTS` from one `CapturePlan`;
- capture exactly one settlement boundary, then require mandatory exit;
- spread convergence is never expected profit in the default strategy;
- executable spread, basis deterioration, liquidity, fees, stale data, and margin risk are costs/gates;
- legacy `single_settlement_hedged_capture_v1` and `synchronized_funding_capture_v2` names are compatibility/version metadata only, not production strategy owners;
- legacy `funding_only`, `spread_only`, `combined`, and `opportunistic_any` are research/experimental labels only unless Codex explicitly enables an experimental profile.

## Paper Bot Timing

- No routes: full scan after the previous scan completes, normally every 5 minutes.
- Watch route exists: focused recheck every 2 seconds by default.
- Urgent route or open/pending position: focused recheck every 1 second by default.
- Initial entry target: T-30 seconds.
- Entry is allowed only in the T-35 to T-25 second window.
- Both legs must be simulated-filled no later than T-20.
- Latest focused observation age target is <= 10 seconds.
- Default paper entry snapshot age must be <= 20 seconds.
- Cross-venue snapshot skew default is <= 5 seconds for VERIFIED_PAPER and <= 15 seconds for PAPER estimate-based entry.
- Default paper entry is PAPER-capable for typed estimates; use `--no-estimated-funding-paper-entry` for verified-only dry runs.
- The old 15-second freeze-window fallback is disabled by default.
- After settlement, preserve reconciliation obligations and schedule mandatory exit at the first normal-safe opportunity.
- Normal close before T+20 is disallowed except for hard-risk events.

## Project Layout

- Funding logic: `smart_money_radar/funding/`
- Paper bot lifecycle: `smart_money_radar/paper_bot/`
- CLI: `smart_money_radar/cli.py`
- Dashboard API: `smart_money_radar/dashboard.py`
- Dashboard UI: `smart_money_radar/web/index.html`
- Storage: `smart_money_radar/storage.py`
- Runtime scripts: `scripts/`
- Tests: `tests/test_funding_radar.py`, `tests/test_funding_paper_trader.py`, `tests/test_funding_retention.py`
  and `tests/test_synchronized_funding_v2.py`

## Current Venue Notes

- Risky venues disabled by user live in `smart_money_radar/funding/venues.py`.
- `risex_points` is a paper-only profile that whitelists RiseX plus core hedging venues. RiseX uses `current_funding_rate` as the per-settlement cashflow rate and `funding_rate_8h` only as audit metadata.
- DEX/perp venues with non-standard mechanics must be modeled conservatively. Pool-based venues need a separate pool perturbation model before they can be treated like normal orderbook venues.
- Variational is quarantined/deactivated. Do not include it in active scans, focused rechecks, dashboard candidates, paper bot profiles, or route displays until Codex explicitly rebuilds the adapter from official docs and tests the units again.

## Workflow

1. Read the relevant files before editing.
2. Make small, scoped changes.
3. Add or update regression tests for every funding arithmetic or lifecycle change.
4. Run focused tests, then the full test suite.
5. Report exact files changed and tests run.

## Coordination With Codex

Qwen/QN has local write/edit/bash access for scoped implementation tasks. When acting as Codex's worker, it may edit files, create tests, run formatters, run the app, and inspect local data within the task boundaries. Return concise summaries with file paths, risks, and tests. Codex owns final architecture, risk acceptance, and commit boundaries.

Default workflow until Daniil says otherwise:

1. Codex inspects the code and decides the architecture.
2. Codex asks Qwen/QN for audits, implementation, tests, or mechanical refactors.
3. Qwen/QN edits and tests directly within the scoped task.
4. Codex reviews, adjusts, tests, and decides whether the change is accepted.
5. Codex reports the final result to Daniil.
