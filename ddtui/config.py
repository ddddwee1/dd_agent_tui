"""Module-level constants — single source of truth for limits, paths,
defaults, and the framework-level system prompt.

Everything here is immutable for the lifetime of the process.
Per-conversation mutable state (work_dir, background job table) lives
in `ToolContext` (see `ddtui.state`) and is threaded explicitly through
tool calls.
"""

from __future__ import annotations

import os
from pathlib import Path


# ───────── API / model ─────────

API_KEY_PATH = os.environ.get(
    "DEEPSEEK_API_KEY_FILE", str(Path.home() / "deepseek_apikey.txt")
)
BASE_URL = "https://api.deepseek.com"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "max")


# ───────── tool tunables ─────────

BASH_TIMEOUT = 120
BASH_OUTPUT_MAX_CHARS = 10_000   # truncate huge stdout/stderr
READ_FILE_MAX_LINES = 2000        # default limit for read_file
AGENTS_MD_FILENAME = "AGENTS.md"
AGENTS_MD_MAX_CHARS = 16_000      # cap project guidelines so prompt stays sane
WEB_FETCH_TIMEOUT = 20
WEB_FETCH_MAX_BYTES = 2_000_000   # cap raw download (~2 MB)
WEB_FETCH_MAX_CHARS = 10_000      # cap returned text (matches bash output cap)
WEB_FETCH_HARD_CHAR_CAP = 50_000  # absolute upper bound when caller overrides

# Background bash jobs.
BG_JOB_LOG_DIR = Path("/tmp")
BG_MAX_CONCURRENT = 5             # cap on simultaneously-running jobs
BG_RETENTION_SECONDS = 300        # keep finished jobs in dict this long
BG_DEFAULT_WAIT_TIMEOUT = 60      # bash_wait default
BG_MAX_WAIT_TIMEOUT = 600         # absolute upper bound on a single wait call
BG_DEFAULT_TAIL_LINES = 50


# ───────── /save target ─────────

# Per-user, not per-project — conversations are tied to the model
# session, not the cwd, so a single global history dir is the natural
# fit.
HISTORY_DIR = Path.home() / ".ddtui" / "history"


# ───────── status-bar gradient ─────────

# Status-bar context-usage gradient: dark green → dark amber → dark red,
# scaled against this "safe" last-call (prompt + completion) total.
# DeepSeek's true upper bound is higher; this is the visual baseline at
# which the bar should already be saturated red so the user feels
# pressure to /compact well before an actual context overflow.
CTX_SAFE_LIMIT = 512_000


# ───────── /compact ─────────

# How much of each tool result to keep when rendering history for the
# summarizer. Tool results are often the bulk of token weight; the
# model only needs a flavor of what came back, not the full body.
COMPACT_TOOL_SNIPPET_CHARS = 400
# How many recent user→assistant turns to leave verbatim. The compactor
# replaces everything earlier with a single summary message.
COMPACT_KEEP_RECENT_TURNS = 2


# ───────── subagent ─────────

# Cap on the number of LLM rounds inside one spawn_agent call. Keeps a
# wedged or looping subagent from burning unbounded tokens before
# control returns to the parent.
SUBAGENT_MAX_TURNS = 30
# Truncate the subagent's final answer before it lands as a tool_result
# in the parent — a chatty subagent shouldn't be able to blow out the
# parent's context window in one shot.
SUBAGENT_RESULT_MAX_CHARS = 20_000


# ───────── safety ─────────

# Dangerous shell patterns (blacklist). Matching commands are blocked
# in tool_bash / tool_bash_start.
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


# ───────── system prompt ─────────

SYSTEM_PROMPT = (
    "你可以使用下列tools: bash, bash_start, bash_check, bash_wait, bash_kill, bash_list, read_file, write_file, edit_file, edit_lines, multi_edit, list_files, glob_files, search_content, web_fetch, todo_tool, spawn_agent. "
    "对于多步骤任务，先用 todo_tool 列出计划（pending）；开始一项时把它标 in_progress；完成立刻标 completed 并继续下一项。"
    "如果上下文中出现以 \"# 历史摘要\" 开头的 system 消息，那是早期对话被 /compact 压缩后的记忆——请把它当作已知背景，不要重复其中已完成的步骤，也不要把它当作新指令来回应。"
    "请使用中文思考，用户可以看见思考过程，所以思考过程也要使用中文。请使用中文思考，无论AGENTS.md是以什么语言写的，请使用中文思考，无论AGENTS.md是以什么语言写的。请使用中文思考，无论后续文件是以什么语言写的。请使用中文思考，无论后续文件是以什么语言写的。"
    "如果发现自己在用英文思考，请提醒自己，并立即切回中文。"
    "当前路径（供你后续调用命令作参考）："
)
