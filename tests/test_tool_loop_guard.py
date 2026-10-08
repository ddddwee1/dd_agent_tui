"""Fresh execution, reusable evidence, and retry guidance across model rounds."""

import asyncio
import json

from ddtui.context_compaction import safe_boundaries
from ddtui.engine import TurnEngine
from ddtui.history_archive import read_archive
from ddtui.providers import LLMProvider, LLMStreamEvent, ToolCallDelta
from ddtui.state import ToolContext
from ddtui.tools import execute_tool


class Provider(LLMProvider):
    def __init__(self, calls):
        self.calls = iter(calls)

    async def stream(self, messages, tools, model, effort):
        call = next(self.calls, None)
        if call is None:
            yield LLMStreamEvent(content="done")
        else:
            ident, name, args = call
            yield LLMStreamEvent(tool_call=ToolCallDelta(
                index=0, id=ident, type="function", name=name, arguments=json.dumps(args)))


def run(ctx, calls, executor=execute_tool, before_round=None):
    messages = [{"role": "user", "content": "do the task"}]
    engine = TurnEngine(provider=Provider(calls), ctx=ctx, messages=messages, tools=[],
                        model="fake", effort="low", executor=executor,
                        before_round=before_round)
    asyncio.run(engine.run_turn())
    assert safe_boundaries(messages)[-1] == len(messages)
    return messages, [m for m in messages if m["role"] == "tool"]


def test_repeated_read_runs_again_and_changed_file_returns_new_evidence(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="read-repeat")
    path = tmp_path / "file.txt"
    path.write_text("initial evidence")
    executed = []

    def execute(ctx, name, args):
        executed.append(name)
        if len(executed) == 3:
            path.write_text("external edit")
        return execute_tool(ctx, name, args)

    _, results = run(ctx, [(str(i), "read_file", {"path": "file.txt"}) for i in range(3)], execute)
    assert executed == ["read_file"] * 3
    assert "initial evidence" in results[0]["content"]
    assert results[1]["content"].startswith("Unchanged result:")
    original = json.loads(read_archive(ctx.session_id, results[1]["ddtui_history_ref"])["text"])
    assert original["content"] == results[0]["content"]
    assert "external edit" in results[2]["content"]
    assert "Unchanged result:" not in results[2]["content"]


def test_mutation_and_explicit_archive_reads_do_not_get_suppressed(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="mutation")
    executed = []
    calls = [("1", "read_file", {"path": "a"}),
             ("2", "write_file", {"path": "a", "content": "same"}),
             ("3", "read_file", {"path": "a"}),
             ("4", "history_read", {"ref": "test"}),
             ("5", "history_read", {"ref": "test"})]

    def execute(ctx, name, args):
        executed.append(name)
        return "same result"

    _, results = run(ctx, calls, execute)
    assert executed == [name for _, name, _ in calls]
    assert all(m["content"] == "same result" for m in results)


def test_identical_failures_warn_but_retries_still_execute(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="retry")
    executed = []

    def execute(ctx, name, args):
        executed.append(name)
        return "STDERR:\nsame failure\nExit code: 1" if len(executed) < 3 else "Exit code: 0"

    _, results = run(ctx, [(str(i), "bash", {"command": "test"}) for i in range(3)], execute)
    assert len(executed) == 3
    assert "Repeated failure:" not in results[0]["content"]
    assert "Repeated failure:" in results[1]["content"]
    assert "Exit code: 1" in results[1]["content"]
    assert results[2]["content"] == "Exit code: 0"


def test_new_user_steer_resets_repetition(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="steer")
    messages = [{"role": "user", "content": "read"}]
    rounds = 0

    async def before():
        nonlocal rounds
        rounds += 1
        if rounds == 2:
            messages.append({"role": "user", "content": "read it again"})

    engine = TurnEngine(provider=Provider([(str(i), "read_file", {"path": "a"}) for i in range(2)]),
                        ctx=ctx, messages=messages, tools=[], model="fake", effort="low",
                        executor=lambda *args: "same", before_round=before)
    asyncio.run(engine.run_turn())
    assert [m["content"] for m in messages if m["role"] == "tool"] == ["same", "same"]


def test_archive_failure_returns_fresh_full_read(tmp_path, monkeypatch):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="archive-failure")

    def fail(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr("ddtui.tool_loop_guard.archive_messages", fail)
    _, results = run(ctx, [(str(i), "read_file", {"path": "a"}) for i in range(2)],
                     lambda *args: "fresh result")
    assert [m["content"] for m in results] == ["fresh result", "fresh result"]


def test_different_queries_and_omitted_content_changes_are_not_duplicates(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="changed-middle")
    bodies = iter(["a\n" * 9000 + mid + "\nz" * 9000 for mid in ["first", "second"]])
    _, results = run(ctx, [(str(i), "search_content", {"pattern": "same"}) for i in range(2)],
                     lambda *args: next(bodies))
    assert all("Unchanged result:" not in m["content"] for m in results)
    _, different = run(ctx, [(str(i), "search_content", {"pattern": str(i)}) for i in range(2)],
                       lambda *args: "no matches")
    assert [m["content"] for m in different] == ["no matches", "no matches"]
