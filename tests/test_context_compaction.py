"""Recovery fidelity and safe context transitions, without live model calls."""

import asyncio
import copy
import json

import pytest

import ddtui.context_compaction as cc
from ddtui.app_history import AppHistoryMixin
from ddtui.explore_core import explore_start, explore_end, resolve_start_index, restore_explore_payload
from ddtui.history_archive import archive_messages, read_archive, search_archive
from ddtui.providers import LLMStreamEvent, ToolCallDelta, _messages_for_wire
from ddtui.state import ExploreState, ToolContext


class Provider:
    def __init__(self, summary="## 待办、阻塞与下一步\n方案 B 已修改，尚未测试。"):
        self.calls = []
        self.summary = summary

    def context_limit_for_model(self, model):
        return 100_000

    async def stream_text(self, messages, model, effort):
        self.calls.append((copy.deepcopy(messages), model, effort))
        yield LLMStreamEvent(content=self.summary)

    async def stream(self, messages, tools, model, effort):
        # In-context summary path: same wire method as a normal turn.
        self.calls.append((copy.deepcopy(messages), list(tools), model, effort))
        yield LLMStreamEvent(content=self.summary)


def call(name, ident):
    return {"role": "assistant", "reasoning_content": "reasoning must travel with retained calls",
            "content": "", "tool_calls": [{"id": ident, "type": "function",
            "function": {"name": name, "arguments": '{"path":"source.py"}'}}]}


def result(ident, body):
    return {"role": "tool", "tool_call_id": ident, "content": body}


def long_task(n=12, size=5000):
    messages = [{"role": "system", "content": "real framework rules"},
                {"role": "user", "content": "修复方案 B；不能修改公共接口。"}]
    for i in range(n):
        messages.extend([call("bash", f"c{i}"), result(f"c{i}", "noise " * size + f"\nFAILED sentinel-{i}")])
    return messages


def compact(messages, ctx=None, provider=None, **kwargs):
    return asyncio.run(cc.compact_history(
        messages, provider=provider or Provider(),
        ctx=ctx or ToolContext(work_dir=".", session_id="test"),
        model="test-model", effort="low", context_limit=100_000, **kwargs))


def test_two_compactions_merge_old_state_and_preserve_sources():
    ctx = ToolContext(work_dir=".", session_id="repeat")
    first_provider = Provider("## 关键决定\n旧决定 A 尚未验证。")
    initial = long_task()
    first, stats = compact(initial, ctx, first_provider)
    original_failure = search_archive(ctx.session_id, "sentinel-0")["matches"][0]["ref"]
    first.extend([{"role": "user", "content": "改用 B，保留公共接口。"}])
    for i in range(10):
        first.extend([call("bash", f"new-{i}"), result(f"new-{i}", "new noise " * 5000)])
    provider = Provider()
    second, stats = compact(first, ctx, provider)
    assert len([m for m in second if m.get("ddtui_kind") == "history_summary"]) == 1
    assert len([m for m in second if m.get("ddtui_kind") == "context_recovery"]) == 1
    assert "旧决定 A" in str(provider.calls)
    assert "改用 B，保留公共接口。" in str(provider.calls)
    assert second[0] == initial[0]
    assert any(m.get("content") == "改用 B，保留公共接口。" for m in second)
    assert read_archive(ctx.session_id, original_failure)["ref"] == original_failure
    assert stats["after_tokens"] < stats["before_tokens"]
    cc.safe_boundaries(second)


def test_legacy_system_summaries_are_consolidated():
    messages = long_task()
    messages[1:1] = [{"role": "system", "content": "# 历史摘要（来自 /compact）\n旧记忆"}]
    provider = Provider()
    candidate, _ = compact(messages, provider=provider)
    assert "旧记忆" in str(provider.calls)
    assert len([m for m in candidate if m.get("content", "").startswith("# 历史摘要")]) == 1


def test_evidence_excerpt_keeps_middle_errors_and_tail():
    body = "opening\n" + "x\n" * 1000 + "ASSERTION ERROR MIDDLE\n" + "y\n" * 1000 + "exit code: 1\nFAILED final-test"
    excerpt = cc.evidence_excerpt(body)
    assert "ASSERTION ERROR MIDDLE" in excerpt
    assert "FAILED final-test" in excerpt
    assert "omitted" in excerpt
    assert len(excerpt) <= 1600


