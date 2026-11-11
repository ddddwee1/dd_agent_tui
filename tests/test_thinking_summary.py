"""Live summaries stay bounded, independent of the agent, and replayable."""

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace

import pytest
import httpx
from textual.app import App
from textual.containers import VerticalScroll

from ddtui import config
from ddtui.api_retry import ProviderHTTPError
from ddtui.app_agent_loop import ParentTurnObserver
from ddtui.app_history import AppHistoryMixin
from ddtui.app_subagents import SubagentTurnObserver
from ddtui.app_ui import AppUiMixin
from ddtui.engine import TurnEngine
from ddtui.providers import CodexResponsesProvider, DeepSeekProvider, LLMProvider, LLMStreamEvent, _messages_for_wire
from ddtui.state import TokenCounter, ToolContext
from ddtui.thinking_summary import (
    MAX_EXCERPT_CHARS,
    ROUND_KEY,
    SUMMARY_KEY,
    ThinkingSummaryCoordinator,
    ThinkingSummaryProgress,
    cancel_thinking_summaries,
    summary_from_message,
    thinking_summary_context,
)
from ddtui.widgets import SubagentTabPane, ThinkingBlock, ThinkingSummaryPanel
from ddtui.widgets import ToolCallBlock


def test_short_rounds_share_growth_and_pacing_without_waiting_for_a_new_interval():
    async def run():
        updates = asyncio.Queue()
        calls = []

        async def request(messages):
            calls.append(json.loads(messages[-1]["content"]))
            return "正在根据读取结果核对宽度约束。"

        first = ThinkingSummaryCoordinator(request, lambda *v: updates.put_nowait(v), interval=30, min_chars=10)
        first.feed("甲" * 6)
        first.stop()
        first_message = {}
        first.finish(first_message)
        progress = first.progress
        assert progress.pending_chars == 6 and not calls
        assert first_message[ROUND_KEY] == 1 and SUMMARY_KEY not in first_message
        # An expired shared deadline represents time spent in tools between rounds.
        progress.next_request = 0
        second = ThinkingSummaryCoordinator(
            request, lambda *v: updates.put_nowait(v), interval=30, min_chars=10,
            progress=progress, recent_tools=[{"name": "read_file", "result_excerpt": "读取完成"}],
        )
        second.feed("乙" * 4)
        await asyncio.wait_for(updates.get(), 1)
        second_message = {}
        second.finish(second_message)
        assert calls[0]["new_excerpt"] == "甲" * 6 + "乙" * 4
        assert calls[0]["recent_tools"][0]["name"] == "read_file"
        assert second_message[ROUND_KEY] == 2
        assert progress.pending_chars == 0 and progress.summary == second.summary

    asyncio.run(run())


def test_aborted_round_cannot_replace_committed_task_continuity():
    progress = ThinkingSummaryProgress(
        pending_text="已提交的短思考", pending_chars=8, summary="上一轮有效摘要", rounds=2,
    )
    aborted = ThinkingSummaryCoordinator(None, lambda *v: None, progress=progress)
    aborted.feed("本轮尚未提交的新文本")
    aborted.summary = "本轮尚未提交的摘要"
    aborted.cancel()
    retry = ThinkingSummaryCoordinator(None, lambda *v: None, progress=progress)
    assert retry.round_number == 3 and retry.previous_summary == "上一轮有效摘要"
    assert progress.pending_text == "已提交的短思考" and progress.rounds == 2
    assert retry.summary == ""
    retry.cancel()


