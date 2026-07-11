"""Conversation-local experiment and evidence ledger.

The ledger is domain-neutral. It binds claims to immutable file hashes,
tracks candidate/fix budgets, and marks performance evidence invalid until
correctness has passed for the same artifact.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import ToolContext, async_task_status
from .tool_utils import _safe_path


_ATTEMPT_TYPES = {"candidate", "fix"}
_KINDS = {"correctness", "performance", "source_gate", "diagnostic", "other"}
_RESULTS = {"pass", "fail", "inconclusive", "skipped"}
_DECISIONS = {"continue", "keep", "rollback", "stop"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(value: Any, limit: int) -> str:
    text = "" if value is None else str(value).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_artifacts(
    ctx: ToolContext, artifacts: list[str] | None
) -> tuple[dict[str, str], str | None]:
    """Resolve and hash task artifacts, returning (hashes, error)."""
    if not artifacts:
        return {}, None
    if not isinstance(artifacts, list) or len(artifacts) > 8:
        return {}, "Error: artifacts must be a list of at most 8 file paths."
    hashes: dict[str, str] = {}
    for raw in artifacts:
        p = _safe_path(ctx.work_dir, str(raw))
        if not p.is_file():
            return {}, f"Error: artifact is not a readable file: {p}"
        try:
            hashes[str(p)] = sha256_file(p)
        except OSError as exc:
            return {}, f"Error hashing artifact {p}: {exc}"
    return hashes, None


def validate_experiment_snapshot(
    ctx: ToolContext, experiment_id: str | None, hashes: dict[str, str]
) -> str | None:
    if not experiment_id:
        return None
    experiment = ctx.experiments.get(experiment_id)
    if experiment is None:
        return f"Error: unknown experiment_id {experiment_id!r}."
    expected_path = experiment["artifact_path"]
    expected_sha = experiment["artifact_sha256"]
    if hashes.get(expected_path) != expected_sha:
        return (
            "Error: task artifact snapshot does not match the experiment. "
            f"Expected {expected_path} sha256={expected_sha}. Start a new "
            "experiment after changing candidate bytes."
        )
    return None


def tool_experiment_start(
    ctx: ToolContext,
    artifact_path: str,
    hypothesis: str,
    boundary: str = "",
    attempt_type: str = "candidate",
    parent_id: str | None = None,
    max_candidates: int = 3,
    max_same_boundary_fixes: int = 2,
) -> str:
    """Start one immutable candidate/fix attempt and snapshot its SHA-256."""
    attempt_type = _clean(attempt_type, 40).lower()
    if attempt_type not in _ATTEMPT_TYPES:
        return "Error: attempt_type must be candidate or fix."
    hypothesis = _clean(hypothesis, 1200)
    boundary = _clean(boundary, 400)
    if not hypothesis:
        return "Error: hypothesis cannot be empty."
    if attempt_type == "fix":
        if not parent_id or parent_id not in ctx.experiments:
            return "Error: a fix requires a valid parent_id."
        if not boundary:
            boundary = ctx.experiments[parent_id].get("boundary") or "unspecified"
    p = _safe_path(ctx.work_dir, artifact_path)
    if not p.is_file():
        return f"Error: artifact is not a readable file: {p}"
    try:
        sha = sha256_file(p)
    except OSError as exc:
        return f"Error hashing artifact {p}: {exc}"

    max_candidates = _bounded_int(max_candidates, 3, 1, 100)
    max_same_boundary_fixes = _bounded_int(max_same_boundary_fixes, 2, 0, 100)
    if ctx.experiments:
        first = next(iter(ctx.experiments.values()))
        max_candidates = _bounded_int(first.get("max_candidates"), 3, 1, 100)
        max_same_boundary_fixes = _bounded_int(
            first.get("max_same_boundary_fixes"), 2, 0, 100
        )
    candidate_number = sum(
        entry["attempt_type"] == "candidate" for entry in ctx.experiments.values()
    )
    fix_number = sum(
        entry["attempt_type"] == "fix" and entry.get("boundary") == boundary
        for entry in ctx.experiments.values()
    )
    if attempt_type == "candidate":
        candidate_number += 1
    else:
        fix_number += 1

    warnings: list[str] = []
    if candidate_number > max_candidates:
        warnings.append(
            f"candidate budget exceeded ({candidate_number}/{max_candidates})"
        )
    if attempt_type == "fix" and fix_number > max_same_boundary_fixes:
        warnings.append(
            "same-boundary fix budget exceeded "
            f"({fix_number}/{max_same_boundary_fixes})"
        )

    experiment_id = ctx.alloc_experiment_id()
    entry = {
        "id": experiment_id,
        "artifact_path": str(p),
        "artifact_sha256": sha,
        "hypothesis": hypothesis,
        "boundary": boundary or "unspecified",
        "attempt_type": attempt_type,
        "parent_id": parent_id,
        "candidate_number": candidate_number,
        "fix_number": fix_number if attempt_type == "fix" else 0,
        "max_candidates": max_candidates,
        "max_same_boundary_fixes": max_same_boundary_fixes,
        "budget_warnings": warnings,
        "records": [],
        "status": "active",
        "created_at": _now(),
        "updated_at": _now(),
    }
    ctx.experiments[experiment_id] = entry
    warning_text = "; ".join(warnings) if warnings else "none"
    return (
        f"Experiment {experiment_id} started\n"
        f"artifact: {p}\n"
        f"artifact_sha256: {sha}\n"
        f"attempt: {attempt_type}\n"
        f"boundary: {entry['boundary']}\n"
        f"candidate_budget: {candidate_number}/{max_candidates}\n"
        f"same_boundary_fix_budget: {fix_number}/{max_same_boundary_fixes}\n"
        f"budget_warning: {warning_text}\n"
        "Bind validation with task_start(experiment_id=..., artifacts=[...])."
    )


def _has_valid_correctness(entry: dict) -> bool:
    for record in reversed(entry["records"]):
        if record["kind"] == "correctness" and record["valid"]:
            return record["result"] == "pass"
    return False


def tool_experiment_record(
    ctx: ToolContext,
    experiment_id: str,
    kind: str,
    result: str,
    task_id: str | None = None,
    command: str | None = None,
    evidence: str | None = None,
    metric_name: str | None = None,
    metric_value: float | int | str | None = None,
    decision: str = "continue",
) -> str:
    """Record evidence, explicitly flagging artifact or ordering drift."""
    entry = ctx.experiments.get(experiment_id)
    if entry is None:
        return f"Error: unknown experiment_id {experiment_id!r}."
    kind = _clean(kind, 40).lower()
    result = _clean(result, 40).lower()
    decision = _clean(decision, 40).lower()
    if kind not in _KINDS:
        return (
            "Error: invalid kind; use correctness/performance/source_gate/"
            "diagnostic/other."
        )
    if result not in _RESULTS:
        return "Error: result must be pass/fail/inconclusive/skipped."
    if decision not in _DECISIONS:
        return "Error: decision must be continue/keep/rollback/stop."

    invalid_reasons: list[str] = []
    artifact_path = Path(entry["artifact_path"])
    try:
        current_sha = sha256_file(artifact_path) if artifact_path.is_file() else None
    except OSError:
        current_sha = None
    if current_sha != entry["artifact_sha256"]:
        invalid_reasons.append("artifact bytes changed after experiment_start")

    task_status = None
    task_return_code = None
    task_duration_sec = None
    task_output_path = None
    task_status_path = None
    task_artifacts: dict[str, str] = {}
    task_command = _clean(command, 4000)
    if task_id:
        task = ctx.tasks.get(task_id)
        if task is None:
            return f"Error: unknown task_id {task_id!r}."
        task_status, task_return_code, task_duration_sec = async_task_status(task)
        if task_status == "running":
            return f"Error: task {task_id} is still running; record after completion."
        task_command = task.command
        task_artifacts = dict(task.artifact_hashes)
        task_output_path = str(task.output_path)
        task_status_path = str(task.status_path)
        if task.experiment_id != experiment_id:
            invalid_reasons.append("task was not bound to this experiment_id")
        if task_artifacts.get(entry["artifact_path"]) != entry["artifact_sha256"]:
            invalid_reasons.append("task artifact SHA does not match experiment SHA")
        if task_status == "killed" and result != "inconclusive":
            invalid_reasons.append(
                f"task status is {task_status} (return_code {task_return_code})"
            )
        elif result == "pass" and task_status != "success":
            invalid_reasons.append(
                f"pass claim conflicts with task status {task_status} "
                f"(return_code {task_return_code})"
            )
    elif not task_command:
        invalid_reasons.append("no task_id or command was recorded")

    if kind in {"correctness", "performance"} and not task_id:
        invalid_reasons.append(
            f"{kind} requires a completed artifact-bound task_id"
        )

    if kind == "performance" and not _has_valid_correctness(entry):
        invalid_reasons.append(
            "performance is not valid before correctness passes for this SHA"
        )
    valid = not invalid_reasons
    record = {
        "kind": kind,
        "result": result,
        "valid": valid,
        "invalid_reasons": invalid_reasons,
        "task_id": task_id,
        "task_status": task_status,
        "task_return_code": task_return_code,
        "task_duration_sec": task_duration_sec,
        "task_output_path": task_output_path,
        "task_status_path": task_status_path,
        "task_artifact_hashes": task_artifacts,
        "command": task_command,
        "evidence": _clean(evidence, 4000),
        "metric_name": _clean(metric_name, 200),
        "metric_value": metric_value,
        "decision": decision,
        "artifact_sha256": entry["artifact_sha256"],
        "recorded_at": _now(),
    }
    if valid and kind in {"correctness", "source_gate"} and result != "pass":
        for prior in entry["records"]:
            if prior["kind"] == "performance" and prior["valid"]:
                prior["valid"] = False
                prior["invalid_reasons"].append(
                    f"invalidated by later {kind} evidence"
                )
        entry["status"] = "invalidated"
    entry["records"].append(record)
    entry["updated_at"] = _now()
    if decision == "keep":
        entry["status"] = "kept"
    elif decision == "rollback":
        entry["status"] = "rolled_back"
    elif decision == "stop":
        entry["status"] = "stopped"
    reason_text = "; ".join(invalid_reasons) if invalid_reasons else "none"
    return (
        f"Experiment evidence recorded: {experiment_id}\n"
        f"artifact_sha256: {entry['artifact_sha256']}\n"
        f"kind: {kind}\n"
        f"result: {result}\n"
        f"valid: {str(valid).lower()}\n"
        f"invalid_reason: {reason_text}\n"
        f"task_id: {task_id or '-'}\n"
        f"decision: {decision}"
    )


def tool_experiment_status(
    ctx: ToolContext, experiment_id: str | None = None
) -> str:
    """Return a compact ledger summary suitable for handoff/compaction."""
    if experiment_id:
        entries = [ctx.experiments.get(experiment_id)]
        if entries[0] is None:
            return f"Error: unknown experiment_id {experiment_id!r}."
    else:
        entries = list(ctx.experiments.values())
    if not entries:
        return "No experiments recorded."
    lines = [f"Experiment ledger: {len(entries)} attempt(s)"]
    for entry in entries:
        valid_correctness = _has_valid_correctness(entry)
        valid_performance = any(
            record["kind"] == "performance" and record["valid"]
            for record in entry["records"]
        )
        lines.append(
            f"- {entry['id']} [{entry['status']}] {entry['attempt_type']} "
            f"sha256={entry['artifact_sha256']} boundary={entry['boundary']}; "
            f"records={len(entry['records'])}, correctness={valid_correctness}, "
            f"performance={valid_performance}"
        )
        for record in entry["records"]:
            metric = ""
            if record["metric_name"]:
                metric = f" {record['metric_name']}={record['metric_value']}"
            invalid = ""
            if not record["valid"]:
                invalid = f" reason={'; '.join(record['invalid_reasons'])}"
            lines.append(
                f"  - {record['kind']} {record['result']} "
                f"valid={record['valid']} task={record['task_id'] or '-'}"
                f"{metric}{invalid}"
            )
        for warning in entry["budget_warnings"]:
            lines.append(f"  - budget_warning: {warning}")
    return "\n".join(lines)
