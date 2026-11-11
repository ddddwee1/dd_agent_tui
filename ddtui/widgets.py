"""Textual widgets that compose the TUI.

Pure presentation layer — every class here only depends on textual /
rich, plus type / data classes from `ddtui.state`. They never reach
into `ddtui.tools` or `ddtui.app`. State is pushed in via constructor
arguments or method calls (e.g. `TasksBlock.render_tasks(tasks)`,
`StatusBar.render_status(counter, ...)`).
"""

from __future__ import annotations


import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
import re
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, DataTable, Static, TextArea

from .state import (
    AsyncTask,
    SubagentSession,
    TokenCounter,
    TOOL_ERROR_PREFIXES,
    async_task_status,
)
from .thinking_summary import normalize_summary, round_from_message, summary_from_message
from .runtime_messages import steer_text
from .ui_theme import UI


# ── Markdown table pipe-escaping helper ──

_TABLE_SEP_RE = re.compile(r'^\|[\s\-:]+\|')


def _sanitize_table_pipes(md_text: str) -> str:
    """Escape unescaped pipe characters inside Markdown table cells.

    The LLM sometimes emits math formulas like ``|x|`` inside table cells
    without escaping the pipes, which causes markdown-it-py to split the
    cell into multiple columns.  We detect table blocks, align each data
    row's pipe positions against the separator row, and backslash-escape
    any extra pipes that are not column delimiters.
    """
    lines = md_text.split('\n')
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        # Look for a table header row: starts and ends with |
        if not (stripped.startswith('|') and stripped.endswith('|')):
            i += 1
            continue
        # Need a separator row on the next line
        if i + 1 >= len(lines):
            i += 1
            continue
        sep_line = lines[i + 1]
        sep_stripped = sep_line.strip()
        if not _TABLE_SEP_RE.match(sep_stripped):
            i += 1
            continue

        # Table found.  Record pipe positions from the separator row
        # (these are the ground-truth column boundaries).
        sep_positions = [j for j, ch in enumerate(sep_line) if ch == '|']
        expected = len(sep_positions)  # columns + 1

        # Walk data rows below the separator
        j = i + 2
        while j < len(lines):
            data = lines[j]
            data_stripped = data.strip()
            if not (data_stripped.startswith('|') and data_stripped.endswith('|')):
                break  # table ended

            pipe_count = data.count('|')
            if pipe_count > expected:
                # Collect all pipe positions, excluding already-escaped
                # ones (preceded by an odd number of backslashes).
                all_pipes = [k for k, ch in enumerate(data) if ch == '|']
                unescaped: list[int] = []
                for k in all_pipes:
                    bs = 0
                    pos = k - 1
                    while pos >= 0 and data[pos] == '\\':
                        bs += 1
                        pos -= 1
                    if bs % 2 == 0:  # even → literal pipe
                        unescaped.append(k)

                if len(unescaped) <= expected:
                    pass  # count is fine after excluding escaped pipes
                else:
                    # The first and last unescaped | in a table row are
                    # always the opening/closing delimiters — lock them in
                    # before position matching.
                    delimiter_set: set[int] = {unescaped[0], unescaped[-1]}
                    used: set[int] = set(delimiter_set)
                    THRESHOLD = 4

                    # Match the remaining separator positions to the
                    # closest unmatched unescaped pipe within threshold.
                    # Skip the first and last sep positions (already matched
                    # by the locked-in first/last data pipes).
                    for ref in sep_positions[1:-1]:
                        best = -1
                        best_dist = THRESHOLD + 1
                        for dp in unescaped:
                            if dp in used:
                                continue
                            dist = abs(dp - ref)
                            if dist < best_dist:
                                best_dist = dist
                                best = dp
                        if best >= 0:
                            delimiter_set.add(best)
                            used.add(best)

                    # Only modify the row if we successfully matched the
                    # expected number of delimiters.  Otherwise the row
                    # format is too different from the separator — bail
                    # out rather than risk mangling the table.
                    if len(delimiter_set) == expected:
                        for k in reversed(unescaped):
                            if k not in delimiter_set:
                                data = data[:k] + '\\' + data[k:]
                        lines[j] = data

            j += 1

        i = j  # skip past the table we just processed

    return '\n'.join(lines)


# ───────── conversation bubbles ─────────

class UserBubble(Static):
    DEFAULT_CSS = """
    UserBubble {
        margin: 1 0;
        padding: 0 1;
        border-left: solid $border-blurred;
        background: $surface;
    }
    UserBubble.pending {
        border-left: solid $primary;
    }
    UserBubble.pending:hover {
        background: $panel;
    }
    """

    class EditRequested(Message):
        """Posted when a *pending* bubble is clicked, so the App can
        open the edit/delete modal. Plain (already-sent) bubbles do not
        carry the `pending` class and won't post this."""

        def __init__(self, bubble: "UserBubble") -> None:
            self.bubble = bubble
            super().__init__()

    def __init__(self, text: str) -> None:
        self._text = text
        super().__init__(self._render_label(text))

    @staticmethod
    def _render_label(text: str) -> Text:
        t = Text()
        t.append("你  ", style=f"bold {UI.accent}")
        t.append(text)
        return t

    def set_text(self, text: str) -> None:
        """Re-render with new text. Used by the edit-modal callback so
        the parked bubble stays in place but reflects the new content."""
        self._text = text
        self.update(self._render_label(text))

    @property
    def text_value(self) -> str:
        return self._text

    def on_click(self, event) -> None:
        if self.has_class("pending"):
            self.post_message(self.EditRequested(self))
            event.stop()


class SteerBubble(Static):
    """A user 'steer' interjection — submitted via Alt/Cmd+Enter while a
    turn is running. The accent marker identifies an interjection; it is injected
    into the next LLM round as `[User interruption] <text>`."""

    DEFAULT_CSS = """
    SteerBubble {
        margin: 1 0 0 0;
        padding: 0 1;
        border-left: solid $primary;
    }
    SteerBubble.pending:hover {
        background: $panel;
    }
    """

    class EditRequested(Message):
        def __init__(self, bubble: "SteerBubble") -> None:
            self.bubble = bubble
            super().__init__()

    def __init__(self, text: str) -> None:
        self._text = text
        super().__init__(self._render_label(text))

    @staticmethod
    def _render_label(text: str) -> Text:
        t = Text()
        t.append("插话  ", style=f"bold {UI.accent}")
        t.append(text)
        return t

    def set_text(self, text: str) -> None:
        self._text = text
        self.update(self._render_label(text))

    @property
    def text_value(self) -> str:
        return self._text

    def on_click(self, event) -> None:
        if self.has_class("pending"):
            self.post_message(self.EditRequested(self))
            event.stop()


