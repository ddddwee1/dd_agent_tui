"""Runtime-message framing and assistant-claim guards."""

from __future__ import annotations

import re


RUNTIME_TASK_EVENT_KIND = "runtime_task_event"
_RESERVED_RE = re.compile(
    r"\[(Async task (?:notice|complete)|Subagent result)(?:[^\]\n]*)\]"
)
_REPLACEMENT = "[assistant runtime-status claim: unverified]"


def runtime_task_event_message(text: str) -> dict:
    """Build a metadata-authenticated runtime task event message."""
    return {
        "role": "user",
        "content": text,
        "ddtui_kind": RUNTIME_TASK_EVENT_KIND,
    }


def guard_assistant_runtime_claims(text: str) -> tuple[str, bool]:
    """Neutralize reserved runtime markers in ordinary assistant prose.

    Markdown fenced code is left untouched so documentation can show the
    protocol literally. Outside fences, only runtime-injected user messages
    carrying ``ddtui_kind=runtime_task_event`` are authoritative.
    """
    if not text:
        return text, False
    changed = False
    in_fence = False
    fence_marker = ""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            out.append(line)
            continue
        if not in_fence:
            line, count = _RESERVED_RE.subn(_REPLACEMENT, line)
            changed = changed or bool(count)
        out.append(line)
    guarded = "".join(out)
    if changed:
        guarded = (
            "> Runtime status in assistant prose is unverified; use the "
            "runtime event card or task_check.\n\n" + guarded
        )
    return guarded, changed
