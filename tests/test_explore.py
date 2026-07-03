"""Explore span: end-tool must mutate messages in place (engine shares the list)."""

import asyncio
import json

import pytest

import ddtui.explore_core as ax
from ddtui.app_explore import AppExploreMixin
from ddtui.state import ExploreState


class FakeProvider:
    async def complete_text(self, messages, model, effort):
        return "## 问题\n找 bug\n\n## 结论\n在 calc.py:12"


class FakeApp(AppExploreMixin):
    def __init__(self):
        self.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "帮我查 calc.py 的 bug"},
        ]
        self._active_explore = None
        self._explore_next_id = 1
        self._session_id = "session-test"
        self.provider = FakeProvider()
        self.model = "test-model"
        self.effort = None
        self.mounted = []

    async def _mount_widget(self, w):
        self.mounted.append(w)


def _tc(name, tc_id):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tc_id,
                "type": "function",
                "function": {"name": name, "arguments": "{}"},
            }
        ],
    }


def _tool(tc_id, content):
    return {"role": "tool", "tool_call_id": tc_id, "content": content}


def _assert_tool_calls_paired(messages):
    """Every assistant(tool_calls) must be followed by matching tool responses."""
    for i, msg in enumerate(messages):
        calls = msg.get("tool_calls") or []
        if msg.get("role") != "assistant" or not calls:
            continue
        expected = [c["id"] for c in calls]
        got = []
        for follower in messages[i + 1 : i + 1 + len(expected)]:
            if follower.get("role") != "tool":
                break
            got.append(follower.get("tool_call_id"))
        assert got == expected, f"orphan tool_calls at index {i}: {expected} vs {got}"


def _run_full_span(app):
    """Drive explore_start → probe → explore_end with engine-like appends."""

    async def scenario():
        # Engine appends the assistant(tool_calls) before dispatching meta.
        app.messages.append(_tc("explore_start", "call_start"))
        result = await app._explore_start_tool(
            {"goal": "定位 calc.py 的 bug", "kind": "debug"}, 1
        )
        assert result.startswith("Exploration exp-1 started.")
        app.messages.append(_tool("call_start", result))

        # The probing span the summary will replace.
        app.messages.append(_tc("read_file", "call_probe"))
        app.messages.append(_tool("call_probe", "def calc(): ..."))
        app.messages.append(
            {"role": "assistant", "content": "bug 在 calc.py:12"}
        )

        app.messages.append(_tc("explore_end", "call_end"))
        result = await app._explore_end_tool({"outcome_hint": "已定位"}, 1)
        assert result.startswith("Exploration exp-1 summarized.")
        # Engine appends the tool response to the list it captured at
        # construction time — the same object app.messages points to.
        app.messages.append(_tool("call_end", result))

    asyncio.run(scenario())


def test_explore_end_mutates_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "EXPLORE_ARCHIVE_DIR", tmp_path)
    app = FakeApp()
    shared = app.messages  # what a running TurnEngine would hold

    _run_full_span(app)

    assert app.messages is shared
    _assert_tool_calls_paired(app.messages)


def test_explore_end_replaces_span_with_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "EXPLORE_ARCHIVE_DIR", tmp_path)
    app = FakeApp()
    _run_full_span(app)

    roles = [m["role"] for m in app.messages]
    # system, user, assistant(start tc), tool, summary, assistant(end tc), tool
    assert roles == [
        "system", "user", "assistant", "tool", "system", "assistant", "tool",
    ]
    summary = app.messages[4]
    assert summary.get("ddtui_kind") == "explore_summary"
    assert "# 探索摘要 exp-1（debug）" in summary["content"]
    assert not any(
        "call_probe" in json.dumps(m, ensure_ascii=False) for m in app.messages
    )
    assert app._active_explore is None


def test_explore_end_archives_raw_span(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "EXPLORE_ARCHIVE_DIR", tmp_path)
    app = FakeApp()
    _run_full_span(app)

    archive = tmp_path / "session-test" / "exp-1.json"
    assert archive.exists()
    payload = json.loads(archive.read_text())
    assert payload["raw_message_count"] == 3
    assert payload["raw_messages"][0]["tool_calls"][0]["id"] == "call_probe"


def test_status_bar_shows_explore_flag(tmp_path):
    from ddtui.state import TokenCounter
    from ddtui.widgets import StatusBar

    bar = StatusBar(str(tmp_path))
    counter = TokenCounter()
    bar.render_status(counter, explore=True)
    assert "explore: on" in str(getattr(bar, "_Static__content", ""))
    bar.render_status(counter, explore=False)
    assert "explore: on" not in str(getattr(bar, "_Static__content", ""))


def test_explore_cancel_keeps_history(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "EXPLORE_ARCHIVE_DIR", tmp_path)
    app = FakeApp()
    shared = app.messages

    async def scenario():
        app.messages.append(_tc("explore_start", "call_start"))
        await app._explore_start_tool({"goal": "试探", "kind": "debug"}, 1)
        app.messages.append(_tool("call_start", "started"))
        app.messages.append(_tc("explore_cancel", "call_cancel"))
        result = await app._explore_cancel_tool({"reason": "没必要"}, 1)
        assert result.startswith("Exploration exp-1 cancelled.")
        app.messages.append(_tool("call_cancel", result))

    asyncio.run(scenario())
    assert app.messages is shared
    assert app._active_explore is None
    _assert_tool_calls_paired(app.messages)


def test_subagent_scoped_span_via_core(tmp_path, monkeypatch):
    """Same core bound to a subagent's own state/messages: archive is
    prefixed with the agent id, mutation stays in place."""
    monkeypatch.setattr(ax, "EXPLORE_ARCHIVE_DIR", tmp_path)
    state = ExploreState()
    messages = [{"role": "system", "content": "sub sys"}]
    shared = messages

    async def scenario():
        messages.append(_tc("explore_start", "c1"))
        result = await ax.explore_start(
            state, messages, {"goal": "查日志", "kind": "log_clustering"}, 1
        )
        assert result.startswith("Exploration exp-1 started.")
        messages.append(_tool("c1", result))
        messages.append(_tc("bash", "c2"))
        messages.append(_tool("c2", "ERROR spam x1000"))
        messages.append(_tc("explore_end", "c3"))
        result, summary = await ax.explore_end(
            state, messages, {"outcome_hint": "聚类完成"}, 1,
            provider=FakeProvider(), model="m", effort="",
            session_id="session-test", agent_id="sub-1",
        )
        assert result.startswith("Exploration exp-1 summarized.")
        assert summary is not None and summary["role"] == "system"
        messages.append(_tool("c3", result))

    asyncio.run(scenario())
    assert messages is shared
    assert state.active is None
    _assert_tool_calls_paired(messages)
    archive = tmp_path / "session-test" / "sub-1-exp-1.json"
    assert archive.exists()
    assert json.loads(archive.read_text())["agent_id"] == "sub-1"


def test_parent_and_subagent_archives_coexist(tmp_path, monkeypatch):
    monkeypatch.setattr(ax, "EXPLORE_ARCHIVE_DIR", tmp_path)
    assert ax.archive_path_for("s", "exp-1") == tmp_path / "s" / "exp-1.json"
    assert (
        ax.archive_path_for("s", "exp-1", agent_id="sub-2")
        == tmp_path / "s" / "sub-2-exp-1.json"
    )
