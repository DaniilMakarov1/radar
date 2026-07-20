# Funding Radar

Funding Radar is a research and paper-execution module for delta-neutral perpetual
carry. It does not trade. It currently builds cross-venue pairs among
Binance USD-M Futures, Bitget, Bybit, OKX, Gate, KuCoin Futures, MEXC,
Backpack, Aster, Hyperliquid, dYdX, Lighter, Paradex, Vertex Base, and Drift
when fresh DLOB data is configured perpetuals.

## Data Flow

1. Load active linear perpetual instruments from all public venue APIs.
2. Match exact symbols plus a small audited alias map for unit-prefixed contracts
   such as `1000PEPE` and `kPEPE`. Unknown aliases remain unmatched.
3. Normalize each venue's funding interval to an hourly rate. Historical settled
   rates are expanded backward over the interval they describe, never forward.
   Binance, Bybit, OKX, Bitget, Gate, KuCoin, MEXC, Aster, Backpack, Lighter, and
   Drift historical intervals
   are inferred locally from adjacent settlement timestamps so a later frequency
   change cannot rewrite older observations.
4. Generate and persist every cross-venue route for every asset traded on at least
   two supported venues. There is no production top-N cap. Every route receives a
   settlement-aware zero-slippage upper bound using the cheaper of taker/taker and
   maker-entry/taker-exit fees plus basis and operations reserves. Full books and
   history are fetched for every route whose upper bound remains positive, without
   a numerical shortlist limit. Routes below that bound are retained in the
   auditable universe table with their exclusion reason; they cannot become
   profitable after adding real slippage. Previous paper candidates are always
   pinned for revalidation.
5. Simulate entry and exit on all four orderbook sides. Route capacity is reduced
   to the weakest executable leg and then receives a depth haircut. Within that
   capacity, test a notional ladder starting at $500 per leg and select the size
   with sufficient dollar Q25 net profit, horizon return, and `P(net > 0)`.
   Capacity uses every published price level returned by the venue API; it is
   not limited by a fixed BPS cutoff or by top-of-book size. Every consumed level
   enters VWAP, and its full entry/exit slippage is deducted before a route can
   pass. A walk deeper than 100 bps remains eligible but receives an explicit
   far-depth dependency risk flag.
   The former fixed 120-second lead-time veto is disabled in the default
   dashboard config. Very near settlement prints remain visible through the
   authorization deadline and should be revalidated manually before any real
   execution.
6. Store every scan, warning, market snapshot, book, history point, route, and
   later paper revalidation in SQLite.

Each route also reads up to 60 prior local L2 snapshots for both venues. For all
four execution sides it records the fraction of snapshots where the selected
notional was fillable, Q25/median displayed capacity, median/Q75 slippage, and a
depth-persistence score. A refill event is a drop of more than 30% in liquidity
within 25 bps of the touch; recovery to at least 90% of the prior depth within
five minutes counts as a refill. These sequence metrics affect confidence and
raise visible fragility flags, but a young local sequence does not by itself hide
an otherwise valid taker route.

Dashboard scans never wait for a historical download. They refresh current
funding and books, reuse the local history cache, and fail closed for a new route
whose history is not ready. The resumable `funding-history-backfill` command
populates every venue outside the live critical path. This separation becomes
mandatory once the full uncapped universe includes hundreds of MEXC contracts
and Paradex's five-second continuous funding series.

The current collectors use public endpoints and require no API keys. Bitget and
Gate contribute their public contract fee, funding interval, current estimate,
book, and settled funding history. Gate contract orderbook sizes are converted
to base units with the published `quanto_multiplier`; stock and pre-market
contracts are excluded. OKX current
funding is collected as one public WebSocket snapshot for the full supported
universe; if that stream fails, the adapter falls back to its public REST endpoint
and records a visible warning.

Aster contributes Binance-compatible public estimates, settled history, and
depth while stock perpetuals are excluded. KuCoin contract quantities are
converted to base units with the published multiplier before VWAP calculation.
Lighter contributes hourly cash-flow funding, settled history, and executable
depth; its public Standard-account zero-fee tier is modeled explicitly, while
account-type and latency risk remain visible in paper-only analysis. The current
Lighter collector preserves the published 8-hour-equivalent display rate for UI
audit, but divides it into the hourly cash-flow rate used by PnL.

