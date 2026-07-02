"""Interactive PTY-backed terminal tools."""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import select
import signal
import subprocess
import termios
import time
from pathlib import Path

from .config import (
    ALLOW_UNSANDBOXED_BASH,
    TERMINAL_DEFAULT_READ_CHARS,
    TERMINAL_MAX_CONCURRENT,
    TERMINAL_MAX_READ_CHARS,
    TERMINAL_OUTPUT_DIR,
    TERMINAL_TAIL_CHARS,
)
from .state import TerminalSession, ToolContext
from .tool_utils import (
    _check_dangerous,
    _safe_path,
    _sandbox_error,
    ensure_private_dir,
)


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _short(text: str, limit: int = 48) -> str:
    one_line = " ".join(str(text).split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3] + "..."


def _running(term: TerminalSession) -> bool:
    return term.proc.poll() is None and not term.closed


def _set_nonblocking(fd: int) -> None:
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def drain_terminal_output(term: TerminalSession) -> int:
    """Drain available PTY output into log_path and the UI tail."""
    if term.closed:
        return 0
    total = 0
    chunks: list[bytes] = []
    with term.lock:
        while True:
            try:
                ready, _w, _x = select.select([term.master_fd], [], [], 0)
            except (OSError, ValueError):
                term.closed = True
                break
            if not ready:
                break
            try:
                data = os.read(term.master_fd, 8192)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno == errno.EIO:
                    term.closed = True
                    break
                raise
            if not data:
                term.closed = True
                break
            chunks.append(data)
            total += len(data)
        if not chunks:
            if term.proc.poll() is not None and term.finished_at is None:
                term.finished_at = time.monotonic()
            return 0
        blob = b"".join(chunks)
        term.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(term.log_path, "ab") as fh:
            fh.write(blob)
        term.output_size += len(blob)
        text = blob.decode("utf-8", errors="replace")
        term.tail = (term.tail + text)[-TERMINAL_TAIL_CHARS:]
        if term.proc.poll() is not None and term.finished_at is None:
            term.finished_at = time.monotonic()
        return total


def _format_status(term: TerminalSession) -> tuple[str, int | None, float]:
    rc = term.proc.poll()
    if rc is None and not term.closed:
        return "running", None, time.monotonic() - term.started_at
    if term.finished_at is None:
        term.finished_at = time.monotonic()
    return "exited", rc, term.finished_at - term.started_at


def tool_terminal_start(
    ctx: ToolContext,
    command: str,
    workdir: str | None = None,
    name: str | None = None,
) -> str:
    """Start a long-lived interactive PTY session."""
    err = _check_dangerous(command)
    if err:
        return f"Error: {err}"
    running = [t for t in ctx.terminals.values() if _running(t)]
    if len(running) >= TERMINAL_MAX_CONCURRENT:
        lines = [f"{t.id}: {t.name} ({_short(t.command, 36)})" for t in running]
        return (
            f"Error: at most {TERMINAL_MAX_CONCURRENT} terminals may run "
            "simultaneously. Currently running:\n  "
            + "\n  ".join(lines)
        )

    cwd_path = (
        _safe_path(ctx.work_dir, workdir)
        if workdir
        else Path(ctx.work_dir).resolve()
    )
    if not ALLOW_UNSANDBOXED_BASH:
        err = _sandbox_error(ctx.work_dir, cwd_path, kind="start terminal in a workdir")
        if err:
            return err
    cwd = str(cwd_path)

    term_id = ctx.alloc_terminal_id()
    term_name = (name or _short(command)).strip() or term_id
    term_name = term_name[:80]
    ensure_private_dir(TERMINAL_OUTPUT_DIR)
    token = f"{term_id}-{os.getpid()}-{int(time.time() * 1000)}"
    log_path = TERMINAL_OUTPUT_DIR / f"ddtui-{token}.terminal.log"

    master_fd, slave_fd = pty.openpty()
    _set_nonblocking(master_fd)
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")

    def _setup_child() -> None:
        os.setsid()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            preexec_fn=_setup_child,
            close_fds=True,
        )
    except Exception as e:
        os.close(master_fd)
        os.close(slave_fd)
        return f"Error starting terminal: {e}"
    finally:
        try:
            os.close(slave_fd)
        except Exception:
            pass

    term = TerminalSession(
        id=term_id,
        name=term_name,
        command=command,
        workdir=cwd,
        master_fd=master_fd,
        log_path=log_path,
        proc=proc,
        started_at=time.monotonic(),
    )
    ctx.terminals[term_id] = term
    time.sleep(0.05)
    drain_terminal_output(term)
    return (
        f"Started terminal {term_id}: {term.name}\n"
        "status: running\n"
        f"pid: {proc.pid}\n"
        f"workdir: {cwd}\n"
        f"log_path: {log_path}\n"
        "Use terminal_send to type into this persistent PTY, and "
        "terminal_read to inspect output. For repeated remote SSH work, "
        "start one terminal with ssh/tmux and send commands into it instead "
        "of repeatedly running ssh commands. For non-interactive long jobs "
        "whose completion matters, prefer task_start."
    )


