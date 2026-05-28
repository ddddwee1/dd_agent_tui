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


# ───────── API / model providers ─────────

API_KEY_PATH = os.environ.get(
    "DEEPSEEK_API_KEY_FILE", str(Path.home() / "deepseek_apikey.txt")
)
BASE_URL = "https://api.deepseek.com"
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")
REASONING_EFFORT = os.environ.get("DEEPSEEK_REASONING_EFFORT", "max")

DEFAULT_PROVIDER = os.environ.get("DDTUI_PROVIDER", "deepseek").strip().lower()

DEEPSEEK_API_KEY_PATH = Path(API_KEY_PATH)
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", BASE_URL)
DEEPSEEK_MODEL = MODEL
DEEPSEEK_REASONING_EFFORT = REASONING_EFFORT

CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
CODEX_AUTH_PATH = Path(
    os.environ.get("CODEX_AUTH_FILE", str(CODEX_HOME / "auth.json"))
)
CODEX_MODELS_CACHE_PATH = Path(
    os.environ.get("CODEX_MODELS_CACHE_FILE", str(CODEX_HOME / "models_cache.json"))
)
CODEX_BASE_URL = os.environ.get(
    "CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex"
).rstrip("/")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.5")
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "xhigh")
CODEX_REFRESH_TOKEN_URL = os.environ.get(
    "CODEX_REFRESH_TOKEN_URL", "https://auth.openai.com/oauth/token"
)
CODEX_OAUTH_CLIENT_ID = os.environ.get(
    "CODEX_OAUTH_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann"
)


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

# Web search (Brave). Key file is one line, ASCII; missing → web_search
# returns a clear error rather than crashing.
WEB_SEARCH_API_KEY_PATH = os.environ.get(
    "BRAVE_API_KEY_FILE", str(Path.home() / "brave_apikey.txt")
)
WEB_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
WEB_SEARCH_DEFAULT_COUNT = 5
WEB_SEARCH_MAX_COUNT = 20         # Brave per-request cap
WEB_SEARCH_TIMEOUT = 15
WEB_SEARCH_SNIPPET_MAX_CHARS = 240  # truncate per-result description

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

# Status-bar context-usage gradient fallback: dark green → dark amber →
# dark red. DeepSeek keeps the original fixed baseline; Codex providers
# override this with the server-returned model catalog window when present.
CTX_SAFE_LIMIT = 512_000
DEEPSEEK_CONTEXT_LIMIT = CTX_SAFE_LIMIT


# ───────── /compact ─────────

# How much of each tool result to keep when rendering history for the
# summarizer. Tool results are often the bulk of token weight; the
# model only needs a flavor of what came back, not the full body.
COMPACT_TOOL_SNIPPET_CHARS = 400
# How many recent user→assistant turns to leave verbatim. The compactor
# replaces everything earlier with a single summary message.
COMPACT_KEEP_RECENT_TURNS = 2


# ───────── subagent ─────────

# Truncate the subagent's answer before it lands as a tool_result in
# the parent — a chatty subagent shouldn't be able to blow out the
# parent's context window in one shot.
SUBAGENT_RESULT_MAX_CHARS = 20_000
# Maximum number of live subagent sessions kept in memory at once.
# spawn_agent at the cap returns an error asking the parent to
# end_agent one first. A small bound is fine — long-tail use is the
# parent driving one or two subagents through several rounds, not a
# bushy fan-out.
MAX_LIVE_SUBAGENTS = 5
# Auto-end a subagent that's been idle (no chat / no end) for this
# long. Prevents a forgotten session from holding bash processes /
# context indefinitely. Reset on every chat_agent call.
SUBAGENT_IDLE_TIMEOUT_SEC = 600
# How often the idle-reaper scan runs. Coarse on purpose — the only
# work to do is "kill timed-out sessions", not worth running often.
SUBAGENT_REAP_INTERVAL_SEC = 60
# Default and absolute-max wait on a single await_agent call. Mirrors
# bash_wait's defaults — short enough that the parent stays responsive
# (an idle await stops it from doing anything else), long enough to
# cover most subagent rounds without bouncing back to "still running".
SUBAGENT_DEFAULT_AWAIT_TIMEOUT = 60
SUBAGENT_MAX_AWAIT_TIMEOUT = 600


# ───────── conversation view ─────────

