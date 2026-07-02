"""TurnEngine unit tests: streaming assembly, tool batches, parallel
execution, meta/confirm hooks, cancellation, abort."""

import asyncio
import time

from ddtui.engine import (
    BLOCKED_BY_CONFIRM,
    TurnEngine,
    TurnObserver,
    outcome_from_result,
)
from ddtui.providers import LLMProvider, LLMStreamEvent, ToolCallDelta
from ddtui.state import ToolContext

SLEEP = 0.15


def tc(i, name, args_json):
    return ToolCallDelta(
        index=i, id=f"call_{i}", type="function", name=name, arguments=args_json
    )


class ScriptedProvider(LLMProvider):
    key = "fake"
    label = "Fake"
    default_model = "m"
    default_effort = "e"

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    async def stream(self, messages, tools, model, effort):
        self.calls.append(len(messages))
        for ev in self.rounds.pop(0):
            yield ev


class Recorder(TurnObserver):
    def __init__(self):
        self.events = []

    async def on_reasoning_delta(self, text):
        self.events.append(("rdelta", text))

    async def on_content_delta(self, text):
        self.events.append(("cdelta", text))

    def on_stream_aborted(self):
        self.events.append(("aborted",))

    async def on_tool_result(self, tc_id, name, args, outcome):
        self.events.append(("result", name, outcome.ok, outcome.diff))


def fake_executor(ctx, name, args):
    if name == "read_file":
        return f"content of {args.get('path')}"
    if name == "bash":
        return "STDOUT:\nhi\nExit code: 0"
    if name == "edit_file":
        return "Edited x (replaced occurrence 1 of 1)\n\n@@ -1 +1 @@\n-a\n+b"
    if name == "boom":
        return "Error: it broke"
    return f"ran {name}"


def make_engine(provider, msgs, **kw):
    kw.setdefault("ctx", ToolContext(work_dir="/tmp"))
    kw.setdefault("tools", [])
    kw.setdefault("model", "m")
    kw.setdefault("effort", "e")
    return TurnEngine(provider=provider, messages=msgs, **kw)


def test_text_round_assembly():
    async def run():
        p = ScriptedProvider([[
            LLMStreamEvent(reasoning="思考"), LLMStreamEvent(reasoning="中"),
            LLMStreamEvent(content="你好"), LLMStreamEvent(content="世界"),
        ]])
        msgs = [{"role": "user", "content": "hi"}]
        rec = Recorder()
        await make_engine(p, msgs, observer=rec).run_turn()
        assert msgs[-1]["content"] == "你好世界"
        assert msgs[-1]["reasoning_content"] == "思考中"  # via attach_reasoning
        deltas = [e for e in rec.events if e[0] in ("rdelta", "cdelta")]
        assert deltas == [("rdelta", "思考"), ("rdelta", "中"),
                          ("cdelta", "你好"), ("cdelta", "世界")]
    asyncio.run(run())


def test_tool_round_serial_diff_and_classification():
    async def run():
        p = ScriptedProvider([
            [LLMStreamEvent(tool_call=tc(0, "edit_file", '{"path":"x"}')),
             LLMStreamEvent(tool_call=tc(1, "boom", "{}"))],
            [LLMStreamEvent(content="done")],
        ])
        msgs = [{"role": "user", "content": "e"}]
        rec = Recorder()
        await make_engine(p, msgs, observer=rec, executor=fake_executor).run_turn()
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["call_0", "call_1"]
        assert "@@" not in tool_msgs[0]["content"]  # diff peeled
        results = [e for e in rec.events if e[0] == "result"]
        assert results[0][3] and "@@" in results[0][3]  # diff to observer
        assert results[1][2] is False  # boom classified not-ok
        assert msgs[-1]["content"] == "done"
        assert p.calls == [1, 4]  # second round saw tool results
    asyncio.run(run())


def test_parallel_reads_run_concurrently_in_order():
    async def run():
        p = ScriptedProvider([
            [LLMStreamEvent(tool_call=tc(0, "read_file", '{"path":"a"}')),
             LLMStreamEvent(tool_call=tc(1, "read_file", '{"path":"b"}')),
             LLMStreamEvent(tool_call=tc(2, "read_file", '{"path":"c"}'))],
            [LLMStreamEvent(content="ok")],
        ])

        def slow_read(ctx, name, args):
            time.sleep(SLEEP)
            return f"content of {args.get('path')}"

        msgs = [{"role": "user", "content": "r"}]
        t0 = time.monotonic()
        await make_engine(p, msgs, executor=slow_read).run_turn()
        assert time.monotonic() - t0 < SLEEP * 2  # concurrent, not 3×serial
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert [m["content"] for m in tool_msgs] == [
            "content of a", "content of b", "content of c",
        ]
    asyncio.run(run())


