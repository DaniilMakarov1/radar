# Model Instructions for Smart Money Radar

This document is the working contract for any AI model that edits or audits this
repository. It is intentionally more operational than the product docs: read it
before changing code, risk gates, venue adapters, paper bot behavior, Telegram
messages, or dashboard logic.

## 1. Current Product Scope

The active product is Funding Radar:

- deterministic public-data scanner for perpetual funding/spread opportunities;
- paper-only position lifecycle;
- local dashboard;
- Telegram notifications;
- CSV export for paper trade reports.

The older Prediction, Dune, wallet research, and on-chain radar work may still
have remnants in history or docs, but the current repo should stay funding-first
unless Daniil explicitly changes direction.

Do not enable live trading. Do not add exchange order placement. Do not treat
paper outcomes as proof of real executable alpha.

## 2. Agent Roles

Default workflow:

- Codex is architect, orchestrator, reviewer, and final risk owner.
- Qwen/QN is an implementation worker for scoped tasks.
- Other models should follow this same split unless Daniil explicitly says to
  work differently.

Implementation workers may edit files, run commands, add tests, and refactor
inside the requested scope. They must not independently change strategy
philosophy, risk gates, venue eligibility, funding-unit assumptions, live-trading
status, or architecture boundaries.

## 3. Repository Map

Important paths:

- `smart_money_radar/funding/` - venue adapters, scanner, forecasts, economics,
  route construction, liquidity and profiles.
- `smart_money_radar/paper_bot/` - paper entry, hold, settlement, close,
  spread monitoring, Telegram formatting.
- `smart_money_radar/storage.py` - SQLite schema and dashboard/report queries.
- `smart_money_radar/cli.py` - operational commands.
- `smart_money_radar/web/index.html` - local dashboard UI.
- `scripts/` - local start/stop/status/launchd helpers.
- `tests/test_funding_radar.py` - scanner/economics/adapters.
- `tests/test_funding_paper_trader.py` - bot lifecycle, entry/hold/close.
- `tests/test_funding_retention.py` - pruning and data retention.
- `QWEN.md` - short worker instructions.
- `docs/AGENT_HANDOFF.md` - handoff context and where raw transcripts live.

Before editing, read the relevant module and the matching tests. Do not patch by
memory.

## 4. Funding Data Contract

Every funding market row must obey this contract:

- `funding_rate`: cashflow rate for the venue's actual settlement interval.
- `funding_interval_hours`: actual settlement interval in hours.
- `hourly_funding_rate`: `funding_rate / funding_interval_hours`; this is for
  cross-venue comparison and projections, not necessarily what the UI displays.
- `next_funding_at`: next expected cashflow time.
- `funding_rate_kind`: audit label for the source and meaning of the rate.
- `published_funding_rate` and `published_funding_interval_hours`: optional UI
  fields when a venue displays a normalized rate that differs from the cashflow
  rate.

Never assume all venues publish the same unit. Some publish a per-settlement
rate, some publish hourly estimates, some publish an 8-hour equivalent, and some
update the estimate continuously inside a settlement interval. If units are
uncertain, fail closed with a visible risk flag rather than creating candidates.

Examples:

- RiseX currently uses `current_funding_rate` as the 1-hour cashflow rate and
  `funding_rate_8h` as display/audit metadata.
- Lighter publishes an 8-hour-equivalent display rate while the cashflow model
  stores hourly funding.
- OKX, Binance, Bybit, Aster, MEXC, and similar CEX venues may publish a current
  next-settlement estimate that can update differently from the cashflow
  settlement interval.

Any adapter change must add tests that prove:

- source units;
- settlement interval;
- `hourly_funding_rate`;
- `published_*` display behavior if relevant;
- depth unit conversion and contract multiplier handling.

## 5. Scanner and Candidate Contract

The scanner should screen the broad route universe without arbitrary top-N caps.
It may use cheap pre-depth arithmetic to avoid impossible full-depth work, but
only when a route cannot become profitable after adding real depth and costs.

Do not add hard route limits such as "top 50", "top 300", or "100 routes" as a
production gate. If a performance guard is necessary, it must be:

- explained as a temporary safety fuse;
- visible in diagnostics;
- tested;
- not able to silently hide otherwise positive routes.

Default candidate status is not "funding is positive" or "spread is positive"
by itself. The active paper strategy is:

