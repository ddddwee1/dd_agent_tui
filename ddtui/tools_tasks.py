"""Managed asynchronous task tool implementations."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import (
    ALLOW_UNSANDBOXED_BASH,
    TASK_DEFAULT_TAIL_LINES,
    TASK_DEFAULT_WAIT_TIMEOUT,
    TASK_EVENT_MAX_CHARS,
    TASK_EVENT_TAIL_LINES,
    TASK_MAX_CONCURRENT,
    TASK_MAX_WAIT_TIMEOUT,
    TASK_OUTPUT_DIR,
    TASK_READ_DEFAULT_CHARS,
    TASK_READ_MAX_CHARS,
    TASK_RETENTION_SECONDS,
)
from .state import AsyncTask, ToolContext, async_task_status as _task_status
from .tool_utils import (
    _check_dangerous,
    _safe_path,
    _sandbox_error,
    _truncate_output,
)


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _short_command(command: str, limit: int = 48) -> str:
    one_line = " ".join(command.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3] + "..."


def _task_count_running(tasks: dict[str, AsyncTask]) -> int:
    return sum(1 for t in tasks.values() if t.proc.poll() is None)


def _tail(path: Path, n: int) -> str:
    if n <= 0:
        return ""
    try:
        data = path.read_text(errors="replace")
    except FileNotFoundError:
        return ""
    except Exception as e:
        return f"<error reading output: {e}>"
    lines = data.splitlines()
    if len(lines) <= n:
        return data
    omitted = len(lines) - n
    return f"...[+{omitted} earlier line(s)]\n" + "\n".join(lines[-n:])


def _iso_from_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _write_status(task: AsyncTask) -> None:
    status, rc, elapsed = _task_status(task)
    payload = {
        "task_id": task.id,
        "name": task.name,
        "status": status,
        "return_code": rc,
        "pid": task.proc.pid,
        "command": task.command,
        "workdir": task.workdir,
        "started_at": _iso_from_ts(task.started_wall),
        "finished_at": _iso_from_ts(task.finished_wall),
        "duration_sec": round(elapsed, 3),
        "output_path": str(task.output_path),
        "notify_on_complete": task.notify_on_complete,
    }
    tmp = task.status_path.with_name(
        f".{task.status_path.name}.{os.getpid()}.tmp"
    )
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(task.status_path)


def _task_gc(tasks: dict[str, AsyncTask]) -> None:
    now = time.monotonic()
    stale: list[str] = []
    for tid, task in tasks.items():
        status, _rc, _elapsed = _task_status(task)
        if status == "running":
            continue
        if task.notify_on_complete and not task.notified:
            continue
        if task.finished_at is not None and now - task.finished_at > TASK_RETENTION_SECONDS:
            stale.append(tid)
    for tid in stale:
        task = tasks.pop(tid)
        for path in (task.output_path, task.status_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass


def _format_task(task: AsyncTask, tail_lines: int) -> str:
    status, rc, elapsed = _task_status(task)
    _write_status(task)
    head = (
        f"Task {task.id}: {status}"
        + (f" (return_code {rc})" if rc is not None else "")
        + f" · runtime {elapsed:.1f}s · pid {task.proc.pid}\n"
        f"Name: {task.name}\n"
        f"Command: {task.command}\n"
        f"Workdir: {task.workdir}\n"
        f"Output: {task.output_path}\n"
        f"Status: {task.status_path}\n"
        f"notify_on_complete: {task.notify_on_complete}"
    )
    tail = _tail(task.output_path, tail_lines)
    if not tail:
        return head + "\n(no output yet)"
    return head + f"\n--- last {tail_lines} lines ---\n" + tail


def _completion_event(task: AsyncTask) -> str:
    status, rc, elapsed = _task_status(task)
    _write_status(task)
    tail = _tail(task.output_path, TASK_EVENT_TAIL_LINES)
    if tail:
        tail = _truncate_output(tail, TASK_EVENT_MAX_CHARS)
    else:
        tail = "(no output)"
    return (
        "[异步任务完成]\n"
        f"task_id: {task.id}\n"
        f"name: {task.name}\n"
        f"status: {status}\n"
        f"return_code: {rc}\n"
        f"duration: {elapsed:.1f}s\n"
        f"output_path: {task.output_path}\n"
        f"status_path: {task.status_path}\n"
        f"\nlast {TASK_EVENT_TAIL_LINES} lines:\n{tail}"
    )


def collect_task_completion_events(ctx: ToolContext) -> list[str]:
    """Return completion events for notified tasks that just finished."""
    events: list[str] = []
    for task in list(ctx.tasks.values()):
        status, _rc, _elapsed = _task_status(task)
        if status == "running":
            continue
        if not task.notify_on_complete or task.notified:
            continue
        task.notified = True
        events.append(_completion_event(task))
    _task_gc(ctx.tasks)
    return events


def tool_task_start(
    ctx: ToolContext,
    command: str,
    workdir: str | None = None,
    name: str | None = None,
    description: str | None = None,
    notify_on_complete: bool = True,
) -> str:
    """Start a managed asynchronous shell task."""
    err = _check_dangerous(command)
    if err:
        return f"Error: {err}"
    _task_gc(ctx.tasks)
    if _task_count_running(ctx.tasks) >= TASK_MAX_CONCURRENT:
        running = [
            f"{t.id}: {t.name} ({_short_command(t.command, 36)})"
            for t in ctx.tasks.values()
            if t.proc.poll() is None
        ]
        return (
            f"Error: at most {TASK_MAX_CONCURRENT} managed tasks may run "
            f"simultaneously. Currently running:\n  "
            + "\n  ".join(running)
            + "\nWait for one to finish or task_kill it first."
        )

    cwd_path = _safe_path(ctx.work_dir, workdir) if workdir else Path(ctx.work_dir).resolve()
    if not ALLOW_UNSANDBOXED_BASH:
        err = _sandbox_error(ctx.work_dir, cwd_path, kind="start task in a workdir")
        if err:
            return err
    cwd = str(cwd_path)

    task_id = ctx.alloc_task_id()
    task_name = (name or description or _short_command(command)).strip()
    if not task_name:
        task_name = task_id
    task_name = task_name[:80]

    TASK_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    file_token = f"{task_id}-{os.getpid()}-{int(time.time() * 1000)}"
    output_path = TASK_OUTPUT_DIR / f"ddtui-{file_token}.out"
    status_path = TASK_OUTPUT_DIR / f"ddtui-{file_token}.status.json"
    try:
        log_fh = open(output_path, "wb")
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            start_new_session=True,
        )
        log_fh.close()
    except Exception as e:
        return f"Error starting task: {e}"

    task = AsyncTask(
        id=task_id,
        name=task_name,
        command=command,
        workdir=cwd,
        output_path=output_path,
        status_path=status_path,
        proc=proc,
        started_at=time.monotonic(),
        started_wall=time.time(),
        notify_on_complete=bool(notify_on_complete),
    )
    ctx.tasks[task_id] = task
    _write_status(task)

    if task.notify_on_complete:
        wait_guidance = (
            "WAITING RULE: notify_on_complete=True, so do NOT call "
            "task_wait just to wait for this task. Continue other useful "
            "work; if no useful work remains, update checkpoint_tool with "
            f"status='waiting' and active_refs=['{task_id}'] if helpful, "
            "then finish/pause the current response and wait for the runtime "
            "completion notification. Use task_wait only when the user "
            "explicitly requested blocking, or for one short bounded wait "
            "for an almost-finished task."
        )
    else:
        wait_guidance = (
            "No completion notification will be sent because "
            "notify_on_complete=False. Use task_check/task_read to inspect "
            "progress, or task_wait when you intentionally want to block."
        )
    return (
        f"Started managed task {task_id}: {task.name}\n"
        "status: running\n"
        f"pid: {proc.pid}\n"
        f"workdir: {cwd}\n"
        f"output_path: {output_path}\n"
        f"status_path: {status_path}\n"
        f"notify_on_complete: {task.notify_on_complete}\n"
        f"{wait_guidance}\n"
        f"Use task_check(\"{task_id}\") to inspect progress, "
        f"or task_read(\"{task_id}\", offset=0) for incremental output."
    )


def tool_task_check(
    ctx: ToolContext,
    task_id: str,
    tail_lines: int = TASK_DEFAULT_TAIL_LINES,
    max_output_chars: int | None = None,
) -> str:
    task = ctx.tasks.get(task_id)
    if task is None:
        return f"Error: unknown task_id {task_id!r}. Use task_list to see tasks."
    tail_lines = _clamp_int(tail_lines, TASK_DEFAULT_TAIL_LINES, 0, 500)
    return _truncate_output(_format_task(task, tail_lines), max_output_chars)


def tool_task_read(
    ctx: ToolContext,
    task_id: str,
    offset: int = 0,
    max_chars: int = TASK_READ_DEFAULT_CHARS,
) -> str:
    task = ctx.tasks.get(task_id)
    if task is None:
        return f"Error: unknown task_id {task_id!r}. Use task_list to see tasks."
    offset = _clamp_int(offset, 0, 0, 10**18)
    max_chars = _clamp_int(
        max_chars, TASK_READ_DEFAULT_CHARS, 1, TASK_READ_MAX_CHARS
    )
    try:
        size = task.output_path.stat().st_size
    except FileNotFoundError:
        size = 0
    offset = min(offset, size)
    try:
        with open(task.output_path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(max_chars)
            next_offset = fh.tell()
    except Exception as e:
        return f"Error reading task output: {e}"
    status, rc, elapsed = _task_status(task)
    _write_status(task)
    content = chunk.decode("utf-8", errors="replace")
    return (
        f"Task {task.id}: {status}"
        + (f" (return_code {rc})" if rc is not None else "")
        + f" · runtime {elapsed:.1f}s\n"
        f"output_path: {task.output_path}\n"
        f"offset: {offset}\n"
        f"next_offset: {next_offset}\n"
        f"bytes_read: {len(chunk)}\n"
        "--- content ---\n"
        f"{content}"
    )


def tool_task_wait(
    ctx: ToolContext,
    task_id: str,
    timeout: int = TASK_DEFAULT_WAIT_TIMEOUT,
    tail_lines: int = TASK_DEFAULT_TAIL_LINES,
    max_output_chars: int | None = None,
) -> str:
    task = ctx.tasks.get(task_id)
    if task is None:
        return f"Error: unknown task_id {task_id!r}."
    timeout = _clamp_int(
        timeout, TASK_DEFAULT_WAIT_TIMEOUT, 1, TASK_MAX_WAIT_TIMEOUT
    )
    tail_lines = _clamp_int(tail_lines, TASK_DEFAULT_TAIL_LINES, 0, 500)

    deadline = time.monotonic() + timeout
    while True:
        status, _rc, _elapsed = _task_status(task)
        if status != "running":
            break
        if time.monotonic() >= deadline:
            output = _format_task(task, tail_lines)
            return (
                f"task_wait timed out after {timeout}s; task {task_id} "
                "is still running. If notify_on_complete=true, do not call "
                "task_wait again just because you are waiting. Continue other "
                "useful work; if none remains, finish/pause the current "
                "response and wait for the completion notification unless "
                "the user explicitly requested blocking.\n"
                + _truncate_output(output, max_output_chars)
            )
        time.sleep(0.2)
    return _truncate_output(_format_task(task, tail_lines), max_output_chars)


def tool_task_kill(ctx: ToolContext, task_id: str, force: bool = False) -> str:
    task = ctx.tasks.get(task_id)
    if task is None:
        return f"Error: unknown task_id {task_id!r}."
    status, rc, _elapsed = _task_status(task)
    if status != "running":
        return f"Task {task_id} already finished as {status} (return_code {rc})."
    sig = signal.SIGKILL if force else signal.SIGTERM
    task.killed = True
    try:
        os.killpg(os.getpgid(task.proc.pid), sig)
    except ProcessLookupError:
        return f"Task {task_id} already gone."
    except Exception as e:
        return f"Error sending signal: {e}"
    for _ in range(10):
        if task.proc.poll() is not None:
            break
        time.sleep(0.05)
    status, rc, _elapsed = _task_status(task)
    _write_status(task)
    return (
        f"Sent {'SIGKILL' if force else 'SIGTERM'} to {task_id}; "
        f"now {status}"
        + (f" (return_code {rc})" if rc is not None else "")
        + "."
    )


def tool_task_list(ctx: ToolContext, include_finished: bool = True) -> str:
    _task_gc(ctx.tasks)
    tasks = list(ctx.tasks.values())
    if not include_finished:
        tasks = [t for t in tasks if _task_status(t)[0] == "running"]
    if not tasks:
        return "No managed tasks."
    lines = ["Managed tasks:"]
    for task in tasks:
        status, rc, elapsed = _task_status(task)
        marker = "running" if status == "running" else f"{status} {rc}"
        cmd = _short_command(task.command, 60)
        lines.append(
            f"  {task.id}  [{marker}]  {elapsed:.1f}s  "
            f"{task.name}  ·  {cmd}\n"
            f"      output: {task.output_path}"
        )
        _write_status(task)
    return "\n".join(lines)
