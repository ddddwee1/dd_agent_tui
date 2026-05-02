"""Small helpers shared by the TUI app mixins."""

from __future__ import annotations

from pathlib import Path

from .config import (
    AGENTS_MD_FILENAME,
    AGENTS_MD_MAX_CHARS,
    COMPACT_TOOL_SNIPPET_CHARS,
)
from .providers import ToolCallDelta


def _render_history_for_summary(messages: list) -> str:
    """Flatten a slice of message history to plain text for the
    summarizer prompt. Drops reasoning_content (only useful at the time
    it was produced) and truncates tool results."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user":
            lines.append(f"=== USER ===\n{content}")
        elif role == "assistant":
            block = ["=== ASSISTANT ==="]
            if content:
                block.append(content)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name", "?")
                args_str = (fn.get("arguments") or "")[:200]
                block.append(f"  [tool_call] {name}({args_str})")
            lines.append("\n".join(block))
        elif role == "tool":
            snippet = content[:COMPACT_TOOL_SNIPPET_CHARS]
            if len(content) > COMPACT_TOOL_SNIPPET_CHARS:
                snippet += " …[truncated]"
            lines.append(f"=== TOOL RESULT ===\n{snippet}")
        # role == "system" intentionally skipped — system messages are
        # preserved across the compaction, so they don't belong in the
        # "what happened" recap.
    return "\n\n".join(lines)


def _merge_tool_call_delta(
    tool_calls: dict[int, dict], delta: ToolCallDelta
) -> None:
    slot = tool_calls.setdefault(
        delta.index,
        {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if delta.id:
        slot["id"] = delta.id
    if delta.type:
        slot["type"] = delta.type
    if delta.name:
        slot["function"]["name"] += delta.name
    if delta.arguments:
        slot["function"]["arguments"] += delta.arguments


def load_agents_md(cwd: str) -> str | None:
    """Read `<cwd>/AGENTS.md` and return its content (or None if absent).

    AGENTS.md hosts project-specific guidance for the agent — coding
    conventions, do-not-touch areas, deployment quirks, etc. Loaded
    once at startup; restart the app (or Ctrl+L preserves system
    messages so a re-launch isn't required for in-session reset) to
    pick up changes.

    Truncated at AGENTS_MD_MAX_CHARS to keep the system prompt bounded.
    """
    p = Path(cwd) / AGENTS_MD_FILENAME
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not text:
        return None
    if len(text) > AGENTS_MD_MAX_CHARS:
        text = (
            text[:AGENTS_MD_MAX_CHARS]
            + f"\n\n…[truncated at {AGENTS_MD_MAX_CHARS} chars]"
        )
    return text
