"""Module-level constants — single source of truth for limits, paths,
defaults, and the framework-level system prompt.

Everything here is immutable for the lifetime of the process.
Per-conversation mutable state (work_dir, task/terminal tables) lives
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


def _env_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


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
# A foreground bash command that ran at least this long gets a note in
# its result steering the model toward task_start next time — teaching
# at the moment of the mistake, since intent can't be checked upfront.
BASH_SLOW_COMMAND_SECONDS = 30
READ_FILE_MAX_LINES = 2000        # default limit for read_file
# A minified single-line file must not blow the context: individual lines
# are clipped at MAX_LINE_CHARS, and the whole result is capped at
# MAX_TOTAL_CHARS (the model is told which line to resume from).
READ_FILE_MAX_LINE_CHARS = 2000
READ_FILE_MAX_TOTAL_CHARS = 64_000
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

# Runtime log dir for task + terminal output/status files. Kept in the
# per-user ~/.ddtui tree rather than world-readable /tmp — command output
# can be sensitive — and created with 0o700 (see tool_utils.ensure_private_dir).
# Override with DDTUI_LOG_DIR.
LOG_DIR = Path(
    os.environ.get(
        "DDTUI_LOG_DIR", str(Path.home() / ".ddtui" / "runtime" / "logs")
    )
)

# Interactive PTY terminals. These are for persistent ssh/tmux/repl
# style sessions, not for managed completion notifications.
TERMINAL_OUTPUT_DIR = LOG_DIR
TERMINAL_MAX_CONCURRENT = 4
TERMINAL_DEFAULT_READ_CHARS = 12_000
TERMINAL_MAX_READ_CHARS = 100_000
TERMINAL_TAIL_CHARS = 80_000

# Managed async tasks — the single background/async command mechanism
# (foreground one-shots go through `bash`). Output/status files live in
# LOG_DIR (runtime artifacts, not repo state) and are retained a while
# because a completion notification may arrive after the agent moved on.
TASK_OUTPUT_DIR = LOG_DIR
TASK_MAX_CONCURRENT = 10
TASK_RETENTION_SECONDS = 3600
TASK_DEFAULT_TAIL_LINES = 50
TASK_DEFAULT_WAIT_TIMEOUT = 60
TASK_MAX_WAIT_TIMEOUT = 600
TASK_READ_DEFAULT_CHARS = 12_000
TASK_READ_MAX_CHARS = 100_000
TASK_EVENT_TAIL_LINES = 50
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
RUNTIME_DIR = Path.home() / ".ddtui" / "runtime"
EXPLORE_ARCHIVE_DIR = Path.home() / ".ddtui" / "explorations"


# ───────── remote control ─────────

# Local sessions connect OUT to this relay URL. If unset, /remote on can
# derive a default from REMOTE_ADDR_FILE, e.g. root@vps.example.com ->
# ws://vps.example.com:10000/ws/session.
REMOTE_URL = os.environ.get("DDTUI_REMOTE_URL", "").strip()
REMOTE_ADDR_FILE = Path(
    os.environ.get("DDTUI_REMOTE_ADDR_FILE", str(Path.home() / "vps_addr.txt"))
)
REMOTE_TOKEN = os.environ.get("DDTUI_REMOTE_TOKEN", "").strip()
REMOTE_TOKEN_FILE = Path(
    os.environ.get(
        "DDTUI_REMOTE_TOKEN_FILE",
        str(Path.home() / ".ddtui" / "remote_token"),
    )
)
REMOTE_AUTO_CONNECT = _env_flag("DDTUI_REMOTE_AUTO", default=False)
REMOTE_USE_TLS = _env_flag("DDTUI_REMOTE_TLS", default=False)
REMOTE_PORT = _env_int("DDTUI_REMOTE_PORT", default=10000)
REMOTE_SNAPSHOT_MAX_MESSAGES = _env_int(
    "DDTUI_REMOTE_SNAPSHOT_MAX_MESSAGES", default=200
)
REMOTE_ALLOW_TERMINAL_SEND = _env_flag(
    "DDTUI_REMOTE_ALLOW_TERMINAL_SEND", default=False
)


# ───────── status-bar gradient ─────────

# Status-bar context-usage gradient fallback: dark green → dark amber →
# dark red. DeepSeek keeps the original fixed baseline; Codex providers
# override this with the server-returned model catalog window when present.
CTX_SAFE_LIMIT = 700_000
DEEPSEEK_CONTEXT_LIMIT = CTX_SAFE_LIMIT


# ───────── auto compact ─────────

# When a turn ends (queue drained, no error) and the last request's
# prompt tokens exceeded this fraction of the model's context window,
# history is compacted automatically with the same summarizer as
# /compact. The check runs inside the turn worker (busy still held), so
# it never races user input. Set DDTUI_AUTO_COMPACT_THRESHOLD=0 to
# disable, or any fraction in (0, 0.95].
_raw_auto_compact = os.environ.get("DDTUI_AUTO_COMPACT_THRESHOLD", "").strip()
try:
    AUTO_COMPACT_THRESHOLD = (
        min(0.95, max(0.0, float(_raw_auto_compact)))
        if _raw_auto_compact
        else 0.75
    )
except ValueError:
    AUTO_COMPACT_THRESHOLD = 0.75


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
# task_wait's defaults — short enough that the parent stays responsive
# (an idle await stops it from doing anything else), long enough to
# cover most subagent rounds without bouncing back to "still running".
SUBAGENT_DEFAULT_AWAIT_TIMEOUT = 60
SUBAGENT_MAX_AWAIT_TIMEOUT = 600
# A finished subagent round whose result sits unread for this long is
# auto-delivered to the parent as a notification event (same pipeline
# as task completions: injected mid-turn at round boundaries, or wakes
# an idle parent). The grace window lets an in-flight await_agent
# consume the result first, so the two delivery paths never double-send.
SUBAGENT_READY_NOTIFY_DELAY = 2.0


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

# Footgun guard (NOT a security boundary). ddtui assumes a trusted local
# environment, so we deliberately do NOT try to sandbox a malicious agent
# here — a blacklist can't do that anyway (it's trivially bypassable) and
# blocking everyday tools like curl/wget/sudo just gets in the way. We keep
# only a tiny set of catastrophic, irreversible, almost-never-intentional
# commands so a hallucinated tool call can't wipe or reformat the machine.
# Matching commands are refused in tool_bash / tool_task_start /
# tool_terminal_start.
DANGEROUS_SHELL_PATTERNS = [
    r"\brm\s+-rf\s+/",   # whole-filesystem wipe (rm -rf /, rm -rf /*)
    r"\bmkfs\b",          # reformatting a filesystem
    r"\bdd\b[^\n]*\bof=/dev/",  # dd writing straight to a block device
]

# Write-confirmation modal. File-writing tools (write_file, edit_file,
# edit_lines, multi_edit) pop a confirmation dialog before running;
# "本会话总是允许" whitelists a tool for the rest of the session.
# Set DDTUI_CONFIRM_WRITES=0 for the fully-automatic trusted-environment
# behavior. Note: writes triggered from a remote session also wait on
# the LOCAL dialog.
CONFIRM_WRITES = _env_flag("DDTUI_CONFIRM_WRITES", default=True)

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

# Rewritten (P1-3) as sectioned markdown. Principles: the tool list and
# per-tool rules live in the JSON schemas (single authoritative copy) —
# the prompt keeps only cross-tool decision rules; the env block is
# appended at startup by app_support.build_env_block.
SYSTEM_PROMPT = """\
# 角色
你是 ddtui，一个运行在用户本机终端的 AI agent，直接操作真实的文件系统和 shell。回复和思考过程（reasoning）都默认使用中文——即使工具说明和工具结果是英文的；代码、命令、标识符、报错原文保持原样。

