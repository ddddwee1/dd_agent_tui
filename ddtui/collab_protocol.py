"""Versioned, provider-independent Git handoff messages."""

from __future__ import annotations

import json
import re
import math
import hashlib
from pathlib import PurePosixPath


MAX_MESSAGE_BYTES = 128 * 1024
REQUEST_REF = "refs/heads/codex/collab-requests"
ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
PRIVATE_PARTS = {".git", ".ssh", ".secrets", "private", "tokens", "credentials"}
SENSITIVE_TEXT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?<![\w./-])(?:/Users/|/home/|/data/)[^\s\"']+|"
    r"(?i:(?:api[_-]?key|access[_-]?token|password|secret)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_/-]{12,}[\"'])|"
    r"\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,})"
)


class CollabError(ValueError):
    """An actionable handoff error, safe to show without a traceback."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise CollabError("IDs must contain 1-64 letters, digits, underscores or hyphens")
    return value


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CollabError("Expected a repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise CollabError("Paths must be relative and cannot contain dot components")
    if any(p in PRIVATE_PARTS or p == ".env" or p.startswith(".env.") for p in path.parts):
        raise CollabError("Private and credential paths cannot enter a handoff")
    return str(path)


def allowed_path(path: str, scopes: list[str]) -> bool:
    relative_path(path)
    return any(path == scope or path.startswith(scope + "/") for scope in scopes)


def shared_text(data: bytes):
    if b"\0" in data:
        raise CollabError("Only text source may enter the default handoff")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise CollabError("Handoff source must be UTF-8 text") from error
    if SENSITIVE_TEXT.search(text):
        raise CollabError("Private paths or credential material detected; review before publishing")


def encoded(payload: dict) -> bytes:
    value = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    if len(value) > MAX_MESSAGE_BYTES:
        raise CollabError("Handoff message exceeds 128 KiB; keep raw evidence on its machine")
    return value


def result_id(worker_id: str, result: dict) -> str:
    """Stable across receipt/status commits; only immutable result content counts."""
    return hashlib.sha256(encoded({"worker_id": identifier(worker_id), "result": result})).hexdigest()


def request_message(payload: dict) -> dict:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise CollabError("Expected a schema_version=1 task object")
    task_id = identifier(payload.get("task_id"))
    worker = identifier(payload.get("worker_id"))
    revision = payload.get("revision")
    if type(revision) is not int or revision < 1:
        raise CollabError("revision must be a positive integer")
    source = payload.get("source_commit", "")
    if not isinstance(source, str) or not SHA.fullmatch(source):
        raise CollabError("source_commit must be a full Git commit ID")
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths or len(paths) > 128:
        raise CollabError("paths must explicitly list 1-128 shared files or directories")
    paths = list(dict.fromkeys(relative_path(p) for p in paths))
    instructions = payload.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise CollabError("instructions must be nonempty")
    state = payload.get("state", "active")
    if state not in ("active", "retired"):
        raise CollabError("state must be active or retired")
    priority = payload.get("priority", 0)
    if type(priority) is not int or not -100 <= priority <= 100:
        raise CollabError("priority must be an integer between -100 and 100")
    dependencies = payload.get("depends_on", [])
    if not isinstance(dependencies, list) or task_id in dependencies:
        raise CollabError("depends_on must list other task IDs")
    dependencies = [identifier(p) for p in dependencies]
    result = {"schema_version": 1, "task_id": task_id, "worker_id": worker,
              "revision": revision, "source_commit": source, "paths": paths,
              "instructions": instructions, "state": state, "priority": priority,
              "depends_on": dependencies}
    encoded(result)
    return result


def result_message(payload: dict, request: dict, commit: str) -> dict:
    if not isinstance(payload, dict):
        raise CollabError("The result must be an object")
    if payload.get("task_id") != request["task_id"] or payload.get("revision") != request["revision"]:
        raise CollabError("The result does not match the active task and revision")
    if payload.get("status") not in ("completed", "blocked", "failed"):
        raise CollabError("Result status must be completed, blocked or failed")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
        raise CollabError("A result needs a nonempty summary of at most 4000 characters")
    evidence = payload.get("evidence", [])
    if not isinstance(evidence, list) or len(evidence) > 64:
        raise CollabError("evidence must contain at most 64 relative evidence paths")
    evidence = [relative_path(p) for p in evidence]
    if payload["status"] == "completed" and not evidence:
        raise CollabError("completed requires evidence; an assistant answer alone is not completion")
    def check_metrics(value, depth=0):
        if depth > 6:
            raise CollabError("Metrics nesting exceeds six levels")
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", key):
                    raise CollabError("Metric keys must be short identifiers, not paths or prose")
                check_metrics(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                check_metrics(item, depth + 1)
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise CollabError("Metrics accept numbers, booleans, null, lists and objects")
        elif isinstance(value, float) and not math.isfinite(value):
            raise CollabError("Metric values must be finite or null")
    metrics = payload.get("metrics", {})
    if not isinstance(metrics, dict):
        raise CollabError("metrics must be an object")
    check_metrics(metrics)
    result = {"schema_version": 1, "task_id": request["task_id"],
              "revision": request["revision"], "source_commit": request["source_commit"],
              "candidate_commit": commit, "status": payload["status"], "summary": summary,
              "evidence": evidence, "metrics": metrics, "validation": "executor_report_requires_review"}
    encoded(result)
    return result
