"""Parent loop end-to-end: a real AgentApp (headless Textual) drives one
turn with a real tool call through the shared TurnEngine."""

import asyncio
import copy
from types import SimpleNamespace

import pytest

import ddtui.app as app_mod
import ddtui.engine as engine_mod
from ddtui.providers import LLMProvider, LLMStreamEvent, ProviderUsage, ToolCallDelta
from ddtui.widgets import AssistantMessage, CompactionBlock, StatusBar, ThinkingBlock, ToolCallBlock, ToolCallGroup
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


@pytest.mark.parametrize("automatic", [False, True], ids=["manual", "automatic"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_compaction_status_survives_animation_and_clears(monkeypatch, tmp_path, automatic, outcome):
    import ddtui.app_history as history
    import ddtui.runtime_state as runtime
    from tests.test_context_compaction import long_task

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        class SummaryProvider(FakeProvider):
            async def complete_text(self, *args):
                raise AssertionError("Compaction must use the streaming API")

            async def stream_text(self, messages, model, effort):
                yield LLMStreamEvent(reasoning="正在整理历史证据…")
                yield LLMStreamEvent(content="## 工作状态\n已完成初步排查；")
                started.set()
                await release.wait()
                if outcome == "failure":
                    raise RuntimeError("summarizer down")
                yield LLMStreamEvent(content="下一步修复方案 B，保留公共接口。")

        monkeypatch.setattr(app_mod, "build_provider", lambda name: SummaryProvider())
        monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
        monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "runtime")
        monkeypatch.setattr(history, "AUTO_COMPACT_THRESHOLD", .1)
        app = app_mod.AgentApp(provider_name="fake")
        emissions = []
        monkeypatch.setattr(app, "_remote_emit", lambda event, payload=None: emissions.append((event, payload)))
        async with app.run_test(size=(80, 24)) as pilot:
            # Small tool results force a summary rather than instant eviction.
            app.messages.extend(long_task(30, 300)[1:])
            original = copy.deepcopy(list(app.messages))
            bar = app.query_one("#status", StatusBar)
            assert not app._compacting
            if automatic:
                app._set_busy(True)
                task = asyncio.create_task(app._auto_compact_if_needed())
            else:
                task = asyncio.create_task(app._compact_worker())
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                assert app._compacting and app._busy
                app._tick_progress_bar()
                await pilot.pause()
                assert "上下文压缩中" in bar.render_line(0).text
                assert app._remote_status_payload()["compacting"] is True
                preview = app.query_one(CompactionBlock)
                assert preview.text == "## 工作状态\n已完成初步排查；"
                assert preview.reasoning == "正在整理历史证据…"
                assert not preview.collapsed
                assert list(app.messages) == original  # preview has not been committed
                snapshot = app._remote_snapshot_payload()["compaction"]
                assert snapshot["content"] == preview.text
                assert any(event == "compaction.progress" and payload["kind"] == "content"
                           for event, payload in emissions)

                if outcome == "cancelled":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    release.set()
                    await task
                await pilot.pause()
                assert not app._compacting
                assert "上下文压缩中" not in str(bar.render())
                assert app._remote_status_payload()["compacting"] is False
                assert app._busy is automatic
                assert preview.collapsed
                assert preview.outcome == {"success": "applied", "failure": "failed", "cancelled": "cancelled"}[outcome]
                assert app._remote_snapshot_payload()["compaction"] is None
                assert any(event == "compaction.finished" for event, _ in emissions)
                if outcome == "success":
                    memory = next(m for m in app.messages if m.get("ddtui_kind") == "history_summary")
                    assert memory["content"].endswith(preview.text)
                    assert "下一步修复方案 B" in preview.text
                    assert "正在整理历史证据" not in memory["content"]
                else:
                    assert list(app.messages) == original
            finally:
                if not task.done():
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task

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
        # started_at, first-token timestamp, end-of-stream.
        ticks = iter([10.0, 11.0, 12.0])
        monkeypatch.setattr(engine_mod, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

        app = app_mod.AgentApp(provider_name="fake")
        async with app.run_test() as pilot:
            bar = app.query_one("#status", StatusBar)
            assert "生成 -- tok/s" in str(bar.render())
            app.run_worker(app._agent_turn("计算速度"), exclusive=True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.counter.average_tokens_per_second == 42
            # The footer shows the decode-only rate (84 tokens in the
            # 1s after the first token) plus the prefill wait.
            assert "生成 84.0 tok/s" in bar.render_line(0).text
            assert "首响 1.0s" in bar.render_line(0).text

            app.action_clear_chat()
            await pilot.pause()
            assert app.counter.average_tokens_per_second is None
            assert app.counter.decode_tokens_per_second is None
            assert app.counter.turns == 0
            assert "生成 -- tok/s" in str(bar.render())

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
