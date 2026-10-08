"""Ordinary results remain exact; oversized results retain recoverable evidence."""

import asyncio
import copy
import json
import subprocess

import pytest

from ddtui.config import BASH_OUTPUT_MAX_CHARS, READ_FILES_MAX_TOTAL_CHARS, TOOL_OUTPUT_MAX_CHARS
from ddtui.context_compaction import safe_boundaries
from ddtui.engine import TurnEngine
from ddtui.history_archive import read_archive
from ddtui.history_store import HistoryStore
from ddtui.providers import LLMProvider, LLMStreamEvent, ToolCallDelta, _messages_for_wire
from ddtui.state import ToolContext
from ddtui.tool_output import limit_tool_output, trim_tool_history
from ddtui.tools_bash import tool_bash
from ddtui.tools_history import tool_history_read
from ddtui.tools_files import tool_read_file, tool_read_files
from ddtui.tools_web import tool_web_fetch


def recover(ctx, ref):
    chunks, start = [], 0
    while True:
        page = read_archive(ctx.session_id, ref, start=start, max_chars=997)
        chunks.append(page["text"])
        start = page["next_start"]
        if start is None:
            return json.loads("".join(chunks))


def noisy_output():
    return ("opening\n" + "普通日志 x\n" * 8000 + "ASSERTION ERROR important-middle\n"
            + "ordinary log y\n" * 8000 + "FAILED last-test\nExit code: 1")


def test_preview_preserves_diagnostics_and_exact_unicode_source(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="unicode")
    original = noisy_output()
    result = limit_tool_output(ctx, original, name="bash", call_id="call-1", max_chars=4000)
    assert len(result) <= 4000
    assert "ASSERTION ERROR important-middle" in result
    assert "FAILED last-test\nExit code: 1" in result
    assert "omitted" in result and "history_read(" in result
    source = recover(ctx, result.ref)
    assert source["content"] == original
    assert source["tool_call_id"] == "call-1"
    assert limit_tool_output(ctx, "short result") == "short result"


def test_bash_budgets_combined_streams_and_archives_before_clipping(tmp_path, monkeypatch):
    ctx = ToolContext(work_dir=str(tmp_path))
    stdout = "stdout start\n" + "stdout noise\n" * 2000 + "stdout tail"
    stderr = "stderr start\n" + "stderr noise\n" * 2000 + "ERROR stderr tail"
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout, stderr)

    monkeypatch.setattr("ddtui.tools_bash.subprocess.run", run)
    result = tool_bash(ctx, "test-command")
    assert len(result) <= BASH_OUTPUT_MAX_CHARS
    assert "ERROR stderr tail" in result and "Exit code: 1" in result
    source = recover(ctx, result.ref)
    assert source["content"] == f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}\nExit code: 1"
    assert source["ddtui_tool_arguments"]["command"] == "test-command"
    assert len(calls) == 1  # Readback never re-executes a command.


def test_web_preview_archives_complete_fetched_text(tmp_path, monkeypatch):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="web")
    original = noisy_output()

    class Response:
        headers = {"Content-Type": "text/plain; charset=utf-8"}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def geturl(self):
            return "https://example.test/log"

        def read(self, size):
            return original.encode()[:size]

    monkeypatch.setattr("ddtui.tools_web.urlopen", lambda *a, **kw: Response())
    result = tool_web_fetch(ctx, "https://example.test/log", max_output_chars=3000)
    assert len(result) <= 3000
    assert "ASSERTION ERROR important-middle" in result
    assert recover(ctx, result.ref)["content"].endswith(original)


def test_batch_file_preview_archives_all_returned_ranges(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="batch-files")
    paths = [f"{i}.txt" for i in range(4)]
    for path in paths:
        (tmp_path / path).write_text("\n".join(f"{i:03d} " + "x" * 60 for i in range(1000)))
    originals = [tool_read_file(ctx, path) for path in paths]
    result = tool_read_files(ctx, [{"path": path} for path in paths])
    assert len(result) <= READ_FILES_MAX_TOTAL_CHARS
    assert result.count("read_files batch cap") == 4
    for path in paths:
        (tmp_path / path).unlink()
    source = recover(ctx, result.ref)["content"]
    for original in originals:
        assert original in source


class Provider(LLMProvider):
    def __init__(self, rounds):
        self.rounds = iter(rounds)
        self.requests = []

    async def stream(self, messages, tools, model, effort):
        self.requests.append(copy.deepcopy(list(messages)))
        for event in next(self.rounds):
            yield event


def tool_call(index, name, args, ident):
    return LLMStreamEvent(tool_call=ToolCallDelta(
        index=index, id=ident, type="function", name=name, arguments=json.dumps(args)))


@pytest.mark.parametrize("subagent", [False, True])
def test_engine_bounds_new_results_without_trimming_accumulated_history(tmp_path, subagent):
    ctx = ToolContext(work_dir=str(tmp_path), session_id=f"engine-{subagent}", is_subagent=subagent)
    rounds = [[tool_call(i, "read_file", {"path": f"{r}-{i}"}, f"c{r}-{i}")
               for i in range(2)] for r in range(8)]
    rounds.append([LLMStreamEvent(content="done")])
    provider = Provider(rounds)
    messages = HistoryStore([{"role": "user", "content": "inspect files"}])
    originals = {}

    def execute(ctx, name, args):
        content = f"[{args['path']}]\n" + noisy_output()
        originals[args["path"]] = content
        return content

    engine = TurnEngine(provider=provider, ctx=ctx, messages=messages, tools=[],
                        model="fake", effort="low", executor=execute)
    asyncio.run(engine.run_turn())
    observed = {}
    for request in provider.requests:
        results = [m for m in request if m["role"] == "tool"]
        assert all(len(m["content"]) <= TOOL_OUTPUT_MAX_CHARS for m in results)
        for message in results:
            assert len(message["content"]) > 32000
            previous = observed.setdefault(message["tool_call_id"], message["content"])
            assert message["content"] == previous
        assert safe_boundaries(request)[-1] == len(request)
        assert all("ddtui_history_ref" not in m for m in _messages_for_wire(request))
    results = [m for m in messages if m["role"] == "tool"]
    assert len(results) == 16
    for r in range(8):
        for i in range(2):
            assert recover(ctx, results[r * 2 + i]["ddtui_history_ref"])["content"] == originals[f"{r}-{i}"]