class EditPendingScreen(ModalScreen[dict | None]):
    """Modal for editing or deleting a parked queue/steer bubble.

    Dismiss payload:
      * ``{"action": "save", "text": <new>}`` — replace the entry
      * ``{"action": "delete"}``                — drop the entry
      * ``None``                                — user cancelled

    Empty save-text collapses to delete so the caller doesn't have to
    handle the "saved blank" edge case.
    """

    BINDINGS = [
        Binding("escape", "do_cancel", show=False),
        Binding("ctrl+s", "do_save", show=False, priority=True),
        Binding("ctrl+d", "do_delete", show=False, priority=True),
        Binding("ctrl+t", "do_convert", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    EditPendingScreen {
        align: center middle;
    }
    #edit-dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }
    #edit-dialog.steer {
        border: thick $warning;
    }
    #edit-title {
        height: auto;
        margin: 0 0 1 0;
    }
    #edit-input {
        height: 10;
        min-height: 5;
        max-height: 20;
        margin: 0 0 1 0;
    }
    #edit-buttons {
        height: 3;
        align-horizontal: right;
    }
    #edit-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, original: str, kind: str) -> None:
        self._original = original
        # "queue" or "steer" — drives title + border colour only.
        self._kind = kind
        super().__init__()

    def compose(self):
        dialog_classes = "steer" if self._kind == "steer" else ""
        with Vertical(id="edit-dialog", classes=dialog_classes):
            title = Text()
            if self._kind == "steer":
                title.append("⚡ 编辑 steer", style=f"bold {UI.warning}")
            else:
                title.append("📋 编辑排队消息", style=f"bold {UI.accent}")
            convert_hint = "转排队" if self._kind == "steer" else "转 steer"
            title.append(
                f"   ·   Ctrl+S 保存   Ctrl+D 删除   Ctrl+T {convert_hint}   Esc 取消",
                style="dim",
            )
            yield Static(title, id="edit-title")
            yield TextArea(self._original, id="edit-input")
            with Horizontal(id="edit-buttons"):
                yield Button("删除", variant="error", id="btn-delete")
                convert_label = "转为排队" if self._kind == "steer" else "转为 steer"
                yield Button(convert_label, id="btn-convert")
                yield Button("取消", id="btn-cancel")
                yield Button("保存", variant="primary", id="btn-save")

    def on_mount(self) -> None:
        ta = self.query_one("#edit-input", TextArea)
        ta.focus()
        # Park the cursor at the end so the user can keep typing without
        # first having to navigate past the prefilled text.
        try:
            last_line = ta.document.line_count - 1
            last_col = len(ta.document.get_line(last_line))
            ta.move_cursor((last_line, last_col))
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-save":
            self.action_do_save()
        elif bid == "btn-delete":
            self.dismiss({"action": "delete"})
        elif bid == "btn-convert":
            self.action_do_convert()
        elif bid == "btn-cancel":
            self.dismiss(None)

    def action_do_save(self) -> None:
        text = self.query_one("#edit-input", TextArea).text.strip()
        if not text:
            self.dismiss({"action": "delete"})
        else:
            self.dismiss({"action": "save", "text": text})

    def action_do_delete(self) -> None:
        self.dismiss({"action": "delete"})

    def action_do_convert(self) -> None:
        text = self.query_one("#edit-input", TextArea).text.strip()
        if not text:
            self.dismiss({"action": "delete"})
        else:
            self.dismiss({"action": "convert", "text": text})

    def action_do_cancel(self) -> None:
        self.dismiss(None)


class ToolConfirmScreen(ModalScreen[str]):
    """Confirmation modal for file-writing tool calls.

    Dismiss payload: ``"allow"`` (this call), ``"always"`` (whitelist the
    tool for the rest of the session), or ``"deny"``. The summary text is
    built by the caller — this screen stays pure presentation.
    """

    BINDINGS = [
        Binding("y", "do_allow", show=False),
        Binding("a", "do_always", show=False),
        Binding("n", "do_deny", show=False),
        Binding("escape", "do_deny", show=False),
    ]

    DEFAULT_CSS = """
    ToolConfirmScreen {
        align: center middle;
    }
    #confirm-dialog {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        background: $surface;
        border: thick $warning;
    }
    #confirm-title {
        height: auto;
        margin: 0 0 1 0;
    }
    #confirm-body {
        height: auto;
        max-height: 20;
        margin: 0 0 1 0;
        overflow-y: auto;
    }
    #confirm-buttons {
        height: 3;
        align-horizontal: right;
    }
    #confirm-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, tool_name: str, summary: str) -> None:
        self._tool_name = tool_name
        self._summary = summary
        super().__init__()

    def compose(self):
        with Vertical(id="confirm-dialog"):
            title = Text()
            title.append(f"⚠ 写操作确认 · {self._tool_name}", style=f"bold {UI.warning}")
            title.append(
                "   ·   Y 允许   A 本会话总是允许   N/Esc 拒绝",
                style="dim",
            )
            yield Static(title, id="confirm-title")
            yield VerticalScroll(Static(Text(self._summary)), id="confirm-body")
            with Horizontal(id="confirm-buttons"):
                yield Button("拒绝", variant="error", id="btn-deny")
                yield Button("本会话总是允许", id="btn-always")
                yield Button("允许", variant="primary", id="btn-allow")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-allow":
            self.dismiss("allow")
        elif bid == "btn-always":
            self.dismiss("always")
        elif bid == "btn-deny":
            self.dismiss("deny")

    def action_do_allow(self) -> None:
        self.dismiss("allow")

    def action_do_always(self) -> None:
        self.dismiss("always")

    def action_do_deny(self) -> None:
        self.dismiss("deny")


class ResumeConversationScreen(ModalScreen[str | None]):
    """Modal picker for `/resume` when the user doesn't pass a name."""

    BINDINGS = [
        Binding("escape", "do_cancel", show=False),
        Binding("enter", "do_select", show=False),
    ]

    DEFAULT_CSS = """
    ResumeConversationScreen {
        align: center middle;
    }
    #resume-dialog {
        width: 90%;
        max-width: 110;
        height: 80%;
        max-height: 32;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }
    #resume-title {
        height: auto;
        margin: 0 0 1 0;
    }
    #resume-empty {
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    #resume-table {
        height: 1fr;
        margin: 0 0 1 0;
    }
    #resume-buttons {
        height: 3;
        align-horizontal: right;
    }
    #resume-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries
        self._names = [str(e.get("name") or "") for e in entries]
        super().__init__()

    def compose(self):
        with Vertical(id="resume-dialog"):
            title = Text()
            title.append("恢复会话", style=f"bold {UI.accent}")
            title.append("   ·   Enter 选择   Esc 取消", style="dim")
            yield Static(title, id="resume-title")
            if not self._entries:
                yield Static(
                    "还没有保存过会话",
                    id="resume-empty",
                )
            else:
                table = DataTable(id="resume-table")
                table.cursor_type = "row"
                table.zebra_stripes = True
                table.add_columns("会话", "最后编辑", "大小")
                for entry in self._entries:
                    name = str(entry.get("name") or "")
                    table.add_row(
                        name,
                        str(entry.get("edited_at_label") or ""),
                        str(entry.get("size_label") or ""),
                        key=name,
                    )
                yield table
            with Horizontal(id="resume-buttons"):
                yield Button("取消", id="btn-resume-cancel")
                yield Button(
                    "恢复",
                    variant="primary",
                    id="btn-resume-open",
                    disabled=not self._entries,
                )

    def on_mount(self) -> None:
        if self._entries:
            self.query_one("#resume-table", DataTable).focus()
        else:
            self.query_one("#btn-resume-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-resume-open":
            self.action_do_select()
        elif bid == "btn-resume-cancel":
            self.action_do_cancel()

    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        self.dismiss(str(event.row_key.value))

    def _current_name(self) -> str | None:
        if not self._entries:
            return None
        try:
            table = self.query_one("#resume-table", DataTable)
            row = table.cursor_row
        except Exception:
            return None
        if 0 <= row < len(self._names):
            return self._names[row]
        return None

    def action_do_select(self) -> None:
        name = self._current_name()
        if name:
            self.dismiss(name)

    def action_do_cancel(self) -> None:
        self.dismiss(None)


class CollapsedHistoryMarker(Static):
    """Inline placeholder for widgets hidden by the long-conversation
    folder. Clicking it restores every widget the App's
    ``_collapsed_widgets`` list is currently tracking, then removes
    itself. Only one marker exists at a time — re-folding updates the
    count instead of stacking more markers.
    """

    DEFAULT_CSS = """
    CollapsedHistoryMarker {
        margin: 1 0;
        padding: 0 1;
        color: $text-muted;
        background: $surface;
        text-align: center;
    }
    CollapsedHistoryMarker:hover {
        background: $panel;
    }
    """

    class ExpandRequested(Message):
        pass

    def __init__(self) -> None:
        self._count = 0
        super().__init__(self._render_label())

    def _render_label(self) -> Text:
        t = Text()
        t.append("── ", style="dim")
        t.append(f"已折叠 {self._count} 条历史", style=f"bold {UI.muted}")
        t.append("  ·  点击展开 ──", style="dim")
        return t

    def set_count(self, n: int) -> None:
        self._count = n
        self.update(self._render_label())

    def on_click(self, event) -> None:
        self.post_message(self.ExpandRequested())
        event.stop()


class AssistantMessage(Static):
    DEFAULT_CSS = """
    AssistantMessage {
        margin: 1 0;
        padding: 0 1;
        color: $foreground;
    }
    """

    def __init__(self) -> None:
        self._buffer = ""
        super().__init__(self._build())

    def append_text(self, text: str) -> None:
        self._buffer += text
        self.update(self._build())

    def set_text(self, text: str) -> None:
        """Replace streamed text when the committed message was guarded."""
        self._buffer = text
        self.update(self._build())

    @property
    def text(self) -> str:
        return self._buffer

    def _build(self) -> RenderableType:
        # Don't name this `_render` — that's a Textual Widget hook that
        # must return a Visual, not a Rich renderable.
        # Markdown lets the model produce tables, code fences, lists,
        # headings and have them render as such (instead of raw `| col |`
        # text).
        if not self._buffer:
            return Text("助手  ", style=UI.muted)
        return Group(
            Text("助手", style=UI.muted),
            Markdown(_sanitize_table_pipes(self._buffer)),
        )


class ThinkingBlock(Collapsible):
    """Streamed `reasoning_content`. Default collapsed; the title carries a
    live character count while streaming, so the folded block still shows
    progress at a glance. Click the header (or focus it and press Enter) to
    toggle this one; Ctrl+T from anywhere folds/unfolds every block at once.

    Title shows the final character count (plus the reasoning token count
    when known) once the stream finishes.
    """

    DEFAULT_CSS = """
    ThinkingBlock {
        margin: 0;
        background: $background;
        border-left: solid $border-blurred;
        border-top: none;
        padding-bottom: 0;
        padding-left: 0;
    }
    ThinkingBlock > CollapsibleTitle { color: $text-muted; text-style: none; }
    ThinkingBlock > CollapsibleTitle:hover {
        background: $panel;
        color: $foreground;
    }
    ThinkingBlock > CollapsibleTitle:focus {
        background: $panel;
        color: $foreground;
    }
    ThinkingBlock > Contents { padding: 0 0 0 2; }
    ThinkingBlock #thinking-body { color: $text-muted; }
    ThinkingBlock.streaming > CollapsibleTitle { color: $text-muted; }
    ThinkingBlock.streaming { border-left: solid $primary; }
    ThinkingBlock > .thinking-summary-preview {
        height: auto;
        padding: 0 1 0 2;
        color: $text-muted;
        display: none;
    }
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._streaming = True
        self.summary = ""
        self.summary_source = ""
        self.round_number = 0
        self._reasoning_tokens = 0
        self.summary_cancel: Callable[[], None] | None = None
        self._summary_preview = Static("", classes="thinking-summary-preview", markup=False)
        self._body = Static("", id="thinking-body", markup=False)
        super().__init__(self._body, title="thinking · 流式中…", collapsed=True)

    def compose(self):
        for index, child in enumerate(super().compose()):
            yield child
            if index == 0:
                yield self._summary_preview

    def set_summary(self, text: str, source: str = "generated") -> None:
        self.summary = normalize_summary(text)
        self.summary_source = source
        self._summary_preview.update(Text(self.summary, style=UI.thinking_summary))
        self._summary_preview.display = bool(self.summary)

    def set_round_number(self, number: int) -> None:
        if number > 0:
            self.round_number = number
            self._refresh_title()

    def _refresh_title(self) -> None:
        label = f"第 {self.round_number} 轮思考" if self.round_number else "思考"
        self.set_class(self._streaming, "streaming")
        parts = [label, f"{len(self._buffer):,} 字符"]
        if self._streaming:
            parts[0] = label + "中"
        elif self._reasoning_tokens:
            parts.append(f"{self._reasoning_tokens:,} tokens")
        self.title = " · ".join(parts).replace("[", "\\[")

    def _refresh_body(self) -> None:
        self._body.update(self._buffer)

    def on_unmount(self) -> None:
        if self.summary_cancel is not None:
            self.summary_cancel()

    def append_text(self, text: str) -> None:
        self._buffer += text
        self._refresh_body()
        # Keep the (collapsed) title informative: a live character count
        # shows how much reasoning has streamed in without unfolding.
        self._refresh_title()

    def finalize(self, reasoning_tokens: int) -> None:
        self._streaming = False
        self._reasoning_tokens = reasoning_tokens
        self._refresh_title()

    def discard(self) -> None:
        """Cancel the summary worker and remove uncommitted reasoning."""
        if self.summary_cancel is not None:
            self.summary_cancel()
        self.remove()

    @property
    def text(self) -> str:
        return self._buffer


