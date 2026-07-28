# Agent Operating Model

Smart Money Radar uses a two-agent workflow by default:

- **Codex** is the orchestrator, architect, and final reviewer.
- **Qwen/QN** is the full-access implementation worker for scoped tasks.

Codex owns architecture, formulas, trading assumptions, risk gates, venue eligibility, final review, and commit boundaries. Qwen/QN can edit files, write tests, run shell commands, run the app, and perform mechanical refactors directly inside a scoped task, but must not change product direction, risk thresholds, funding semantics, live-trading status, or strategy behavior independently.

The active paper strategy is `synchronized_funding_capture_v2`. Legacy `funding_only`, `spread_only`, `combined`, and `opportunistic_any` are experimental/research labels unless Codex explicitly enables an experimental profile.

## Default Flow

1. Codex inspects the code and chooses the implementation direction.
2. Codex gives Qwen/QN a task when useful.
3. Qwen/QN implements, tests, and reports the scoped result.
4. Codex reviews, corrects, and runs tests.
5. Codex reports the accepted result to Daniil.

This workflow remains active until Daniil explicitly asks to work differently.
