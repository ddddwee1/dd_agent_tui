"""Tool confirmation: hook type + the write-confirmation policy mixin."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .tools import CONFIRM_TOOLS
from .widgets import ToolConfirmScreen


ToolConfirmHook = Callable[[str, dict], Awaitable[bool]]

# File-mutating tools gated behind the confirmation modal — derived
# from the tool registry (ToolSpec.confirm). Read-only and command
# tools go through the footgun guard (DANGEROUS_SHELL_PATTERNS) instead.
WRITE_CONFIRM_TOOLS = CONFIRM_TOOLS


async def always_allow(name: str, args: dict) -> bool:
    """Trusted-environment policy: every tool call is allowed.

    Wired in when DDTUI_CONFIRM_WRITES=0. Returning False from a hook
    makes the runner inject a "Tool execution blocked" tool result
    instead of invoking the function — the model sees this and can adapt.
    """
    return True


def _clip(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n} chars)"


def _summarize_write_args(name: str, args: dict) -> str:
    """Human-oriented summary of a write-tool call for the modal body."""
    path = args.get("path", "?")
    if name == "write_file":
        content = args.get("content") or ""
        lines = content.splitlines()
        preview = "\n".join(lines[:8])
        more = f"\n…(+{len(lines) - 8} 行)" if len(lines) > 8 else ""
        return (
            f"path: {path}\n"
            f"整文件写入：{len(content)} chars · {len(lines)} 行\n"
            f"───\n{preview}{more}"
        )
    if name == "edit_file":
        old = args.get("old_string") or ""
        new = args.get("new_string") or ""
        scope = "（全部出现）" if args.get("replace_all") else ""
        return f"path: {path}{scope}\n- {_clip(old)}\n+ {_clip(new)}"
    if name == "edit_lines":
        mode = args.get("mode", "?")
        start = args.get("start_line", "?")
        end = args.get("end_line", start)
        return f"path: {path}\nmode: {mode} · lines {start}–{end}"
    if name == "multi_edit":
        edits = args.get("edits") or []
        return f"path: {path}\n{len(edits)} 个替换操作"
    return f"path: {path}"


class AppConfirmMixin:
    """Write-confirmation policy for AgentApp.

    `AgentApp.__init__` points `self.tool_confirm` here when
    config.CONFIRM_WRITES is on. Both the parent loop and subagent
    rounds call the hook, so dialogs are serialized through
    `_confirm_lock` — two concurrent write calls confirm one after the
    other, never as stacked modals.
    """

    async def _confirm_tool_call(self, name: str, args: dict) -> bool:
        if name not in WRITE_CONFIRM_TOOLS:
            return True
        if name in self._confirm_always:
            return True
        async with self._confirm_lock:
            # Re-check after waiting: the confirmation that held the
            # lock may have whitelisted this tool for the session.
            if name in self._confirm_always:
                return True
            fut: asyncio.Future[str] = (
                asyncio.get_running_loop().create_future()
            )

            def _on_close(result: str | None) -> None:
                if not fut.done():
                    fut.set_result(result or "deny")

            screen = ToolConfirmScreen(name, _summarize_write_args(name, args))
            self.push_screen(screen, _on_close)
            try:
                verdict = await fut
            except asyncio.CancelledError:
                # ESC×2 interrupted the turn while the dialog was open —
                # take the dialog down with it. dismiss() re-enters
                # _on_close, where fut.done() blocks the stale result.
                try:
                    screen.dismiss("deny")
                except Exception:
                    pass
                raise
        if verdict == "always":
            self._confirm_always.add(name)
            return True
        return verdict == "allow"
