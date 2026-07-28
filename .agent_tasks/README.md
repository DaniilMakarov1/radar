# Agent Tasks

This directory holds scoped task descriptions for the Qwen worker and other
subordinate agents.

## Workflow

1. Codex/GPT writes a task file here (e.g. `QWEN-003-fix-retention.md`).
2. The task file describes the exact scope, files, and acceptance criteria.
3. `scripts/run_qwen_worker.py --task-id <id> --task-file <path>` executes it.
4. The worker reads `.qwen/worker_system.md` for rules and returns JSON
   matching `.qwen/worker_result.schema.json`.
5. Codex reviews the result in `.agent_runs/<task-id>/`.

## Naming Convention

- `QWEN-<NNN>-<short-slug>.md` for Qwen worker tasks.
- `CODEX-<NNN>-<short-slug>.md` for Codex tasks (if any).

## Task File Template

```markdown
# Task: <short title>

## Scope
- Files to modify: ...
- Files NOT to touch: ...

## Requirements
1. ...
2. ...

## Acceptance Criteria
- [ ] Tests pass: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q`
- [ ] compileall passes: `python3 -m compileall smart_money_radar`
- [ ] ...

## Constraints
- Do not add dependencies.
- Do not change risk thresholds.
- ...
```

## Active Tasks

See individual `.md` files for current task descriptions.
