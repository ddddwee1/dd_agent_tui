"""Tool definitions: JSON schemas the model sees + Python implementations
the runtime calls.

Every `tool_*` function takes a `ToolContext` as its first parameter so
the work-dir, bash-job table, and id allocator are explicit (rather
than module-level globals). `execute_tool` is the single dispatch
entry point — it threads the ctx through to whichever tool the model
asked for.

Future subagents will get their own ToolContext instance and reuse
this same module unchanged.
"""

from __future__ import annotations

import difflib
import fnmatch
import gzip
import json
import os
import re
import signal
import subprocess
import time
import zlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import (
    BASH_OUTPUT_MAX_CHARS,
    BASH_TIMEOUT,
    BG_DEFAULT_TAIL_LINES,
    BG_DEFAULT_WAIT_TIMEOUT,
    BG_JOB_LOG_DIR,
    BG_MAX_CONCURRENT,
    BG_MAX_WAIT_TIMEOUT,
    BG_RETENTION_SECONDS,
    DANGEROUS_SHELL_PATTERNS,
    READ_FILE_MAX_LINES,
    WEB_FETCH_HARD_CHAR_CAP,
    WEB_FETCH_MAX_BYTES,
    WEB_FETCH_MAX_CHARS,
    WEB_FETCH_TIMEOUT,
)
from .state import BgJob, ToolContext, bg_job_status as _bg_status


