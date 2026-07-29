# Pacifica, Nado, Variational Verification

Evidence checked at: 2026-07-29T11:05:58Z

This document records only official-source conclusions used by the funding
runtime pass. It is intentionally fail-closed: data ingestion does not imply
shadow candidate, paper, or live eligibility.

## Pacifica

| Field | Value |
| --- | --- |
| venue | `pacifica` |
| environment | mainnet |
| official base URL | `https://api.pacifica.fi/api/v1` |
| endpoints | `GET /info`, `GET /info/prices`, `GET /book?symbol=...`, `GET /funding_rate/history`, `GET /info/fees`, account `GET /funding/history`, account `GET /positions` |
| endpoint purpose | catalog/specs; current prices/funding/mark/oracle/OI/volume; real orderbook depth; public funding history; public fee tiers; account funding/positions for future canary |
| execution model | `CLOB` |
| funding accrual model | hourly funding epoch; treated as `POSITION_AT_EVENT_FULL` only for diagnostics |
| position inclusion rule | open position during hourly epoch |
| position inclusion rule verified | no, requires reviewed mainnet canary |
| duration-dependent | not proven |
| rate calculation period | hourly funding epoch |
| displayed rate period | 1 hour |
| actual settlement period | 1 hour |
| dynamic interval | no evidence of dynamic interval |
| next settlement source | derived next UTC hour from hourly docs and `info/prices.timestamp`; no explicit `next_funding_at` endpoint found |
| raw rate units | decimal fraction per hourly epoch |
| raw integer scale | not integer-scaled |
| normalized sign convention | positive means longs pay shorts |
| normalized rate per settlement | `next_funding` from `/info/prices` or `next_funding_rate` from `/info` |
| funding history source | public `GET /funding_rate/history`; account `GET /funding/history` |
| cumulative funding source | not found |
| public settlement confirmation | public funding-rate history record only, not local wall-clock boundary |
| account settlement confirmation | account funding history |
| mark price source | `/info/prices.mark` |
| oracle/index source | `/info/prices.oracle` |
| open interest source | `/info/prices.open_interest` |
| volume source | `/info/prices.volume_24h` |
| orderbook source | `/book?symbol=...` |
| fee source | `/info/fees` public fee levels |
| fee scope | public, not account-specific |
| source_event timestamp semantics | `/info/prices.timestamp` in milliseconds |
| response timestamp semantics | local post-response timestamp when added by runtime |
| timing uncertainty | unknown until boundary observation/canary |
| environment identity source | `VenueEndpointIdentity` for `https://api.pacifica.fi/api/v1` |
| verification level | `MAINNET_CANARY_REQUIRED` |
| data eligibility | true |
| strategy eligibility | diagnostic observation only |
| shadow candidate eligibility | false |
| paper eligibility | false |
| live eligibility | false |
| official evidence links | https://docs.pacifica.fi/api-documentation/api/rest-api/markets/get-market-info; https://docs.pacifica.fi/api-documentation/api/rest-api/markets/get-prices; https://docs.pacifica.fi/api-documentation/api/rest-api/markets/get-orderbook; https://docs.pacifica.fi/api-documentation/api/rest-api/markets/get-historical-funding; https://docs.pacifica.fi/api-documentation/api/rest-api/markets/get-fee-levels; https://docs.pacifica.fi/trading-on-pacifica/funding-rates |
| remaining blockers | reviewed mainnet canary, exact inclusion proof, account-specific fee tier for paper/live |

## Nado

