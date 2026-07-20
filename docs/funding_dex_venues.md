# Funding DEX Venues

This note defines how DEX-style perpetual venues should enter Funding Radar.
The goal is to avoid false candidates caused by treating very different
execution models as if they were the same venue type.

## Venue Classes

### CLOB or DLOB perpetuals

These venues expose a real orderbook or distributed limit-order book. They can
fit the existing route model when the adapter provides:

- current funding estimate or current period rate;
- funding interval and next settlement/checkpoint time;
- settled funding history;
- executable bid and ask depth;
- fee schedule or conservative public default.

Currently covered or partially covered:

- Hyperliquid;
- dYdX;
- Paradex;
- Lighter;
- Backpack;
- Vertex Base;
- Drift/Velocity through fresh Velocity stats plus public DLOB depth;
- Extended on Starknet through public markets, orderbook, hourly funding
  history, and flat public fees;
- Ethereal through public product config, projected one-hour funding,
  market-price, market-liquidity, and one-hour funding history.

### Pool or AMM perpetuals

These venues do not have the same depth semantics as a CEX book. Capacity and
cost come from pool state, utilization, oracle pricing, trade-impact formulas,
borrow/funding-fee state, caps, and gas. They need a separate route model.

Priority venues:

- Jupiter Perps on Solana;
- GMX v2 on Arbitrum and Avalanche;
- Gains Network;
- Synthetix perps;
- Vertex/EdgeX/Bluefin/SynFutures only after their public depth, funding, and
  fee surfaces are verified.

## Build Priority

1. Keep Vertex Base in the CLOB scanner, but fail closed when its public
   endpoint is unreachable. Its SDK documents product symbols, orderbook
   liquidity, latest prices, funding rates, historical snapshots, and fee rates,
   so it fits the existing scanner better than a pool-perps venue.
2. Add Jupiter as the first Solana pool-perps research adapter. Keep it out of
   live candidates until pool impact, borrow fees, and Solana transaction costs
   are modeled.
3. Add GMX v2 as the first EVM pool-perps adapter. GMX has no normal book-depth
   constraint; price impact and caps come from open-interest and pool imbalance,
   so it should validate the same `pool_perp` model as Jupiter.
4. Add Gains/Synthetix only after the first pool-perps implementation proves
   that the dashboard can compare DEX carry without CLOB assumptions.

## Extended Decision

Extended fits the existing CLOB scanner because its public API exposes markets,
market statistics, order books, and funding-rate history from one documented
surface:

- https://api.docs.extended.exchange/
- https://docs.extended.exchange/extended-resources/trading/trading-fees-and-rebates

Implementation decision:

- include only active crypto `PERPETUAL` markets;
- exclude RFQ, off-hours, spot, and TradFi markets;
- treat the current `marketStats.fundingRate` as an hourly current funding
  estimate;
- treat funding history `data[].f` as settled hourly funding;
- use flat public fees: taker 0.025%, maker 0%;
- fail closed on missing depth or malformed market statistics.

## Ethereal Decision

Ethereal fits the existing CLOB scanner because its official HTTP API exposes
product configuration, prices, projected funding, market liquidity, and funding
history:

- https://docs.ethereal.trade/protocol-reference/api-hosts
- https://docs.ethereal.trade/developer-guides/trading-api/products
- https://docs.ethereal.trade/developer-guides/trading-api/funding-rates

Implementation decision:

- include active USD perpetual products only;
- use `fundingRateProjected1h` as the current one-hour funding nowcast and
  settled `fundingRate1h` rows as history;
- use best bid/ask mid from `market-price` as the route mark and oracle price as
  index;
- use `market-liquidity` as executable depth, not synthetic pool capacity;
- use product-level public maker/taker fees;
- fail closed on missing product id, price, funding, or depth.

## Jupiter Perps Decision

Jupiter is strategically interesting because Solana execution is cheap and funds
stay in the user's wallet until position actions are signed. It should not be
added as a normal Funding Radar adapter yet.

Reasons:

- Jupiter's public Perps docs mark the API as work in progress:
  https://developers.jup.ag/docs/perps
- Pool data is represented by on-chain pool/custody accounts rather than a CLOB
  orderbook:
  https://developers.jup.ag/docs/perps/pool-account
  https://developers.jup.ag/docs/perps/custody-account
