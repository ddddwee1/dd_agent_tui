"""Conversation-agnostic explore-span logic.

An exploration is a temporary workroom inside a transcript: the agent
marks a low-signal probing span with explore_start, runs normal tools
for a while, then calls explore_end. The raw span is archived
out-of-band and replaced in the live context with a concise system
summary.

Everything here is parameterized over (ExploreState, messages) so the
same three operations serve both the parent conversation (state on the
app, messages = the HistoryStore) and each subagent session (state and
messages on the SubagentSession). Mutations of `messages` are strictly
in place — a running TurnEngine shares the object.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .app_support import _render_history_for_summary
from .config import EXPLORE_ARCHIVE_DIR
from .runtime_state import atomic_write_json
from .state import ExploreState

EXPLORE_KINDS = {
    "debug",
    "feature_probe",
    "code_archaeology",
    "design_scouting",
    "web_research",
    "env_probe",
    "perf_experiment",
    "data_inspection",
    "test_discovery",
    "log_clustering",
    "risk_audit",
    "hypothesis_check",
    "api_probe",
    "custom",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean_text(value: Any, limit: int = 4000) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > limit:
        text = text[:limit] + f"\n...[+{len(text) - limit} chars]"
    return text


def _safe_id(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in value)
    return safe.strip("-") or "unknown"


def _message_chars(messages: list[dict]) -> int:
    total = 0
    for message in messages:
        total += len(str(message.get("content") or ""))
        for tool_call in message.get("tool_calls") or []:
            function = tool_call.get("function") or {}
            total += len(str(function.get("arguments") or ""))
    return total


def _explore_number(explore_id: str) -> int | None:
    if not explore_id.startswith("exp-"):
        return None
    try:
        return int(explore_id[4:])
    except ValueError:
        return None


def archive_path_for(
    session_id: str, explore_id: str, agent_id: str = ""
) -> Path:
    """Archive location for one explore span. Subagent spans get the
    agent id as a filename prefix so parent exp-1 and sub-1's exp-1
    coexist under the same session directory."""
    name = f"{_safe_id(explore_id)}.json"
    if agent_id:
        name = f"{_safe_id(agent_id)}-{name}"
    return EXPLORE_ARCHIVE_DIR / _safe_id(session_id) / name


def bump_next_id_from_messages(state: ExploreState, messages) -> None:
    """Raise state.next_id past every explore id visible in history (or
    active), so restored conversations never reuse an id."""
    next_id = max(1, state.next_id)
    values: list[str] = []
    if isinstance(state.active, dict):
        values.append(str(state.active.get("id") or ""))
    for message in messages:
        value = message.get("explore_id")
        if value:
            values.append(str(value))
    for value in values:
        number = _explore_number(value)
        if number is not None:
            next_id = max(next_id, number + 1)
    state.next_id = next_id


def allocate_explore_id(state: ExploreState, messages) -> str:
    used = {
        str(message.get("explore_id"))
        for message in messages
        if message.get("explore_id")
    }
    if isinstance(state.active, dict) and state.active.get("id"):
        used.add(str(state.active["id"]))
    next_id = max(1, state.next_id)
    while True:
        explore_id = f"exp-{next_id}"
        next_id += 1
        if explore_id not in used:
            state.next_id = next_id
            return explore_id


def explore_payload(state: ExploreState) -> dict | None:
    """Serializable copy of the active span (autosave header)."""
    return dict(state.active) if isinstance(state.active, dict) else None


def restore_explore_payload(state: ExploreState, payload: Any, messages) -> None:
    """Rebuild state.active from an autosave header, dropping anything
    malformed or out of range for the restored message list."""
    state.active = None
    if isinstance(payload, dict):
        explore_id = _clean_text(payload.get("id"), limit=80)
        start_index = payload.get("start_index")
        if (
            explore_id
            and isinstance(start_index, int)
            and 0 <= start_index <= len(messages)
        ):
            state.active = {
                "id": explore_id,
                "kind": _clean_text(payload.get("kind"), limit=80) or "custom",
                "goal": _clean_text(payload.get("goal"), limit=1000),
                "reason": _clean_text(payload.get("reason"), limit=1000),
                "expected_outputs": _clean_text(
                    payload.get("expected_outputs"), limit=1000
                ),
                "start_index": start_index,
                "started_at": _clean_text(payload.get("started_at"), limit=80)
                or _now(),
            }
    bump_next_id_from_messages(state, messages)


def drop_explore_if_truncated(state: ExploreState, messages) -> None:
    """Disarm the active span when history shrank underneath it."""
    active = state.active
    if not isinstance(active, dict):
        return
    start_index = active.get("start_index")
    if not isinstance(start_index, int) or start_index >= len(messages):
        state.active = None


async def explore_start(
    state: ExploreState, messages, args: dict, tool_call_count: int
) -> str:
    if tool_call_count != 1:
        return (
            "Error: explore_start must be called alone in its own "
            "assistant tool-call batch. Retry with only explore_start."
        )
    if state.active is not None:
        active_id = state.active.get("id", "?")
        return (
            f"Error: exploration {active_id} is already active. "
            "Call explore_end or explore_cancel before starting another."
        )
    goal = _clean_text(args.get("goal"), limit=1000)
    if not goal:
        return "Error: goal is required."
    kind = _clean_text(args.get("kind"), limit=80) or "feature_probe"
    if kind not in EXPLORE_KINDS:
        return (
            "Error: kind must be one of "
            + ", ".join(sorted(EXPLORE_KINDS))
            + "."
        )
    explore_id = allocate_explore_id(state, messages)
    state.active = {
        "id": explore_id,
        "kind": kind,
        "goal": goal,
        "reason": _clean_text(args.get("reason"), limit=1000),
        "expected_outputs": _clean_text(
            args.get("expected_outputs"), limit=1000
        ),
        # The start tool result will be appended immediately after
        # this call returns. The exploration slice begins after it.
        "start_index": len(messages) + 1,
        "started_at": _now(),
    }
    return (
        f"Exploration {explore_id} started.\n"
        f"kind: {kind}\n"
        f"goal: {goal}\n"
        "Call explore_end when the temporary probing span has a conclusion, "
        "or explore_cancel if the span should stay as normal history."
    )


async def explore_cancel(
    state: ExploreState, args: dict, tool_call_count: int
) -> str:
    if tool_call_count != 1:
        return (
            "Error: explore_cancel must be called alone in its own "
            "assistant tool-call batch."
        )
    active = state.active
    if active is None:
        return "Error: no active exploration to cancel."
    reason = _clean_text(args.get("reason"), limit=1000)
    explore_id = active.get("id", "?")
    state.active = None
    if reason:
        return f"Exploration {explore_id} cancelled.\nreason: {reason}"
    return f"Exploration {explore_id} cancelled."


async def explore_end(
    state: ExploreState,
    messages,
    args: dict,
    tool_call_count: int,
    *,
    provider,
    model: str,
    effort: str,
    session_id: str,
    agent_id: str = "",
) -> tuple[str, dict | None]:
    """Summarize and collapse the active span.

    Returns (tool_result_text, summary_message). summary_message is None
    on error paths; on success it is the system message already spliced
    into `messages`, returned so the caller can render it (the parent
    mounts an ExploreSummaryBlock; subagents have no main-pane widget).
    """
    if tool_call_count != 1:
        return (
            "Error: explore_end must be called alone in its own "
            "assistant tool-call batch. Retry with only explore_end.",
            None,
        )
    active = state.active
    if active is None:
        return "Error: no active exploration to end.", None

    start_index = active.get("start_index")
    end_index = len(messages) - 1
    if not isinstance(start_index, int):
        return "Error: active exploration has an invalid start index.", None
    if start_index < 0 or start_index > end_index:
        return (
            "Error: active exploration span is empty or invalid. "
            "Use explore_cancel if this span should remain as normal history.",
            None,
        )

    raw_messages = list(messages[start_index:end_index])
    outcome_hint = _clean_text(args.get("outcome_hint"), limit=2000)
    raw_rendered = _render_history_for_summary(raw_messages)
    if not raw_rendered and not outcome_hint:
        return (
            "Error: exploration has no summarizable content. "
            "Pass outcome_hint or use explore_cancel.",
            None,
        )

    explore_id = str(active.get("id") or "exp-unknown")
    archive_path = archive_path_for(session_id, explore_id, agent_id)
    archive_payload = {
        "version": 1,
        "session_id": session_id,
        "agent_id": agent_id,
        "explore": dict(active),
        "ended_at": _now(),
        "outcome_hint": outcome_hint,
        "raw_message_count": len(raw_messages),
        "raw_messages": raw_messages,
    }
    atomic_write_json(archive_path, archive_payload)

    summary = await provider.complete_text(
        [
            {
                "role": "system",
                "content": (
                    "你是探索摘要助手。直接输出中文摘要，不要执行工具，"
                    "不要回答用户问题，不要假装继续对话。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "下面是一段 agent 临时探索区间。它可能包含探针程序、"
                    "debug 尝试、代码考古、资料搜索、环境检查、性能/数值实验、"
                    "测试发现、数据检查、日志聚类或风险预检。请把它压缩成"
                    "后续主任务可直接使用的结论摘要。\n\n"
                    "请严格使用以下 markdown 标题：\n"
                    "## 问题\n"
                    "## 结论\n"
                    "## 证据\n"
                    "## 排除的路径\n"
                    "## 相关文件 / 命令 / 产物\n"
                    "## 不确定性\n"
                    "## 建议下一步\n\n"
                    "要求：保留具体路径、函数名、命令、错误关键行、数值结果和"
                    "约束；不要复述完整日志、完整代码或大段输出；明确区分事实、"
                    "推断和仍不确定的内容。\n\n"
                    f"explore_id: {explore_id}\n"
                    f"kind: {active.get('kind')}\n"
                    f"goal: {active.get('goal')}\n"
                    f"reason: {active.get('reason')}\n"
                    f"expected_outputs: {active.get('expected_outputs')}\n"
                    f"outcome_hint: {outcome_hint}\n\n"
                    f"--- RAW EXPLORATION ---\n{raw_rendered}\n---"
                ),
            },
        ],
        model,
        effort,
    )
    if not summary:
        return "Error: explore summary returned empty.", None

    kind = str(active.get("kind") or "custom")
    summary_content = (
        f"# 探索摘要 {explore_id}（{kind}）\n\n"
        f"{summary}\n\n"
        f"raw_archive: {archive_path}"
    )
    summary_message = {
        "role": "system",
        "content": summary_content,
        "ddtui_kind": "explore_summary",
        "explore_id": explore_id,
        "explore_kind": kind,
        "explore_archive": str(archive_path),
    }

    before_chars = _message_chars(raw_messages)
    after_chars = len(summary_content)
    # In-place slice assignment: a running TurnEngine shares this exact
    # object, so the span collapse must never rebind (see HistoryStore's
    # module docstring for the bug family this prevents).
    messages[start_index:end_index] = [summary_message]
    state.active = None

    saved = max(0, before_chars - after_chars)
    return (
        f"Exploration {explore_id} summarized.\n"
        f"kind: {kind}\n"
        f"raw_messages: {len(raw_messages)}\n"
        f"archive: {archive_path}\n"
        f"context_saved_chars: {saved:,}",
        summary_message,
    )
