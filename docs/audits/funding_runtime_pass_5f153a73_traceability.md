# Funding Runtime Pass 5f153a73 Traceability

Evidence checked at UTC: 2026-07-29T09:41:38Z

Starting branch: `review/synchronized-funding-v2-qwen-20260728-075432`

Starting HEAD: `5f153a73cea2ccab7dafb3fd83b3d81c021dbf96`

Baseline command: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`

Baseline result: `493 passed in 8.60s` on Python 3.11.5.

## Preflight

| Check | Result | Evidence/Notes |
| --- | --- | --- |
| Remote | DONE | `origin https://github.com/DaniilMakarov1/radar.git` |
| Fetch | DONE | `git fetch origin` completed successfully. |
| Branch | DONE | `review/synchronized-funding-v2-qwen-20260728-075432` |
| HEAD | DONE | `5f153a73cea2ccab7dafb3fd83b3d81c021dbf96` |
| Origin branch HEAD | DONE | `5f153a73cea2ccab7dafb3fd83b3d81c021dbf96` |
| Working tree | DONE | `git status --short` was empty before changes. |
| Published diff | DONE | `git diff --stat origin/review/synchronized-funding-v2-qwen-20260728-075432..HEAD` was empty. |

## Existing Production Call Graph Inventory

```text
CLI funding-paper-trader
-> smart_money_radar.cli.funding_paper_trader_config
-> smart_money_radar.funding.trader.PaperBot
-> PaperBot.run_loop/run_iteration/run_hot_iteration/run_full_iteration
-> smart_money_radar.funding.service.run_funding_scan or PaperBot._run_lightweight_discovery
-> funding client factory funding_client_for_venue / active_default_funding_clients
-> venue adapters catalog_and_markets / market_snapshot / orderbook
-> normalize_catalog_canonical_units / normalize_orderbook_canonical_units
-> funding market snapshots and routes in SQLiteStore
-> PaperBot.process_synchronized_entry_candidates
-> focused_recheck_route / _refresh_open_capture_leg
-> SynchronizedFundingRuntimeV2.consider_route
-> route_next_settlement / entry_underwriting / initial_entry_economics
-> entry_risk_gates / simulate_marketable_ioc
-> funding_capture_positions / funding_paper_orders / paper_event_ledger
-> refresh_open_capture_route / _open_capture_route_quality
-> mark_settlement_crossed / build_settlement_crossing_rows
-> next_cycle_hold_or_close_decision / close_position
-> process_pending_reconciliations / StoredFundingSettlementDataProvider
```

```text
CLI funding-shadow-monitor
-> shadow_monitor_clients
-> FundingShadowMonitor.run/run_once
-> broad_funding_sweep / focused_route_refresh
-> adapter catalog_and_markets or funding_sweep
-> settlement_contract_from_market / funding_adapter_contract_from_market
-> build_settlement_capture_opportunity
-> funding_shadow_opportunities / funding_shadow_settlement_events / funding_shadow_alerts
```

## Inventory Findings Before Code Changes

