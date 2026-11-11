"""Workbench controls preserve the conversation across resize and observation."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from textual.app import App
from textual.widgets import Static

from ddtui import app as app_mod
from ddtui import app_history, app_ui, runtime_state
from ddtui.providers import LLMProvider, LLMStreamEvent, ProviderUsage
from ddtui.state import TokenCounter
from ddtui.widgets import MultilineInput, StatusBar, ThinkingBlock, ThinkingSummaryPanel, UserBubble, WorkbenchOverview


class PreviewProvider(LLMProvider):
    key = "preview"
    label = "Preview"
    default_model = "preview-model"
    default_effort = "low"
    default_context_limit = 100_000


@pytest.fixture
def make_app(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "build_provider", lambda name: PreviewProvider())
    monkeypatch.setattr(app_history, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(runtime_state, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(app_ui, "CONFIG_FILE_KEY_COUNT", 0)
    monkeypatch.setattr(app_ui, "CONFIG_FILE_ERROR", "")

    def create():
        app = app_mod.AgentApp(provider_name="preview")
        monkeypatch.setattr(app, "_maybe_start_remote_from_config", lambda: None)
        return app

    return create


def test_wide_workbench_keeps_composer_full_width_and_respects_manual_collapse(make_app):
    async def run():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            sidebar = app.query_one("#sidebar")
            composer = app.query_one("#composer")
            user_input = app.query_one("#user-input", MultilineInput)
            assert sidebar.display
            assert sidebar.region.bottom <= composer.region.y
            assert user_input.region.width == 118
            assert app.query_one("#status").region.width == 120
            assert not app.query_one("ContentTabs").display
            await pilot.click("#toggle-sidebar")
            app._refresh_task_panel()
            await pilot.pause()
            assert not sidebar.display
            assert app.query_one("#conversation").region.width == 120
            await app._update_todo_sidebar({"items": [{"content": "检查布局", "status": "in_progress"}]})
            assert not sidebar.display
            user_input.load_text("保留输入")
            user_input.focus()
            await pilot.press("ctrl+b")
            await pilot.pause()
            assert sidebar.display and user_input.text == "保留输入"

    asyncio.run(run())


def test_narrow_workbench_switches_views_without_covering_the_input(make_app):
    async def run():
        app = make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            sidebar = app.query_one("#sidebar")
            main = app.query_one("#main")
            assert not sidebar.display and main.display
            await pilot.click("#toggle-sidebar")
            await pilot.pause()
            assert sidebar.display and not main.display
            assert app.query_one("#user-input", MultilineInput).has_focus
            assert sidebar.region.width == 80
            assert sidebar.region.bottom <= app.query_one("#composer").region.y
            assert app.query_one("#user-input").region.width == 78
            await pilot.resize_terminal(120, 36)
            await pilot.pause()
            assert sidebar.display and main.display
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert not sidebar.display and main.display

    asyncio.run(run())


def test_observation_tab_labels_input_as_main_conversation(make_app):
    async def run():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            session = SimpleNamespace(id="sub-preview", pane=None, messages=[])
            await app._add_sub_tab(session)
            tabs = app.query_one("#tabs")
            tabs.active = "tab-sub-preview"
            await pilot.pause()
            assert app.query_one("ContentTabs").display
            assert "输入发送至主会话" in str(app.query_one("#workbench-phase", Static).render())
            await app._remove_sub_tab(session.id)
            await pilot.pause()
            assert tabs.active == "tab-main"
            assert not app.query_one("ContentTabs").display

    asyncio.run(run())


def test_goal_summary_and_hints_follow_actual_task_state(make_app):
    async def run():
        app = make_app()
        async with app.run_test(size=(120, 36)) as pilot:
            app.messages.append({"role": "user", "content": "核对侧栏的宽度约束"})
            app._set_busy(True)
            app._set_workbench_phase("thinking")
            app._set_thinking_summary("正在核对侧栏的宽度约束。")
            await pilot.pause()
            assert "核对侧栏" in str(app.query_one(WorkbenchOverview).render())
            assert "当前思考" in str(app.query_one(ThinkingSummaryPanel).render())
            assert "Enter 排队" in str(app.query_one("#hint", Static).render())
            assert "Esc×2 中断" in str(app.query_one("#hint", Static).render())
            app._set_workbench_phase("tool", "read_file")
            assert "read_file" in str(app.query_one("#workbench-phase", Static).render())
            app._set_busy(False)
            await pilot.pause()
            assert "思考摘要" in str(app.query_one(ThinkingSummaryPanel).render())
            assert "Enter 发送" in str(app.query_one("#hint", Static).render())
            assert app.query_one(ThinkingSummaryPanel).display

    asyncio.run(run())


def test_context_usage_stays_visible_when_footer_shrinks(tmp_path):
    class Harness(App):
        def compose(self):
            yield StatusBar(str(tmp_path))

    async def run():
        app = Harness()
        async with app.run_test(size=(100, 5)) as pilot:
            counter = TokenCounter()
            counter.add(ProviderUsage(50_000, 0), elapsed=2, ttft=1)
            bar = app.query_one(StatusBar)
            bar.render_status(counter, context_limit=100_000)
            await pilot.resize_terminal(32, 5)
            await pilot.pause()
            line = bar.render_line(0).text
            assert "50%" in line and "上下文" in line
            assert cell_len(line) <= 32

    asyncio.run(run())


def test_restored_todo_does_not_open_the_narrow_sidebar(make_app):
    async def run():
        app = make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            args = {"items": [{"content": "检查布局", "status": "in_progress"}]}
            app.messages.extend([
                {"role": "user", "content": "继续布局任务"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "todo-1", "function": {"name": "todo_tool", "arguments": json.dumps(args)}},
                ]},
                {"role": "tool", "tool_call_id": "todo-1", "content": "ok"},
            ])
            await app._replay_messages_to_view()
            await pilot.pause()
            assert app._todo_block is not None
            assert not app.query_one("#sidebar").display
            assert app.query_one("#conversation").region.width == 80

    asyncio.run(run())


def test_short_window_keeps_long_input_and_pending_messages_above_footer(make_app):
    async def run():
        app = make_app()
        async with app.run_test(size=(80, 18)) as pilot:
            user_input = app.query_one("#user-input", MultilineInput)
            text = "\n".join(f"第 {number} 行补充说明" for number in range(20))
            user_input.load_text(text)
            for number in range(4):
                await app._mount_pending(UserBubble(f"排队消息 {number}\n第二行内容"))
            await pilot.pause()
            assert user_input.text == text
            assert app.query_one("#status").region.bottom <= 18
            assert user_input.region.bottom < 18
            assert app.query_one("#body").region.height >= 2

    asyncio.run(run())


def test_timeline_accumulates_followups_and_resets_with_the_conversation(make_app):
    async def run():
        ready, release = asyncio.Event(), asyncio.Event()

        class LiveProvider(PreviewProvider):
            calls = 0

            async def stream(self, messages, tools, model, effort):
                self.calls += 1
                if self.calls == 1:
                    yield LLMStreamEvent(reasoning="检查当前布局。")
                    ready.set()
                    await release.wait()
                yield LLMStreamEvent(content="检查完成。")

        app = make_app()
        app.provider = LiveProvider()
        async with app.run_test(size=(120, 30)) as pilot:
            app.run_worker(app._agent_turn("检查布局"), exclusive=True)
            await asyncio.wait_for(ready.wait(), 3)
            view = app._timeline_view
            first = view.timeline
            await pilot.pause()
            await pilot.click("#timeline-toggle")
            await pilot.pause()
            assert view.display and view.table.has_focus
            assert first.spans[-1].kind == "thinking"
            assert first.spans[-1].ended_at is None

            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert view.display and first.status == "done"
            assert first.spans[-1].kind == "answer"
            assert view.table.row_count == len(first.spans)
            previous_spans = list(first.spans)
            previous_times = [(span.started_at, span.ended_at, span.status) for span in previous_spans]
            origin = first.started_at
            first_thinking = app.query_one(ThinkingBlock)
            await pilot.press("+")
            time_range = view._time_range
            assert time_range is not None

            app.run_worker(app._agent_turn("继续检查"), exclusive=True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert view.timeline is first and first.status == "done"
            assert first.started_at == origin
            assert first.spans[:len(previous_spans)] == previous_spans
            assert [(span.started_at, span.ended_at, span.status) for span in previous_spans] == previous_times
            assert first.round_number == first.request_number == 2
            assert len(first.spans) == 5
            assert view._time_range == time_range
            await pilot.click("#timeline-latest")
            assert view.table.row_count == 5
            view.table.move_cursor(row=1)
            await pilot.press("enter")
            await pilot.pause()
            assert first_thinking.query_one("CollapsibleTitle").has_focus
            assert not view.display
            app.action_clear_chat()
            await pilot.pause()
            assert view.timeline is None and not view.display
            assert app.query_one("#conversation").display
            assert not app.query_one("#timeline-toggle").display

    asyncio.run(run())


def test_timeline_survives_interrupt_and_followup_through_input(make_app):
    async def run():
        ready = asyncio.Event()

        class InterruptedProvider(PreviewProvider):
            calls = 0

            async def stream(self, messages, tools, model, effort):
                self.calls += 1
                if self.calls == 1:
                    yield LLMStreamEvent(reasoning="检查会话状态。")
                    ready.set()
                    await asyncio.Event().wait()
                yield LLMStreamEvent(content="继续完成检查。")

        app = make_app()
        app.provider = InterruptedProvider()
        async with app.run_test(size=(120, 30)) as pilot:
            user_input = app.query_one("#user-input", MultilineInput)
            user_input.load_text("开始检查")
            await pilot.press("enter")
            await asyncio.wait_for(ready.wait(), 3)
            first = app._timeline_view.timeline
            await pilot.press("escape", "escape")
            await pilot.pause()
            assert not app._busy and first.status == "cancelled"
            interrupted = list(first.spans)
            previous_times = [(span.started_at, span.ended_at, span.status) for span in interrupted]
            assert any(span.status == "cancelled" for span in interrupted)

            user_input.load_text("继续检查")
            user_input.focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._timeline_view.timeline is first
            assert first.status == "done"
            assert first.round_number == first.request_number == 2
            assert len(first.spans) == len(interrupted) + 2
            assert [(span.started_at, span.ended_at, span.status) for span in interrupted] == previous_times
            assert all(span.ended_at is not None for span in first.spans)

    asyncio.run(run())