- Jupiter Perps charges fixed opening/increasing and closing/decreasing fees,
  and custody state includes hourly borrow-fee fields. Those costs must be
  modeled explicitly before route economics are comparable with CEX funding.

Implementation requirement:

1. Add a `pool_perp` execution model beside the current `clob_perp` model.
2. Pull pool/custody state from Solana RPC or an official stable API.
3. Convert custody borrow/funding state into hourly cash-flow estimates.
4. Simulate position size against max position, available long/short capacity,
   utilization, oracle price, trade impact, open/close fees, and gas.
5. Show Jupiter routes in a separate DEX panel until the economics are validated
   with paper observations.

## GMX v2 Pool-Perp Decision

GMX v2 is strategically useful, but it also cannot be inserted into the CLOB
scanner as a fake orderbook. GMX docs explicitly describe perps arbitrage through
price impact, open-interest imbalance, and funding fees, and note that adaptive
funding can decay as the market becomes balanced:

- https://docs.gmx.io/docs/trading/fees/
- https://github.com/gmx-io/gmx-synthetics

Implementation requirement:

1. Read market/pool state from the official contracts, subgraph/indexer, or a
   verified near-live endpoint.
2. Model open and close price impact from the change in long/short imbalance.
3. Model funding, borrow fees, open/close fees, execution gas, keeper/network
   buffers, and collateral token effects.
4. Simulate $500, $1,000, and $5,000 order sizes against caps and impact limits.
5. Keep GMX routes in a `pool_perp` research panel until quotes and paper
   outcomes match the model within a tight tolerance.

If quote parity is not available, GMX should remain research-only. A wrong pool
impact model is more dangerous than not showing the route.

## Pool-Perp Normalized Snapshot

The `pool_perp` adapter family should emit snapshots shaped like this before any
route is compared with CEX or CLOB venues:

```text
pool_perp_market_snapshots(
  venue, chain, market, index_asset, collateral_asset, side,
  mark_price, oracle_price,
  funding_rate_hourly, borrow_rate_hourly,
  open_interest_long_usd, open_interest_short_usd,
  pool_long_usd, pool_short_usd,
  available_long_usd, available_short_usd,
  max_position_usd,
  price_impact_bps_at_500,
  price_impact_bps_at_1000,
  price_impact_bps_at_5000,
  open_fee_rate, close_fee_rate,
  gas_estimate_usd, priority_or_keeper_fee_usd,
  observed_at, raw_json
)
```

Route economics should then use:

```text
carry = funding_cashflow - borrow_cashflow
execution_cost = open_fee + close_fee + price_impact + gas + oracle/slippage reserve
capacity = min(available_side_capacity, max_position, impact_threshold_capacity)
net = carry - execution_cost
```

Validation gate:

- paper/research only until app/contract quotes match model output for several
  sizes and both long/short directions;
- no live candidate unless funding, borrow, impact, fees, gas, and capacity are
  all present from the same observation window;
- if a source is stale, missing, or inconsistent, the route must be hidden rather
  than patched with synthetic data.

## Drift/Velocity Decision

The legacy Drift Data API host still responds, but its live funding rows were
stale in the July 2026 check. The adapter now uses fresh Velocity market stats
for current funding and the public DLOB endpoint for executable depth:

- https://docs.velocity.exchange/developers/data-api
- https://docs.velocity.exchange/protocol/trading/funding-rates
- https://docs.velocity.exchange/protocol/trading/trading-fees
- https://dlob.velocity.exchange

Decision:

- Do not loosen the stale-data gate.
- Treat Velocity `fundingRate.long/short` as side cashflow and normalize it to
  the scanner's canonical CEX sign convention.
- Use DLOB levels for depth; do not synthesize executable depth from
  AMM/oracle/open-interest fields.
- Keep the legacy Drift Data API as history fallback only.

## Risk Shift Compared With CEX

DEX venues reduce custodial risk, but they add other risks:

- smart-contract and admin-key risk;
- oracle and confidence-interval risk;
- RPC/indexer lag;
- gas and priority-fee spikes;
- transaction simulation/finalization delay;
- partial execution and liquidation during transaction latency;
- bridge/collateral fragmentation.

These risks should be visible in the dashboard as route flags, not hidden inside
one generic fee number.
