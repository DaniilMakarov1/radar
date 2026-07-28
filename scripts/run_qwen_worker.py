#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a scoped Qwen worker task.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task-file", required=True)
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
        command = [
            "qwen",
            "--prompt",
            prompt,
            "--model",
            "qwen3.8-max-preview",
            "--output-format",
            "json",
        ]
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1200,
            check=False,
        )
        write(run_dir / "stdout.json", completed.stdout)
        write(run_dir / "stderr.txt", completed.stderr)
        write(run_dir / "exit_code.txt", f"{completed.returncode}\n")
        write(run_dir / "git_status_after.txt", run_capture(["git", "status", "--short"]))
        write(run_dir / "git_diff_after.patch", run_capture(["git", "diff"]))
        metadata = {
            "task_id": args.task_id,
            "task_file": str(task_file),
            "command": ["qwen", "--prompt", "<redacted>", "--model", "qwen3.8-max-preview", "--output-format", "json"],
            "started_at_epoch": started,
            "finished_at_epoch": time.time(),
            "exit_code": completed.returncode,
        }
        write(run_dir / "metadata.json", json.dumps(metadata, indent=2, sort_keys=True))
        return completed.returncode
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
