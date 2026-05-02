"""Bash and background bash tool implementations."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from .config import (
    ALLOW_UNSANDBOXED_BASH,
    BASH_OUTPUT_MAX_CHARS,
    BASH_TIMEOUT,
    BG_DEFAULT_TAIL_LINES,
    BG_DEFAULT_WAIT_TIMEOUT,
    BG_JOB_LOG_DIR,
    BG_MAX_CONCURRENT,
    BG_MAX_WAIT_TIMEOUT,
    BG_RETENTION_SECONDS,
)
from .state import BgJob, ToolContext, bg_job_status as _bg_status
from .tool_utils import (
    _BASH_HARD_CHAR_CAP,
    _check_dangerous,
    _safe_path,
    _sandbox_error,
    _truncate_output,
)


def tool_bash(
    ctx: ToolContext,
    command: str,
    workdir: str | None = None,
    max_output_chars: int | None = None,
) -> str:
    """Run a shell command with safety checks, timeout, and output truncation."""
    err = _check_dangerous(command)
    if err:
        return f"Error: {err}"

    cap = BASH_OUTPUT_MAX_CHARS
    if max_output_chars is not None:
        try:
            cap = max(1, min(_BASH_HARD_CHAR_CAP, int(max_output_chars)))
        except (TypeError, ValueError):
            return f"Error: max_output_chars must be an integer, got {max_output_chars!r}"

    cwd_path = _safe_path(ctx.work_dir, workdir) if workdir else Path(ctx.work_dir).resolve()
    if not ALLOW_UNSANDBOXED_BASH:
        err = _sandbox_error(ctx.work_dir, cwd_path, kind="run bash in a workdir")
        if err:
            return err
    cwd = str(cwd_path)
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {BASH_TIMEOUT}s"

    parts = []
    if result.stdout:
        out = result.stdout
        if len(out) > cap:
            out = out[:cap] + (
                f"\n…[+{len(result.stdout) - cap} chars]"
            )
        parts.append(f"STDOUT:\n{out}")
    if result.stderr:
        err_out = result.stderr
        if len(err_out) > cap:
            err_out = err_out[:cap] + (
                f"\n…[+{len(result.stderr) - cap} chars]"
            )
        parts.append(f"STDERR:\n{err_out}")
    parts.append(f"Exit code: {result.returncode}")
    return "\n".join(parts)


def _bg_gc(jobs: dict[str, BgJob]) -> None:
    """Drop finished jobs older than BG_RETENTION_SECONDS so the dict
    doesn't grow unbounded across a long session."""
    now = time.monotonic()
    stale = []
    for jid, j in jobs.items():
        status, _rc, _elapsed = _bg_status(j)
        if status == "exited" and j.finished_at is not None:
            if now - j.finished_at > BG_RETENTION_SECONDS:
                stale.append(jid)
    for jid in stale:
        del jobs[jid]


def _bg_tail(log_path: Path, n: int) -> str:
    """Return the last *n* lines of *log_path* as a string. Robust to
    missing/empty files."""
    if n <= 0:
        return ""
    try:
        # Read full file — log files are typically small. If they get
        # huge, Popen's pipe-to-file means we still don't pay memory
        # until we read here, and the model usually only asks for tens
        # of lines so the truncation cost is minor.
        data = log_path.read_text(errors="replace")
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"<error reading log: {e}>"
    lines = data.splitlines()
    if len(lines) <= n:
        return data
    return "…[+{n_omitted} earlier line(s)]\n".format(
        n_omitted=len(lines) - n
    ) + "\n".join(lines[-n:])


def _bg_count_running(jobs: dict[str, BgJob]) -> int:
    return sum(1 for j in jobs.values() if j.proc.poll() is None)


def tool_bash_start(ctx: ToolContext, command: str, workdir: str | None = None) -> str:
    """Launch *command* in the background and return its job id."""
    err = _check_dangerous(command)
    if err:
        return f"Error: {err}"
    _bg_gc(ctx.bash_jobs)
    if _bg_count_running(ctx.bash_jobs) >= BG_MAX_CONCURRENT:
        running = [
            f"{j.id}: {j.command[:40]}"
            for j in ctx.bash_jobs.values()
            if j.proc.poll() is None
        ]
        return (
            f"Error: at most {BG_MAX_CONCURRENT} background jobs may run "
            f"simultaneously. Currently running:\n  "
            + "\n  ".join(running)
            + "\nWait for one to finish or bash_kill it first."
        )

    cwd_path = _safe_path(ctx.work_dir, workdir) if workdir else Path(ctx.work_dir).resolve()
    if not ALLOW_UNSANDBOXED_BASH:
        err = _sandbox_error(ctx.work_dir, cwd_path, kind="start bash in a workdir")
        if err:
            return err
    cwd = str(cwd_path)
    job_id = ctx.alloc_bash_id()

    BG_JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = BG_JOB_LOG_DIR / f"ddtui-{job_id}.out"
    try:
        log_fh = open(log_path, "wb")
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
        )
        # Popen dups the fd so we can close ours; the child keeps its own.
        log_fh.close()
    except Exception as e:
        return f"Error starting background job: {e}"

    job = BgJob(
        id=job_id,
        command=command,
        workdir=cwd,
        log_path=log_path,
        proc=proc,
        started_at=time.monotonic(),
    )
    ctx.bash_jobs[job_id] = job
    return (
        f"Started job {job_id} (pid {proc.pid}): {command}\n"
        f"Log: {log_path}\n"
        f"Use bash_check(\"{job_id}\") to peek, bash_wait(\"{job_id}\") "
        f"to block until it finishes."
    )


