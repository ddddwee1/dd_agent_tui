"""Conversation-local checkpoint tools."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .history_archive import open_archive
from .runtime_state import read_json, task_registry_path
from .state import ToolContext, async_task_status


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
_STAGES = {"implement", "debug", "optimize", "deliver"}
_LIST_FIELDS = (
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


def evidence_refs(ctx: ToolContext, refs: list[str] | None) -> list[str]:
    """Check evidence provenance and available artifact identities, not causality."""
    if refs is None:
        return []
    if not isinstance(refs, list) or len(refs) > 8:
        raise ValueError("evidence_refs must contain at most eight task-N, exp-N or msg-SHA references")
    for ref in refs:
        if not isinstance(ref, str):
            raise ValueError("evidence references must be strings")
        artifacts = {}
        if re.fullmatch(r"task-\d+", ref):
            task = ctx.tasks.get(ref)
            if task:
                if async_task_status(task)[0] == "running":
                    raise ValueError(f"{ref} has no completed result yet")
                output = task.output_path
                artifacts = task.artifact_hashes
            else:
                saved = read_json(task_registry_path(ctx.session_id or "default", ref)) or {}
                status = read_json(Path(saved["status_path"])) if saved.get("status_path") else None
                if not status or status.get("status") not in {"success", "failed", "killed"}:
                    raise ValueError(f"{ref} has no available completed result")
                output = Path(saved["output_path"])
                artifacts = saved.get("artifact_hashes") or {}
            if not output.is_file():
                raise ValueError(f"{ref} output is unavailable")
        elif re.fullmatch(r"exp-\d+", ref):
            experiment = ctx.experiments.get(ref) or {}
            record = next((item for item in reversed(experiment.get("records", []))
                           if item.get("valid") and item.get("task_id")), None)
            if record is None:
                raise ValueError(f"{ref} has no valid task-bound observation")
            evidence_refs(ctx, [record["task_id"]])
            artifacts = (experiment.get("source_snapshot") or {}).get("files") or {}
        elif re.fullmatch(r"msg-[0-9a-f]{64}", ref):
            try:
                with closing(open_archive(ctx.session_id)) as db:
                    row = db.execute("SELECT role FROM messages WHERE ref = ?", (ref,)).fetchone()
            except (OSError, ValueError, sqlite3.Error) as exc:
                raise ValueError(f"{ref} is unavailable: {exc}") from exc
            if not row or row[0] != "tool":
                raise ValueError(f"{ref} must reference an archived tool observation, not an assistant claim")
        else:
            raise ValueError(f"unknown evidence reference {ref!r}; use task-N, exp-N or msg-SHA")
        for filename, expected in artifacts.items():
            try:
                digest = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
            except OSError as exc:
                raise ValueError(f"{ref} artifact is unavailable: {filename}") from exc
            if digest != expected:
                raise ValueError(f"{ref} artifact changed: {filename}; keep the hypothesis open or cite a current observation")
    return list(dict.fromkeys(refs))


def _hypotheses(ctx: ToolContext, value: Any) -> list[dict]:
    if isinstance(value, str):
        value = _clean_list(value)
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("hypotheses must contain at most twenty entries")
    result, ids = [], set()
    for item in value:
        if isinstance(item, str):
            item = {"id": "hyp-" + hashlib.sha256(item.encode()).hexdigest()[:12], "claim": item}
        if not isinstance(item, dict):
            raise ValueError("each hypothesis must be an object or legacy string")
        entry = {key: _clean_text(item.get(key), limit=800) for key in ("id", "claim", "scope", "test")}
        status = item.get("status", "open")
        if not re.fullmatch(r"[\w-]{1,64}", entry["id"]) or entry["id"] in ids or not entry["claim"]:
            raise ValueError("each hypothesis needs a unique id and nonempty claim")
        if status not in {"open", "supported", "refuted"}:
            raise ValueError("hypothesis status must be open, supported or refuted")
        refs = item.get("evidence_refs") or []
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs) or len(refs) > 8:
            raise ValueError("hypothesis evidence_refs must be a list of at most eight references")
        if status != "open":
            if not entry["scope"] or not entry["test"] or not refs:
                raise ValueError("supported/refuted hypotheses need scope, a discriminating test and evidence_refs")
            refs = evidence_refs(ctx, refs)
        result.append({**entry, "status": status, "evidence_refs": refs})
        ids.add(entry["id"])
    return result


def request_stage_review(ctx: ToolContext, reason: str) -> None:
    cp = ctx.checkpoint
    if cp and cp.get("stage") and cp.get("status") not in {"done", "abandoned"}:
        cp["stage_review_required"] = True
        cp["stage_review_reason"] = reason


def workflow_card(ctx: ToolContext) -> str:
    cp = ctx.checkpoint or {}
    if cp.get("status") in {"done", "abandoned"}:
        return ""
    hypotheses = cp.get("hypotheses") or []
    if not cp.get("stage") and not hypotheses:
        return ""
    state = {key: cp.get(key) for key in ("stage", "skill_ref", "workflow_ref", "stage_review_required", "stage_review_reason")}
    state["hypotheses"] = [
        {key: item.get(key) for key in ("id", "claim", "scope", "test", "status", "evidence_refs")}
        if isinstance(item, dict) else {"claim": str(item), "status": "open"}
        for item in hypotheses
    ]
    review = ("Review the current result and applicable owner, then confirm the stage with "
              "checkpoint_tool(stage=..., workflow_ref=...) before the next managed task. "
              if cp.get("stage_review_required") else "The recorded stage is confirmed; choose the next action from the current evidence. ")
    if not cp.get("stage"):
        review = "No workflow is registered; these are the current hypothesis assessments. "
    return ("[Workflow checkpoint]\n" + json.dumps(state, ensure_ascii=False) + "\n" + review +
            "Reuse sufficient material already in context; no query tool is mandatory. "
            "Task completion/exit zero is not numerical acceptance or a stage transition. "
            "Hypothesis status is a scoped model assessment with checked provenance, not an automatic root-cause verdict. "
            "Summaries and confidence labels cannot promote an open hypothesis into an observed fact.")


def checkpoint_feedback(ctx: ToolContext, messages) -> str | None:
    recovery = next((m for m in reversed(messages) if m.get("ddtui_kind") == "context_recovery"), None)
    if recovery:
        key = recovery.get("history_batch") or recovery.get("ddtui_timestamp")
        if key != ctx.checkpoint_recovery_key:
            request_stage_review(ctx, "Context restored; reconcile the current stage and unresolved hypotheses.")
            ctx.checkpoint_recovery_key = key
    cp = ctx.checkpoint or {}
    if cp.get("stage") and cp.get("workflow_ref"):
        try:
            current = hashlib.sha256(Path(cp["workflow_ref"].split("#", 1)[0]).read_bytes()).hexdigest()
        except OSError:
            current = None
        if current != cp.get("workflow_sha256"):
            request_stage_review(ctx, "The workflow owner changed or is unavailable; select the applicable current owner.")
    card = workflow_card(ctx)
    if not card or card == ctx.checkpoint_notice:
        return None
    ctx.checkpoint_notice = card
    return card


def checkpoint_task_gate(ctx: ToolContext) -> str | None:
    cp = ctx.checkpoint or {}
    if cp.get("status") in {"done", "abandoned"}:
        return None
    finished = [ref for ref in {*cp.get("active_refs", []), *cp.get("workflow_tasks", [])}
                if ref in ctx.tasks and async_task_status(ctx.tasks[ref])[0] != "running"]
    if finished:
        prune_checkpoint_refs(ctx, finished)
    if (ctx.checkpoint or {}).get("stage") and ctx.checkpoint.get("stage_review_required"):
        return "Error: stage review is required before starting another managed task.\n" + workflow_card(ctx)
    return None


def track_checkpoint_task(ctx: ToolContext, task_id: str) -> None:
    if ctx.checkpoint and ctx.checkpoint.get("stage") and ctx.checkpoint.get("status") not in {"done", "abandoned"}:
        ctx.checkpoint["workflow_tasks"] = [*ctx.checkpoint.get("workflow_tasks", []), task_id]


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
    for key in ("stage", "skill_ref", "workflow_ref", "workflow_tasks", "stage_review_required", "stage_review_reason"):
        if key in cp:
            lines.append(f"{key}: {cp[key]}")
    for hypothesis in cp.get("hypotheses") or []:
        lines.append("hypothesis: " + json.dumps(hypothesis, ensure_ascii=False) if isinstance(hypothesis, dict)
                     else f"hypothesis [open]: {hypothesis}")
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
    hypotheses: list[dict | str] | str | None = None,
    evidence: list[str] | str | None = None,
    decisions: list[str] | str | None = None,
    blockers: list[str] | str | None = None,
    next_steps: list[str] | str | None = None,
    active_refs: list[str] | str | None = None,
    files: list[str] | str | None = None,
    confidence: str = "medium",
    note: str | None = None,
    stage: str | None = None,
    workflow_ref: str | None = None,
    skill_ref: str | None = None,
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
    checkpoint_task_gate(ctx)
    previous = ctx.checkpoint or {}
    workflow = {key: previous[key] for key in ("stage", "workflow_ref", "workflow_sha256", "skill_ref", "workflow_tasks", "stage_review_required", "stage_review_reason") if key in previous}
    if workflow.get("stage") and goal != previous.get("goal"):
        workflow.update(stage_review_required=True, stage_review_reason="The goal changed; reconcile the stage and applicable workflow.")
    try:
        if stage is not None or workflow_ref is not None:
            if not isinstance(stage, str) or stage not in _STAGES or not isinstance(workflow_ref, str) or not workflow_ref:
                raise ValueError("confirm both stage (implement/debug/optimize/deliver) and workflow_ref together")
            path, sep, anchor = workflow_ref.partition("#")
            owner = Path(path).expanduser()
            if not owner.is_absolute():
                owner = Path(ctx.work_dir) / owner
            owner = owner.resolve()
            workflow.update(stage=stage, workflow_ref=str(owner) + (sep + anchor if sep else ""),
                            workflow_sha256=hashlib.sha256(owner.read_bytes()).hexdigest(),
                            stage_review_required=False, stage_review_reason="")
        if skill_ref is not None:
            workflow["skill_ref"] = _clean_text(skill_ref, 1000)
        cleaned_hypotheses = (_hypotheses(ctx, hypotheses) if hypotheses is not None
                              else previous.get("hypotheses", []))
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"
    checkpoint = {
        **workflow,
        "goal": goal,
        "status": status,
        "summary": summary,
        "current_focus": _clean_text(current_focus, limit=1000),
        "hypotheses": cleaned_hypotheses,
        "evidence": _clean_list(evidence),
        "decisions": _clean_list(decisions),
        "blockers": _clean_list(blockers),
        "next_steps": _clean_list(next_steps),
        "active_refs": _clean_list(active_refs) if active_refs is not None else previous.get("active_refs", []),
        "files": _clean_list(files),
        "confidence": confidence,
        "note": _clean_text(note, limit=1500),
        "updated_at": _now(),
    }
    ctx.checkpoint = checkpoint
    card = workflow_card(ctx)
    ctx.checkpoint_notice = card
    n_next = len(checkpoint["next_steps"])
    n_blockers = len(checkpoint["blockers"])
    return (
        "Checkpoint updated.\n"
        f"status: {status}\n"
        f"confidence: {confidence}\n"
        f"goal: {goal}\n"
        f"next_steps: {n_next}\n"
        f"blockers: {n_blockers}"
        + ("\n" + card if card else "")
    )


def tool_checkpoint_clear(ctx: ToolContext, reason: str | None = None) -> str:
    """Clear the current conversation checkpoint."""
    had_checkpoint = bool(ctx.checkpoint)
    ctx.checkpoint = None
    ctx.checkpoint_notice = ""
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

    Retain registered workflow and hypothesis state when waiting ends.
    Legacy waiting-only checkpoints can be cleared with their last ref.
    """
    cp = ctx.checkpoint
    if not cp:
        return {"changed": False, "cleared": False, "removed": []}
    active_refs = [str(x) for x in cp.get("active_refs") or []]
    workflow_tasks = cp.get("workflow_tasks") or []
    completed = {str(x) for x in completed_refs if str(x)}
    if not completed:
        return {"changed": False, "cleared": False, "removed": []}
    remaining = [ref for ref in active_refs if ref not in completed]
    removed = sorted((set(active_refs) | set(workflow_tasks)) & completed)
    if not removed:
        return {"changed": False, "cleared": False, "removed": []}
    if cp.get("status") == "waiting" and not remaining and not cp.get("stage") and not cp.get("hypotheses"):
        ctx.checkpoint = None
        return {"changed": True, "cleared": True, "removed": removed}
    updated = dict(cp)
    updated["active_refs"] = remaining
    updated["workflow_tasks"] = [ref for ref in workflow_tasks if ref not in completed]
    if updated.get("status") == "waiting" and not remaining and not updated["workflow_tasks"]:
        updated["status"] = "in_progress"
    updated["updated_at"] = _now()
    ctx.checkpoint = updated
    request_stage_review(ctx, "Related tasks completed: " + ", ".join(removed) + "; inspect their actual results before choosing the next stage.")
    return {"changed": True, "cleared": False, "removed": removed}


def tool_checkpoint_get(ctx: ToolContext) -> str:
    """Return the current conversation checkpoint."""
    if not ctx.checkpoint:
        return "No checkpoint set."
    return _format_checkpoint(ctx.checkpoint)