For venues that publish funding cap/floor and per-symbol interval overrides,
such as Binance and Aster, market snapshots store those limits alongside the
current funding estimate. A rate exactly at its cap or floor is displayed as an
advisory risk because it can look artificially stable inside the interval and may
reset sharply after settlement. This flag never blocks a route by itself.

MEXC contributes a bulk public current-funding catalog, next-settlement schedule,
settled history, and depth. Every depth quantity is converted from contracts to
base units using the contract's published `contractSize` before capacity and VWAP
are calculated. Stock, TradFi, hidden, and pre-market contracts are excluded.

Paradex contributes public continuous funding and executable depth. Because it
has no settlement cliff, the adapter converts its native period rate to a
one-hour cash-flow equivalent and uses a one-hour revalidation checkpoint. Its
five-second public funding series is averaged only over completed UTC hours;
the latest eight hours are fetched on each stale-history refresh and the local store
accumulates the longer 14/30-day windows over time. A Paradex checkpoint must not
be interpreted as an exchange fee-assessment timestamp.

Backpack contributes public one-hour funding estimates, full depth, and settled
history. Its still-open future funding row is excluded from realized history.
Vertex Base is integrated as a CLOB-style DEX venue through the public Vertex
engine/indexer surfaces documented by the official SDK. It contributes perp
symbols, public x18 fee rates, latest x18 funding rates, hourly market-snapshot
history, and executable market liquidity when the Base endpoint is reachable.
Vertex remains fail-closed: missing funding, missing symbols, transport failure,
or incomplete depth prevents routes rather than filling gaps with synthetic data.
Drift/Velocity live funding now uses the fresh
`https://data.velocity.exchange/stats/markets` surface. Velocity reports funding
as side cashflow in percentage points, so the adapter normalizes it to the
scanner's CEX convention where positive funding means longs pay shorts.
Executable depth comes from the public DLOB endpoint, defaulting to
`https://dlob.velocity.exchange`; the legacy Drift Data API remains a
history-only fallback and is never allowed to override stale live funding.

Extended contributes active crypto perpetual markets from the public Starknet
API, executable orderbook depth, hourly funding history, and flat public fees.
RFQ, off-hours, spot, and TradFi markets are excluded before route construction.
Extended funding is modeled as one-hour funding; the public fee schedule is
taker 0.025% and maker 0%.

Ethereal contributes active USD perpetual products from its public HTTP API.
Funding is modeled as one-hour funding: the adapter uses projected one-hour
funding for the nowcast and settled one-hour funding rows for history.
`market-price` supplies best bid/ask mid and oracle index price; `market-liquidity`
supplies executable depth. Product-level public fees are used per market.

Jupiter Perps is not modeled as a normal CLOB venue. Jupiter's public Perps docs
mark the API as work in progress, while the on-chain program exposes pool,
custody, borrow-fee, utilization, max-position, and fixed open/close fee state
rather than a simple executable orderbook. A Jupiter integration must therefore
be a separate pool-perps adapter that models hourly borrow fees, pool capacity,
price impact, gas/priority fees, collateral asset, and long/short utilization.
Until that model exists, Jupiter is research-only and must not be compared to
Binance/OKX/Bybit orderbooks as if it had equivalent depth semantics.

GMX v2 follows the same rule. It can be valuable for funding and price-impact
arbitrage, but it needs a `pool_perp` model over open-interest imbalance,
adaptive funding, borrow fees, price impact, open/close fees, keeper/network
fees, and caps. It should stay in the planned DEX panel until quote parity and
paper outcomes validate the model.

OKX books publish size in contracts, so the adapter converts each level to base
units using `ctVal * ctMult` before any USD depth or VWAP calculation. dYdX
publishes an oracle price but no separate mark in its market catalog; route basis
therefore uses the contemporaneous executable orderbook mid as mark and keeps the
oracle as index.

## Route Economics

For each asset, the scanner evaluates both directions and keeps the orientation
with the better scheduled cash flow to the next revalidation point. With equal
settlement timing this is usually "long lower normalized funding, short higher
normalized funding"; with mixed one-hour/four-hour/eight-hour venues, the first
cash-flow event can make the safer direction asymmetric.