# ───────── tools (JSON schemas) ─────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a bash command and return stdout/stderr/exit code. "
                "The command runs in the project working directory by default. "
                "Output is truncated at 10 000 chars. "
                "Dangerous patterns (sudo, curl, wget, chmod 777, mkfs, dd, rm -rf /) are blocked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to run.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Optional working directory for this command. "
                            "Defaults to the project directory."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_start",
            "description": (
                "Start a long-running bash command in the BACKGROUND and "
                "return immediately with a job id (e.g. 'bg-3'). The "
                "process is detached (own session) so it survives short "
                "agent stalls. stdout+stderr are merged into a log file. "
                "Use bash_check / bash_wait / bash_kill / bash_list to "
                "interact with it. Cap: at most 5 jobs running at once. "
                "Same dangerous-pattern blacklist as bash."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bash command to run.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": (
                            "Optional working directory. Defaults to "
                            "the project directory."
                        ),
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_check",
            "description": (
                "Peek at a background job: returns current status "
                "(running / exited <code>) and the last N lines of "
                "merged stdout+stderr. Non-blocking — returns immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id from bash_start (e.g. 'bg-3').",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "How many trailing lines of log to return. Default 50.",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_wait",
            "description": (
                "BLOCK until a background job finishes or *timeout* "
                "seconds elapse. Returns final status + log tail. Use "
                "this when you have nothing else to do but wait for a "
                "long task to finish. Default timeout 60s; max 600s. "
                "If the timeout fires while the job is still running, "
                "the job KEEPS RUNNING — call bash_wait again or "
                "bash_check to follow up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id from bash_start.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to block. Default 60, max 600.",
                    },
                    "tail_lines": {
                        "type": "integer",
                        "description": "Trailing log lines to return. Default 50.",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_kill",
            "description": (
                "Terminate a running background job. Sends SIGTERM by "
                "default (graceful); set force=true to send SIGKILL. "
                "No-op if the job has already exited."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id from bash_start.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Send SIGKILL instead of SIGTERM. Default false.",
                    },
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_list",
            "description": (
                "List all known background jobs (running and recently "
                "finished) with id, command, status, runtime, and log "
                "path. Finished jobs are kept around for 5 minutes."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a text file, with optional offset/limit for pagination. "
                "Returns up to 2000 lines by default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start reading from (0-indexed). Default 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum lines to return. Default 2000.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create or overwrite a file with the given content. "
                "If force=False and the file already exists, the operation is refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite without confirmation. Default true.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace ONE occurrence of old_string with new_string in a file. "
                "old_string must match exactly. If there are multiple matches, "
                "either provide a larger string with more surrounding context to "
                "make it unique, or specify the occurrence number (1-indexed). "
                "When multiple matches are found and no occurrence is given, "
                "the tool returns line numbers and surrounding context for each match."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to replace.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                    "occurrence": {
                        "type": "integer",
                        "description": (
                            "Which occurrence to replace (1-indexed). "
                            "Only needed when old_string appears more than once."
                        ),
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_lines",
            "description": (
                "Edit a file by line numbers (1-indexed). Use this for precise "
                "line-level edits when you know which lines to change. "
                "mode='replace' replaces lines [start_line..end_line] inclusive with content. "
                "mode='insert' inserts content BEFORE start_line; end_line is ignored. "
                "mode='delete' removes lines [start_line..end_line]; content is ignored. "
                "content can be multi-line (use '\\n'). For substring substitution within a line, "
                "use edit_file instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["replace", "insert", "delete"],
                        "description": "Edit mode.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Start line (1-indexed).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "End line (1-indexed, inclusive). Required for replace/delete.",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content. Required for replace/insert.",
                    },
                },
                "required": ["path", "mode", "start_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": (
                "Apply multiple string replacements to ONE file in a single call. "
                "Edits are applied in order; each one operates on the result of "
                "the previous, so a later edit can reference text introduced by "
                "an earlier one. All edits must succeed atomically — if any one "
                "fails, the file is left untouched. Use this instead of multiple "
                "edit_file calls when changing several parts of the same file. "
                "Each edit follows edit_file rules: old_string must match exactly. "
                "If old_string appears more than once in the (current) buffer, "
                "either set replace_all=true or specify occurrence (1-indexed). "
                "replace_all and occurrence are mutually exclusive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file.",
                    },
                    "edits": {
                        "type": "array",
                        "description": "Ordered list of edit operations.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {
                                    "type": "string",
                                    "description": "Exact text to replace.",
                                },
                                "new_string": {
                                    "type": "string",
                                    "description": "Replacement text.",
                                },
                                "occurrence": {
                                    "type": "integer",
                                    "description": (
                                        "Which occurrence to replace (1-indexed). "
                                        "Required when old_string matches multiple "
                                        "times and replace_all is not set."
                                    ),
                                },
                                "replace_all": {
                                    "type": "boolean",
                                    "description": (
                                        "Replace every occurrence. Default false. "
                                        "Mutually exclusive with occurrence."
                                    ),
                                },
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and directories under a given path, recursively up to a "
                "specified depth (default 2). Supports an optional glob filter for "
                "file names (e.g. '*.py'). Directories are suffixed with '/'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list. Default '.'.",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Maximum recursion depth. Default 2.",
                    },
                    "file_filter": {
                        "type": "string",
                        "description": "Optional glob pattern for files (e.g. '*.py').",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_content",
            "description": (
                "Search for a regex pattern in files under a path. "
                "Returns matching lines with file:line:content. "
                "Supports file_filter glob (e.g. '*.py') and case-insensitive "
                "matching by default. Results are truncated at 500 matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in. Default '.'.",
                    },
                    "file_filter": {
                        "type": "string",
                        "description": "Optional glob to filter files (e.g. '*.py', '*.txt').",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Set to true for case-sensitive search. Default false.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "Find files by glob pattern, recursively. Use this when you "
                "need a multi-level pattern like '**/*.py' that list_files's "
                "single-level file_filter can't express. '**' matches any "
                "number of directories. Hidden files / directories (starting "
                "with '.') are skipped unless the pattern names them "
                "explicitly. Returns matching paths sorted by modification "
                "time descending (most-recent first), capped at 1000 results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Glob pattern (e.g. '**/*.py', 'src/**/test_*.ts')."
                        ),
                    },
                    "path": {
                        "type": "string",
                        "description": "Root directory. Default '.'.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch the contents of an http(s) URL. HTML pages are stripped to "
                "plain text (script/style/svg removed, whitespace collapsed); JSON, "
                "XML, plain text and similar are returned verbatim. Binary content "
                "(images, video, pdf, octet-stream, …) is refused. Output is "
                "truncated at 10 000 chars by default."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL to fetch.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "Override the 10 000-char output cap. "
                            "Hard upper bound is 50 000."
                        ),
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": (
                "Spawn an isolated subagent to handle a self-contained "
                "subtask, then return its final answer as this tool's "
                "result. Use spawn_agent for: (1) reducing context "
                "noise — let a subagent grind through a search, build, "
                "or codebase tour and only the conclusion comes back; "
                "(2) parallel-style investigations you'll fold into "
                "your main work. The subagent has its own conversation "
                "history and bash job table; this conversation only "
                "sees its final assistant message. The subagent CAN'T "
                "spawn further subagents (depth is limited to 1) and "
                "CAN'T read your scratch state — give it everything it "
                "needs in `prompt`. Token usage is billed to this "
                "conversation and shows up in the status bar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The subagent's user message. State the "
                            "task, success criteria, and any context "
                            "(file paths, prior findings) the subagent "
                            "needs — it starts with no memory of this "
                            "conversation."
                        ),
                    },
                    "system": {
                        "type": "string",
                        "description": (
                            "Optional extra system instruction appended "
                            "to the framework system prompt. Use to "
                            "narrow the subagent's role, e.g. 'You are "
                            "a read-only investigator: report findings, "
                            "do not modify files.'"
                        ),
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_tool",
            "description": (
                "Maintain a structured TODO list across the conversation. "
                "Pass the FULL list every call (overwrite mode) — there is "
                "no append/edit operation. Use it for any multi-step task "
                "so the user can see progress: list everything as 'pending' "
                "first, mark exactly one item 'in_progress' while you work "
                "on it, mark it 'completed' as soon as it's done, then "
                "promote the next 'pending' to 'in_progress'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Full ordered list. Replaces any existing list.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "Task description (imperative verb).",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "Current state of this item.",
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
]


# ───────── helpers ─────────

def _safe_path(work_dir: str, raw: str) -> Path:
    """Resolve a user-supplied path relative to *work_dir*.

    Returns an absolute Path. Does NOT enforce a sandbox (the user may
    navigate anywhere) — use with caution.
    """
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path(work_dir, p).resolve()


def _check_dangerous(command: str) -> str | None:
    """Return an error string if *command* matches any dangerous pattern, else None."""
    for pat in DANGEROUS_SHELL_PATTERNS:
        if re.search(pat, command):
            return f"Blocked: command matches dangerous pattern /{pat}/"
    return None


def _unified_diff(before: str, after: str, p: Path, context: int = 2) -> str:
    """Render a unified-diff between two file contents. Used in edit_* tool
    results so both the model and the UI can see exactly what changed."""
    diff_lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            lineterm="",
            fromfile=f"a/{p.name}",
            tofile=f"b/{p.name}",
            n=context,
        )
    )
    return "\n".join(diff_lines) if diff_lines else "(no changes)"


