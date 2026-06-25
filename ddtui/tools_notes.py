"""Project-local note tools.

Notes are durable, repo-scoped runbook memory: verified commands,
environment setup, gotchas, conventions, and user-approved decisions.
They live in a small JSON file so the user can inspect, edit, commit, or
ignore them like any other project artifact.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import (
    PROJECT_NOTE_BODY_MAX_CHARS,
    PROJECT_NOTE_DEFAULT_LIST_LIMIT,
    PROJECT_NOTE_DEFAULT_SEARCH_LIMIT,
    PROJECT_NOTE_SEARCH_SNIPPET_CHARS,
    PROJECT_NOTES_DIR,
)
from .state import ToolContext


_SOURCES = {"user", "observed", "inferred", "imported"}
_CONFIDENCE = {"low", "medium", "high"}
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|password|passwd|secret)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:-]{12,}"
    ),
]


class NoteError(ValueError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _find_project_root(work_dir: str) -> Path:
    start = Path(work_dir).expanduser().resolve()
    cur = start if start.is_dir() else start.parent
    while True:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return start
        cur = cur.parent


def _notes_paths(ctx: ToolContext) -> tuple[Path, Path, Path]:
    project_root = _find_project_root(ctx.work_dir)
    if PROJECT_NOTES_DIR:
        notes_dir = Path(PROJECT_NOTES_DIR).expanduser().resolve()
    else:
        notes_dir = project_root / ".ddtui" / "notes"
    return project_root, notes_dir, notes_dir / "notes.json"


def _empty_store(project_root: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "project_root": str(project_root),
        "notes": {},
    }


def _load_store(ctx: ToolContext) -> tuple[dict[str, Any], Path, Path]:
    project_root, notes_dir, notes_file = _notes_paths(ctx)
    if not notes_file.exists():
        return _empty_store(project_root), notes_dir, notes_file
    try:
        data = json.loads(notes_file.read_text(encoding="utf-8"))
    except Exception as e:
        raise NoteError(f"could not read notes file {notes_file}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("notes"), dict):
        raise NoteError(f"notes file has unrecognized format: {notes_file}")
    data.setdefault("version", 1)
    data.setdefault("project_root", str(project_root))
    return data, notes_dir, notes_file


def _save_store(store: dict[str, Any], notes_dir: Path, notes_file: Path) -> None:
    notes_dir.mkdir(parents=True, exist_ok=True)
    tmp = notes_file.with_name(f".{notes_file.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(notes_file)


def _normalize_tags(tags: list[str] | str | None) -> list[str]:
    if tags is None:
        return []
    raw_items: list[Any]
    if isinstance(tags, str):
        raw_items = re.split(r"[,\s]+", tags)
    elif isinstance(tags, list):
        raw_items = tags
    else:
        raise NoteError("tags must be an array of strings")
    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        tag = str(item).strip().lower()
        if not tag or tag in seen:
            continue
        if len(tag) > 40:
            tag = tag[:40]
        seen.add(tag)
        out.append(tag)
        if len(out) >= 20:
            break
    return out


def _normalize_source(source: str | None) -> str:
    value = (source or "observed").strip().lower()
    if value not in _SOURCES:
        raise NoteError(
            f"source must be one of {', '.join(sorted(_SOURCES))}"
        )
    return value


def _normalize_confidence(confidence: str | None) -> str:
    value = (confidence or "medium").strip().lower()
    if value not in _CONFIDENCE:
        raise NoteError(
            f"confidence must be one of {', '.join(sorted(_CONFIDENCE))}"
        )
    return value


def _check_secret_text(title: str, body: str) -> None:
    text = f"{title}\n{body}"
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise NoteError(
                "note appears to contain secret-like text; redact tokens, "
                "passwords, API keys, or private keys before saving"
            )


def _validate_title_body(title: str, body: str) -> tuple[str, str]:
    title = title.strip()
    body = body.strip()
    if not title:
        raise NoteError("title cannot be empty")
    if not body:
        raise NoteError("body cannot be empty")
    if len(title) > 200:
        title = title[:200]
    if len(body) > PROJECT_NOTE_BODY_MAX_CHARS:
        raise NoteError(
            f"body is too large ({len(body):,} chars); keep notes concise "
            f"or summarize below {PROJECT_NOTE_BODY_MAX_CHARS:,} chars"
        )
    _check_secret_text(title, body)
    return title, body


def _make_note_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"note-{stamp}-{secrets.token_hex(3)}"


def _parse_bool(value: bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _matching_notes(
    notes: dict[str, dict],
    tags: list[str] | str | None,
    include_archived: bool,
) -> list[dict]:
    tag_filter = set(_normalize_tags(tags))
    out: list[dict] = []
    for note in notes.values():
        if not isinstance(note, dict):
            continue
        if note.get("archived") and not include_archived:
            continue
        note_tags = set(note.get("tags") or [])
        if tag_filter and not tag_filter.issubset(note_tags):
            continue
        out.append(note)
    return out


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", text.lower()) if t]


def _score_note(note: dict, query: str) -> int:
    q = query.strip().lower()
    if not q:
        return 1
    title = str(note.get("title") or "")
    body = str(note.get("body") or "")
    tags = [str(t) for t in note.get("tags") or []]
    hay_title = title.lower()
    hay_body = body.lower()
    hay_tags = " ".join(tags).lower()
    score = 0
    if q in hay_title:
        score += 20
    if q in hay_body:
        score += 8
    if q in hay_tags:
        score += 12
    for tok in _tokens(q):
        if tok in hay_title:
            score += 6
        if tok in hay_tags:
            score += 5
        if tok in hay_body:
            score += 2
    return score


def _snippet(note: dict, query: str) -> str:
    body = str(note.get("body") or "")
    limit = PROJECT_NOTE_SEARCH_SNIPPET_CHARS
    if len(body) <= limit:
        return body
    q = query.strip().lower()
    idx = -1
    if q:
        idx = body.lower().find(q)
    if idx < 0:
        for tok in _tokens(q):
            idx = body.lower().find(tok)
            if idx >= 0:
                break
    if idx < 0:
        return body[:limit] + f"\n...[+{len(body) - limit} chars]"
    start = max(0, idx - limit // 3)
    end = min(len(body), start + limit)
    prefix = "..." if start else ""
    suffix = f"\n...[+{len(body) - end} chars]" if end < len(body) else ""
    return prefix + body[start:end] + suffix


def _format_note_summary(note: dict, query: str = "") -> str:
    tags = ", ".join(note.get("tags") or []) or "-"
    archived = " archived" if note.get("archived") else ""
    return (
        f"{note.get('id')}  rev={note.get('rev', 1)}{archived}\n"
        f"title: {note.get('title')}\n"
        f"tags: {tags}  source={note.get('source')}  "
        f"confidence={note.get('confidence')}  updated={note.get('updated_at')}\n"
        f"snippet:\n{_snippet(note, query)}"
    )


def _format_note_full(note: dict, notes_file: Path) -> str:
    tags = ", ".join(note.get("tags") or []) or "-"
    return (
        f"id: {note.get('id')}\n"
        f"title: {note.get('title')}\n"
        f"rev: {note.get('rev', 1)}\n"
        f"created_at: {note.get('created_at')}\n"
        f"updated_at: {note.get('updated_at')}\n"
        f"archived: {bool(note.get('archived'))}\n"
        f"tags: {tags}\n"
        f"source: {note.get('source')}\n"
        f"confidence: {note.get('confidence')}\n"
        f"notes_file: {notes_file}\n"
        "--- body ---\n"
        f"{note.get('body') or ''}"
    )


def _get_note(store: dict[str, Any], note_id: str) -> dict:
    notes = store.get("notes") or {}
    note = notes.get(note_id)
    if not isinstance(note, dict):
        raise NoteError(f"unknown note_id {note_id!r}")
    return note


def tool_project_note_add(
    ctx: ToolContext,
    title: str,
    body: str,
    tags: list[str] | str | None = None,
    source: str = "observed",
    confidence: str = "medium",
) -> str:
    """Add a new reusable project-local note."""
    try:
        title, body = _validate_title_body(title, body)
        note_tags = _normalize_tags(tags)
        note_source = _normalize_source(source)
        note_confidence = _normalize_confidence(confidence)
        store, notes_dir, notes_file = _load_store(ctx)
        notes = store.setdefault("notes", {})
        note_id = _make_note_id()
        now = _now()
        notes[note_id] = {
            "id": note_id,
            "title": title,
            "body": body,
            "tags": note_tags,
            "source": note_source,
            "confidence": note_confidence,
            "created_at": now,
            "updated_at": now,
            "rev": 1,
            "archived": False,
        }
        _save_store(store, notes_dir, notes_file)
    except NoteError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error adding project note: {e}"
    return (
        f"Added project note {note_id} (rev 1)\n"
        f"title: {title}\n"
        f"tags: {', '.join(note_tags) or '-'}\n"
        f"notes_file: {notes_file}"
    )


def tool_project_note_search(
    ctx: ToolContext,
    query: str,
    tags: list[str] | str | None = None,
    limit: int = PROJECT_NOTE_DEFAULT_SEARCH_LIMIT,
    include_archived: bool = False,
) -> str:
    """Search project-local notes by simple token score."""
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        limit = PROJECT_NOTE_DEFAULT_SEARCH_LIMIT
    try:
        store, _notes_dir, notes_file = _load_store(ctx)
        candidates = _matching_notes(
            store.get("notes") or {},
            tags,
            _parse_bool(include_archived),
        )
        ranked = [
            (score, note)
            for note in candidates
            if (score := _score_note(note, query)) > 0
        ]
        ranked.sort(
            key=lambda item: (
                item[0],
                str(item[1].get("updated_at") or ""),
            ),
            reverse=True,
        )
    except NoteError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error searching project notes: {e}"
    if not ranked:
        return f"No matching project notes in {notes_file}."
    lines = [
        f"Project notes matching {query!r} ({min(limit, len(ranked))}/{len(ranked)})",
        f"notes_file: {notes_file}",
    ]
    for score, note in ranked[:limit]:
        lines.append("")
        lines.append(f"score: {score}")
        lines.append(_format_note_summary(note, query))
    return "\n".join(lines)


def tool_project_note_list(
    ctx: ToolContext,
    tags: list[str] | str | None = None,
    limit: int = PROJECT_NOTE_DEFAULT_LIST_LIMIT,
    include_archived: bool = False,
) -> str:
    try:
        limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        limit = PROJECT_NOTE_DEFAULT_LIST_LIMIT
    try:
        store, _notes_dir, notes_file = _load_store(ctx)
        notes = _matching_notes(
            store.get("notes") or {},
            tags,
            _parse_bool(include_archived),
        )
        notes.sort(key=lambda n: str(n.get("updated_at") or ""), reverse=True)
    except NoteError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error listing project notes: {e}"
    if not notes:
        return f"No project notes in {notes_file}."
    lines = [
        f"Project notes ({min(limit, len(notes))}/{len(notes)})",
        f"notes_file: {notes_file}",
    ]
    for note in notes[:limit]:
        tags_text = ", ".join(note.get("tags") or []) or "-"
        archived = " archived" if note.get("archived") else ""
        lines.append(
            f"  {note.get('id')}  rev={note.get('rev', 1)}{archived}  "
            f"updated={note.get('updated_at')}  tags={tags_text}\n"
            f"    {note.get('title')}"
        )
    return "\n".join(lines)


def tool_project_note_read(ctx: ToolContext, note_id: str) -> str:
    try:
        store, _notes_dir, notes_file = _load_store(ctx)
        note = _get_note(store, note_id)
    except NoteError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error reading project note: {e}"
    return _format_note_full(note, notes_file)


def tool_project_note_update(
    ctx: ToolContext,
    note_id: str,
    title: str | None = None,
    body: str | None = None,
    tags: list[str] | str | None = None,
    source: str | None = None,
    confidence: str | None = None,
    archived: bool | None = None,
) -> str:
    """Patch an existing note in-place."""
    try:
        store, notes_dir, notes_file = _load_store(ctx)
        note = _get_note(store, note_id)
        old_rev = int(note.get("rev") or 1)
        old_title = str(note.get("title") or "")
        old_body = str(note.get("body") or "")
        old_tags = list(note.get("tags") or [])

        changed: list[str] = []
        new_title = old_title if title is None else title.strip()
        new_body = old_body if body is None else body.strip()
        if title is not None or body is not None:
            new_title, new_body = _validate_title_body(new_title, new_body)
            if new_title != old_title:
                note["title"] = new_title
                changed.append("title")
            if new_body != old_body:
                note["body"] = new_body
                changed.append("body")
        if tags is not None:
            new_tags = _normalize_tags(tags)
            if new_tags != old_tags:
                note["tags"] = new_tags
                changed.append("tags")
        if source is not None:
            new_source = _normalize_source(source)
            if new_source != note.get("source"):
                note["source"] = new_source
                changed.append("source")
        if confidence is not None:
            new_confidence = _normalize_confidence(confidence)
            if new_confidence != note.get("confidence"):
                note["confidence"] = new_confidence
                changed.append("confidence")
        if archived is not None:
            new_archived = bool(archived)
            if new_archived != bool(note.get("archived")):
                note["archived"] = new_archived
                changed.append("archived")
        if not changed:
            return f"No changes for project note {note_id} (rev {old_rev})."

        note["updated_at"] = _now()
        note["rev"] = old_rev + 1
        _save_store(store, notes_dir, notes_file)
    except NoteError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error updating project note: {e}"
    return (
        f"Updated project note {note_id} (rev {old_rev} -> {note['rev']})\n"
        f"changed: {', '.join(changed)}\n"
        f"title: {note.get('title')}\n"
        f"tags: {', '.join(note.get('tags') or []) or '-'}\n"
        f"body: {len(old_body):,} chars -> {len(note.get('body') or ''):,} chars\n"
        f"notes_file: {notes_file}"
    )


def tool_project_note_delete(ctx: ToolContext, note_id: str) -> str:
    """Soft-delete a project note by archiving it."""
    return tool_project_note_update(ctx, note_id, archived=True)
