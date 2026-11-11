"""Long tasks keep their timeline readable and browsing position stable."""

import asyncio
import time
from unittest.mock import patch

import pytest
from textual import events
from textual.app import App
from textual.widgets import Static

from ddtui.timeline_view import TimelineView, format_duration
from ddtui.turn_timeline import TurnTimeline


class Harness(App):
    selected = None

    def compose(self):
        yield TimelineView()
        yield Static("tool output", id="record")

    def on_timeline_view_jump_requested(self, event):
        self.selected = event.target


def task_with_rounds(count, *, finished=True):
    timeline = TurnTimeline()
    timeline.started_at -= count * 3
    for number in range(1, count + 1):
        timeline.round_number = timeline.request_number = number
        start = timeline.started_at + (number - 1) * 3
        phases = (
            ("waiting", 0, .3), ("thinking", .3, .6), ("tool_args", .9, .1),
            ("tool", 1, .5), ("tool", 1, 1.5), ("answer", 2.5, .5),
        )
        for index, (kind, offset, duration) in enumerate(phases):
            anchor = f"tool:{number}:{index}" if kind == "tool" else kind
            span = timeline.begin(kind, at=start + offset, label="read_file", anchor=anchor)
            timeline.end(span, at=start + offset + duration)
    if finished:
        timeline.finish("done")
    return timeline


def test_hundreds_of_rounds_use_local_scale_and_support_direct_navigation():
    async def run():
        app = Harness()
        async with app.run_test(size=(110, 24)) as pilot:
            view = app.query_one(TimelineView)
            trace = task_with_rounds(500)
            view.display = True
            view.set_timeline(trace)
            await pilot.pause()
            assert view.table.row_count == 30
            assert view.table.get_row_at(0)[0].plain.startswith("496 ·")
            assert view.table.get_row_at(1)[2].plain.count("━") >= 2
            with patch.object(view, "_render_span", wraps=view._render_span) as render:
                view.refresh_timeline(force=True)
                assert render.call_count == 30

            await pilot.click("#timeline-previous")
            assert view.table.get_row_at(0)[0].plain.startswith("491 ·")
            jump = app.query_one("#timeline-round")
            jump.value = "247"
            jump.focus()
            await pilot.press("enter")
            assert view.table.get_row_at(0)[0].plain.startswith("247 ·")
            assert view.table.row_count == 30
            trace.request_number = 247
            target = app.query_one("#record")
            view.link("tool:247:3", target)
            view.table.move_cursor(row=3)
            await pilot.press("enter")
            await pilot.pause()
            assert app.selected is target
            await pilot.click("#timeline-latest")
            assert view.table.get_row_at(0)[0].plain.startswith("496 ·")

            await pilot.resize_terminal(32, 16)
            await pilot.pause()
            assert [key.value for key in view.table.columns] == ["step", "duration", "state"]
            assert view.table.max_scroll_x == 0
            assert jump.region.right <= view.region.right
            assert format_duration(1500) == "25m00s"
            assert format_duration(7380) == "2h03m"

    asyncio.run(run())


def test_browsing_history_stays_put_when_new_rounds_arrive():
    async def run():
        app = Harness()
        async with app.run_test(size=(110, 24)) as pilot:
            view = app.query_one(TimelineView)
            trace = task_with_rounds(500, finished=False)
            view.display = True
            view.set_timeline(trace)
            await pilot.pause()
            assert view.table.cursor_row == 29
            assert view.table.scroll_y + 1 >= view.table.max_scroll_y
            view.table.move_cursor(row=0)
            trace.round_number = trace.request_number = 501
            waiting = trace.begin("waiting", at=time.monotonic(), anchor="request")
            view.refresh_timeline()
            assert view.table.get_row_at(0)[0].plain.startswith("496 ·")
            assert view.table.row_count == 30 and view.table.cursor_row == 0
            assert not app.query_one("#timeline-latest").disabled

            await pilot.click("#timeline-latest")
            assert view.table.get_row_at(0)[0].plain.startswith("497 ·")
            assert view.table.row_count == 25 and view.table.cursor_row == 24
            trace.end(waiting, at=time.monotonic())
            trace.begin("thinking", at=time.monotonic(), anchor="thinking")
            view.refresh_timeline(force=True)
            assert view.table.row_count == 26 and view.table.cursor_row == 25
            view._set_time_range(trace.started_at, time.monotonic() - trace.started_at)
            with patch.object(view, "_render_span", wraps=view._render_span) as render:
                view.refresh_timeline()
                assert render.call_count == 1

    asyncio.run(run())


def test_time_column_drag_pans_across_rounds_and_releases_capture():
    async def run():
        app = Harness()
        async with app.run_test(size=(110, 24)) as pilot:
            view = app.query_one(TimelineView)
            trace = task_with_rounds(500)
            view.display = True
            view.set_timeline(trace)
            await pilot.pause()
            target = app.query_one("#record")
            for request in range(490, 501):
                trace.request_number = request
                for anchor in ("waiting", "thinking", "tool_args", "answer", f"tool:{request}:3", f"tool:{request}:4"):
                    view.link(anchor, target)
            before = view._last_window
            left, width = view.table._plot_geometry()
            x = int(left + width / 2)
            await pilot.mouse_down(view.table, offset=(x, 2))
            assert app.mouse_captured is view.table
            await pilot.hover(view.table, offset=(x + 8, 2))
            await pilot.mouse_up(view.table, offset=(x + 8, 2))
            await pilot.pause()
            assert app.mouse_captured is None
            assert app.selected is None
            assert view._time_range[0] < before[0]
            assert view._time_range[1] - view._time_range[0] == pytest.approx(before[1] - before[0])
            assert view.table.get_row_at(0)[0].plain.startswith("494 ·")
            view.table.move_cursor(row=0)
            await pilot.press("enter")
            await pilot.pause()
            assert app.selected is target
            await pilot.click("#timeline-latest")
            assert view._time_range is None
            assert view.table.get_row_at(0)[0].plain.startswith("496 ·")

    asyncio.run(run())


def test_zoom_preserves_pointer_time_and_keyboard_pan_stays_in_bounds():
    async def run():
        app = Harness()
        async with app.run_test(size=(110, 24)) as pilot:
            view = app.query_one(TimelineView)
            trace = task_with_rounds(500)
            view.display = True
            view.set_timeline(trace)
            await pilot.pause()
            start, end = view._last_window
            left, width = view.table._plot_geometry()
            x = int(left + width / 3)
            fraction = (x - left) / width
            anchor_time = start + (end - start) * fraction
            view.table.post_message(events.MouseScrollUp(
                view.table, x, 2, 0, 0, 0, False, False, True,
            ))
            await pilot.pause()
            zoom_start, zoom_end = view._time_range
            assert zoom_end - zoom_start == pytest.approx((end - start) * .8)
            assert zoom_start + (zoom_end - zoom_start) * fraction == pytest.approx(anchor_time)
            view.table.focus()
            await pilot.press("left")
            assert view._time_range[0] < zoom_start
            duration = view._time_range[1] - view._time_range[0]
            await pilot.press("+")
            assert view._time_range[1] - view._time_range[0] == pytest.approx(duration * .8)

            jump = app.query_one("#timeline-round")
            jump.value = "1"
            jump.focus()
            await pilot.press("enter")
            view.table.focus()
            await pilot.press("left")
            assert view._time_range[0] == trace.started_at
            for _ in range(4):
                await pilot.press("-")
            assert view._time_range[0] >= trace.started_at
            assert view._time_range[1] <= trace.ended_at

    asyncio.run(run())
