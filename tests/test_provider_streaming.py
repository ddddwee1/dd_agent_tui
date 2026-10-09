"""Text-only streaming keeps provider settings and rejects incomplete output."""

import asyncio
from types import SimpleNamespace

import pytest

from ddtui.providers import CodexResponsesProvider, DeepSeekProvider, LLMStreamEvent, ProviderUsage


class FakeStream:
    def __init__(self, iterator):
        self.iterator = iterator
        self.closed = False

    def __aiter__(self):
        return self.iterator

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True
        await self.iterator.aclose()


def test_deepseek_summary_streams_without_tools_or_enabling_thinking():
    async def run():
        requests = []
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10)

        async def chunks():
            for delta in [{"reasoning_content": "整理"}, {"content": "摘要"}, {"content": "完成"}]:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(**delta))], usage=None)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(), finish_reason="stop")], usage=None)
            yield SimpleNamespace(choices=[], usage=usage)

        response = FakeStream(chunks())
        async def create(**kwargs):
            requests.append(kwargs)
            return response

        provider = DeepSeekProvider.__new__(DeepSeekProvider)
        provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        events = [event async for event in provider.stream_text([
            {"role": "user", "content": "历史", "ddtui_kind": "local-metadata"}
        ], "summary-model", "max")]
        assert "".join(event.content for event in events) == "摘要完成"
        assert "".join(event.reasoning for event in events) == "整理"
        assert events[-1].usage is usage
        assert response.closed
        request = requests[0]
        assert request["stream"] is True and request["model"] == "summary-model"
        assert request["messages"] == [{"role": "user", "content": "历史"}]
        assert not {"tools", "tool_choice", "reasoning_effort", "extra_body"} & request.keys()

    asyncio.run(run())


def test_codex_text_stream_passes_deltas_and_closes_on_abort():
    async def run():
        provider = CodexResponsesProvider.__new__(CodexResponsesProvider)
        closed = []

        async def stream(messages, tools, model, effort):
            assert tools == [] and model == "m" and effort == "low"
            try:
                yield LLMStreamEvent(reasoning="整理")
                yield LLMStreamEvent(content="摘要")
            finally:
                closed.append(True)

        provider.stream = stream
        text_stream = provider.stream_text([], "m", "low")
        assert (await anext(text_stream)).reasoning == "整理"
        await text_stream.aclose()
        assert closed == [True]

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["error", "response.failed", "response.incomplete"])
def test_codex_failed_stream_does_not_silently_accept_partial_text(kind):
    async def run():
        provider = CodexResponsesProvider.__new__(CodexResponsesProvider)
        with pytest.raises(RuntimeError, match="流式输出未完成"):
            async for _ in provider._event_to_stream_events({"type": kind}, {}, set()):
                pass

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["length", "content_filter", None])
def test_deepseek_truncated_stream_is_an_error(reason):
    async def run():
        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="摘要前半段"))], usage=None)
            if reason is not None:
                yield SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason)], usage=None)

        response = FakeStream(chunks())
        async def create(**kwargs):
            return response

        provider = DeepSeekProvider.__new__(DeepSeekProvider)
        provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with pytest.raises(RuntimeError, match="流式输出未完成"):
            async for _ in provider.stream_text([], "m", "low"):
                pass
        assert response.closed

    asyncio.run(run())


@pytest.mark.parametrize("completed", [False, True])
def test_codex_summary_requires_the_completion_event(completed):
    async def run():
        provider = CodexResponsesProvider.__new__(CodexResponsesProvider)

        async def stream(*args):
            yield LLMStreamEvent(content="摘要")
            if completed:
                yield LLMStreamEvent(usage=ProviderUsage())

        provider.stream = stream
        async def collect():
            return "".join([event.content async for event in provider.stream_text([], "m", "low")])
        if completed:
            assert await collect() == "摘要"
        else:
            with pytest.raises(RuntimeError, match="未收到完成事件"):
                await collect()

    asyncio.run(run())