# ───────── bash ─────────

def tool_bash(ctx: ToolContext, command: str, workdir: str | None = None) -> str:
    """Run a shell command with safety checks, timeout, and output truncation."""
    err = _check_dangerous(command)
    if err:
        return f"Error: {err}"

    cwd = str(_safe_path(ctx.work_dir, workdir)) if workdir else ctx.work_dir
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
        if len(out) > BASH_OUTPUT_MAX_CHARS:
            out = out[:BASH_OUTPUT_MAX_CHARS] + (
                f"\n…[+{len(result.stdout) - BASH_OUTPUT_MAX_CHARS} chars]"
            )
        parts.append(f"STDOUT:\n{out}")
    if result.stderr:
        err_out = result.stderr
        if len(err_out) > BASH_OUTPUT_MAX_CHARS:
            err_out = err_out[:BASH_OUTPUT_MAX_CHARS] + (
                f"\n…[+{len(result.stderr) - BASH_OUTPUT_MAX_CHARS} chars]"
            )
        parts.append(f"STDERR:\n{err_out}")
    parts.append(f"Exit code: {result.returncode}")
    return "\n".join(parts)


# ───────── background bash ─────────

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

    cwd = str(_safe_path(ctx.work_dir, workdir)) if workdir else ctx.work_dir
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
    ctx: ToolContext, job_id: str, tail_lines: int = BG_DEFAULT_TAIL_LINES
) -> str:
    job = ctx.bash_jobs.get(job_id)
    if job is None:
        return f"Error: unknown job_id {job_id!r}. Use bash_list to see jobs."
    try:
        tail_lines = max(0, min(500, int(tail_lines)))
    except (TypeError, ValueError):
        tail_lines = BG_DEFAULT_TAIL_LINES
    return _format_status(job, tail_lines)


