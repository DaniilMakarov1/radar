# Synchronized Funding Paper MVP Runbook

This is the first runnable paper-trading MVP for `synchronized_funding_capture_v2`.
It is not production live trading.

## Test Commands

Targeted synchronized funding runtime tests:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_synchronized_funding_v2.py tests/test_reconciliation_worker.py -q
```

Full test suite:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

Deterministic PaperBot MVP scenario:

```bash
PYTHONPATH=. python3 scripts/run_synchronized_funding_paper_mvp.py
```

The scenario uses a temp SQLite database, fake clock, fixture market snapshots,
fixture endpoint identity, trusted configured fee evidence, and
`FakeFundingSettlementDataProvider`. It runs:

`discover -> qualify -> arm -> open -> boundary -> replan -> close -> reconcile`

Expected output is JSON lines for `armed`, `open`, `boundary`,
`post_boundary_iteration`, `replan_close`, and `reconcile`.

## Read-Only Public Smoke

When network is available, run a short public-data smoke without Telegram:

```bash
PYTHONPATH=. python3 -m smart_money_radar.cli --db data/funding-readonly-smoke.sqlite funding-shadow-monitor --duration-seconds 1 --no-telegram
```

Allowed in this smoke: public instruments, prices, funding rates, orderbooks,
funding history, and endpoint identity checks.

Forbidden: orders, cancellations, account state mutation, Telegram sends,
deposits, withdrawals, live canaries, and real secrets.

## Environment

Required for deterministic tests and scenario: none.

Useful for public smoke:

- `PYTHONPATH=.` when running from the repository root.
- Network access for public endpoints.

Live execution must remain disabled. Do not set live-order credentials for this
MVP run. Keep Telegram disabled with `--no-telegram` for smoke commands.

Fee overrides are allowed only as explicit configured evidence inputs for paper
tests. Bare numeric fees without provenance fail closed.

## Venue Scope

Paper-enabled core route universe is the active registered CLOB venues that pass
strict endpoint identity, capability, funding semantics, fee provenance,
freshness, liquidity, and paper execution gates. The deterministic MVP scenario
uses fixture-backed `binance` and `bybit` paper legs.

Research-only/fail-closed for this MVP:

- `pacifica`: remains blocked unless instrument mapping, funding units,
  settlement interval, position inclusion rule, fee source, endpoint identity,
  freshness, and public/account history semantics are confirmed.
- `nado`: remains blocked unless cumulative index/rate normalization,
  interval, next update timestamp, position rule, fee evidence, and endpoint
  identity are confirmed.
- `variational`: strictly `RESEARCH_ONLY`; RFQ data is not converted into CLOB
  orderbook execution.

Disabled venues still respect `DEACTIVATED_FUNDING_VENUES`.

## Data Sources

Deterministic MVP scenario:

- Fixture market snapshots and orderbooks.
- Fixture endpoint identity.
- Fixture trusted configured fee evidence.
- Fixture settlement public events and mark snapshots.
- No public HTTP and no secrets.

Public smoke:

- Public venue endpoints only.
- No account endpoints and no private credentials.

## Current Limits

- MVP is paper-only and deterministic for local verification.
- HOLD requires reliable reconciled history; insufficient history closes or
  treats the next cycle as a fresh independent entry.
- Cross USDC/USDT routes share USD collateral class only when a fresh
  stablecoin snapshot passes peg/freshness/source gates.
- Full scans are background work and are not required for the hot lifecycle
  scenario.
