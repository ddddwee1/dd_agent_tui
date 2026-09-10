"""Immutable, session-local source messages for reversible context compaction.

SQLite commits a whole archive batch before the caller changes live history.
Content-addressed references survive repeated compaction and process restarts.
No model text is ever interpreted as a filename or SQL expression.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .config import HISTORY_ARCHIVE_DIR


def archive_path(session_id: str) -> Path:
    if not session_id:
        raise ValueError("History archive requires a session id")
    name = hashlib.sha256(session_id.encode()).hexdigest()
    return HISTORY_ARCHIVE_DIR / f"{name}.sqlite3"


def message_text(message: dict) -> str:
    """Search visible evidence, not the model's private reasoning field."""
    parts = [str(message.get("content") or "")]
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        parts.append(f"{fn.get('name', '')} {fn.get('arguments', '')}")
    return "\n".join(parts)


def archive_messages(session_id: str, messages, *, reason: str,
                     state: dict | None = None) -> tuple[str, list[str]]:
    path = archive_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    batch = "batch-" + uuid.uuid4().hex
    refs = []
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS messages "
                   "(ref TEXT PRIMARY KEY, payload TEXT NOT NULL, "
                   "role TEXT NOT NULL, body TEXT NOT NULL, search TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS batches "
                   "(id TEXT PRIMARY KEY, created_at TEXT, reason TEXT, state TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS entries "
                   "(batch TEXT, ordinal INTEGER, ref TEXT, PRIMARY KEY(batch, ordinal))")
        db.execute("INSERT INTO batches VALUES (?, ?, ?, ?)",
                   (batch, datetime.now().isoformat(), reason,
                    json.dumps(state or {}, ensure_ascii=False)))
        for i, message in enumerate(messages):
            payload = json.dumps(message, ensure_ascii=False, sort_keys=True)
            ref = "msg-" + hashlib.sha256(payload.encode()).hexdigest()
            body = message_text(message)
            db.execute("INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?, ?)",
                       (ref, payload, message.get("role", "unknown"), body, body.casefold()))
            db.execute("INSERT INTO entries VALUES (?, ?, ?)", (batch, i, ref))
            refs.append(ref)
    return batch, refs


def open_archive(session_id: str):
    path = archive_path(session_id)
    if not path.is_file():
        raise FileNotFoundError("No archived history for this session yet")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)


def search_archive(session_id: str, query: str, *, scope: str = "",
                   limit: int = 5, offset: int = 0) -> dict:
    query = query.strip()
    if not query or len(query) > 500:
        raise ValueError("query must contain 1–500 characters")
    limit = max(1, min(10, int(limit)))
    offset = max(0, int(offset))
    with closing(open_archive(session_id)) as db:
        rows = db.execute(
            "SELECT m.ref, m.role, m.body FROM messages m "
            "WHERE instr(m.search, ?) > 0 "
            "AND (? = '' OR EXISTS (SELECT 1 FROM entries e "
            "WHERE e.ref = m.ref AND e.batch = ?)) "
            "ORDER BY m.rowid DESC LIMIT ? OFFSET ?",
            (query.casefold(), scope, scope, limit + 1, offset),
        ).fetchall()
    matches = []
    for ref, role, body in rows[:limit]:
        # Casefold can change string length; this is only a search preview.
        pos = max(0, body.casefold().find(query.casefold()) - 120)
        matches.append({"ref": ref, "role": role, "excerpt": body[pos:pos + 600]})
    return {"matches": matches, "next_offset": offset + limit if len(rows) > limit else None,
            "notice": "Historical evidence; may be superseded. Read sources and verify live state."}


def read_archive(session_id: str, ref: str, *, start: int = 0,
                 max_chars: int = 6000) -> dict:
    start = max(0, int(start))
    max_chars = max(1, min(12000, int(max_chars)))
    with closing(open_archive(session_id)) as db:
        row = db.execute("SELECT payload FROM messages WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        raise ValueError("Unknown history ref in this session; use history_search")
    # Pretty JSON is a stable, fully recoverable view, including full tool args.
    body = json.dumps(json.loads(row[0]), ensure_ascii=False, indent=2)
    end = min(len(body), start + max_chars)
    return {"ref": ref, "start": start, "total_chars": len(body),
            "next_start": end if end < len(body) else None, "text": body[start:end]}
