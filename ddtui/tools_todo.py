"""TODO sidebar tool implementation."""

from __future__ import annotations

from .state import ToolContext


def tool_todo_tool(ctx: ToolContext, items: list | None = None) -> str:
    """Return a one-line summary of the TODO state.

    The actual UI rendering (the persistent TodoBlock widget) is wired
    in `AgentApp._run_one_turn` — that's where the running app instance
    lives. This function exists so `execute_tool` can produce the
    message-history string for the model to see.
    """
    items = items or []
    n = len(items)
    n_done = sum(1 for i in items if i.get("status") == "completed")
    n_progress = sum(1 for i in items if i.get("status") == "in_progress")
    n_pending = n - n_done - n_progress
    return (
        f"TODO updated: {n_done} completed, {n_progress} in progress, "
        f"{n_pending} pending (total {n})"
    )
