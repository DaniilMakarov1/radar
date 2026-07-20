# How It Works

## Simple Mental Model

Smart Money Radar has five jobs:

1. Collect facts.
2. Store facts.
3. Clean and classify facts.
4. Score patterns.
5. Show the result in a dashboard or alert.

The LLM is not the always-on scanner. Code does the scanning. The LLM becomes useful after a candidate signal exists, because it can explain the evidence, risks, and historical analogs in plain language.

## Current Working Parts

### Binance Announcement Collector

The collector calls Binance's public announcement API and saves:

- announcement id;
- article code;
- title;
- release time;
- source URL;
- raw JSON;
- article body when details are available;
- explorer links such as BaseScan, BscScan, Etherscan, or Solscan links.

The collector also makes a first-pass classification:

- `spot`
- `futures`
- `tokenized_stock`
- `margin`
- `product_bundle`
- `alpha_or_airdrop`
- `unknown`

This matters because Binance puts many different announcement types into the same "New Cryptocurrency Listing" catalog. We do not want bStocks, margin updates, or futures launches polluting the spot listing ground truth.

### Local Database

The current database is SQLite at:

```text
data/radar.sqlite
```

SQLite is enough for the first research phase. Later, Postgres or ClickHouse can replace it without changing the product logic too much.

### Dashboard

The dashboard reads the SQLite database and exposes:

- current qualified Radar signals and their stage;
- complete wallet addresses, evidence, entities, and clusters;
- Research Validity dataset gates, model runs, and Bayesian wallet scores;
- chronological backtest readiness and Base target coverage.

The dashboard refreshes from local API endpoints every 30 seconds.

### Status And Reports

The fastest way to see whether the project is working is:

```bash
python3 -m smart_money_radar.cli status
```

For a fuller local artifact:

```bash
python3 -m smart_money_radar.cli report
```

This writes:

```text
reports/latest.md
reports/latest.html
```

These reports are intentionally more useful than pretty: they show counts, blockers, API key status, Base-ready targets, and the next practical step.

## How Blockchain Analysis Works

The current pipeline already stores Base historical buyers, holding behavior, wallet
scores, clusters, and near-live observations. Research Validity V2 adds the complete
negative universe and wallet misses needed to test whether that history is real edge.

### Step 1: Build The Target List

First, we need to know which tokens Binance listed and when. This becomes the target history.

Example:

```text
Token XYZ was announced by Binance on 2025-04-10.
```

Now we can look backward and ask:

```text
Who bought XYZ before 2025-04-10?
```

### Step 2: Pull Historical DEX Trades

For Base first, we will use Dune and DexScreener:

- Dune gives historical DEX trades.
- DexScreener helps discover pairs, liquidity, prices, and websites.
- BaseScan helps verify contracts and inspect Base-specific details.

Later:

- BNB Chain uses BscScan.
- Ethereum uses Etherscan.
- Solana uses Solscan, Helius, Birdeye, or SolanaFM.

### Step 3: Identify Early Buyers

For every future Binance-listed token, the system checks who bought before the announcement:

```text
Wallet A bought 30 days before listing.
Wallet B bought 12 days before listing.
Wallet C bought 2 days before listing.
```

Then it asks:

```text
Have these wallets done this before?
Did they hold?
Did they profit?
Were they early by luck or repeatedly early?
```

### Step 4: Score Wallets

A wallet becomes interesting only if it has repeatable behavior.

Good signs:

- bought multiple future Binance-listed tokens before listing;
- bought before public hype;
- entered early enough to matter;
- held through meaningful upside;
- was not obviously a CEX, deployer, MEV bot, or team wallet.

Bad signs:

- one lucky trade;
- suspicious deployer links;
- constant bot-like flipping;
- wallets that are all controlled by the same entity.

The provisional wallet score already separates meaningful accumulation from dust and
flipping across the imported Base targets. The V2 score goes further: it cannot call
a wallet proven smart money until its complete token denominator, misses, PnL,
drawdown, turnover, relative sizing, and exit quality are available.

Provisional wallet outputs:

- `interest_score`: how interesting the wallet looks from timing, size, and early entry.
- `noise_score`: how bot-like or noisy it looks from excessive trade counts and tiny average trades.
- `confidence_score`: how much we can trust the signal with current evidence.
- `label`: `watch_candidate`, `weak_candidate`, `likely_noise`, or `ignore`.

### Step 5: Detect New Accumulation

When live monitoring starts, the system watches Base first.

It looks for patterns like:

```text
12 strong wallets bought the same token.
They bought within 48 hours.
They belong to 5 independent clusters.
The token has a website.
Liquidity is acceptable.
        Public attention is accelerating more slowly than on-chain flow.
Contract risk is acceptable.
```

That becomes a candidate signal.

### Step 6: Score The Token

The token signal score combines:

- wallet quality;
- cluster independence;
- net buy amount;
- liquidity;
- contract safety;
- holder concentration;
- website/social presence;
- social silence;
- similarity to historical winners.

### Step 7: Produce A Signal

The system sends only candidates that pass the signal gate.

A later Telegram alert can use the same stored evidence:

```text
TOKEN: XYZ
Chain: Base
Signal: High interest
Confidence: 74/100

Why it matters:
14 tracked wallets bought in the last 36 hours.
5 independent clusters involved.
Similar pattern appeared before 3 past Binance listings.
  Attention Gap is positive across available non-X sources.

Risks:
Liquidity is thin.
Top holders are concentrated.
Contract is new.
```

## Why This Is Not A Constant LLM Agent

An always-running LLM would be expensive and less reliable.

The better design:

- code watches the market continuously;
- code computes scores cheaply;
- code filters low-quality noise;
- LLM writes analysis only when something passes the filter.

This keeps cost down and makes every signal auditable.
