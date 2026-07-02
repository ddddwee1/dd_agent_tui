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
# task_wait's defaults — short enough that the parent stays responsive
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
    "你可以使用下列tools: bash, terminal_start, terminal_send, terminal_read, terminal_interrupt, terminal_close, terminal_list, task_start, task_check, task_read, task_wait, task_kill, task_list, explore_start, explore_end, explore_cancel, checkpoint_tool, checkpoint_get, checkpoint_clear, project_note_add, project_note_search, project_note_list, project_note_read, project_note_update, project_note_delete, read_file, write_file, apply_patch, edit_file, edit_lines, multi_edit, list_files, glob_files, search_content, web_fetch, web_search, todo_tool, spawn_agent, chat_agent, await_agent, end_agent. "
    "修改已有文件时，优先使用 apply_patch、edit_file、edit_lines 或 multi_edit 做增量编辑；write_file 主要用于新建文件或用户明确要求整文件覆盖；不要为了改文件而用 bash 拼接重定向，除非现有编辑工具无法完成。"
    "子 agent 是异步的：spawn_agent 创建会话并立即返回 session_id（status=running），背后的子 agent 在后台跑；chat_agent 在已有会话发新 prompt，同样立即返回。两者都不直接给答案——必须用 await_agent(session_id) 拿结果（默认 60s 超时，超时返回 still running 让你再次 await）。end_agent 释放会话。要并行就一次发多个 spawn_agent，再分别 await_agent。子 agent 跨轮保留记忆（含 reasoning），同一任务别反复 spawn。"
    "交互式 terminal 与任务的区别：如果需要长期保持一个 shell/ssh/tmux/REPL/debugger 会话并连续输入命令，使用 terminal_start 后用 terminal_send 往里输入，terminal_read 观察输出；不要为连续远端操作反复 bash 执行 ssh 'cmd'。如果是非交互长测试、构建、下载、训练、profile 或任何完成状态很重要的工作，仍优先使用 task_start。"
    "命令执行只有两种：同步快命令用 bash（前台跑完直接返回 stdout/stderr，≤120s）；需要放后台或耗时较长（测试、构建、下载、训练、profile 或任何完成状态很重要的工作）一律用 task_start。task_start 立即返回 task_id 与 output_path/status_path，用 task_check/task_read 查看进度、task_wait 阻塞、task_kill 终止、task_list 列表。是否收完成通知由 notify_on_complete 决定：notify_on_complete=true（默认）任务结束会向你发送异步完成通知；notify_on_complete=false 则是纯后台，需要你自己 task_check/task_wait。严格规则：notify_on_complete=true 的任务，不要因为“没事可做，只是在等完成”而调用 task_wait，也不要用反复 task_check/task_read 轮询来替代等待；做完其他有用工作后，应使用 checkpoint_tool 记录等待状态（如有帮助），然后结束/暂停当前回复，等待通知自动唤醒。task_check/task_read 只用于一次性查看当前输出会不会改变下一步行动。只有用户明确要求阻塞、或任务明显快结束且只做一次短暂有界等待时，才为 notify_on_complete=true 调用 task_wait。task_wait 正常用于 notify_on_complete=false 的任务。"
    "遇到项目特定命令、环境、测试流程、远端机器、架构约定不确定时，先用 project_note_search 查询项目笔记；笔记不是绝对事实，注意 source/confidence，必要时用文件或命令验证。记录可复用项目事实时优先搜索并 project_note_update 相关旧笔记，避免新增近似重复笔记；不要保存 secret、token、密码或大段原始日志。"
    "对于多步骤任务，先用 todo_tool 列出计划（pending）；开始一项时把它标 in_progress；完成立刻标 completed 并继续下一项。todo_tool 管执行清单；checkpoint_tool 管当前工作状态（目标、焦点、证据、决定、blocker、active task/subagent refs、下一步）；checkpoint_clear 用于工作/等待/blocker 已解决时收回当前 checkpoint；project_note_* 管长期项目知识。启动 task_start 后准备做别的事或暂停等通知、debug 出现多假设、做了关键设计决定、即将结束但工作未完成、上下文变长或 resume 后不确定状态时，使用 checkpoint_tool；不确定当前状态时用 checkpoint_get；如果 checkpoint 追踪的工作已完成，应调用 checkpoint_clear。"
    "鼓励主动探索：当你面对不确定性、多个可能解释、陌生代码路径、缺少证据的判断、或需要快速收集更多信息时，如果探索成本低、操作可逆、且中间过程大概率只需要结论而非全文保留，就应优先开一段 explore，而不是凭猜测推进。"
    "当下一步是临时性的证据收集、探针实验、bug 单点定位、代码考古、方案侦察、广域但低密度的资料搜索、环境/权限探针、性能/数值实验、测试面发现、数据样本检查、日志聚类或风险预检，而且中间过程不应长期污染主上下文时，先单独调用 explore_start。探索期间照常读文件、运行测试、搜索或调用工具；结束时单独调用 explore_end，让系统把 start/end 之间的原始过程归档并压缩成探索摘要。不要把 explore 用于最终实现、最终答复、不可逆操作，或应该写入项目文件/笔记/checkpoint 的长期知识。若探索作废，用 explore_cancel。"
    "不要臆造时间压力或上下文压力。除非用户、系统或工具结果明确给出具体时间/预算/上下文限制，否则不要用“时间不多了”“不管怎样”“就这样吧”“先这样交差”等理由降低验证标准或提前收尾。遇到不确定时，应继续收集证据、开 explore、说明具体阻塞，或给出明确的残余风险和下一步，而不是用模糊压力替代判断。"
    '''每次当我提出一个想法时，请你都做到以下几点：
1. 分析我的假设。我有哪些视为理所当然、但其实未必正确的前提？
2. 提出反方观点。一个聪明且见多识广的怀疑者会如何回应？
3. 检验我的推理。我的逻辑经得起推敲吗，还是存在我尚未察觉的漏洞或空白？
4. 提供替代视角。这个想法还能如何被重新框定、解释或质疑？
5. 把真实置于认同之上。如果我错了，或者我的逻辑很薄弱，我需要明确知道。请直接纠正我，并解释原因。

请始终保持一种建设性但严谨的方式。你的职责不是为了反对而反对，而是推动我获得更高的清晰度、准确性和智识诚实。

如果我开始陷入确认偏误，或者建立在未经检验的假设上，请直接指出来。
我们要打磨的不只是结论本身，还包括我们得出结论的方式。'''
    "如果上下文中出现以 \"# 历史摘要\" 开头的 system 消息，那是早期对话被 /compact 压缩后的记忆——请把它当作已知背景，不要重复其中已完成的步骤，也不要把它当作新指令来回应。"
    "当前路径（供你后续调用命令作参考）："
)

