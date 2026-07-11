"""Managed asynchronous task tool implementations."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .config import (
    ALLOW_UNSANDBOXED_BASH,
    TASK_DEFAULT_TAIL_LINES,
    TASK_DEFAULT_NOTICE_TIME,
    TASK_DEFAULT_WAIT_TIMEOUT,
    TASK_EVENT_MAX_CHARS,
    TASK_EVENT_TAIL_LINES,
    TASK_MAX_CONCURRENT,
    TASK_MAX_NOTICE_TIME,
    TASK_MAX_WAIT_TIMEOUT,
    TASK_MIN_NOTICE_TIME,
    TASK_OUTPUT_DIR,
    TASK_READ_DEFAULT_CHARS,
    TASK_READ_MAX_CHARS,
    TASK_RETENTION_SECONDS,
)
from .runtime_state import (
    atomic_write_json,
    clear_path,
    read_json,
    task_registry_path,
    task_spec_path,
    tasks_dir,
)
from .state import (
    AsyncTask,
    RecoveredProcess,
    ToolContext,
    async_task_status as _task_status,
)
from .tools_checkpoint import prune_checkpoint_refs
from .tools_experiments import snapshot_artifacts, validate_experiment_snapshot
from .tool_utils import (
    _check_dangerous,
    _safe_path,
    _sandbox_error,
    _truncate_output,
    ensure_private_dir,
)


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _clamp_notice_time(value) -> float | None:
    """Normalize task_start.notice_time.

    None/0 disables running notices; positive values are clamped so a
    typo cannot create a sub-second wake loop or a multi-day silence.
    """
    if value is None:
        return float(TASK_DEFAULT_NOTICE_TIME)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return float(TASK_DEFAULT_NOTICE_TIME)
    if seconds <= 0:
        return None
    return float(max(TASK_MIN_NOTICE_TIME, min(TASK_MAX_NOTICE_TIME, seconds)))


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


def _ts_from_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _task_registry_payload(task: AsyncTask) -> dict:
    status, rc, elapsed = _task_status(task)
    return {
        "version": 1,
        "session_id": task.session_id,
        "task_id": task.id,
        "name": task.name,
        "command": task.command,
        "workdir": task.workdir,
        "output_path": str(task.output_path),
        "status_path": str(task.status_path),
        "registry_path": str(task.registry_path) if task.registry_path else None,
        "runner_pid": task.runner_pid or getattr(task.proc, "pid", None),
        "pid": task.runner_pid or getattr(task.proc, "pid", None),
        "started_wall": task.started_wall,
        "started_at": _iso_from_ts(task.started_wall),
        "finished_at": _iso_from_ts(task.finished_wall),
        "duration_sec": round(elapsed, 3),
        "status": status,
        "return_code": rc,
        "notify_on_complete": task.notify_on_complete,
        "notice_time": task.notice_time,
        "notice_count": task.notice_count,
        "artifact_hashes": task.artifact_hashes,
        "experiment_id": task.experiment_id,
        "next_notice_at": _iso_from_ts(
            time.time() + (task.next_notice_at - time.monotonic())
            if task.next_notice_at is not None else None
        ),
        "notified": task.notified,
        "killed": task.killed,
    }


def _write_registry(task: AsyncTask) -> None:
    if task.registry_path is None:
        return
    atomic_write_json(task.registry_path, _task_registry_payload(task))


def _write_status(task: AsyncTask) -> None:
    status, rc, elapsed = _task_status(task)
    payload = {
        "task_id": task.id,
        "name": task.name,
        "status": status,
        "return_code": rc,
        "pid": task.proc.pid,
        "runner_pid": task.runner_pid or getattr(task.proc, "pid", None),
        "session_id": task.session_id,
        "command": task.command,
        "workdir": task.workdir,
        "started_at": _iso_from_ts(task.started_wall),
        "finished_at": _iso_from_ts(task.finished_wall),
        "duration_sec": round(elapsed, 3),
        "output_path": str(task.output_path),
        "status_path": str(task.status_path),
        "notify_on_complete": task.notify_on_complete,
        "notice_time": task.notice_time,
        "notice_count": task.notice_count,
        "artifact_hashes": task.artifact_hashes,
        "experiment_id": task.experiment_id,
    }
    tmp = task.status_path.with_name(
        f".{task.status_path.name}.{os.getpid()}.tmp"
    )
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(task.status_path)
    _write_registry(task)


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
        paths = [task.output_path, task.status_path]
        if task.registry_path is not None:
            paths.append(task.registry_path)
            paths.append(task.registry_path.with_suffix(".spec.json"))
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass


def recover_tasks_for_session(ctx: ToolContext, session_id: str) -> list[AsyncTask]:
    """Load persisted task registry entries for *session_id* into ctx."""
    ctx.tasks.clear()
    max_seen = 0
    recovered: list[AsyncTask] = []
    task_dir = tasks_dir(session_id)
    if not task_dir.exists():
        return recovered
    for path in sorted(task_dir.glob("task-*.json")):
        if path.name.endswith(".spec.json"):
            continue
        payload = read_json(path)
        if not payload:
            continue
        task_id = str(payload.get("task_id") or path.stem)
        try:
            idx = int(task_id.rsplit("-", 1)[-1])
            max_seen = max(max_seen, idx)
        except Exception:
            pass
        status_path = Path(str(payload.get("status_path") or ""))
        output_path = Path(str(payload.get("output_path") or ""))
        runner_pid = payload.get("runner_pid") or payload.get("pid") or 0
        try:
            runner_pid = int(runner_pid)
        except Exception:
            runner_pid = 0
        started_wall = payload.get("started_wall")
        if not isinstance(started_wall, (int, float)):
            started_wall = (
                _ts_from_iso(str(payload.get("started_at") or ""))
                or time.time()
            )
        started_at = time.monotonic() - max(
            0.0, time.time() - float(started_wall)
        )
        proc = RecoveredProcess(pid=runner_pid, status_path=status_path)
        task = AsyncTask(
            id=task_id,
            name=str(payload.get("name") or task_id),
            command=str(payload.get("command") or ""),
            workdir=str(payload.get("workdir") or ctx.work_dir),
            output_path=output_path,
            status_path=status_path,
            proc=proc,
            started_at=started_at,
            started_wall=float(started_wall),
            notify_on_complete=bool(payload.get("notify_on_complete", True)),
            notice_time=_clamp_notice_time(payload.get("notice_time")),
            notice_count=_clamp_int(
                payload.get("notice_count"), 0, 0, 10**9
            ),
            artifact_hashes={
                str(key): str(value)
                for key, value in (payload.get("artifact_hashes") or {}).items()
            },
            experiment_id=(
                str(payload.get("experiment_id"))
                if payload.get("experiment_id")
                else None
            ),
            session_id=session_id,
            registry_path=path,
            runner_pid=runner_pid,
            recovered=True,
            notified=bool(payload.get("notified", False)),
            killed=bool(payload.get("killed", False)),
        )
        if task.notice_time:
            task.next_notice_at = task.started_at + (
                task.notice_time * (task.notice_count + 1)
            )
        ctx.tasks[task_id] = task
        recovered.append(task)
    if max_seen >= ctx.task_next_id:
        ctx.task_next_id = max_seen + 1
    _task_gc(ctx.tasks)
    return recovered


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
        f"notify_on_complete: {task.notify_on_complete}\n"
        f"notice_time: {task.notice_time if task.notice_time else 'disabled'}"
    )
    if task.experiment_id:
        head += f"\nexperiment_id: {task.experiment_id}"
    for path, sha in task.artifact_hashes.items():
        head += f"\nartifact_sha256_at_start: {sha}  {path}"
    if status == "running" and task.notify_on_complete:
        if task.check_count >= 2:
            head += (
                f"\nwaiting_rule: you have now checked this RUNNING task "
                f"{task.check_count} times — this IS the polling loop you "
                "were told to avoid. STOP. Ending your current response "
                "is NOT abandoning the task: the completion notification "
                "automatically starts a new turn where you pick up the "
                "remaining steps. Record a checkpoint if helpful, then "
                "END your response and let the notification wake you."
            )
        else:
            head += (
                "\nwaiting_rule: do not poll task_check/task_read just to wait. "
                "Work on other useful tasks in parallel, or if nothing useful "
                "remains, pause the session and wait for the completion "
                "notification."
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
    evidence = ""
    if task.experiment_id:
        evidence += f"experiment_id: {task.experiment_id}\n"
    for path, sha in task.artifact_hashes.items():
        evidence += f"artifact_sha256_at_start: {sha}  {path}\n"
    return (
        "[Async task complete]\n"
        f"task_id: {task.id}\n"
        f"name: {task.name}\n"
        f"status: {status}\n"
        f"return_code: {rc}\n"
        f"duration: {elapsed:.1f}s\n"
        f"output_path: {task.output_path}\n"
        f"status_path: {task.status_path}\n"
        f"{evidence}"
        f"\nlast {TASK_EVENT_TAIL_LINES} lines:\n{tail}"
    )


def _notice_event(task: AsyncTask) -> str:
    status, rc, elapsed = _task_status(task)
    _write_status(task)
    tail = _tail(task.output_path, TASK_EVENT_TAIL_LINES)
    if tail:
        tail = _truncate_output(tail, TASK_EVENT_MAX_CHARS)
    else:
        tail = "(no output)"
    evidence = ""
    if task.experiment_id:
        evidence += f"experiment_id: {task.experiment_id}\n"
    for path, sha in task.artifact_hashes.items():
        evidence += f"artifact_sha256_at_start: {sha}  {path}\n"
    return (
        "[Async task notice]\n"
        f"task_id: {task.id}\n"
        f"name: {task.name}\n"
        f"status: {status}\n"
        f"return_code: {rc}\n"
        f"runtime: {elapsed:.1f}s\n"
        f"notice_count: {task.notice_count}\n"
        f"output_path: {task.output_path}\n"
        f"status_path: {task.status_path}\n"
        f"{evidence}"
        f"\nlast {TASK_EVENT_TAIL_LINES} lines:\n{tail}"
    )


def collect_task_events(ctx: ToolContext) -> list[str]:
    """Return running notices and completion events for notified tasks."""
    events: list[str] = []
    now = time.monotonic()
    for task in list(ctx.tasks.values()):
        status, _rc, _elapsed = _task_status(task)
        if not task.notify_on_complete or task.notified:
            continue
        if status == "running":
            if not task.notice_time:
                continue
            if task.next_notice_at is None:
                task.next_notice_at = task.started_at + task.notice_time
            if now < task.next_notice_at:
                continue
            task.notice_count += 1
            task.next_notice_at = now + task.notice_time
            events.append(_notice_event(task))
            _write_registry(task)
            continue
        task.notified = True
        events.append(_completion_event(task))
        prune_checkpoint_refs(ctx, [task.id])
        _write_registry(task)
    _task_gc(ctx.tasks)
    return events


def collect_task_completion_events(ctx: ToolContext) -> list[str]:
    """Compatibility wrapper for older app code/tests."""
    return collect_task_events(ctx)


def tool_task_start(
    ctx: ToolContext,
    command: str,
    workdir: str | None = None,
    name: str | None = None,
    description: str | None = None,
    notify_on_complete: bool = True,
    notice_time: float | int | None = None,
    artifacts: list[str] | None = None,
    experiment_id: str | None = None,
) -> str:
    """Start a managed asynchronous shell task."""
    err = _check_dangerous(command)
    if err:
        return f"Error: {err}"
    notice_seconds = _clamp_notice_time(notice_time)
    if not notify_on_complete:
        notice_seconds = None
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

    cwd_path = (
        _safe_path(ctx.work_dir, workdir)
        if workdir
        else Path(ctx.work_dir).resolve()
    )
    if not ALLOW_UNSANDBOXED_BASH:
        err = _sandbox_error(ctx.work_dir, cwd_path, kind="start task in a workdir")
        if err:
            return err
    cwd = str(cwd_path)

    artifact_hashes, artifact_error = snapshot_artifacts(ctx, artifacts)
    if artifact_error:
        return artifact_error
    experiment_error = validate_experiment_snapshot(
        ctx, experiment_id, artifact_hashes
    )
    if experiment_error:
        return experiment_error

    task_id = ctx.alloc_task_id()
    task_name = (name or description or _short_command(command)).strip()
    if not task_name:
        task_name = task_id
    task_name = task_name[:80]

    ensure_private_dir(TASK_OUTPUT_DIR)
    file_token = f"{task_id}-{os.getpid()}-{int(time.time() * 1000)}"
    output_path = TASK_OUTPUT_DIR / f"ddtui-{file_token}.out"
    status_path = TASK_OUTPUT_DIR / f"ddtui-{file_token}.status.json"
    session_id = ctx.session_id or "default"
    registry_path = task_registry_path(session_id, task_id)
    spec_path = task_spec_path(session_id, task_id)
    started_wall = time.time()
    spec = {
        "version": 1,
        "session_id": session_id,
        "task_id": task_id,
        "name": task_name,
        "command": command,
        "workdir": cwd,
        "output_path": str(output_path),
        "status_path": str(status_path),
        "notify_on_complete": bool(notify_on_complete),
        "notice_time": notice_seconds,
        "artifact_hashes": artifact_hashes,
        "experiment_id": experiment_id,
        "started_wall": started_wall,
    }
    atomic_write_json(spec_path, spec)
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).with_name("task_runner.py")),
                str(spec_path),
            ],
            # DEVNULL stdin is load-bearing: without it the runner (and
            # the user's command under it) inherits the TUI's tty and
            # any stdin read steals keystrokes from Textual's input
            # loop — start_new_session puts it in another session, so
            # SIGTTIN never blocks those reads.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        clear_path(spec_path)
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
        started_wall=started_wall,
        notify_on_complete=bool(notify_on_complete),
        notice_time=notice_seconds,
        artifact_hashes=artifact_hashes,
        experiment_id=experiment_id,
        next_notice_at=(
            time.monotonic() + notice_seconds
            if notice_seconds is not None else None
        ),
        session_id=session_id,
        registry_path=registry_path,
        runner_pid=proc.pid,
    )
    ctx.tasks[task_id] = task
    _write_registry(task)
    _write_status(task)

    if task.notify_on_complete:
        if ctx.is_subagent:
            wait_guidance = (
                "SUBAGENT WAITING RULE: do NOT block with task_wait. "
                "Continue useful work in this round; if no useful work "
                "remains, call task_pause(task_ids=["
                f"'{task_id}'"
                "]) to park. The runtime will inject [Async task notice] "
                "while it is still running and [Async task complete] when "
                "it exits, then wake you to inspect output with "
                "task_check/task_read and continue."
            )
        else:
            wait_guidance = (
                "WAITING RULE: notify_on_complete=True, so do NOT call "
                "task_wait, repeated task_check, or repeated task_read just to "
                "wait for this task. You can work on other useful tasks in "
                "parallel while this task runs. If no useful work remains, "
                "update checkpoint_tool with "
                f"status='waiting' and active_refs=['{task_id}'] if helpful, "
                "then pause the session by finishing the current response and "
                "wait for the runtime task notification. Do not call "
                "task_check as a polling loop. Use task_check/task_read only "
                "for a one-off inspection when the current output changes what "
                "you should do next."
            )
    else:
        wait_guidance = (
            "No completion notification will be sent because "
            "notify_on_complete=False. Use task_check/task_read to inspect "
            "progress; start a new notified task instead of blocking if "
            "completion matters."
        )
    evidence = ""
    if task.experiment_id:
        evidence += f"experiment_id: {task.experiment_id}\n"
    evidence += "".join(
        f"artifact_sha256_at_start: {sha}  {path}\n"
        for path, sha in task.artifact_hashes.items()
    )
    return (
        f"Started managed task {task_id}: {task.name}\n"
        "status: running\n"
        f"pid: {proc.pid}\n"
        f"workdir: {cwd}\n"
        f"output_path: {output_path}\n"
        f"status_path: {status_path}\n"
        f"notify_on_complete: {task.notify_on_complete}\n"
        f"notice_time: {task.notice_time if task.notice_time else 'disabled'}\n"
        f"{evidence}"
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
    status, _rc, _elapsed = _task_status(task)
    if status == "running" and task.notify_on_complete:
        task.check_count += 1
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
    warn = ""
    if status == "running" and task.notify_on_complete:
        task.check_count += 1
        if task.check_count >= 2:
            warn = (
                f"waiting_rule: you have now inspected this RUNNING task "
                f"{task.check_count} times — this IS the polling loop you "
                "were told to avoid. STOP. Ending your current response is "
                "NOT abandoning the task: the completion notification "
                "automatically starts a new turn where you continue. END "
                "your response and let the notification wake you.\n"
            )
    return (
        f"Task {task.id}: {status}"
        + (f" (return_code {rc})" if rc is not None else "")
        + f" · runtime {elapsed:.1f}s\n"
        f"output_path: {task.output_path}\n"
        f"offset: {offset}\n"
        f"next_offset: {next_offset}\n"
        f"bytes_read: {len(chunk)}\n"
        + warn
        + "--- content ---\n"
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
    tail_lines = _clamp_int(tail_lines, TASK_DEFAULT_TAIL_LINES, 0, 500)
    output = _format_task(task, tail_lines)
    return _truncate_output(
        "Error: task_wait is deprecated and no longer blocks. Use "
        "task_check/task_read for a non-blocking inspection. Parent agents "
        "should let task_start notifications wake the next turn; subagents "
        "should call task_pause to enter the waiting phase.\n"
        + output,
        max_output_chars,
    )


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