def test_full_arguments_and_runtime_event_identity_reach_archive():
    messages = long_task()
    messages[2]["tool_calls"][0]["function"]["arguments"] = json.dumps({"path": "source.py", "content": "x" * 3000 + "argument-end"})
    event = {"role": "user", "ddtui_kind": "runtime_task_event", "content": "runtime-only-not-a-user-goal"}
    messages[4:4] = [event]
    provider = Provider()
    candidate, _ = compact(messages, provider=provider)
    assert "runtime_task_event" in str(provider.calls)
    assert "argument-end" in str(provider.calls)
    match = search_archive("test", "argument-end")["matches"][0]
    recovered = read_archive("test", match["ref"], max_chars=12000)
    assert "argument-end" in recovered["text"]
    assert not any(m is event for m in candidate)


def test_pending_compact_self_batch_kept_with_its_reasoning():
    messages = long_task()
    pending = call("compact_self", "pending")
    pending["tool_calls"].append({"id": "next", "type": "function", "function": {"name": "bash", "arguments": "{}"}})
    messages.append(pending)
    candidate, _ = compact(messages)
    assert candidate[-1] == pending
    candidate.extend([result("pending", "compacted"), result("next", "done")])
    assert cc.safe_boundaries(candidate)[-1] == len(candidate)
    assert candidate[-3]["reasoning_content"] == pending["reasoning_content"]
    assert all("ddtui_kind" not in m for m in _messages_for_wire(candidate))


@pytest.mark.parametrize("summary", ["", "   "])
def test_invalid_summary_does_not_change_history(summary):
    messages = long_task()
    before = copy.deepcopy(messages)
    with pytest.raises((RuntimeError, ValueError)):
        compact(messages, provider=Provider(summary))
    assert messages == before
    assert search_archive("test", "sentinel-0")["matches"]


def test_summary_over_former_character_limit_is_accepted_in_full():
    summary = "保留必要工作记录。" * 2500
    provider = Provider(summary)
    candidate, stats = compact(long_task(), provider=provider)
    memory = next(m for m in candidate if m.get("ddtui_kind") == "history_summary")
    assert memory["content"].endswith(summary)
    assert stats["after_tokens"] < stats["before_tokens"]
    assert provider.calls
    assert all("输出最多" not in messages[0]["content"]
               for messages, _, _ in provider.calls)


def test_archive_failure_never_calls_model_or_changes_history(monkeypatch):
    messages = long_task()
    before = copy.deepcopy(messages)
    provider = Provider()
    def fail(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(cc, "archive_messages", fail)
    with pytest.raises(OSError, match="disk full"):
        compact(messages, provider=provider)
    assert not provider.calls
    assert messages == before


def test_cancellation_keeps_original_messages():
    class Cancelled(Provider):
        async def stream_text(self, *args):
            yield LLMStreamEvent(content="未完成的摘要")
            raise asyncio.CancelledError()
    messages = long_task()
    before = copy.deepcopy(messages)
    with pytest.raises(asyncio.CancelledError):
        compact(messages, provider=Cancelled())
    assert messages == before


@pytest.mark.parametrize("event", [
    LLMStreamEvent(reasoning="只有思路，没有摘要"),
    LLMStreamEvent(tool_call=ToolCallDelta(index=0, name="bash", arguments="{}")),
])
def test_reasoning_and_tool_calls_cannot_be_committed_as_summary(event):
    class InvalidProvider(Provider):
        async def stream_text(self, *args):
            yield event
    messages = long_task()
    before = copy.deepcopy(messages)
    with pytest.raises(RuntimeError):
        compact(messages, provider=InvalidProvider())
    assert messages == before


def test_checkpoint_is_directly_available_in_recovery_card():
    ctx = ToolContext(work_dir=".", session_id="checkpoint")
    ctx.checkpoint = {"goal": "preserve ABI", "next_steps": ["run correctness tests"], "updated_at": "earlier"}
    ctx.doc_receipts = {"doc-1": {}}
    ctx.experiments = {"exp-1": {}}
    candidate, _ = compact(long_task(), ctx)
    card = next(m["content"] for m in candidate if m.get("ddtui_kind") == "context_recovery")
    assert "preserve ABI" in card and "run correctness tests" in card
    assert "doc_route_status" in card and "experiment_status" in card


def test_open_exploration_compacts_in_stages_and_can_still_end(tmp_path, monkeypatch):
    import ddtui.explore_core as ex
    monkeypatch.setattr(ex, "EXPLORE_ARCHIVE_DIR", tmp_path / "explorations")
    async def run():
        ctx = ToolContext(work_dir=".", session_id="exploring")
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "找 bug"}, call("explore_start", "start")]
        state = ExploreState()
        answer = await explore_start(state, messages, {"goal": "find bug", "kind": "debug"}, 1)
        messages.append(result("start", answer))
        for i in range(12):
            messages.extend([call("bash", f"p{i}"), result(f"p{i}", "noise " * 5000)])
        prefix = resolve_start_index(state.active, messages)
        candidate, _ = await cc.compact_history(messages, provider=Provider(), ctx=ctx,
                                               model="m", effort="e", context_limit=100_000,
                                               protected_prefix=prefix)
        messages[:] = candidate
        assert resolve_start_index(state.active, messages) == prefix
        restored = ExploreState()
        restore_explore_payload(restored, dict(state.active), messages)
        assert restored.active["start_message_id"] == state.active["start_message_id"]
        messages.append(call("explore_end", "end"))
        answer, summary = await explore_end(state, messages, {}, 1, provider=Provider(), model="m", effort="e", session_id=ctx.session_id)
        messages.append(result("end", answer))
        assert summary and state.active is None
        assert cc.safe_boundaries(messages)[-1] == len(messages)
    asyncio.run(run())


