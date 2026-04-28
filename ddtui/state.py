"""Per-conversation mutable state.

`ToolContext` is the dependency-injection seam — every `tool_*`
function in `ddtui.tools` takes one as its first parameter so the
work-dir, bash-job table, and id allocator are explicit rather than
hidden module-level globals.

Why this matters: spawning a subagent later means handing it its own
ToolContext (or a derived copy with isolated bash_jobs) so its tool
calls don't trip the parent's bash limits or path resolution.

`TokenCounter` and `bg_job_status` live here too because they're
per-conversation runtime state queried from both the tool layer (tools
needs bg_job_status to format check/wait/list output) and the UI layer
(the BgJobsBlock sidebar and the StatusBar both render it). Putting
them here keeps `widgets` from having to reach into `tools`'s privates.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BgJob:
    """One background bash process started by tool_bash_start.

    `proc` is held so we can poll() / signal it; `log_path` is the
    merged stdout+stderr file. `finished_at` caches the exit timestamp
    so successive bg_job_status calls report a stable runtime instead
    of ever-growing wall time.
    """

    id: str
    command: str
    workdir: str
    log_path: Path
    proc: subprocess.Popen
    started_at: float
    finished_at: float | None = None


def kill_all_bash_jobs(jobs: dict[str, BgJob]) -> None:
    """Tear down every still-running BgJob in *jobs*: SIGTERM the
    process group, give it ~100 ms to exit, then SIGKILL stragglers.
    Finally clear the dict.

    Used when a ToolContext goes out of scope — e.g. AgentApp.action_clear_chat
    nuking conversation state, or a subagent returning — so background
    processes don't leak past the conversation that started them.
    """
    def _signal_pg(j: BgJob, sig: int) -> None:
        try:
            os.killpg(os.getpgid(j.proc.pid), sig)
        except Exception:
            pass

    for j in jobs.values():
        if j.proc.poll() is None:
            _signal_pg(j, signal.SIGTERM)
    for _ in range(20):
        alive = [j for j in jobs.values() if j.proc.poll() is None]
        if not alive:
            break
        time.sleep(0.005)
    for j in jobs.values():
        if j.proc.poll() is None:
            _signal_pg(j, signal.SIGKILL)
    jobs.clear()


def bg_job_status(job: BgJob) -> tuple[str, int | None, float]:
    """Returns (status_word, returncode_or_None, elapsed_sec).

    status_word ∈ {"running", "exited"}.  Caches finished_at the first
    time we see the process gone so subsequent calls report the same
    runtime (rather than ever-growing wall time)."""
    rc = job.proc.poll()
    if rc is None:
        return "running", None, time.monotonic() - job.started_at
    if job.finished_at is None:
        job.finished_at = time.monotonic()
    return "exited", rc, job.finished_at - job.started_at


@dataclass
class ToolContext:
    """Runtime state shared across tool invocations within ONE
    conversation. AgentApp constructs one at startup and threads it
    through `execute_tool`; a future subagent will get its own."""

    work_dir: str
    bash_jobs: dict[str, BgJob] = field(default_factory=dict)
    bash_next_id: int = 1

    def alloc_bash_id(self) -> str:
        """Reserve and return the next bash job id ('bg-N')."""
        i = self.bash_next_id
        self.bash_next_id += 1
        return f"bg-{i}"


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
    ctx: "ToolContext"       # subagent's own tool context (its own bash_jobs)
    sub_tools: list[dict]    # tool schemas the subagent sees (no meta tools)
    turn: int = 0            # cumulative round count across all chat calls
    # phase ∈ {"thinking","answering","tool","ready","idle","done","error"}
    # ready  = round finished, result waiting in last_result
    # idle   = result already consumed, awaiting next chat_agent
    phase: str = "thinking"
    last_tool: str | None = None
    tokens_in: int = 0       # cumulative prompt tokens for this subagent
    tokens_out: int = 0      # cumulative completion tokens
    # asyncio.Task running the current round; None between rounds.
    # Set by spawn_agent / chat_agent, cleared in the task wrapper's
    # `finally`. Cancelled by end_agent / action_clear_chat.
    task: "asyncio.Task | None" = None
    # The most recent round's final answer, waiting for await_agent to
    # consume. None means "no result pending" (already consumed, or
    # task still running with nothing to return yet).
    last_result: str | None = None


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
