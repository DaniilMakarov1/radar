from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from smart_money_radar.config import load_env_file


TELEGRAM_API_BASE = "https://api.telegram.org"


@dataclass(frozen=True)
class NotificationResult:
    status: str
    error: str | None = None
    payload: dict[str, Any] | None = None


class TelegramNotifier:
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        timeout_seconds: int = 10,
    ) -> None:
        load_env_file()
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.timeout_seconds = max(1, int(timeout_seconds))

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> NotificationResult:
        if not self.token:
            return NotificationResult("not_configured", "TELEGRAM_BOT_TOKEN missing")
        if not self.chat_id:
            return NotificationResult("not_configured", "TELEGRAM_CHAT_ID missing")
        data = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{TELEGRAM_API_BASE}/bot{self.token}/sendMessage",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "smart-money-radar/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return NotificationResult("failed", str(exc))
        if payload.get("ok"):
            return NotificationResult("sent", payload=payload)
        return NotificationResult("failed", str(payload))


def telegram_update_chat_ids(
    token: str | None = None,
    timeout_seconds: int = 10,
) -> list[dict[str, Any]]:
    load_env_file()
    resolved_token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not resolved_token:
        return []
    request = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/bot{resolved_token}/getUpdates",
        headers={"User-Agent": "smart-money-radar/0.1"},
    )
    with urllib.request.urlopen(request, timeout=max(1, int(timeout_seconds))) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        return []
    chats: dict[str, dict[str, Any]] = {}
    for update in payload.get("result") or []:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        chats[str(chat_id)] = {
            "chat_id": str(chat_id),
            "type": chat.get("type"),
            "title": chat.get("title"),
            "username": chat.get("username"),
            "first_name": chat.get("first_name"),
            "last_name": chat.get("last_name"),
        }
    return list(chats.values())
