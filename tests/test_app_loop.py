"""Parent loop end-to-end: a real AgentApp (headless Textual) drives one
turn with a real tool call through the shared TurnEngine."""

import asyncio
from types import SimpleNamespace

import ddtui.app as app_mod
import ddtui.engine as engine_mod
from ddtui.providers import LLMProvider, LLMStreamEvent, ProviderUsage, ToolCallDelta
from ddtui.widgets import AssistantMessage, StatusBar, ThinkingBlock, ToolCallBlock, ToolCallGroup
from tests.conftest import REPO_ROOT


class FakeProvider(LLMProvider):
    key = "fake"
    label = "Fake"
    default_model = "fake-model"
    default_effort = "low"
    default_context_limit = 100_000

    def __init__(self):
        readme = REPO_ROOT / "README.md"
        self.rounds = [
            [
                LLMStreamEvent(reasoning="先读一下 README"),
                LLMStreamEvent(tool_call=ToolCallDelta(
                    index=0, id="call_0", type="function",
                    name="read_file",
                    arguments=f'{{"path": "{readme}", "limit": 3}}',
                )),
                LLMStreamEvent(tool_call=ToolCallDelta(
                    index=1, id="call_1", type="function",
                    name="read_file",
                    arguments=f'{{"path": "{readme}", "limit": 1}}',
                )),
            ],
            [
                LLMStreamEvent(reasoning="再看一眼开头"),
                LLMStreamEvent(tool_call=ToolCallDelta(
                    index=0, id="call_2", type="function",
                    name="read_file",
                    arguments=f'{{"path": "{readme}", "limit": 1}}',
                )),
            ],
            [LLMStreamEvent(content="已读取，"), LLMStreamEvent(content="完成。")],
        ]

    async def stream(self, messages, tools, model, effort):
        for ev in self.rounds.pop(0):
            yield ev


def test_full_turn_through_engine(monkeypatch):
    async def run():
        fake = FakeProvider()
        monkeypatch.setattr(app_mod, "build_provider", lambda name: fake)

        app = app_mod.AgentApp(provider_name="fake")
        async with app.run_test() as pilot:
            app.run_worker(app._agent_turn("检查 readme"), exclusive=True)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

            roles = [m["role"] for m in app.messages if m["role"] != "system"]
            assert roles == [
                "user",
                "assistant", "tool", "tool",
                "assistant", "tool",
                "assistant",
            ]

            asst = next(m for m in app.messages if m.get("tool_calls"))
            assert asst["tool_calls"][0]["function"]["name"] == "read_file"
            assert asst["tool_calls"][1]["function"]["name"] == "read_file"
            assert asst["reasoning_content"] == "先读一下 README"

            tool_msg = next(m for m in app.messages if m["role"] == "tool")
            assert "README.md] lines 1-3" in tool_msg["content"]
            assert "\t" in tool_msg["content"]  # numbered output

            assert app.messages[-1]["content"] == "已读取，完成。"
            thinking_blocks = list(app.query(ThinkingBlock))
            tool_blocks = list(app.query(ToolCallBlock))
            assert len(thinking_blocks) == 2
            assert len(tool_blocks) == 3
            assert all(block.collapsed for block in thinking_blocks)
            assert all(block.collapsed for block in tool_blocks)
            conversation = app.query_one("#conversation")
            groups = list(app.query(ToolCallGroup))
            assert [len(group.tool_calls) for group in groups] == [3]
            assert groups[0].thinking_blocks == tuple(thinking_blocks)
            assert all(group.parent is conversation for group in groups)
            assert all(group.collapsed for group in groups)
            assert all(block._tool_group in groups for block in tool_blocks)
            assert len(app.query(AssistantMessage)) == 1
            assert app._busy is False

    asyncio.run(run())


def test_footer_rate_updates_and_clear_starts_fresh(monkeypatch, tmp_path):
    import ddtui.app_history as history
    import ddtui.runtime_state as runtime

    async def run():
        fake = FakeProvider()
        fake.rounds = [[LLMStreamEvent(content="完成"),
                        LLMStreamEvent(usage=ProviderUsage(1000, 84))]]
        monkeypatch.setattr(app_mod, "build_provider", lambda name: fake)
        monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
        monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "runtime")
        ticks = iter([10.0, 12.0])
        monkeypatch.setattr(engine_mod, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

        app = app_mod.AgentApp(provider_name="fake")
        async with app.run_test() as pilot:
            bar = app.query_one("#status", StatusBar)
            assert "平均 -- tok/s" in str(bar.render())
            app.run_worker(app._agent_turn("计算速度"), exclusive=True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.counter.average_tokens_per_second == 42
            assert "平均 42.0 tok/s" in bar.render_line(0).text

            app.action_clear_chat()
            await pilot.pause()
            assert app.counter.average_tokens_per_second is None
            assert app.counter.turns == 0
            assert "平均 -- tok/s" in str(bar.render())

    asyncio.run(run())


def test_long_single_turn_auto_compacts_and_continues(monkeypatch, tmp_path):
    """Exercise the real parent pre-round hook, autosave and tool dispatch."""
    import ddtui.app_history as history
    import ddtui.runtime_state as runtime
    from ddtui.context_compaction import safe_boundaries
    from ddtui.history_archive import search_archive
    from ddtui.tools import TOOL_FUNCS

    async def run():
        class LongProvider(FakeProvider):
            def __init__(self):
                self.rounds = 0
            async def stream(self, messages, tools, model, effort):
                self.rounds += 1
                if self.rounds == 1:
                    yield LLMStreamEvent(tool_call=ToolCallDelta(
                        index=0, id="large-output", type="function", name="bash", arguments="{}"))
                else:
                    assert any(m.get("ddtui_history_ref") for m in messages)
                    assert safe_boundaries(messages)[-1] == len(messages)
                    yield LLMStreamEvent(content="已接续长任务")
            async def complete_text(self, *args):
                raise AssertionError("Large-output eviction should not need an LLM summary")

        fake = LongProvider()
        monkeypatch.setattr(app_mod, "build_provider", lambda name: fake)
        monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
        monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setitem(TOOL_FUNCS, "bash", lambda ctx, **kwargs: "log " * 90000 + "\nFAILED exact-end")
        app = app_mod.AgentApp(provider_name="fake")
        async with app.run_test() as pilot:
            app.run_worker(app._agent_turn("持续排查，保留公共接口"), exclusive=True)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert fake.rounds == 2
            assert app.messages[-1]["content"] == "已接续长任务"
            assert app._busy is False
            assert search_archive(app.ctx.session_id, "exact-end")["matches"]
            assert app._ensure_autosave_path().is_file()
            assert not runtime.turn_journal_path(app.ctx.session_id).exists()
    asyncio.run(run())