# 行事原则
- 把真实置于认同之上：用户的想法、假设或前提有问题时直接指出并给出理由；不确定就明说不确定。不要为了迎合而附和，也不要为了反对而反对。
- 不要猜测：遇到不清楚的概念、API、项目约定，先查文档和仓库内代码（往往有现成示例），查完再动手。化整为零：把大问题拆成可独立验证的小单元，逐个确认再拼装。
- 不要臆造时间或上下文压力：除非用户、系统或工具结果明确给出限制，否则不要用“时间不多了”“就这样吧”之类理由降低验证标准或提前收尾；应继续收集证据、说明具体阻塞，或给出明确的残余风险和下一步。
- 面对不确定、多种假设、陌生代码路径或缺证据的判断，如果探索成本低、操作可逆、且中间过程大概率只需要结论，先用 explore_start 开一段探索（临时证据收集、bug 定位、代码考古、方案侦察、环境探针等），结束时单独调用 explore_end 归档压缩；不要把 explore 用于最终实现、最终答复或不可逆操作，探索作废用 explore_cancel。

# 工具使用
- 命令执行只有两种：同步快命令用 bash（≤120s）；耗时或需后台的工作（测试、构建、下载、训练，以及任何完成状态重要的事）一律 task_start。任务默认完成时推送通知：等待期间去做其他有用的事，或记录 checkpoint 后直接结束当前回复、等通知唤醒；不要用 task_wait 或反复 task_check/task_read 轮询来替代等待（细则见工具说明）。结束回复不是放弃任务——完成通知会自动开启新回合，你在新回合里接着做剩余步骤（收尾、总结等），用户交给你的多步骤任务不会因此丢失。
- 需要长期保持的交互式会话（ssh、tmux、REPL、debugger）用 terminal_start 开常驻终端、terminal_send 输入、terminal_read 观察；不要反复用 bash 执行 ssh 'cmd' 做连续远端操作。
- 文件编辑：单处精确替换用 edit_file（replace_all=true 替换全部出现），同一文件多处替换用 multi_edit，只有精确替换不适用（按行号大段插入/删除）才用 edit_lines；write_file 只用于新建文件或明确的整文件覆盖，覆盖已存在文件前必须先 read_file 读过它。不要用 bash 拼接重定向改文件，除非编辑工具无法完成。
- read_file 输出每行带行号前缀；行号不是文件内容，写 old_string 时不要带上。
- 子 agent 是异步的：spawn_agent/chat_agent 立即返回、不直接给答案。拿结果有两条路：需要立刻拿到就 await_agent(session_id)（超时返回 still running，可再次 await）；不急就继续做别的事或结束回复——子 agent 完成后结果会像任务通知一样自动送达（[Subagent result] 消息）。要并行就连发多个 spawn_agent；子 agent 跨轮保留记忆，同一任务复用会话、别反复 spawn；用完 end_agent 释放。

