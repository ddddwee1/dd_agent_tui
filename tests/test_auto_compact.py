"""Request-boundary budgeting, low-cost eviction and failure backoff."""

import asyncio
import copy

import pytest

import ddtui.context_compaction as cc
from ddtui.app_history import AppHistoryMixin
from ddtui.engine import TurnEngine
from ddtui.providers import LLMProvider, LLMStreamEvent, ProviderUsage, ToolCallDelta
from ddtui.state import ToolContext
from tests.test_context_compaction import Provider, long_task


def run(messages, ctx, provider=None, *, threshold=.75, limit=100_000, tools=(), on_compacting=None):
    return asyncio.run(cc.auto_compact(messages, provider=provider or Provider(), ctx=ctx,
                                      model="m", effort="low", tools=tools,
                                      context_limit=limit, threshold=threshold,
                                      on_compacting=on_compacting))


def test_single_user_long_turn_summarizes_instead_of_in_place_eviction():
    # In-place eviction of retained messages would invalidate the provider's
    # prefix/context cache from the first edited message onward, so even a
    # single long turn must go through the summary pass.
    messages = long_task(12, 5000)
    ctx = ToolContext(work_dir=".", session_id="long")
    provider = Provider()
    shared = messages
    states = []
    stats = run(messages, ctx, provider, on_compacting=states.append)
    assert states == [True, False]
    assert stats["mode"] == "summary"
    assert provider.calls
    assert stats["after_tokens"] <= stats["target_tokens"]
    assert messages is shared
    assert len([m for m in messages if m["role"] == "user"]) == 1
    assert cc.safe_boundaries(messages)[-1] == len(messages)


def test_below_threshold_and_disabled_do_not_archive_or_summarize():
    ctx = ToolContext(work_dir=".", session_id="disabled")
    provider = Provider()
    states = []
    assert run(long_task(1, 10), ctx, provider, on_compacting=states.append) is None
    assert run(long_task(), ctx, provider, threshold=0, on_compacting=states.append) is None
    assert run(long_task(), ctx, provider, limit=None, on_compacting=states.append) is None
    assert not states
    assert not provider.calls
    assert ctx.compact_retry_after == 0


@pytest.mark.parametrize("limit, trigger", [
    (700_000, 500_000),
    (1_000_000, 500_000),
    (100_000, 90_000),
])
def test_token_threshold_and_response_reserve(monkeypatch, limit, trigger):
    ctx = ToolContext(work_dir=".", session_id="token-threshold")
    provider = Provider()
    messages = long_task()
    _, available = cc.request_pressure(ctx, messages, (), limit)
    pressure = trigger - 1
    monkeypatch.setattr(cc, "request_pressure", lambda *args: (pressure, available))
    assert run(messages, ctx, provider, threshold=500_000, limit=limit) is None
    assert not any(m.get("ddtui_history_ref") for m in messages)
    pressure = trigger
    stats = run(messages, ctx, provider, threshold=500_000, limit=limit)
    assert stats["after_tokens"] < stats["before_tokens"]
    # Archived originals stay reachable through the recovery entry; retained
    # messages are no longer edited in place just to carry refs.
    assert any(m.get("ddtui_kind") == "context_recovery" for m in messages)


def test_new_tool_output_triggers_before_next_request_without_usage():
    ctx = ToolContext(work_dir=".", session_id="growth")
    messages = long_task(1, 10)
    assert run(messages, ctx) is None
    messages.extend(long_task(10, 5000)[2:])
    assert run(messages, ctx)


def test_usage_calibration_and_tool_schema_budget_are_counted():
    ctx = ToolContext(work_dir=".", session_id="calibrated")
    messages = long_task(1, 10)
    ctx.context_last_estimate = cc.history_tokens(messages)
    ctx.context_last_prompt = 50_000
    before, available = cc.request_pressure(ctx, messages, [], 100_000)
    assert before >= 50_000
    after, _ = cc.request_pressure(ctx, messages, [{"schema": "x" * 5000}], 100_000)
    assert after > before
    assert available == 90_000


def test_huge_latest_result_can_be_evicted():
    messages = long_task(1, 60000)
    ctx = ToolContext(work_dir=".", session_id="huge")
    stats = run(messages, ctx)
    assert stats and stats["after_tokens"] < 100_000
    assert messages[-1]["tool_call_id"] == "c0"
    assert "FAILED sentinel-0" in messages[-1]["content"]


@pytest.mark.parametrize("error", [RuntimeError("summarizer down"), asyncio.CancelledError()])
def test_failure_leaves_history_and_backs_off(monkeypatch, error):
    calls = []
    states = []
    async def fail(*a, **k):
        assert states == [True]
        calls.append(1)
        raise error
    monkeypatch.setattr(cc, "compact_history", fail)
    ctx = ToolContext(work_dir=".", session_id="failed")
    messages = long_task()
    before = copy.deepcopy(messages)
    with pytest.raises(type(error)):
        run(messages, ctx, on_compacting=states.append)
    assert messages == before
    assert states == [True, False]
    assert run(messages, ctx, on_compacting=states.append) is None
    assert states == [True, False]
    assert len(calls) == 1


def test_model_loop_compacts_between_tools_and_next_request():
    async def scenario():
        ctx = ToolContext(work_dir=".", session_id="engine")
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "one long task"}]
        class StreamProvider(LLMProvider):
            rounds = 0
            async def stream(self, messages, tools, model, effort):
                self.rounds += 1
                if self.rounds == 1:
                    yield LLMStreamEvent(tool_call=ToolCallDelta(index=0, id="c1", type="function", name="bash", arguments="{}"))
                    yield LLMStreamEvent(usage=ProviderUsage(prompt_tokens=100))
                else:
                    assert messages[-1]["role"] == "tool"
                    assert messages[-1].get("ddtui_history_ref")
                    cc.safe_boundaries(messages)
                    yield LLMStreamEvent(content="continued")
        provider = StreamProvider()
        async def before_round():
            await cc.auto_compact(messages, provider=provider, ctx=ctx, model="m", effort="low",
                                  tools=[], context_limit=100_000, threshold=.75)
        engine = TurnEngine(provider=provider, ctx=ctx, messages=messages, tools=[], model="m", effort="low",
                            before_round=before_round, executor=lambda *args: "log " * 100000 + "\nFAILED end")
        await engine.run_turn()
        assert provider.rounds == 2
        assert messages[-1]["content"] == "continued"
    asyncio.run(scenario())


def test_parent_wrapper_saves_and_marks_journal_after_success():
    class App(AppHistoryMixin):
        def __init__(self):
            self.messages = long_task()
            self.ctx = ToolContext(work_dir=".", session_id="host")
            self.provider = Provider()
            self.model, self.effort = "m", "low"
            self._active_explore = None
            self.saved, self.journal, self.mounted = 0, {}, []
            self.compaction_states = []
        def _context_limit(self): return 100_000
        def _set_compacting(self, value): self.compaction_states.append(value)
        async def _autosave_conversation(self): self.saved += 1
        def _write_turn_journal(self, **kwargs): self.journal.update(kwargs)
        async def _mount_widget(self, widget): self.mounted.append(widget)
    app = App()
    asyncio.run(app._auto_compact_if_needed())
    assert app.saved == 1 and app.journal["context_compacted"]
    assert app.compaction_states == [True, False]
    assert app.mounted
