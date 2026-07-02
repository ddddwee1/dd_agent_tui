"""Per-conversation mutable state.

`ToolContext` is the dependency-injection seam — every `tool_*`
function in `ddtui.tools` takes one as its first parameter so the
work-dir, task/terminal tables, and id allocators are explicit rather
than hidden module-level globals.

Why this matters: spawning a subagent later means handing it its own
ToolContext (or a derived copy with isolated task/terminal tables) so
its tool calls don't trip the parent's limits or path resolution.

`TokenCounter` lives here too because it's per-conversation runtime
state queried from both the tool layer and the UI layer (the StatusBar
renders it). Putting it here keeps `widgets` from having to reach into
`tools`'s privates.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .runtime_state import pid_alive, read_json


@dataclass
class TerminalSession:
    """One interactive PTY-backed terminal session.

    Unlike task_start, this is meant for long-lived interactive shells:
    ssh, tmux attach, REPLs, debuggers, and installers that need prompt
    state. Output is mirrored to log_path and a bounded in-memory tail
    for the live UI tab.
    """

    id: str
    name: str
    command: str
    workdir: str
    master_fd: int
    log_path: Path
    proc: subprocess.Popen
    started_at: float
    output_size: int = 0
    tail: str = ""
    finished_at: float | None = None
    closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    pane: "TerminalTabPane | None" = None


@dataclass
class AsyncTask:
    """One managed asynchronous task started by task_start.

    It is shell-backed, but carries task-level lifecycle metadata:
    stable status file, optional completion notification, and a
    retention window for later inspection.
    """

    id: str
    name: str
    command: str
    workdir: str
    output_path: Path
    status_path: Path
    proc: subprocess.Popen | "RecoveredProcess"
    started_at: float
    started_wall: float
    notify_on_complete: bool = True
    session_id: str | None = None
    registry_path: Path | None = None
    runner_pid: int | None = None
    recovered: bool = False
    finished_at: float | None = None
    finished_wall: float | None = None
    return_code: int | None = None
    killed: bool = False
    notified: bool = False


@dataclass
class RecoveredProcess:
    """Small poll-compatible stand-in for a runner from a prior TUI process."""

    pid: int
    status_path: Path

    def poll(self) -> int | None:
        payload = read_json(self.status_path) or {}
        status = payload.get("status")
        rc = payload.get("return_code")
        if status in ("success", "failed", "killed"):
            if isinstance(rc, int):
                return rc
            return 0 if status == "success" else 1
        if pid_alive(self.pid):
            return None
        if isinstance(rc, int):
            return rc
        return 1


def kill_all_terminals(terminals: dict[str, TerminalSession]) -> None:
    """Terminate every live terminal and clear the table."""
    def _signal_pg(t: TerminalSession, sig: int) -> None:
        try:
            os.killpg(os.getpgid(t.proc.pid), sig)
        except Exception:
            pass

    for t in terminals.values():
        if t.proc.poll() is None:
            _signal_pg(t, signal.SIGTERM)
    for _ in range(20):
        alive = [t for t in terminals.values() if t.proc.poll() is None]
        if not alive:
            break
        time.sleep(0.005)
    for t in terminals.values():
        if t.proc.poll() is None:
            _signal_pg(t, signal.SIGKILL)
    for t in terminals.values():
        try:
            os.close(t.master_fd)
        except Exception:
            pass
        t.closed = True
    terminals.clear()


def kill_all_tasks(tasks: dict[str, AsyncTask]) -> None:
    """Tear down every still-running AsyncTask and clear the table."""
    def _signal_pg(t: AsyncTask, sig: int) -> None:
        try:
            os.killpg(os.getpgid(t.proc.pid), sig)
        except Exception:
            pass

    for t in tasks.values():
        if t.proc.poll() is None:
            t.killed = True
            _signal_pg(t, signal.SIGTERM)
    for _ in range(20):
        alive = [t for t in tasks.values() if t.proc.poll() is None]
        if not alive:
            break
        time.sleep(0.005)
    for t in tasks.values():
        if t.proc.poll() is None:
            _signal_pg(t, signal.SIGKILL)
    tasks.clear()


def async_task_status(task: AsyncTask) -> tuple[str, int | None, float]:
    """Returns (status_word, returncode_or_None, elapsed_sec).

    status_word ∈ {"running", "success", "failed", "killed"}.
    """
    payload = read_json(task.status_path) or {}
    payload_status = payload.get("status")
    payload_rc = payload.get("return_code")
    if payload_status in ("success", "failed", "killed"):
        if task.finished_at is None:
            task.finished_at = time.monotonic()
            finished_raw = payload.get("finished_at")
            if isinstance(finished_raw, str):
                try:
                    task.finished_wall = datetime.fromisoformat(
                        finished_raw
                    ).timestamp()
                except Exception:
                    task.finished_wall = time.time()
            else:
                task.finished_wall = time.time()
            task.return_code = payload_rc if isinstance(payload_rc, int) else None
        if payload_status == "killed":
            task.killed = True
            return "killed", task.return_code, task.finished_at - task.started_at
        return (
            "success" if payload_status == "success" else "failed",
            task.return_code,
            task.finished_at - task.started_at,
        )

    rc = task.proc.poll()
    if rc is None:
        return "running", None, time.monotonic() - task.started_at
    if task.finished_at is None:
        task.finished_at = time.monotonic()
        task.finished_wall = time.time()
        task.return_code = rc
    if task.killed:
        return "killed", rc, task.finished_at - task.started_at
    return (
        "success" if rc == 0 else "failed",
        rc,
        task.finished_at - task.started_at,
    )


@dataclass
class ToolContext:
    """Runtime state shared across tool invocations within ONE
    conversation. AgentApp constructs one at startup and threads it
    through `execute_tool`; a future subagent will get its own."""

    work_dir: str
    session_id: str = ""
    terminals: dict[str, TerminalSession] = field(default_factory=dict)
    tasks: dict[str, AsyncTask] = field(default_factory=dict)
    checkpoint: dict | None = None
    terminal_next_id: int = 1
    task_next_id: int = 1

    def alloc_terminal_id(self) -> str:
        """Reserve and return the next terminal id ('term-N')."""
        i = self.terminal_next_id
        self.terminal_next_id += 1
        return f"term-{i}"

    def alloc_task_id(self) -> str:
        """Reserve and return the next managed task id ('task-N')."""
        i = self.task_next_id
        self.task_next_id += 1
        return f"task-{i}"


@dataclass
class SubagentSession:
    """One persistent subagent — created by spawn_agent, fed by chat_agent,
    released by end_agent (or the idle reaper).

    Concurrency model: each round runs in its own `asyncio.Task` stored
    on `task`. `spawn_agent` and `chat_agent` schedule the task and
    return immediately so the parent can pursue other work; the parent
    fetches the round's answer with `await_agent`, which blocks (with
    timeout) on the task and consumes `last_result`. `task is None`
    means the session is between rounds; `last_result is not None`
    means a finished round's answer is sitting unread.

    Holds both the runtime conversation state (messages, own
    ToolContext, tool schemas) AND the UI-visible snapshot fields
    (turn, phase, token totals). AgentApp keeps a `dict[str,
    SubagentSession]`; presence in that dict is the liveness signal.

    `messages` includes the subagent system prompt at index 0 and grows
    every round. `reasoning_content` is preserved on assistant turns
    because DeepSeek thinking-mode requires it on subsequent calls.
    """

    id: str                  # "sub-1", "sub-2", …
    prompt_preview: str      # first ~40 chars of the original spawn prompt
    started_at: float
    last_active_at: float    # bumped on every chat_agent / end of round
    messages: list[dict]     # full subagent conversation, system msg at [0]
    ctx: "ToolContext"       # subagent's own tool context (its own task/terminal tables)
    sub_tools: list[dict]    # tool schemas the subagent sees (no meta tools)
    turn: int = 0            # cumulative round count across all chat calls
    # phase ∈ {"thinking","answering","tool","ready","idle","done","error"}
    # ready  = round finished, result waiting in last_result
    # idle   = result already consumed, awaiting next chat_agent
    phase: str = "thinking"
    last_tool: str | None = None
    tokens_in: int = 0       # cumulative prompt tokens for this subagent
    tokens_out: int = 0      # cumulative completion tokens
    # Most recent call's prompt_tokens and the model's context window
    # so the sidebar can render a ctx% gauge and await_agent can prefix
    # its return with the same number — that's what tells the parent
    # when to chat_agent the subagent into a compact_self call.
    last_prompt_tokens: int = 0
    context_limit: int | None = None
    # asyncio.Task running the current round; None between rounds.
    # Set by spawn_agent / chat_agent, cleared in the task wrapper's
    # `finally`. Cancelled by end_agent / action_clear_chat.
    task: "asyncio.Task | None" = None
    # The most recent round's final answer, waiting for await_agent to
    # consume. None means "no result pending" (already consumed, or
    # task still running with nothing to return yet).
    last_result: str | None = None
    # SubagentTabPane the round runner streams thinking/answer/tool
    # widgets into. Constructed synchronously in _spawn_subagent so the
    # ref exists before the first stream chunk arrives; mounted into the
    # TabbedContent shortly after via call_later. None during /load
    # restores (we don't persist subagent state).
    pane: "SubagentTabPane | None" = None


class TokenCounter:
    """Cumulative + last-call token usage. AgentApp holds one and
    feeds every API response's `usage` into `add()`. StatusBar reads
    the counters to render context-usage gradient + breakdown."""

    def __init__(self) -> None:
        self.prompt_total = 0
        self.completion_total = 0
        self.reasoning_total = 0
        self.last_prompt = 0
        self.last_completion = 0
        self.last_reasoning = 0
        self.turns = 0

    def add(self, usage) -> None:
        if not usage:
            return
        self.last_prompt = getattr(usage, "prompt_tokens", 0) or 0
        self.last_completion = getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "completion_tokens_details", None)
        self.last_reasoning = (
            (getattr(details, "reasoning_tokens", 0) or 0) if details else 0
        )
        self.prompt_total += self.last_prompt
        self.completion_total += self.last_completion
        self.reasoning_total += self.last_reasoning
        self.turns += 1
