# Backtest Methodology

## Purpose

The backtest is the most important part of Smart Money Radar. It protects the project from retrospective storytelling and false confidence.

The system must prove that it could have identified useful signals using only data available at the time.

## Core Rule

No look-ahead bias.

At historical date `T`, every feature, label, wallet score, liquidity snapshot, social metric, and token risk metric must be computed using data with timestamp less than or equal to `T`.

## Target Event

V1 target event:

- Binance spot listing announcement.

Future target events:

- Binance futures listing.
- Binance Launchpool/HODLer Airdrop/Alpha.
- Coinbase listing.
- Upbit/Bithumb listing.
- OKX/Bybit listing.
- Major social hype breakout.

## Time Windows

Evaluate signals over multiple forward windows:

- 7 days
- 14 days
- 30 days
- 60 days
- 90 days

Wallet behavior windows before date `T`:

- 7 days
- 14 days
- 30 days
- 60 days
- 180 days
- 365 days

## Dataset Construction

For each historical date `T`:

1. Build the token universe visible at `T`.
2. Exclude tokens that fail minimum quality filters at `T`.
3. Compute wallet scores using only pre-`T` history.
4. Detect accumulation events up to `T`.
5. Compute token, liquidity, risk, and social features as of `T`.
6. Generate candidate signals.
7. Later evaluate whether each candidate received a Binance listing in the selected forward window.

### Research Validity V2

The production research row is one `chain + token + weekly snapshot`. The universe
is built before outcomes are joined. A Binance event remains in
`research_event_coverage` even when no pre-announcement DEX flow is found; this is a
real zero, not a row to discard.

For every eligible snapshot the dataset stores:

- listing within 30, 60, and 90 days;
- return after 7, 30, 60, and 90 days;
- 30/90-day maximum favorable excursion and drawdown;
- activity collapse, observed liquidity collapse when available, and an explicitly
  marked activity-based liquidity proxy otherwise;
- a 30-day rug proxy and label-maturity timestamp.

Immature horizons remain `NULL`. They must never be converted to negatives.

## Wallet Score

Wallet score should be point-in-time.

Potential features:

- Number of historical pre-Binance hits before `T`.
- Median lead time before listing.
- Realized and unrealized performance after listing.
- Average position size.
- Holding time.
- Win rate across listing-like events.
- Recency of success.
- Diversity of successful tokens.
- Whether wallet appears connected to CEX, deployer, MEV, market maker, or team wallets.
- Whether wallet acts independently or as part of a controlled cluster.

One lucky trade must not make a wallet "smart."

The V2 denominator contains every qualifying token bought by each selected wallet,
not only future Binance winners. It records buys, sells, misses, turnover, position
size relative to observed wallet flow, mark-to-market PnL, drawdown, holding time,
and exit quality. The 90-day hit rate uses a Beta-Binomial posterior with a global
base-rate prior and a 95% credible interval. A wallet cannot receive an institutional
label from one lucky hit.

## Cluster Score

The system should prefer independent clusters over raw wallet count.

Potential cluster links:

- Shared funder.
- Repeated same-block or same-route activity.
- Common CEX deposit/withdrawal patterns.
- Reused intermediary wallets.
- Similar trade timing across many tokens.
- Shared deployer/team interactions.

Signals should distinguish:

- 20 wallets from one likely entity.
- 5 independent clusters with strong historical records.

The second is usually more interesting.

## Token Filters

Minimum V1 filters:

- Has website.
- Has DEX liquidity above configured threshold.
- Contract is verified when applicable.
- No obvious honeypot.
- No extreme buy/sell tax.
- Liquidity is not obviously fake.
- Top-holder concentration is not extreme.
- Deployer/team activity is not obviously predatory.

## Metrics

Primary metrics:

- Precision@10.
- Precision@50.
- Recall against Binance listings.
- Lead time distribution.
- False positive rate.
- Signal-to-listing conversion by horizon.

Trading realism metrics:

- Entry liquidity.
- Estimated slippage.
- Maximum drawdown after signal.
- Maximum favorable excursion.
- Post-signal return after 1d, 7d, 14d, 30d, 60d, 90d.
- Whether position could be exited before/after listing.

Signal quality metrics:

- Social silence at signal time.
- Number of strong wallets.
- Number of independent clusters.
- Net buy amount relative to liquidity.
- Historical similarity score.
- Risk score at signal time.

## Negative Examples

The dataset must include tokens that looked promising but did not get listed or turned into bad trades.

False positives are valuable. They teach the model what to avoid.

## Backtest Output

Every backtest run should produce:

- Config used.
- Data freshness bounds.
- Candidate signals.
- Final labels.
- Metrics.
- Worst false positives.
- Best true positives.
- Feature importance or diagnostic analysis.
- Notes on data gaps.

## Acceptance Standard

Do not trust live alerts until the backtest shows repeatable edge across multiple historical periods and market regimes.

No tabular model is fitted before both gates pass:

- at least 100,000 mature token-time snapshots;
- at least 100 independent listing events.

The first models are regularized logistic regression and gradient boosting with a
strict chronological holdout and a 90-day embargo. Their probabilities are also
evaluated as a ranking per snapshot. Survival/ranking models follow only after this
baseline is stable. LLM output is never a training feature or prediction engine.

Model readiness additionally requires at least 20 independent positive events and
12 dates in validation, a 95% snapshot-block bootstrap lower bound above 1.0 for
top-five lift, and a Brier score better than the constant base-rate forecast.

Capital remains blocked after model readiness until identity coverage reaches 95%
and the shadow ledger contains at least 100 signals, 1,000 marks, and 60 days of
history. Automatic execution is a separate decision and is disabled by default.
