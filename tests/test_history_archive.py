"""Exact source recovery, scoped lookup and bounded output."""

import json

import pytest

from ddtui.history_archive import archive_messages, read_archive, search_archive
from ddtui.state import ToolContext
from ddtui.tools import execute_tool


def test_roundtrip_pagination_keeps_full_source_and_immutable_refs():
    source = {"role": "tool", "tool_call_id": "call-1", "content": "中间证据\n" * 10000 + "END"}
    batch, refs = archive_messages("session", [source], reason="test")
    _, same = archive_messages("session", [source], reason="test")
    assert refs == same
    pieces = []
    start = 0
    while start is not None:
        page = read_archive("session", refs[0], start=start, max_chars=12000)
        assert len(page["text"]) <= 12000
        pieces.append(page["text"])
        start = page["next_start"]
    assert json.loads("".join(pieces)) == source
    assert search_archive("session", "END", scope=batch)["matches"][0]["ref"] == refs[0]


def test_scope_sessions_and_literals():
    first, refs = archive_messages("parent", [{"role": "user", "content": "No API change; literal %_"}], reason="test")
    second, _ = archive_messages("parent", [{"role": "tool", "content": "different record"}], reason="test")
    archive_messages("child", [{"role": "user", "content": "child secret"}], reason="test")
    assert search_archive("parent", "no api")["matches"]
    assert search_archive("parent", "%_")["matches"]
    assert not search_archive("parent", "no api", scope=second)["matches"]
    assert not search_archive("parent", "child secret")["matches"]
    assert not search_archive("parent", "' OR 1=1 --")["matches"]
    with pytest.raises(ValueError, match="Unknown history ref"):
        read_archive("child", refs[0])


def test_search_pagination_and_clamped_limits():
    archive_messages("many", [{"role": "user", "content": f"needle-{i}"} for i in range(25)], reason="test")
    first = search_archive("many", "needle", limit=999)
    second = search_archive("many", "needle", offset=first["next_offset"])
    assert len(first["matches"]) == 10 and first["next_offset"] == 10
    assert not ({m["ref"] for m in first["matches"]} & {m["ref"] for m in second["matches"]})


def test_tools_return_actionable_missing_archive_errors():
    ctx = ToolContext(work_dir=".", session_id="no-archive")
    assert execute_tool(ctx, "history_search", {"query": "missing"}).startswith("Error: No archived history")
    assert execute_tool(ctx, "history_read", {"ref": "missing"}).startswith("Error: No archived history")


def test_reloaded_session_can_search_same_archive():
    ctx = ToolContext(work_dir=".", session_id="restore-me")
    _, refs = archive_messages(ctx.session_id, [{"role": "user", "content": "keep-public-ABI"}], reason="test")
    reloaded = ToolContext(work_dir=".", session_id=ctx.session_id)
    found = json.loads(execute_tool(reloaded, "history_search", {"query": "keep-public-ABI"}))
    assert found["matches"][0]["ref"] == refs[0]