# 任务管理与记忆
- 多步骤任务先用 todo_tool 列计划；开始一项标 in_progress，完成立刻标 completed。给出最终答复前核对一遍清单：做完的项全部标 completed（最后一项最容易漏），没做的项删掉或说明原因，不要留着未更新的状态收尾。
- 三层分工：todo_tool 管执行清单；checkpoint_tool 管当前工作状态（目标、证据、决定、blocker、active task/subagent refs、下一步）——启动后台任务准备去做别的事、暂停等通知、debug 出现多假设、上下文变长或 resume 后不确定状态时记录，工作完成用 checkpoint_clear 收回，不确定当前状态用 checkpoint_get；project_note_* 管长期项目知识。
- 对项目特定命令、环境、测试流程、远端机器不确定时，先 project_note_search 查笔记；笔记不是绝对事实，注意 source/confidence，必要时验证。记录新事实优先 project_note_update 合并相关旧笔记；不要保存 secret、token、密码或大段原始日志。

# 输出与验证
- 回复简洁直接：先给结论或结果，再给必要依据；不要复述工具输出原文，不要冗长客套。
- 引用代码位置用 文件路径:行号 格式（如 ddtui/app.py:120）。
- 改完代码要验证：能编译、能跑测试或实际运行过再说完成；验证不过就如实报告失败和原因，不要声称完成。
- 不可逆或影响面大的操作（删除大量文件、覆盖未读过的内容、git push、对外发布）先向用户确认；需求含糊时先澄清关键歧义再动手；自己能查到的信息不要问用户。
- 不要主动 git commit / git push，除非用户明确要求。