class CompactionBlock(Collapsible):
    """Live compaction preview, kept separate from assistant messages."""

    DEFAULT_CSS = """
    CompactionBlock {
        margin: 0 0 1 0;
        border-left: thick $primary;
    }
    CompactionBlock > CollapsibleTitle { color: $primary; }
    """

    def __init__(self, *, automatic: bool) -> None:
        self.automatic = automatic
        self.phase = "准备压缩…"
        self.text = ""
        self.reasoning = ""
        self.outcome: str | None = None
        self._body = Static(Text("等待模型输出…", style="dim"))
        super().__init__(self._body, title="📦 上下文压缩中…", collapsed=False)

    def update_progress(self, kind: str, text: str) -> None:
        if kind == "start":
            self.phase = text
            self.text = self.reasoning = ""
        elif kind == "phase":
            self.phase = text
        elif kind == "content":
            self.text += text
        elif kind == "reasoning":
            self.reasoning += text
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        label = "自动压缩" if self.automatic else "压缩"
        self.title = f"📦 {label} · {self.phase} · 摘要 {len(self.text):,} 字符"
        if self.reasoning:
            self.title += f" · 思路 {len(self.reasoning):,} 字符"
        parts = []
        if self.reasoning:
            parts.append(Text(self.reasoning[-4000:], style="dim"))
        if self.text:
            parts.append(Markdown(_sanitize_table_pipes(self.text)))
        self._body.update(Group(*(parts or [Text("等待模型输出…", style="dim")])))

    def finish(self, outcome: str) -> None:
        self.outcome = outcome
        self.phase = {
            "applied": "已完成",
            "failed": "未应用，原上下文保留",
            "cancelled": "已取消，原上下文保留",
        }[outcome]
        self._refresh_preview()
        self.collapsed = True

    def snapshot(self) -> dict:
        return {
            "automatic": self.automatic, "phase": self.phase,
            "content": self.text, "reasoning": self.reasoning[-4000:],
            "content_chars": len(self.text), "reasoning_chars": len(self.reasoning),
        }


class ToolCallBlock(Collapsible):
    """One tool call with its result. Default collapsed; click the
    header to toggle. Title shows `● <name> <short args> <status>`;
    the body holds the pretty-printed args and the (truncated) result.
    """

    DEFAULT_CSS = """
    ToolCallBlock {
        margin: 0;
        background: $background;
        border-left: solid $border-blurred;
        border-top: none;
        padding-bottom: 0;
        padding-left: 0;
    }
    ToolCallBlock > CollapsibleTitle { color: $foreground 55%; text-style: none; }
    ToolCallBlock > CollapsibleTitle:hover {
        background: $panel;
        color: $primary;
    }
    ToolCallBlock > CollapsibleTitle:focus {
        background: $panel;
        color: $foreground;
    }
    ToolCallBlock > Contents { padding: 0 0 0 2; }
    ToolCallBlock #toolcall-body { color: $text-muted; }
    ToolCallBlock.running > CollapsibleTitle { color: $text-muted; }
    ToolCallBlock.running { border-left: solid $primary; }
    ToolCallBlock.has-error > CollapsibleTitle { color: $error; }
    """

    def __init__(self, name: str, args: dict) -> None:
        self._tool_name = name  # not _name — Widget already uses that
        self._args = args
        self._result: str | None = None
        self._blocked = False
        # markup=False so result text containing '[' or ']' won't be parsed.
        self._body = Static(self._build_body(), id="toolcall-body", markup=False)
        super().__init__(self._body, title=self._build_title(), collapsed=True)
        self.add_class("running")

    def set_result(self, result: str, blocked: bool = False) -> None:
        self._result = result
        self._blocked = blocked or result.startswith(TOOL_ERROR_PREFIXES)
        self.remove_class("running")
        self.set_class(self._blocked, "has-error")
        self._body.update(self._build_body())
        self.title = self._build_title()

    @property
    def is_pending(self) -> bool:
        return self._result is None

    def _short_args(self) -> str:
        for key in ("path", "pattern", "command", "query", "url", "name"):
            value = self._args.get(key)
            if isinstance(value, str) and value:
                preview = " ".join(value.split())
                break
        else:
            try:
                preview = json.dumps(self._args, ensure_ascii=False)
            except Exception:
                preview = str(self._args)
        short = Text(preview, no_wrap=True, overflow="ellipsis")
        short.truncate(60, overflow="ellipsis")
        return short.plain

    def _build_title(self) -> str:
        if self._result is None:
            status = "执行中…"
        elif self._blocked:
            status = "✗ 异常"
        else:
            status = "✓"
        title = f"● {self.description}  {status}"
        # Escaping '[' prevents Textual markup parser from treating
        # JSON brackets (e.g. ["vec"]) as malformed markup tags.
        return title.replace('[', '\\[')

    @property
    def description(self) -> str:
        return f"{self._tool_name}  {self._short_args()}"

    def _build_body(self) -> RenderableType:
        try:
            args_pretty = json.dumps(self._args, ensure_ascii=False, indent=2)
        except Exception:
            args_pretty = str(self._args)
        parts: list[RenderableType] = [
            Text(f"工具 · {self._tool_name}", style=UI.muted), Text(args_pretty), Text(""),
        ]

        if self._result is None:
            parts.append(Text("执行中…", style="italic dim"))
        else:
            # For edit_file / edit_lines / multi_edit the diff is already
            # stripped before set_result is called (see AgentApp), so a
            # uniform snippet path works for every tool.
            snippet = (
                self._result
                if len(self._result) <= 4000
                else self._result[:4000] + f"\n…[+{len(self._result) - 4000} chars]"
            )
            parts.append(Text(snippet))
        return Group(*parts)


