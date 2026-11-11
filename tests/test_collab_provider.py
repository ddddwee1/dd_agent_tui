"""Local endpoints do not silently use cloud models or DeepSeek-only fields."""

import asyncio
import fcntl
import os
from types import SimpleNamespace

import pytest

import ddtui.app as app_module
import ddtui.providers as providers
from ddtui.engine import TurnEngine
from ddtui.providers import LLMStreamEvent, LocalProvider, ProviderConfigError, ToolCallDelta
from ddtui.state import ToolContext
from tests.test_provider_streaming import FakeStream
from ddtui.local_resource import inference_locks


def test_local_requires_explicit_endpoint_and_model(monkeypatch):
    monkeypatch.setattr(providers, "LOCAL_BASE_URL", "")
    with pytest.raises(ProviderConfigError, match="LOCAL_BASE_URL"):
        LocalProvider()
    calls = []

    def fail(name):
        calls.append(name)
        raise ProviderConfigError("not configured")

    monkeypatch.setattr(app_module, "build_provider", fail)
    with pytest.raises(ProviderConfigError):
        app_module.AgentApp(provider_name="local")
    assert calls == ["local"]


def test_local_wire_request_and_tool_calls():
    async def run():
        requests = []

        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                reasoning="consider", tool_calls=[SimpleNamespace(index=0, id="call", type="function",
                    function=SimpleNamespace(name="read_file", arguments='{"path":"src"}'))]), finish_reason=None)])
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(), finish_reason="tool_calls")])

        async def create(**options):
            requests.append(options)
            return FakeStream(chunks())

        provider = LocalProvider.__new__(LocalProvider)
        provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        events = [event async for event in provider.stream(
            [{"role": "assistant", "content": "previous", "reasoning_content": "private thought"}],
            [{"type": "function", "function": {"name": "read_file"}}], "local-model", "default")]
        assert any(event.tool_call and event.tool_call.name == "read_file" for event in events)
        assert events[0].reasoning == "consider"
        assert not {"extra_body", "reasoning_effort", "stream_options"} & requests[0].keys()
        assert "reasoning_content" not in requests[0]["messages"][0]
    asyncio.run(run())


def test_local_blocks_cloud_web_tools_even_if_model_calls_hidden_tool():
    async def run():
        class Provider(LocalProvider):
            def __init__(self):
                self.turn = 0

            async def stream(self, messages, tools, model, effort):
                assert not tools
                self.turn += 1
                if self.turn == 1:
                    yield LLMStreamEvent(tool_call=ToolCallDelta(index=0, id="x", type="function",
                        name="web_fetch", arguments='{"url":"https://example.test"}'))
                else:
                    yield LLMStreamEvent(content="blocked")

        def fail(*args):
            raise AssertionError("Cloud web executor must not run")

        messages = [{"role": "user", "content": "test"}]
        engine = TurnEngine(provider=Provider(), ctx=ToolContext(work_dir="/tmp"), messages=messages,
                            tools=[{"function": {"name": "web_fetch"}}], model="local", effort="default",
                            executor=fail)
        await engine.run_turn()
        assert "disabled for this provider" in next(m["content"] for m in messages if m["role"] == "tool")
    asyncio.run(run())


def test_inference_waits_for_device_and_cancellation_releases_prior_locks(tmp_path):
    async def run():
        environment, device = tmp_path / "environment.lock", tmp_path / "device.lock"
        environment.touch()
        device.touch()
        fd = os.open(device, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        entered = asyncio.Event()

        async def request():
            async with inference_locks([environment, device], 5):
                entered.set()

        task = asyncio.create_task(request())
        try:
            await asyncio.sleep(0.15)
            assert not entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            probe = os.open(environment, os.O_RDWR)
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(probe)
        finally:
            os.close(fd)
        async with inference_locks([environment, device], 1):
            pass
    asyncio.run(run())
