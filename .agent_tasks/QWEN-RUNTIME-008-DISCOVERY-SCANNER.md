IMPLEMENT NOW. EDIT PRODUCTION RUNTIME FILES.
DO NOT RETURN A REVIEW-ONLY RESPONSE.

You are Qwen 3.7 Plus, implementation engineer for Radar.

This is runtime-completion pass 008. Change production runtime code. Do not
run real bot/dashboard/live execution, do not read secrets, and do not touch
real SQLite databases. Tests must use FakeClock, fake clients/adapters, and
temporary SQLite only.

Current confirmed defects after pass 007:

- `PaperBot._run_lightweight_discovery()` is timer-only. It returns
  `{"status": "scheduled"}` and does not fetch market snapshots, match routes,
  apply capability gates, or add watch routes.
- Full scan is still the only path that can discover new synchronized routes
  between 300-second scans.
- Synchronized scanner status is mostly correct in `evaluate_perp_route`:
  settlement_capture returns `watch` or `research_only`, not final
  `paper_candidate`, and does not call legacy `build_strategy_evaluation`.
  Preserve that behavior.

Implement only the missing runtime integration needed for lightweight discovery:

1. Actual lightweight discovery

Inside `PaperBot._run_lightweight_discovery()`:

- keep the 30-second cadence and focused capacity check;
- use active funding clients to fetch lightweight catalog/market snapshots;
- do not fetch orderbooks for the whole universe;
- do not fetch long history;
- do not run full research forecast;
- consider markets with:

```
55 <= seconds_to_settlement <= 600
```

- match same canonical asset across venues;
- require long/short exact next funding timestamp skew <= 1 second;
- require preliminary gross funding > 0 using normalized next rates:

```
long_funding = -q * long_mark * long_normalized_next_funding_rate
short_funding = q * short_mark * short_normalized_next_funding_rate
gross = long_funding + short_funding
```

Use the configured target notional to derive a rough quantity. This is only
structural discovery, not final entry underwriting.

- run synchronized capability gate;
- compatible routes become watch/hot routes only;
- incompatible routes count as research_only with concrete reasons;
- do not create final `paper_candidate` from lightweight discovery;
- focused orderbooks remain requested only later by focused recheck/runtime.

Return summary:

```
markets_checked
routes_structurally_matched
watch_routes_added
research_only_routes
rejection_reasons
```

2. Route shape

Routes added to `self.hot_routes` must contain enough information for focused
recheck/runtime to continue:

- route_key;
- status="watch";
- canonical_asset;
- long_venue/short_venue;
- long_symbol/short_symbol;
- target_notional;
- legs with side, venue, symbol, next_funding_at, normalized_next_funding_rate,
  mark_price, index_price, volume_24h_usd, open_interest_usd, fee/minimum/step
  fields when available;
- evidence.synchronized_capability_passed and capability rejection reasons;
- evidence.lightweight_discovery summary;
- no orderbook-derived fields unless already present from the market snapshot.

3. Do not regress scanner economics

- Do not make spread convergence affect synchronized route status.
- Do not call `build_strategy_evaluation` for `decision_mode == "settlement_capture"`.
- Do not make `current_opportunity_net` with spread pass/fail synchronized routes.

4. Tests

Add FakeClock/fake-client/temp SQLite tests proving:

- A route appearing between 300-second full scans is added by 30-second
  lightweight discovery.
- The added route is `watch`, not `paper_candidate`.
- No orderbook fetch is called during lightweight discovery.
- Capability fail-closed route becomes research_only count/reason, not watch.
- Zero/negative preliminary gross funding does not become watch.
- Same exact next timestamp with different nominal intervals can become watch.
- Legacy `build_strategy_evaluation` is not called for settlement_capture.

Acceptance:

- production runtime diff is non-empty;
- PaperBot `_run_lightweight_discovery` actually adds watch routes;
- tests use fake clients and do not call real APIs;
- targeted and full tests pass.
