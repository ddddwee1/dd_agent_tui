"""MCP observe shaping: detail levels, tool folding, result poll."""

import asyncio

import pytest

from ddtui.mcp_server import (
    RelayBackend,
    _condense_messages,
    _last_assistant_text,
)


def _snapshot_messages():
    return [
        {"role": "user", "content": "帮我修 calc.py 的 bug"},
        {
            "role": "assistant",
            "content": "我先看看文件",
            "reasoning_content": "用户想修 bug，我需要先读文件…" * 50,
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "c2", "type": "function",
                 "function": {"name": "bash", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "x" * 64_000},
        {"role": "tool", "tool_call_id": "c2", "content": "ok"},
        {"role": "assistant", "content": "修好了，bug 在 calc.py:12。" + "详" * 900},
    ]


def test_chat_detail_folds_tools_and_drops_reasoning():
    out = _condense_messages(
        _snapshot_messages(), detail="chat", max_messages=20, max_chars=100
    )
    assert [m["role"] for m in out] == [
        "user", "assistant", "tool", "tool", "assistant",
    ]
    asst = out[1]
    assert "reasoning_content" not in asst
    assert "[calls: read_file, bash]" in asst["content"]
    assert out[2]["content"] == "[tool read_file → 64,000 chars]"
    assert out[3]["content"] == "[tool bash → 2 chars]"
    final = out[4]["content"]
    assert final.startswith("修好了")
    assert "chars]" in final and len(final) < 200  # clipped


def test_fold_window_resolves_names_from_outside_tail():
    # tail starts AFTER the assistant that declared the calls
    out = _condense_messages(
        _snapshot_messages(), detail="chat", max_messages=3, max_chars=100
    )
    assert out[0]["content"] == "[tool read_file → 64,000 chars]"
    assert out[1]["content"] == "[tool bash → 2 chars]"


def test_status_detail_returns_no_transcript():
    out = _condense_messages(
        _snapshot_messages(), detail="status", max_messages=20, max_chars=100
    )
    assert out == []


def test_full_detail_is_verbatim_tail():
    msgs = _snapshot_messages()
    out = _condense_messages(msgs, detail="full", max_messages=2, max_chars=100)
    assert out == msgs[-2:]
    assert out[1] is msgs[-1]  # untouched objects


def test_last_assistant_skips_empty_and_clips():
    msgs = _snapshot_messages()
    msgs.append({"role": "assistant", "content": "", "tool_calls": []})
    text = _last_assistant_text(msgs, 50)
    assert text.startswith("修好了")
    assert "chars]" in text


def _backend_with_snapshot():
    backend = RelayBackend(url="ws://x", token="t", client_id="cid")
    backend._snapshots["s1"] = {
        "status": {"busy": True, "queued": 2, "steer": 0},
        "system_message_count": 3,
        "messages": _snapshot_messages(),
    }
    backend._statuses["s1"] = {"busy": True, "queued": 2, "steer": 0}
    return backend


def test_session_view_chat_shape():
    view = _backend_with_snapshot()._session_view(
        "s1", max_events=0, detail="chat", max_messages=20, max_chars=100
    )
    assert view["detail"] == "chat"
    assert view["last_assistant"].startswith("修好了")
    assert all("reasoning_content" not in m for m in view["messages"])
    joined = "".join(m["content"] for m in view["messages"])
    assert "x" * 1000 not in joined  # raw tool payload never leaks


def test_result_poll_minimal(monkeypatch):
    backend = _backend_with_snapshot()

    async def noop(*a, **k):
        return None

    backend._ensure_connected = noop
    backend._subscribe = noop
    out = asyncio.run(backend.result("s1", refresh=False, max_chars=60))
    assert set(out) == {
        "session_id", "busy", "queued", "steer", "last_assistant", "observed_at",
    }
    assert out["busy"] is True and out["queued"] == 2
    assert out["last_assistant"].startswith("修好了")
