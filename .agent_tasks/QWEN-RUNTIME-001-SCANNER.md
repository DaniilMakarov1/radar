IMPLEMENT NOW. EDIT PRODUCTION FILES.
DO NOT RETURN A REVIEW-ONLY RESPONSE.

You are Qwen 3.7 Plus acting as implementation engineer. Codex/GPT is the architect/reviewer.

Scope: scanner, capabilities, timestamps, observations for synchronized_funding_capture_v2.

Hard constraints:
- Edit production runtime code, not only tests/docs/helpers.
- Do not run live bots, dashboards, watchers, migrations against the real DB, or real account/API endpoints.
- Use only temporary SQLite/fake data for tests.
- Do not read .env, .env.*, secrets, real DB files, .git/**, or .agent_runs/**.
- Do not commit/push.
- Do not create isolated helpers unless the main PaperBot/runtime actually calls them.

Required runtime changes:
1. The synchronized_funding_capture path must not use legacy build_strategy_evaluation to decide paper_candidate, selected_strategy, or expected_net_pnl.
2. Market-wide scan may only produce rejected/research_only/watch for synchronized_funding_capture. A route can become paper_candidate only after focused observations/underwriting.
3. For synchronized_funding_capture, spread convergence must always be zero:
   - expected_spread_convergence_pnl = 0.0
   - spread_pnl_component = 0.0
   - signed_spread_pnl_component = 0.0
4. Change VenueCapability to fail closed:
   - all critical bool defaults False;
   - add funding_rate_unit, funding_sign_convention, contract_kind;
   - base/unknown adapter capability must be research-only;
   - active adapters still scan, but unknown/unclear capabilities cannot be paper eligible.
5. Paper eligibility requires:
   - contract_kind == linear_perpetual;
   - funding_rate_semantics == next_settlement;
   - funding_rate_unit == fraction_of_notional_per_settlement;
   - funding_sign_convention == positive_long_pays;
   - supports_discrete_funding, supports_next_funding_timestamp, supports_mark_price, supports_index_price, supports_orderbook_timestamp, supports_orderbook_depth, supports_24h_quote_volume, supports_open_interest, supports_taker_fee, supports_quantity_step, supports_min_notional all True;
   - same collateral_asset and same quote_asset;
   - collateral in USDT/USDC/USD.
6. Core strategy must consume normalized_next_funding_rate only. Adapter/raw conversion must store raw_funding_rate, raw_funding_rate_unit, normalized_next_funding_rate, normalization_evidence where available. Do not use ambiguous last/current funding as next settlement funding.
7. Capture real request timestamps for each venue/market/orderbook where the data model allows it:
   - request_started_at, response_received_at, normalized_at, venue_server_time, source_event_at, orderbook_event_time.
   - scan observed_at can remain metadata only; it must not be used for cross-venue freshness.
8. Focused pre-entry observation validity:
   - long_age <= 2 seconds;
   - short_age <= 2 seconds;
   - abs(long_response_received_at - short_response_received_at) <= 1 second;
   - both next funding timestamps belong to same cycle;
   - both books executable;
   - capabilities passed.
9. First structural watch should create a v2 funding_capture_position in state DISCOVERED. Entering arm window should update state ARMED and create cycle 1.
10. Entry underwriting requires at least 10 valid focused observations, at least 20 seconds span, latest age <= 2 seconds, all gross_funding_pnl > 0, latest_gross >= 0.8 * median_gross. Conservative funding = 0.9 * min(gross funding).

Tests required:
- Add/update tests proving build_strategy_evaluation is not called by synchronized scanner/runtime; monkeypatch it to raise.
- Zero funding + positive spread convergence must not become paper_candidate.
- Capability fail-closed when explicit next-settlement semantics/unit/sign/contract_kind missing.
- USDT vs USDC is research_only.
- Response skew 1.000 seconds valid; 1.001 invalid.
- 9 observations no entry; 10 observations with span 19.9 no entry; 10 observations with span 20 eligible.

Acceptance:
- Production code diff is non-empty.
- PaperBot or scanner runtime actually calls the new synchronized path.
- Focused tests pass:
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_synchronized_funding_v2.py tests/test_funding_paper_trader.py -q