def tool_bash_wait(
    ctx: ToolContext,
    job_id: str,
    timeout: int = BG_DEFAULT_WAIT_TIMEOUT,
    tail_lines: int = BG_DEFAULT_TAIL_LINES,
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
            return (
                f"bash_wait timed out after {timeout}s; job {job_id} is "
                f"still running.\n"
                + _format_status(job, tail_lines)
            )
        # 200ms strikes a balance: short enough that the model gets a
        # near-instant return on quick jobs, long enough that polling
        # overhead stays trivial.
        time.sleep(0.2)
    return _format_status(job, tail_lines)


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


# ───────── files ─────────

def tool_read_file(
    ctx: ToolContext,
    path: str,
    offset: int = 0,
    limit: int = READ_FILE_MAX_LINES,
) -> str:
    """Read a file with optional pagination."""
    p = _safe_path(ctx.work_dir, path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return f"Error: file not found: {p}"
    except UnicodeDecodeError:
        return f"Error: cannot read {p} as UTF-8 text (possibly binary)"
    except Exception as e:
        return f"Error reading {p}: {e}"

    lines = text.splitlines()
    total = len(lines)
    if offset < 0:
        offset = 0
    if offset >= total:
        return f"File has {total} lines; offset {offset} is out of range."
    if limit <= 0:
        limit = READ_FILE_MAX_LINES

    chunk = lines[offset : offset + limit]
    header = f"[{p}] lines {offset}-{offset + len(chunk) - 1} of {total}"
    return header + "\n" + "\n".join(chunk)


def tool_write_file(
    ctx: ToolContext, path: str, content: str, force: bool = True
) -> str:
    """Create or overwrite a file."""
    p = _safe_path(ctx.work_dir, path)
    if not force and p.exists():
        return (
            f"Error: {p} already exists. "
            "Set force=True to overwrite, or use edit_file to modify it."
        )
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"Wrote {len(content)} chars to {p}"
    except Exception as e:
        return f"Error writing {p}: {e}"


def tool_edit_file(
    ctx: ToolContext,
    path: str,
    old_string: str,
    new_string: str,
    occurrence: int | None = None,
) -> str:
    """Replace one occurrence of old_string with new_string.

    When *occurrence* is omitted and old_string matches more than once,
    return contextual information about every match so the caller can
    refine the request.
    """
    p = _safe_path(ctx.work_dir, path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return f"Error: file not found: {p}"
    except UnicodeDecodeError:
        return f"Error: cannot read {p} as UTF-8 text (possibly binary)"
    except Exception as e:
        return f"Error reading {p}: {e}"

    # Count and locate matches
    positions: list[int] = []
    start = 0
    while True:
        idx = text.find(old_string, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1  # allow overlapping; unlikely for practical uses

    if not positions:
        return f"Error: old_string not found in {p}"

    if len(positions) > 1 and occurrence is None:
        # Build context for each match
        lines = text.splitlines()
        # Build a cheap char→line map
        char_to_line: list[int] = []
        for i, line in enumerate(lines):
            char_to_line.extend([i] * (len(line) + 1))  # +1 for newline
        ctx_parts = [
            f"old_string matches {len(positions)} times in {p}. "
            "Specify occurrence=N or add more surrounding context to make it unique.\n"
            "Matches:"
        ]
        for i, pos in enumerate(positions, 1):
            if pos < len(char_to_line):
                lineno = char_to_line[pos] + 1  # 1-indexed
            else:
                lineno = -1
            # Extract surrounding snippet (up to 3 lines around)
            before = text[max(0, pos - 80):pos]
            snippet = text[pos:pos + len(old_string)]
            after = text[pos + len(old_string):pos + len(old_string) + 80]
            ctx_parts.append(
                f"  [{i}] line ~{lineno}  …{before!r}>>>{snippet}<<<{after!r}…"
            )
        return "\n".join(ctx_parts)

    # Determine which occurrence to replace
    if occurrence is not None:
        if occurrence < 1 or occurrence > len(positions):
            return (
                f"Error: occurrence={occurrence} out of range; "
                f"there are {len(positions)} match(es) in {p}"
            )
        idx = positions[occurrence - 1]
    else:
        idx = positions[0]

    new_text = text[:idx] + new_string + text[idx + len(old_string):]
    try:
        p.write_text(new_text)
    except Exception as e:
        return f"Error writing {p}: {e}"
    diff = _unified_diff(text, new_text, p)
    return (
        f"Edited {p} (replaced occurrence {occurrence or 1} of {len(positions)})\n\n"
        f"{diff}"
    )


def tool_edit_lines(
    ctx: ToolContext,
    path: str,
    mode: str,
    start_line: int,
    end_line: int | None = None,
    content: str = "",
) -> str:
    """Edit a file by line numbers (1-indexed).

    mode == "replace": lines [start_line..end_line] inclusive → content
    mode == "insert":  insert content before start_line; end_line ignored
    mode == "delete":  remove lines [start_line..end_line]; content ignored
    """
    p = _safe_path(ctx.work_dir, path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return f"Error: file not found: {p}"
    except UnicodeDecodeError:
        return f"Error: cannot read {p} as UTF-8 text (possibly binary)"
    except Exception as e:
        return f"Error reading {p}: {e}"

    had_trailing_newline = text.endswith("\n")
    lines = text.splitlines()  # ["a", "b", "c"] — no line endings retained
    n = len(lines)

    def _split_content(c: str) -> list[str]:
        # Drop a single trailing newline so content="line\n" inserts ONE line,
        # not two (the second being empty).
        if c.endswith("\n"):
            c = c[:-1]
        return c.split("\n")

    if mode == "replace":
        if end_line is None:
            return "Error: replace mode requires end_line"
        if start_line < 1 or end_line < 1:
            return "Error: line numbers must be >= 1"
        if start_line > n or end_line > n:
            return f"Error: range [{start_line}..{end_line}] out of bounds; file has {n} line(s)"
        if start_line > end_line:
            return f"Error: start_line ({start_line}) > end_line ({end_line})"
        new_block = _split_content(content)
        result_lines = lines[: start_line - 1] + new_block + lines[end_line:]
        action = f"replaced lines {start_line}-{end_line} with {len(new_block)} line(s)"
    elif mode == "insert":
        if start_line < 1:
            return "Error: line number must be >= 1"
        if start_line > n + 1:
            return (
                f"Error: start_line {start_line} out of bounds; "
                f"file has {n} line(s) (max insert position {n + 1})"
            )
        new_block = _split_content(content)
        result_lines = lines[: start_line - 1] + new_block + lines[start_line - 1 :]
        action = f"inserted {len(new_block)} line(s) before line {start_line}"
    elif mode == "delete":
        if end_line is None:
            return "Error: delete mode requires end_line"
        if start_line < 1 or end_line < 1:
            return "Error: line numbers must be >= 1"
        if start_line > n or end_line > n:
            return f"Error: range [{start_line}..{end_line}] out of bounds; file has {n} line(s)"
        if start_line > end_line:
            return f"Error: start_line ({start_line}) > end_line ({end_line})"
        result_lines = lines[: start_line - 1] + lines[end_line:]
        action = f"deleted lines {start_line}-{end_line} ({end_line - start_line + 1} line(s))"
    else:
        return f"Error: unknown mode '{mode}'. Use 'replace', 'insert', or 'delete'."

    new_text = "\n".join(result_lines)
    if result_lines and had_trailing_newline:
        new_text += "\n"

    try:
        p.write_text(new_text)
    except Exception as e:
        return f"Error writing {p}: {e}"
    diff = _unified_diff(text, new_text, p)
    return (
        f"Edited {p}: {action}; file now has {len(result_lines)} line(s)\n\n"
        f"{diff}"
    )


def _replace_nth(buffer: str, old: str, new: str, n: int) -> tuple[str, int]:
    """Replace the *n*-th (1-indexed) occurrence of *old* with *new* in
    *buffer*. Returns (new_buffer, total_match_count). Caller is
    responsible for validating n against the count."""
    positions: list[int] = []
    start = 0
    while True:
        idx = buffer.find(old, start)
        if idx == -1:
            break
        positions.append(idx)
        start = idx + 1
    if n < 1 or n > len(positions):
        return buffer, len(positions)
    idx = positions[n - 1]
    return buffer[:idx] + new + buffer[idx + len(old):], len(positions)


def tool_multi_edit(ctx: ToolContext, path: str, edits: list | None = None) -> str:
    """Apply a list of string replacements to one file atomically."""
    p = _safe_path(ctx.work_dir, path)
    try:
        before = p.read_text()
    except FileNotFoundError:
        return f"Error: file not found: {p}"
    except UnicodeDecodeError:
        return f"Error: cannot read {p} as UTF-8 text (possibly binary)"
    except Exception as e:
        return f"Error reading {p}: {e}"

    if not isinstance(edits, list) or not edits:
        return "Error: 'edits' must be a non-empty array of edit operations"

    buffer = before
    summary: list[str] = []
    for i, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            return f"Edit #{i}: must be a JSON object"
        old = edit.get("old_string")
        new = edit.get("new_string")
        occurrence = edit.get("occurrence")
        replace_all = bool(edit.get("replace_all", False))
        if old is None or new is None:
            return f"Edit #{i}: missing 'old_string' or 'new_string'"
        if not isinstance(old, str) or not isinstance(new, str):
            return f"Edit #{i}: 'old_string' and 'new_string' must be strings"
        if old == new:
            return f"Edit #{i}: old_string == new_string (no-op)"
        if replace_all and occurrence is not None:
            return (
                f"Edit #{i}: replace_all and occurrence are mutually "
                "exclusive — pick one"
            )

        count = buffer.count(old)
        if count == 0:
            return (
                f"Edit #{i}: old_string not found in current buffer. "
                "Note that earlier edits in this call may have already "
                "transformed the file; old_string must match the buffer "
                "AT THE TIME this edit runs."
            )

        if replace_all:
            buffer = buffer.replace(old, new)
            summary.append(f"  #{i}: replaced all {count} occurrence(s)")
        elif occurrence is not None:
            if not isinstance(occurrence, int):
                return f"Edit #{i}: 'occurrence' must be an integer"
            if occurrence < 1 or occurrence > count:
                return (
                    f"Edit #{i}: occurrence={occurrence} out of range "
                    f"(buffer has {count} match(es))"
                )
            buffer, _ = _replace_nth(buffer, old, new, occurrence)
            summary.append(
                f"  #{i}: replaced occurrence {occurrence}/{count}"
            )
        else:
            if count > 1:
                return (
                    f"Edit #{i}: old_string matches {count} times in the "
                    "current buffer. Set replace_all=true, set "
                    "occurrence=N (1-indexed), or expand old_string with "
                    "more surrounding context to make it unique."
                )
            buffer = buffer.replace(old, new, 1)
            summary.append(f"  #{i}: replaced 1 occurrence")

    if buffer == before:
        return "No changes — every edit was a no-op."

    try:
        p.write_text(buffer)
    except Exception as e:
        return f"Error writing {p}: {e}"

    diff = _unified_diff(before, buffer, p)
    head = (
        f"multi_edit applied {len(edits)} edit(s) to {p}:\n"
        + "\n".join(summary)
    )
    return f"{head}\n\n{diff}"


# ───────── search ─────────

def tool_list_files(
    ctx: ToolContext,
    path: str = ".",
    depth: int = 2,
    file_filter: str | None = None,
) -> str:
    """List files and directories recursively, with depth limit and glob filter."""
    p = _safe_path(ctx.work_dir, path)
    if not p.exists():
        return f"Error: path not found: {p}"
    if not p.is_dir():
        # Single file – just show it
        return str(p)

    entries: list[str] = []
    for root, dirs, files in os.walk(p):
        rel = os.path.relpath(root, p)
        if rel == ".":
            current_depth = 0
        else:
            current_depth = rel.count(os.sep) + 1

        if current_depth > depth:
            dirs[:] = []  # prune deeper dirs
            continue

        for d in sorted(dirs):
            entries.append(os.path.join(rel, d) + "/")
        for f in sorted(files):
            if file_filter and not fnmatch.fnmatch(f, file_filter):
                continue
            entries.append(os.path.join(rel, f))

    if not entries:
        return f"{p} (empty or no files matching filter)"
    return "\n".join(entries)


def tool_search_content(
    ctx: ToolContext,
    pattern: str,
    path: str = ".",
    file_filter: str = "*",
    case_sensitive: bool = False,
) -> str:
    """Grep-like regex search over files."""
    p = _safe_path(ctx.work_dir, path)
    if not p.exists():
        return f"Error: path not found: {p}"

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: invalid regex pattern: {e}"

    MAX_MATCHES = 500
    matches: list[str] = []

    if p.is_file():
        files_to_search = [p]
    else:
        files_to_search = []
        for root, _dirs, files in os.walk(p):
            for f in files:
                if fnmatch.fnmatch(f, file_filter):
                    files_to_search.append(Path(root, f))
            if len(files_to_search) > 2000:
                break  # don't scan massive trees

    for fpath in files_to_search:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    if regex.search(line):
                        rel = os.path.relpath(str(fpath), str(p))
                        matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                        if len(matches) >= MAX_MATCHES:
                            matches.append(f"…[truncated at {MAX_MATCHES} matches]")
                            return "\n".join(matches)
        except Exception:
            continue

    return "\n".join(matches) if matches else "(no matches)"


def tool_glob_files(ctx: ToolContext, pattern: str, path: str = ".") -> str:
    """Recursive glob: returns file paths matching *pattern* under *path*.

    Path.glob already skips entries starting with '.' for `*` / `**`, so
    `.git`, `node_modules` (when hidden) etc. don't pollute results.
    Sorted by mtime descending so recently-touched files come first.
    """
    p = _safe_path(ctx.work_dir, path)
    if not p.exists():
        return f"Error: path not found: {p}"
    if not p.is_dir():
        return f"Error: not a directory: {p}"

    MAX_RESULTS = 1000
    matches: list[Path] = []
    try:
        for match in p.glob(pattern):
            if not match.is_file():
                continue
            matches.append(match)
            # Hard cap on scan to avoid pathological patterns blowing memory.
            if len(matches) >= MAX_RESULTS * 5:
                break
    except Exception as e:
        return f"Error: glob failed: {e}"

    if not matches:
        return f"(no files matching {pattern!r} under {p})"

    try:
        matches.sort(key=lambda m: m.stat().st_mtime, reverse=True)
    except Exception:
        pass

    truncated = len(matches) > MAX_RESULTS
    shown = matches[:MAX_RESULTS]
    lines: list[str] = []
    for m in shown:
        try:
            lines.append(str(m.relative_to(p)))
        except ValueError:
            lines.append(str(m))

    head = f"{len(matches)}{' (truncated)' if truncated else ''} match(es) under {p}:"
    out = head + "\n" + "\n".join(lines)
    if truncated:
        out += f"\n…[truncated at {MAX_RESULTS} files]"
    return out


# ───────── web ─────────

class _HTMLTextExtractor(HTMLParser):
    """Strip HTML to readable plain text.

    Skips script/style/noscript/svg/head subtrees entirely. Inserts a newline
    around block-level tags so the resulting text retains paragraph structure.
    Whitespace is collapsed by the caller.
    """

    SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "head"})
    BLOCK_TAGS = frozenset({
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "nav", "aside",
        "blockquote", "pre", "table", "hr", "ul", "ol",
    })

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse intra-line runs of whitespace, drop trailing spaces, then
        # squeeze 3+ blank lines down to one.
        cleaned: list[str] = []
        blank_run = 0
        for line in raw.splitlines():
            line = re.sub(r"[ \t]+", " ", line).strip()
            if not line:
                blank_run += 1
                if blank_run <= 1:
                    cleaned.append("")
            else:
                blank_run = 0
                cleaned.append(line)
        return "\n".join(cleaned).strip()


_BINARY_CTYPE_PREFIXES = (
    "image/", "audio/", "video/", "font/",
    "application/pdf", "application/zip", "application/x-tar",
    "application/x-gzip", "application/x-bzip", "application/x-7z",
    "application/octet-stream",
)


def tool_web_fetch(
    ctx: ToolContext, url: str, max_chars: int | None = None
) -> str:
    """Fetch an http(s) URL and return its body as text.

    HTML is stripped to plain text; other text-like content types are
    returned verbatim. Binary types are refused. Size is capped both at
    the byte level (raw download) and the char level (returned string).
    """
    cap = WEB_FETCH_MAX_CHARS
    if max_chars is not None:
        try:
            cap = max(1, min(WEB_FETCH_HARD_CHAR_CAP, int(max_chars)))
        except (TypeError, ValueError):
            return f"Error: max_chars must be an integer, got {max_chars!r}"

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"Error: only http(s) URLs are allowed (got: {url!r})"

    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ddtui-agent/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
    )
    try:
        with urlopen(req, timeout=WEB_FETCH_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").strip()
            encoding = (resp.headers.get("Content-Encoding") or "").lower().strip()
            final_url = resp.geturl()
            status = resp.status
            # Read at most MAX_BYTES + 1 so we can detect truncation. Keep
            # the +1 separate from MAX_BYTES because some servers ignore
            # range hints; this just bounds memory.
            raw = resp.read(WEB_FETCH_MAX_BYTES + 1)
    except HTTPError as e:
        return f"HTTP error {e.code} {e.reason} for {url}"
    except URLError as e:
        return f"URL error: {e.reason} for {url}"
    except TimeoutError:
        return f"Error: request timed out after {WEB_FETCH_TIMEOUT}s"
    except Exception as e:
        return f"Error fetching {url}: {e}"

    truncated_bytes = len(raw) > WEB_FETCH_MAX_BYTES
    if truncated_bytes:
        raw = raw[:WEB_FETCH_MAX_BYTES]

    ctype_lc = ctype.lower()
    if any(ctype_lc.startswith(p) for p in _BINARY_CTYPE_PREFIXES):
        return (
            f"Refused: content-type {ctype!r} is binary; web_fetch returns "
            f"text only ({final_url})"
        )

    # Decode Content-Encoding (only matters when server actually compressed).
    try:
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        elif encoding == "deflate":
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                # Some servers send raw deflate (no zlib header).
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception as e:
        return f"Error: failed to decompress {encoding!r} response: {e}"

    charset = "utf-8"
    m = re.search(r"charset=([\w\-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    try:
        body = raw.decode(charset, errors="replace")
    except LookupError:
        body = raw.decode("utf-8", errors="replace")

    looks_html = (
        "html" in ctype_lc
        or body.lstrip()[:14].lower().startswith(("<!doctype html", "<html"))
    )
    if looks_html:
        parser = _HTMLTextExtractor()
        try:
            parser.feed(body)
            parser.close()
            text = parser.text() or body
        except Exception:
            text = body
    else:
        text = body

    notes: list[str] = []
    if len(text) > cap:
        text = text[:cap]
        notes.append(f"truncated at {cap} chars")
    if truncated_bytes:
        notes.append(f"raw response exceeded {WEB_FETCH_MAX_BYTES // 1000} KB")
    suffix = f"\n\n…[{'; '.join(notes)}]" if notes else ""

    header = f"GET {final_url} → {status} ({ctype or 'unknown content-type'})"
    return f"{header}\n\n{text}{suffix}"


# ───────── todo ─────────

def tool_todo_tool(ctx: ToolContext, items: list | None = None) -> str:
    """Return a one-line summary of the TODO state.

    The actual UI rendering (the persistent TodoBlock widget) is wired
    in `AgentApp._run_one_turn` — that's where the running app instance
    lives. This function exists so `execute_tool` can produce the
    message-history string for the model to see.
    """
    items = items or []
    n = len(items)
    n_done = sum(1 for i in items if i.get("status") == "completed")
    n_progress = sum(1 for i in items if i.get("status") == "in_progress")
    n_pending = n - n_done - n_progress
    return (
        f"TODO updated: {n_done} completed, {n_progress} in progress, "
        f"{n_pending} pending (total {n})"
    )


# ───────── dispatch ─────────

TOOL_FUNCS = {
    "bash": tool_bash,
    "bash_start": tool_bash_start,
    "bash_check": tool_bash_check,
    "bash_wait": tool_bash_wait,
    "bash_kill": tool_bash_kill,
    "bash_list": tool_bash_list,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "edit_lines": tool_edit_lines,
    "multi_edit": tool_multi_edit,
    "list_files": tool_list_files,
    "glob_files": tool_glob_files,
    "search_content": tool_search_content,
    "web_fetch": tool_web_fetch,
    "todo_tool": tool_todo_tool,
}


def execute_tool(ctx: ToolContext, name: str, args: dict) -> str:
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(ctx, **args)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:
        return f"Tool {name} crashed: {e}"
