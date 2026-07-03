"""Fresh-session semantics: /clear rebuilds the system prefix (current
AGENTS.md + fresh env), and the remote clear command drives it safely."""

import asyncio
from types import SimpleNamespace

import pytest

from ddtui.app import AgentApp
from ddtui.app_remote import AppRemoteMixin


def _stub_app(tmp_path):
    app = object.__new__(AgentApp)  # skip heavyweight __init__
    app.ctx = SimpleNamespace(work_dir=str(tmp_path))
    app.provider = SimpleNamespace(label="FakeProv")
    app.model = "test-model"
    return app


def test_prefix_picks_up_agents_md_edits(tmp_path):
    app = _stub_app(tmp_path)

    (tmp_path / "AGENTS.md").write_text("# 约定 v1\n跑 make test\n")
    prefix = app._build_system_prefix()
    assert len(prefix) == 2
    assert "约定 v1" in prefix[1]["content"]
    assert app._agents_md_loaded is True

    # An external controller edits the docs, then resets the session:
    # the rebuilt prefix must carry the NEW content.
    (tmp_path / "AGENTS.md").write_text("# 约定 v2\n先跑 lint 再跑 make test\n")
    prefix = app._build_system_prefix()
    assert "约定 v2" in prefix[1]["content"]
    assert "约定 v1" not in prefix[1]["content"]


def test_prefix_without_agents_md(tmp_path):
    app = _stub_app(tmp_path)
    prefix = app._build_system_prefix()
    assert len(prefix) == 1
    assert app._agents_md_loaded is False
    assert "FakeProv / test-model" in prefix[0]["content"]


def test_prefix_appends_post_system_prompt(tmp_path, monkeypatch):
    import ddtui.app as app_mod

    monkeypatch.setattr(app_mod, "POST_SYSTEM_PROMPT", "全局覆盖规则")
    app = _stub_app(tmp_path)
    prefix = app._build_system_prefix()
    assert prefix[-1]["content"] == "全局覆盖规则"


class FakeRemoteApp(AppRemoteMixin):
    def __init__(self, busy):
        self._busy = busy
        self.cleared = 0
        self.scheduled = []

    def call_later(self, fn, *args):
        self.scheduled.append(fn)
        fn(*args)

    def action_clear_chat(self):
        self.cleared += 1


def test_remote_clear_refused_while_busy():
    app = FakeRemoteApp(busy=True)
    ack = asyncio.run(app._remote_handle_command({"type": "clear"}))
    assert ack["status"] == "error"
    assert "busy" in ack["message"]
    assert app.cleared == 0


def test_remote_clear_defers_via_call_later():
    app = FakeRemoteApp(busy=False)
    ack = asyncio.run(app._remote_handle_command({"type": "clear"}))
    assert ack["status"] == "accepted"
    assert "new" in ack["message"]
    # scheduled through call_later (ack must be written before the
    # remote client restarts), and it does run action_clear_chat
    assert app.scheduled and app.cleared == 1