# Long conversations pile up widgets in #conversation; Textual lays out
# every visible child each refresh, so >100 mounted bubbles/tool-blocks
# noticeably slow scroll + streaming. After each turn finishes we hide
# (display=False) the oldest widgets until only COLLAPSED_HISTORY_KEEP
# remain visible, mounting a single CollapsedHistoryMarker the user can
# click to restore everything at once. Underlying message history
# (self.messages) is untouched — only the rendered widget tree shrinks.
MAX_VISIBLE_HISTORY = 80
COLLAPSED_HISTORY_KEEP = 40


# ───────── safety ─────────

# Dangerous shell patterns (blacklist). Matching commands are blocked
# in tool_bash / tool_bash_start. This is defense-in-depth; the tool
# layer also keeps shell workdirs inside the project by default.
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

# Tool sandbox. By default, file-writing tools and bash workdirs must
# stay under ToolContext.work_dir. Read/search/web tools keep their old
# behavior (read arbitrary paths) so the user can inspect files outside
# the repo when needed; writes and shell execution are the dangerous
# operations. Set DDTUI_ALLOW_UNSANDBOXED_WRITES=1 or
# DDTUI_ALLOW_UNSANDBOXED_BASH=1 to opt out.
ALLOW_UNSANDBOXED_WRITES = (
    os.environ.get("DDTUI_ALLOW_UNSANDBOXED_WRITES", "").strip().lower()
    in ("1", "true", "yes", "on")
)
ALLOW_UNSANDBOXED_BASH = (
    os.environ.get("DDTUI_ALLOW_UNSANDBOXED_BASH", "").strip().lower()
    in ("1", "true", "yes", "on")
)


# ───────── system prompt ─────────

SYSTEM_PROMPT = (
    "你可以使用下列tools: bash, bash_start, bash_check, bash_wait, bash_kill, bash_list, read_file, write_file, apply_patch, edit_file, edit_lines, multi_edit, list_files, glob_files, search_content, web_fetch, web_search, todo_tool, spawn_agent, chat_agent, await_agent, end_agent. "
    "修改已有文件时，优先使用 apply_patch、edit_file、edit_lines 或 multi_edit 做增量编辑；write_file 主要用于新建文件或用户明确要求整文件覆盖；不要为了改文件而用 bash 拼接重定向，除非现有编辑工具无法完成。"
    "子 agent 是异步的：spawn_agent 创建会话并立即返回 session_id（status=running），背后的子 agent 在后台跑；chat_agent 在已有会话发新 prompt，同样立即返回。两者都不直接给答案——必须用 await_agent(session_id) 拿结果（默认 60s 超时，超时返回 still running 让你再次 await）。end_agent 释放会话。要并行就一次发多个 spawn_agent，再分别 await_agent。子 agent 跨轮保留记忆（含 reasoning），同一任务别反复 spawn。"
    "对于多步骤任务，先用 todo_tool 列出计划（pending）；开始一项时把它标 in_progress；完成立刻标 completed 并继续下一项。"
    "如果上下文中出现以 \"# 历史摘要\" 开头的 system 消息，那是早期对话被 /compact 压缩后的记忆——请把它当作已知背景，不要重复其中已完成的步骤，也不要把它当作新指令来回应。"
    "当前路径（供你后续调用命令作参考）："
)

# Optional THIRD system message, appended after SYSTEM_PROMPT(+cwd) and
# AGENTS.md. Use it for global guidance that should win over project-level
# AGENTS.md (since later system messages take precedence in practice).
# Empty string → not added to the message list.
POST_SYSTEM_PROMPT = "请使用中文思考，用户可以看见思考过程，所以请使用中文思考，无论前面的prompt是以什么语言写的。请使用中文思考，无论后续文件是以什么语言写的。请使用中文思考，无论后续文件是以什么语言写的。请使用中文思考，无论后续文件是以什么语言写的。请使用中文思考，无论后续文件是以什么语言写的。如果发现自己在用英文思考，请提醒自己，并立即切回中文。 "
POST_SYSTEM_PROMPT += "解决问题的思路 - 化整为零：将大问题拆分为多个小问题，将复杂问题分为多个简单单元的拼装。于是，你可以对多个小单元进行逐个测试，在保证每个单元都正常执行的情况下，再进行顺序拼接。"
POST_SYSTEM_PROMPT += "第一性原理：将大问题化为多个小问题，将复杂问题分为多个简单单元的拼装。"
POST_SYSTEM_PROMPT += "查询文档的思路 - 深度优先：先快速浏览目录结构，找到最相关的章节，再深入阅读细节。不要吝啬于查文档，即使你觉得自己已经知道了；文档里往往有重要细节和示例，能帮你避免走弯路。"