def _format_status(job: BgJob, tail_lines: int) -> str:
    status, rc, elapsed = _bg_status(job)
    head = (
        f"Job {job.id}: {status}"
        + (f" (exit {rc})" if status == "exited" else "")
        + f" · runtime {elapsed:.1f}s · pid {job.proc.pid}\n"
        f"Command: {job.command}\n"
        f"Log: {job.log_path}"
    )
    tail = _bg_tail(job.log_path, tail_lines)
    if not tail:
        return head + "\n(no output yet)"
    return head + "\n--- last "f"{tail_lines} lines ---\n" + tail


def tool_bash_check(
    ctx: ToolContext,
    job_id: str,
    tail_lines: int = BG_DEFAULT_TAIL_LINES,
    max_output_chars: int | None = None,
) -> str:
    job = ctx.bash_jobs.get(job_id)
    if job is None:
        return f"Error: unknown job_id {job_id!r}. Use bash_list to see jobs."
    try:
        tail_lines = max(0, min(500, int(tail_lines)))
    except (TypeError, ValueError):
        tail_lines = BG_DEFAULT_TAIL_LINES
    output = _format_status(job, tail_lines)
    return _truncate_output(output, max_output_chars)


def tool_bash_wait(
    ctx: ToolContext,
    job_id: str,
    timeout: int = BG_DEFAULT_WAIT_TIMEOUT,
    tail_lines: int = BG_DEFAULT_TAIL_LINES,
    max_output_chars: int | None = None,
) -> str:
    job = ctx.bash_jobs.get(job_id)
    if job is None:
        return f"Error: unknown job_id {job_id!r}."
    try:
        timeout = max(1, min(BG_MAX_WAIT_TIMEOUT, int(timeout)))
    except (TypeError, ValueError):
        timeout = BG_DEFAULT_WAIT_TIMEOUT
    try:
        tail_lines = max(0, min(500, int(tail_lines)))
    except (TypeError, ValueError):
        tail_lines = BG_DEFAULT_TAIL_LINES

    deadline = time.monotonic() + timeout
    while True:
        if job.proc.poll() is not None:
            break
        if time.monotonic() >= deadline:
            output = _format_status(job, tail_lines)
            return (
                f"bash_wait timed out after {timeout}s; job {job_id} is "
                f"still running.\n"
                + _truncate_output(output, max_output_chars)
            )
        # 200ms strikes a balance: short enough that the model gets a
        # near-instant return on quick jobs, long enough that polling
        # overhead stays trivial.
        time.sleep(0.2)
    output = _format_status(job, tail_lines)
    return _truncate_output(output, max_output_chars)


def tool_bash_kill(ctx: ToolContext, job_id: str, force: bool = False) -> str:
    job = ctx.bash_jobs.get(job_id)
    if job is None:
        return f"Error: unknown job_id {job_id!r}."
    if job.proc.poll() is not None:
        return f"Job {job_id} already exited (code {job.proc.returncode})."
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        # We started_new_session, so signal the whole process group to
        # take down children too.
        os.killpg(os.getpgid(job.proc.pid), sig)
    except ProcessLookupError:
        return f"Job {job_id} already gone."
    except Exception as e:
        return f"Error sending signal: {e}"
    # Give it a moment to actually die so the next bash_list sees it gone.
    for _ in range(10):
        if job.proc.poll() is not None:
            break
        time.sleep(0.05)
    status, rc, _ = _bg_status(job)
    return (
        f"Sent {'SIGKILL' if force else 'SIGTERM'} to {job_id}; "
        f"now {status}"
        + (f" (exit {rc})" if status == "exited" else "")
        + "."
    )


def tool_bash_list(ctx: ToolContext) -> str:
    _bg_gc(ctx.bash_jobs)
    if not ctx.bash_jobs:
        return "No background jobs."
    lines = ["Background jobs:"]
    for j in ctx.bash_jobs.values():
        status, rc, elapsed = _bg_status(j)
        marker = "running" if status == "running" else f"exit {rc}"
        cmd = j.command if len(j.command) <= 60 else j.command[:57] + "..."
        lines.append(
            f"  {j.id}  [{marker}]  {elapsed:.1f}s  {cmd}"
        )
    return "\n".join(lines)