```text
synchronized_funding_capture_v2
```

This is cross-venue delta-neutral funding capture:

- long perpetual on one venue;
- short perpetual on another venue;
- same canonical base quantity;
- entry reason is the nearest synchronized funding settlement;
- spread convergence is not expected profit;
- executable spread, book walking, fees, basis deterioration, stale data,
  liquidation/margin risk, and venue capability are costs/gates.

The initial economic model is:

```text
initial_expected_net_pnl =
  conservative_funding_gross
  - baseline_round_trip_book_cost
  - total_round_trip_fee_estimate
  - entry_basis_reserve_usd
  - entry_legging_reserve_usd
```

Entry requires:

```text
conservative_funding_gross >= max(2.50, reference_notional * 0.005)
initial_expected_net_pnl >= max(1.00, reference_notional * 0.002)
initial_cost_coverage_ratio >= 1.50
```

Legacy `funding_only`, `spread_only`, `combined`, and `opportunistic_any`
remain available for experimental/research profiles, but they are not the
default paper strategy and must not be shown as production-ready by default.

## 6. Paper Bot Runtime Modes

The Paper Bot loop lives mostly in `smart_money_radar/funding/trader.py` and
`smart_money_radar/paper_bot/position.py`.

Risk exits:

- A common 10% move in both legs is telemetry, not an automatic close. It should
  create a warning/critical event and force fresh risk recalculation.
- Basis/spread deterioration, stale data, quantity mismatch, margin/liquidation
  risk, mark/index divergence, venue failure, and schedule mismatch can close or
  emergency-unwind the position.
- Basis/spread stop-loss protects against adverse hedge divergence.

The intended runtime cadence is:

- no hot routes: full market scan every `scan_interval_seconds` after the
  previous scan completes;
- hot/watch route exists: focused recheck every `monitor_interval_seconds`;
- urgent route, pending settlement, or open position: focused recheck every
  `hot_interval_seconds`;
- status Telegram report: every `status_report_interval_seconds`, or disabled if
  the value is `0`.

The launch scripts may override class defaults through environment variables.
When auditing live behavior, check the active command or launchd script, not only
`PaperBotConfig`.

The current launch defaults are approximately:

- full scan: 300 seconds;
- watch/open normal recheck: 2 seconds;
- urgent/open recheck: 1 second;
- status report: 900 seconds from `.env.example`/launch scripts, while direct
  `PaperBotConfig()` defaults to 3600 seconds;
- target notional: $500 per leg;
- starting virtual balance: $1000 per venue.

Full market scan can run in the background while hot rechecks continue. It must
not block urgent entry checks. The current implementation uses an isolated
temporary SQLite store for the background scan and merges only route payloads
back into the in-memory watch list.

## 7. When The Bot Enters a Position

The bot opens only paper positions. Entry is controlled by `route_entry_decision`.

A route can enter only if all conditions are true:

1. The route status is `paper_candidate`.
2. The route has an eligible selected strategy allowed by
   `config.strategy_set`.
3. Both long and short legs exist.
4. Both legs have parseable `next_funding_at`.
5. The two `next_funding_at` timestamps are synchronized:
   `abs(long_next_funding_at - short_next_funding_at) <= 1.0 second`.
6. Both legs are inside the entry window:
   `entry_min_lead_seconds <= lead <= entry_max_lead_seconds`.
   With the current defaults this means T-35 to T-25 seconds, target T-30.
7. Both legs must be simulated-filled no later than T-20.
8. Neither settlement has already passed.
9. If both legs are in the entry window, the route snapshot must be fresh:
   `route_data_age_seconds <= max_entry_snapshot_age_seconds`; current default
   is 2 seconds.
10. Each venue has enough paper balance for that leg:
   `notional * (1 + collateral_reserve_fraction)`.
11. Selected strategy expected net PnL is positive and at least
   `required_live_net_profit`.
12. There is no already-open position for the same `route_key`.
13. There is no existing position with the same `entry_key`.
14. If focused recheck is enabled, the route is rechecked before opening.

`required_live_net_profit` is:

```text
max(config.min_live_net_profit, selected_strategy.actionable_profit_threshold)
```

In the scanner, `actionable_profit_threshold` normally comes from
`FundingScanConfig.minimum_net_profit`, currently $1 by default for the selected
size.

Important final-window behavior:

