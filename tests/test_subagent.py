"""Subagent integration: spawn assembly, model/effort override, tool
rounds through the shared engine, compact_self, result auto-delivery."""

import asyncio

import pytest

from ddtui.app_subagents import AppSubagentMixin
from ddtui.providers import LLMProvider, LLMStreamEvent, ToolCallDelta
from ddtui.state import TokenCounter, ToolContext
from ddtui.tools_tasks import tool_task_kill, tool_task_start


def _tc(i, name, args_json):
    return ToolCallDelta(
        index=i, id=f"c{i}", type="function", name=name, arguments=args_json
    )


class FakeProvider(LLMProvider):
    key = "fake"
    label = "FakeProv"
    default_model = "m"
    default_effort = "e"

    def __init__(self):
        self.stream_calls = []

    def context_limit_for_model(self, model):
        return 100_000

    async def stream(self, messages, tools, model, effort):
        self.stream_calls.append((model, effort))
        yield LLMStreamEvent(content="done")


async def _allow_all(name, args):
    return True


class FakeApp(AppSubagentMixin):
    def __init__(self, workdir):
        self.provider = FakeProvider()
        self.model = "parent-model"
        self.effort = "high"
        self.ctx = ToolContext(work_dir=workdir)
        self.counter = TokenCounter()
        self._live_subagents = {}
        self._subagent_next_id = 1
        self.tool_confirm = _allow_all

    def call_later(self, *a, **k):
        pass

    def _add_sub_tab(self, sess):
        pass

    def _refresh_status(self):
        pass


async def _wait_round(sess, tries=60):
    for _ in range(tries):
        await asyncio.sleep(0.02)
        if sess.task is None:
            return
    raise TimeoutError("subagent round never finished")


