"""Conversation history with a stable object identity.

A running TurnEngine captures a reference to the message sequence at
construction time. Historically the app sometimes *rebound* its
``messages`` attribute to a freshly built list (compact, explore_end,
/resume), stranding the engine's subsequent appends — including tool
responses — on the stale object and corrupting the wire protocol
(orphan assistant tool_calls → 400 on the next request). That bug
family was fixed three separate times by hand-writing slice
assignments.

HistoryStore makes the safe behavior structural: the backing list is
private and never swapped, so *every* mutation — including wholesale
replacement — happens in place. The app exposes ``messages`` as a
property whose setter delegates to :meth:`replace_all`, which turns
even a naive ``self.messages = new`` into an in-place swap.

Slice reads return plain lists; :meth:`snapshot` is the explicit
serialization boundary (``json.dumps`` does not understand this type,
by design — it forces callers to state where history leaves the
process).
"""

from __future__ import annotations

from collections.abc import Iterable, MutableSequence
from typing import Any


class HistoryStore(MutableSequence):
    __slots__ = ("_items",)

    def __init__(self, initial: Iterable[dict] | None = None) -> None:
        self._items: list[dict] = list(initial or [])

    # ── MutableSequence core ──
    # (append/extend/pop/remove/index/count/__iadd__ come from the ABC)

    def __getitem__(self, index):
        return self._items[index]

    def __setitem__(self, index, value) -> None:
        self._items[index] = value

    def __delitem__(self, index) -> None:
        del self._items[index]

    def __len__(self) -> int:
        return len(self._items)

    def insert(self, index: int, value: dict) -> None:
        self._items.insert(index, value)

    # The ABC mixins fall back to __getitem__ loops; go straight to the
    # backing list for the hot paths.
    def __iter__(self):
        return iter(self._items)

    def __contains__(self, item: Any) -> bool:
        return item in self._items

    def __eq__(self, other: Any):
        if isinstance(other, HistoryStore):
            return self._items == other._items
        if isinstance(other, list):
            return self._items == other
        return NotImplemented

    __hash__ = None  # mutable container

    def __repr__(self) -> str:
        return f"HistoryStore(<{len(self._items)} messages>)"

    # ── semantic operations ──

    def replace_all(self, new_messages: Iterable[dict]) -> None:
        """Wholesale swap that keeps identity. ``list()`` materializes
        first, so passing a view of self (or self) cannot self-clobber."""
        self._items[:] = list(new_messages)

    def replace_span(
        self, start: int, end: int, replacement: Iterable[dict]
    ) -> None:
        """Replace messages[start:end] in place (explore_end's summary
        collapse and friends)."""
        self._items[start:end] = list(replacement)

    def snapshot(self) -> list[dict]:
        """Shallow copy for serialization: a plain list, same message
        dicts."""
        return list(self._items)