def test_stable_explore_anchor_survives_preceding_message_removal():
    state = {"id": "exp-1", "start_index": 99, "start_message_id": "stable"}
    messages = [{**call("explore_start", "start"), "ddtui_message_id": "stable"}, result("start", "started")]
    assert resolve_start_index(state, messages) == 2
    with pytest.raises(ValueError, match="missing"):
        resolve_start_index(state, [])


def test_hierarchical_summarizer_bounds_requests():
    provider = Provider("small summary with source msg-ref")
    progress = []
    async def on_progress(kind, text):
        progress.append((kind, text))
    output = asyncio.run(cc.summarize(provider, "m", "e", "中文证据" * 8000, "current goal",
                                      input_tokens=4000, on_progress=on_progress))
    assert output
    assert len(provider.calls) > 2
    assert all(cc.estimate_tokens(messages) < 4000 for messages, _, _ in provider.calls)
    starts = [text for kind, text in progress if kind == "start"]
    assert len(starts) == len(provider.calls)
    assert starts[0].startswith("分段摘要 · 第 1 轮 · 1/")
    assert starts[-1] == "合并分段摘要…"
    assert [text for kind, text in progress if kind == "content"] == [provider.summary] * len(starts)


def test_no_reduction_rejected():
    with pytest.raises(ValueError, match="未减少"):
        compact([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}])


def test_recovery_after_compaction_does_not_apply_old_journal_indices(tmp_path, monkeypatch):
    import ddtui.runtime_state as runtime
    monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "runtime")
    session = "recovery-journal"
    ctx = ToolContext(work_dir=".", session_id=session)
    compacted, _ = compact(long_task(), ctx)
    runtime.atomic_write_json(runtime.turn_journal_path(session), {
        "phase": "streaming", "tool_started": False, "context_compacted": True,
        "message_len_before_turn": 2, "pending_user_text": "long task",
    })
    history, notice, input_text, modified = AppHistoryMixin()._recover_messages_from_journal(compacted, session)
    assert history == compacted
    assert "压缩后" in notice
    assert input_text is None and not modified


def test_main_and_subagent_wrapper_use_correct_context_model_and_effort():
    app = AppHistoryMixin()
    app.provider, app.model, app.effort = Provider(), "parent-model", "high"
    app.ctx = ToolContext(work_dir=".", session_id="parent")
    child = ToolContext(work_dir=".", session_id="parent-sub-1", is_subagent=True)
    candidate, _ = asyncio.run(app._compact_messages(long_task(), ctx=child, model="child-model", effort="low"))
    assert all(model == "child-model" and effort == "low" for _, model, effort in app.provider.calls)
    assert search_archive(child.session_id, "sentinel-0")["matches"]
    assert "checkpoint_get" not in next(m["content"] for m in candidate if m.get("ddtui_kind") == "context_recovery")
    from ddtui.history_archive import archive_path
    assert not archive_path(app.ctx.session_id).exists()


def test_malformed_tool_history_rejected_before_archive():
    from ddtui.history_archive import archive_path
    with pytest.raises(ValueError, match="orphan"):
        compact([{"role": "user", "content": "task"}, result("missing", "result")])
    assert not archive_path("test").exists()


def test_multi_turn_tail_remains_verbatim_when_within_budget():
    messages = long_task()
    recent = [{"role": "user", "content": "new question"}, {"role": "assistant", "content": "answer"},
              {"role": "user", "content": "latest correction"}, {"role": "assistant", "content": "ok"}]
    messages.extend(recent)
    candidate, _ = compact(messages)
    assert candidate[-len(recent):] == recent
