# Product Spec

## Goal

Build a high-quality on-chain screener that detects early accumulation by historically strong wallets and wallet clusters before public hype, with Binance listing alpha as the first measurable objective.

The first version should produce watchlist-grade signals, not automatic trade execution.

## V1 Scope

### Exchange Target

- Binance only.
- Track spot listings first.
- Later add futures listings, Launchpool, HODLer Airdrops, Alpha, Coinbase, Upbit, OKX, and Bybit.

### Networks

- Live MVP: Base.
- Research and historical comparison from the start:
  - Base
  - Solana
  - BNB Chain
  - Ethereum

Ethereum should be used heavily for labels, history, and high-conviction watchlists, but not as the primary execution/testing network because trading costs are higher.

### Signal Horizons

- Early signal: 14-90 days before a potential listing or public hype.
- Warm signal: 1-14 days before a potential listing or public hype.
- Excluded in V1: intraday scalping signals.

### Token Universe

Allowed:

- Memecoins with sufficient liquidity and clean risk profile.
- Microcaps with a real website and visible project footprint.
- Infrastructure, AI, DePIN, DeFi, gaming, social, RWA, consumer, and other narrative tokens.

Excluded in V1:

- Tokens with no website.
- Honeypots.
- Extreme buy/sell tax tokens.
- Obvious rugs.
- Tokens with fake or unusable liquidity.
- Tokens dominated by suspicious deployer/team wallets.

## Signal Definition

A candidate signal is a token-level event where historically strong wallets or independent wallet clusters accumulate the same token within a meaningful window while social/public attention remains relatively low.

The system should prefer:

- Independent clusters over many addresses controlled by one entity.
- Repeatable historical behavior over one lucky trade.
- Net accumulation over short-term flipping.
- Clean liquidity and contract risk over pure momentum.
- Low social noise before high social noise.

## Output Format

Each signal should eventually include:

- Token name, symbol, chain, contract, website.
- Signal level: low, medium, high, critical watch.
- Confidence score from 0 to 100.
- Time window of detected accumulation.
- Number of tracked wallets involved.
- Number of independent clusters involved.
- Net buy amount in USD.
- Liquidity and volume context.
- Wallet score summary.
- Historical analogs.
- Social silence score.
- Risk flags.
- Bull case.
- Bear case.
- What would confirm the signal.
- What would invalidate the signal.

## Product Philosophy

This is not a hype bot and not a copy-trading bot. It is an evidence engine.

The system should be conservative by default. Missing a noisy low-quality pump is acceptable. Repeatedly flagging scams is not.