class WorkbenchOverview(Static):
    """The current user goal, kept visible independently of background tasks."""

    DEFAULT_CSS = """
    WorkbenchOverview {
        height: auto;
        margin: 1 0;
        padding: 0 1;
        color: $foreground;
    }
    """

    def set_goal(self, goal: str) -> None:
        goal = " ".join(goal.split()) or "等待输入任务"
        if len(goal) > 100:
            goal = goal[:99] + "…"
        if goal != getattr(self, "_goal", None):
            self._goal = goal
            self.update(Text.assemble(("当前目标\n", UI.muted), goal))


class ThinkingSummaryPanel(Static):
    """The latest main-conversation summary in the workbench sidebar."""

    DEFAULT_CSS = """
    ThinkingSummaryPanel {
        height: auto;
        max-height: 12;
        margin: 1 0;
        padding: 0 1;
        border-top: solid $border-blurred;
        color: $foreground;
        display: none;
    }
    """

    def set_summary(self, text: str, *, active: bool = True) -> None:
        label = "当前思考" if active else "思考摘要"
        state = (text, label)
        if state != getattr(self, "_last_state", None):
            self._last_state = state
            self.update(Text.assemble((label + "\n", UI.muted), (text, UI.thinking_summary)))


class DiffBlock(Collapsible):
    """Standalone diff view shown right after a successful edit_file or
    edit_lines call. Default expanded — the change is the headline; the
    user shouldn't have to dig for it. Title format: `📝 path  +N -M`.
    """

    DEFAULT_CSS = """
    DiffBlock {
        margin: 0 0 1 0;
        background: $background;
        border-left: thick $primary;
        padding-bottom: 0;
        padding-left: 0;
    }
    DiffBlock > CollapsibleTitle { color: $primary; }
    DiffBlock > CollapsibleTitle:hover {
        background: $panel;
        color: $primary;
    }
    DiffBlock > CollapsibleTitle:focus {
        background: $panel;
        color: $foreground;
    }
    DiffBlock > Contents { padding: 0 0 0 2; }
    """

    def __init__(self, path: str, diff_text: str) -> None:
        self._diff_text = diff_text
        body = Static(self._render_diff(diff_text), markup=False)
        super().__init__(
            body, title=self._make_title(path, diff_text), collapsed=False
        )

    @staticmethod
    def _make_title(path: str, diff_text: str) -> str:
        plus = minus = 0
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                plus += 1
            elif line.startswith("-") and not line.startswith("---"):
                minus += 1
        return f"📝 {path}  +{plus} -{minus}"

    @staticmethod
    def _render_diff(diff_text: str) -> Text:
        """Highlight additions and deletions on quiet semantic backgrounds."""
        t = Text()
        for line in diff_text.splitlines(keepends=True):
            head = line.rstrip("\n")
            if head.startswith("+++") or head.startswith("---"):
                t.append(line, style="bold")
            elif head.startswith("@@"):
                t.append(line, style=f"bold {UI.accent}")
            elif head.startswith("+"):
                t.append(line, style=f"bold {UI.text} on {UI.added}")
            elif head.startswith("-"):
                t.append(line, style=f"bold {UI.text} on {UI.removed}")
            else:
                t.append(line)
        return t


class ExploreSummaryBlock(Collapsible):
    """Rendered summary of a completed exploration span."""

    DEFAULT_CSS = """
    ExploreSummaryBlock {
        margin: 0 0 1 0;
        background: $background;
        border-left: thick $primary;
        padding-bottom: 0;
        padding-left: 0;
    }
    ExploreSummaryBlock > CollapsibleTitle { color: $primary; }
    ExploreSummaryBlock > CollapsibleTitle:hover {
        background: $panel;
        color: $primary;
    }
    ExploreSummaryBlock > CollapsibleTitle:focus {
        background: $panel;
        color: $foreground;
    }
    ExploreSummaryBlock > Contents { padding: 0 0 0 2; }
    """

    def __init__(self, message: dict) -> None:
        self._message = message
        body = Static(Markdown(_sanitize_table_pipes(message.get("content") or "")))
        super().__init__(body, title=self._title(message), collapsed=False)

    @staticmethod
    def _title(message: dict) -> str:
        explore_id = message.get("explore_id") or "explore"
        kind = message.get("explore_kind") or "custom"
        return f"explore summary · {explore_id} · {kind}"


# ───────── sidebar ─────────

class CheckpointBlock(Static):
    """Sidebar snapshot of the current conversation working state."""

    DEFAULT_CSS = """
    CheckpointBlock {
        margin: 1 0;
        padding: 0 1;
        border: round $primary;
    }
    """

    def __init__(self) -> None:
        self._checkpoint: dict | None = None
        super().__init__(self._build())

    def set_checkpoint(self, checkpoint: dict | None) -> None:
        self._checkpoint = checkpoint
        self.update(self._build())

    @staticmethod
    def _short(value: str, limit: int = 72) -> str:
        text = (value or "").replace("\n", " ").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    def _append_items(
        self, t: Text, label: str, items: list[str], *, limit: int = 3
    ) -> None:
        if not items:
            return
        t.append(f"\n{label}", style=f"bold {UI.accent}")
        extra = max(0, len(items) - limit)
        for item in items[:limit]:
            t.append("\n  - ", style="dim")
            t.append(self._short(item), style="dim")
        if extra:
            t.append(f"\n  … +{extra}", style="dim")

    def _build(self) -> Text:
        cp = self._checkpoint or {}
        t = Text()
        t.append("任务计划", style=UI.muted)
        if not cp:
            t.append("\n  (empty)", style="dim italic")
            return t
        status = cp.get("status") or "?"
        confidence = cp.get("confidence") or "?"
        t.append(f"  {status} · {confidence}", style="dim")
        goal = self._short(str(cp.get("goal") or ""), 80)
        if goal:
            t.append("\n目标  ", style="bold")
            t.append(goal)
        focus = self._short(str(cp.get("current_focus") or ""), 80)
        if focus:
            t.append("\n当前步骤 ", style="bold")
            t.append(focus, style="dim")
        blockers = [str(x) for x in cp.get("blockers") or []]
        next_steps = [str(x) for x in cp.get("next_steps") or []]
        refs = [str(x) for x in cp.get("active_refs") or []]
        self._append_items(t, "阻碍", blockers, limit=2)
        self._append_items(t, "下一步", next_steps, limit=3)
        if refs:
            t.append("\n参考  ", style="bold")
            t.append(", ".join(refs[:4]), style="dim")
            if len(refs) > 4:
                t.append(f", … +{len(refs) - 4}", style="dim")
        return t