@pytest.fixture
def playground(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# 项目约定\n测试命令是 make test。\n")
    return str(tmp_path)


def test_spawn_prompt_stack_and_override(playground):
    async def run():
        app = FakeApp(playground)
        r = app._spawn_subagent("查一下", "只读调查", model="cheap-model", effort="low")
        assert "session_id=sub-1" in r
        sess = app._live_subagents["sub-1"]
        assert sess.model == "cheap-model" and sess.effort == "low"
        await _wait_round(sess)
        assert app.provider.stream_calls == [("cheap-model", "low")]

        sys_msgs = [m["content"] for m in sess.messages if m["role"] == "system"]
        assert any("你是 ddtui 的子 agent" in c for c in sys_msgs)
        assert not any("# 任务管理与记忆" in c for c in sys_msgs)  # not parent's
        assert any("测试命令是 make test" in c for c in sys_msgs)   # AGENTS.md kept
        assert any("# 环境" in c for c in sys_msgs)
        assert any("FakeProv / cheap-model" in c for c in sys_msgs)
        assert any("# Subagent role / constraints" in c and "只读调查" in c
                   for c in sys_msgs)
    asyncio.run(run())


def test_spawn_defaults_inherit_parent(playground):
    async def run():
        app = FakeApp(playground)
        app._spawn_subagent("任务", "")
        sess = app._live_subagents["sub-1"]
        assert sess.model == "parent-model" and sess.effort == "high"
        await _wait_round(sess)
        assert app.provider.stream_calls == [("parent-model", "high")]
    asyncio.run(run())


def test_sub_toolset_and_ctx_flag(playground):
    async def run():
        app = FakeApp(playground)
        app._spawn_subagent("t", "")
        sess = app._live_subagents["sub-1"]
        names = {t["function"]["name"] for t in sess.sub_tools}
        assert {"task_start", "task_wait", "terminal_start"} <= names
        assert not {"checkpoint_tool", "explore_start", "spawn_agent"} & names
        assert sess.ctx.is_subagent is True
        await _wait_round(sess)
    asyncio.run(run())


def test_subagent_task_start_forced_background(playground):
    ctx = ToolContext(work_dir=playground, is_subagent=True)
    r = tool_task_start(ctx, "echo sub-test", name="t")
    assert "notify_on_complete: False" in r
    assert "You are a subagent" in r and "task_wait" in r
    for tid in list(ctx.tasks):
        tool_task_kill(ctx, tid)
    # parent ctx unaffected
    pctx = ToolContext(work_dir=playground)
    r = tool_task_start(pctx, "echo parent-test", name="p")
    assert "notify_on_complete: True" in r
    for tid in list(pctx.tasks):
        tool_task_kill(pctx, tid)


def test_tool_round_blocked_tools_and_real_execution(playground):
    async def run():
        class ToolRoundProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.rounds = [
                    [LLMStreamEvent(tool_call=_tc(0, "read_file", '{"path": "AGENTS.md", "limit": 1}')),
                     LLMStreamEvent(tool_call=_tc(1, "checkpoint_get", "{}"))],
                    [LLMStreamEvent(content="调查完成")],
                ]

            async def stream(self, messages, tools, model, effort):
                self.stream_calls.append((model, effort))
                for ev in self.rounds.pop(0):
                    yield ev

        app = FakeApp(playground)
        app.provider = ToolRoundProvider()
        app._spawn_subagent("读约定", "")
        sess = app._live_subagents["sub-1"]
        await _wait_round(sess)
        tool_msgs = [m for m in sess.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 2
        assert "AGENTS.md] lines 1-1" in tool_msgs[0]["content"]
        assert "not available to subagents" in tool_msgs[1]["content"]
        assert sess.last_result == "调查完成" and sess.phase == "ready"
    asyncio.run(run())


def test_compact_self_keeps_list_identity(playground):
    async def run():
        class CompactProvider(FakeProvider):
            def __init__(self):
                super().__init__()
                self.rounds = [
                    [LLMStreamEvent(tool_call=_tc(0, "compact_self", "{}"))],
                    [LLMStreamEvent(content="压缩后继续")],
                ]

            async def stream(self, messages, tools, model, effort):
                self.stream_calls.append(len(messages))
                for ev in self.rounds.pop(0):
                    yield ev

        app = FakeApp(playground)
        app.provider = CompactProvider()

        async def fake_compact(messages):
            kept = [m for m in messages if m["role"] == "system"]
            kept.append({"role": "system", "content": "# 历史摘要\n（压缩）"})
            return kept, {"before_n": len(messages), "after_n": len(kept), "saved": 12345}

        app._compact_messages = fake_compact
        app._spawn_subagent("长任务", "")
        sess = app._live_subagents["sub-1"]
        list_id = id(sess.messages)
        await _wait_round(sess)
        assert id(sess.messages) == list_id  # engine shares this object
        assert any("# 历史摘要" in (m.get("content") or "") for m in sess.messages)
        assert any("已压缩" in (m.get("content") or "")
                   for m in sess.messages if m["role"] == "tool")
        assert sess.last_result == "压缩后继续"
    asyncio.run(run())


def test_result_auto_delivery(playground):
    async def run():
        app = FakeApp(playground)
        app._spawn_subagent("查字段", "")
        sess = app._live_subagents["sub-1"]
        await _wait_round(sess)
        assert sess.phase == "ready" and sess.last_result

        # grace window: an in-flight await must win → no early delivery
        assert app._collect_ready_subagent_events() == []

        # idle past the grace window → delivered exactly once, consumed
        sess.last_active_at -= 3.0
        events = app._collect_ready_subagent_events()
        assert len(events) == 1
        assert events[0].startswith("[Subagent result] session_id=sub-1")
        assert "done" in events[0]
        assert "do NOT call await_agent" in events[0]
        assert sess.phase == "idle" and sess.last_result is None
        assert app._collect_ready_subagent_events() == []

        # awaiting an already-delivered result errors cleanly
        r = await app._await_subagent("sub-1", 0)
        assert "no pending result" in r
    asyncio.run(run())


def test_await_first_no_double_delivery(playground):
    async def run():
        app = FakeApp(playground)
        app._spawn_subagent("任务", "")
        sess = app._live_subagents["sub-1"]
        await _wait_round(sess)
        r = await app._await_subagent("sub-1", 5)
        assert "done" in r
        sess.last_active_at -= 3.0
        assert app._collect_ready_subagent_events() == []
    asyncio.run(run())
