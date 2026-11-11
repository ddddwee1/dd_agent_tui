"""DeepSeek thinking-mode TUI agent — UI orchestrator.

`AgentApp` owns the top-level Textual shell and wires together mixins for
input handling, history persistence, the parent agent loop, subagents, and
sidebar/UI lifecycle. Tool implementations and pure UI widgets live in
`ddtui.tools` and `ddtui.widgets` respectively.
"""

from __future__ import annotations

import asyncio
import os

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static, TabbedContent, TabPane

from .app_agent_loop import AppAgentLoopMixin
from .app_collab import AppCollabMixin
from .app_confirm import AppConfirmMixin, ToolConfirmHook, always_allow
from .app_explore import AppExploreMixin
from .app_history import AppHistoryMixin
from .app_input import AppInputMixin
from .app_remote import AppRemoteMixin
from .app_subagents import AppSubagentMixin
from .app_support import build_env_block, load_agents_md
from .app_ui import AppUiMixin
from .history_store import HistoryStore, stamp_message
from .config import (
    CONFIRM_WRITES,
    DEFAULT_PROVIDER,
    POST_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from .providers import ProviderConfigError, build_provider
from .runtime_state import new_session_id
from .state import TokenCounter, ToolContext
from .timeline_view import TimelineView
from .ui_theme import WORKBENCH_THEME
from .widgets import (
    CheckpointBlock,
    CollapsedHistoryMarker,
    MultilineInput,
    SlashPopup,
    StatusBar,
    SubagentsBlock,
    TasksBlock,
    ThinkingSummaryPanel,
    TerminalTabPane,
    TodoBlock,
    WorkbenchOverview,
)


class AgentApp(
    AppCollabMixin,
    AppRemoteMixin,
    AppInputMixin,
    AppHistoryMixin,
    AppExploreMixin,
    AppAgentLoopMixin,
    AppSubagentMixin,
    AppConfirmMixin,
    AppUiMixin,
    App,
):
    CSS = """
    Screen { layout: vertical; background: $background; color: $foreground; }
    #workbench-header {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $foreground;
    }
    #workbench-brand { width: 7; color: $primary; text-style: bold; }
    #workbench-project { width: 1fr; text-overflow: ellipsis; }
    #workbench-model {
        width: auto;
        max-width: 55%;
        padding-left: 2;
        color: $text-muted;
        text-overflow: ellipsis;
    }
    #body { height: 1fr; layout: horizontal; }
    #main { width: 1fr; layout: vertical; }
    #sidebar {
        height: 1fr;
        width: 34;
        min-width: 28;
        max-width: 38;
        layout: vertical;
        border-left: solid $border-blurred;
        padding: 0 1;
        background: $surface;
        display: none;
    }
    #sidebar.visible { display: block; }
    #body.sidebar-only > #main { display: none; }
    #body.sidebar-only > #sidebar { width: 1fr; min-width: 0; max-width: 100%; }
    #workbench-empty { height: auto; padding: 1 1; color: $text-muted; }
    #sidebar CheckpointBlock, #sidebar TodoBlock, #sidebar TasksBlock, #sidebar SubagentsBlock {
        border: none;
        border-top: solid $border-blurred;
        padding: 1 1 0 1;
        margin: 1 0;
    }
    #conversation { height: 1fr; padding: 0 2; background: $background; }
    #tabs { height: 1fr; }
    #tabs TabPane { padding: 0; }
    #tabs.single-pane > ContentTabs { display: none; }
    #tabs ContentTabs { background: $background; }
    #composer { height: auto; padding: 0 1; }
    #composer-meta { height: 1; margin-top: 0; }
    #workbench-phase { width: 1fr; color: $text-muted; text-overflow: ellipsis; }
    #toggle-sidebar {
        width: auto;
        min-width: 8;
        height: 1;
        padding: 0 1;
        border: none;
        background: $background;
        color: $text-muted;
    }
    #toggle-sidebar:hover, #toggle-sidebar:focus { color: $primary; text-style: underline; }
    #hint { height: 1; color: $text-muted; background: $background; }
    #pending {
        height: auto;
        max-height: 8;
        background: $surface;
        overflow-y: auto;
    }
    #user-input { background: $panel; color: $foreground; border: solid $border-blurred; }
    #user-input:focus { border: solid $primary; }
    #status-row { height: 1; background: $surface; }
    #status { width: 1fr; background: $surface; color: $text-muted; }
    #timeline-toggle {
        display: none;
        width: auto;
        min-width: 0;
        height: 1;
        padding: 0 1;
        border: none;
        background: $surface;
        color: $primary;
    }
    #timeline-toggle:hover, #timeline-toggle:focus { text-style: underline; }
    """

    BINDINGS = [
        # priority=True so this fires before TextArea's built-in
        # ctrl+c=copy binding. The input is always focused, so without
        # priority the dispatcher hits TextArea's copy first and a
        # widget-level selection (a drag on a Static/Markdown bubble in
        # the conversation) never reaches Screen.action_copy_text.
        Binding("ctrl+c", "copy_selection", "复制选中", priority=True),
        Binding("ctrl+q", "quit", "退出"),
        Binding("ctrl+l", "clear_chat", "清空对话"),
        Binding("ctrl+t", "toggle_thinking", "展开/折叠思考"),
        Binding("ctrl+b", "toggle_sidebar", "展开/收起工作台", priority=True),
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

    def __init__(self, provider_name: str = DEFAULT_PROVIDER) -> None:
        super().__init__()
        self.register_theme(WORKBENCH_THEME)
        self.theme = WORKBENCH_THEME.name
        self._sidebar_open_wide = True
        self._sidebar_open_compact = False
        self._workbench_phase = "idle"
        self._workbench_detail = ""
        self._thinking_summary_text = ""
        try:
            self.provider = build_provider(provider_name)
        except ProviderConfigError as first_error:
            fallback = None if provider_name.strip().lower() in ("codex", "local") else "codex"
            if fallback is not None:
                try:
                    self.provider = build_provider(fallback)
                except ProviderConfigError as fallback_error:
                    raise ProviderConfigError(
                        f"启动 provider {provider_name!r} 不可用：{first_error}; "
                        f"fallback {fallback!r} 也不可用：{fallback_error}"
                    ) from fallback_error
                self._startup_provider_error = (
                    f"启动 provider {provider_name!r} 不可用：{first_error}\n"
                    f"已回退到 {fallback}。"
                )
            else:
                raise
        else:
            self._startup_provider_error = None
        self.provider_name = self.provider.key
        # Mutable copies of the module-level defaults — `/model` and
        # `/effort` slash commands rebind these mid-session so the user
        # can switch without restarting.
        self.model = self.provider.default_model
        self.effort = self.provider.default_effort
        # Per-conversation runtime state: work_dir + task/terminal tables
        # + id allocators. Threaded explicitly through every execute_tool
        # call so a future subagent can be handed its own ctx.
        self._session_id = new_session_id()
        self.ctx = ToolContext(
            work_dir=os.getcwd(),
            session_id=self._session_id,
        )
        # The framework prompt plus a startup snapshot of the
        # environment (OS, cwd, git state, date, provider/model) so the
        # model doesn't have to probe for basics or hallucinate them.
        # The store's identity never changes for the app's lifetime —
        # see the `messages` property below.
        self._history = HistoryStore()
        self.messages = self._build_system_prefix()
        self._active_explore: dict | None = None
        self._explore_next_id = 1
        self.counter = TokenCounter()
        # Hook: gates every tool call before execution. Default policy
        # confirms file-writing tools via a modal (AppConfirmMixin);
        # DDTUI_CONFIRM_WRITES=0 swaps in always_allow. Embedders can
        # replace the attribute with their own hook.
        self._confirm_always: set[str] = set()
        self._confirm_lock = asyncio.Lock()
        self.tool_confirm: ToolConfirmHook = (
            self._confirm_tool_call if CONFIRM_WRITES else always_allow
        )
        self._busy = False
        self._compacting = False
        self._compaction_progress: dict | None = None
        # Follow-bottom state: when True, every mount/chunk-update
        # auto-scrolls the conversation to the end. Toggled by
        # `_handle_scroll_change` which watches the user's scroll position.
        self._follow_bottom = True
        # Single TodoBlock kept in place across todo_tool calls. Reset
        # to None when /clear or Ctrl+L wipes the conversation, or
        # when an "all completed" fade-out finishes.
        self._todo_block: TodoBlock | None = None
        self._todo_dismiss_timer = None  # set_timer handle for fade-out
        # Single CheckpointBlock mirroring ctx.checkpoint. Reset on
        # /clear; restored on /resume replay from the latest
        # checkpoint_tool call.
        self._checkpoint_block: CheckpointBlock | None = None
        # Single TasksBlock that mirrors ctx.tasks into the sidebar.
        self._tasks_block: TasksBlock | None = None
        # Live subagent sessions, keyed by id. spawn_agent inserts one;
        # chat_agent advances the turn counter and messages on the
        # existing entry; end_agent (or the idle reaper) removes it.
        # SubagentsBlock renders the dict at 1 Hz; presence in the dict
        # is the liveness signal — there is no "finished" state.
        self._live_subagents: dict[str, SubagentSession] = {}
        self._subagent_next_id = 1
        self._subagent_block: SubagentsBlock | None = None
        # Terminal PTY tabs, keyed by terminal_id.
        self._terminal_panes: dict[str, TerminalTabPane] = {}
        # Message queue: messages submitted while a turn is in flight
        # land here and are consumed in FIFO order after the current
        # turn finishes. Wiped on /clear and on turn-level runtime error
        # (so the user can decide what to do next instead of being railroaded).
        # Each entry pairs the text with its bubble widget parked in
        # the #pending tray, so consumption / cancel can remove it.
        self._queued: list[tuple[str, "UserBubble"]] = []
        # Steer: messages submitted via alt/cmd+enter that should be
        # injected mid-turn at the next LLM round boundary (rather than
        # waiting for the whole turn to finish like _queued does).
        # Drained by `_run_one_turn` before each `_stream_one()` call.
        self._steer: list[tuple[str, "SteerBubble"]] = []
        # Long-conversation folding. When #conversation gets too many
        # children, _maybe_collapse_history hides the oldest with
        # display=False and stashes them here so the marker's expand
        # callback can restore them. Single marker, single list — refold
        # extends both rather than stacking markers.
        self._collapsed_widgets: list = []
        self._history_marker: "CollapsedHistoryMarker | None" = None
        # Currently-running agent turn worker — kept so ESC×2 can
        # cancel it. None when idle.
        self._agent_worker = None
        # Per-process autosave target. Fresh app launches allocate a
        # distinct file; loading/resuming a saved conversation retargets
        # autosave to that conversation so later edits bump its mtime.
        self._autosave_path = None
        # Managed task completion events waiting to be delivered to
        # the agent. The UI poller appends here; the agent loop drains
        # it before model calls, or auto-wakes from idle.
        self._task_events: list[str] = []
        self._task_event_wake_scheduled = False
        # ESC×2 to interrupt: first press arms, second press within
        # ESC_DOUBLE_WINDOW seconds fires _interrupt_now. The disarm
        # timer auto-resets the armed flag if no second press lands.
        self._esc_armed = False
        self._esc_disarm_timer = None
        self._init_remote_state()
        self._init_collab_state()

    # ─ conversation history ─

    def _build_system_prefix(self) -> list[dict]:
        """Framework prompt + a FRESH environment snapshot + the CURRENT
        AGENTS.md + optional global override.

        Called at construction and by /clear — a new conversation always
        picks up the latest project guidelines, so an external loop can
        edit AGENTS.md and reset the session to try again. The AGENTS.md
        content lands in a SECOND system message so framework rules stay
        separate from project-specific guidance; POST_SYSTEM_PROMPT (if
        set) lands third so it can supersede both.
        """
        prefix = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
                + "\n"
                + build_env_block(
                    self.ctx.work_dir, self.provider.label, self.model
                ),
            },
        ]
        agents_md = load_agents_md(self.ctx.work_dir)
        if agents_md:
            prefix.append(
                {
                    "role": "system",
                    "content": f"# Project guidelines (from AGENTS.md)\n\n{agents_md}",
                }
            )
        self._agents_md_loaded = agents_md is not None
        if POST_SYSTEM_PROMPT:
            prefix.append({"role": "system", "content": POST_SYSTEM_PROMPT})
        return [stamp_message(message) for message in prefix]

    @property
    def messages(self) -> HistoryStore:
        return self._history

    @messages.setter
    def messages(self, value) -> None:
        # Rebinding is the historical footgun: a running TurnEngine
        # holds the object captured at construction, so a fresh list
        # here would strand its subsequent appends (see HistoryStore's
        # module docstring). Assignment therefore MEANS in-place
        # replacement — the store's identity never changes.
        self._history.replace_all(value)

    # ─ compose ─

    def compose(self) -> ComposeResult:
        with Horizontal(id="workbench-header"):
            yield Static("ddtui", id="workbench-brand", markup=False)
            yield Static(os.path.basename(self.ctx.work_dir) or self.ctx.work_dir, id="workbench-project", markup=False)
            yield Static("", id="workbench-model", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="main"):
                # Top-level tab bar: "main" is the parent conversation;
                # each live SubagentSession adds a "sub-N" tab as a
                # read-only transcript view, removed when the session
                # ends or gets idle-reaped. Input box / status bar /
                # sidebar are shared across tabs (always operate on
                # the parent), so the sub tabs are pure observation.
                with TabbedContent(initial="tab-main", id="tabs", classes="single-pane"):
                    with TabPane("主会话", id="tab-main"):
                        yield VerticalScroll(id="conversation")
                        self._timeline_view = TimelineView(id="task-timeline")
                        yield self._timeline_view
            with VerticalScroll(id="sidebar"):
                yield WorkbenchOverview(id="workbench-overview")
                yield ThinkingSummaryPanel(id="thinking-summary")
                yield Static("暂无后台任务\n任务和子 agent 会显示在这里。", id="workbench-empty", markup=False)
        with Vertical(id="composer"):
            with Horizontal(id="composer-meta"):
                yield Static("● 就绪", id="workbench-phase", markup=False)
                yield Button("收起工作台", id="toggle-sidebar", tooltip="Ctrl+B 展开 / 收起工作台")
            yield Vertical(id="pending")
            popup = SlashPopup(id="slash-popup")
            popup.arg_candidates = self._slash_arg_candidates
            yield popup
            user_input = MultilineInput(id="user-input")
            user_input.border_title = "消息 · 输入 / 查看命令"
            yield user_input
            yield Static("", id="hint", markup=False)
        with Horizontal(id="status-row"):
            yield Button("会话耗时", id="timeline-toggle", tooltip="查看会话时间线")
            yield StatusBar(self.ctx.work_dir, id="status")


def main() -> None:
    try:
        app = AgentApp(provider_name=DEFAULT_PROVIDER)
    except ProviderConfigError as e:
        raise SystemExit(
            "Provider configuration error: "
            f"{e}\n"
            "请设置 DEEPSEEK_API_KEY / DEEPSEEK_API_KEY_FILE，"
            "或先运行 `codex login` 后用 DDTUI_PROVIDER=codex；"
            "本地服务使用 DDTUI_PROVIDER=local、DDTUI_LOCAL_BASE_URL 和 DDTUI_LOCAL_MODEL。"
        ) from e
    app.run()


if __name__ == "__main__":
    main()
