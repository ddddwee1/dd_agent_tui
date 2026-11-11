"""Thoughts, tools and diffs are independent entries in execution order."""

import asyncio
from types import SimpleNamespace

from textual.app import App
from textual.containers import VerticalScroll

from ddtui.app_history import AppHistoryMixin
from ddtui.app_ui import AppUiMixin
from ddtui.widgets import (
    AssistantMessage, DiffBlock, SteerBubble, SubagentTabPane,
    ThinkingBlock, ToolCallBlock, UserBubble,
)


class Harness(App):
    action_toggle_thinking = AppUiMixin.action_toggle_thinking
    _mount_widget = AppUiMixin._mount_widget
    _pair_orphan_tool_calls = AppUiMixin._pair_orphan_tool_calls
    _replay_messages_to_view = AppHistoryMixin._replay_messages_to_view
    _follow_bottom = False
    messages = []

    def compose(self):
        yield VerticalScroll(id="conversation")


def tool_message(ident):
    return {"id": ident, "type": "function", "function": {"name": "read_file", "arguments": '{"path":"x.py"}'}}


def test_consecutive_tools_and_diff_mount_directly_and_toggle_independently():
    async def run():
        app = Harness()
        async with app.run_test(size=(110, 30)) as pilot:
            first = ToolCallBlock("read_file", {"path": "a.py"})
            second = ToolCallBlock("edit_file", {"path": "b.py"})
            diff = DiffBlock("b.py", "@@ -1 +1 @@\n-old\n+new")
            for widget in (first, second, diff):
                await app._mount_widget(widget)
            await pilot.pause()
            assert list(app.query_one("#conversation").children) == [first, second, diff]
            assert first.collapsed and second.collapsed and not diff.collapsed
            first.query_one("CollapsibleTitle").focus()
            await pilot.press("enter")
            first.set_result("ok")
            second.set_result("Error: failed to edit")
            await pilot.pause()
            assert not first.collapsed and second.collapsed and not diff.collapsed
            assert "✓" in first.title and "异常" in second.title
            assert not first.is_pending and not second.is_pending

    asyncio.run(run())


def test_visible_messages_keep_consecutive_tools_as_siblings():
    async def run():
        app = Harness()
        async with app.run_test() as pilot:
            expected = []
            for divider in [UserBubble("user"), AssistantMessage(), SteerBubble("correction")]:
                widgets = [divider, ToolCallBlock("read_file", {}), ToolCallBlock("read_file", {})]
                expected.extend(widgets)
                for widget in widgets:
                    await app._mount_widget(widget)
            await pilot.pause()
            assert list(app.query_one("#conversation").children) == expected

    asyncio.run(run())


def test_replay_keeps_rounds_tools_and_errors_in_execution_order():
    async def run():
        app = Harness()
        app.messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "reasoning_content": "first thought", "tool_calls": [tool_message("a"), tool_message("b")]},
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
            {"role": "tool", "tool_call_id": "b", "content": "Error: failed"},
            {"role": "assistant", "reasoning_content": "next thought", "tool_calls": [tool_message("c")]},
            {"role": "tool", "tool_call_id": "c", "content": "ok"},
            {"role": "assistant", "content": "next step", "tool_calls": [tool_message("d")]},
            {"role": "tool", "tool_call_id": "d", "content": "ok"},
        ]
        expected = [
            "UserBubble", "ThinkingBlock", "ToolCallBlock", "ToolCallBlock",
            "ThinkingBlock", "ToolCallBlock", "AssistantMessage", "ToolCallBlock",
        ]
        async with app.run_test() as pilot:
            for _ in range(2):
                await app._replay_messages_to_view()
                await pilot.pause()
                view = app.query_one("#conversation")
                assert [type(widget).__name__ for widget in view.children] == expected
                assert [block.round_number for block in app.query(ThinkingBlock)] == [1, 2]
                assert all(block.collapsed for block in app.query(ThinkingBlock))
                assert all(block.collapsed for block in app.query(ToolCallBlock))
                assert "异常" in list(app.query(ToolCallBlock))[1].title
                await view.remove_children()

    asyncio.run(run())