def test_context_follows_tools_and_steer_but_new_user_task_starts_fresh():
    messages = [
        {"role": "user", "content": "排查布局"},
        {"role": "assistant", "reasoning_content": "第一轮", SUMMARY_KEY: {"text": "先检查布局。"},
         "tool_calls": [{"id": "read-1", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "read-1", "content": "Error: 文件不存在"},
        {"role": "user", "content": "[实时插话] 注意窄窗口"},
        {"role": "user", "content": "后台任务完成", "ddtui_kind": "runtime_task_event"},
        {"role": "assistant", "reasoning_content": "第二轮"},
    ]
    task, previous, tools, round_number = thinking_summary_context(messages)
    assert task == "排查布局\n[实时插话] 注意窄窗口"
    assert previous == "先检查布局。" and round_number == 3
    assert tools == [{"name": "read_file", "result_excerpt": "Error: 文件不存在"}]
    messages.append({"role": "user", "content": "另一个任务"})
    assert thinking_summary_context(messages) == ("另一个任务", "", [], 1)


def test_coalesces_deltas_without_overlapping_requests_or_growing_input():
    async def run():
        calls, updates = [], asyncio.Queue()
        started, release = asyncio.Event(), asyncio.Event()
        in_flight = 0
        peak = 0

        async def request(messages):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            calls.append(json.loads(messages[-1]["content"]))
            started.set()
            await release.wait()
            in_flight -= 1
            return f"正在分析第{len(calls)}个布局约束。"

        coordinator = ThinkingSummaryCoordinator(
            request, lambda *value: updates.put_nowait(value),
            interval=0, min_chars=10,
        )
        coordinator.feed("甲" * 9000)
        await asyncio.wait_for(started.wait(), 1)
        coordinator.feed("乙" * 500)
        coordinator.feed("丙" * 500)
        assert len(calls) == 1
        release.set()
        first = await asyncio.wait_for(updates.get(), 1)
        second = await asyncio.wait_for(updates.get(), 1)
        assert peak == 1 and len(calls) == 2
        assert len(calls[0]["new_excerpt"]) == MAX_EXCERPT_CHARS
        assert calls[1]["new_excerpt"] == "乙" * 500 + "丙" * 500
        assert calls[1]["previous_summary"] == first[0]
        assert second[0] != first[0]
        coordinator.cancel()

    asyncio.run(run())


def test_interval_and_minimum_growth_both_gate_requests():
    async def run():
        calls = []
        updated = asyncio.Queue()

        async def request(messages):
            calls.append(messages)
            return "检查侧栏宽度。"

        coordinator = ThinkingSummaryCoordinator(
            request, lambda *value: updated.put_nowait(value),
            interval=0.04, min_chars=10,
        )
        coordinator.feed("短文本")
        await asyncio.sleep(0.05)
        assert not calls
        coordinator.feed("足够的新增思考文本")
        await asyncio.wait_for(updated.get(), 1)
        coordinator.feed("又有足够的新增思考文本")
        await asyncio.sleep(0.01)
        assert len(calls) == 1
        await asyncio.sleep(0.05)
        assert len(calls) == 2
        coordinator.cancel()

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["error", "timeout", "empty"])
def test_summary_failure_preserves_previous_text_and_allows_new_progress(failure):
    async def run():
        calls = 0
        updates = []

        async def request(messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                if failure == "error":
                    raise RuntimeError("offline")
                if failure == "timeout":
                    await asyncio.Event().wait()
                return ""
            return "继续检查输入区。"

        coordinator = ThinkingSummaryCoordinator(
            request, lambda text, source: updates.append(text),
            interval=0, min_chars=1, timeout=0.01,
        )
        coordinator.summary = "检查布局约束。"
        coordinator.feed("第一段")
        await asyncio.sleep(0.03)
        assert coordinator.summary == "检查布局约束。" and not updates
        coordinator.feed("第二段")
        await asyncio.sleep(0.03)
        assert updates == ["继续检查输入区。"]
        coordinator.cancel()

    asyncio.run(run())


def test_cancel_discards_late_output_even_when_request_delays_cancellation():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        updates = []

        async def request(messages):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return "过期结果"

        coordinator = ThinkingSummaryCoordinator(
            request, lambda *value: updates.append(value), interval=0, min_chars=1,
        )
        coordinator.feed("取消前的思考")
        await asyncio.wait_for(started.wait(), 1)
        coordinator.cancel()
        release.set()
        with suppress(asyncio.CancelledError):
            await coordinator._worker
        assert not updates and not coordinator.summary

    asyncio.run(run())


def test_native_summary_needs_no_extra_request_and_only_final_text_is_persisted():
    updates = []
    coordinator = ThinkingSummaryCoordinator(None, lambda *value: updates.append(value))
    coordinator.feed("**检查布局**\n\n正在比较主区域的宽度。", native=True)
    message = {"role": "assistant", "reasoning_content": "原文", "content": ""}
    coordinator.finish(message)
    coordinator.feed("过期文本", native=True)
    assert message["reasoning_content"] == "原文"
    assert summary_from_message(message) == ("正在比较主区域的宽度。", "native")
    assert _messages_for_wire([message]) == [
        {"role": "assistant", "reasoning_content": "原文", "content": ""}
    ]
    assert len(updates) == 1
    assert summary_from_message({SUMMARY_KEY: "malformed"}) == ("", "")


def test_disabled_summary_and_native_transition_do_not_leak_raw_text():
    disabled = ThinkingSummaryCoordinator(None, lambda *value: pytest.fail("disabled"), enabled=False)
    disabled.feed("思考文本", native=True)
    message = {}
    disabled.finish(message)
    assert SUMMARY_KEY not in message
    updates = []
    native = ThinkingSummaryCoordinator(None, lambda text, source: updates.append(text))
    native.feed("尚未摘要的很长原文")
    native.feed("服务端返回的摘要。", native=True)
    assert updates == ["服务端返回的摘要。"]
    native.cancel()


def test_deepseek_progress_request_disables_thinking_and_has_no_tools():
    async def run():
        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="检查布局。"),
            )])

        provider = DeepSeekProvider.__new__(DeepSeekProvider)
        provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = await provider.summarize_thinking(
            [{"role": "user", "content": "思考片段", SUMMARY_KEY: {"text": "旧摘要"}}], "summary-model"
        )
        assert result == "检查布局。" and len(calls) == 1
        request = calls[0]
        assert request["extra_body"] == {"thinking": {"type": "disabled"}}
        assert request["max_tokens"] == 192
        assert request["messages"] == [{"role": "user", "content": "思考片段"}]
        assert not {"tools", "tool_choice", "reasoning_effort"} & request.keys()

    asyncio.run(run())


