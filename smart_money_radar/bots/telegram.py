from __future__ import annotations

from html import escape as html_escape
from typing import Any


def tg(value: Any) -> str:
    if value is None:
        return "-"
    return html_escape(str(value), quote=False)


def tg_attr(value: Any) -> str:
    if value is None:
        return ""
    return html_escape(str(value), quote=True)


def fmt_money(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def fmt_signed(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"+${amount:.2f}" if amount >= 0 else f"-${abs(amount):.2f}"


def fmt_rate(value: Any) -> str:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{rate * 10_000:.3f} bps/h"


def fmt_seconds(value: Any) -> str:
    if value is None:
        return "-"
    seconds = max(0, int(float(value)))
    minutes, rest = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {rest}s"


def fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


def shorten(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
