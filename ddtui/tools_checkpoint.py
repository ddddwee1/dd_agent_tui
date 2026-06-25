"""Conversation-local checkpoint tools."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .state import ToolContext


_STATUSES = {
    "planning",
    "in_progress",
    "waiting",
    "blocked",
    "ready_for_review",
    "done",
    "abandoned",
}
_CONFIDENCE = {"low", "medium", "high"}
_LIST_FIELDS = (
    "hypotheses",
    "evidence",
    "decisions",
    "blockers",
    "next_steps",
    "active_refs",
    "files",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_text(value: Any, limit: int = 4000) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > limit:
        text = text[:limit] + f"\n...[+{len(text) - limit} chars]"
    return text


def _clean_list(value: Any, limit: int = 20) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [line.strip() for line in value.splitlines()]
    elif isinstance(value, list):
        raw = value
    else:
        raw = [value]
    out: list[str] = []
    for item in raw:
        text = _clean_text(item, limit=800)
        if not text:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _format_checkpoint(cp: dict) -> str:
    lines = [
        "Current checkpoint:",
        f"status: {cp.get('status')}",
        f"confidence: {cp.get('confidence')}",
        f"updated_at: {cp.get('updated_at')}",
        f"goal: {cp.get('goal')}",
        f"summary: {cp.get('summary')}",
    ]
    if cp.get("current_focus"):
        lines.append(f"current_focus: {cp.get('current_focus')}")
    for field in _LIST_FIELDS:
        items = cp.get(field) or []
        if not items:
            continue
        lines.append(f"{field}:")
        for item in items:
            lines.append(f"- {item}")
    if cp.get("note"):
        lines.append(f"note: {cp.get('note')}")
    return "\n".join(lines)


def tool_checkpoint_tool(
    ctx: ToolContext,
    goal: str,
    status: str,
    summary: str,
    current_focus: str | None = None,
    hypotheses: list[str] | str | None = None,
    evidence: list[str] | str | None = None,
    decisions: list[str] | str | None = None,
    blockers: list[str] | str | None = None,
    next_steps: list[str] | str | None = None,
    active_refs: list[str] | str | None = None,
    files: list[str] | str | None = None,
    confidence: str = "medium",
    note: str | None = None,
) -> str:
    """Replace the current conversation checkpoint."""
    goal = _clean_text(goal, limit=1000)
    summary = _clean_text(summary, limit=3000)
    status = _clean_text(status, limit=80).lower()
    confidence = _clean_text(confidence, limit=80).lower()
    if not goal:
        return "Error: goal cannot be empty."
    if not summary:
        return "Error: summary cannot be empty."
    if status not in _STATUSES:
        return (
            "Error: status must be one of "
            + ", ".join(sorted(_STATUSES))
            + "."
        )
    if confidence not in _CONFIDENCE:
        return (
            "Error: confidence must be one of "
            + ", ".join(sorted(_CONFIDENCE))
            + "."
        )
    checkpoint = {
        "goal": goal,
        "status": status,
        "summary": summary,
        "current_focus": _clean_text(current_focus, limit=1000),
        "hypotheses": _clean_list(hypotheses),
        "evidence": _clean_list(evidence),
        "decisions": _clean_list(decisions),
        "blockers": _clean_list(blockers),
        "next_steps": _clean_list(next_steps),
        "active_refs": _clean_list(active_refs),
        "files": _clean_list(files),
        "confidence": confidence,
        "note": _clean_text(note, limit=1500),
        "updated_at": _now(),
    }
    ctx.checkpoint = checkpoint
    n_next = len(checkpoint["next_steps"])
    n_blockers = len(checkpoint["blockers"])
    return (
        "Checkpoint updated.\n"
        f"status: {status}\n"
        f"confidence: {confidence}\n"
        f"goal: {goal}\n"
        f"next_steps: {n_next}\n"
        f"blockers: {n_blockers}"
    )


def tool_checkpoint_clear(ctx: ToolContext, reason: str | None = None) -> str:
    """Clear the current conversation checkpoint."""
    had_checkpoint = bool(ctx.checkpoint)
    ctx.checkpoint = None
    reason = _clean_text(reason, limit=1000)
    if not had_checkpoint:
        return "No checkpoint set."
    if reason:
        return f"Checkpoint cleared.\nreason: {reason}"
    return "Checkpoint cleared."


def prune_checkpoint_refs(
    ctx: ToolContext, completed_refs: list[str]
) -> dict[str, Any]:
    """Remove completed task refs from the current checkpoint.

    Returns a small change report. If a waiting checkpoint was only
    waiting on these refs, it is cleared entirely so the sidebar does
    not keep stale state around.
    """
    cp = ctx.checkpoint
    if not cp:
        return {"changed": False, "cleared": False, "removed": []}
    active_refs = [str(x) for x in cp.get("active_refs") or []]
    completed = {str(x) for x in completed_refs if str(x)}
    if not active_refs or not completed:
        return {"changed": False, "cleared": False, "removed": []}
    remaining = [ref for ref in active_refs if ref not in completed]
    removed = [ref for ref in active_refs if ref in completed]
    if not removed:
        return {"changed": False, "cleared": False, "removed": []}
    if cp.get("status") == "waiting" and not remaining:
        ctx.checkpoint = None
        return {"changed": True, "cleared": True, "removed": removed}
    updated = dict(cp)
    updated["active_refs"] = remaining
    updated["updated_at"] = _now()
    ctx.checkpoint = updated
    return {"changed": True, "cleared": False, "removed": removed}


def tool_checkpoint_get(ctx: ToolContext) -> str:
    """Return the current conversation checkpoint."""
    if not ctx.checkpoint:
        return "No checkpoint set."
    return _format_checkpoint(ctx.checkpoint)