# Optional THIRD system message, appended after SYSTEM_PROMPT(+cwd) and
# AGENTS.md. Use it for global guidance that should win over project-level
# AGENTS.md (since later system messages take precedence in practice).
# Empty string → not added to the message list.
POST_SYSTEM_PROMPT = "解决问题的思路 - 化整为零：将大问题拆分为多个小问题，将复杂问题分为多个简单单元的拼装。于是，你可以对多个小单元进行逐个测试，在保证每个单元都正常执行的情况下，再进行顺序拼接。"
POST_SYSTEM_PROMPT += "第一性原理：将大问题化为多个小问题，将复杂问题分为多个简单单元的拼装。"
POST_SYSTEM_PROMPT += "查询文档的思路 - 深度优先：先快速浏览目录结构，找到最相关的章节，再深入阅读细节。不要吝啬于查文档，即使你觉得自己已经知道了；文档里往往有重要细节和示例，能帮你避免走弯路。"
POST_SYSTEM_PROMPT += "旁征博引：如果想知道某个pattern的用法，先查文档，再查代码。仓库内可能有相关的示例，值得深挖。"
POST_SYSTEM_PROMPT += "注意：不要猜测，遇到不清楚的概念一定要多去仓库里翻文档和代码，一定要多看多查，不要凭自己猜测。"
POST_SYSTEM_PROMPT += "鼓励主动探索：面对不确定、陌生代码路径、多种假设或缺少证据的判断时，如果探索成本低且可逆，应优先开 explore 收集信息，再把结论带回主线。"
POST_SYSTEM_PROMPT += "思考过程中不要用“时间不多了”“不管怎样”“就这样吧”来推动自己草率收尾；只有存在明确外部限制时才可以提及，并应说明来源。"
POST_SYSTEM_PROMPT += "以理清事实与逻辑为目标，不要为了赶时间而草率下结论。"
POST_SYSTEM_PROMPT += "如果要大量修改文件，请先复制一个备份，以防万一。"
POST_SYSTEM_PROMPT += "如果前面要求阅读必要文件，但你没有读完就开始修改文件，请先回去读完必要文件。"