- `final_recheck_freeze_seconds` defaults to 0 in the v2 launch path.
- Do not use the old 30-second fallback snapshot for entry.
- If no fresh snapshot exists, skip entry.

Telegram `ARMED` means the route is within the wider arm window and still
economically valid. It is not the same as `OPEN`. `OPEN` happens only after the
final entry checks above.

Objective assessment:

- This matches Daniil's requested paper-test idea: do not enter three minutes
  early; track candidates in advance, then enter around T-30 with a fresh
  snapshot.
- The current entry window is intentionally non-zero because future real
  execution needs submission/fill time.
- Requiring both legs to be inside the same final window is conservative. It
  avoids many one-sided funding captures. That matches the safer logic discussed,
  but it also means the bot may miss opportunities where one leg settles now and
  the other settles later.

## 8. When The Bot Continues Holding

Open position processing happens before settlement close. First, if spread
monitoring is enabled, the bot can close immediately on spread stop-loss.

Spread stop-loss:

- compute current executable spread/basis from the latest route;
- compare it with entry spread;
- if unrealized basis loss reaches `basis_stop_loss_bps`, close immediately;
- default is 200 bps.

If the spread stop-loss does not trigger, `close_decision` handles settlement and
hold:

1. If `max_settlement_at` is missing, wait until the publication lag limit. If it
   remains missing after `max_settlement_publication_lag_seconds`, close with
   `settlement_publication_timeout`.
2. If current time is before
   `max_settlement_at + no_normal_exit_before_settlement_plus_seconds`, normal
   close is disallowed unless there is a hard-risk event.
3. Around T+5, probe the next funding schedule for both venues.
4. Around T+30, run hold underwriting for the next synchronized cycle.
5. Pull realized funding rates from local funding history near
   the settlement time.
6. If history is missing, use entry estimate fallback for paper accounting and
   mark the settlement as preliminary.
7. Fetch the latest route by `route_key`.
8. Run `position_hold_decision`.
9. If hold is true, accrue the funding settlement, update the position to the
   next route's future settlement times, and keep the position open.
10. If hold is false, close and record funding PnL, basis PnL, execution cost, and
   reason.

The position continues holding only if all conditions are true:

- latest route exists;
- route snapshot is fresh enough;
- both route legs exist;
- route does not contain hard data-quality flags such as `unit_identity_mismatch`
  or `basis_divergence`;
- both next settlements are parseable and future;
- next settlements are synchronized within 1 second;
- next synchronized settlement is at least 300 seconds and at most 14,400 seconds
  away;
- the position has captured fewer than 4 settlements;
- projected position age stays within 14,700 seconds;
- selected strategy is still allowed and eligible;
- selected strategy expected net PnL is still positive;
- for funding-sensitive strategies, funding direction has not inverted
  (`short_hourly` must not be below `long_hourly`).

Objective assessment:

- This matches the agreed correction that the bot should not close merely because
  one funding settlement happened. It accrues funding and keeps the position open
  while the arbitrage window remains valid.
- The hold gate is incremental: opening fees are sunk costs and must not be
  subtracted again. It compares closing now against waiting for the next
  synchronized settlement.
- The hold decision uses the latest route snapshot. If focused recheck fails and
  no fresh route is available, the bot should close or skip holding rather than
  assume the old opportunity survived.

## 9. PnL Accounting

Funding cashflow signs:

```text
long leg PnL  = -notional * funding_rate
short leg PnL =  notional * funding_rate
```

Therefore:

- positive funding: longs pay, shorts receive;
- negative funding: longs receive, shorts pay.

Current executable PnL for an open position must exclude pending future funding:

```text
paper_net_if_exit_now =
  current_long_price_pnl
  + current_short_price_pnl
  + confirmed_funding_pnl
  - paper_open_fees
  - current_close_fees
  - emergency_unwind_costs_already_incurred
```

Do not include expected future funding, provisional settlement estimates, or
spread convergence in `paper_net_if_exit_now`.

Closed/reconciled result:

```text
paper_net_pnl_reconciled =
  paper_long_price_pnl
  + paper_short_price_pnl
  + sum(reconciled_cycle_funding_pnl)
  - paper_open_fees
  - paper_close_fees
  - paper_emergency_unwind_cost
```

The older dashboard/storage layer still contains compatibility fields such as
`actual_net_pnl`. New accounting code should prefer the `paper_*` names and
should not treat pending funding as confirmed PnL.

