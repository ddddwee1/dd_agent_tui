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


def _env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


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

# Managed async tasks. Task output/status files are intentionally under
# /tmp like background bash logs: they are runtime artifacts, not repo
# state. task_* keeps them longer than bash_start logs because a
# completion notification may arrive after the agent has moved on.
TASK_OUTPUT_DIR = Path("/tmp")
TASK_MAX_CONCURRENT = 5
TASK_RETENTION_SECONDS = 3600
TASK_DEFAULT_TAIL_LINES = 80
TASK_DEFAULT_WAIT_TIMEOUT = 60
TASK_MAX_WAIT_TIMEOUT = 600
TASK_READ_DEFAULT_CHARS = 12_000
TASK_READ_MAX_CHARS = 100_000
TASK_EVENT_TAIL_LINES = 40
TASK_EVENT_MAX_CHARS = 12_000

# Project-local notes. By default each repo gets <repo>/.ddtui/notes,
# but users can point DDTUI_PROJECT_NOTES_DIR elsewhere for personal
# notes that should not live in the checkout.
PROJECT_NOTES_DIR = os.environ.get("DDTUI_PROJECT_NOTES_DIR", "").strip()
PROJECT_NOTE_BODY_MAX_CHARS = 20_000
PROJECT_NOTE_SEARCH_SNIPPET_CHARS = 600
PROJECT_NOTE_DEFAULT_SEARCH_LIMIT = 5
PROJECT_NOTE_DEFAULT_LIST_LIMIT = 20


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

# Tool sandbox. File-writing tools may edit arbitrary paths by default
# so the agent can work across repos. Set
# DDTUI_ALLOW_UNSANDBOXED_WRITES=0 to restore the old project-only
# write/edit restriction. Bash workdirs remain under
# ToolContext.work_dir unless DDTUI_ALLOW_UNSANDBOXED_BASH=1 is set.
ALLOW_UNSANDBOXED_WRITES = _env_flag(
    "DDTUI_ALLOW_UNSANDBOXED_WRITES", default=True
)
ALLOW_UNSANDBOXED_BASH = _env_flag(
    "DDTUI_ALLOW_UNSANDBOXED_BASH", default=False
)


# ───────── system prompt ─────────

SYSTEM_PROMPT = (
    "你可以使用下列tools: bash, bash_start, bash_check, bash_wait, bash_kill, bash_list, task_start, task_check, task_read, task_wait, task_kill, task_list, checkpoint_tool, checkpoint_get, project_note_add, project_note_search, project_note_list, project_note_read, project_note_update, project_note_delete, read_file, write_file, apply_patch, edit_file, edit_lines, multi_edit, list_files, glob_files, search_content, web_fetch, web_search, todo_tool, spawn_agent, chat_agent, await_agent, end_agent. "
    "修改已有文件时，优先使用 apply_patch、edit_file、edit_lines 或 multi_edit 做增量编辑；write_file 主要用于新建文件或用户明确要求整文件覆盖；不要为了改文件而用 bash 拼接重定向，除非现有编辑工具无法完成。"
    "子 agent 是异步的：spawn_agent 创建会话并立即返回 session_id（status=running），背后的子 agent 在后台跑；chat_agent 在已有会话发新 prompt，同样立即返回。两者都不直接给答案——必须用 await_agent(session_id) 拿结果（默认 60s 超时，超时返回 still running 让你再次 await）。end_agent 释放会话。要并行就一次发多个 spawn_agent，再分别 await_agent。子 agent 跨轮保留记忆（含 reasoning），同一任务别反复 spawn。"
    "后台 shell 与托管任务的区别：bash_start 只是原始后台 shell，适合简单后台进程；如果是长时间的测试、构建、下载、训练、profile 或任何完成状态很重要的工作，优先使用 task_start。task_start 会输出 output_path/status_path；notify_on_complete=true 时任务结束会向你发送异步完成通知。严格规则：notify_on_complete=true 的任务，不要因为“没事可做，只是在等完成”而调用 task_wait；做完其他有用工作后，应使用 checkpoint_tool 记录等待状态（如有帮助），然后结束/暂停当前回复，等待通知自动唤醒。只有用户明确要求阻塞、或任务明显快结束且只做一次短暂有界等待时，才为 notify_on_complete=true 调用 task_wait。task_wait 正常用于 notify_on_complete=false 的任务。"
    "遇到项目特定命令、环境、测试流程、远端机器、架构约定不确定时，先用 project_note_search 查询项目笔记；笔记不是绝对事实，注意 source/confidence，必要时用文件或命令验证。记录可复用项目事实时优先搜索并 project_note_update 相关旧笔记，避免新增近似重复笔记；不要保存 secret、token、密码或大段原始日志。"
    "对于多步骤任务，先用 todo_tool 列出计划（pending）；开始一项时把它标 in_progress；完成立刻标 completed 并继续下一项。todo_tool 管执行清单；checkpoint_tool 管当前工作状态（目标、焦点、证据、决定、blocker、active task/subagent refs、下一步）；project_note_* 管长期项目知识。启动 task_start 后准备做别的事或暂停等通知、debug 出现多假设、做了关键设计决定、即将结束但工作未完成、上下文变长或 resume 后不确定状态时，使用 checkpoint_tool；不确定当前状态时用 checkpoint_get。"
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
POST_SYSTEM_PROMPT += "注意：不要猜测，遇到不清楚的概念一定要多去仓库里翻文档和代码，一定要多看多查，不要凭自己猜测。"
