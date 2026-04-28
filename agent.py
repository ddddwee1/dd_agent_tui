#!/usr/bin/env python3
"""DeepSeek thinking-mode TUI agent built on Textual.

Layout
------
    ┌──────────────────────────────────────────────────────┐
    │ Header (model · effort)                              │
    ├──────────────────────────────────────────────────────┤
    │                                                      │
    │   conversation (VerticalScroll)                      │
    │     UserBubble                                       │
    │     ThinkingBlock (Collapsible, default collapsed)   │
    │     AssistantMessage                                 │
    │     ToolCallBlock                                    │
    │                                                      │
    ├──────────────────────────────────────────────────────┤
    │ Input  ──────────────────────────────────────────────│
    │ StatusBar (token totals, always visible)             │
    └──────────────────────────────────────────────────────┘

Streaming
---------
AsyncOpenAI runs in a Textual worker. Each `reasoning_content` /
`content` / `tool_calls` delta updates the live widget in place via
`Static.update(...)` (no append-only log, so partial-line chunks behave).

Hooks
-----
`AgentApp.tool_confirm` is awaited before every tool execution. The
default `always_allow` returns True; replace it on the App instance
(or override on a subclass) to gate destructive ops behind a modal.

See: https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
"""

from __future__ import annotations

import asyncio
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
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from openai import AsyncOpenAI
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Collapsible, Header, Static, TextArea


# ───────── config ─────────

API_KEY_PATH = os.environ.get(
    "DEEPSEEK_API_KEY_FILE", str(Path.home() / "deepseek_apikey.txt")
)
BASE_URL = "https://api.deepseek.com"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "max")
BASH_TIMEOUT = 120
BASH_OUTPUT_MAX_CHARS = 10_000  # truncate huge stdout/stderr
READ_FILE_MAX_LINES = 2000       # default limit for read_file
AGENTS_MD_FILENAME = "AGENTS.md"
AGENTS_MD_MAX_CHARS = 16_000     # cap project guidelines so prompt stays sane
WEB_FETCH_TIMEOUT = 20
WEB_FETCH_MAX_BYTES = 2_000_000  # cap raw download (~2 MB)
WEB_FETCH_MAX_CHARS = 10_000     # cap returned text (matches bash output cap)
WEB_FETCH_HARD_CHAR_CAP = 50_000 # absolute upper bound when caller overrides

# /save target. Per-user, not per-project — conversations are tied to the
# model session, not the cwd, so a single global history dir is the
# natural fit.
HISTORY_DIR = Path.home() / ".ddtui" / "history"

# Background bash jobs: tunables.
BG_JOB_LOG_DIR = Path("/tmp")
BG_MAX_CONCURRENT = 5            # cap on simultaneously-running jobs
BG_RETENTION_SECONDS = 300       # keep finished jobs in dict this long
BG_DEFAULT_WAIT_TIMEOUT = 60     # bash_wait default
BG_MAX_WAIT_TIMEOUT = 600        # absolute upper bound on a single wait call
BG_DEFAULT_TAIL_LINES = 50

# Working directory – set at startup so tool functions can reference it.
WORK_DIR = os.getcwd()

# Dangerous shell patterns (blacklist). Matching commands are blocked.
DANGEROUS_SHELL_PATTERNS = [
    r"\bsudo\b",
    r"\bcurl\b",
    r"\bwget\b",
    r"\bchmod\s+777\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\b>:/\w",
    r"\brm\s+-rf\s+/",
]


SYSTEM_PROMPT = (
    "你可以使用下列tools: bash, bash_start, bash_check, bash_wait, bash_kill, bash_list, read_file, write_file, edit_file, edit_lines, multi_edit, list_files, search_content, web_fetch, todo_tool. "
    "对于多步骤任务，先用 todo_tool 列出计划（pending）；开始一项时把它标 in_progress；完成立刻标 completed 并继续下一项。"
    "如果上下文中出现以 \"# 历史摘要\" 开头的 system 消息，那是早期对话被 /compact 压缩后的记忆——请把它当作已知背景，不要重复其中已完成的步骤，也不要把它当作新指令来回应。"
    "请使用中文思考，用户可以看见思考过程，所以思考过程也要使用中文。请使用中文思考，无论AGENTS.md是以什么语言写的，请使用中文思考，无论AGENTS.md是以什么语言写的。请使用中文思考，无论后续文件是以什么语言写的。请使用中文思考，无论后续文件是以什么语言写的。"
    "当前路径（供你后续调用命令作参考）："
)


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


# ───────── tool implementations ─────────

def _safe_path(raw: str) -> Path:
    """Resolve a user-supplied path relative to WORK_DIR.

    Returns an absolute Path.  Does NOT enforce a sandbox (the user may
    navigate anywhere) — use with caution.
    """
    p = Path(raw)
    if p.is_absolute():
        return p
    return Path(WORK_DIR, p).resolve()


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


def tool_bash(command: str, workdir: str | None = None) -> str:
    """Run a shell command with safety checks, timeout, and output truncation."""
    err = _check_dangerous(command)
    if err:
        return f"Error: {err}"

    cwd = str(_safe_path(workdir)) if workdir else WORK_DIR
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

@dataclass
class BgJob:
    id: str
    command: str
    workdir: str
    log_path: Path
    proc: subprocess.Popen
    started_at: float
    finished_at: float | None = None  # set on first poll() that sees exit


_BG_JOBS: dict[str, BgJob] = {}
_BG_NEXT_ID: int = 1


def _bg_status(job: BgJob) -> tuple[str, int | None, float]:
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


def _bg_gc() -> None:
    """Drop finished jobs older than BG_RETENTION_SECONDS so the dict
    doesn't grow unbounded across a long session."""
    now = time.monotonic()
    stale = []
    for jid, j in _BG_JOBS.items():
        status, _rc, _elapsed = _bg_status(j)
        if status == "exited" and j.finished_at is not None:
            if now - j.finished_at > BG_RETENTION_SECONDS:
                stale.append(jid)
    for jid in stale:
        del _BG_JOBS[jid]


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


def _bg_count_running() -> int:
    return sum(1 for j in _BG_JOBS.values() if j.proc.poll() is None)


def tool_bash_start(command: str, workdir: str | None = None) -> str:
    """Launch *command* in the background and return its job id."""
    err = _check_dangerous(command)
    if err:
        return f"Error: {err}"
    _bg_gc()
    if _bg_count_running() >= BG_MAX_CONCURRENT:
        running = [
            f"{j.id}: {j.command[:40]}"
            for j in _BG_JOBS.values()
            if j.proc.poll() is None
        ]
        return (
            f"Error: at most {BG_MAX_CONCURRENT} background jobs may run "
            f"simultaneously. Currently running:\n  "
            + "\n  ".join(running)
            + "\nWait for one to finish or bash_kill it first."
        )

    cwd = str(_safe_path(workdir)) if workdir else WORK_DIR
    global _BG_NEXT_ID
    job_id = f"bg-{_BG_NEXT_ID}"
    _BG_NEXT_ID += 1

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
    _BG_JOBS[job_id] = job
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
    job_id: str, tail_lines: int = BG_DEFAULT_TAIL_LINES
) -> str:
    job = _BG_JOBS.get(job_id)
    if job is None:
        return f"Error: unknown job_id {job_id!r}. Use bash_list to see jobs."
    try:
        tail_lines = max(0, min(500, int(tail_lines)))
    except (TypeError, ValueError):
        tail_lines = BG_DEFAULT_TAIL_LINES
    return _format_status(job, tail_lines)


