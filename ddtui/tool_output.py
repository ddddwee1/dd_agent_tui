"""Bound oversized tool results with recoverable, session-local originals."""

from __future__ import annotations

import logging
import uuid

from .config import TOOL_HISTORY_MAX_CHARS, TOOL_HISTORY_SNIPPET_CHARS, TOOL_OUTPUT_MAX_CHARS
from .context_compaction import evidence_excerpt
from .history_archive import archive_messages
from .history_store import stamp_message


log = logging.getLogger(__name__)


class ArchivedOutput(str):
    """A normal tool string with a source reference for the engine to persist."""

    def __new__(cls, text: str, ref: str, original_chars: int):
        obj = super().__new__(cls, text)
        obj.ref = ref
        obj.original_chars = original_chars
        return obj


def _footer(ref: str, original_chars: int) -> str:
    return (f'\n[Full output archived: {original_chars} chars. '
            f'history_read(ref="{ref}") for exact text; follow next_start as needed.]')


def limit_tool_output(ctx, text: str, *, name: str = "", call_id: str | None = None,
                      arguments: dict | None = None,
                      source_message: dict | None = None,
                      preview: str | None = None,
                      max_chars: int = TOOL_OUTPUT_MAX_CHARS) -> str:
    """Archive BEFORE shortening. On storage failure retain the original evidence.

    No model call is needed. Existing refs survive further shortening without
    archiving a preview in place of the original. The footer counts in the cap.
    """
    cap = max(512, max_chars)
    if len(text) <= cap and preview is None:
        return text
    original_chars = getattr(text, "original_chars", len(text))
    ref = getattr(text, "ref", None)
    preview = str(text) if preview is None else preview
    if ref:
        preview = preview.removesuffix(_footer(ref, original_chars))
    else:
        # Standalone tool callers may not have allocated a session yet.
        if not ctx.session_id:
            ctx.session_id = "outputs-" + uuid.uuid4().hex
        source = (dict(source_message) if source_message is not None
                  else stamp_message({"role": "tool"}))
        source.update(name=name, content=str(text))
        if call_id is not None:
            source["tool_call_id"] = call_id
        if arguments is not None:
            source["ddtui_tool_arguments"] = arguments
        try:
            _, refs = archive_messages(ctx.session_id, [source], reason="tool_output")
        except Exception:
            log.warning("Tool output archive failed; retaining complete result", exc_info=True)
            return text
        ref = refs[0]
    footer = _footer(ref, original_chars)
    return ArchivedOutput(evidence_excerpt(preview, cap - len(footer)) + footer,
                          ref, original_chars)


def trim_tool_history(messages, ctx, *, max_chars: int = TOOL_HISTORY_MAX_CHARS) -> None:
    """Shorten older tool bodies under a separate rolling context budget.

    Disabled by default: context-window compaction handles accumulated history.
    When explicitly enabled, keep message indices and call/result pairing
    stable, including explore boundaries. Never shorten the latest batch before
    the model has a chance to consume it; the budget is a soft target.
    """
    if max_chars <= 0:
        return
    total = sum(len(m.get("content") or "") for m in messages if m.get("role") == "tool")
    if total <= max_chars:
        return
    latest_call = max((i for i, m in enumerate(messages) if m.get("tool_calls")), default=-1)
    for i, message in enumerate(messages):
        if total <= max_chars:
            break
        content = message.get("content") or ""
        if (message.get("role") != "tool" or len(content) <= TOOL_HISTORY_SNIPPET_CHARS
                or (latest_call >= 0 and i > latest_call)):
            continue
        if message.get("ddtui_history_ref"):
            content = ArchivedOutput(content, message["ddtui_history_ref"],
                                     message.get("ddtui_output_chars", len(content)))
        shortened = limit_tool_output(ctx, content, call_id=message.get("tool_call_id"),
                                      name=message.get("name", ""),
                                      source_message=message,
                                      max_chars=TOOL_HISTORY_SNIPPET_CHARS)
        if len(shortened) >= len(content):
            continue
        messages[i] = {**message, "content": str(shortened),
                       "ddtui_history_ref": shortened.ref,
                       "ddtui_output_chars": shortened.original_chars}
        total -= len(content) - len(shortened)
