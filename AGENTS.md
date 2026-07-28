# Radar Agent Contract

This repository is currently Funding-first.

## Roles

- Codex/GPT is the architect, orchestrator, risk owner, and final reviewer.
- Qwen/QN is a scoped implementation worker. It may edit code only inside a concrete task and must not choose formulas, thresholds, venue eligibility, strategy behavior, or commit boundaries independently.

## Active Strategy

Default paper trading uses `synchronized_funding_capture_v2` only.

- Entry reason: nearest synchronized funding settlement.
- Position shape: long one venue, short another venue, same canonical base quantity.
- Entry window: both legs 25-35 seconds before settlement, target T-30.
- Fill deadline: both legs filled by T-20.
- Settlement alignment: long and short next funding timestamps must differ by at most 1 second.
- Spread convergence is not expected profit for the default strategy; spread/basis is modeled as cost and risk.
- Legacy `funding_only`, `spread_only`, `combined`, and `opportunistic_any` belong to experimental/research profiles unless Codex explicitly says otherwise.

## Runtime Safety

- Paper-only. Do not enable live trading or real exchange order placement.
- Use all active registered funding adapters by default; do not hardcode a small allowed venue list.
- Respect `DEACTIVATED_FUNDING_VENUES`.
- Incompatible active venues are diagnostics/research-only, not paper-eligible candidates.
- A common 10% price move is telemetry and a fresh-risk warning, not an automatic close.
- Every settlement is a separate cycle; reconciliation does not block hold/close decisions.
- A position can capture at most 4 settlements and live about 4 hours 5 minutes.

## Before Editing

Read the relevant source and tests first. For funding lifecycle work, start with:

- `docs/MODEL_INSTRUCTIONS.md`
- `QWEN.md`
- `smart_money_radar/funding/trader.py`
- `smart_money_radar/paper_bot/position.py`
- `smart_money_radar/funding/strategy_synchronized_funding.py`
- matching tests under `tests/`

Run focused tests for your area, then run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```
