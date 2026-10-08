"""Grouped tool UI: live updates, replay, cancellation, keyboard and subpanes."""

import asyncio
from types import SimpleNamespace

from textual.app import App
from textual.containers import VerticalScroll

from ddtui.app_history import AppHistoryMixin
from ddtui.app_ui import AppUiMixin
from ddtui.widgets import (AssistantMessage, DiffBlock, SteerBubble, SubagentTabPane,
                           ThinkingBlock, ToolCallBlock, ToolCallGroup, UserBubble)


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


def test_group_expand_status_updates_and_diff_stay_together():
    async def scenario():
        app = Harness()
        async with app.run_test(size=(110, 30)) as pilot:
            first = ToolCallBlock("read_file", {"path": "a.py"})
            second = ToolCallBlock("edit_file", {"path": "b.py"})
            await app._mount_widget(first)
            await app._mount_widget(second)
            diff = DiffBlock("b.py", "@@ -1 +1 @@\n-old\n+new")
            await app._mount_widget(diff)
            await pilot.pause()
            group = app.query_one(ToolCallGroup)
            assert group.collapsed and "2 项" in group.title and "执行中 2" in group.title
            assert group.query(DiffBlock).first() is diff
            title = group.query_one("ToolCallGroup > CollapsibleTitle")
            title.focus()
            await pilot.press("enter")
            assert not group.collapsed
            first.set_result("ok")
            second.set_result("Error: failed to edit")
            await pilot.pause()
            assert "✓ 1" in group.title and "异常 1" in group.title
            assert "执行中" not in group.title
            assert not group.collapsed
            # Nested detail headers toggle only their own detail, not the group.
            first.query_one("ToolCallBlock > CollapsibleTitle").focus()
            await pilot.press("enter")
            assert not first.collapsed and not group.collapsed
            title.focus()
            await pilot.press("enter")
            third = ToolCallBlock("read_file", {"path": "c.py"})
            await app._mount_widget(third)
            assert group.collapsed and "3 项" in group.title
            assert third._tool_group is group
    asyncio.run(scenario())


def test_visible_messages_split_groups_across_tool_rounds():
    async def scenario():
        app = Harness()
        async with app.run_test() as pilot:
            for divider in [UserBubble("user"), AssistantMessage(), SteerBubble("correction")]:
                await app._mount_widget(divider)
                await app._mount_widget(ToolCallBlock("read_file", {}))
                await app._mount_widget(ToolCallBlock("read_file", {}))
            await pilot.pause()
            assert [len(g.tool_calls) for g in app.query(ToolCallGroup)] == [2, 2, 2]
    asyncio.run(scenario())


def test_replay_matches_live_group_boundaries_and_errors():
    async def scenario():
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
        async with app.run_test() as pilot:
            await app._replay_messages_to_view()
            await pilot.pause()
            groups = list(app.query(ToolCallGroup))
            assert [len(g.tool_calls) for g in groups] == [3, 1]
            assert "异常 1" in groups[0].title
            assert len(groups[0].thinking_blocks) == 2
            assert "思考中" not in groups[0].title
            assert all(g.collapsed for g in groups)
            await app.query_one("#conversation").remove_children()
            await app._replay_messages_to_view()
            await pilot.pause()
            assert [len(g.tool_calls) for g in app.query(ToolCallGroup)] == [3, 1]
    asyncio.run(scenario())


def test_cancel_updates_pending_nested_tools_without_touching_old_group():
    async def scenario():
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
            assert old.is_pending and not second.is_pending
            assert "异常 1" in second._tool_group.title
            assert "✓ 1" in second._tool_group.title
    asyncio.run(scenario())