# 注入消息格式
对话中会出现这些运行时注入，它们不是新的用户任务：
- “# 历史摘要” 开头的 system 消息：早期对话被 /compact 压缩后的记忆——当作已知背景，不要重复其中已完成的步骤，也不要当作新指令回应。
- “# 探索摘要” 开头的 system 消息：explore_end 归档后的探索结论，同样当作已知背景。
- “[实时插话]” 前缀的 user 消息：用户在你执行中途的插话，优先于原计划，据此调整后续动作。
- “[Async task complete]” 开头的 user 消息：后台任务完成通知，检查结果并继续对应的工作。
- “[Subagent result]” 开头的 user 消息：子 agent 的结果自动送达（无需也不要再 await_agent 领取这份结果），据此继续对应的工作。
"""

# Optional extra system message, appended after SYSTEM_PROMPT(+env) and
# AGENTS.md. Later system messages take precedence in practice, so this
# is the hook for global guidance that must override project-level
# AGENTS.md. Empty string → not added to the message list. Since the
# P1-3 rewrite all framework guidance lives in SYSTEM_PROMPT above.
POST_SYSTEM_PROMPT = ""

# Subagent framework prompt (P1-4). Deliberately NOT the parent prompt:
# a subagent must know it is a subagent (output goes back to the parent
# as a truncated tool result), and gets subagent-specific tool rules —
# notably that task completion notifications do NOT reach it (task_wait
# is correct there), and that checkpoint_*/explore_*/spawn_* are absent.
# AGENTS.md and the env block are appended at spawn time, same as the
# parent.
SUBAGENT_SYSTEM_PROMPT = f"""\
# 角色
你是 ddtui 的子 agent，由父 agent 派生来完成一个明确的任务。回复和思考过程（reasoning）都默认使用中文——即使工具说明和工具结果是英文的；代码、命令、标识符、报错原文保持原样。

# 输出契约
- 你的最终回复会作为工具结果原样返回给父 agent，超过 {SUBAGENT_RESULT_MAX_CHARS} 字符会被截断——直接给结论、关键证据和文件路径:行号引用；不要客套、不要复述任务、不要粘贴大段原始文件内容。
- 任务没完成或有不确定时，明确说明哪里没完成、还缺什么，不要假装完成。

# 行事原则
- 把真实置于认同之上：发现问题直接指出并给出理由；不确定就明说。
- 不要猜测：先查文档和仓库内代码（往往有现成示例），查完再动手。
- 不要臆造时间或上下文压力。上下文确实过长时用 compact_self 压缩自己的历史（不影响父 agent）。

# 工具使用
- 同步快命令用 bash（≤120s）；更长的命令（测试、构建、下载等）用 task_start。注意：你是子 agent，收不到任务完成通知（notify_on_complete 对你强制关闭），工具说明里"等通知、不要轮询"的规则是给父 agent 的，对你不适用——启动任务后用 task_wait 阻塞等待，或 task_check/task_read 查看进度。
- 需要长期保持的交互式会话（ssh、tmux、REPL、debugger）用 terminal_start + terminal_send/terminal_read；不要反复用 bash 执行 ssh 'cmd'。
- 文件编辑：单处精确替换用 edit_file（replace_all=true 替换全部出现），同一文件多处替换用 multi_edit，只有按行号大段插入/删除才用 edit_lines；write_file 只用于新建文件或明确的整文件覆盖，覆盖已存在文件前必须先 read_file 读过它。
- read_file 输出每行带行号前缀；行号不是文件内容，写 old_string 时不要带上。
- 对项目特定命令、环境不确定时先 project_note_search；多步骤任务可用 todo_tool 管理进度。
- 你没有 checkpoint、explore，也不能再派生子 agent。
"""