class TodoBlock(Static):
    """Persistent TODO list maintained by the model via `todo_tool`.

    Only ONE instance lives in the conversation at a time — every call
    overwrites its contents in place, so the user sees a single panel
    that ticks items off as work progresses (rather than a stream of
    snapshots).
    """

    DEFAULT_CSS = """
    TodoBlock {
        margin: 1 0;
        padding: 0 1;
        border: round $primary;
    }
    TodoBlock.all-done { border: round $warning; }
    """

    def __init__(self) -> None:
        self._items: list[dict] = []
        super().__init__(self._build())

    def set_items(self, items: list[dict]) -> None:
        self._items = items
        self.update(self._build())

    def _build(self) -> Text:
        n = len(self._items)
        n_done = sum(1 for i in self._items if i.get("status") == "completed")
        n_prog = sum(1 for i in self._items if i.get("status") == "in_progress")
        n_pend = n - n_done - n_prog

        t = Text()
        t.append("待办  ", style=UI.muted)
        t.append(
            f"({n_prog} 进行中 · {n_done} 完成 · {n_pend} 待办)",
            style="dim",
        )
        if not self._items:
            t.append("\n  (empty)", style="dim italic")
            return t
        for item in self._items:
            status = item.get("status", "pending")
            content = item.get("content", "")
            t.append("\n")
            if status == "completed":
                t.append("  [", style="dim")
                t.append("✓", style=f"bold {UI.success}")
                t.append("] ", style="dim")
                t.append(content, style="strike dim")
            elif status == "in_progress":
                t.append("  [", style="bold")
                t.append("▶", style=f"bold {UI.accent}")
                t.append("] ", style="bold")
                t.append(content, style=f"bold {UI.accent}")
            else:  # pending
                t.append("  [ ] ", style="dim")
                t.append(content, style="dim")
        return t


class SubagentsBlock(Static):
    """Sidebar widget showing live `SubagentSession`s.

    Mirrors `AgentApp._live_subagents` once a second. A session lives
    until the parent calls `end_agent` or the idle reaper kicks it out,
    so the panel is visible whenever at least one session exists —
    including ones currently sitting at `idle` between rounds.
    """

    DEFAULT_CSS = """
    SubagentsBlock {
        margin: 1 0;
        padding: 0 1;
        border: round $error;
    }
    """

    # phase → (icon, style). Distinct hues so the eye can tell at a
    # glance whether the subagent is spending tokens (thinking /
    # answering), burning wallclock on a tool, parked on task events
    # (waiting), has an answer waiting (ready), or is between rounds
    # (idle).
    _PHASE_GLYPH = {
        "thinking":  ("◐", f"bold {UI.accent}"),
        "answering": ("▶", f"bold {UI.accent}"),
        "tool":      ("⚙", f"bold {UI.accent}"),
        "compacting": ("📦", f"bold {UI.accent}"),
        "waiting":   ("◌", f"bold {UI.warning}"),
        "ready":     ("▣", f"bold {UI.success}"),
        "idle":      ("○", UI.muted),
        "done":      ("✓", f"bold {UI.success}"),
        "error":     ("✗", f"bold {UI.error}"),
    }

    def render_sessions(self, sessions: list[SubagentSession]) -> None:
        t = Text()
        t.append("子 agent  ", style=UI.muted)
        n_busy = sum(
            1 for s in sessions
            if s.phase in ("thinking", "answering", "tool", "compacting")
        )
        n_waiting = sum(1 for s in sessions if s.phase == "waiting")
        n_ready = sum(1 for s in sessions if s.phase == "ready")
        n_idle = sum(1 for s in sessions if s.phase == "idle")
        t.append(
            f"({n_busy} 跑 · {n_waiting} 等 · {n_ready} 待领 · {n_idle} 闲)",
            style="dim",
        )
        if not sessions:
            t.append("\n  (empty)", style="dim italic")
            self._update_live_text(t)
            return
        now = time.monotonic()
        for s in sessions:
            icon, style = self._PHASE_GLYPH.get(s.phase, ("·", "dim"))
            t.append("\n  ")
            t.append(f"{icon} ", style=style)
            t.append(f"{s.id}", style=style)
            t.append(f"  轮 {s.turn}", style="dim")
            if s.phase == "idle":
                idle_for = now - s.last_active_at
                t.append(f"  闲 {idle_for:5.1f}s", style="dim")
            else:
                elapsed = now - s.started_at
                t.append(f"  {elapsed:5.1f}s", style="dim")
            if s.phase == "compacting":
                t.append("  · 上下文压缩中…", style=UI.accent)
            elif s.last_tool and s.phase != "idle":
                t.append(f"  · {s.last_tool}", style=UI.accent)
            if s.tokens_in or s.tokens_out:
                t.append(
                    f"\n    [{s.tokens_in:,}↓ / {s.tokens_out:,}↑]",
                    style="dim",
                )
            # Last-call context usage as a percentage of the model's
            # window. Color jumps once the parent should think about
            # nudging a compact_self via chat_agent.
            if s.context_limit and s.last_prompt_tokens:
                pct = s.last_prompt_tokens * 100.0 / s.context_limit
                if pct >= 85:
                    ctx_style = f"bold {UI.error}"
                elif pct >= 70:
                    ctx_style = f"bold {UI.warning}"
                else:
                    ctx_style = "dim"
                t.append(f"  ctx {pct:.0f}%", style=ctx_style)
            if s.phase == "thinking" and getattr(s, "thinking_summary", ""):
                t.append(f"\n    {s.thinking_summary}", style=UI.thinking_summary)
            elif s.prompt_preview:
                t.append(f'\n    "{s.prompt_preview}"', style="dim italic")
        self._update_live_text(t)

    def _update_live_text(self, text: Text) -> None:
        line_count = text.plain.count("\n") + 1
        layout = line_count != getattr(self, "_last_line_count", None)
        self._last_line_count = line_count
        self.update(text, layout=layout)


class TerminalTabPane(VerticalScroll):
    """Live read-only view of one PTY terminal session."""

    DEFAULT_CSS = """
    TerminalTabPane { padding: 0 1; }
    """

    def __init__(self, sess) -> None:
        super().__init__()
        self.sess = sess
        self._content = Static()

    def on_mount(self) -> None:
        self.mount(self._content)
        self.refresh_from_session()

    def refresh_from_session(self) -> None:
        rc = self.sess.proc.poll()
        if rc is None and not self.sess.closed:
            status = "running"
            style = f"bold {UI.accent}"
        else:
            status = f"exited {rc}"
            style = f"bold {UI.success}" if rc == 0 else f"bold {UI.error}"
        header = Text()
        header.append(f"{self.sess.id}  ", style=f"bold {UI.accent}")
        header.append(self.sess.name, style="bold")
        header.append(f"  {status}", style=style)
        header.append(f"\ncmd: {self.sess.command}", style="dim")
        header.append(f"\nlog: {self.sess.log_path}\n\n", style="dim")
        body = Text.from_ansi(self.sess.tail or "(no output yet)")
        self._content.update(Group(header, body))
        self.scroll_end(animate=False)


