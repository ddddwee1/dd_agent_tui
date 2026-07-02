"""Parent loop end-to-end: a real AgentApp (headless Textual) drives one
turn with a real tool call through the shared TurnEngine."""

import asyncio

import ddtui.app as app_mod
from ddtui.providers import LLMProvider, LLMStreamEvent, ToolCallDelta
from ddtui.widgets import AssistantMessage, ThinkingBlock, ToolCallBlock
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
            assert roles == ["user", "assistant", "tool", "assistant"]

            asst = next(m for m in app.messages if m.get("tool_calls"))
            assert asst["tool_calls"][0]["function"]["name"] == "read_file"
            assert asst["reasoning_content"] == "先读一下 README"

            tool_msg = next(m for m in app.messages if m["role"] == "tool")
            assert "README.md] lines 1-3" in tool_msg["content"]
            assert "\t" in tool_msg["content"]  # numbered output

            assert app.messages[-1]["content"] == "已读取，完成。"
            assert len(app.query(ThinkingBlock)) == 1
            assert len(app.query(ToolCallBlock)) == 1
            assert len(app.query(AssistantMessage)) == 1
            assert app._busy is False

    asyncio.run(run())
