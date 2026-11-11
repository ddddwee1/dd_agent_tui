"""TUI task attachment and graceful release with a fake local model."""

import asyncio
from pathlib import Path

import pytest

import ddtui.app as app_mod
import ddtui.app_collab as collab_ui
import ddtui.app_history as history
import ddtui.runtime_state as runtime
from ddtui.collab_protocol import CollabError
from ddtui.collab_store import ClaimLock
from ddtui.providers import LLMProvider, LLMStreamEvent, ToolCallDelta
from tests.test_collab import handoff, receive


class Provider(LLMProvider):
    key = "fake"
    label = "Fake"
    default_model = "fake-model"
    default_effort = "low"
    default_context_limit = 100_000

    def __init__(self, gate=None):
        self.calls = []
        self.gate = gate

    async def stream(self, messages, tools, model, effort):
        self.calls.append(list(messages))
        if self.gate:
            await self.gate.wait()
        yield LLMStreamEvent(content="Current source reviewed; awaiting independent evidence.")


def setup(monkeypatch, service, tmp_path, provider):
    monkeypatch.setenv("DDTUI_COLLAB_HOME", str(service.home))
    monkeypatch.setattr(collab_ui, "ensure_daemon", lambda home: None)
    monkeypatch.setattr(collab_ui, "rpc", lambda home, action, **kw: service.dispatch({"action": action, **kw}))
    monkeypatch.setattr(app_mod, "build_provider", lambda name: provider)
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "runtime")


def test_resume_pause_and_resume_in_another_session(handoff, monkeypatch, tmp_path):
    async def run():
        service, _, _, _ = handoff
        receive(service)
        model = Provider()
        setup(monkeypatch, service, tmp_path, model)
        app = app_mod.AgentApp(provider_name="fake")
        original = app.ctx.work_dir
        async with app.run_test() as pilot:
            await app._handle_collab_command("/collab use remote")
            await app._handle_collab_command("/collab task resume op-a")
            await app.workers.wait_for_complete()
            assert len(model.calls) == 1
            assert Path(app.ctx.work_dir).is_relative_to(service.home / "workspaces")
            assert app.ctx.collab_lock_fd is not None
            assert service.store.task("remote", "op-a")["state"] == "awaiting_input"
            saved = app._autosave_path
            messages = list(app.messages)
            app.action_clear_chat()
            assert list(app.messages) == messages
            with pytest.raises(CollabError):
                ClaimLock(service.home, "remote", "op-a").acquire()
            await app._handle_collab_command("/collab task pause op-a")
            assert service.store.task("remote", "op-a")["state"] == "paused"
            assert app._collab_task is None and app.ctx.work_dir == original
            assert saved.is_file()
            await pilot.pause()
        second = app_mod.AgentApp(provider_name="fake")
        async with second.run_test():
            await second._handle_collab_command("/collab use remote")
            await second._handle_collab_command("/collab task resume op-a")
            await second.workers.wait_for_complete()
            assert len(model.calls) == 2
            assert any("恢复任务" in str(m.get("content", "")) for m in second.messages)
            assert service.store.task("remote", "op-a")["generation"] == 2
            await second._handle_collab_command("/collab task pause op-a")
    asyncio.run(run())


def test_pause_waits_for_model_boundary_and_then_releases(handoff, monkeypatch, tmp_path):
    async def run():
        service, _, _, _ = handoff
        receive(service)
        gate = asyncio.Event()
        model = Provider(gate)
        setup(monkeypatch, service, tmp_path, model)
        app = app_mod.AgentApp(provider_name="fake")
        async with app.run_test() as pilot:
            await app._handle_collab_command("/collab use remote")
            await app._handle_collab_command("/collab task resume op-a")
            await pilot.pause()
            await app._handle_collab_command("/collab task pause op-a")
            assert service.store.task("remote", "op-a")["state"] == "pausing"
            with pytest.raises(CollabError):
                ClaimLock(service.home, "remote", "op-a").acquire()
            gate.set()
            await app.workers.wait_for_complete()
            assert service.store.task("remote", "op-a")["state"] == "paused"
            assert not app._collab_task
    asyncio.run(run())


def test_quit_releases_owned_task(handoff, monkeypatch, tmp_path):
    async def run():
        service, _, _, _ = handoff
        receive(service)
        setup(monkeypatch, service, tmp_path, Provider())
        app = app_mod.AgentApp(provider_name="fake")
        async with app.run_test():
            await app._handle_collab_command("/collab use remote")
            await app._handle_collab_command("/collab task resume op-a")
            await app.workers.wait_for_complete()
            await app.action_quit()
        assert service.store.task("remote", "op-a")["state"] == "paused"
        assert service.store.task("remote", "op-a")["owner"] is None
    asyncio.run(run())


def test_failed_turn_preserves_executed_tools_and_reports_attention(handoff, monkeypatch, tmp_path):
    async def run():
        service, _, _, _ = handoff
        receive(service)

        class Failing(Provider):
            async def stream(self, messages, tools, model, effort):
                self.calls.append(list(messages))
                if len(self.calls) == 1:
                    yield LLMStreamEvent(tool_call=ToolCallDelta(index=0, id="read", type="function",
                        name="read_file", arguments='{"path":"src/kernel.py"}'))
                else:
                    raise RuntimeError("model unavailable after tool execution")

        model = Failing()
        setup(monkeypatch, service, tmp_path, model)
        app = app_mod.AgentApp(provider_name="fake")
        async with app.run_test():
            await app._handle_collab_command("/collab use remote")
            await app._handle_collab_command("/collab task resume op-a")
            await app.workers.wait_for_complete()
            assert any(m.get("role") == "tool" and "value = 1" in m.get("content", "") for m in app.messages)
            assert service.store.task("remote", "op-a")["state"] == "needs_attention"
            assert service.store.task("remote", "op-a")["result"] is None
            await app._handle_collab_command("/collab task pause op-a")
    asyncio.run(run())


def test_missing_task_history_does_not_replay_task(handoff, monkeypatch, tmp_path):
    async def run():
        service, _, _, _ = handoff
        receive(service)
        model = Provider()
        setup(monkeypatch, service, tmp_path, model)
        app = app_mod.AgentApp(provider_name="fake")
        async with app.run_test():
            await app._handle_collab_command("/collab use remote")
            await app._handle_collab_command("/collab task resume op-a")
            await app.workers.wait_for_complete()
            saved = app._autosave_path
            await app._handle_collab_command("/collab task pause op-a")
            saved.unlink()
            await app._handle_collab_command("/collab task resume op-a")
            assert len(model.calls) == 1
            assert app._collab_task is None
            assert service.store.task("remote", "op-a")["owner"] is None
    asyncio.run(run())