## 10. Venue Rules

Risky or disabled venues live in `smart_money_radar/funding/venues.py`. Do not
resurrect a disabled venue by only changing the UI. A venue is active only when:

- its adapter is registered;
- it is not in `DEACTIVATED_FUNDING_VENUES`;
- funding units are tested;
- contract/base quantity units are tested;
- depth units are tested;
- failure mode is fail-closed;
- it has regression tests.

Default profile uses `venue_set=None`, meaning all active registered adapters are
scanned automatically. Do not replace this with a hardcoded production list.

For `synchronized_funding_capture_v2`, active is still not enough. A venue is
paper-eligible only if it supports perpetuals, linear contracts, USDT/USDC/USD
collateral, discrete next-settlement funding, next funding timestamp, mark/index
prices, executable orderbook depth, 24h quote volume, open interest, taker fee,
quantity step, and min notional. Otherwise it remains diagnostics/research-only
with an explicit reason.

For CLOB venues, depth must be modeled by walking all returned levels needed for
the target base quantity. Do not use only top-of-book size. Do not use a fixed
BPS cutoff as a hard gate. Large book walks may receive risk flags, but if the
VWAP math still leaves positive net, the route can remain visible.

For pool-based DEX perps such as Jupiter Perps or GMX, do not pretend they are
CLOB venues. They need separate pool-perps economics: utilization, borrow fees,
position caps, price impact, keeper/network fees, collateral constraints, and
pool imbalance.

## 11. Data Retention and Runtime Data

The system should preserve paper trade history and compact evidence needed to
understand decisions. It should not accumulate unlimited scan diagnostics.

Keep:

- paper positions;
- paper events needed to explain opens, holds, closes, crashes, and Telegram;
- latest useful scan snapshots;
- compact funding history needed for forecasts.

Prune:

- stale route universes;
- stale orderbook snapshots;
- stale market snapshots;
- old routine scan/status events;
- old diagnostic raw JSON unless explicitly debugging.

Retention must never break paper-position foreign keys. If pruning fails with a
SQLite lock or FK issue, the trading loop should continue and record a warning.

## 12. Dashboard and Telegram Rules

The dashboard and Telegram should show decision-relevant information only:

- active candidates with positive actionable PnL;
- funding rate per leg and interval;
- published/display rate if it differs from cashflow rate;
- next settlement timing;
- selected edge label;
- funding component, spread component, fees/slippage/reserves when relevant;
- clear close reason with details.

Do not show negative-PnL routes as candidates. They can be diagnostics only.

Funding bot must use funding Telegram credentials:

- `FUNDING_TELEGRAM_BOT_TOKEN`;
- `FUNDING_TELEGRAM_CHAT_ID`.

Prediction or other bots must not send funding status messages.

## 13. Testing Requirements

Run focused tests for the area changed. For funding arithmetic, adapter units,
entry/hold/close behavior, or route filtering, run at least:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_funding_radar.py tests/test_funding_paper_trader.py tests/test_synchronized_funding_v2.py -q
```

For retention or storage changes, also run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_funding_retention.py -q
```

Before claiming the system is working, run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q
```

If tests cannot run, state exactly why and what residual risk remains.

## 14. Common Failure Patterns

Be especially suspicious of:

- funding values that are 4x or 8x too large;
- UI multiplying an already per-interval rate by the interval again;
- `published_funding_rate` being used for cashflow PnL;
- route filters that separately demand "good funding" and "good spread" instead
  of evaluating total net;
- old history acting as a hard veto for a strong next-settlement live
  opportunity;
- full market scans blocking urgent focused checks;
- stale snapshots being used as fresh entry data;
- common 10% price moves being treated as automatic close instead of telemetry;
- spread-only routes ignoring funding drag;
- funding-only routes ignoring spread/basis drag;
- disabled venues appearing in candidates, paper accounts, or Telegram;
- contract-unit aliases such as `1000TOKEN`, `kTOKEN`, or venue-specific lot
  units being compared as if they were the same base quantity.

## 15. Change Reporting

Every model should report:

- files changed;
- behavior changed;
- tests run and results;
- risks that remain;
- whether the implementation matches the intended trading logic or changes it.

If a model is uncertain whether a trading/risk behavior should change, it should
list the uncertainty instead of silently choosing a more aggressive rule.