def test_codex_requests_native_summary_and_marks_the_stream_event(monkeypatch):
    async def run():
        import ddtui.providers as providers
        monkeypatch.setattr(providers, "THINKING_SUMMARY_ENABLED", True)
        provider = CodexResponsesProvider.__new__(CodexResponsesProvider)
        payload = provider._build_payload([{"role": "user", "content": "hello"}], [], "m", "low")
        assert payload["reasoning"] == {"effort": "low", "summary": "auto"}
        events = [event async for event in provider._event_to_stream_events(
            {"type": "response.reasoning_summary_text.delta", "delta": "分析布局"}, {}, set()
        )]
        assert events[0].reasoning == "分析布局" and events[0].reasoning_kind == "summary"
        monkeypatch.setattr(providers, "THINKING_SUMMARY_ENABLED", False)
        assert provider._build_payload([], [], "m", "low")["reasoning"] == {"effort": "low"}

    asyncio.run(run())


@pytest.mark.parametrize("summary_rejected", [True, False])
def test_codex_optional_summary_rejection_does_not_break_the_main_request(monkeypatch, summary_rejected):
    async def run():
        import ddtui.providers as providers
        monkeypatch.setattr(providers, "THINKING_SUMMARY_ENABLED", True)
        calls = []

        def handle(request):
            payload = json.loads(request.content)
            calls.append(payload)
            if payload["reasoning"].get("summary"):
                message = "Unsupported parameter: reasoning.summary" if summary_rejected else "Unknown model"
                return httpx.Response(400, json={"error": {"message": message}})
            content = 'data: {"type":"response.output_text.delta","delta":"done"}\n\ndata: {"type":"response.completed","response":{}}\n\n'
            return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(handle)
        monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))

        async def headers():
            return {}

        provider = CodexResponsesProvider.__new__(CodexResponsesProvider)
        provider.auth = SimpleNamespace(headers=headers)
        provider.base_url = "https://provider.test"
        if summary_rejected:
            events = [event async for event in provider.stream([], [], "m", "low")]
            assert "".join(event.content for event in events) == "done"
            assert len(calls) == 2 and "summary" not in calls[-1]["reasoning"]
            assert "summary" not in provider._build_payload([], [], "m", "low")["reasoning"]
            assert provider._build_payload([], [], "another-model", "low")["reasoning"]["summary"] == "auto"
        else:
            with pytest.raises(ProviderHTTPError):
                async for _ in provider.stream([], [], "m", "low"):
                    pass
            assert len(calls) == 1

    asyncio.run(run())