| Category | Current paths | Evidence/Notes |
| --- | --- | --- |
| Production PaperBot alignment gates | `smart_money_radar/paper_bot/runtime_v2.py:89`, `:1289`, `:2351`, `smart_money_radar/funding/trader.py:2444`, `:3024`, legacy `smart_money_radar/paper_bot/position.py:99`, `:198` | `route_next_settlement`, lightweight discovery, open refresh quality and legacy position path block on aligned funding timestamps. |
| Shadow-only event planner | `smart_money_radar/funding/strategy_synchronized_funding.py:401`, `smart_money_radar/funding/shadow_monitor.py:794` | `build_settlement_capture_opportunity` is used by shadow. PaperBot does not consume this route plan before opening. |
| Duplicate planning paths | `strategy_synchronized_funding.build_settlement_capture_opportunity`, `runtime_v2.entry_underwriting/_initial_economics`, `trader._build_lightweight_watch_routes`, legacy `paper_bot.position.route_entry_decision` | PaperBot v2 uses observation/economics helpers with synchronized timestamp assumptions instead of the event-window result. |
| Two-event limit | `smart_money_radar/funding/strategy_synchronized_funding.py:520` | Planner creates `exit_after_first_settlement` and optionally `exit_after_second_settlement`; no source-of-truth list beyond two exit points. |
| Float math in critical funding planner | `smart_money_radar/funding/strategy_synchronized_funding.py` | `FundingSettlementEvent` rate/cashflow fields and planner economics use `float`. |
| Trust self-promotion | `smart_money_radar/funding/settlement_contracts.py:433` | Market rows can override `verification_level`, `official_evidence_urls`, `position_inclusion_rule_verified`, timing windows and evidence date. |
| Environment fallback | `smart_money_radar/funding/shadow_monitor.py:960`, `smart_money_radar/funding/trader.py:3024`, adapter constructors | `client_environment(client, fallback)` can use monitor config when actual client identity is missing; most clients lack typed endpoint identity. |
| Source timestamp fabrication | `smart_money_radar/funding/stablecoins.py:96`, `:113`, `trader._fetch_lightweight_market_snapshots`, `runtime_v2._refresh_open_capture_leg` | Stablecoin provider uses `observed_at` for both source and response timestamps; fallback request times can collapse started/received values. |
| Unknown cost fallback | `smart_money_radar/funding/fees.py:52`, `strategy_synchronized_funding._fee_rate`, `_planned_costs` | Missing fees can become public defaults or zero; slippage/reserves default to zero in planner config. |
| Same-asset stablecoin pass | `smart_money_radar/funding/stablecoins.py:239` | Any identical collateral symbol passes before trusted collateral-family verification. |
| Alert claiming | `smart_money_radar/funding/shadow_monitor.py:280`, `smart_money_radar/storage.py:8366` | Alerts use memory dedupe plus `INSERT OR IGNORE`; no persisted CLAIMED/SENT/FAILED_RETRYABLE retry contract. |
| RiseX placeholder | `smart_money_radar/funding/risex_probe.py:291` | Canary mode returns `private_testnet_order_client_not_implemented_in_this_pass`. |
| Pacifica unsafe data | `smart_money_radar/funding/adapters/pacifica.py` | Uses synthetic `next_utc_hour`, no funding history, hardcoded fees, mark/index proxy. |
| Nado missing | `smart_money_radar/funding/adapters/` | No Nado adapter at starting HEAD. |
| Variational synthetic orderbook | `smart_money_radar/funding/adapters/variational.py:123` | RFQ quote tiers are exposed through `orderbook()` as bids/asks. |
| Paradex exclusion | `smart_money_radar/funding/settlement_contracts.py:128`, `shadow_monitor.py:682` | Contract is continuous pro-rata and shadow skips it for strategy pairs; regression coverage must remain. |

## Phase B Evidence

| Area | Result | Evidence/Notes |
| --- | --- | --- |
| Shared planner | DONE | `FundingSettlementPlanner` in `smart_money_radar/funding/strategy_synchronized_funding.py`; `build_settlement_capture_opportunity` is now a wrapper around it. |
| PaperBot planner call | DONE | `SynchronizedFundingRuntimeV2` accepts/invokes planner dependency; `PaperBot.process_synchronized_entry_candidates` computes event-window monitor state from the same planner. Test: `test_paperbot_v2_actually_calls_shared_planner`. |
| Alignment gate removal for v2 runtime | DONE | `runtime_v2.route_next_settlement`, `validate_focused_observation`, `_observation_timestamp_match`, `_record_schedule_probe`, `trader._build_lightweight_watch_routes`, and v2 orchestration no longer use settlement mismatch as a v2 blocker. Legacy `paper_bot.position` still needs Phase C cleanup. |
| ONE_SETTLEMENT PaperBot | DONE | Test: `test_paperbot_v2_accepts_one_settlement_alignment_mismatch`. |
| MULTIPLE_SETTLEMENTS PaperBot | DONE | Test: `test_paperbot_v2_multi_event_survives_first_event_and_closes_after_last`. |
| Three-event planner | DONE | Test: `test_three_settlement_events_are_considered_as_timeline`. |
| Shadow/Paper parity | DONE | Test: `test_shadow_and_paperbot_route_plan_parity`. |
| Focused Phase B commands | DONE | `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_synchronized_funding_v2.py -k 'v2 or settlement_crossed or partial_entry or entry or close or quantity_step or max_position_limit or insufficient_balance or paperbot or alignment or parity' --tb=short` -> `139 passed in 2.81s`; `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_funding_shadow_monitor.py -k 'settlement or event or paradex or shadow_monitor_does_not_mutate_paper_state or three' --tb=short` -> `12 passed, 44 deselected in 0.22s`. |

