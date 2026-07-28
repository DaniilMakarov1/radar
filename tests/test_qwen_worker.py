"""Tests for the Qwen worker infrastructure."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKER_SCRIPT = REPO_ROOT / "scripts" / "run_qwen_worker.py"
WORKER_SYSTEM = REPO_ROOT / ".qwen" / "worker_system.md"
WORKER_SCHEMA = REPO_ROOT / ".qwen" / "worker_result.schema.json"
AGENT_TASKS_DIR = REPO_ROOT / ".agent_tasks"


def test_worker_script_exists() -> None:
    assert WORKER_SCRIPT.exists()
    assert WORKER_SCRIPT.stat().st_size > 0


def test_worker_system_doc_exists() -> None:
    assert WORKER_SYSTEM.exists()
    content = WORKER_SYSTEM.read_text(encoding="utf-8")
    assert "implementation worker" in content.lower() or "scoped" in content.lower()


def test_worker_schema_exists_and_valid_json() -> None:
    assert WORKER_SCHEMA.exists()
    schema = json.loads(WORKER_SCHEMA.read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert "status" in schema["properties"]
    assert "summary" in schema["properties"]
    assert "changed_files" in schema["properties"]
    assert set(schema["required"]) >= {"status", "summary", "changed_files"}


def test_worker_schema_status_enum() -> None:
    schema = json.loads(WORKER_SCHEMA.read_text(encoding="utf-8"))
    assert set(schema["properties"]["status"]["enum"]) == {"completed", "blocked", "failed"}


def test_agent_tasks_directory_exists() -> None:
    assert AGENT_TASKS_DIR.exists()
    assert AGENT_TASKS_DIR.is_dir()


def test_agent_tasks_readme_exists() -> None:
    readme = AGENT_TASKS_DIR / "README.md"
    assert readme.exists()
    content = readme.read_text(encoding="utf-8")
    assert "task" in content.lower()


def test_worker_system_supports_code_writing() -> None:
    content = WORKER_SYSTEM.read_text(encoding="utf-8")
    assert "edit" in content.lower() or "create" in content.lower()
    assert "test" in content.lower()


def test_worker_script_acquire_lock_creates_lock(tmp_path) -> None:
    lock_path = tmp_path / "test.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    assert lock_path.exists()
    lock_path.unlink()
    assert not lock_path.exists()


def test_worker_result_schema_no_additional_properties() -> None:
    schema = json.loads(WORKER_SCHEMA.read_text(encoding="utf-8"))
    assert schema.get("additionalProperties") is False


def test_worker_system_forbids_secrets() -> None:
    content = WORKER_SYSTEM.read_text(encoding="utf-8")
    assert ".env" in content
    assert "credentials" in content.lower() or "secret" in content.lower()


def test_worker_system_forbids_git_mutation() -> None:
    content = WORKER_SYSTEM.read_text(encoding="utf-8")
    assert "commit" in content.lower()
    assert "push" in content.lower()


def test_worker_system_forbids_live_trading() -> None:
    content = WORKER_SYSTEM.read_text(encoding="utf-8")
    assert "live trading" in content.lower() or "live" in content.lower()


def test_worker_script_build_prompt_includes_system_and_schema(tmp_path) -> None:
    task_file = tmp_path / "task.md"
    task_file.write_text("# Test task\nDo something.", encoding="utf-8")
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_qwen_worker", str(WORKER_SCRIPT))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with mock.patch.object(module, "REPO_ROOT", REPO_ROOT):
        with mock.patch.object(module, "SYSTEM_PATH", WORKER_SYSTEM):
            with mock.patch.object(module, "SCHEMA_PATH", WORKER_SCHEMA):
                prompt = module.build_prompt(task_file)
    assert "Test task" in prompt
    assert "implementation worker" in prompt.lower() or "scoped" in prompt.lower()


def test_worker_default_command_uses_qwen37_auto_sandbox() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_qwen_worker", str(WORKER_SCRIPT))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    command = module.build_qwen_command(
        prompt="task",
        model="qwen3.7-plus",
        approval_mode="auto",
        sandbox=True,
        max_wall_time="1h",
        max_tool_calls=600,
        max_session_turns=250,
    )

    assert command[:2] == ["qwen", "--prompt"]
    assert "--sandbox" in command
    assert command[command.index("--model") + 1] == "qwen3.7-plus"
    assert command[command.index("--approval-mode") + 1] == "auto"
    assert command[command.index("--max-tool-calls") + 1] == "600"
    assert command[command.index("--max-session-turns") + 1] == "250"


def test_worker_detects_model_mismatch() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_qwen_worker", str(WORKER_SCRIPT))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    stdout = json.dumps([
        {"type": "system", "model": "qwen3.7-plus"},
        {"type": "result", "stats": {"models": {"qwen3.7-plus": {}}}},
    ])

    assert module.models_used_from_transcript(stdout) == {"qwen3.7-plus"}
    assert not module.model_mismatch("qwen3.7-plus", {"qwen3.7-plus"})
    assert module.model_mismatch("qwen3.7-plus", {"qwen3.8-max-preview"})