class SummaryHarness(App):
    _mount_widget = AppUiMixin._mount_widget
    _set_thinking_summary = AppUiMixin._set_thinking_summary
    _refresh_sidebar_visibility = AppUiMixin._refresh_sidebar_visibility
    _replay_messages_to_view = AppHistoryMixin._replay_messages_to_view
    _follow_bottom = False
    _busy = True
    model = "model"

    def __init__(self, provider):
        super().__init__()
        self.provider = provider
        self.counter = TokenCounter()
        self.messages = [{"role": "user", "content": "检查布局"}]
        self.remote_events = []

    async def _autosave_conversation(self):
        pass

    def _remote_emit(self, event, payload):
        self.remote_events.append((event, payload))

    def _refresh_status(self):
        pass

    def compose(self):
        yield VerticalScroll(id="conversation")
        with VerticalScroll(id="sidebar"):
            yield ThinkingSummaryPanel(id="thinking-summary")

    def on_unmount(self):
        cancel_thinking_summaries(self)


def test_live_thinking_sidebar_and_replay_share_summary_without_resetting_expansion(monkeypatch):
    async def run():
        monkeypatch.setattr(config, "THINKING_SUMMARY_ENABLED", True)
        monkeypatch.setattr(config, "THINKING_SUMMARY_INTERVAL", 0)
        monkeypatch.setattr(config, "THINKING_SUMMARY_MIN_CHARS", 1)
        requested = asyncio.Event()

        async def summarize(messages, model):
            requested.set()
            return "正在比较侧栏和主区域的宽度约束。"

        provider = SimpleNamespace(supports_thinking_summary=True, summarize_thinking=summarize)
        app = SummaryHarness(provider)
        async with app.run_test(size=(100, 30)) as pilot:
            observer = ParentTurnObserver(app)
            await observer.on_reasoning_delta("需要核对侧栏宽度。")
            thinking = app.query_one(ThinkingBlock)
            thinking.collapsed = False
            await asyncio.wait_for(requested.wait(), 1)
            await pilot.pause()
            assert not thinking.collapsed
            assert thinking.parent is app.query_one("#conversation")
            preview = thinking.query_one(".thinking-summary-preview")
            assert preview.display and "侧栏" in str(preview.render())
            panel = app.query_one(ThinkingSummaryPanel)
            assert panel.display and "侧栏" in str(panel.render())
            message = {"role": "assistant", "content": "", "reasoning_content": "需要核对侧栏宽度。"}
            app.messages.append(message)
            await observer.on_assistant_message(message)
            assert summary_from_message(message)[0] == thinking.summary
            assert not app._thinking_summaries
            await app.query_one("#conversation").remove_children()
            await app._replay_messages_to_view()
            await pilot.pause()
            restored = app.query_one(ThinkingBlock)
            assert restored.summary == message[SUMMARY_KEY]["text"]
            assert restored.text == message["reasoning_content"]
            assert restored.collapsed
            assert restored.parent is app.query_one("#conversation")
            restored_preview = app.query_one(".thinking-summary-preview")
            assert restored_preview.region.height in (1, 2)
            assert app.counter.turns == 0

    asyncio.run(run())


def test_subagent_uses_native_summary_and_retries_start_with_empty_state(monkeypatch):
    async def run():
        monkeypatch.setattr(config, "THINKING_SUMMARY_ENABLED", True)
        app = SummaryHarness(SimpleNamespace())
        sess = SimpleNamespace(messages=[{"role": "user", "content": "检查子任务"}], model="m", pane=None, phase="thinking")
        pane = sess.pane = SubagentTabPane(sess)
        async with app.run_test() as pilot:
            await app.query_one("#conversation").mount(pane)
            await pilot.pause()
            observer = SubagentTurnObserver(app, sess)
            await observer.on_reasoning_summary_delta("正在检查子任务的布局。")
            await pilot.pause()
            assert sess.thinking_summary == "正在检查子任务的布局。"
            observer.on_stream_aborted()
            await pilot.pause()
            assert sess.thinking_summary == "" and not app._thinking_summaries
            await observer.on_reasoning_summary_delta("正在检查新的布局。")
            message = {"role": "assistant", "content": "", "reasoning_content": "正在检查新的布局。"}
            await observer.on_assistant_message(message)
            await pilot.pause()
            assert summary_from_message(message) == ("正在检查新的布局。", "native")
            assert pane.query_one(ThinkingBlock).text == message["reasoning_content"]

    asyncio.run(run())