## Phase C Safety Hardening Evidence

| Gate | Status | Evidence |
| --- | --- | --- |
| Settlement contract self-promotion blocked | DONE | Production file: `smart_money_radar/funding/settlement_contracts.py`. Tests: `test_market_row_cannot_self_promote_settlement_semantics`, `test_unknown_adapter_payload_cannot_self_promote_settlement_contract`. Command: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_funding_shadow_monitor.py -k 'self_promote or risex_payload or stablecoin or stable or usde or collateral or missing_fee or zero_fee or negative_second' --tb=short` -> `19 passed, 48 deselected in 0.07s`. |
| Unknown fee and nonzero reserve fail-closed | DONE_FAIL_CLOSED | Production file: `smart_money_radar/funding/strategy_synchronized_funding.py`. Tests: `test_missing_fee_is_unknown_and_blocks_candidate`, `test_explicit_zero_fee_is_allowed_when_payload_provides_it`. Same focused command -> `19 passed, 48 deselected in 0.07s`. Full typed `CostEstimate` model remains open under COST-01. |
| Same-asset stablecoin trust gate | DONE | Production file: `smart_money_radar/funding/stablecoins.py`. Tests: `test_untrusted_same_asset_stable_route_is_blocked`, `test_trusted_same_asset_stable_route_is_allowed`. Same focused command -> `19 passed, 48 deselected in 0.07s`. |
| Stablecoin response timestamp source handling | DONE | Production file: `smart_money_radar/funding/stablecoins.py`. Test: `test_public_stablecoin_provider_does_not_fabricate_source_timestamp`. Same focused command -> `19 passed, 48 deselected in 0.07s`. |
| Alignment mismatch production blocker removal | DONE | Production files: `smart_money_radar/paper_bot/position.py`, `smart_money_radar/paper_bot/cycle_manager.py`, `smart_money_radar/funding/trader.py`. Test: `test_different_timestamps_are_not_alignment_blockers`. Command: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests/test_synchronized_funding_v2.py -k 'v2 or settlement_crossed or partial_entry or entry or close or quantity_step or max_position_limit or insufficient_balance or paperbot or alignment or parity' --tb=short` -> `139 passed in 2.80s`. |

## Requirement Matrix

