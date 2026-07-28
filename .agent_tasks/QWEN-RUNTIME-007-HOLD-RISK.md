IMPLEMENT NOW. EDIT PRODUCTION RUNTIME FILES.
DO NOT RETURN A REVIEW-ONLY RESPONSE.

You are Qwen 3.7 Plus, implementation engineer for Radar.

This is runtime-completion pass 007. Change production runtime code. Do not
run real bot/dashboard/live execution, do not read secrets, and do not touch
real SQLite databases. Tests must use FakeClock, fake adapters/providers, and
temporary SQLite only.

Current confirmed defects after pass 006:

- Post-settlement hold observations begin too late. `next_cycle_hold_or_close_decision`
  waits until T+20 before collecting hold observations, but T+30 underwriting
  requires a 20-second span.
- Hold economics uses `position.paper_net_pnl_estimated + incremental_hold_net`.
  It must use the fresh executable `paper_net_if_exit_now + incremental_hold_net`.
- Schedule probing is still a single decision check, not a T+5..T+15 probe
  timeline with two consecutive fresh matching responses.
- Some tests manually call `collect_hold_observation`; add real PaperBot polling
  tests that advance FakeClock second by second.
- Entry/hold reserves still fall back too eagerly to static 30/15 bps where
  enough observations exist.
- Runtime risk must continue using actual entry values and must not reintroduce
  fake `liquidation_distance=1.0` or `margin_safety_ratio=99.0`.

Implement:

1. Post-settlement timeline

At settlement T:

- `mark_settlement_crossed` creates PENDING reconciliation rows and sets cycle
  state to `SETTLEMENT_CROSSED`.

At T+5:

- state should become `POST_SETTLEMENT_EVALUATION`;
- begin exact schedule probes;
- begin hold observations.

From T+5 through T+15:

- schedule probe every 1 second;
- require two consecutive fresh probes with the same aligned next funding time.

If mismatch confirmed or timestamp missing through T+15:

- schedule close at T+20.

If aligned:

- do not close merely because T+20 passed;
- continue collecting hold observations every 1 second through T+30.

At T+30:

Hold requires:

```
observation_count >= 15
observation_span >= 20 seconds
latest age <= 2 seconds
cross_venue_skew <= 1 second
all next gross funding > 0
latest >= 0.8 * median
```

Normal exit is forbidden before T+20. Hard risk exit is always allowed.

2. Hold uses current executable PnL

Replace:

```
position.paper_net_pnl_estimated + incremental_hold_net
```

with:

```
paper_net_if_exit_now + incremental_hold_net
```

Use `record_current_executable_pnl(...)` or an equivalent runtime value that is
actually updated by `PaperBot.process_synchronized_open_positions`.

Opening fees remain sunk costs for incremental hold economics, but stay included
once in total position PnL through current executable PnL.

3. Schedule probes

Store enough lightweight state in the position config/cycle decision payload to
know:

- first probe time;
- latest probe time;
- previous probed next-settlement pair;
- consecutive matching probe count;
- aligned next settlement timestamp;
- mismatch or missing reason.

Do not use `funding_interval_hours` equality as schedule gate. Use exact next
timestamps:

```
abs(long_next_funding_at - short_next_funding_at) <= 1.0 second
```

Different nominal intervals are allowed if exact next timestamp is aligned.

4. Risk and reserves

- Do not reintroduce fixed 200 bps basis stop in synchronized runtime.
- Do not reintroduce fake risk values.
- If enough entry observations exist, compute entry basis reserve using p95
  adverse executable exit-spread changes and entry legging reserve using p95
  absolute one-second mark returns.
- Fallback 30/15 bps only when there are fewer than 10 usable changes/returns.
- Missing fee/rate/mark/index should reject route for synchronized entry/hold,
  not silently become zero.

5. Tests

Add end-to-end tests with FakeClock/temp SQLite/fake routes:

- Hold observations begin at T+5, not T+20.
- Advance FakeClock one second at a time from T through T+30 via
  `PaperBot.process_synchronized_open_positions`; do not manually call
  `collect_hold_observation`; aligned +3600 schedule and passing economics
  produces `HOLDING_NEXT_CYCLE`.
- Mismatch next settlement (+3600 vs +14400) closes at T+20.
- Different nominal intervals but same exact next timestamp pass schedule gate.
- Hold uses current executable PnL: set initial estimate positive but current
  executable PnL negative enough; hold must reject.
- Missing fee/rate/mark/index rejects synchronized route/hold.
- No `liquidation_distance_fraction=1.0` or `margin_safety_ratio=99.0` in v2.

Acceptance:

- production runtime diff is non-empty;
- PaperBot polling loop, not helper-only tests, proves the hold timeline;
- targeted tests pass;
- no live bot/dashboard/database run.
