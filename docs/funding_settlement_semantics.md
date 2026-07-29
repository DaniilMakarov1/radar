# Funding Settlement Semantics

Checked: 2026-07-29

This document is the machine-readable contract inventory companion for
`smart_money_radar.funding.settlement_contracts`. The settlement-capture
strategy may create `SHADOW_CANDIDATE` only when the registry and current market
row prove an eligible accrual model, verified position inclusion, matching
environment, fresh timestamps, and positive conservative net after all reserves.

Allowed accrual models for this strategy:

- `POSITION_AT_EVENT_FULL`
- `PERIODIC_INDEX_STEP`

Excluded:

- `CONTINUOUS_PRO_RATA`
- `UNKNOWN`

| venue | environment | endpoint / evidence | accrual model | position inclusion rule | verified | settlement interval | displayed rate period | dynamic interval | timing uncertainty | realized confirmation source | strategy eligibility | implementation notes |
|---|---|---|---|---|---|---:|---:|---|---|---|---|---|
| RiseX | testnet | https://www.rise.trade/en, https://testnet.rise.trade/en/trade/BTC-PERP | `UNKNOWN` | `perp_position_at_settlement` hypothesis only | no | public payload currently shows 1h | 8h display metadata observed in adapter | yes | unknown | unavailable until testnet canary | `RESEARCH_ONLY` | Mandatory monitoring venue. Candidate execution remains fail-closed until at least three canary observations support snapshot/full mechanics. |
| RiseX | mainnet | https://www.rise.trade/en | `UNKNOWN` | unknown | no | unknown | unknown | yes | unknown | unavailable | `CAPABILITY_BLOCKED` | No official mainnet API endpoint is proven in this pass; testnet must not be labelled mainnet. |
| Hyperliquid | mainnet | https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding | `PERIODIC_INDEX_STEP` | hourly funding step position exposure | public observed | 1h | 8h formula paid hourly | no | 2s registry reserve | funding history / account ledger | eligible for shadow | Funding paid hourly at one eighth of the 8h computed rate. |
| Paradex | mainnet | https://docs.paradex.trade/risk/funding-mechanism | `CONTINUOUS_PRO_RATA` | continuous funding index delta | yes | continuous index, API history hourly | 8h parameters | no | none for event capture | account funding history | excluded | Do not build settlement-capture opportunities with Paradex on either leg. Blocker: `funding_continuous_pro_rata`. |
| Extended | mainnet | https://docs.extended.exchange/extended-resources/trading/funding-payments | `UNKNOWN` | hourly payment described, snapshot inclusion not proven | no | 1h | 8h realization period | no | unknown | account funding history | `RESEARCH_ONLY` | Public docs say payments are applied hourly, but this pass does not prove full position-at-assessment inclusion. |
| edgeX | mainnet | https://edgex-1.gitbook.io/edgex-documentation/trading/funding-fees | `UNKNOWN` | unknown | no | N-hour formula | N-hour | yes | unknown | unknown | `RESEARCH_ONLY` | Funding formula docs exist; candidate eligibility waits for exact interval and position inclusion proof. |
| Ethereal | mainnet | https://docs.ethereal.trade/trading/perpetual-futures/funding-rates | `UNKNOWN` | unknown | no | market parameter | market parameter | yes | unknown | Trading API / account history | `RESEARCH_ONLY` | Current/historical funding exposed, but position inclusion and timing jitter are not verified. |
| GRVT | mainnet | https://api-docs.grvt.io/market_data_api/ | `UNKNOWN` | unknown | no | instrument-specific funding interval | instrument-specific | yes | unknown | account ledger | `RESEARCH_ONLY` | API exposes funding timestamp and mark price; exact inclusion rule is not proven. |
| Lighter | mainnet | https://docs.lighter.xyz/trading/funding, https://apidocs.lighter.xyz/reference/funding-rates | `PERIODIC_INDEX_STEP` | hourly funding step position exposure | public observed | 1h | 8h equivalent in adapter | no | 2s registry reserve | funding history / account ledger | eligible for shadow | Adapter must keep 8h display rate separate from per-hour cashflow rate. |
| dYdX | mainnet | https://docs.dydx.xyz/concepts/trading/funding | `PERIODIC_INDEX_STEP` | hourly funding step position exposure | public observed | 1h | 1h | no | 2s registry reserve | account ledger | eligible for shadow | Funding sign follows positive rate: longs pay shorts. |
| Binance | mainnet | https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data, https://www.binance.com/en/support/faq/detail/360033525031 | `POSITION_AT_EVENT_FULL` | open position at funding fee assessment | public observed | symbol dependent | symbol dependent | yes | Binance FAQ notes possible timing deviation | income history `FUNDING_FEE` | eligible for shadow | Market row must provide current interval and next funding timestamp; do not hardcode 8h. |
| Bybit | mainnet | https://bybit-exchange.github.io/docs/v5/market/tickers, https://bybit-exchange.github.io/docs/v5/market/history-fund-rate | `POSITION_AT_EVENT_FULL` | open position at funding timestamp | public observed | symbol dependent | symbol dependent | yes | 2s registry reserve | transaction log funding | eligible for shadow | Funding interval is symbol-specific; use instruments-info / market row. |
| OKX | mainnet | https://www.okx.com/docs-v5/en/#public-data-rest-api-get-funding-rate, https://www.okx.com/en-us/help/perps-funding-fee-mechanism | `POSITION_AT_EVENT_FULL` | open position at fee assessment | public observed | default 8h, can be 1h/2h/4h | contract dependent | yes | assessed within milliseconds | account bills | eligible for shadow | OKX explicitly says closing before fee assessment avoids paying/collecting. |

## RiseX Probe Contract

`funding-risex-probe` defaults to public, testnet-only, no orders, no Telegram,
and `data/risex-funding-probe-testnet.sqlite`. Canary mode is blocked unless:

- `--environment testnet`
- `--mode testnet-canary`
- `--confirm-testnet-canary`
- testnet credentials are already present in process environment
- `--max-notional-usd <= 10`
- base URL is explicitly testnet

The probe records public observations and, when a future private testnet canary
client is implemented, will compare:

- expected full funding
- expected pro-rata funding
- realized funding
- balance delta
- observed/full and observed/pro-rata ratios

RiseX must not move above `RESEARCH_ONLY` for candidate eligibility until the
minimum canary evidence threshold is met.