Funding intervals are stored per market snapshot, not per venue name. A venue can
have mixed one-hour, two-hour, four-hour, and eight-hour products at the same
time, so the dashboard always displays each leg's published rate and its own
period. Fixed 4h/8h comparison columns are event-based: they show a dash when no
paired funding cash flow can occur inside that horizon, and otherwise sum the
live nowcast over actual settlement events while labeling the result as "if live
spread holds".

Settlement interval and estimate update frequency are different concepts. The
interval defines when cash actually transfers and how PnL is accrued. The
published next-funding estimate can update inside that interval, update in larger
steps, or remain pinned when the symbol is at its cap or floor.

Projected funding spread is conservative. It is zero unless enough synchronized,
dense, recent, and contiguous history exists and both the current spread and
recent realized spread have the expected sign. The live anchor has five explicit
allocations. At the baseline 45% next-estimate weight, they are 45% exchange
next-funding nowcast, 25% 24-hour realized mean spread, 15% 72-hour realized mean
spread, 10% 14-day persistence-adjusted spread, and 5% reserved for the 30-day
regime-risk filter. The 30-day median is not added to expected carry.

The exchange nowcast weight is dynamic rather than fixed. It rises piecewise from
15% at the start of the funding interval to 25% at 25% elapsed, 45% at 75%
elapsed, and 60% immediately before settlement. Each leg receives its own weight,
because two venues can be at different stages of their funding intervals. The
route-level diagnostic reports the less mature leg. The nowcast is applied only
to that venue's nearest settlement; later settlements use the historical baseline
and decay rather than repeating one estimate across the entire horizon. The
30-day regime-risk allocation remains fixed at 5%; the remaining
weight is redistributed between 24-hour, 72-hour, and 14-day components in the
baseline 5:3:2 ratio. Binance, Bybit, and MEXC use their public current funding
estimate, OKX uses its public current estimate, Hyperliquid uses
`predictedFundings`, dYdX uses its published next-hour rate, and Paradex uses a
conservative hourly equivalent of its continuously accrued public rate. Each
route stores the source kind.

A forecast window is eligible only after at least 18 synchronized hourly points
for 24 hours, 54 for 72 hours, 252 for 14 days, and 540 for 30 days. The 24-hour
window is the only mandatory predictive window. Missing 72-hour, 14-day, or
30-day context is disclosed and conservatively shrinks the signal; it does not
silently increase the nowcast weight or independently block an otherwise valid
short-horizon route.

The 30-day window also acts as the primary live anomaly and volatility filter, so
its influence is larger on risk than its 5% contribution to expected spread.
Historical forward outcomes come only from the trailing 30 days, are conditioned
on a similar starting spread, use non-overlapping windows, and receive recency
weights with the strongest weight in the latest 72 hours. Positive outcomes receive
a regime-age penalty, and the settlement forecast decays by an estimated half-life
instead of extending one constant rate linearly.

The full 90-day history does not set expected carry. It supplies robust z-score,
tail percentile, long-regime volatility, anomaly penalty, and duration-survival
context. This prevents an old regime from dominating the next-settlement forecast
while preserving its value for risk limits.

The trading horizons are the first upcoming funding cash flow on either leg, fixed 4 hours, fixed 8 hours,
or fixed 24 hours. The 72-hour mode is explicitly `Research` and can never become
a paper candidate. Funding PnL is calculated as discrete venue settlement events,
including one-sided event times when intervals differ; continuously accrued
Paradex funding uses hourly cash-flow checkpoints. The next-settlement route
is revalidated at that first event rather than silently assuming a later hold.

Completed positive-spread regimes from the full historical window are also used
as a survival sample. Conditioned on the current regime age, the model reports
remaining-duration Q25, median, and Q75 plus the probability of surviving another
4, 8, and 24 hours. This duration forecast is separate from PnL authorization.

Expected net profit includes:

- projected funding over the selected holding horizon;
- four taker operations: open and close on both venues;
- executable VWAP slippage on both entry and exit sides;
- signed executable entry basis and adverse basis-change scenarios;
- a small model-uncertainty reserve;
- an operations buffer.

Both legs use the same canonical base-asset quantity, not merely the same dollar
notional. Bundled contracts such as `1000PEPE` and `KSHIB` are converted to the
same token units before prices, depth, and hedge quantities are compared. For an
executable basis above 50 bps, the route also needs synchronized L2 basis history
and must pass a separate stress: Q25 funding after adverse convergence or widening,
all fees, depth, and buffers must still clear the actionable-profit threshold.
There is no 20/200-bps trading veto. A 2,000-bps absolute sanity ceiling remains
to catch likely contract-identity or unit errors.