def tool_bash_wait(
    job_id: str,
    timeout: int = BG_DEFAULT_WAIT_TIMEOUT,
    tail_lines: int = BG_DEFAULT_TAIL_LINES,
) -> str:
    job = _BG_JOBS.get(job_id)
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


def tool_bash_kill(job_id: str, force: bool = False) -> str:
    job = _BG_JOBS.get(job_id)
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


def tool_bash_list() -> str:
    _bg_gc()
    if not _BG_JOBS:
        return "No background jobs."
    lines = ["Background jobs:"]
    for j in _BG_JOBS.values():
        status, rc, elapsed = _bg_status(j)
        marker = "running" if status == "running" else f"exit {rc}"
        cmd = j.command if len(j.command) <= 60 else j.command[:57] + "..."
        lines.append(
            f"  {j.id}  [{marker}]  {elapsed:.1f}s  {cmd}"
        )
    return "\n".join(lines)


def tool_read_file(path: str, offset: int = 0, limit: int = READ_FILE_MAX_LINES) -> str:
    """Read a file with optional pagination."""
    p = _safe_path(path)
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


def tool_write_file(path: str, content: str, force: bool = True) -> str:
    """Create or overwrite a file."""
    p = _safe_path(path)
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
    p = _safe_path(path)
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
        # Make sure we can index safely
        total_chars = sum(len(l) + 1 for l in lines)
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
    p = _safe_path(path)
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


def tool_multi_edit(path: str, edits: list | None = None) -> str:
    """Apply a list of string replacements to one file atomically."""
    p = _safe_path(path)
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


def tool_list_files(
    path: str = ".",
    depth: int = 2,
    file_filter: str | None = None,
) -> str:
    """List files and directories recursively, with depth limit and glob filter."""
    p = _safe_path(path)
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
    pattern: str,
    path: str = ".",
    file_filter: str = "*",
    case_sensitive: bool = False,
) -> str:
    """Grep-like regex search over files."""
    p = _safe_path(path)
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


def tool_web_fetch(url: str, max_chars: int | None = None) -> str:
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


def tool_todo_tool(items: list | None = None) -> str:
    """Return a one-line summary of the TODO state.

    The actual UI rendering (the persistent TodoBlock widget) is wired in
    `AgentApp._agent_turn` — that's where the running app instance lives.
    This function exists so `execute_tool` can produce the message-history
    string for the model to see.
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
    "search_content": tool_search_content,
    "web_fetch": tool_web_fetch,
    "todo_tool": tool_todo_tool,
}


def execute_tool(name: str, args: dict) -> str:
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(**args)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:
        return f"Tool {name} crashed: {e}"


# ───────── /compact helpers ─────────

# How much of each tool result to keep when rendering history for the
# summarizer. Tool results are often the bulk of token weight; the model
# only needs a flavor of what came back, not the full body.
COMPACT_TOOL_SNIPPET_CHARS = 400
# How many recent user→assistant turns to leave verbatim. The compactor
# replaces everything earlier with a single summary message.
COMPACT_KEEP_RECENT_TURNS = 2


def _render_history_for_summary(messages: list) -> str:
    """Flatten a slice of message history to plain text for the
    summarizer prompt. Drops reasoning_content (only useful at the time
    it was produced) and truncates tool results."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user":
            lines.append(f"=== USER ===\n{content}")
        elif role == "assistant":
            block = ["=== ASSISTANT ==="]
            if content:
                block.append(content)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name", "?")
                args_str = (fn.get("arguments") or "")[:200]
                block.append(f"  [tool_call] {name}({args_str})")
            lines.append("\n".join(block))
        elif role == "tool":
            snippet = content[:COMPACT_TOOL_SNIPPET_CHARS]
            if len(content) > COMPACT_TOOL_SNIPPET_CHARS:
                snippet += " …[truncated]"
            lines.append(f"=== TOOL RESULT ===\n{snippet}")
        # role == "system" intentionally skipped — system messages are
        # preserved across the compaction, so they don't belong in the
        # "what happened" recap.
    return "\n\n".join(lines)


# ───────── tool-confirm hook ─────────

ToolConfirmHook = Callable[[str, dict], Awaitable[bool]]


async def always_allow(name: str, args: dict) -> bool:
    """Default policy: every tool call is allowed.

    Future use: replace `app.tool_confirm = my_hook` to gate execution on
    a modal, an allowlist, or per-tool rules. Returning False will make
    the runner inject a "Tool execution blocked" tool result instead of
    invoking the function — the model sees this and can adapt.
    """
    return True


# ───────── token counter ─────────

class TokenCounter:
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
        self.last_reasoning = (getattr(details, "reasoning_tokens", 0) or 0) if details else 0
        self.prompt_total += self.last_prompt
        self.completion_total += self.last_completion
        self.reasoning_total += self.last_reasoning
        self.turns += 1


# ───────── widgets ─────────

class UserBubble(Static):
    DEFAULT_CSS = "UserBubble { margin: 1 0 0 0; }"

    def __init__(self, text: str) -> None:
        t = Text()
        t.append("you  ", style="bold blue")
        t.append(text)
        super().__init__(t)


class SteerBubble(Static):
    """A user 'steer' interjection — submitted via Alt/Cmd+Enter while a
    turn is running. Visually marked with ⚡ + golden border so it's
    obvious this isn't a normal turn-starting message; it'll be injected
    into the next LLM round as `[实时插话] <text>`."""

    DEFAULT_CSS = """
    SteerBubble {
        margin: 1 0 0 0;
        padding: 0 1;
        border-left: thick #f9e2af;
    }
    """

    def __init__(self, text: str) -> None:
        t = Text()
        t.append("⚡ steer  ", style="bold #f9e2af")
        t.append(text)
        super().__init__(t)


class AssistantMessage(Static):
    DEFAULT_CSS = "AssistantMessage { margin: 0 0 1 0; }"

    def __init__(self) -> None:
        self._buffer = ""
        super().__init__(self._build())

    def append_text(self, text: str) -> None:
        self._buffer += text
        self.update(self._build())

    @property
    def text(self) -> str:
        return self._buffer

    def _build(self) -> RenderableType:
        # Don't name this `_render` — that's a Textual Widget hook that must
        # return a Visual, not a Rich renderable.
        # Markdown lets the model produce tables, code fences, lists, headings
        # and have them render as such (instead of raw `| col |` text).
        if not self._buffer:
            return Text("assistant  ", style="bold green")
        return Group(
            Text("assistant", style="bold green"),
            Markdown(self._buffer),
        )


