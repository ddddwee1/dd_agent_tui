"""Message times survive persistence, recovery, archive views and wire translation."""

import asyncio
import copy
import json
import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from ddtui.app import AgentApp
from ddtui.context_compaction import history_tokens
from ddtui.engine import TurnEngine
from ddtui.history_archive import read_archive, search_archive
from ddtui.history_store import HistoryStore, stamp_message
from ddtui.mcp_server import _condense_messages
from ddtui.providers import CodexResponsesProvider, LLMStreamEvent, _messages_for_wire
from ddtui.state import ToolContext
from ddtui.tool_output import limit_tool_output, trim_tool_history
from tests.test_app_loop import FakeProvider
from tests.test_context_compaction import compact, long_task
from tests.test_subagent import FakeApp, _wait_round
from tests.test_tool_output import Provider, recover, tool_call


KNOWN_TIME = "2026-10-09T12:34:56.789+08:00"


def _assert_timestamp(message):
    value = message["ddtui_timestamp"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}", value)
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() is not None
    return parsed


def test_session_save_resume_and_legacy_continuation(tmp_path, monkeypatch):
    import ddtui.app as app_mod
    import ddtui.app_history as history
    import ddtui.runtime_state as runtime

    async def run():
        monkeypatch.setattr(app_mod, "build_provider", lambda name: FakeProvider())
        monkeypatch.setattr(history, "HISTORY_DIR", tmp_path / "history")
        monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "runtime")
        app = AgentApp(provider_name="fake")
        emitted = []
        app._remote_emit = lambda kind, payload: emitted.append((kind, payload))
        async with app.run_test() as pilot:
            await pilot.pause()
            app._steer = [("保留接口", SimpleNamespace(remove=lambda: None))]
            app._task_events = ["[Async task complete — task-1: success.]"]
            await app._agent_turn("检查 readme")

            times = [_assert_timestamp(message) for message in app.messages]
            assert times == sorted(times)
            steer = next(m for m in app.messages if m.get("content") == "[实时插话] 保留接口")
            assert any(kind == "message.append" and payload.get("message") is steer
                       for kind, payload in emitted)
            assert any(m.get("ddtui_kind") == "runtime_task_event" for m in app.messages)
            assert {m["role"] for m in app.messages} == {"system", "user", "assistant", "tool"}

            saved = json.loads(app._autosave_path.read_text(encoding="utf-8"))
            assert saved["messages"] == list(app.messages)
            await app._load_conversation(app._autosave_path.stem)
            assert list(app.messages) == saved["messages"]

            legacy = copy.deepcopy(saved)
            for message in legacy["messages"]:
                message.pop("ddtui_timestamp")
            (history.HISTORY_DIR / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")
            await app._load_conversation("legacy")
            assert list(app.messages) == legacy["messages"]
            old_count = len(app.messages)
            app.provider.rounds = [[LLMStreamEvent(content="继续完成")]]
            await app._agent_turn("继续")
            assert list(app.messages[:old_count]) == legacy["messages"]
            assert [m["role"] for m in app.messages[old_count:]] == ["user", "assistant"]
            for message in app.messages[old_count:]:
                _assert_timestamp(message)

    asyncio.run(run())


def test_orphan_tool_recovery_stamps_only_the_new_stub():
    app = object.__new__(AgentApp)
    original = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "cancelled", "function": {"name": "bash", "arguments": "{}"}},
    ]}
    app._history = HistoryStore([original])
    assert app._pair_orphan_tool_calls() == 1
    _assert_timestamp(app.messages[-1])
    assert "ddtui_timestamp" not in original
    assert app._pair_orphan_tool_calls() == 0


def test_subagent_prompts_replies_and_notifications_have_timestamps(tmp_path):
    async def run():
        app = FakeApp(str(tmp_path))
        app._spawn_subagent("调查", "只读")
        sess = app._live_subagents["sub-1"]
        await _wait_round(sess)
        first_round = copy.deepcopy(sess.messages)
        for message in first_round:
            _assert_timestamp(message)
        sess.last_result = None
        sess.pending_events = ["[Async task complete — task-1: success.]"]
        assert "chat sent" in app._chat_subagent("sub-1", "继续")
        await _wait_round(sess)
        assert sess.messages[:len(first_round)] == first_round
        assert any(m.get("ddtui_kind") == "runtime_task_event" for m in sess.messages)
        for message in sess.messages:
            _assert_timestamp(message)

    asyncio.run(run())