The executable candidate gate remains taker-entry/taker-exit. Every route also
stores an indicative maker-entry/taker-exit scenario with public or overridden
maker fees, visible queue-ahead notional, joint fill probability, one-leg mismatch
reserve, adverse-selection reserve, and expected attempt PnL. Maker probability is
estimated from the local snapshot sequence: a buy at the best bid is conservatively
treated as filled only when a later best ask trades through that quote within 120
seconds, and vice versa for a sell. A Wilson lower bound replaces the prior after
at least ten trials per leg. `both filled`, `one leg only`, and `neither filled`
are modeled separately. Because public L2 snapshots cannot distinguish trades from
cancellations or reconstruct queue position, this remains a shadow scenario and
cannot promote a route to `paper_candidate`.

Account-specific fees can be supplied without changing code:

```bash
FUNDING_BINANCE_TAKER_FEE=0.00031
FUNDING_BINANCE_MAKER_FEE=0.00010
FUNDING_FEE_OVERRIDES_JSON='{"okx":{"taker":0.00035,"maker":0.00010}}'
```

The route records whether each fee came from an account override, a venue public
tier, or a conservative public default.

Return on capital conservatively reserves full notional on both venues plus a 20%
collateral reserve. Thus a $500 position per leg requires $1,100 of modeled capital.
Headline funding APR is displayed as context and is never treated as expected PnL.
The 25th percentile of net PnL must clear both a $5 absolute hurdle and 10 bps of
modeled total capital after all costs. `P(net > 0)` must also clear its gate.
Annualized ROC is retained only as legacy output and never authorizes or ranks a
route. Median PnL is displayed as context but does not authorize a candidate. The
probability gate uses the lower bound of a 90% Wilson interval; the empirical win
fraction remains visible for comparison.

Historical funding outcomes are genuine point-in-time rates. Until the local
collector has accumulated enough historical books, execution costs in funding
outcomes still use the current four-side fee and slippage snapshot. Signed basis
stress uses synchronized local L2 sequences and a structural widening floor.
The dashboard separately exposes the genuine local L2 persistence/refill sequence
and labels the remaining funding/execution join as a cost-stressed historical
proxy rather than a completed execution backtest.

## Fail-Closed Gates

A route remains in `Watch` when any of the following is true:

- insufficient synchronized funding history;
- sparse or stale synchronized history;
- unstable funding direction or weak persistence;
- fewer than the required contiguous historical forward outcomes;
- no confirmed funding cash flow inside the holding horizon;
- incomplete mandatory 24-hour forecast window;
- empty or crossed orderbooks;
- insufficient two-sided depth;
- Q25 funding carry does not cover all modeled costs and reserves;
- `P(net > 0)` is below the minimum gate;
- Q25 dollar PnL is below the greater of $5 or 10 bps of total modeled capital;
- a large basis is not covered by Q25 funding under the basis stress;
- executable basis exceeds the 2,000-bps identity/unit sanity ceiling.

All routes also disclose that cross-venue execution is non-atomic, public fee tiers
may differ from the actual account when no override is configured, account-specific
margin is unverified, and cross-venue identity still lacks a verified contract
identity graph. These are explicit risk flags rather than hidden assumptions.

## Paper Validation

The first qualifying snapshot creates a `paper_candidate`, not a fill. Its
authorization expires at the earliest upcoming settlement on either leg. A later
scan rebuilds the same orientation with fresh books and records either
`settlement_reauthorized` or `settlement_exit_required` after a crossed settlement.
It never scales PnL beyond the notional actually repriced through that later book.
The module never labels projected PnL as realized PnL.

## Commands