| Field | Value |
| --- | --- |
| venue | `nado` |
| environment | mainnet |
| official base URL | Gateway V2 `https://gateway.prod.nado.xyz/v2`; Archive V2 `https://archive.prod.nado.xyz/v2`; Archive V1 `https://archive.prod.nado.xyz/v1` |
| endpoints | Gateway `GET /pairs?market=perp`, `GET /orderbook`; Archive V2 `GET /contracts`; Archive V1 POST `funding_rates`, POST `funding_rate_history` |
| endpoint purpose | catalog; CLOB depth; contract prices/OI/volume/next funding timestamp; latest predicted funding x18; realized hourly funding history |
| execution model | `CLOB` |
| funding accrual model | `PERIODIC_INDEX_STEP` for diagnostics |
| position inclusion rule | open position during hourly funding step, unverified |
| position inclusion rule verified | no |
| duration-dependent | not proven for funding capture; PnL settlement docs separately describe continuous balance realization |
| rate calculation period | crypto formula uses 8h internally; API latest rate is 24h equivalent |
| displayed rate period | 24h predicted/current API rate |
| actual settlement period | 1h |
| dynamic interval | no evidence for crypto hourly funding |
| next settlement source | `contracts.next_funding_rate_timestamp` |
| raw rate units | `funding_rate_x18` |
| raw integer scale | `1e18` |
| normalized sign convention | positive means longs pay shorts |
| normalized rate per settlement | `Decimal(funding_rate_x18) / 1e18 / 24` |
| funding history source | Archive POST `funding_rate_history`; realized hourly x18 rates |
| cumulative funding source | not used for capture |
| public settlement confirmation | Archive funding-rate-history hourly tick |
| account settlement confirmation | Archive interest/funding payments endpoint, not implemented for default tests |
| mark price source | Archive V2 contracts `mark_price` |
| oracle/index source | Archive V2 contracts `index_price` |
| open interest source | Archive V2 contracts `open_interest_usd` |
| volume source | Archive V2 contracts `quote_volume` |
| orderbook source | Gateway V2 orderbook |
| fee source | account fee-rates query exists; no safe public/account-independent fee for default adapter |
| fee scope | unknown/account-specific |
| source_event timestamp semantics | funding endpoint `update_time` seconds; history `timestamp` seconds |
| response timestamp semantics | local post-response timestamp when added by runtime |
| timing uncertainty | unknown until canary |
| environment identity source | `VenueEndpointIdentity` for Gateway V2 |
| verification level | `MAINNET_CANARY_REQUIRED` |
| data eligibility | true |
| strategy eligibility | diagnostic observation only |
| shadow candidate eligibility | false |
| paper eligibility | false |
| live eligibility | false |
| official evidence links | https://docs.nado.xyz/developer-resources/api/endpoints; https://docs.nado.xyz/core/funding-rates; https://docs.nado.xyz/developer-resources/api/v2/contracts; https://docs.nado.xyz/developer-resources/api/archive-indexer/funding-rate; https://docs.nado.xyz/developer-resources/api/archive-indexer/funding-rate-history; https://docs.nado.xyz/developer-resources/api/v2/orderbook |
| remaining blockers | position inclusion proof, fee source/tier, account funding payment parser/canary, reviewed promotion |

## Variational

| Field | Value |
| --- | --- |
| venue | `variational` |
| environment | mainnet |
| official base URL | `https://omni-client-api.prod.ap-northeast-1.variational.io` |
| endpoint | public metadata/stats adapter endpoint; RFQ docs for execution model |
| endpoint purpose | research ingestion of market/funding metadata only |
| execution model | `RFQ` |
| funding accrual model | `UNKNOWN` |
| position inclusion rule | unknown |
| position inclusion rule verified | no |
| duration-dependent | unknown |
| rate calculation period | variable 1h-8h documented; exact API unit not changed in this pass |
| displayed rate period | interval-specific |
| actual settlement period | variable, unverified for capture |
| dynamic interval | yes |
| next settlement source | derived interval boundary in adapter for diagnostics only |
| raw rate units | API funding field, unit not promoted beyond current adapter assumption |
| raw integer scale | not applicable |
| normalized sign convention | not promoted for strategy |
| normalized rate per settlement | research-only adapter value; not candidate math |
| funding history source | not implemented |
| cumulative funding source | not found |
| public settlement confirmation | not found |
| account settlement confirmation | not implemented |
| mark price source | metadata/stats mark |
| oracle/index source | not independently verified |
| open interest source | metadata/stats open_interest |
| volume source | metadata/stats volume_24h |
| orderbook source | none; synthetic orderbook removed |
| fee source | missing; no verified zero-fee source used |
| fee scope | unknown |
| source_event timestamp semantics | unavailable |
| response timestamp semantics | local post-response timestamp when added by runtime |
| timing uncertainty | unknown |
| environment identity source | `VenueEndpointIdentity` for `https://omni-client-api.prod.ap-northeast-1.variational.io`; not promoted to paper/live |
| verification level | `UNVERIFIED` |
| data eligibility | true for research ingestion |
| strategy eligibility | false |
| shadow candidate eligibility | false |
| paper eligibility | false |
| live eligibility | false |
| official evidence links | https://docs.variational.io/developer/api; https://docs.variational.io/developer/rfq |
| remaining blockers | RFQ execution layer, funding mechanics proof, fee model, canary, reviewed approval |