class ThinkingBlock(Collapsible):
    """Streamed `reasoning_content`. Default expanded; click the header
    (or focus it and press Enter) to toggle this one. Ctrl+T from anywhere
    folds/unfolds every thinking block at once.

    Title shows the reasoning token count once the stream finishes.
    """

    DEFAULT_CSS = """
    ThinkingBlock { margin: 0 0 1 0; }
    ThinkingBlock > Contents { padding: 0 0 0 2; }
    ThinkingBlock #thinking-body { color: $text-muted; }
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._body = Static("", id="thinking-body", markup=False)
        super().__init__(self._body, title="thinking · 流式中…", collapsed=False)

    def append_text(self, text: str) -> None:
        self._buffer += text
        self._body.update(self._buffer)

    def finalize(self, reasoning_tokens: int) -> None:
        if reasoning_tokens:
            self.title = f"thinking · {reasoning_tokens:,} tokens (点击展开/折叠)"
        else:
            self.title = "thinking · 完成 (点击展开/折叠)"

    @property
    def text(self) -> str:
        return self._buffer


class ToolCallBlock(Collapsible):
    """One tool call with its result. Default expanded; click the header
    to toggle. Title shows `● <name> <short args> <status>`; the body
    holds the pretty-printed args and the (truncated) result.
    """

    DEFAULT_CSS = """
    ToolCallBlock { margin: 0 0 1 0; }
    ToolCallBlock > Contents { padding: 0 0 0 2; }
    ToolCallBlock #toolcall-body { color: $text-muted; }
    """

    def __init__(self, name: str, args: dict) -> None:
        self._tool_name = name  # not _name — Widget already uses that
        self._args = args
        self._result: str | None = None
        self._blocked = False
        # markup=False so result text containing '[' or ']' won't be parsed.
        self._body = Static(self._build_body(), id="toolcall-body", markup=False)
        super().__init__(self._body, title=self._build_title(), collapsed=True)

    def set_result(self, result: str, blocked: bool = False) -> None:
        self._result = result
        self._blocked = blocked
        self._body.update(self._build_body())
        self.title = self._build_title()

    def _short_args(self) -> str:
        try:
            preview = json.dumps(self._args, ensure_ascii=False)
        except Exception:
            preview = str(self._args)
        if len(preview) > 80:
            preview = preview[:80] + "…"
        return preview

    def _build_title(self) -> str:
        if self._result is None:
            status = "执行中…"
        elif self._blocked:
            status = "⊘ 已拦截"
        else:
            status = "✓"
        return f"● {self._tool_name}  {self._short_args()}  {status}"

    def _build_body(self) -> RenderableType:
        try:
            args_pretty = json.dumps(self._args, ensure_ascii=False, indent=2)
        except Exception:
            args_pretty = str(self._args)
        parts: list[RenderableType] = [Text(args_pretty), Text("")]

        if self._result is None:
            parts.append(Text("执行中…", style="italic dim"))
        else:
            # For edit_file / edit_lines the diff is already stripped before
            # set_result is called (see AgentApp._agent_turn), so a uniform
            # snippet path works for every tool.
            snippet = (
                self._result
                if len(self._result) <= 4000
                else self._result[:4000] + f"\n…[+{len(self._result) - 4000} chars]"
            )
            parts.append(Text(snippet))
        return Group(*parts)


class DiffBlock(Collapsible):
    """Standalone diff view shown right after a successful edit_file or
    edit_lines call. Default expanded — the change is the headline; the
    user shouldn't have to dig for it. Title format: `📝 path  +N -M`.
    """

    DEFAULT_CSS = """
    DiffBlock { margin: 0 0 1 0; }
    DiffBlock > Contents { padding: 0 0 0 2; }
    """

    def __init__(self, path: str, diff_text: str) -> None:
        self._diff_text = diff_text
        body = Static(self._render_diff(diff_text), markup=False)
        super().__init__(
            body, title=self._make_title(path, diff_text), collapsed=False
        )

    @staticmethod
    def _make_title(path: str, diff_text: str) -> str:
        plus = minus = 0
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                plus += 1
            elif line.startswith("-") and not line.startswith("---"):
                minus += 1
        return f"📝 {path}  +{plus} -{minus}"

    @staticmethod
    def _render_diff(diff_text: str) -> Text:
        """Color a unified diff: + lines bright-white-fg on dark-green-bg,
        - lines bright-white-fg on dark-red-bg. The dark backgrounds
        (#0d3b18 / #3d0a0a) keep the bar low-key on Ubuntu's purple, while
        bright_white keeps the text crisp against them."""
        t = Text()
        for line in diff_text.splitlines(keepends=True):
            head = line.rstrip("\n")
            if head.startswith("+++") or head.startswith("---"):
                t.append(line, style="bold")
            elif head.startswith("@@"):
                t.append(line, style="bold cyan")
            elif head.startswith("+"):
                t.append(line, style="bright_white on #0d3b18")
            elif head.startswith("-"):
                t.append(line, style="bright_white on #3d0a0a")
            else:
                t.append(line)
        return t


class TodoBlock(Static):
    """Persistent TODO list maintained by the model via `todo_tool`.

    Only ONE instance lives in the conversation at a time — every call
    overwrites its contents in place, so the user sees a single panel
    that ticks items off as work progresses (rather than a stream of
    snapshots).
    """

    DEFAULT_CSS = """
    TodoBlock {
        margin: 1 0;
        padding: 0 1;
        border: round #cba6f7;
    }
    TodoBlock.all-done { border: round #f9e2af; }
    """

    def __init__(self) -> None:
        self._items: list[dict] = []
        super().__init__(self._build())

    def set_items(self, items: list[dict]) -> None:
        self._items = items
        self.update(self._build())

    def _build(self) -> Text:
        n = len(self._items)
        n_done = sum(1 for i in self._items if i.get("status") == "completed")
        n_prog = sum(1 for i in self._items if i.get("status") == "in_progress")
        n_pend = n - n_done - n_prog

        t = Text()
        t.append("TODO  ", style="bold #cba6f7")
        t.append(
            f"({n_prog} 进行中 · {n_done} 完成 · {n_pend} 待办)",
            style="dim",
        )
        if not self._items:
            t.append("\n  (empty)", style="dim italic")
            return t
        for item in self._items:
            status = item.get("status", "pending")
            content = item.get("content", "")
            t.append("\n")
            if status == "completed":
                t.append("  [", style="dim")
                t.append("✓", style="bold green")
                t.append("] ", style="dim")
                t.append(content, style="strike dim")
            elif status == "in_progress":
                t.append("  [", style="bold")
                t.append("▶", style="bold cyan")
                t.append("] ", style="bold")
                t.append(content, style="bold cyan")
            else:  # pending
                t.append("  [ ] ", style="dim")
                t.append(content, style="dim")
        return t


class BgJobsBlock(Static):
    """Sidebar widget showing live & recently-finished background bash
    jobs. Re-rendered on a 1-second timer driven by the App."""

    DEFAULT_CSS = """
    BgJobsBlock {
        margin: 1 0;
        padding: 0 1;
        border: round #94e2d5;
    }
    """

    def render_jobs(self, jobs: list[BgJob]) -> None:
        t = Text()
        t.append("Background  ", style="bold #94e2d5")
        n_run = sum(1 for j in jobs if j.proc.poll() is None)
        n_done = len(jobs) - n_run
        t.append(f"({n_run} 跑 · {n_done} 完)", style="dim")
        if not jobs:
            t.append("\n  (empty)", style="dim italic")
            self.update(t)
            return
        for j in jobs:
            status, rc, elapsed = _bg_status(j)
            cmd = j.command if len(j.command) <= 26 else j.command[:23] + "…"
            t.append("\n  ")
            if status == "running":
                t.append("▶ ", style="bold cyan")
                t.append(f"{j.id}", style="bold cyan")
            elif rc == 0:
                t.append("✓ ", style="bold green")
                t.append(f"{j.id}", style="green")
            else:
                t.append("✗ ", style="bold red")
                t.append(f"{j.id}", style="red")
            t.append(f"  {elapsed:5.1f}s ", style="dim")
            t.append(cmd)
        self.update(t)


class MultilineInput(TextArea):
    """Multi-line prompt box.

    User-facing key bindings:
      Enter           → submit (start a new turn / queue if busy)
      Option+Enter    → insert literal newline   (textual: alt+enter)
      Cmd+Enter       → submit as STEER          (textual: ctrl+enter,
                        via iTerm2 escape `[13;5u` for ⌘ Return)
      Ctrl+X          → forward to App.action_cancel_pending

    The textual binding names below use `alt+enter` and `ctrl+enter`
    because textual identifies modifiers by Linux/X11 conventions —
    macOS Option physically *is* alt, and Cmd+Enter is converted by
    iTerm2 (or the user's terminal) into the CSI u escape that textual
    decodes as ctrl+enter. macOS Terminal.app cannot do this conversion
    so Cmd+Enter won't reach steer there; use Option+Enter only.
    """

    BINDINGS = [
        Binding("enter", "do_submit", show=False, priority=True),
        # alt+enter ≡ Option+Enter on macOS
        Binding("alt+enter", "do_newline", show=False, priority=True),
        # ctrl+enter ≡ Cmd+Enter once iTerm2 maps ⌘ Return → [13;5u
        Binding("ctrl+enter", "do_steer", show=False, priority=True),
        # Reclaim ctrl+x from TextArea's default `cut`. macOS users
        # use ⌘+X for cut anyway, so this is a low-cost reassignment.
        Binding("ctrl+x", "app.cancel_pending", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    MultilineInput {
        height: 3;
        min-height: 3;
        max-height: 12;
        border: solid $border-blurred;
    }
    MultilineInput:focus {
        border: solid $border;
    }
    """

    # Outer height = inner rows + 2 (top + bottom border).
    _MIN_ROWS = 3
    _MAX_ROWS = 12

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class SteerSubmitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def action_do_submit(self) -> None:
        self.post_message(self.Submitted(self.text))

    def action_do_newline(self) -> None:
        # Mirrors what TextArea's built-in Enter handler does, but only
        # fires on Alt+Enter so plain Enter remains "submit".
        self.insert("\n")

    def action_do_steer(self) -> None:
        self.post_message(self.SteerSubmitted(self.text))

    def _fit_height(self) -> None:
        # Border eats two rows, so outer height = line_count + 2 to keep
        # every line visible. Past _MAX_ROWS the TextArea scrolls.
        rows = max(self._MIN_ROWS, min(self._MAX_ROWS, self.document.line_count + 2))
        self.styles.height = rows

    def on_mount(self) -> None:
        self._fit_height()

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._fit_height()


def _short_cwd(cwd: str) -> str:
    """Compress $HOME to ~ for status-bar display."""
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):]
    return cwd


def _detect_git_branch(cwd: str) -> str | None:
    """Return branch name, ``detached@<short>``, or None if *cwd* isn't
    inside a git repo (or git is unavailable). Runs once at startup —
    callers should cache."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    if name and name != "HEAD":
        return name
    # Detached HEAD: substitute the short commit hash so the user can
    # tell which detached state they're in at a glance.
    try:
        sh = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=2,
        )
        short = sh.stdout.strip()
    except Exception:
        short = ""
    return f"detached@{short}" if short else "detached"


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $primary 30%;
        color: $text;
        padding: 0 1;
    }
    """

    # Indeterminate scroll: a single block bounces left ↔ right across a
    # 7-cell track. 12 frames; ~80ms per frame → ~1s per round-trip.
    BAR_FRAMES = (
        "▰▱▱▱▱▱▱",
        "▱▰▱▱▱▱▱",
        "▱▱▰▱▱▱▱",
        "▱▱▱▰▱▱▱",
        "▱▱▱▱▰▱▱",
        "▱▱▱▱▱▰▱",
        "▱▱▱▱▱▱▰",
        "▱▱▱▱▱▰▱",
        "▱▱▱▱▰▱▱",
        "▱▱▱▰▱▱▱",
        "▱▱▰▱▱▱▱",
        "▱▰▱▱▱▱▱",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._frame = 0
        # Cached at construction; refreshed by refresh_location() after
        # each turn so that an agent-initiated `git checkout` shows up.
        self._cwd_short = _short_cwd(WORK_DIR)
        self._branch = _detect_git_branch(WORK_DIR)

    def advance_frame(self) -> None:
        self._frame = (self._frame + 1) % len(self.BAR_FRAMES)

    def refresh_location(self) -> None:
        """Recompute cwd + branch. Cheap (one subprocess), but call from
        a background thread to avoid blocking the event loop."""
        self._cwd_short = _short_cwd(WORK_DIR)
        self._branch = _detect_git_branch(WORK_DIR)

    def _location(self) -> str:
        if self._branch:
            return f"{self._cwd_short} · ⎇ {self._branch}"
        return self._cwd_short

    def render_status(
        self,
        counter: TokenCounter,
        *,
        busy: bool = False,
        queued: int = 0,
        steer: int = 0,
    ) -> None:
        location = self._location()
        if counter.turns == 0:
            body = f"{location} · 等待第一轮…"
        else:
            total = counter.prompt_total + counter.completion_total
            thinking = (
                f" / Think {counter.reasoning_total:,}" if counter.reasoning_total else ""
            )
            last_total = counter.last_prompt + counter.last_completion
            last_thinking = (
                f" / Think {counter.last_reasoning:,}" if counter.last_reasoning else ""
            )
            # "Context" = the prompt + completion of the most recent API
            # call. It approximates "what the model just saw" / "what
            # roughly goes into the next call" — i.e. context-window
            # usage, NOT cumulative billing.
            body = (
                f"{location} · Context {last_total:,} "
                f"(in {counter.last_prompt:,} / out {counter.last_completion:,}{last_thinking}) "
                f"· 累计 {total:,} "
                f"(in {counter.prompt_total:,} / out {counter.completion_total:,}{thinking}) "
                f"· {counter.turns} 轮"
            )

        if queued:
            body += f" · 📋 排队 {queued}"
        if steer:
            body += f" · ⚡ steer {steer}"

        if busy:
            t = Text()
            for ch in self.BAR_FRAMES[self._frame]:
                t.append(ch, style="bold cyan" if ch == "▰" else "dim")
            t.append("  ")
            t.append(body)
            self.update(t)
        else:
            self.update(body)


# ───────── app ─────────

class AgentApp(App):
    CSS = """
    Screen { layout: vertical; background: #300A24; }
    Header { height: 1; }
    #body { height: 1fr; layout: horizontal; }
    #main { width: 1fr; layout: vertical; }
    #sidebar {
        width: 36;
        layout: vertical;
        border-left: solid #cba6f7;
        padding: 0 1;
        background: #300A24;
        display: none;
    }
    #sidebar.visible { display: block; }
    #conversation { height: 1fr; padding: 0 1; background: #300A24; }
    #hint { height: 1; padding: 0 1; color: $text-muted; background: #300A24; }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出"),
        Binding("ctrl+l", "clear_chat", "清空对话"),
        Binding("ctrl+t", "toggle_thinking", "展开/折叠思考"),
        Binding("ctrl+x", "cancel_pending", "取消队列/steer"),
        # Double-tap ESC to interrupt. priority=True so the App handler
        # wins over any future widget-level ESC binding (TextArea today
        # doesn't bind escape, so it would bubble anyway — but pin it).
        Binding("escape", "esc", "ESC×2 中断", show=False, priority=True),
        # Steer (Alt/Cmd+Enter inside the input box) is wired via
        # MultilineInput.SteerSubmitted, not an app-level binding.
    ]

    # How long the first ESC stays armed before auto-disarming. Short
    # enough that an accidental press doesn't sit there indefinitely;
    # long enough to be comfortably double-tappable.
    ESC_DOUBLE_WINDOW = 0.6

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.client = AsyncOpenAI(api_key=api_key, base_url=BASE_URL)
        # SYSTEM_PROMPT ends with "当前路径为：" — append cwd at startup so
        # the model knows where it's operating.
        self.messages: list = [
            {"role": "system", "content": SYSTEM_PROMPT + os.getcwd()},
        ]
        # If <cwd>/AGENTS.md exists, append it as a SECOND system message so
        # the framework's identity/tools/language rules stay separate from
        # project-specific guidance — easier to audit later.
        agents_md = load_agents_md(WORK_DIR)
        if agents_md:
            self.messages.append(
                {
                    "role": "system",
                    "content": f"# Project guidelines (from AGENTS.md)\n\n{agents_md}",
                }
            )
        self._agents_md_loaded = agents_md is not None
        self.counter = TokenCounter()
        # Hook: replace this attribute (or override) to gate tools.
        self.tool_confirm: ToolConfirmHook = always_allow
        self._busy = False
        # Follow-bottom state: when True, every mount/chunk-update auto-scrolls
        # the conversation to the end. Toggled by `_handle_scroll_change`
        # which watches the user's scroll position.
        self._follow_bottom = True
        # Single TodoBlock kept in place across todo_tool calls. Reset to
        # None when /clear or Ctrl+L wipes the conversation, or when an
        # "all completed" fade-out finishes.
        self._todo_block: TodoBlock | None = None
        self._todo_dismiss_timer = None  # set_timer handle for fade-out
        # Single BgJobsBlock that mirrors _BG_JOBS into the sidebar.
        # Driven by a 1-second timer started in on_mount.
        self._bg_jobs_block: BgJobsBlock | None = None
        # Message queue: messages submitted while a turn is in flight
        # land here and are consumed in FIFO order after the current
        # turn finishes. Wiped on /clear and on API error (so the user
        # can decide what to do next instead of being railroaded).
        self._queued: list[str] = []
        # Steer: messages submitted via alt/cmd+enter that should be
        # injected mid-turn at the next LLM round boundary (rather than
        # waiting for the whole turn to finish like _queued does).
        # Drained by `_run_one_turn` before each `_stream_one()` call.
        self._steer: list[str] = []
        # Currently-running agent turn worker — kept so ESC×2 can cancel
        # it. None when idle.
        self._agent_worker = None
        # ESC×2 to interrupt: first press arms, second press within
        # ESC_DOUBLE_WINDOW seconds fires _interrupt_now. The disarm
        # timer auto-resets the armed flag if no second press lands.
        self._esc_armed = False
        self._esc_disarm_timer = None

    # ─ compose ─

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield VerticalScroll(id="conversation")
                yield Static(
                    "Enter 发送 · Option+Enter 换行 · Cmd+Enter steer · ESC×2 中断 · Ctrl+X 取消 · Ctrl+L 清空 · Ctrl+C 退出",
                    id="hint",
                )
                yield MultilineInput(id="user-input")
                yield StatusBar(id="status")
            yield Vertical(id="sidebar")

    def on_mount(self) -> None:
        self.title = "DeepSeek Agent"
        sub = f"{MODEL} · effort={REASONING_EFFORT}"
        if self._agents_md_loaded:
            sub += " · AGENTS.md ✓"
        self.sub_title = sub
        self._refresh_status()
        self.query_one("#user-input", MultilineInput).focus()
        # Watch the conversation's scroll position: scrolling up disables
        # auto-follow; scrolling back to the bottom re-enables it. The
        # watcher fires on every scroll_y change, including ones we cause
        # via scroll_end() — that's fine: after scroll_end, scroll_y ==
        # max_scroll_y, so the reading is still "at bottom".
        view = self.query_one("#conversation", VerticalScroll)
        self.watch(view, "scroll_y", self._handle_scroll_change, init=False)
        # Drive the StatusBar's indeterminate scrolling progress while busy.
        # ~80ms per frame; the bar has 12 frames so a full bounce is ~1s.
        self.set_interval(0.08, self._tick_progress_bar)
        # Mirror _BG_JOBS into the sidebar once a second. Cheap — just
        # iterates the dict (≤ ~10 entries) and re-renders one widget.
        self.set_interval(1.0, self._refresh_bg_panel)

    def _tick_progress_bar(self) -> None:
        if not self._busy:
            return
        try:
            bar = self.query_one("#status", StatusBar)
        except Exception:
            return
        bar.advance_frame()
        bar.render_status(
            self.counter,
            busy=True,
            queued=len(self._queued),
            steer=len(self._steer),
        )

    def _refresh_bg_panel(self) -> None:
        """Mirror the _BG_JOBS dict into the sidebar widget. Called once
        per second from on_mount's set_interval. Panel visibility is
        gated on the presence of running jobs — finished jobs stay in
        _BG_JOBS for bash_check/bash_list to find, but they don't keep
        the panel pinned in the sidebar."""
        jobs = list(_BG_JOBS.values())
        running = [j for j in jobs if j.proc.poll() is None]
        try:
            sidebar = self.query_one("#sidebar", Vertical)
        except Exception:
            return
        if not running:
            # Nothing in flight → drop the panel. Don't toggle sidebar
            # visibility class here; TodoBlock owns that.
            if (
                self._bg_jobs_block is not None
                and self._bg_jobs_block.is_mounted
            ):
                self._bg_jobs_block.remove()
            self._bg_jobs_block = None
            return
        if (
            self._bg_jobs_block is None
            or not self._bg_jobs_block.is_mounted
        ):
            self._bg_jobs_block = BgJobsBlock()
            self.call_later(self._mount_bg_block)
            return
        # Render the running jobs plus any siblings that finished while
        # this batch was in flight, so the user sees "3 of 5 done" in
        # context. Once the *last* runner exits we'll unmount on the
        # next tick.
        self._bg_jobs_block.render_jobs(jobs)
        sidebar.add_class("visible")

    async def _mount_bg_block(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", Vertical)
        except Exception:
            return
        if self._bg_jobs_block is None:
            return
        if not self._bg_jobs_block.is_mounted:
            await sidebar.mount(self._bg_jobs_block)
        sidebar.add_class("visible")
        self._bg_jobs_block.render_jobs(list(_BG_JOBS.values()))

    # ─ key bindings / actions ─

    def action_clear_chat(self) -> None:
        # Keep ALL leading system messages (core prompt + optional AGENTS.md).
        # Stop at the first non-system message — anything after that is
        # conversation history that should be wiped.
        keep = []
        for m in self.messages:
            if m["role"] == "system":
                keep.append(m)
            else:
                break
        self.messages = keep
        # Drop any messages that hadn't started running yet.
        self._queued.clear()
        self._steer.clear()
        self._refresh_status()
        view = self.query_one("#conversation", VerticalScroll)
        for child in list(view.children):
            child.remove()
        self._cancel_dismiss()
        if self._todo_block is not None and self._todo_block.is_mounted:
            self._todo_block.remove()
        self._todo_block = None
        # Background jobs are tied to the conversation that spawned them,
        # so a /clear nukes them too. SIGTERM the whole process group
        # (we used start_new_session=True), reap zombies so subsequent
        # poll() calls report exit, then escalate any stragglers to
        # SIGKILL. ~100 ms cap on the wait so /clear stays snappy.
        def _signal_pg(j: BgJob, sig: int) -> None:
            try:
                os.killpg(os.getpgid(j.proc.pid), sig)
            except Exception:
                pass
        for j in _BG_JOBS.values():
            if j.proc.poll() is None:
                _signal_pg(j, signal.SIGTERM)
        for _ in range(20):
            alive = [j for j in _BG_JOBS.values() if j.proc.poll() is None]
            if not alive:
                break
            time.sleep(0.005)
        for j in _BG_JOBS.values():
            if j.proc.poll() is None:
                _signal_pg(j, signal.SIGKILL)
        _BG_JOBS.clear()
        if self._bg_jobs_block is not None and self._bg_jobs_block.is_mounted:
            self._bg_jobs_block.remove()
        self._bg_jobs_block = None
        try:
            self.query_one("#sidebar").remove_class("visible")
        except Exception:
            pass

    # ─ todo sidebar lifecycle ─

    def _start_dismiss(self) -> None:
        """Fade the TodoBlock out over 1.5s, then unmount and hide sidebar.
        Triggered when every TODO item is marked completed."""
        if self._todo_block is None:
            return
        self._todo_block.add_class("all-done")
        self._todo_block.styles.animate("opacity", value=0.0, duration=1.5)
        self._todo_dismiss_timer = self.set_timer(1.5, self._do_dismiss)

    def _cancel_dismiss(self) -> None:
        """Abort a pending fade-out — happens when a fresh todo_tool call
        revives the list before it has finished disappearing."""
        if self._todo_dismiss_timer is not None:
            self._todo_dismiss_timer.stop()
            self._todo_dismiss_timer = None
        if self._todo_block is not None and self._todo_block.is_mounted:
            self._todo_block.styles.opacity = 1.0
            self._todo_block.remove_class("all-done")

    def _do_dismiss(self) -> None:
        """Final step of the fade: actually unmount the widget and hide sidebar."""
        self._todo_dismiss_timer = None
        if self._todo_block is not None and self._todo_block.is_mounted:
            self._todo_block.remove()
        self._todo_block = None
        try:
            self.query_one("#sidebar").remove_class("visible")
        except Exception:
            pass

    def _handle_scroll_change(self, scroll_y: float) -> None:
        """Reactive watcher: keep `_follow_bottom` in sync with where the
        user has scrolled to. +1 tolerance covers float rounding."""
        try:
            view = self.query_one("#conversation", VerticalScroll)
        except Exception:
            return
        self._follow_bottom = scroll_y + 1 >= view.max_scroll_y

    def action_toggle_thinking(self) -> None:
        """Fold every ThinkingBlock if any is currently expanded; else
        unfold every block. One key, mirrors the user's intent."""
        blocks = list(self.query(ThinkingBlock))
        if not blocks:
            return
        target_collapsed = any(not b.collapsed for b in blocks)
        for b in blocks:
            b.collapsed = target_collapsed

    def action_cancel_pending(self) -> None:
        """Ctrl+X: drop everything that hasn't started running yet.

        Wipes BOTH the queue and the steer buffer in one shot — meant
        for "I changed my mind about everything I just typed". Already-
        running tools/streams are not affected by this binding (use
        ESC×2 for that).
        """
        n_q, n_s = len(self._queued), len(self._steer)
        if n_q == 0 and n_s == 0:
            return
        self._queued.clear()
        self._steer.clear()
        self._refresh_status()
        self.notify(
            f"已取消 {n_q} 条排队 + {n_s} 条 steer",
            timeout=2.5,
        )

    # ─ ESC×2 interrupt ─

    def action_esc(self) -> None:
        """First ESC arms the interrupt; second ESC within
        ESC_DOUBLE_WINDOW fires it. Single ESC when nothing is in
        flight is silently ignored so the key feels inert when there's
        nothing to cancel."""
        worker_alive = (
            self._agent_worker is not None and not self._agent_worker.is_finished
        )
        if not worker_alive and not self._queued and not self._steer:
            return
        if self._esc_armed:
            self._esc_armed = False
            self._cancel_disarm_timer()
            self._interrupt_now()
            return
        self._esc_armed = True
        self._cancel_disarm_timer()
        self._esc_disarm_timer = self.set_timer(
            self.ESC_DOUBLE_WINDOW, self._disarm_esc
        )
        self.notify("再按一次 ESC 中断", timeout=self.ESC_DOUBLE_WINDOW)

    def _disarm_esc(self) -> None:
        self._esc_armed = False
        self._esc_disarm_timer = None

    def _cancel_disarm_timer(self) -> None:
        if self._esc_disarm_timer is not None:
            self._esc_disarm_timer.stop()
            self._esc_disarm_timer = None

    def _interrupt_now(self) -> None:
        n_q, n_s = len(self._queued), len(self._steer)
        self._queued.clear()
        self._steer.clear()
        worker = self._agent_worker
        cancelled_turn = False
        if worker is not None and not worker.is_finished:
            worker.cancel()
            cancelled_turn = True
        self._refresh_status()
        if cancelled_turn:
            extra = ""
            if n_q or n_s:
                extra = f"（同时丢弃 {n_q} 条排队 + {n_s} 条 steer）"
            self.notify(f"已中断当前任务{extra}", timeout=2.5)
        elif n_q or n_s:
            self.notify(
                f"已取消 {n_q} 条排队 + {n_s} 条 steer", timeout=2.5
            )

    def _rollback_last_user_turn(self) -> None:
        """Drop the trailing in-flight user turn from message history so
        retrying doesn't leave a half-finished assistant/tool sequence."""
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                del self.messages[i:]
                return

    # ─ input handler ─

    async def on_multiline_input_submitted(
        self, event: "MultilineInput.Submitted"
    ) -> None:
        text = event.value.strip()
        inp = self.query_one("#user-input", MultilineInput)
        if not text:
            return
        if text in ("/exit", "/quit"):
            self.exit()
            return
        if text == "/clear":
            self.action_clear_chat()
            inp.text = ""
            return
        if text == "/compact":
            inp.text = ""
            if self._busy:
                self.notify(
                    "正在跑任务；先 ESC×2 中断或等结束再 /compact",
                    timeout=3,
                )
                return
            # Run on its own worker group so it doesn't fight the agent
            # turn group's exclusive=True semantics.
            self.run_worker(
                self._compact_worker(), exclusive=True, group="compact"
            )
            return
        if text == "/save" or text.startswith("/save "):
            inp.text = ""
            arg = text[5:].strip()
            if not arg:
                await self._mount_widget(Static(Text(
                    "用法：/save <filename>  → 保存到 "
                    "~/.ddtui/history/<filename>.json",
                    style="dim",
                )))
                return
            await self._save_conversation(arg)
            return
        inp.text = ""

        if self._busy:
            # Queue mode: stash the text and let the running turn pick it
            # up at the next safe boundary (between LLM rounds). The
            # bubble mounts immediately so the user sees their message
            # was registered; the status bar shows the queue depth.
            self._queued.append(text)
            await self._mount_widget(UserBubble(text))
            self._refresh_status()
            return

        # New turn: snap back to the bottom — the user just submitted, so
        # they want to see the response.
        self._follow_bottom = True
        await self._mount_widget(UserBubble(text))
        self._agent_worker = self.run_worker(
            self._agent_turn(text), exclusive=True
        )

    async def on_multiline_input_steer_submitted(
        self, event: "MultilineInput.SteerSubmitted"
    ) -> None:
        """Cmd/Ctrl+Enter inside the input box: stash the text into the
        steer buffer; the next LLM round will see it as `[实时插话]`."""
        text = event.value.strip()
        if not text:
            return
        self.query_one("#user-input", MultilineInput).text = ""
        self._steer.append(text)
        await self._mount_widget(SteerBubble(text))
        self._refresh_status()

    # ─ helpers ─

    async def _mount_widget(self, widget) -> None:
        view = self.query_one("#conversation", VerticalScroll)
        await view.mount(widget)
        if self._follow_bottom:
            view.scroll_end(animate=False)

    def _refresh_status(self) -> None:
        try:
            bar = self.query_one("#status", StatusBar)
        except Exception:
            return
        bar.render_status(
            self.counter,
            busy=self._busy,
            queued=len(self._queued),
            steer=len(self._steer),
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_status()

    # ─ /compact ─

    async def _compact_worker(self) -> None:
        """Replace the early portion of message history with a single
        summary message, leaving COMPACT_KEEP_RECENT_TURNS user→assistant
        turns intact at the tail. System messages are preserved.

        On error, the original history is left untouched and a red
        message is mounted asking the user to retry.
        """
        self._set_busy(True)
        try:
            # Find the leading run of system messages — those are kept verbatim.
            prefix_end = 0
            for i, m in enumerate(self.messages):
                if m.get("role") == "system":
                    prefix_end = i + 1
                else:
                    break
            user_idxs = [
                i
                for i, m in enumerate(self.messages)
                if m.get("role") == "user"
            ]
            if len(user_idxs) <= COMPACT_KEEP_RECENT_TURNS:
                await self._mount_widget(
                    Static(Text(
                        f"对话还不够长，无需压缩（user 消息数 ≤ "
                        f"{COMPACT_KEEP_RECENT_TURNS}）。",
                        style="dim",
                    ))
                )
                return
            cut_at = user_idxs[-COMPACT_KEEP_RECENT_TURNS]
            to_compact = self.messages[prefix_end:cut_at]
            keep_recent = self.messages[cut_at:]
            if not to_compact:
                await self._mount_widget(
                    Static(Text("没有可压缩的历史段。", style="dim"))
                )
                return

            await self._mount_widget(
                Static(Text("📦 正在压缩历史…", style="bold cyan"))
            )

            rendered = _render_history_for_summary(to_compact)
            try:
                resp = await self.client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是一个对话历史摘要助手。直接输出中文摘要，"
                                "不要执行任何工具调用，不要回答用户问题，"
                                "不要假装继续对话。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "下面是一段 agent 与用户的对话历史，需要被压缩成简明摘要，"
                                "供后续轮次作为上下文使用。请按以下结构输出（用 markdown 标题）：\n"
                                "## 用户目标\n"
                                "## 已完成的工作\n"
                                "## 改动过的文件 / 关键决定\n"
                                "## 悬而未决的问题或下一步\n\n"
                                "要求：保留具体函数名、文件路径、行号等可定位的事实；"
                                "不要复述完整代码、完整命令输出；"
                                "不要遗漏待办事项。\n\n"
                                f"---\n{rendered}\n---"
                            ),
                        },
                    ],
                    stream=False,
                )
                summary = (resp.choices[0].message.content or "").strip()
                if not summary:
                    raise RuntimeError("摘要返回为空")
            except Exception as e:
                await self._mount_widget(
                    Static(Text(
                        f"❌ 压缩失败：{e}\n请稍后再试 /compact。",
                        style="bold red",
                    ))
                )
                return

            before_n = len(self.messages)
            before_chars = sum(
                len(m.get("content") or "") for m in self.messages
            )
            self.messages = (
                self.messages[:prefix_end]
                + [
                    {
                        "role": "system",
                        "content": (
                            "# 历史摘要（来自 /compact，覆盖此前若干轮对话）\n\n"
                            f"{summary}"
                        ),
                    }
                ]
                + keep_recent
            )
            after_n = len(self.messages)
            after_chars = sum(
                len(m.get("content") or "") for m in self.messages
            )
            saved = max(0, before_chars - after_chars)
            await self._mount_widget(
                Static(Text(
                    f"📦 已压缩：{before_n} → {after_n} 条消息，"
                    f"约 -{saved:,} 字符（保留最近 "
                    f"{COMPACT_KEEP_RECENT_TURNS} 轮原文）",
                    style="bold cyan",
                ))
            )
        finally:
            self._set_busy(False)
            self._refresh_status()

    # ─ /save ─

    async def _save_conversation(self, raw_name: str) -> None:
        """Dump self.messages (plus a small metadata header) to
        HISTORY_DIR / <name>.json. Overwrites silently — the user already
        provided a name, second-guessing them would be annoying."""
        name = raw_name.strip()
        if name.endswith(".json"):
            name = name[:-5]
        name = name.strip()
        if not name:
            await self._mount_widget(Static(Text(
                "错误：文件名不能为空。", style="bold red"
            )))
            return
        # Cheap sanity check before letting Path see the string. This
        # rejects most accidental path-injection patterns with a clearer
        # error than the resolve()/relative_to guard below.
        if "/" in name or "\\" in name or ".." in name:
            await self._mount_widget(Static(Text(
                f"错误：文件名 {raw_name!r} 不能含 / \\ 或 ..",
                style="bold red",
            )))
            return

        try:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            target = HISTORY_DIR / f"{name}.json"
            # Defense in depth: even if the cheap check missed something,
            # make sure the resolved path is still inside HISTORY_DIR.
            resolved = target.resolve()
            try:
                resolved.relative_to(HISTORY_DIR.resolve())
            except ValueError:
                await self._mount_widget(Static(Text(
                    f"错误：解析后路径逃出 history 目录：{resolved}",
                    style="bold red",
                )))
                return

            existed = target.exists()
            payload = {
                "version": 1,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "model": MODEL,
                "messages": self.messages,
            }
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            size = target.stat().st_size
        except Exception as e:
            await self._mount_widget(Static(Text(
                f"❌ 保存失败：{e}", style="bold red"
            )))
            return

        try:
            display = "~/" + str(target.relative_to(Path.home()))
        except ValueError:
            display = str(target)
        note = "（已覆盖原文件）" if existed else ""
        await self._mount_widget(Static(Text(
            f"💾 已保存 {len(self.messages)} 条消息（{size:,} 字节）"
            f"到 {display}{note}",
            style="bold green",
        )))

    # ─ agent loop ─

    async def _agent_turn(self, user_text: str) -> None:
        """Outer loop: drives one or more user turns end-to-end.

        Drains the queue between turns — every iteration commits one
        user message to history, runs `_run_one_turn` to completion (or
        error), and then either pops the next queued message or returns.
        """
        self._set_busy(True)
        pending = user_text
        try:
            while True:
                self.messages.append({"role": "user", "content": pending})
                ok = await self._run_one_turn()
                if not ok:
                    # API error already surfaced + rolled back.
                    # Wipe the queue: don't railroad the user into more
                    # failing turns; let them re-submit if they want.
                    self._queued.clear()
                    self._refresh_status()
                    return
                if not self._queued:
                    return
                pending = self._queued.pop(0)
                # UserBubble for queued messages was mounted at submit
                # time, so we only refresh state here.
                self._follow_bottom = True
                self._refresh_status()
        except asyncio.CancelledError:
            # ESC×2 cancellation: roll back the in-flight turn so retry
            # doesn't see a dangling user/assistant/tool tail, then mark
            # the spot in the conversation. Re-raise so the worker is
            # marked CANCELLED rather than COMPLETED.
            self._rollback_last_user_turn()
            try:
                await self._mount_widget(
                    Static(Text("⛔ 已中断", style="bold yellow"))
                )
            except Exception:
                pass
            raise
        finally:
            self._set_busy(False)
            self._agent_worker = None
            # Refresh location after the turn — the agent may have run
            # `git checkout` or otherwise moved through bash. Off the
            # event loop so a slow git invocation can't stall UI.
            try:
                bar = self.query_one("#status", StatusBar)
                await asyncio.to_thread(bar.refresh_location)
            except Exception:
                pass
            self._refresh_status()

    async def _run_one_turn(self) -> bool:
        """Inner loop: one user→assistant→(tools→assistant)+ turn.

        Returns True if the turn completed normally, False if an API
        error was caught (the error is shown to the UI and the trailing
        user/tool messages are rolled back so the user can retry).
        """
        try:
            while True:
                # Drain any steer messages submitted since the last round.
                # Tagged with [实时插话] so the model can tell this is a
                # mid-turn interjection from the user (and reconsider its
                # plan accordingly), not a fresh user request.
                if self._steer:
                    pending_steer = self._steer
                    self._steer = []
                    for s in pending_steer:
                        self.messages.append(
                            {
                                "role": "user",
                                "content": f"[实时插话] {s}",
                            }
                        )
                    self._refresh_status()

                assistant_msg = await self._stream_one()
                self.messages.append(assistant_msg)
                tool_calls = assistant_msg.get("tool_calls") or []
                if not tool_calls:
                    return True
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw = tc["function"].get("arguments") or "{}"
                    try:
                        args = json.loads(raw)
                    except json.JSONDecodeError:
                        block = ToolCallBlock(name, {"_raw": raw})
                        await self._mount_widget(block)
                        result = f"Error: could not parse arguments JSON: {raw}"
                        block.set_result(result, blocked=True)
                    else:
                        # Special-case todo_tool: maintain a single
                        # TodoBlock widget in the right-hand sidebar.
                        # Fade out + unmount when every item is completed.
                        if name == "todo_tool":
                            items = (
                                args.get("items", [])
                                if isinstance(args, dict)
                                else []
                            )
                            sidebar = self.query_one("#sidebar", Vertical)
                            self._cancel_dismiss()
                            if (
                                self._todo_block is None
                                or not self._todo_block.is_mounted
                            ):
                                self._todo_block = TodoBlock()
                                await sidebar.mount(self._todo_block)
                            self._todo_block.set_items(items)
                            sidebar.add_class("visible")
                            if items and all(
                                i.get("status") == "completed" for i in items
                            ):
                                self._start_dismiss()
                            result = await asyncio.to_thread(
                                execute_tool, name, args
                            )
                            self.messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": result,
                                }
                            )
                            continue

                        block = ToolCallBlock(name, args)
                        await self._mount_widget(block)
                        allowed = await self.tool_confirm(name, args)
                        if allowed:
                            result = await asyncio.to_thread(execute_tool, name, args)
                            # For edit_*, peel the diff out of the result
                            # before it goes into message history: the diff
                            # is shown to the user as a DiffBlock, but
                            # sending it back to the model wastes tokens
                            # (the head summary already says what changed).
                            diff_text = None
                            if name in ("edit_file", "edit_lines", "multi_edit"):
                                head, sep, diff = result.partition("\n\n")
                                if sep and "@@" in diff:
                                    diff_text = diff
                                    result = head
                            block.set_result(result)
                            if diff_text:
                                await self._mount_widget(
                                    DiffBlock(args.get("path", "?"), diff_text)
                                )
                        else:
                            result = "Tool execution blocked by user policy."
                            block.set_result(result, blocked=True)
                    self.messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": result}
                    )
        except Exception as e:
            err = Static(Text(f"API error: {e}", style="bold red"))
            await self._mount_widget(err)
            # roll back the trailing user/tool turn so the user can retry
            for i in range(len(self.messages) - 1, -1, -1):
                if self.messages[i]["role"] == "user":
                    del self.messages[i:]
                    break
            return False

    async def _stream_one(self) -> dict:
        thinking: ThinkingBlock | None = None
        answer: AssistantMessage | None = None
        tool_calls: dict[int, dict] = {}
        final_usage = None

        stream = await self.client.chat.completions.create(
            model=MODEL,
            messages=self.messages,
            tools=TOOLS,
            tool_choice="auto",
            stream=True,
            stream_options={"include_usage": True},
            reasoning_effort=REASONING_EFFORT,
            extra_body={"thinking": {"type": "enabled"}},
        )

        # `_mount_widget` scrolls to the end on mount, but widgets that grow
        # via `update()` (the streaming case) don't trigger scroll on their
        # own — so explicitly follow the bottom on every chunk.
        view = self.query_one("#conversation", VerticalScroll)

        async for chunk in stream:
            if getattr(chunk, "usage", None):
                final_usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            rc = getattr(delta, "reasoning_content", None)
            if rc:
                if thinking is None:
                    thinking = ThinkingBlock()
                    await self._mount_widget(thinking)
                thinking.append_text(rc)
                if self._follow_bottom:
                    view.scroll_end(animate=False)

            text = getattr(delta, "content", None)
            if text:
                if answer is None:
                    answer = AssistantMessage()
                    await self._mount_widget(answer)
                answer.append_text(text)
                if self._follow_bottom:
                    view.scroll_end(animate=False)

            for tc in getattr(delta, "tool_calls", None) or []:
                idx = tc.index if tc.index is not None else 0
                slot = tool_calls.setdefault(
                    idx,
                    {"id": "", "type": "function",
                     "function": {"name": "", "arguments": ""}},
                )
                if tc.id:
                    slot["id"] = tc.id
                if tc.type:
                    slot["type"] = tc.type
                if tc.function:
                    if tc.function.name:
                        slot["function"]["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["function"]["arguments"] += tc.function.arguments

        self.counter.add(final_usage)
        if thinking is not None:
            thinking.finalize(self.counter.last_reasoning)
        self._refresh_status()
        # After the last chunk, max_scroll_y may still grow in the next
        # layout pass — defer a final scroll so we land on the real bottom.
        if self._follow_bottom:
            view.call_after_refresh(view.scroll_end, animate=False)

        msg: dict = {"role": "assistant", "content": answer.text if answer else ""}
        if thinking is not None and thinking.text:
            msg["reasoning_content"] = thinking.text
        if tool_calls:
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return msg


# ───────── entry ─────────

def load_api_key() -> str:
    """Resolve the DeepSeek API key.

    Lookup order:
      1. $DEEPSEEK_API_KEY env var (preferred — works without a file).
      2. The file at $DEEPSEEK_API_KEY_FILE, or ~/deepseek_apikey.txt.
    """
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        key = Path(API_KEY_PATH).read_text().strip()
    except Exception as e:
        raise SystemExit(
            f"No DEEPSEEK_API_KEY env var, and cannot read API key from "
            f"{API_KEY_PATH}: {e}"
        )
    if not key:
        raise SystemExit(f"API key file is empty: {API_KEY_PATH}")
    return key


def load_agents_md(cwd: str) -> str | None:
    """Read `<cwd>/AGENTS.md` and return its content (or None if absent).

    AGENTS.md hosts project-specific guidance for the agent — coding
    conventions, do-not-touch areas, deployment quirks, etc. Loaded once
    at startup; restart the app (or Ctrl+L preserves system messages so a
    re-launch isn't required for in-session reset) to pick up changes.

    Truncated at AGENTS_MD_MAX_CHARS to keep the system prompt bounded.
    """
    p = Path(cwd) / AGENTS_MD_FILENAME
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not text:
        return None
    if len(text) > AGENTS_MD_MAX_CHARS:
        text = (
            text[:AGENTS_MD_MAX_CHARS]
            + f"\n\n…[truncated at {AGENTS_MD_MAX_CHARS} chars]"
        )
    return text


def main() -> None:
    AgentApp(api_key=load_api_key()).run()


if __name__ == "__main__":
    main()