def test_compaction_keeps_source_times_and_stamps_new_summaries():
    messages = long_task()
    for message in messages:
        message["ddtui_timestamp"] = KNOWN_TIME
    source = copy.deepcopy(messages)
    ctx = ToolContext(work_dir=".", session_id="timestamp-compact")
    candidate, _ = compact(messages, ctx=ctx)
    assert messages == source
    for message in candidate:
        if message.get("ddtui_kind") in {"history_summary", "context_recovery"}:
            _assert_timestamp(message)
        else:
            assert message["ddtui_timestamp"] == KNOWN_TIME
    ref = search_archive(ctx.session_id, "sentinel-0")["matches"][0]["ref"]
    archived = recover(ctx, ref)
    assert archived["ddtui_timestamp"] == KNOWN_TIME


@pytest.mark.parametrize("detail", ["chat", "full"])
def test_mcp_views_keep_times_and_legacy_unknowns(detail):
    messages = [
        {"role": "user", "content": "old input"},
        stamp_message({"role": "assistant", "content": "checking", "tool_calls": [
            {"id": "one", "function": {"name": "read_file"}},
        ]}),
        stamp_message({"role": "tool", "tool_call_id": "one", "content": "x" * 2000}),
    ]
    out = _condense_messages(messages, detail=detail, max_messages=20, max_chars=100)
    assert "ddtui_timestamp" not in out[0]
    assert [m["ddtui_timestamp"] for m in out[1:]] == [m["ddtui_timestamp"] for m in messages[1:]]
    if detail == "chat":
        assert len(out[2]["content"]) < 100
    else:
        assert out[2] is messages[2]


def test_timestamp_metadata_does_not_change_provider_requests_or_token_budget():
    messages = [
        {"role": "system", "content": "framework"},
        {"role": "user", "content": "read"},
        {"role": "assistant", "content": "checking", "reasoning_content": "think",
         "tool_calls": [{"id": "one", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "one", "content": "result"},
    ]
    stamped = [stamp_message(copy.deepcopy(message)) for message in messages]
    assert _messages_for_wire(stamped) == messages
    provider = CodexResponsesProvider.__new__(CodexResponsesProvider)
    assert provider._build_payload(stamped, [], "model", "low") == provider._build_payload(messages, [], "model", "low")
    assert history_tokens(stamped) == history_tokens(messages)
    existing = {"role": "user", "content": "earlier", "ddtui_timestamp": KNOWN_TIME}
    assert stamp_message(existing)["ddtui_timestamp"] == KNOWN_TIME


def test_tool_output_and_repeated_result_archives_keep_original_times(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="tool-times")
    provider = Provider([
        [tool_call(0, "read_file", {"path": "small"}, "first")],
        [tool_call(0, "read_file", {"path": "small"}, "repeat")],
        [tool_call(0, "read_file", {"path": "large"}, "large")],
        [LLMStreamEvent(content="done")],
    ])
    original = "x" * 150000
    messages = []
    asyncio.run(TurnEngine(provider=provider, ctx=ctx, messages=messages, tools=[],
                           model="fake", effort="low", executor=lambda ctx, name, args:
                           "small result" if args["path"] == "small" else original).run_turn())
    first, repeat, large = [m for m in messages if m["role"] == "tool"]
    repeated_source = json.loads(read_archive(ctx.session_id, repeat["ddtui_history_ref"])["text"])
    assert repeated_source["ddtui_timestamp"] == first["ddtui_timestamp"]
    large_source = recover(ctx, large["ddtui_history_ref"])
    assert large_source["ddtui_timestamp"] == large["ddtui_timestamp"]
    assert large_source["content"] == original
    for message in messages:
        _assert_timestamp(message)


@pytest.mark.parametrize("timestamp", [None, KNOWN_TIME])
def test_trimming_old_tool_output_preserves_known_or_unknown_time(tmp_path, timestamp):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="trim-times")
    message = {"role": "tool", "tool_call_id": "old", "content": "x" * 20000}
    if timestamp is not None:
        message["ddtui_timestamp"] = timestamp
    messages = [message]
    trim_tool_history(messages, ctx, max_chars=9000)
    assert messages[0].get("ddtui_timestamp") == timestamp
    source = recover(ctx, messages[0]["ddtui_history_ref"])
    assert source.get("ddtui_timestamp") == timestamp
    assert ("ddtui_timestamp" in source) is (timestamp is not None)
    fresh = limit_tool_output(ctx, "y" * 2000, max_chars=512)
    _assert_timestamp(recover(ctx, fresh.ref))
