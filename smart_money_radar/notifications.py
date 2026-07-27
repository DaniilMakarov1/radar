from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from smart_money_radar.config import load_env_file


TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_HTML_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|s|code|pre|a|blockquote)(?:\s[^>]*)?>"
)


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
        token_env_var: str = "TELEGRAM_BOT_TOKEN",
        chat_id_env_var: str = "TELEGRAM_CHAT_ID",
        fallback_to_default: bool = False,
    ) -> None:
        load_env_file()
        self.token_env_var = token_env_var
        self.chat_id_env_var = chat_id_env_var
        self.token = token or os.environ.get(token_env_var)
        self.chat_id = chat_id or os.environ.get(chat_id_env_var)
        if (
            fallback_to_default
            and not self.token
            and token_env_var != "TELEGRAM_BOT_TOKEN"
        ):
            self.token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if (
            fallback_to_default
            and not self.chat_id
            and chat_id_env_var != "TELEGRAM_CHAT_ID"
        ):
            self.chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        self.timeout_seconds = max(1, int(timeout_seconds))

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> NotificationResult:
        if not self.token:
            return NotificationResult("not_configured", f"{self.token_env_var} missing")
        if not self.chat_id:
            return NotificationResult("not_configured", f"{self.chat_id_env_var} missing")
        message_payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if TELEGRAM_HTML_TAG_RE.search(text):
            message_payload["parse_mode"] = "HTML"
        data = urllib.parse.urlencode(message_payload).encode("utf-8")
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