```bash
python3 -m smart_money_radar.cli funding-scan --target-notional 10000 --horizon-mode next_settlement
python3 -m smart_money_radar.cli funding-scan --target-notional 10000 --horizon-mode fixed --horizon-hours 24
python3 -m smart_money_radar.cli funding-scan --horizon-mode research --horizon-hours 72
python3 -m smart_money_radar.cli funding-history-backfill --days 90 --limit 200
python3 -m smart_money_radar.cli funding-history-backfill --venue mexc --limit 50
python3 -m smart_money_radar.cli funding-history-backfill --venue paradex --limit 10
python3 -m smart_money_radar.cli funding-report
python3 -m smart_money_radar.cli funding-watch --interval-seconds 60
python3 -m smart_money_radar.cli funding-paper-trader --target-notional 500
python3 -m smart_money_radar.cli funding-paper-report
python3 -m smart_money_radar.cli funding-paper-export
python3 -m smart_money_radar.cli dashboard
```

## Paper Trader

`funding-paper-trader` is a deterministic local paper-trading loop. It does not
use an LLM or live capital. The trader initializes $1,000 virtual cash per live
venue, runs `next_settlement` Funding Radar scans, and opens a paper position
only when both legs have funding settlement inside the configured entry window
of 180 seconds. It reserves margin on both venues, records every action in
SQLite, exports CSV files under `exports/funding_paper/`, and optionally sends
Telegram notifications.

The first model is a strict settlement-capture test. It closes after the paired
settlement window once funding history is published. If final funding history is
not yet available, the position stays in `settlement_pending` rather than
inventing immediate PnL. After a configurable publication deadline, it can close
with an explicit entry-estimate fallback flag.

Telegram setup uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. After
sending any message to the bot, run:

```bash
python3 -m smart_money_radar.cli telegram-chat-id
python3 -m smart_money_radar.cli telegram-test
```

Open `http://127.0.0.1:8787`; `Funding` is the default view. The main table contains
only routes that pass every economic and data-quality gate. Rejected positive or
near-break-even routes are used for diagnostics only, not as trading opportunities.
Each displayed route shows the published funding rate, the normalized cash-flow
rate, and the settlement interval for both the long and short venue.

`Полный route universe` reports the number of generated routes, settlement-ready
routes, routes sent to full-depth evaluation, and routes rejected by the provable
zero-slippage cost bound. `--max-candidates` is a test/debug override only; its
default value `0` means unlimited, and the dashboard always uses unlimited mode.

The dashboard's `Почему нет кандидатов` band classifies every route by its first
failed gate, so counts are mutually exclusive. It also shows median current gross,
full modeled cost, the dollar break-even gap, and the five routes nearest to
covering their costs. The older risk summary remains available as a non-exclusive
diagnostic and should not be used to add blocker counts together.

When no route qualifies, the dashboard shows a sequential economics funnel for
routes with at least $500 capacity: gross carry covering the complete execution
cost, positive median PnL, positive Q25 PnL, and Q25 clearing the actionable
profit threshold. Thin routes do not inflate these counters or the blocker summary.
It also shows the horizon, Q25 and median PnL, `P(net > 0)`, decay half-life,
conditional regime survival, remaining-duration quantiles, authorization deadline,
and settlement-by-settlement forecast.
Routes with less than $500 executable capacity per leg are omitted from both the
Candidates and Watch views. `Position / leg` is the optimized modeled notional;
`Capacity` is the larger maximum allowed by current two-sided depth.

Automatic refresh is disabled in the dashboard by default. Manual `Update` always
requests fresh current funding and selected orderbooks from venues; settled
funding history stays out of the critical path and is maintained by the backfill
job. If auto-refresh is re-enabled later, refreshes must not overlap, and automatic
scans should reuse the last completed scan configuration so stale browser tabs
cannot silently overwrite the active size or horizon.

## Current Limitations

- Fee overrides are supported, but authenticated fee-tier, margin balance, and
  liquidation-distance checks are not connected.
- No automated transfer-time, deposit/withdrawal, borrow, or collateral-rebalancing model.
- No live order placement; both legs remain non-atomic.
- The first route family is pairwise perpetual carry; spot-perp and dated futures
  basis routes are not implemented.
- Exact ticker matching is not sufficient for live capital allocation.
- Historical persistence is not proof that future funding will remain positive.
- Historical execution currently uses a current-cost proxy until enough local
  point-in-time orderbook and basis snapshots accumulate.
- Public API availability can be jurisdiction-dependent. A failed venue is shown
  as a scan warning while the remaining venue pairs continue to run.

The next production step is shadow execution with account-specific fees and margin,
followed by portfolio-level collateral allocation. Live trading
should remain disabled until the paper ledger has enough later-snapshot observations
to estimate fill decay and false-positive rates.
