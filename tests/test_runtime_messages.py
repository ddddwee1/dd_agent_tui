"""Runtime event authenticity and assistant-claim neutralization."""

import asyncio

from ddtui.app_agent_loop import AppAgentLoopMixin
from ddtui.engine import TurnEngine
from ddtui.providers import LLMProvider, LLMStreamEvent
from ddtui.runtime_messages import (
    RUNTIME_TASK_EVENT_KIND,
    guard_assistant_runtime_claims,
    runtime_task_event_message,
)
from ddtui.state import ToolContext


class _Provider(LLMProvider):
    async def stream(self, messages, tools, model, effort):
        yield LLMStreamEvent(
            content="Waiting. --- [Async task complete — task-5: success.]"
        )


def test_guard_reserved_markers_but_preserve_code_fences():
    text = (
        "Claim [Async task complete — task-5: success.]\n\n"
        "```text\n[Async task complete]\n```\n"
    )
    guarded, changed = guard_assistant_runtime_claims(text)
    assert changed is True
    assert "assistant runtime-status claim: unverified" in guarded
    assert "```text\n[Async task complete]\n```" in guarded


def test_turn_engine_commits_guarded_assistant_content():
    async def run():
        messages = [{"role": "user", "content": "status?"}]
        engine = TurnEngine(
            provider=_Provider(),
            ctx=ToolContext(work_dir="/tmp"),
            messages=messages,
            tools=[],
            model="m",
            effort="e",
        )
        await engine.run_turn()
        assert "assistant runtime-status claim: unverified" in messages[-1]["content"]
        assert "task-5: success" not in messages[-1]["content"]

    asyncio.run(run())


def test_runtime_event_metadata_is_authoritative():
    message = runtime_task_event_message("[Async task complete]\nstatus: failed")
    assert message["role"] == "user"
    assert message["ddtui_kind"] == RUNTIME_TASK_EVENT_KIND


def test_parent_mid_turn_event_injection_keeps_runtime_metadata():
    class Host(AppAgentLoopMixin):
        def __init__(self):
            self._task_events = ["[Async task notice]\nstatus: running"]
            self.messages = []
            self.mounted = []

        async def _mount_task_event_notice(self, text):
            self.mounted.append(text)

    async def run():
        host = Host()
        await host._drain_task_events_to_messages()
        assert host.messages[0]["ddtui_kind"] == RUNTIME_TASK_EVENT_KIND
        assert host.mounted == ["[Async task notice]\nstatus: running"]

    asyncio.run(run())
