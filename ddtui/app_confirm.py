"""Tool confirmation hook types."""

from __future__ import annotations

from typing import Awaitable, Callable


ToolConfirmHook = Callable[[str, dict], Awaitable[bool]]


async def always_allow(name: str, args: dict) -> bool:
    """Default policy: every tool call is allowed.

    Replace `app.tool_confirm = my_hook` to gate execution on a modal,
    an allowlist, or per-tool rules. Returning False makes the runner
    inject a "Tool execution blocked" tool result instead of invoking
    the function — the model sees this and can adapt.
    """
    return True