def test_engine_keeps_explicit_archive_page_exact(tmp_path, monkeypatch):
    from functools import partial
    monkeypatch.setattr("ddtui.engine.limit_tool_output", partial(limit_tool_output, max_chars=4000))
    ctx = ToolContext(work_dir=str(tmp_path), session_id="paging")
    archived = limit_tool_output(ctx, ('"\\quoted text"\n' * 5000), name="bash", max_chars=4000)
    args = {"ref": archived.ref, "max_chars": 12000}
    expected = tool_history_read(ctx, **args)
    assert len(expected) > 4000
    provider = Provider([[tool_call(0, "history_read", args, "read-1")],
                         [LLMStreamEvent(content="read")]])
    messages = [{"role": "user", "content": "read original"}]
    asyncio.run(TurnEngine(provider=provider, ctx=ctx, messages=messages, tools=[],
                           model="fake", effort="low").run_turn())
    result = provider.requests[-1][-1]
    assert result["content"] == expected
    assert "ddtui_history_ref" not in result
    assert json.loads(result["content"])["next_start"] is not None


def test_large_parallel_batch_is_not_trimmed_before_first_followup(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="large-batch")
    provider = Provider([[tool_call(i, "read_file", {"path": str(i)}, f"batch-{i}") for i in range(6)],
                         [LLMStreamEvent(content="done")]])
    messages = [{"role": "user", "content": "inspect"}]
    asyncio.run(TurnEngine(provider=provider, ctx=ctx, messages=messages, tools=[],
                           model="fake", effort="low",
                           executor=lambda *args: noisy_output()).run_turn())
    request = provider.requests[-1]
    results = [m for m in request if m["role"] == "tool"]
    assert len(results) == 6
    assert all(32000 < len(m["content"]) <= TOOL_OUTPUT_MAX_CHARS for m in results)
    assert all("ASSERTION ERROR important-middle" in m["content"] for m in results)
    assert safe_boundaries(request)[-1] == len(request)


def test_repeated_shortening_retains_original_reference(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="repeat-output")
    original = noisy_output()
    first = limit_tool_output(ctx, original)
    message = {"role": "tool", "tool_call_id": "one", "content": str(first),
               "ddtui_history_ref": first.ref, "ddtui_output_chars": len(original)}
    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "one", "function": {"name": "bash", "arguments": "{}"}}]}, message,
                {"role": "assistant", "tool_calls": [{"id": "next", "function": {"name": "bash"}}]}]
    trim_tool_history(messages, ctx, max_chars=9000)
    assert len(messages[1]["content"]) < len(first)
    shortened = copy.deepcopy(messages)
    trim_tool_history(messages, ctx, max_chars=9000)
    assert messages == shortened
    assert messages[1]["content"].count("Full output archived:") == 1
    assert messages[1]["ddtui_history_ref"] == first.ref
    assert recover(ctx, first.ref)["content"] == original


def test_archive_failure_preserves_evidence_without_reexecuting(tmp_path, monkeypatch):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="failure")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("ddtui.tool_output.archive_messages", fail)
    original = noisy_output()
    assert limit_tool_output(ctx, original) == original
    messages = [{"role": "tool", "tool_call_id": "one", "content": original}]
    before = copy.deepcopy(messages)
    trim_tool_history(messages, ctx, max_chars=9000)
    assert messages == before


@pytest.mark.parametrize("cap,size", [(None, 20000), (100000, 80000)])
def test_bash_output_reaches_model_without_extra_clipping(tmp_path, monkeypatch, cap, size):
    stdout = "command output\n" + "x" * size
    monkeypatch.setattr("ddtui.tools_bash.subprocess.run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, stdout, ""))
    args = {"command": "test-command"}
    if cap is not None:
        args["max_output_chars"] = cap
    provider = Provider([[tool_call(0, "bash", args, "command")],
                         [LLMStreamEvent(content="done")]])
    asyncio.run(TurnEngine(provider=provider, ctx=ToolContext(work_dir=str(tmp_path)),
                           messages=[{"role": "user", "content": "run command"}], tools=[],
                           model="fake", effort="low").run_turn())
    result = provider.requests[-1][-1]
    assert result["content"] == f"STDOUT:\n{stdout}\nExit code: 0"
    assert "ddtui_history_ref" not in result


def test_optional_history_budget_preserves_latest_batch(tmp_path):
    ctx = ToolContext(work_dir=str(tmp_path), session_id="latest-batch")
    messages = []
    for index in range(2):
        messages.extend([
            {"role": "assistant", "tool_calls": [{"id": str(index), "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": str(index), "content": "x" * 20000},
        ])
    before = copy.deepcopy(messages)
    trim_tool_history(messages, ctx)
    assert messages == before  # Default history policy leaves >32k intact.
    trim_tool_history(messages, ctx, max_chars=10000)
    assert len(messages[1]["content"]) < 20000
    assert messages[-1] == before[-1]  # Fresh evidence can exceed the soft target.
    safe_boundaries(messages)