def tool_terminal_send(
    ctx: ToolContext,
    terminal_id: str,
    text: str,
    wait_ms: int = 200,
) -> str:
    term = ctx.terminals.get(terminal_id)
    if term is None:
        return f"Error: unknown terminal_id {terminal_id!r}. Use terminal_list."
    if not _running(term):
        status, rc, _elapsed = _format_status(term)
        return f"Error: terminal {terminal_id} is {status} (return_code {rc})."
    data = str(text).encode("utf-8", errors="replace")
    with term.lock:
        try:
            os.write(term.master_fd, data)
        except OSError as e:
            return f"Error writing to terminal {terminal_id}: {e}"
    wait_ms = _clamp_int(wait_ms, 200, 0, 5000)
    if wait_ms:
        time.sleep(wait_ms / 1000)
    drain_terminal_output(term)
    return (
        f"Sent {len(data)} byte(s) to {terminal_id}.\n"
        f"log_path: {term.log_path}\n"
        f"next_offset: {term.output_size}\n"
        "Use terminal_read with an earlier offset if you need the new output."
    )


def tool_terminal_read(
    ctx: ToolContext,
    terminal_id: str,
    offset: int = 0,
    max_chars: int = TERMINAL_DEFAULT_READ_CHARS,
) -> str:
    term = ctx.terminals.get(terminal_id)
    if term is None:
        return f"Error: unknown terminal_id {terminal_id!r}. Use terminal_list."
    drain_terminal_output(term)
    offset = _clamp_int(offset, 0, 0, 10**18)
    max_chars = _clamp_int(
        max_chars, TERMINAL_DEFAULT_READ_CHARS, 1, TERMINAL_MAX_READ_CHARS
    )
    try:
        size = term.log_path.stat().st_size
    except FileNotFoundError:
        size = 0
    offset = min(offset, size)
    try:
        with open(term.log_path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(max_chars)
            next_offset = fh.tell()
    except Exception as e:
        return f"Error reading terminal output: {e}"
    status, rc, elapsed = _format_status(term)
    content = chunk.decode("utf-8", errors="replace")
    return (
        f"Terminal {term.id}: {status}"
        + (f" (return_code {rc})" if rc is not None else "")
        + f" · runtime {elapsed:.1f}s\n"
        f"log_path: {term.log_path}\n"
        f"offset: {offset}\n"
        f"next_offset: {next_offset}\n"
        f"bytes_read: {len(chunk)}\n"
        "--- output ---\n"
        f"{content}"
    )


def tool_terminal_interrupt(ctx: ToolContext, terminal_id: str) -> str:
    term = ctx.terminals.get(terminal_id)
    if term is None:
        return f"Error: unknown terminal_id {terminal_id!r}. Use terminal_list."
    if not _running(term):
        return f"Terminal {terminal_id} is not running."
    try:
        os.write(term.master_fd, b"\x03")
    except OSError as e:
        return f"Error sending Ctrl-C to {terminal_id}: {e}"
    time.sleep(0.1)
    drain_terminal_output(term)
    return f"Sent Ctrl-C to {terminal_id}."


def tool_terminal_close(ctx: ToolContext, terminal_id: str) -> str:
    term = ctx.terminals.get(terminal_id)
    if term is None:
        return f"Error: unknown terminal_id {terminal_id!r}."
    if term.proc.poll() is None:
        try:
            os.killpg(os.getpgid(term.proc.pid), signal.SIGTERM)
        except Exception:
            pass
        for _ in range(20):
            if term.proc.poll() is not None:
                break
            time.sleep(0.01)
        if term.proc.poll() is None:
            try:
                os.killpg(os.getpgid(term.proc.pid), signal.SIGKILL)
            except Exception:
                pass
    drain_terminal_output(term)
    try:
        os.close(term.master_fd)
    except Exception:
        pass
    term.closed = True
    ctx.terminals.pop(terminal_id, None)
    return f"Closed terminal {terminal_id}. log_path: {term.log_path}"


def tool_terminal_list(ctx: ToolContext) -> str:
    if not ctx.terminals:
        return "No terminals."
    lines = ["Terminals:"]
    for term in ctx.terminals.values():
        drain_terminal_output(term)
        status, rc, elapsed = _format_status(term)
        marker = "running" if status == "running" else f"{status} {rc}"
        lines.append(
            f"  {term.id}  [{marker}]  {elapsed:.1f}s  "
            f"{term.name}  ·  {_short(term.command, 60)}\n"
            f"      log: {term.log_path}"
        )
    return "\n".join(lines)
