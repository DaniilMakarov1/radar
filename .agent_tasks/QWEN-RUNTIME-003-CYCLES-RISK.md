IMPLEMENT NOW. EDIT PRODUCTION FILES.
DO NOT RETURN A REVIEW-ONLY RESPONSE.

You are Qwen 3.7 Plus acting as implementation engineer. Codex/GPT is the architect/reviewer.

Scope: post-settlement cycle manager, exact timestamp hold underwriting, integrated risk engine, lightweight discovery.

Hard constraints:
- Edit production runtime code, not only tests/docs/helpers.
- Do not run live bots, dashboards, watchers, migrations against the real DB, or real account/API endpoints.
- Use only temporary SQLite/fake data for tests.
- Do not read .env, .env.*, secrets, real DB files, .git/**, or .agent_runs/**.
- Do not commit/push.
- New helpers count only if PaperBot/runtime actually calls them.

Required runtime changes:
1. Cycle captured only if both legs were OPEN through scheduled settlement timestamp.
2. At crossing:
   - cycle.state = SETTLEMENT_CROSSED;
   - settlements_captured_count += 1;
   - create reconciliation rows;
   - do not accrue funding estimates.
3. Normal exit forbidden until T+20 seconds. Hard-risk exit allowed always.
4. Schedule probes:
   - start T+5, continue through T+15, 1 second interval;
   - require two consecutive fresh responses with same next timestamps;
   - exact next timestamp alignment <= 1 second;
   - mismatch or missing timestamp by T+15 => close at T+20;
   - do not use funding_interval_hours equality as schedule gate.
5. Next-cycle observations until T+30:
   - at least 15 valid observations;
   - at least 20 seconds span;
   - latest age <= 2 seconds;
   - cross-venue skew <= 1 second;
   - all next gross funding > 0;
   - latest >= 0.8 * median;
   - next_conservative_funding = 0.9 * min(next gross funding);
   - next settlement wait 300..14400 seconds.
6. Hold economics:
   - opening fees are sunk, never charged again in incremental hold;
   - current close fees stress 1.10;
   - basis reserve by 30s adverse exit spread changes and duration floor;
   - legging/time/liquidity reserves;
   - hold allowed only if next funding gross >= max(2.50, reference_notional*0.005), incremental_hold_net >= max(1.00, reference_notional*0.002), coverage >=1.5, projected total after next cycle >=0, captured settlements <4, projected age <=14700s, all risk gates passed.
7. Integrate risk.py into real PaperBot open-position polling:
   - poll every 2s outside hot window, 1s near settlement/T..T+30;
   - calculate mark/index divergence, liquidation price/distance, leg equity, maintenance margin, margin safety ratio;
   - paper MMR fallback 0.02 only in paper mode;
   - entry gates liquidation_distance >=0.50, margin_safety_ratio>=10, mark/index <=50bps;
   - warnings liquidation_distance<0.30 or margin_safety_ratio<5;
   - hard exit liquidation_distance<=0.20, margin_safety_ratio<=3, mark/index>100bps.
8. Dynamic basis risk:
   - entry spread = short_entry_sell_price - long_entry_buy_price;
   - current executable exit spread = short_exit_buy_vwap - long_exit_sell_vwap;
   - deterioration = max(0, current_exit_spread-entry_spread)/reference_price*10000;
   - active_risk_budget_bps = clamp(25, 50, 0.5 * active_cycle_conservative_funding_edge_bps);
   - immediate hard exit if deterioration >= budget;
   - executable PnL hard exit after two consecutive fresh snapshots if paper_net_if_exit_now <= -risk_budget_usd.
   - do not use fixed 200 bps stop in synchronized strategy.
9. Stale data:
   - healthy age <=2s;
   - degraded >2s, entry forbidden, retry 3 times at 0.5s;
   - still >5s => EMERGENCY_UNWIND with reason risk_data_hard_stale and 100bps penalty.
10. Common price move:
   - 5% warning, 10% critical/fresh risk recalculation;
   - common move alone never closes safe position.
11. Add lightweight discovery every 30s:
   - fetch only market snapshots, next funding timestamps, normalized next rates, mark/index, volume, open interest;
   - no long history or full orderbooks for entire universe;
   - consider settlements 55..600s;
   - create watch only if capabilities, same asset/collateral/quote, settlement skew <=1s, preliminary gross funding >0;
   - focused orderbooks only for watch routes.
   - if capacity cannot maintain 1s focused cadence, reason focused_recheck_capacity_insufficient and entry forbidden.

Tests required:
- Hold can proceed while prior reconciliation is pending.
- Equal timestamps eligible; same interval but different timestamps closes; different nominal intervals but equal next timestamp passes.
- 1h and 4h aligned next settlements can hold; >4h closes.
- Open fees are sunk in hold economics.
- Max settlements and max age gates.
- Risk helper is called by real PaperBot runtime, not only direct helper tests.
- Basis stop 39.99/40.00 boundary with dynamic budget.
- 10% common move alerts but does not close if otherwise safe.
- Hard stale after retries => EMERGENCY_UNWIND.
- Lightweight discovery finds route between full scans.

Acceptance:
- Production code diff is non-empty and includes PaperBot/runtime calls to cycle_manager and risk.
- Focused tests pass:
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_synchronized_funding_v2.py tests/test_funding_paper_trader.py -q