class SubagentTabPane(VerticalScroll):
    """Live + read-only transcript view for one SubagentSession.

    Lives inside a TabPane in the top-level TabbedContent. Renders
    `sess.messages` as the same widget vocabulary the main conversation
    uses (UserBubble / ThinkingBlock / AssistantMessage / ToolCallBlock)
    so the parent's "what did the subagent do?" view feels familiar.

    Two complementary paths populate it:

    1. **Live streaming** — `_run_subagent_round` pushes reasoning /
       content chunks into `_live_thinking` / `_live_answer` as they
       arrive and mounts a ToolCallBlock as each tool call lands,
       so the user sees token-level output in real time. After each
       message commits to `sess.messages`, the round runner calls
       `mark_round_committed()` to advance `_rendered_idx` so the
       fallback path doesn't repaint the same content.

    2. **Fallback replay** — the 1 Hz `_refresh_subagent_panel` tick
       calls `refresh_from_session()`, which renders any message in
       `sess.messages` that hasn't been live-mounted (e.g. the user
       prompt, or content from a state restore).

    `_tool_blocks` maps tool_call_id → ToolCallBlock so a later
    `role=tool` message in the replay path can attach its result to
    the right block. The live path uses the same map so tool result
    set after async tool execution finds the block created at
    tool-call dispatch.

    `_pending_mounts` covers the spawn-time race: the round task
    starts before the TabPane is mounted into the DOM. Live widgets
    constructed during that window queue here and flush in `on_mount`.
    """

    DEFAULT_CSS = """
    SubagentTabPane { padding: 0 1; }
    """

    def __init__(self, sess: SubagentSession) -> None:
        super().__init__()
        self.sess = sess
        self._rendered_idx = 0
        self._tool_blocks: dict[str, "ToolCallBlock"] = {}
        # Live-stream widgets owned by the in-flight round. Cleared at
        # round end (finalize_*) or on stream error (remove_live_blocks).
        self._live_thinking: ThinkingBlock | None = None
        self._live_answer: AssistantMessage | None = None
        # Widgets created before on_mount runs (spawn-time race). Flushed
        # in order by on_mount AFTER refresh_from_session has rendered
        # the user prompt, so the DOM order ends up user → thinking →
        # answer → tools.
        self._pending_mounts: list = []
        self._thinking_round = 0
        self._transcript_ready = False

    def _append_transcript(self, widget) -> None:
        """Append each entry directly, preserving live and replay order."""
        if self._transcript_ready:
            self.mount(widget)
        else:
            self._pending_mounts.append(widget)

    def on_mount(self) -> None:
        # Textual dispatches on_mount before is_mounted becomes true. Use
        # an explicit readiness flag so replay/flush doesn't requeue forever.
        self._transcript_ready = True
        # Render history (system msgs skipped, user prompt mounted) FIRST
        # so the spawn prompt lands above any live widgets the round
        # task already queued via _pending_mounts.
        try:
            self.refresh_from_session()
        except Exception:
            pass
        if self._pending_mounts:
            pending, self._pending_mounts = self._pending_mounts, []
            for w in pending:
                try:
                    self._append_transcript(w)
                    if isinstance(w, ThinkingBlock) and not w._streaming:
                        self._thinking_round = max(self._thinking_round, w.round_number)
                except Exception:
                    pass
            self.scroll_end(animate=False)

    # ───────── live streaming hooks (called from _run_subagent_round) ─────────

    def start_thinking(self) -> ThinkingBlock:
        """Mount a fresh ThinkingBlock for the current LLM call.
        Idempotent — repeated calls within the same round are no-ops."""
        if self._live_thinking is not None:
            return self._live_thinking
        self._live_thinking = ThinkingBlock()
        self._live_thinking.set_round_number(self._thinking_round + 1)
        self._append_transcript(self._live_thinking)
        return self._live_thinking

    def add_compaction_block(self, block: CompactionBlock) -> None:
        self._append_transcript(block)
        if self.is_mounted:
            self.scroll_end(animate=False)

    def append_thinking(self, text: str) -> None:
        if self._live_thinking is None:
            return
        self._live_thinking.append_text(text)
        if self.is_mounted:
            self.scroll_end(animate=False)

    def finalize_thinking(self, reasoning_tokens: int) -> None:
        if self._live_thinking is not None:
            self._live_thinking.finalize(reasoning_tokens)
            self._thinking_round = self._live_thinking.round_number
            self._live_thinking = None

    def start_answer(self) -> None:
        if self._live_answer is not None:
            return
        self._live_answer = AssistantMessage()
        self._append_transcript(self._live_answer)

    def append_answer(self, text: str) -> None:
        if self._live_answer is None:
            return
        self._live_answer.append_text(text)
        if self.is_mounted:
            self.scroll_end(animate=False)

    def sync_answer(self, text: str) -> None:
        """Synchronize a live answer with its final committed content."""
        if self._live_answer is not None and self._live_answer.text != text:
            self._live_answer.set_text(text)

    def finalize_answer(self) -> None:
        # Just drop the ref — the widget stays mounted as historical
        # content. Same shape as ThinkingBlock.finalize but no title
        # to refresh.
        self._live_answer = None

    def add_tool_block(
        self, tool_call_id: str, name: str, args: dict
    ) -> "ToolCallBlock":
        block = ToolCallBlock(name, args)
        if tool_call_id:
            self._tool_blocks[tool_call_id] = block
        self._append_transcript(block)
        if self.is_mounted:
            self.scroll_end(animate=False)
        return block

    def set_tool_result(
        self, tool_call_id: str, result: str, blocked: bool = False
    ) -> None:
        block = self._tool_blocks.get(tool_call_id)
        if block is not None:
            block.set_result(result, blocked=blocked)

    def add_tool_diff(self, path: str, diff: str) -> None:
        self._append_transcript(DiffBlock(path, diff))

    def remove_live_blocks(self) -> None:
        """Drop the in-flight ThinkingBlock / AssistantMessage on a
        stream error or cancellation. Caller should follow up with
        mount_exception_notice() so the user sees what went wrong
        instead of a silent gap."""
        live = (self._live_thinking, self._live_answer)
        self._pending_mounts = [widget for widget in self._pending_mounts if widget not in live]
        if self._live_thinking is not None:
            try:
                self._live_thinking.discard()
            except Exception:
                pass
            self._live_thinking = None
        if self._live_answer is not None:
            try:
                self._live_answer.remove()
            except Exception:
                pass
            self._live_answer = None

    def mount_exception_notice(self, text: str, *, style: str = f"bold {UI.error}") -> None:
        block = Static(Text(text, style=style), markup=False)
        self._append_transcript(block)
        if self.is_mounted:
            self.scroll_end(animate=False)

    def mark_round_committed(self) -> None:
        """Advance _rendered_idx to len(sess.messages) so the 1 Hz
        fallback won't repaint messages the live path already
        mounted. Called after each sess.messages.append in the round
        runner."""
        self._rendered_idx = len(self.sess.messages)

    # ───────── fallback replay (1 Hz tick + on_mount) ─────────

    def refresh_from_session(self) -> None:
        """Append widgets for any messages that arrived since last call.
        Cheap when nothing is new (early-out)."""
        msgs = self.sess.messages
        if self._rendered_idx >= len(msgs):
            return
        for m in msgs[self._rendered_idx:]:
            self._render_one(m)
        self._rendered_idx = len(msgs)
        # Keep the view glued to the bottom while the subagent is
        # actively producing output — same UX as the main conversation.
        if self.is_mounted:
            self.scroll_end(animate=False)

    def _render_one(self, m: dict) -> None:
        role = m.get("role")
        if role == "system":
            # The framework prompt + optional role spec — long, static,
            # not interesting in a transcript view.
            return
        if role == "user":
            if not m.get("ddtui_kind") and steer_text(str(m.get("content") or "")) is None:
                self._thinking_round = 0
            self._append_transcript(UserBubble(m.get("content") or ""))
            return
        if role == "assistant":
            rc = m.get("reasoning_content")
            if rc:
                self._thinking_round = max(self._thinking_round + 1, round_from_message(m))
                tb = ThinkingBlock()
                tb.append_text(rc)
                tb.set_round_number(self._thinking_round)
                tb.set_summary(*summary_from_message(m))
                # Historical reasoning has no live token count; pass 0 so
                # `finalize` titles the block with just its character count.
                # It stays collapsed by default (matching the live path and
                # the main conversation) — the count in the title signals
                # there's reasoning to unfold.
                tb.finalize(0)
                self._append_transcript(tb)
            content = m.get("content") or ""
            if content:
                am = AssistantMessage()
                am.append_text(content)
                self._append_transcript(am)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name", "?")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {"_raw": raw_args}
                self.add_tool_block(tc.get("id") or "", name, args)
            return
        if role == "tool":
            tcid = m.get("tool_call_id") or ""
            content = m.get("content") or ""
            blk = self._tool_blocks.get(tcid)
            if blk is not None:
                blk.set_result(content)
            else:
                # Shouldn't happen — every tool message follows an
                # assistant tool_call with the same id. Render a
                # standalone fallback rather than dropping the data.
                self._append_transcript(Static(Text(f"[orphan tool result] {content}")))
            return


class TasksBlock(Static):
    """Sidebar widget showing live managed async tasks."""

    DEFAULT_CSS = """
    TasksBlock {
        margin: 1 0;
        padding: 0 1;
        border: round $primary;
    }
    """

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        one_line = " ".join(text.split())
        if len(one_line) <= limit:
            return one_line
        return one_line[: limit - 1] + "…"

    def render_tasks(self, tasks: list[AsyncTask]) -> None:
        t = Text()
        t.append("后台任务  ", style=UI.muted)
        t.append(f"({len(tasks)} 跑)", style="dim")
        if not tasks:
            t.append("\n  (empty)", style="dim italic")
            self._update_live_text(t)
            return
        for task in tasks:
            status, rc, elapsed = async_task_status(task)
            label = self._clip(task.name or task.command, 30)
            output = self._clip(str(task.output_path), 34)
            t.append("\n  ")
            t.append("▶ ", style=f"bold {UI.accent}")
            t.append(f"{task.id}", style=f"bold {UI.accent}")
            t.append(f"  {elapsed:5.1f}s ", style="dim")
            t.append(label)
            if rc is not None:
                t.append(f" rc={rc}", style="dim")
            t.append("\n     out: ", style="dim")
            t.append(output, style="dim")
        self._update_live_text(t)

    def _update_live_text(self, text: Text) -> None:
        line_count = text.plain.count("\n") + 1
        layout = line_count != getattr(self, "_last_line_count", None)
        self._last_line_count = line_count
        self.update(text, layout=layout)


