from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Any

from smart_money_radar.config import load_env_file


TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_HTML_TAG_RE = re.compile(
    r"</?(?:b|strong|i|em|u|s|code|pre|a|blockquote)(?:\s[^>]*)?>"
)
TELEGRAM_BOT_PATH_RE = re.compile(r"/bot[^/\s]+/")


class TelegramScope(str, Enum):
    DEFAULT = "default"
    FUNDING = "funding"
    SHADOW = "shadow"


@dataclass(frozen=True)
class NotificationResult:
    status: str
    error: str | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class TelegramCredentials:
    scope: TelegramScope
    token: str | None
    chat_id: str | None
    token_env_var: str
    chat_id_env_var: str
    token_source_env_var: str | None
    chat_id_source_env_var: str | None


def telegram_scope(value: str | TelegramScope | None) -> TelegramScope:
    if isinstance(value, TelegramScope):
        return value
    text = str(value or "default").strip().lower()
    for scope in TelegramScope:
        if scope.value == text:
            return scope
    raise ValueError(f"Unknown Telegram scope: {value}")


def redact_telegram_secret(text: str | None) -> str | None:
    if text is None:
        return None
    return TELEGRAM_BOT_PATH_RE.sub("/bot<redacted>/", str(text))


def resolve_telegram_credentials(
    scope: str | TelegramScope = TelegramScope.DEFAULT,
) -> TelegramCredentials:
    load_env_file()
    resolved_scope = telegram_scope(scope)
    if resolved_scope == TelegramScope.DEFAULT:
        token_envs = ("TELEGRAM_BOT_TOKEN",)
        chat_envs = ("TELEGRAM_CHAT_ID",)
    elif resolved_scope == TelegramScope.FUNDING:
        token_envs = ("FUNDING_TELEGRAM_BOT_TOKEN",)
        chat_envs = ("FUNDING_TELEGRAM_CHAT_ID",)
    else:
        token_envs = (
            "FUNDING_SHADOW_TELEGRAM_BOT_TOKEN",
            "FUNDING_TELEGRAM_BOT_TOKEN",
        )
        chat_envs = (
            "FUNDING_SHADOW_TELEGRAM_CHAT_ID",
            "FUNDING_TELEGRAM_CHAT_ID",
        )

    token = None
    token_source = None
    for env_var in token_envs:
        value = os.environ.get(env_var)
        if value:
            token = value
            token_source = env_var
            break
    chat_id = None
    chat_source = None
    for env_var in chat_envs:
        value = os.environ.get(env_var)
        if value:
            chat_id = value
            chat_source = env_var
            break
    return TelegramCredentials(
        scope=resolved_scope,
        token=token,
        chat_id=chat_id,
        token_env_var=token_envs[0],
        chat_id_env_var=chat_envs[0],
        token_source_env_var=token_source,
        chat_id_source_env_var=chat_source,
    )


class TelegramNotifier:
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        timeout_seconds: int = 10,
        token_env_var: str = "TELEGRAM_BOT_TOKEN",
        chat_id_env_var: str = "TELEGRAM_CHAT_ID",
        fallback_to_default: bool = False,
        scope: str | TelegramScope | None = None,
    ) -> None:
        load_env_file()
        if scope is not None:
            credentials = resolve_telegram_credentials(scope)
            self.token_env_var = credentials.token_env_var
            self.chat_id_env_var = credentials.chat_id_env_var
            self.token = token or credentials.token
            self.chat_id = chat_id or credentials.chat_id
            self.scope = credentials.scope
        else:
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
            self.scope = TelegramScope.DEFAULT
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
            return NotificationResult("failed", redact_telegram_secret(str(exc)))
        if payload.get("ok"):
            return NotificationResult("sent", payload=payload)
        return NotificationResult("failed", redact_telegram_secret(str(payload)))


def telegram_update_chat_ids(
    token: str | None = None,
    timeout_seconds: int = 10,
    scope: str | TelegramScope = TelegramScope.DEFAULT,
) -> list[dict[str, Any]]:
    credentials = resolve_telegram_credentials(scope)
    resolved_token = token or credentials.token
    if not resolved_token:
        return []
    request = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/bot{resolved_token}/getUpdates",
        headers={"User-Agent": "smart-money-radar/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1, int(timeout_seconds))) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
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
