#!/usr/bin/env python3
"""Telegram heartbeat for the qwen channel bot.

Sends a short status line every N minutes so the user knows the bot is alive.
Checks key launchd services and reports their state in a few words.

Deploy: copy to ~/.local/share/qwen-channel/heartbeat.py and install the
com.smartmoneyradar.qwen-heartbeat launchd agent.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

BOT_TOKEN = os.environ.get("QWEN_CHANNEL_TELEGRAM_TOKEN") or os.environ.get(
    "TELEGRAM_BOT_TOKEN", ""
)
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
INTERVAL_MINUTES = int(os.environ.get("HEARTBEAT_INTERVAL_MINUTES", "60"))

SERVICES = [
    ("com.smartmoneyradar.funding-paper-trader", "funding"),
]

QWEN_CHANNEL_LABEL = "com.smartmoneyradar.qwen-channel"
CPU_BUSY_THRESHOLD = 2.0


def _launchctl_list() -> list[list[str]]:
    try:
        out = subprocess.check_output(
            ["launchctl", "list"], text=True, timeout=5
        )
        rows: list[list[str]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                rows.append([p.strip() for p in parts[:3]])
        return rows
    except Exception:
        return []


def service_running(label: str, rows: list[list[str]] | None = None) -> bool:
    if rows is None:
        rows = _launchctl_list()
    for parts in rows:
        if len(parts) >= 3 and parts[2] == label:
            return parts[0] != "-"
    return False


def service_pid(label: str, rows: list[list[str]] | None = None) -> int | None:
    if rows is None:
        rows = _launchctl_list()
    for parts in rows:
        if len(parts) >= 3 and parts[2] == label and parts[0] != "-":
            try:
                return int(parts[0])
            except ValueError:
                return None
    return None


def qwen_busy(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        out = subprocess.check_output(
            ["ps", "-o", "%cpu=", "-p", str(pid)],
            text=True,
            timeout=5,
        )
        cpu = float(out.strip())
        return cpu > CPU_BUSY_THRESHOLD
    except Exception:
        return False


def build_message() -> str:
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    rows = _launchctl_list()

    parts: list[str] = []
    all_ok = True
    for label, short in SERVICES:
        ok = service_running(label, rows)
        icon = "🟢" if ok else "🔴"
        if not ok:
            all_ok = False
        parts.append(f"{icon}{short}")
    status = " | ".join(parts)

    qwen_pid = service_pid(QWEN_CHANNEL_LABEL, rows)
    if qwen_pid is None:
        qwen_line = "🔴 Qwen: оффлайн"
    elif qwen_busy(qwen_pid):
        qwen_line = "🟡 Qwen: работает над задачей"
    else:
        qwen_line = "🔵 Qwen: ожидает задачу"

    header = "✅ alive" if all_ok else "⚠️ issue"
    return f"{header} · {now}\n{status}\n{qwen_line}"


def send_telegram(text: str, chat_id: str = "") -> None:
    target = chat_id or CHAT_ID
    if not BOT_TOKEN or not target:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps(
        {"chat_id": target, "text": text, "disable_notification": True}
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        pass


def discover_chat_id() -> str:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?limit=1"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        updates = data.get("result") or []
        if updates:
            msg = updates[-1].get("message") or updates[-1].get(
                "my_chat_member", {}
            ).get("chat", {})
            chat_id = msg.get("chat", {}).get("id") or msg.get("id")
            if chat_id:
                return str(chat_id)
    except Exception:
        pass
    return ""


def main() -> None:
    if not BOT_TOKEN:
        print("no bot token — skipping heartbeat", file=sys.stderr)
        return
    chat_id = CHAT_ID or discover_chat_id()
    if chat_id:
        send_telegram(build_message(), chat_id)


if __name__ == "__main__":
    main()