def test_engine_routes_native_summary_without_inserting_a_chat_message(tmp_path, monkeypatch):
    class NativeProvider(LLMProvider):
        async def stream(self, messages, tools, model, effort):
            yield LLMStreamEvent(reasoning="正在检查侧栏的宽度约束。", reasoning_kind="summary")
            yield LLMStreamEvent(content="检查完成。")

    async def run():
        monkeypatch.setattr(config, "THINKING_SUMMARY_ENABLED", True)
        app = SummaryHarness(NativeProvider())
        async with app.run_test() as pilot:
            observer = ParentTurnObserver(app)
            engine = TurnEngine(
                provider=app.provider, ctx=ToolContext(work_dir=str(tmp_path)),
                messages=app.messages, tools=[], model="m", effort="low", observer=observer,
            )
            await engine.run_turn()
            await pilot.pause()
            assert [message["role"] for message in app.messages] == ["user", "assistant"]
            message = app.messages[-1]
            assert message["content"] == "检查完成。"
            assert message["reasoning_content"] == "正在检查侧栏的宽度约束。"
            assert summary_from_message(message) == ("正在检查侧栏的宽度约束。", "native")
            events = app.remote_events
            reasoning_index = next(i for i, (name, payload) in enumerate(events)
                                   if name == "assistant.delta" and payload["kind"] == "reasoning")
            summary_index = next(i for i, (name, payload) in enumerate(events)
                                 if name == "assistant.summary" and payload["text"])
            assert reasoning_index < summary_index
            assert not app._thinking_summaries

    asyncio.run(run())


def test_multiple_round_summaries_remain_in_order_and_survive_replay(monkeypatch):
    async def run():
        monkeypatch.setattr(config, "THINKING_SUMMARY_ENABLED", True)
        app = SummaryHarness(SimpleNamespace())
        first_text, second_text = "先定位侧栏的最小宽度。", "根据读取结果，正在检查窄窗口布局。"
        async with app.run_test() as pilot:
            observer = ParentTurnObserver(app)
            await observer.on_reasoning_summary_delta(first_text)
            first = {"role": "assistant", "content": "", "reasoning_content": first_text,
                     "tool_calls": [{"id": "read-1", "function": {"name": "read_file", "arguments": "{}"}}]}
            app.messages.append(first)
            await observer.on_assistant_message(first)
            tool = ToolCallBlock("read_file", {})
            await app._mount_widget(tool)
            tool.set_result("ok")
            app.messages.append({"role": "tool", "tool_call_id": "read-1", "content": "ok"})
            await observer.on_reasoning_summary_delta(second_text)
            blocks = list(app.query(ThinkingBlock))
            blocks[0].collapsed = False
            second = {"role": "assistant", "content": "", "reasoning_content": second_text}
            app.messages.append(second)
            await observer.on_assistant_message(second)
            await pilot.pause()
            assert [block.summary for block in blocks] == [first_text, second_text]
            assert [block.round_number for block in blocks] == [1, 2]
            assert not blocks[0].collapsed and blocks[1].collapsed
            assert first_text in str(blocks[0].query_one(".thinking-summary-preview").render())
            assert second_text in str(blocks[1].query_one(".thinking-summary-preview").render())
            assert all(block.parent is app.query_one("#conversation") for block in blocks)
            assert [first[ROUND_KEY], second[ROUND_KEY]] == [1, 2]
            await app.query_one("#conversation").remove_children()
            await app._replay_messages_to_view()
            await pilot.pause()
            restored = list(app.query(ThinkingBlock))
            assert [block.round_number for block in restored] == [1, 2]
            assert [block.summary for block in restored] == [first_text, second_text]
            assert [type(block).__name__ for block in app.query_one("#conversation").children] == [
                "UserBubble", "ThinkingBlock", "ToolCallBlock", "ThinkingBlock",
            ]

    asyncio.run(run())
