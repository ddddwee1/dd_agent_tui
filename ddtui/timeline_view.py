"""The selectable timeline for the current main conversation."""

from __future__ import annotations

import math
import time
from bisect import bisect_right

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Static

from .turn_timeline import TimelineSpan, TurnTimeline
from .ui_theme import UI
from .widgets import ToolCallBlock


_PHASE_LABELS = {
    "waiting": "等待首段",
    "thinking": "思考输出",
    "tool_args": "生成工具参数",
    "answer": "生成回答",
    "retry": "等待重试",
}
_STATUS_LABELS = {
    "running": "运行中",
    "done": "完成",
    "failed": "失败",
    "cancelled": "中断",
    "blocked": "拦截",
}
_STATUS_COLORS = {
    "running": UI.accent,
    "failed": UI.error,
    "cancelled": UI.warning,
    "blocked": UI.warning,
}


def format_duration(seconds: float) -> str:
    if seconds < .001:
        return "<1ms"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds >= 3600:
        hours = int(seconds // 3600)
        minutes = int(seconds // 60) % 60
        return f"{hours}h{minutes:02d}m"
    if seconds >= 60:
        minutes = int(seconds // 60)
        remainder = int(seconds) % 60
        return f"{minutes}m{remainder:02d}s"
    return f"{seconds:.1f}s"


class TimelineTable(DataTable):
    """A selectable table whose time column supports dragging and zooming."""

    BINDINGS = [
        Binding("left", "pan_left", show=False),
        Binding("right", "pan_right", show=False),
        Binding("+", "zoom_in", show=False),
        Binding("=", "zoom_in", show=False),
        Binding("-", "zoom_out", show=False),
    ]

    class PanRequested(Message):
        def __init__(self, fraction: float) -> None:
            super().__init__()
            self.fraction = fraction

    class ZoomRequested(Message):
        def __init__(self, factor: float, anchor: float = .5) -> None:
            super().__init__()
            self.factor = factor
            self.anchor = anchor

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._drag_x: float | None = None
        self._dragged = False
        self._suppress_click = False
        self.tooltip = "拖动时间条平移 · Ctrl+滚轮缩放 · ← → / + −"

    def _plot_geometry(self) -> tuple[float, int] | None:
        left = -self.scroll_x
        for column in self.ordered_columns:
            if column.key.value == "bar":
                return left + self.cell_padding, column.width
            left += column.get_render_width(self)
        return None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self._suppress_click = False
        plot = self._plot_geometry()
        if event.button == 1 and plot is not None and plot[0] <= event.x < plot[0] + plot[1]:
            self._drag_x = event.screen_x
            self._dragged = False
            self.focus(scroll_visible=False)
            self.capture_mouse()
            event.prevent_default()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_x is None:
            return
        plot = self._plot_geometry()
        delta = event.screen_x - self._drag_x
        self._drag_x = event.screen_x
        if delta and plot is not None:
            self._dragged = True
            self.post_message(self.PanRequested(-delta / max(1, plot[1])))
        event.prevent_default()
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag_x is not None:
            self._suppress_click = self._dragged
            self._drag_x = None
            self.release_mouse()
            event.prevent_default()
            event.stop()

    def on_mouse_release(self) -> None:
        self._drag_x = None

    def cancel_drag(self) -> None:
        if self._drag_x is not None:
            self._drag_x = None
            self.release_mouse()
        self._suppress_click = False

    def on_click(self, event: events.Click) -> None:
        if self._suppress_click:
            self._suppress_click = False
            event.prevent_default()
            event.stop()

    def _wheel(self, event: events.MouseEvent, direction: int) -> None:
        if event.ctrl:
            plot = self._plot_geometry()
            anchor = .5 if plot is None else max(0, min(1, (event.x - plot[0]) / max(1, plot[1])))
            self.post_message(self.ZoomRequested(.8 if direction < 0 else 1.25, anchor))
        elif event.shift:
            self.post_message(self.PanRequested(direction * .15))
        else:
            return
        event.prevent_default()
        event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(event, -1)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(event, 1)

    def on_mouse_scroll_left(self, event: events.MouseEvent) -> None:
        self.post_message(self.PanRequested(-.15))
        event.prevent_default()
        event.stop()

    def on_mouse_scroll_right(self, event: events.MouseEvent) -> None:
        self.post_message(self.PanRequested(.15))
        event.prevent_default()
        event.stop()

    def action_pan_left(self) -> None:
        self.post_message(self.PanRequested(-.15))

    def action_pan_right(self) -> None:
        self.post_message(self.PanRequested(.15))

    def action_zoom_in(self) -> None:
        self.post_message(self.ZoomRequested(.8))

    def action_zoom_out(self) -> None:
        self.post_message(self.ZoomRequested(1.25))


class TimelineView(Vertical):
    ROUNDS_PER_WINDOW = 5

    DEFAULT_CSS = """
    TimelineView {
        display: none;
        height: 1fr;
        background: $background;
        padding: 0 1;
    }
    TimelineView > Static { height: 1; color: $text-muted; }
    TimelineView > Horizontal { height: 1; }
    TimelineView Button.-textual-compact {
        width: auto;
        min-width: 0;
        height: 1;
        margin: 0;
        padding: 0;
        border: none;
        background: $background;
        color: $text-muted;
        text-style: none;
    }
    TimelineView Button:hover, TimelineView Button:focus { color: $primary; text-style: underline; }
    TimelineView Input {
        width: 8;
        height: 1;
        border: none;
        padding: 0 1;
        background: $surface;
    }
    TimelineView > DataTable { height: 1fr; background: $background; }
    TimelineView DataTable > .datatable--header {
        background: $surface;
        color: $text-muted;
        text-style: none;
    }
    TimelineView DataTable > .datatable--cursor {
        background: $panel;
        color: $foreground;
        text-style: none;
    }
    """

    class JumpRequested(Message):
        def __init__(self, target: Widget) -> None:
            super().__init__()
            self.target = target

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.timeline: TurnTimeline | None = None
        self._anchors: dict[tuple[int, str], Widget] = {}
        self._rows: dict[str, tuple[Text, ...]] = {}
        self._row_versions: dict[str, tuple[float | None, str]] = {}
        self._paint_window: tuple[float, float, int] | None = None
        self._width = 0
        self._bar_width = 0
        self._columns: list[str] = []
        self._caption = Static(markup=False)
        self._caption_text = ""
        self._rendered_at: float | None = None
        self._indexed_count = 0
        self._round_spans: dict[int, list[TimelineSpan]] = {}
        self._round_numbers: list[int] = []
        self._round_starts: list[float] = []
        self._page_start: int | None = None
        self._time_range: tuple[float, float] | None = None
        self._last_window: tuple[float, float] | None = None
        self._shown_start = 1
        self._visible_spans: list[TimelineSpan] = []
        self._previous = Button("← 前段", id="timeline-previous", compact=True)
        self._next = Button("后段 →", id="timeline-next", compact=True)
        self._latest = Button("最新", id="timeline-latest", compact=True)
        self._round_input = Input(
            placeholder="轮次↵", restrict=r"[0-9]*",
            tooltip="输入轮次后按 Enter 跳转", id="timeline-round",
        )
        self._axis = Static(markup=False)
        self.table = TimelineTable(cursor_type="row", show_row_labels=False)

    def compose(self) -> ComposeResult:
        yield self._caption
        with Horizontal():
            yield self._previous
            yield self._next
            yield self._latest
            yield self._round_input
        yield self._axis
        yield self.table

    def on_mount(self) -> None:
        self.set_interval(.25, self.refresh_timeline)

    def on_resize(self) -> None:
        self.refresh_timeline()

    def on_hide(self) -> None:
        self.table.cancel_drag()

    def set_timeline(self, timeline: TurnTimeline | None) -> None:
        self.table.cancel_drag()
        self.timeline = timeline
        self._anchors.clear()
        self._rows.clear()
        self._row_versions.clear()
        self._paint_window = None
        self._width = 0
        self._rendered_at = None
        self._indexed_count = 0
        self._round_spans.clear()
        self._round_numbers.clear()
        self._round_starts.clear()
        self._page_start = None
        self._time_range = None
        self._last_window = None
        self._shown_start = 1
        self._visible_spans.clear()
        self._round_input.value = ""
        self.table.clear(columns=True)
        self.refresh_timeline()

    def link(self, anchor: str, widget: Widget) -> None:
        if self.timeline is None:
            return
        request = self.timeline.request_number
        self._anchors[request, anchor] = widget
        if anchor == "retry":
            self._anchors[request, "request"] = widget
        else:
            self._anchors.setdefault((request, "request"), widget)
        if anchor.startswith("tool:"):
            self._anchors.setdefault((request, "tool_args"), widget)

    def _layout_columns(self, width: int) -> None:
        self._width = width
        self._bar_width = 0
        if width >= 64:
            # Fixed columns, their padding, and the scrollbar occupy 34 cells.
            chart_space = width - 34
            step_width = min(36, chart_space * 2 // 3)
            self._bar_width = chart_space - step_width
            columns = [
                ("step", "步骤", step_width),
                ("start", "起点", 8),
                ("bar", "窗口内", self._bar_width),
            ]
        else:
            columns = [("step", "步骤", max(4, width - 22))]
        columns.extend([("duration", "耗时", 8), ("state", "状态", 6)])
        self.table.clear(columns=True)
        self._rows.clear()
        self._row_versions.clear()
        self._columns = [key for key, _, _ in columns]
        for key, title, column_width in columns:
            self.table.add_column(title, key=key, width=column_width)

    def _render_span(
        self, span: TimelineSpan, now: float, window_start: float, window_duration: float,
    ) -> tuple[Text, ...]:
        timeline = self.timeline
        assert timeline is not None
        start = span.started_at - timeline.started_at
        end = now if span.ended_at is None else span.ended_at
        name = _PHASE_LABELS.get(span.kind, span.label)
        target = self._anchors.get((span.request_number, span.anchor))
        if span.kind == "tool" and isinstance(target, ToolCallBlock):
            name = target.description
        cells = {
            "step": Text(f"{span.round_number} · {name}"),
            "duration": Text(format_duration(end - span.started_at), style=UI.muted),
            "state": Text(_STATUS_LABELS[span.status], style=_STATUS_COLORS.get(span.status, UI.muted)),
        }
        if self._bar_width:
            scale = self._bar_width / max(window_duration, 1e-6)
            left = min(self._bar_width - 1, max(0, int((span.started_at - window_start) * scale)))
            right = min(self._bar_width, math.ceil((end - window_start) * scale))
            color = UI.accent if span.kind == "tool" else UI.muted
            offset = f"{start:.1f}s" if start < 60 else format_duration(start)
            cells["start"] = Text(offset, style=UI.muted)
            cells["bar"] = Text(" " * left + "━" * max(1, right - left), style=color)
        return tuple(cells[key] for key in self._columns)

    def _total_rounds(self) -> int:
        if self.timeline is None:
            return 1
        return max(1, self.timeline.round_number)

    def _show_page(self, first: int | None) -> None:
        latest_start = max(1, self._total_rounds() - self.ROUNDS_PER_WINDOW + 1)
        self._page_start = None if first is None else max(1, min(first, latest_start))
        self._time_range = None
        self._visible_spans.clear()
        self.refresh_timeline(force=True)
        self.table.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button is self._previous:
            self._show_page(self._shown_start - self.ROUNDS_PER_WINDOW)
        elif event.button is self._next:
            first = self._shown_start + self.ROUNDS_PER_WINDOW
            latest_start = max(1, self._total_rounds() - self.ROUNDS_PER_WINDOW + 1)
            self._show_page(None if first >= latest_start else first)
        elif event.button is self._latest:
            self._show_page(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value.strip()
        if not value:
            self.table.focus()
            return
        if not value.isdecimal() or not 1 <= int(value) <= self._total_rounds():
            self.app.notify(f"请输入 1–{self._total_rounds()} 之间的轮次", timeout=2)
            return
        round_number = int(value)
        self._show_page(round_number)
        self._round_input.value = ""
        row = next(
            (index for index, span in enumerate(self._visible_spans) if span.round_number == round_number), 0,
        )
        self.table.move_cursor(row=row)

    def _set_time_range(self, start: float, duration: float) -> None:
        timeline = self.timeline
        if timeline is None:
            return
        end = time.monotonic() if timeline.ended_at is None else timeline.ended_at
        duration = max(1e-6, min(duration, end - timeline.started_at))
        start = max(timeline.started_at, min(start, end - duration))
        self._time_range = start, start + duration
        self._page_start = self._shown_start
        self.refresh_timeline(force=True)

    def on_timeline_table_pan_requested(self, event: TimelineTable.PanRequested) -> None:
        event.stop()
        if self._last_window is not None:
            start, end = self._last_window
            self._set_time_range(start + (end - start) * event.fraction, end - start)

    def on_timeline_table_zoom_requested(self, event: TimelineTable.ZoomRequested) -> None:
        event.stop()
        if self._last_window is not None:
            start, end = self._last_window
            duration = (end - start) * event.factor
            anchor_time = start + (end - start) * event.anchor
            self._set_time_range(anchor_time - duration * event.anchor, duration)

    def refresh_timeline(self, *, force: bool = False) -> None:
        timeline = self.timeline
        if not self.display or not self.is_mounted or timeline is None:
            return
        now = time.monotonic() if timeline.ended_at is None else timeline.ended_at
        elapsed = timeline.elapsed(now)
        width = self.content_size.width or self.app.size.width
        resized = self._width != width
        paused = False
        if self._page_start is None and self._visible_spans:
            if (
                self.table.cursor_row != len(self._visible_spans) - 1
                or self.table.scroll_y + 1 < self.table.max_scroll_y
            ):
                self._page_start = self._shown_start
                paused = True
        if not force and not paused and not resized and now == self._rendered_at:
            return
        self._rendered_at = now
        for span in timeline.spans[self._indexed_count:]:
            if span.round_number not in self._round_spans:
                self._round_numbers.append(span.round_number)
                self._round_starts.append(span.started_at)
            self._round_spans.setdefault(span.round_number, []).append(span)
        self._indexed_count = len(timeline.spans)
        total_rounds = self._total_rounds()
        first = self._page_start or max(1, total_rounds - self.ROUNDS_PER_WINDOW + 1)
        last = min(total_rounds, first + self.ROUNDS_PER_WINDOW - 1)
        if self._time_range is None:
            visible = [
                span for number in range(first, last + 1)
                for span in self._round_spans.get(number, ())
            ]
            window_start = min((span.started_at for span in visible), default=now)
            window_end = max(
                (now if span.ended_at is None else span.ended_at for span in visible), default=now,
            )
        else:
            window_start, window_end = self._time_range
            left = max(0, bisect_right(self._round_starts, window_start) - 1)
            right = bisect_right(self._round_starts, window_end)
            numbers = self._round_numbers[left:right]
            visible = [
                span for number in numbers for span in self._round_spans[number]
                if span.started_at < window_end
                and (now if span.ended_at is None else span.ended_at) > window_start
            ]
            first = numbers[0] if numbers else first
            last = numbers[-1] if numbers else first
        self._last_window = window_start, window_end
        cursor = self.table.cursor_row
        selected = self._visible_spans[cursor] if cursor < len(self._visible_spans) else None
        keys = [str(id(span)) for span in visible]
        replaced = list(self._rows) != keys[:len(self._rows)]
        if resized:
            self._layout_columns(width)
        elif replaced:
            self.table.clear()
            self._rows.clear()
            self._row_versions.clear()
        self._shown_start = first
        caption = f"第 {first}–{last} / {total_rounds} 轮 · {format_duration(elapsed)} · {_STATUS_LABELS[timeline.status]}"
        if width >= 80:
            caption += "    ↑↓ 选择 · Enter 定位"
        if caption != self._caption_text:
            self._caption_text = caption
            self._caption.update(Text(caption, no_wrap=True, overflow="ellipsis"))
        origin = timeline.started_at
        start_text = "0.0s" if window_start == origin else format_duration(window_start - origin)
        axis = f"{start_text} → {format_duration(window_end - origin)}"
        if width >= 64:
            axis += "    拖动平移 · Ctrl+滚轮缩放"
        self._axis.update(Text(axis, no_wrap=True, overflow="ellipsis"))
        self._previous.disabled = first == 1
        self._next.disabled = last == total_rounds
        self._latest.disabled = self._page_start is None
        previous_count = len(self._rows)
        paint_window = window_start, window_end, self._bar_width
        axis_changed = paint_window != self._paint_window
        for span in visible:
            key = str(id(span))
            previous = self._rows.get(key)
            version = span.ended_at, span.status
            if (
                not force and not axis_changed and previous is not None
                and span.ended_at is not None and self._row_versions.get(key) == version
            ):
                continue
            row = self._render_span(span, now, window_start, window_end - window_start)
            if previous is None:
                self.table.add_row(*row, key=key)
            elif previous != row:
                for column, old, new in zip(self._columns, previous, row):
                    if old != new:
                        self.table.update_cell(key, column, new)
            self._rows[key] = row
            self._row_versions[key] = version
        self._paint_window = paint_window
        self._visible_spans = visible
        if visible:
            if self._page_start is None:
                if resized or replaced or len(visible) != previous_count:
                    self.table.move_cursor(row=len(visible) - 1)
            elif resized or replaced:
                row = visible.index(selected) if selected in visible else 0
                self.table.move_cursor(row=row, scroll=replaced and self._time_range is not None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        if self.timeline is None:
            return
        span = next((item for item in self._visible_spans if str(id(item)) == event.row_key.value), None)
        if span is None:
            return
        target = self._anchors.get((span.request_number, span.anchor))
        if span.kind != "tool" and (target is None or not target.is_attached):
            target = self._anchors.get((span.request_number, "request"))
        if target is not None and target.is_attached:
            self.post_message(self.JumpRequested(target))
        else:
            self.app.notify("此步骤暂无可定位的对话记录", timeout=2)