def test_mixed_batch_preserves_order():
    async def run():
        p = ScriptedProvider([
            [LLMStreamEvent(tool_call=tc(0, "read_file", '{"path":"a"}')),
             LLMStreamEvent(tool_call=tc(1, "bash", '{"command":"x"}')),
             LLMStreamEvent(tool_call=tc(2, "read_file", '{"path":"b"}'))],
            [LLMStreamEvent(content="ok")],
        ])
        msgs = [{"role": "user", "content": "m"}]
        await make_engine(p, msgs, executor=fake_executor).run_turn()
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["call_0", "call_1", "call_2"]
    asyncio.run(run())


def test_confirm_deny_blocks():
    async def run():
        async def deny_writes(name, args):
            return name != "edit_file"

        p = ScriptedProvider([
            [LLMStreamEvent(tool_call=tc(0, "edit_file", '{"path":"x"}'))],
            [LLMStreamEvent(content="ok")],
        ])
        msgs = [{"role": "user", "content": "e"}]
        await make_engine(
            p, msgs, tool_confirm=deny_writes, executor=fake_executor
        ).run_turn()
        assert any(
            m["role"] == "tool" and m["content"] == BLOCKED_BY_CONFIRM
            for m in msgs
        )
    asyncio.run(run())


def test_meta_handler_intercepts_and_never_parallel_guards():
    async def run():
        async def meta(tc_id, name, args, batch_size):
            if name == "checkpoint_get":
                return "Error: checkpoint_get is not available to subagents."
            return None

        p = ScriptedProvider([
            [LLMStreamEvent(tool_call=tc(0, "read_file", '{"path":"a"}')),
             LLMStreamEvent(tool_call=tc(1, "checkpoint_get", "{}")),
             LLMStreamEvent(tool_call=tc(2, "read_file", '{"path":"b"}'))],
            [LLMStreamEvent(content="ok")],
        ])
        executed = []

        def tracking_exec(ctx, name, args):
            executed.append(name)
            return "x"

        msgs = [{"role": "user", "content": "m"}]
        await make_engine(
            p, msgs, meta_handler=meta,
            never_parallel=frozenset({"checkpoint_get"}),
            executor=tracking_exec,
        ).run_turn()
        assert "checkpoint_get" not in executed  # intercepted, never ran
        blocked = [m for m in msgs
                   if m["role"] == "tool" and "not available" in m["content"]]
        assert len(blocked) == 1 and blocked[0]["tool_call_id"] == "call_1"
    asyncio.run(run())


def test_bad_json_args():
    async def run():
        p = ScriptedProvider([
            [LLMStreamEvent(tool_call=tc(0, "read_file", "{bad json"))],
            [LLMStreamEvent(content="ok")],
        ])
        msgs = [{"role": "user", "content": "j"}]
        await make_engine(p, msgs, executor=fake_executor).run_turn()
        assert any("could not parse arguments JSON" in m["content"]
                   for m in msgs if m["role"] == "tool")
    asyncio.run(run())


def test_cancel_mid_tool_appends_stub_and_propagates():
    async def run():
        p = ScriptedProvider([
            [LLMStreamEvent(tool_call=tc(0, "bash", '{"command":"sleep"}'))],
        ])

        def hang(ctx, name, args):
            time.sleep(5)
            return "never"

        msgs = [{"role": "user", "content": "c"}]
        task = asyncio.ensure_future(
            make_engine(p, msgs, executor=hang).run_turn()
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
            cancelled = False
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled
        assert any(m["role"] == "tool" and "⛔" in m["content"] for m in msgs)
    asyncio.run(run())


def test_stream_abort_notifies_and_appends_nothing():
    async def run():
        class Exploding(ScriptedProvider):
            async def stream(self, messages, tools, model, effort):
                yield LLMStreamEvent(content="partial")
                raise RuntimeError("stream died")

        msgs = [{"role": "user", "content": "x"}]
        rec = Recorder()
        try:
            await make_engine(Exploding([]), msgs, observer=rec).run_turn()
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        assert ("aborted",) in rec.events
        assert all(m["role"] != "assistant" for m in msgs)
    asyncio.run(run())


def test_outcome_classification():
    assert outcome_from_result("bash", "Blocked: nope").ok is False
    assert outcome_from_result("bash", "Error: x").ok is False
    assert outcome_from_result("bash", "fine").ok is True
