"""In-context summarization: cache-friendly wire shape and its fallbacks."""

import asyncio

import ddtui.context_compaction as cc
from ddtui.providers import LLMStreamEvent, ToolCallDelta
from ddtui.state import ToolContext
from tests.test_context_compaction import Provider, long_task


def compact(messages, provider, **kwargs):
    return asyncio.run(cc.compact_history(
        messages, provider=provider,
        ctx=ToolContext(work_dir=".", session_id="in-context"),
        model="test-model", effort="low", context_limit=100_000, **kwargs))


def test_in_context_summary_repeats_conversation_wire_shape():
    messages = long_task(6, 3000)
    original = list(messages)
    provider = Provider()
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]
    candidate, stats = compact(messages, provider, tools=tools)
    assert stats["mode"] == "summary"
    # The summary request went through stream() — the normal turn's method —
    # with the SAME tools, and its messages are the live sequence plus one
    # trailing instruction. Byte-identical prefix → full cache reuse.
    request, sent_tools, _model, _effort = provider.calls[-1]
    assert sent_tools == tools
    assert request[:-1] == original
    assert request[-1]["role"] == "user"
    assert "上下文压缩" in request[-1]["content"]
    assert any(m.get("ddtui_kind") == "history_summary" for m in candidate)


def test_oversized_history_falls_back_to_rendered_path():
    # ~240k estimated tokens against a 100k limit: the live sequence cannot
    # fit beside the instruction, so the rendered-text path must run.
    messages = long_task(20, 8000)
    provider = Provider()
    candidate, stats = compact(messages, provider)
    assert stats["mode"] == "summary"
    assert provider.calls
    # stream_text records 3-tuples; the in-context stream records 4-tuples.
    assert all(len(call) == 3 for call in provider.calls)


def test_summary_tool_call_falls_back_to_rendered_path():
    class ActsInsteadOfSummarizing(Provider):
        async def stream(self, messages, tools, model, effort):
            self.calls.append((messages, list(tools), model, effort))
            yield LLMStreamEvent(tool_call=ToolCallDelta(
                index=0, id="t0", type="function", name="bash", arguments="{}"))

    messages = long_task(6, 3000)
    provider = ActsInsteadOfSummarizing()
    candidate, stats = compact(messages, provider)
    assert stats["mode"] == "summary"
    # First attempt used the in-context shape (4-tuple), then the fallback
    # produced the summary through stream_text (3-tuple).
    assert len(provider.calls[0]) == 4
    assert len(provider.calls[-1]) == 3
    assert any(m.get("ddtui_kind") == "history_summary" for m in candidate)
