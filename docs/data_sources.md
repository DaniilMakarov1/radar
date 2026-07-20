# Data Sources

## Philosophy

Start with low-cost and free-tier sources. Keep the architecture compatible with paid data providers later.

Every source should be wrapped behind an internal adapter so it can be replaced without changing scoring logic.

## Initial Sources

### Binance Listings

Purpose:

- Build the target-event history.
- Track new listing announcements.

Data needed:

- Announcement title.
- Announcement URL.
- Announcement timestamp.
- Token symbol.
- Trading pairs.
- Spot/futures/other category.
- Trading start time when available.

### Dune

Purpose:

- Historical DEX trades.
- Cross-chain research.
- Backtest datasets.

Networks:

- Base.
- Ethereum.
- BNB Chain.
- Solana.

Notes:

- Use partition filters and time windows to avoid expensive queries.
- Prefer curated DEX tables for the first implementation.

### DexScreener

Purpose:

- Pair discovery.
- Liquidity snapshots.
- Volume snapshots.
- Price context.
- Website/social links when available.

Use cases:

- Check whether a token has a website.
- Estimate whether liquidity is sufficient.
- Track active pairs on Base first.

### Blockscout and Explorer APIs

Purpose:

- Contract metadata.
- Token transfers.
- Verified source status.
- Address-level history.

Current Base implementation:

- Public per-instance Blockscout REST API at `base.blockscout.com/api/v2`.
- Contract verification, scam reputation, proxy type and implementation addresses.
- Holder count as an independent cross-check against GoPlus.
- No Blockscout PRO key is required for this Base MVP path.

Networks later:

- Base: BaseScan.
- Ethereum: Etherscan.
- BNB Chain: BscScan.
- Other EVM networks later: Arbiscan, Optimistic Etherscan, PolygonScan, Snowtrace, etc.

Notes:

- Treat these as chain-specific explorer adapters, not as one generic Etherscan dependency.
- Some APIs share similar patterns, but rate limits, endpoint coverage, and labels can differ.
- Etherscan-compatible PRO APIs remain replaceable adapters rather than a scoring dependency.

### Solana Explorers and RPC Providers

Purpose:

- Solana token metadata.
- Wallet and token-account history.
- Transaction details.
- Program interaction history.

Potential sources:

- Solscan.
- SolanaFM.
- Helius.
- Birdeye.

Notes:

- Solana should be implemented as a separate non-EVM module.
- Do not try to force Solana into the EVM transaction model.

### Moralis

Purpose:

- Current Base top-holder distribution split into EOA, contract and labeled-entity shares.
- EVM wallet and token data later.
- Potential real-time streams/webhooks later.

Notes:

- Realized wallet PnL is not used until provider output is validated against raw swaps.
- Provider-derived analytics must not become a hard feature without point-in-time backtests.

### Envio HyperSync

Purpose:

- Fast Base event backfills and a future low-latency trigger layer.
- Independent direct ERC-20 transfer confirmation for tracked wallets.

Notes:

- Direct-transfer absence does not invalidate Dune swap rows because routed swaps can
  use intermediate contracts.
- HyperSync evidence is currently informational and is not a qualification gate.

### Token Security Sources

Potential sources:

- GoPlus.
- TokenSniffer.
- De.Fi.

Purpose:

- Honeypot checks.
- Buy/sell tax checks.
- Contract risk.
- Holder concentration.
- Ownership/admin risk.
- Liquidity lock or suspicious liquidity.

### Social Sources

X is disabled in live scoring. Research Validity V2 uses an X-independent
`Attention Gap` and keeps unavailable providers as missing data rather than zero.

Current free inputs:

- DexScreener boosts, ads, and profile orders;
- GDELT news counts for 24 hours and seven days;
- GitHub commit and contributor activity discovered from project links;
- optional Farcaster search through Neynar when a key is configured;
- weekly trader, volume, wallet, and net-flow acceleration from on-chain history.

The score compares recent on-chain acceleration with public-attention acceleration.
It is not a sentiment classifier.

Initial low-cost options:

- X/Twitter search where accessible.
- Project website metadata.
- DexScreener social links.
- Manual source lists.

Potential paid/future:

- Kaito.
- LunarCrush.
- Cookie.fun.
- Social listening providers.

Purpose:

- Measure whether on-chain accumulation happens before social hype.

### Prediction Markets

Current free sources:

- Polymarket Gamma API for events, outcomes, fees, and resolution text;
- Polymarket CLOB batch books for full YES/NO depth;
- Polymarket Data API for public leaderboard and closed wallet positions;
- Kalshi public REST for events, rules, markets, and batch orderbooks.

No trading credentials are required. Kalshi orderbooks expose YES and NO bids;
the adapter derives the corresponding asks through binary complementarity.

The normalized contract stores deadlines, settlement sources, cancellation rules,
mutual exclusivity, exhaustiveness, negative-risk flags, fee policy, and raw source
payload. Kalshi taker calculations use the current general `0.07 * C * p * (1-p)`
schedule. The conservative maker model uses `0.0175` unless a future per-market
fee adapter proves a lower value.

Prediction-market prices are also alternative data for Smart Money Radar through
`prediction_event_token_links`. They are supporting evidence, never a direct token
buy instruction.

## Data Source Priority

### Phase 1

- Binance announcements.
- Dune historical DEX trades.
- DexScreener token/pair data.
- Basic EVM metadata.

### Phase 2

- Token security APIs.
- Moralis streams for Base.
- Better wallet-level history.
- Social snapshots.

### Phase 3

- Solana-specific sources such as Helius/Birdeye.
- Better wallet/entity labels.
- Paid intelligence sources if the free stack proves the concept.

## API Keys

Current local implementation supports:

- Dune API key.
- Moralis API key.
- HyperSync API token.
- Public Base Blockscout without a key.
- Optional GitHub token for higher API limits.
- Optional Neynar key for Farcaster coverage.

Likely needed later:

- Etherscan or Blockscout PRO key if public explorer limits become insufficient.
- BscScan-compatible access for BNB Chain only if the selected multichain provider lacks coverage.
- Optional GoPlus/TokenSniffer/De.Fi access.
- Optional Solscan/Helius/Birdeye access.
- Optional X API access only when the social gate is deliberately restored.
- Optional Telegram bot token and chat ID.

Telegram is not critical for the first research build. It becomes useful once signal generation works.