| Requirement ID | Требование | Исходное состояние | Код/файлы | Тесты | Статус | Evidence/Notes |
| --- | --- | --- | --- | --- | --- | --- |
| CORE-01 | One shared deterministic funding settlement planner used by shadow and PaperBot. | Shadow has event-window helper; PaperBot used separate synchronized path. | `strategy_synchronized_funding.py`, `runtime_v2.py`, `trader.py` | `test_paperbot_v2_actually_calls_shared_planner`; focused v2 command `139 passed in 2.81s` | DONE | `FundingSettlementPlanner` is injected into `SynchronizedFundingRuntimeV2`; shadow wrapper delegates to same planner. |
| CORE-02 | Typed `FundingSettlementEvent` with UTC datetimes and Decimal math. | Event model existed but used float math and missed raw unit/scale/payer fields. | `strategy_synchronized_funding.py` | `test_three_settlement_events_are_considered_as_timeline`; shadow focused `12 passed, 44 deselected in 0.22s` | DONE | Dataclass now carries raw unit/scale, payer side, source sequence, verification level; planner critical sums use `Decimal` internally. Datetime values are serialized UTC ISO strings for storage compatibility. |
| CORE-03 | Typed `FundingRoutePlan` with eligibility/lifecycle/economics and no duplicate worse candidate. | Planner returned dict, two exit points, no explicit lifecycle axis. | `strategy_synchronized_funding.py` | `test_shadow_and_paperbot_route_plan_parity`; P0 focused tests `5 passed in 0.46s` | DONE | `FundingRoutePlan` dataclass returns single best candidate plan with included/excluded/ambiguous events, eligibility, lifecycle and conservative economics. |
| CORE-04 | PaperBot integration P0 across discovery, final refresh, open monitoring, close and accounting. | PaperBot blocked before event-window plan and required alignment. | `runtime_v2.py`, `trader.py` | `test_paperbot_v2_accepts_one_settlement_alignment_mismatch`; `test_paperbot_v2_multi_event_survives_first_event_and_closes_after_last`; focused v2 `139 passed in 2.81s` | DONE | V2 entry orchestration, final observation underwriting, open cycles, crossing and planned close consume stored route plan. |
| PLAN-01 | Event-window planning without blocking timestamp alignment; include/exclude/ambiguous events. | Shadow planner supported partial window but only for two events; PaperBot blocked alignment. | `strategy_synchronized_funding.py`, `runtime_v2.py`, `trader.py` | `test_paperbot_v2_accepts_one_settlement_alignment_mismatch`; `test_negative_second_settlement_is_excluded_when_exit_is_guaranteed`; `test_unavoidable_negative_second_settlement_is_deducted` | DONE | Alignment skew remains diagnostic; event timeline chooses included/excluded/ambiguous events by planned exit guarantees. |
| PLAN-02 | Any number of settlement events. | Planner considered first and optional second event only. | `strategy_synchronized_funding.py` | `test_three_settlement_events_are_considered_as_timeline`; shadow focused `12 passed, 44 deselected in 0.22s` | DONE | Source of truth is `funding_settlement_events`/`settlement_events` list; planner considers exit after each event within configured hold window. |
| LIFE-01 | Separate eligibility and lifecycle axes with entry pending/window/missed and multi-event state rules. | Current states were position lifecycle only; opportunity status and lifecycle were mixed. | `strategy_synchronized_funding.py`, `runtime_v2.py`, `trader.py` | `test_paperbot_v2_accepts_one_settlement_alignment_mismatch`; existing entry-window tests in focused v2 `139 passed in 2.81s` | DONE | Route plan exposes `eligibility_status` and `lifecycle_state`; PaperBot maps missed entry to `ENTRY_WINDOW_MISSED` and does not open. |
| LIFE-02 | Multi-event monitor_until/expires_at based on last included event and post-event reevaluation. | `expires_at` was first event; open lifecycle used one current cycle. | `strategy_synchronized_funding.py`, `runtime_v2.py` | `test_paperbot_v2_multi_event_survives_first_event_and_closes_after_last`; `test_paperbot_v2_closes_after_planned_event_window_without_reconciled_prior_cycle` | DONE | `monitor_until`/`expires_at` derive from last included event; PaperBot stores one cycle per included timestamp and closes after `planned_exit_at`. |
| SCHED-01 | Independent broad and focused scheduler clocks. | PaperBot has full/lightweight/hot cadence; shadow run sleeps broad interval and focused refresh is inside broad run. | `trader.py`, `shadow_monitor.py` | Pending | FAILED | Needs independent due tracking/metrics. |
| SCHED-02 | In-flight key, bounded queue/workers, cumulative metrics, stale rejection. | Shadow has per-venue in-flight set and last-run metrics; PaperBot has pending futures by venue only. | `shadow_monitor.py`, `trader.py` | Pending | FAILED | Needs cumulative metrics and key scope. |
| ENV-01 | Typed endpoint identity from client factory, no payload override/fallback. | Most clients lack identity; shadow can fallback to monitor environment. | `adapters/base.py`, `trader.py`, `shadow_monitor.py` | Pending | FAILED | Add identity wrapper/model. |
| ENV-02 | Production composition path from CLI/factory/constructor/parser/identity/planner/PaperBot. | No production constructor test proving identity reaches PaperBot route. | `cli.py`, `trader.py`, adapters, tests | Pending | FAILED | Add fake transport integration test. |
| TRUST-01 | Settlement contracts only from reviewed registry/evidence; market cannot self-promote. | Market can override verification/evidence/timing. | `settlement_contracts.py` | `test_market_row_cannot_self_promote_settlement_semantics`; `test_unknown_adapter_payload_cannot_self_promote_settlement_contract`; focused safety command `19 passed, 48 deselected in 0.07s` | DONE | Registry/template values are the only source of verified accrual model, verification level, official URLs, position inclusion rule, timing guarantees and confirmation sources; environment conflicts force UNVERIFIED. |
| TRUST-02 | Verification-level gate explicit for paper eligibility. | Eligibility mainly blockers and non-empty fields. | `settlement_contracts.py`, `venue_capabilities.py`, `strategy_synchronized_funding.py` | `test_risex_payload_promotion_remains_research_only`; focused safety command `19 passed, 48 deselected in 0.07s` | FAILED | Market self-promotion is blocked, but paper eligibility is still not governed by a standalone typed verification-level threshold. |
| FRESH-01 | Separate timestamps and no fabrication. | Stablecoin provider fabricates source time from observed time; some response fallbacks collapse. | `stablecoins.py`, `trader.py`, `shadow_monitor.py` | `test_public_stablecoin_provider_does_not_fabricate_source_timestamp`; focused safety command `19 passed, 48 deselected in 0.07s` | FAILED | Stablecoin source timestamp fabrication is fixed. General endpoint freshness contracts and route-wide fail-closed freshness remain open. |
| FRESH-02 | Sequence/generation stale write protection. | Shadow opportunity upsert rejects older `last_observed_at`; no general generation/request/source sequence model. | `storage.py`, `shadow_monitor.py`, `trader.py` | Pending | FAILED | Add minimal focused refresh protection. |
| COST-01 | Typed cost model; UNKNOWN is not zero. | Fee fallback/defaults and planner zero reserves can allow unknown cost as zero. | `strategy_synchronized_funding.py`, `runtime_v2.py` | `test_missing_fee_is_unknown_and_blocks_candidate`; `test_explicit_zero_fee_is_allowed_when_payload_provides_it`; focused safety command `19 passed, 48 deselected in 0.07s` | FAILED | Unknown fee now blocks candidate and default slippage/basis/execution reserves are nonzero. Full typed `CostEstimate` representation is not complete. |
| COST-02 | Execution model gates for CLOB/RFQ/POOL/UNKNOWN. | No common execution model; Variational RFQ is exposed as orderbook. | adapters, `venue_capabilities.py`, `trader.py` | Pending | FAILED | Add minimal enum/metadata/gates. |
| STABLE-01 | Same-asset only for trusted USD/USDT/USDC collateral registry. | Any same symbol passes. | `stablecoins.py`, `adapter_contracts.py` | `test_untrusted_same_asset_stable_route_is_blocked`; `test_trusted_same_asset_stable_route_is_allowed`; focused safety command `19 passed, 48 deselected in 0.07s` | DONE | Same-asset auto-pass now requires USD/USDT/USDC and trusted USD-major collateral family; USDe/DAI/USDT0/UNKNOWN are Research Only. |
| STABLE-02 | Stablecoin provider two sources, freshness, no fabricated source timestamp. | Two public sources exist for USDC/USDT but source_event_at equals observed_at. | `stablecoins.py` | `test_public_stablecoin_provider_does_not_fabricate_source_timestamp`; focused safety command `19 passed, 48 deselected in 0.07s` | DONE | Public provider uses CoinGecko and Coinbase, caches by sweep timestamp, leaves source_event_at empty for current snapshots, records response_received_at after fetch, applies max age and source divergence checks. |
| ALERT-01 | Individual alerts only for SHADOW_CANDIDATE with persisted claim/retry states. | Individual gated to SHADOW_CANDIDATE, but persistence is weak and summary notify can still send if enabled. | `shadow_monitor.py`, `storage.py` | Pending | FAILED | Add claim states/retry semantics. |
| LATENCY-01 | Worker request/response/parsing timestamps and per-worker latency. | Shadow latency computed after join; no parsing timestamp. | `shadow_monitor.py`, `trader.py` | Pending | FAILED | Capture worker completion timestamps. |
| POINTS-01 | Typed incentive metadata and no PnL promotion. | Placeholder `{"incentive_program_status": "UNKNOWN"}`. | `shadow_monitor.py` | Pending | FAILED | Add `IncentiveProgramMetadata`. |
| RISEX-01 | RiseX mandatory venue with health, strict mode and environment safety. | RiseX in primary inventory and profile; health fallback exists; needs regression hardening. | `adapter_contracts.py`, `profiles.py`, `risex.py`, `shadow_monitor.py` | Pending | FAILED | Add removal-fails and strict tests. |
| RISEX-02 | One-symbol public boundary probe with separate market/boundary/confirmation counts. | Probe stores snapshots but not robust boundary event confirmation. | `risex_probe.py`, `cli.py`, `storage.py` | Pending | FAILED | Rework public probe contract. |
| RISEX-03 | Honest canary: full safe implementation or explicit unsupported, no placeholder. | Placeholder string remains. | `risex_probe.py` | Pending | FAILED | Replace with typed unsupported reason or safe canary. |
| PAC-01 | Pacifica official verification. | Current adapter assumptions not verified in this pass. | `pacifica.py`, docs | Pending | BLOCKED_EXTERNAL_EVIDENCE | Requires official docs/API verification. |
| PAC-02 | Pacifica adapter only official public support, Research Only, no hardcoded fees. | Adapter exists with hardcoded fees and synthetic next hour. | `pacifica.py` | Pending | FAILED | Fix after evidence. |
| PAC-03 | Pacifica tests. | Existing tests incomplete. | tests | Pending | FAILED | Add constructor/parser/Research Only tests. |
| NADO-01 | Nado official verification and classification. | No adapter/evidence in repo. | docs | Pending | BLOCKED_EXTERNAL_EVIDENCE | Requires official Gateway/Indexer/API evidence. |
| NADO-02 | Nado public adapter with Decimal funding math and no execution. | Missing. | adapters | Pending | BLOCKED_EXTERNAL_EVIDENCE | Implement only if official endpoints confirmed. |
| NADO-03 | Nado tests. | Missing. | tests | Pending | BLOCKED_EXTERNAL_EVIDENCE | Add if adapter implemented. |
| VAR-01 | Variational verification; only proven data bugs fixed. | Current adapter assumes `/100`, zero fees and orderbook semantics. | `variational.py`, docs | Pending | BLOCKED_EXTERNAL_EVIDENCE | Needs official verification. |
| VAR-02 | Variational RFQ quarantine. | Adapter active and exposes synthetic orderbook; venue deactivated list includes it. | `variational.py`, `venues.py` | Pending | FAILED | Remove orderbook capability/gate strategy. |
| EXEC-01 | Common execution model gating. | Missing common enum/model. | adapters, capabilities, planner | Pending | FAILED | Minimal model needed. |
| TEST-CORE | Core tests TEST-CORE-01..20. | Partial existing synchronized tests; not full event-window PaperBot parity. | `tests/test_synchronized_funding_v2.py` | Pending | FAILED | Add focused tests after Phase B. |
| TEST-SAFE | Safety tests TEST-SAFE-01..22. | Partial existing tests; gaps in self-promotion, costs, stablecoin same-asset. | tests | Pending | FAILED | Add Phase C tests. |
| TEST-SCHED | Scheduler tests TEST-SCHED-01..07. | Partial shadow in-flight tests only. | tests | Pending | FAILED | Add minimal scheduler metrics tests. |
| TEST-ALERT | Alert tests TEST-ALERT-01..06. | Token redaction exists; claim/retry missing. | tests | Pending | FAILED | Add persisted alert tests. |
| TEST-VENUE | Venue tests for RiseX/Pacifica/Nado/Variational/Paradex/existing venues. | Partial RiseX/shadow tests; Nado missing. | tests | Pending | FAILED | Add after venue phases. |
| SMOKE-A | Public shadow smoke on separate DB. | Not run for this pass yet. | CLI | Pending | FAILED | Must run after implementation. |
| SMOKE-B | Deterministic PaperBot smoke with fake transports/temp DB. | Not run for this pass yet. | tests/CLI | Pending | FAILED | Must run after implementation. |
| SMOKE-C | RiseX public probe smoke. | Not run for this pass yet. | CLI | Pending | FAILED | Must run after probe fix. |
| SMOKE-D | Pacifica public smoke. | Not run for this pass yet. | CLI/tests | Pending | BLOCKED_EXTERNAL_EVIDENCE | Requires verified endpoints. |
| SMOKE-E | Nado public smoke. | Not run for this pass yet. | CLI/tests | Pending | BLOCKED_EXTERNAL_EVIDENCE | Requires adapter/evidence. |
| SMOKE-F | Variational research smoke. | Not run for this pass yet. | CLI/tests | Pending | FAILED | Must prove strategy/paper route count zero. |
