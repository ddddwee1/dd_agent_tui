"""Formatting helpers for exceptions shown in the conversation UI."""

from __future__ import annotations

import json
import traceback

from rich.text import Text
from textual.widgets import Collapsible, Static


def _one_line(text: str, limit: int = 180) -> str:
    """Collapse whitespace and truncate for widget titles."""
    text = " ".join(text.strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _exception_extra(exc: BaseException) -> list[str]:
    """Best-effort extraction of provider / HTTP error fields.

    OpenAI/httpx exceptions often carry useful data on attributes rather
    than in ``str(exc)``. Pull those into the visible error body so the
    TUI doesn't show a bare ``API error:`` when the exception message is
    empty or too terse.
    """
    lines: list[str] = []
    for attr in ("status_code", "code", "request_id"):
        value = getattr(exc, attr, None)
        if value not in (None, ""):
            lines.append(f"{attr}: {value}")

    request = getattr(exc, "request", None)
    if request is not None:
        method = getattr(request, "method", "") or ""
        url = getattr(request, "url", "") or ""
        if method or url:
            lines.append(f"request: {method} {url}".strip())

    body = getattr(exc, "body", None)
    if body not in (None, ""):
        try:
            body_text = json.dumps(body, ensure_ascii=False, indent=2)
        except Exception:
            body_text = str(body)
        lines.append("body:\n" + body_text[:4000])

    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        if status is not None and not any(l.startswith("status_code:") for l in lines):
            lines.append(f"status_code: {status}")
        text = getattr(response, "text", None)
        if text:
            lines.append("response text:\n" + str(text)[:4000])

    return lines


def _format_exception_for_ui(exc: BaseException) -> tuple[str, str]:
    exc_type = type(exc).__name__
    message = str(exc).strip()
    summary = _one_line(f"{exc_type}: {message or repr(exc)}")

    tb = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).strip()
    detail_parts = [
        "本轮已回滚，下面是原始异常信息。",
        "这不一定是 API 本身失败，也可能是工具、provider 转换或 UI 处理异常。",
    ]
    extra = _exception_extra(exc)
    if extra:
        detail_parts.extend(["", "附加信息:", *extra])
    detail_parts.extend(["", "Traceback:", tb or f"{exc_type}: {message or repr(exc)}"])
    return summary, "\n".join(detail_parts)


def _exception_block(title: str, exc: BaseException) -> Collapsible:
    summary, detail = _format_exception_for_ui(exc)
    return Collapsible(
        Static(Text(detail, style="#f92672"), markup=False),
        title=f"❌ {title} · {summary}".replace("[", "\\["),
        collapsed=False,
    )