def test_subagent_pre_mount_buffering_and_replay_share_grouping():
    async def scenario():
        sess = SimpleNamespace(messages=[{"role": "user", "content": "task"}])
        pane = SubagentTabPane(sess)
        pane.start_thinking()
        pane.append_thinking("first thought")
        pane.finalize_thinking(0)
        one = pane.add_tool_block("one", "read_file", {})
        two = pane.add_tool_block("two", "read_file", {})
        one.set_result("ok")
        two.set_result("Error: failed")
        app = Harness()
        async with app.run_test() as pilot:
            await app.query_one("#conversation").mount(pane)
            await pilot.pause()
            groups = list(pane.query(ToolCallGroup))
            assert len(groups) == 1 and len(groups[0].tool_calls) == 2
            assert "异常 1" in groups[0].title
            assert len(groups[0].thinking_blocks) == 1
            assert isinstance(pane.children[0], UserBubble)
            pane.start_thinking()
            pane.append_thinking("next thought")
            pane.finalize_thinking(0)
            pane.add_tool_block("three", "edit_file", {})
            pane.add_tool_diff("x.py", "@@ -1 +1 @@\n-a\n+b")
            pane.set_tool_result("three", "done")
            await pilot.pause()
            assert len(groups[0].tool_calls) == 3
            assert len(groups[0].thinking_blocks) == 2
            assert len(groups[0].query(DiffBlock)) == 1
            sess.messages.extend([{"role": "user", "content": "next task"},
                                  {"role": "assistant", "tool_calls": [tool_message("four"), tool_message("five")]},
                                  {"role": "tool", "tool_call_id": "four", "content": "ok"}])
            pane.refresh_from_session()
            await pilot.pause()
            assert [len(g.tool_calls) for g in pane.query(ToolCallGroup)] == [3, 2]
            assert len(pane.query(ToolCallBlock)) == 5
    asyncio.run(scenario())


def test_thinking_and_tools_share_live_group_and_keyboard_toggle():
    async def scenario():
        app = Harness()
        async with app.run_test() as pilot:
            first = ThinkingBlock()
            await app._mount_widget(first)
            first.append_text("abc")
            group = app.query_one(ToolCallGroup)
            assert group.collapsed and "3 字符" in group.title
            assert "思考中" in group.title
            first.finalize(5)
            assert "思考中" not in group.title
            tool = ToolCallBlock("read_file", {})
            await app._mount_widget(tool)
            tool.set_result("ok")
            second = ThinkingBlock()
            await app._mount_widget(second)
            second.append_text("de")
            await pilot.pause()
            assert len(app.query(ToolCallGroup)) == 1
            assert list(group._items.children) == [first, tool, second]
            assert "thinking 2 段" in group.title and "5 字符" in group.title
            assert "✓ 1" in group.title and "思考中" in group.title
            app.action_toggle_thinking()
            assert not group.collapsed and not first.collapsed and not second.collapsed
            second.finalize(2)
            assert not group.collapsed
            app.action_toggle_thinking()
            assert group.collapsed and first.collapsed and second.collapsed
            # A manually folded parent must still open on the next Ctrl+T.
            first.collapsed = False
            app.action_toggle_thinking()
            assert not group.collapsed and not first.collapsed
    asyncio.run(scenario())


def test_aborted_thinking_cleans_group_and_preserves_completed_tools():
    from ddtui.app_agent_loop import ParentTurnObserver

    async def scenario():
        app = Harness()
        async with app.run_test() as pilot:
            observer = ParentTurnObserver(app)
            await observer.on_reasoning_delta("aborted")
            observer.on_stream_aborted()
            await pilot.pause()
            assert not list(app.query(ToolCallGroup))
            tool = ToolCallBlock("read_file", {})
            await app._mount_widget(tool)
            tool.set_result("ok")
            await observer.on_reasoning_delta("retry")
            observer.on_stream_aborted()
            await pilot.pause()
            group = app.query_one(ToolCallGroup)
            assert group.tool_calls == (tool,)
            assert not group.thinking_blocks
            assert "thinking" not in group.title and "✓ 1" in group.title
    asyncio.run(scenario())
