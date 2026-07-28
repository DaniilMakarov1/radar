#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = REPO_ROOT / ".agent_runs"
LOCK_PATH = RUNS_DIR / ".qwen_worker.lock"
SYSTEM_PATH = REPO_ROOT / ".qwen" / "worker_system.md"
SCHEMA_PATH = REPO_ROOT / ".qwen" / "worker_result.schema.json"


def run_capture(args: list[str], *, timeout: int | None = None) -> str:
    completed = subprocess.run(
        args,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    return completed.stdout


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def parse_duration_seconds(value: str) -> int:
    raw = str(value).strip().lower()
    if not raw:
        raise argparse.ArgumentTypeError("duration cannot be empty")
    suffix = raw[-1]
    multiplier = 1
    number = raw
    if suffix in {"s", "m", "h"}:
        number = raw[:-1]
        multiplier = {"s": 1, "m": 60, "h": 3600}[suffix]
    try:
        parsed = float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid duration: {value!r}") from exc
    seconds = int(parsed * multiplier)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("duration must be positive")
    return seconds


def acquire_lock() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(f"Qwen worker lock exists: {LOCK_PATH}")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


def build_prompt(task_file: Path) -> str:
    schema = read(SCHEMA_PATH)
    system = read(SYSTEM_PATH)
    task = read(task_file)
    return (
        f"{system}\n\n"
        "Return only JSON matching this schema:\n"
        f"{schema}\n\n"
        "TASK:\n"
        f"{task}\n"
    )


def build_qwen_command(
    *,
    prompt: str,
    model: str,
    approval_mode: str,
    sandbox: bool,
    max_wall_time: str,
    max_tool_calls: int,
    max_session_turns: int,
    qwen_bin: str = "qwen",
) -> list[str]:
    command = [
        qwen_bin,
        "--prompt",
        prompt,
        "--model",
        model,
        "--approval-mode",
        approval_mode,
        "--output-format",
        "json",
        "--max-wall-time",
        max_wall_time,
        "--max-tool-calls",
        str(max_tool_calls),
        "--max-session-turns",
        str(max_session_turns),
    ]
    if sandbox:
        command.append("--sandbox")
    return command


def models_used_from_transcript(stdout: str) -> set[str]:
    try:
        events = json.loads(stdout)
    except json.JSONDecodeError:
        return set()
    models: set[str] = set()
    if not isinstance(events, list):
        return models
    for event in events:
        if not isinstance(event, dict):
            continue
        model = event.get("model")
        if isinstance(model, str):
            models.add(model)
        message = event.get("message")
        if isinstance(message, dict):
            message_model = message.get("model")
            if isinstance(message_model, str):
                models.add(message_model)
        stats = event.get("stats")
        if isinstance(stats, dict):
            stats_models = stats.get("models")
            if isinstance(stats_models, dict):
                models.update(str(name) for name in stats_models)
    return models


def model_mismatch(expected: str, actual_models: Iterable[str]) -> bool:
    actual = {str(model) for model in actual_models if str(model)}
    return expected not in actual or any(model != expected for model in actual)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a scoped Qwen worker task.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--model", default="qwen3.7-plus")
    parser.add_argument("--approval-mode", default="auto")
    parser.add_argument("--sandbox", dest="sandbox", action="store_true", default=True)
    parser.add_argument("--no-sandbox", dest="sandbox", action="store_false")
    parser.add_argument("--max-wall-time", default="1h")
    parser.add_argument("--max-tool-calls", type=int, default=600)
    parser.add_argument("--max-session-turns", type=int, default=250)
    parser.add_argument("--qwen-bin", default="qwen")
    args = parser.parse_args()

    task_file = Path(args.task_file).expanduser().resolve()
    if not task_file.exists():
        raise SystemExit(f"Task file does not exist: {task_file}")

    run_dir = RUNS_DIR / args.task_id
    run_dir.mkdir(parents=True, exist_ok=True)

    acquire_lock()
    started = time.time()
    try:
        write(run_dir / "git_status_before.txt", run_capture(["git", "status", "--short"]))
        write(run_dir / "git_diff_before.patch", run_capture(["git", "diff"]))
        prompt = build_prompt(task_file)
        command = build_qwen_command(
            prompt=prompt,
            model=args.model,
            approval_mode=args.approval_mode,
            sandbox=bool(args.sandbox),
            max_wall_time=args.max_wall_time,
            max_tool_calls=max(0, int(args.max_tool_calls)),
            max_session_turns=max(1, int(args.max_session_turns)),
            qwen_bin=args.qwen_bin,
        )
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=parse_duration_seconds(args.max_wall_time) + 30,
            check=False,
        )
        actual_models = sorted(models_used_from_transcript(completed.stdout))
        mismatch = model_mismatch(args.model, actual_models)
        write(run_dir / "stdout.json", completed.stdout)
        write(run_dir / "stderr.log", completed.stderr)
        exit_code = completed.returncode if not mismatch else 3
        write(run_dir / "exit_code.txt", f"{exit_code}\n")
        write(run_dir / "git_status_after.txt", run_capture(["git", "status", "--short"]))
        write(run_dir / "git_diff_after.patch", run_capture(["git", "diff"]))
        metadata = {
            "task_id": args.task_id,
            "task_file": str(task_file),
            "command": [
                args.qwen_bin,
                "--prompt",
                "<redacted>",
                "--model",
                args.model,
                "--approval-mode",
                args.approval_mode,
                "--output-format",
                "json",
            ],
            "sandbox": bool(args.sandbox),
            "requested_model": args.model,
            "actual_models": actual_models,
            "model_mismatch": mismatch,
            "started_at_epoch": started,
            "finished_at_epoch": time.time(),
            "qwen_exit_code": completed.returncode,
            "exit_code": exit_code,
        }
        write(run_dir / "metadata.json", json.dumps(metadata, indent=2, sort_keys=True))
        return exit_code
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
