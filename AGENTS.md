# Radar Agent Contract

This repository is currently Funding-first.

## Roles

- Codex/GPT is the architect, orchestrator, risk owner, and final reviewer.
- Qwen/QN is a scoped implementation worker. It may edit code only inside a concrete task and must not choose formulas, thresholds, venue eligibility, strategy behavior, or commit boundaries independently.

## Active Strategy

Default paper trading uses `synchronized_funding_capture_v2` only.

- Entry reason: nearest synchronized funding settlement.
- Position shape: long one venue, short another venue, same canonical base quantity.
- Default paper entry is EXPERIMENTAL_PAPER-capable for typed estimated funding; use `--no-estimated-funding-paper-entry` for verified-only dry runs.
- Scanner status before focused observations is `watch`, not `paper_candidate`.
- Entry window: both legs 25-35 seconds before settlement, target T-30.
- Fill deadline: both legs filled by T-20.
- Settlement alignment: long and short next funding timestamps must differ by at most 1 second.
- Spread convergence is not expected profit for the default strategy; spread/basis is modeled as cost and risk.
- Funding estimates are not cashflow; confirmed funding enters paper PnL only
  after public rate plus settlement mark reconciliation.
- Hold uses exact next timestamps and incremental economics. Pending
  reconciliation from the previous cycle does not block hold.
- Legacy `funding_only`, `spread_only`, `combined`, and `opportunistic_any` belong to experimental/research profiles unless Codex explicitly says otherwise.

## Runtime Safety

- Paper-only. Do not enable live trading or real exchange order placement.
- Use all active registered funding adapters by default; do not hardcode a small allowed venue list.
- Respect `DEACTIVATED_FUNDING_VENUES`.
- Incompatible active venues are diagnostics/research-only, not paper-eligible candidates.
- A common 10% price move is telemetry and a fresh-risk warning, not an automatic close.
- Every settlement is a separate cycle; reconciliation does not block hold/close decisions.
- A position can capture at most 4 settlements and live about 4 hours 5 minutes.

## Design Rules From Project History

- Prove one full synchronized funding cycle before expanding scope: scan, watch, focused observations, T-30 entry, T-20 fill deadline, settlement, reconciliation, hold/close, and restart recovery.
- Do not add a venue to paper eligibility just because it returns data. Promote venues through explicit stages: data-only, research-only, shadow, experimental-ready, then verified paper.
- Write or confirm the data contract before implementation: funding units, interval, next settlement timestamp, public/final rate source, settlement mark source, symbol identity, collateral, market type, and known failure modes.
- Treat zero candidates as a diagnostics problem, not permission to weaken gates. Add funnel visibility before relaxing any paper eligibility rule.
- Scanner output is not a trade decision. Before focused observations and executable books, route status is `watch` or diagnostics, never production-ready.
- Funding estimates, long raw history, and spread convergence are not realized funding cashflow. Only reconciled settlement rate plus settlement mark can enter confirmed paper PnL.
- Route identity must not collapse different markets. Include venue, canonical base, venue symbol, quote/collateral, market type, environment/profile, and settlement timestamps wherever identity affects storage, dedupe, or risk.
- Scheduler and hot-path behavior are strategy-critical. Full scans must not block urgent focused checks, open-position risk checks, T-30 entry, T-20 fill handling, or settlement reconciliation.
- UI and Telegram must stay actionable and bounded. Negative-PnL, expired, capacity-zero, research-only, and incompatible routes may be diagnostics, but not paper candidates or noisy alerts.
- Treat every worker-model result as an untrusted diff until Codex verifies the production call graph, relevant tests, and final git diff. Worker models may implement scoped tasks, not decide formulas, thresholds, venue eligibility, strategy behavior, or commit boundaries.
- Prefer small, reviewable changes with regression tests over broad rewrites or multi-module feature bursts. Large historical mega-sessions are not a substitute for executable invariants.

## Before Editing

Read the relevant source and tests first. For funding lifecycle work, start with:

- `docs/MODEL_INSTRUCTIONS.md`
- `QWEN.md`
- `smart_money_radar/funding/trader.py`
- `smart_money_radar/paper_bot/runtime_v2.py`
- `smart_money_radar/funding/strategy_synchronized_funding.py`
- `smart_money_radar/paper_bot/execution.py`
- `smart_money_radar/paper_bot/accounting.py`
- `smart_money_radar/paper_bot/settlement.py`
- `smart_money_radar/paper_bot/risk.py`
- matching tests under `tests/`

Run focused tests for your area, then run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```