def test_cancel_updates_pending_tools_without_touching_the_previous_turn():
    async def run():
        app = Harness()
        app.messages = [{"role": "user", "content": "new"},
                        {"role": "assistant", "tool_calls": [tool_message("a"), tool_message("b")]},
                        {"role": "tool", "tool_call_id": "a", "content": "ok"}]
        async with app.run_test() as pilot:
            old = ToolCallBlock("read_file", {})
            await app._mount_widget(old)
            await app._mount_widget(UserBubble("new"))
            first, second = ToolCallBlock("read_file", {}), ToolCallBlock("read_file", {})
            await app._mount_widget(first)
            await app._mount_widget(second)
            first.set_result("ok")
            assert app._pair_orphan_tool_calls() == 1
            await pilot.pause()
            assert old.is_pending and not first.is_pending and not second.is_pending
            assert "异常" in second.title

    asyncio.run(run())


def test_subagent_pre_mount_buffering_and_live_updates_have_no_outer_wrapper():
    async def run():
        sess = SimpleNamespace(messages=[{"role": "user", "content": "task"}])
        pane = SubagentTabPane(sess)
        pane.start_thinking()
        pane.append_thinking("first thought")
        pane.finalize_thinking(0)
        first = pane.add_tool_block("one", "read_file", {})
        second = pane.add_tool_block("two", "read_file", {})
        first.set_result("ok")
        second.set_result("Error: failed")
        app = Harness()
        async with app.run_test() as pilot:
            await app.query_one("#conversation").mount(pane)
            await pilot.pause()
            assert [type(widget).__name__ for widget in pane.children] == [
                "UserBubble", "ThinkingBlock", "ToolCallBlock", "ToolCallBlock",
            ]
            pane.start_thinking()
            pane.append_thinking("next thought")
            pane.finalize_thinking(0)
            pane.add_tool_block("three", "edit_file", {})
            pane.add_tool_diff("x.py", "@@ -1 +1 @@\n-a\n+b")
            pane.set_tool_result("three", "done")
            await pilot.pause()
            assert [type(widget).__name__ for widget in pane.children] == [
                "UserBubble", "ThinkingBlock", "ToolCallBlock", "ToolCallBlock",
                "ThinkingBlock", "ToolCallBlock", "DiffBlock",
            ]
            assert [block.round_number for block in pane.query(ThinkingBlock)] == [1, 2]
            sess.messages.extend([{"role": "user", "content": "next task"},
                                  {"role": "assistant", "reasoning_content": "new task thought",
                                   "tool_calls": [tool_message("four"), tool_message("five")]},
                                  {"role": "tool", "tool_call_id": "four", "content": "ok"}])
            pane.refresh_from_session()
            await pilot.pause()
            assert len(pane.query(ToolCallBlock)) == 5
            assert list(pane.query(ThinkingBlock))[-1].round_number == 1

    asyncio.run(run())


def test_thinking_toggle_keeps_summary_visible_and_does_not_toggle_tools():
    async def run():
        app = Harness()
        async with app.run_test() as pilot:
            first, second = ThinkingBlock(), ThinkingBlock()
            first.set_summary("先检查布局。")
            first.append_text("abc")
            first.finalize(5)
            second.set_summary("再检查输入区。")
            second.append_text("de")
            tool = ToolCallBlock("read_file", {})
            tool.collapsed = False
            for widget in (first, tool, second):
                await app._mount_widget(widget)
            await pilot.pause()
            assert all(block.query_one(".thinking-summary-preview").region.height > 0 for block in (first, second))
            app.action_toggle_thinking()
            assert not first.collapsed and not second.collapsed and not tool.collapsed
            app.action_toggle_thinking()
            assert first.collapsed and second.collapsed and not tool.collapsed
            await pilot.pause()
            assert all(block.query_one(".thinking-summary-preview").region.height > 0 for block in (first, second))

    asyncio.run(run())


def test_aborted_thinking_preserves_completed_tools_in_both_mount_states():
    from ddtui.app_agent_loop import ParentTurnObserver

    async def run():
        app = Harness()
        async with app.run_test() as pilot:
            observer = ParentTurnObserver(app)
            tool = ToolCallBlock("read_file", {})
            await app._mount_widget(tool)
            tool.set_result("ok")
            await observer.on_reasoning_delta("aborted")
            observer.on_stream_aborted()
            await pilot.pause()
            assert list(app.query_one("#conversation").children) == [tool]
            sess = SimpleNamespace(messages=[{"role": "user", "content": "task"}])
            pane = SubagentTabPane(sess)
            completed = pane.add_tool_block("completed", "read_file", {})
            completed.set_result("ok")
            pane.start_thinking()
            pane.append_thinking("aborted before mount")
            pane.remove_live_blocks()
            await app.query_one("#conversation").mount(pane)
            await pilot.pause()
            assert [type(widget).__name__ for widget in pane.children] == ["UserBubble", "ToolCallBlock"]
            assert completed.parent is pane

    asyncio.run(run())
