# Agent Operating Model

Smart Money Radar uses a two-agent workflow by default:

- **Codex** is the orchestrator, architect, and final reviewer.
- **Qwen/QN** is the full-access implementation worker for scoped tasks.

Codex owns architecture, trading assumptions, risk gates, venue eligibility, final review, and commit boundaries. Qwen/QN can edit files, write tests, run shell commands, run the app, and perform mechanical refactors directly, but should not change product direction independently.

## Default Flow

1. Codex inspects the code and chooses the implementation direction.
2. Codex gives Qwen/QN a task when useful.
3. Qwen/QN implements, tests, and reports the scoped result.
4. Codex reviews, corrects, and runs tests.
5. Codex reports the accepted result to Daniil.

This workflow remains active until Daniil explicitly asks to work differently.
