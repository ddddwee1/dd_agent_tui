"""Crash-recovery runtime state helpers.

Conversation history is the long-lived transcript. Runtime state is the
small, mutable layer that lets a fresh TUI process reconnect to work that
was in flight when the previous process disappeared.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import RUNTIME_DIR


def new_session_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"session-{stamp}-{uuid.uuid4().hex[:8]}"


def _safe_id(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in value)
    return safe.strip("-") or new_session_id()


def session_dir(session_id: str) -> Path:
    return RUNTIME_DIR / _safe_id(session_id)


def turn_journal_path(session_id: str) -> Path:
    return session_dir(session_id) / "turn.json"


def tasks_dir(session_id: str) -> Path:
    return session_dir(session_id) / "tasks"


def task_registry_path(session_id: str, task_id: str) -> Path:
    return tasks_dir(session_id) / f"{_safe_id(task_id)}.json"


def task_spec_path(session_id: str, task_id: str) -> Path:
    return tasks_dir(session_id) / f"{_safe_id(task_id)}.spec.json"


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def clear_path(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def list_turn_journals() -> list[dict[str, Any]]:
    if not RUNTIME_DIR.exists():
        return []
    journals: list[dict[str, Any]] = []
    for path in RUNTIME_DIR.glob("*/turn.json"):
        payload = read_json(path)
        if not payload:
            continue
        payload["_path"] = str(path)
        journals.append(payload)
    journals.sort(key=lambda p: str(p.get("updated_at") or ""), reverse=True)
    return journals