# ───────── input ─────────

class SlashPopup(Static):
    """Auto-shown command hint above the input box.

    Pure display, in two modes. Before the first space it lists every
    slash command whose name still matches the typed prefix. After it,
    for commands that declare candidates (see `arg_candidates`), it
    switches to filtering those — so ``/model dee`` narrows to matching
    model ids. Anything else hides it. There's no keyboard navigation:
    the user keeps typing in the input and reads the popup as a hint,
    or presses Tab to accept the top row (see `completion`).
    """

    #: Resolves argument candidates for a command name, e.g. "/model" →
    #: [(id, note), ...]; returns [] for commands without any. A
    #: callback rather than a static table because the candidates are
    #: live state — model ids depend on the active provider.
    arg_candidates: Callable[[str], list[tuple[str, str]]] | None = None

    #: Input text that accepting the top row would produce, or None
    #: when the popup is hidden. A class attribute (not set in
    #: __init__) so it exists even before the widget is mounted.
    _completion: str | None = None

    DEFAULT_CSS = """
    SlashPopup {
        height: auto;
        padding: 0 1;
        border: round $primary;
        background: $background;
        overflow-y: auto;
        display: none;
    }
    """

    # (display form, description). Display form may include an
    # ``<arg>`` placeholder so the user sees the expected shape at a
    # glance; the matcher only looks at the first whitespace-delimited
    # token so the placeholder doesn't break prefix matching.
    COMMANDS: tuple[tuple[str, str], ...] = (
        ("/clear", "清空对话（保留 system prompt + AGENTS.md）"),
        ("/compact", "归档并压缩历史，原文可检索恢复"),
        ("/save <name>", "保存到 ~/.ddtui/history/<name>.json"),
        ("/load <name>", "读回保存的对话"),
        ("/resume [name]", "恢复对话；不带 name 时打开选择窗口"),
        ("/remote [status|on|off]", "管理 VPS 远控连接"),
        ("/collab [bind|on|off|tasks|task]", "可选 Git 交接与任务领取/释放"),
        ("/list-history", "列出已保存对话"),
        ("/provider [deepseek|codex|local]", "查看 / 切换 provider"),
        ("/model [<id>]", "列出候选 / 切换模型（下一轮请求生效）"),
        ("/effort [<level>]", "查看 / 切换 reasoning effort"),
        ("/rewind", "退回上一条用户消息（文本回填输入框）"),
        ("/rethink", "删除最近一轮助手回复，让模型重新思考"),
        ("/help", "显示帮助"),
        ("/exit", "退出"),
    )

    def update_for_text(self, text: str) -> None:
        """Show/hide and filter based on current input text."""
        # A newline means the user is composing a prompt, not a command.
        if not text.startswith("/") or "\n" in text:
            self._hide()
            return
        name, sep, arg = text.partition(" ")
        if sep:
            self._show_arguments(name, arg)
            return
        matches = [
            (cmd, desc) for cmd, desc in self.COMMANDS
            if cmd.split(" ", 1)[0].startswith(text)
        ]
        if not matches:
            self._hide()
            return
        top = matches[0][0]
        head, sep, _ = top.partition(" ")
        # A display form like "/save <name>" carries a placeholder, so
        # land the cursor past a space and straight into argument mode;
        # bare commands like "/clear" complete to just themselves.
        self._completion = f"{head} " if sep else head
        self._paint(
            "Slash 命令", "  (Tab 补全 · 继续输入过滤 · Enter 发送)", matches
        )

    def _show_arguments(self, name: str, arg: str) -> None:
        """Filter one command's argument candidates by what's typed."""
        typed = arg.strip()
        # Only the first token is a candidate; past that the user is
        # writing free text (`/save my notes`) and hints stop helping.
        if any(c.isspace() for c in typed):
            self._hide()
            return
        matches = [
            (value, note) for value, note in self._candidates_for(name)
            if value.startswith(typed)
        ]
        if not matches:
            self._hide()
            return
        self._completion = f"{name} {matches[0][0]}"
        self._paint(
            f"{name} 候选", "  (Tab 补全 · 继续输入过滤 · Enter 发送)", matches
        )

    def _candidates_for(self, name: str) -> list[tuple[str, str]]:
        if self.arg_candidates is None:
            return []
        try:
            return list(self.arg_candidates(name))
        except Exception:
            # Candidates come from live state (provider catalogs, disk
            # caches); a hint popup must never take the input box down
            # with it.
            return []

    def _paint(
        self,
        title: str,
        subtitle: str,
        rows: list[tuple[str, str]],
    ) -> None:
        # Don't name this `_render` — that's a Textual Widget hook that
        # takes no arguments and must return a Visual; overriding it
        # crashes the moment the popup becomes visible.
        # Align descriptions on a single column so the list reads as a
        # table rather than a ragged left edge.
        width = max(len(left) for left, _ in rows)
        t = Text()
        t.append(title, style=f"bold {UI.accent}")
        t.append(subtitle, style="dim")
        for left, right in rows:
            t.append("\n  ")
            t.append(left.ljust(width), style=f"bold {UI.accent}")
            if right:
                t.append("  ")
                t.append(right, style="dim")
        self.update(t)
        if not self.display:
            self.display = True

    def completion(self) -> str | None:
        """Input text that accepting the top row would produce.

        None when there's nothing to accept (popup hidden), which is
        the caller's cue to let Tab do whatever it normally does.
        """
        return self._completion

    def _hide(self) -> None:
        self._completion = None
        if self.display:
            self.display = False


