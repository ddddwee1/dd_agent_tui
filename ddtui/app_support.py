"""Small helpers shared by the TUI app mixins."""

from __future__ import annotations

import platform
import subprocess
import time
from pathlib import Path

from .config import (
    AGENTS_MD_FILENAME,
    AGENTS_MD_MAX_CHARS,
    COMPACT_TOOL_SNIPPET_CHARS,
)
from .providers import ToolCallDelta


def _git_snapshot(work_dir: str) -> str | None:
    """Current branch name for the env block, or None outside a repo.

    Best-effort with a short timeout — a hung/absent git must not delay
    startup. Branch only, no dirty state: the agent can always run
    `git status` itself for the live picture.
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    branch = r.stdout.strip()
    return f"分支 {branch}" if branch else None


def build_env_block(work_dir: str, provider_label: str, model: str) -> str:
    """Render the "# 环境" system-prompt section at session start.

    Gives the model the facts it would otherwise have to probe for (or
    hallucinate): OS, cwd, git state, date, and what model it is running
    as. Values are a startup snapshot, not live state.
    """
    lines = [
        f"- 操作系统：{platform.system()} {platform.release()}（{platform.machine()}）",
        f"- 当前目录（后续命令的默认 workdir）：{work_dir}",
    ]
    git = _git_snapshot(work_dir)
    lines.append(f"- Git：{git}" if git else "- Git：当前目录不是 git 仓库")
    lines.append(f"- 今天日期：{time.strftime('%Y-%m-%d')}")
    lines.append(f"- Provider / 模型：{provider_label} / {model}")
    return "# 环境（会话启动时的快照）\n" + "\n".join(lines)


def _render_history_for_summary(messages: list) -> str:
    """Flatten a slice of message history to plain text for the
    summarizer prompt. Drops reasoning_content (only useful at the time
    it was produced) and truncates tool results. Ordinary system messages
    are skipped because leading system prompts are preserved, but
    non-leading exploration summaries need to survive later compaction."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user":
            lines.append(f"=== USER ===\n{content}")
        elif role == "system" and m.get("ddtui_kind") == "explore_summary":
            lines.append(f"=== EXPLORE SUMMARY ===\n{content}")
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
        # Other system messages are intentionally skipped — leading
        # system prompts are preserved across compaction.
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
