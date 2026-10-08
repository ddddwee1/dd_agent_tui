"""Detect unproductive repetition after fresh execution, without caching live state."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass

from .history_archive import archive_messages
from .tool_output import ArchivedOutput


log = logging.getLogger(__name__)

# Deliberately exclude archive pagination, task reads and successful mutations.
# Those calls may intentionally return the same text with different significance.
READ_TOOLS = frozenset({
    "read_file", "read_files", "list_files", "glob_files", "search_content",
    "read_doc", "follow_doc_link", "doc_route_status", "web_fetch", "web_search",
    "project_note_search", "project_note_list", "project_note_read",
})


def result_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class _Observation:
    digest: str
    source: dict
    count: int = 1
    ref: str | None = None
    original_chars: int = 0


class ToolLoopGuard:
    """Per-turn, bounded observations. Never skip execution or decide a task is done."""

    def __init__(self):
        self._observations: dict[tuple[str, str], _Observation] = {}
        self._latest_user = None

    def sync_context(self, messages) -> None:
        latest = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if latest is not self._latest_user:
            self._observations.clear()
            self._latest_user = latest

    def observe(self, ctx, *, name: str, args: dict, call_id: str | None,
                content: str, digest: str, ok: bool) -> str:
        # Bash's exit status is the last real status line in its result; stdout
        # may contain other "Exit code" strings. Errors retain their full preview.
        exit_codes = re.findall(r"^Exit code: (-?\d+)\s*$", content, re.M) if name == "bash" else []
        failed = not ok or bool(exit_codes and int(exit_codes[-1]) != 0)
        key = (name, result_digest(json.dumps(args, ensure_ascii=False, sort_keys=True)))
        previous = self._observations.get(key)
        if name not in READ_TOOLS:
            # A command/edit/meta tool may change the conditions behind earlier
            # reads. Keep only its own failed attempt for immediate retry guidance.
            self._observations.clear()
            if not failed:
                return content
        if previous is None or previous.digest != digest:
            self._observations[key] = _Observation(
                digest=digest,
                source={"role": "tool", "tool_call_id": call_id, "name": name,
                        "content": str(content)},
                ref=getattr(content, "ref", None),
                original_chars=getattr(content, "original_chars", len(content)),
            )
            if len(self._observations) > 128:
                self._observations.pop(next(iter(self._observations)))
            return content
        previous.count += 1
        self._observations[key] = previous
        if failed:
            note = (f"\n[Repeated failure: identical {name} arguments and result "
                    f"{previous.count} times. This attempt added no new error evidence. "
                    "Address the cause or change the hypothesis before retrying; "
                    "retry unchanged only for an explicit requirement or transient failure.]")
            text = str(content) + note
            return (ArchivedOutput(text, content.ref, content.original_chars)
                    if isinstance(content, ArchivedOutput) else text)
        # Identical read results can point to the already-observed source. Archive
        # small originals on the first repeat; large results already carry refs.
        if previous.ref is None:
            if not ctx.session_id:
                # Normally the engine's context already has its session identity.
                ctx.session_id = "outputs-" + uuid.uuid4().hex
            try:
                _, refs = archive_messages(ctx.session_id, [previous.source], reason="repeated_tool_result")
            except Exception:
                log.warning("Cannot archive repeated result; retaining fresh output", exc_info=True)
                return content
            previous.ref = refs[0]
        text = (f"Unchanged result: {name} was executed again and returned the same "
                f"result as tool call {previous.source['tool_call_id']} "
                f"({previous.count} identical observations). Reuse that evidence and "
                "continue the next needed step; request a different range/query only "
                "when it answers a remaining question.\n"
                f'Exact original: history_read(ref="{previous.ref}").')
        return ArchivedOutput(text, previous.ref, previous.original_chars)