class MultilineInput(TextArea):
    """Multi-line prompt box.

    User-facing key bindings:
      Enter           → submit (start a new turn / queue if busy)
      Tab             → accept the slash popup's top row; falls back
                        to normal focus movement when it's hidden
      Option+Enter    → insert literal newline   (textual: alt+enter)
      Cmd+Enter       → submit as STEER          (textual: ctrl+enter,
                        via iTerm2 escape `[13;5u` for ⌘ Return)
      Ctrl+X          → forward to App.action_cancel_pending

    The textual binding names below use `alt+enter` and `ctrl+enter`
    because textual identifies modifiers by Linux/X11 conventions —
    macOS Option physically *is* alt, and Cmd+Enter is converted by
    iTerm2 (or the user's terminal) into the CSI u escape that textual
    decodes as ctrl+enter. macOS Terminal.app cannot do this conversion
    so Cmd+Enter won't reach steer there; use Option+Enter only.
    """

    BINDINGS = [
        Binding("enter", "do_submit", show=False, priority=True),
        # priority: plain `tab` is App-level focus_next, and TextArea
        # only swallows it under tab_behavior="indent" (not our case).
        Binding("tab", "do_complete", show=False, priority=True),
        # alt+enter ≡ Option+Enter on macOS
        Binding("alt+enter", "do_newline", show=False, priority=True),
        # ctrl+enter ≡ Cmd+Enter once iTerm2 maps ⌘ Return → [13;5u
        Binding("ctrl+enter", "do_steer", show=False, priority=True),
        # Reclaim ctrl+x from TextArea's default `cut`. macOS users
        # use ⌘+X for cut anyway, so this is a low-cost reassignment.
        Binding("ctrl+x", "app.cancel_pending", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    MultilineInput {
        height: 3;
        min-height: 3;
        max-height: 12;
        border: solid $border-blurred;
    }
    MultilineInput:focus {
        border: solid $border;
    }
    """

    # Outer height = inner rows + 2 (top + bottom border).
    _MIN_ROWS = 3
    _MAX_ROWS = 12

    # Paste protection. Modern terminals send a paste as a single
    # textual.events.Paste event (bracketed paste) and Enter never
    # fires — but older / minimal terminals replay the paste as a
    # stream of key events, ending in a literal `\n` that hits the
    # Enter binding and auto-submits the half-pasted buffer. To catch
    # that case: if two text-changed events arrive within
    # _PASTE_DETECT_GAP seconds, mark "paste in progress"; for the
    # next _PASTE_BLOCK_WINDOW seconds, demote Enter from submit to
    # insert-newline. Real human typing never trips it (ms gap is far
    # below typing cadence).
    _PASTE_DETECT_GAP = 0.02
    _PASTE_BLOCK_WINDOW = 0.2

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class SteerSubmitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class CompletionRequested(Message):
        """Tab pressed. The app owns the popup, so it decides whether
        there's a completion to accept or whether Tab should fall
        through to focus movement."""

    def action_do_submit(self) -> None:
        # Demote Enter to "insert newline" while a paste is in flight —
        # see the _PASTE_DETECT_GAP comment above.
        if (
            time.monotonic() - getattr(self, "_last_paste_at", 0.0)
            < self._PASTE_BLOCK_WINDOW
        ):
            self.insert("\n")
            return
        self.post_message(self.Submitted(self.text))

    def action_do_newline(self) -> None:
        # Mirrors what TextArea's built-in Enter handler does, but only
        # fires on Alt+Enter so plain Enter remains "submit".
        self.insert("\n")

    def action_do_steer(self) -> None:
        self.post_message(self.SteerSubmitted(self.text))

    def action_do_complete(self) -> None:
        self.post_message(self.CompletionRequested())

    def _fit_height(self) -> None:
        # Use wrapped_document.height (visible lines AFTER soft-wrap),
        # not document.line_count (logical \n-separated lines) — a long
        # pasted line that wraps onto 3 visible rows would otherwise
        # report 1 logical line and the box would stay at _MIN_ROWS.
        # Border eats 2 rows, so outer height = visible + 2; past
        # _MAX_ROWS the TextArea starts scrolling internally.
        try:
            visible = self.wrapped_document.height
        except Exception:
            visible = self.document.line_count
        limit = self._MAX_ROWS
        try:
            height = getattr(self.app, "_workbench_height", self.screen.size.height)
            limit = max(self._MIN_ROWS, min(limit, height // 3))
        except Exception:
            pass
        rows = max(self._MIN_ROWS, min(limit, visible + 2))
        if getattr(self, "_fit_height_rows", None) != rows:
            self.styles.height = rows
            self._fit_height_rows = rows

    def on_resize(self, _event) -> None:
        # When the terminal (or surrounding layout) resizes, the soft-
        # wrap column changes — same text may now occupy more/fewer
        # visible rows. Re-fit so the box matches its content height
        # without waiting for the next keystroke.
        self._fit_height()

    def on_mount(self) -> None:
        self._fit_height()
        self._last_change_at = 0.0
        self._last_paste_at = 0.0

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._fit_height()
        now = time.monotonic()
        last = getattr(self, "_last_change_at", 0.0)
        # Two changed events within _PASTE_DETECT_GAP almost certainly
        # come from a paste — human typing rate is ~80ms/char minimum.
        if 0 < now - last < self._PASTE_DETECT_GAP:
            self._last_paste_at = now
        self._last_change_at = now

    def on_paste(self, event) -> None:
        # Bracketed-paste path: terminal hands us the whole paste as
        # one event, no Enter keys ever fire — but stamp the timestamp
        # anyway so any trailing newline that might leak through is
        # also caught by action_do_submit. Textual will still dispatch
        # TextArea's own paste handler later in the MRO; don't call
        # super() here, or the paste text is inserted twice.
        self._last_paste_at = time.monotonic()


# ───────── status bar ─────────

def _short_cwd(cwd: str) -> str:
    """Compress $HOME to ~ for status-bar display."""
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):]
    return cwd


def _detect_git_branch(cwd: str) -> str | None:
    """Return branch name, ``detached@<short>``, or None if *cwd* isn't
    inside a git repo (or git is unavailable). Cheap (one subprocess);
    callers may cache."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    if name and name != "HEAD":
        return name
    # Detached HEAD: substitute the short commit hash so the user can
    # tell which detached state they're in at a glance.
    try:
        sh = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=2,
        )
        short = sh.stdout.strip()
    except Exception:
        short = ""
    return f"detached@{short}" if short else "detached"


class StatusBar(Static):
    """Keep context usage visible while secondary metrics yield to narrow widths."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        color: $text-muted;
        background: $surface;
        padding: 0 1;
    }
    """

    BAR_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, work_dir: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._work_dir = work_dir
        self._frame = 0
        self._last_status = None
        self._cwd_short = _short_cwd(work_dir)
        self._branch = _detect_git_branch(work_dir)

    def advance_frame(self) -> None:
        self._frame = (self._frame + 1) % len(self.BAR_FRAMES)

    def refresh_location(self) -> None:
        self._cwd_short = _short_cwd(self._work_dir)
        self._branch = _detect_git_branch(self._work_dir)

    def _location(self) -> str:
        if self._branch:
            return f"{self._cwd_short} · ⎇ {self._branch}"
        return self._cwd_short

    def on_resize(self) -> None:
        if self._last_status is not None:
            counter, options = self._last_status
            self.render_status(counter, **options)

    def render_status(
        self,
        counter: TokenCounter,
        *,
        context_limit: int | None = None,
        busy: bool = False,
        compacting: bool = False,
        queued: int = 0,
        steer: int = 0,
        explore: bool = False,
    ) -> None:
        self._last_status = (counter, dict(
            context_limit=context_limit, busy=busy, compacting=compacting,
            queued=queued, steer=steer, explore=explore,
        ))
        width = max(1, self.size.width - 2) if self.size.width else 100
        last_total = counter.last_prompt + counter.last_completion
        limit = context_limit if context_limit and context_limit > 0 else None
        ratio = last_total / limit if limit and counter.turns else 0
        color = UI.error if ratio >= .85 else UI.warning if ratio >= .7 else UI.accent
        context = Text("上下文 ", style=UI.muted)
        if not counter.turns:
            context.append("--")
        elif limit:
            filled = max(0, min(6, round(ratio * 6)))
            if width >= 55:
                context.append("▰" * filled, style=color)
                context.append("▱" * (6 - filled) + " ", style=UI.border)
            if width >= 110:
                context.append(f"{last_total:,}/{limit:,} ")
            context.append(f"{ratio * 100:.0f}%", style=color)
        else:
            context.append(f"{last_total:,}", style=color)
        if compacting:
            context = Text.assemble(("上下文压缩中 · ", UI.accent), context)

        speed = counter.decode_tokens_per_second
        rate = "--" if speed is None else f"{speed:.1f}"
        candidates = [Text(f"生成 {rate} tok/s", style=UI.muted)]
        if counter.last_ttft is not None:
            candidates.append(Text(f"首响 {counter.last_ttft:.1f}s", style=UI.muted))
        if queued:
            candidates.append(Text(f"排队 {queued}", style=UI.accent))
        if steer:
            candidates.append(Text(f"插话 {steer}", style=UI.accent))
        if explore:
            candidates.append(Text("探索中", style=UI.accent))

        def join(parts):
            result = Text()
            for index, part in enumerate(parts):
                if index:
                    result.append(" · ", style=UI.border)
                result.append_text(part)
            return result

        extras = []
        minimum_location = min(20, width // 5)
        for candidate in candidates:
            proposal = join(extras + [candidate, context])
            if proposal.cell_len <= width - minimum_location - 2:
                extras.append(candidate)
        right = join(extras + [context])
        right.truncate(width, overflow="ellipsis")
        location_budget = max(0, width - right.cell_len - 2)
        location = self._location()
        if cell_len(location) > location_budget:
            location = Path(self._work_dir).name or self._cwd_short
            if self._branch:
                location += f" · ⎇ {self._branch}"
        left = Text(location, style=UI.muted)
        left.truncate(location_budget, overflow="ellipsis")
        if busy and location_budget >= 4:
            left.truncate(location_budget - 2, overflow="ellipsis")
            left = Text.assemble((self.BAR_FRAMES[self._frame] + " ", UI.accent), left)
        padding = max(0, width - left.cell_len - right.cell_len)
        self.update(Text.assemble(left, " " * padding, right), layout=False)
