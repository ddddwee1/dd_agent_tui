"""DeepSeek thinking-mode TUI agent — UI orchestrator.

`AgentApp` owns the top-level Textual shell and wires together mixins for
input handling, history persistence, the parent agent loop, subagents, and
sidebar/UI lifecycle. Tool implementations and pure UI widgets live in
`ddtui.tools` and `ddtui.widgets` respectively.
"""

from __future__ import annotations

import os

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Static, TabbedContent, TabPane

from .app_agent_loop import AppAgentLoopMixin
from .app_confirm import ToolConfirmHook, always_allow
from .app_explore import AppExploreMixin
from .app_history import AppHistoryMixin
from .app_input import AppInputMixin
from .app_remote import AppRemoteMixin
from .app_subagents import AppSubagentMixin
from .app_support import load_agents_md
from .app_ui import AppUiMixin
from .config import DEFAULT_PROVIDER, POST_SYSTEM_PROMPT, SYSTEM_PROMPT
from .providers import ProviderConfigError, build_provider
from .runtime_state import new_session_id
from .state import TokenCounter, ToolContext
from .widgets import (
    CheckpointBlock,
    CollapsedHistoryMarker,
    MultilineInput,
    SlashPopup,
    StatusBar,
    SubagentsBlock,
    TasksBlock,
    TerminalTabPane,
    TodoBlock,
)


class AgentApp(
    AppRemoteMixin,
    AppInputMixin,
    AppHistoryMixin,
    AppExploreMixin,
    AppAgentLoopMixin,
    AppSubagentMixin,
    AppUiMixin,
    App,
):
    CSS = """
    Screen { layout: vertical; background: #272822; color: #f8f8f2; }
    Header { height: 1; background: #3e3d32; color: #f8f8f2; }
    #body { height: 1fr; layout: horizontal; }
    #main { width: 1fr; layout: vertical; }
    #sidebar {
        width: 26%;
        min-width: 30;
        max-width: 60;
        layout: vertical;
        border-left: solid #ae81ff;
        padding: 0 1;
        background: #272822;
        display: none;
    }
    #sidebar.visible { display: block; }
    #conversation { height: 1fr; padding: 0 1; background: #272822; }
    /* TabbedContent host: take all remaining vertical space inside
       #main, and let each TabPane occupy its full area without an
       inner padding (the panes themselves set their own padding). */
    #tabs { height: 1fr; }
    #tabs TabPane { padding: 0; }
    #hint { height: 1; padding: 0 1; color: #a59f85; background: #272822; }
    /* Pending tray: parks queued/steer bubbles above the input box
       until the turn consumes them. height: auto + max-height keeps
       it invisible when empty. */
    #pending {
        height: auto;
        max-height: 8;
        padding: 0 1;
        background: #2d2e27;
        overflow-y: auto;
    }
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
        try:
            self.provider = build_provider(provider_name)
        except ProviderConfigError as first_error:
            fallback = None if provider_name.strip().lower() == "codex" else "codex"
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
        # SYSTEM_PROMPT ends with "当前路径为：" — append cwd at startup
        # so the model knows where it's operating.
        self.messages: list = [
            {"role": "system", "content": SYSTEM_PROMPT + self.ctx.work_dir},
        ]
        # If <cwd>/AGENTS.md exists, append it as a SECOND system
        # message so the framework's identity/tools/language rules stay
        # separate from project-specific guidance — easier to audit.
        agents_md = load_agents_md(self.ctx.work_dir)
        if agents_md:
            self.messages.append(
                {
                    "role": "system",
                    "content": f"# Project guidelines (from AGENTS.md)\n\n{agents_md}",
                }
            )
        self._agents_md_loaded = agents_md is not None
        # Optional THIRD system message — global override that lands after
        # AGENTS.md so it can correct or supersede project-level guidance.
        if POST_SYSTEM_PROMPT:
            self.messages.append(
                {"role": "system", "content": POST_SYSTEM_PROMPT}
            )
        self._active_explore: dict | None = None
        self._explore_next_id = 1
        self.counter = TokenCounter()
        # Hook: replace this attribute (or override) to gate tools.
        # Default policy allows every tool call; embedders can swap in
        # a stricter hook (allowlist, modal, per-tool rules, etc.).
        self.tool_confirm: ToolConfirmHook = always_allow
        self._busy = False
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

    # ─ compose ─

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="main"):
                # Top-level tab bar: "main" is the parent conversation;
                # each live SubagentSession adds a "sub-N" tab as a
                # read-only transcript view, removed when the session
                # ends or gets idle-reaped. Input box / status bar /
                # sidebar are shared across tabs (always operate on
                # the parent), so the sub tabs are pure observation.
                with TabbedContent(initial="tab-main", id="tabs"):
                    with TabPane("main", id="tab-main"):
                        yield VerticalScroll(id="conversation")
                yield Static(
                    "Enter 发送 · Option+Enter 换行 · Cmd+Enter steer · 点击下方排队/steer 气泡可编辑/删除 · ESC×2 中断 · Ctrl+X 全部取消 · Ctrl+L 清空 · Ctrl+Q 退出",
                    id="hint",
                )
                yield Vertical(id="pending")
                yield SlashPopup(id="slash-popup")
                yield MultilineInput(id="user-input")
                yield StatusBar(self.ctx.work_dir, id="status")
            yield Vertical(id="sidebar")


def main() -> None:
    try:
        app = AgentApp(provider_name=DEFAULT_PROVIDER)
    except ProviderConfigError as e:
        raise SystemExit(
            "Provider configuration error: "
            f"{e}\n"
            "请设置 DEEPSEEK_API_KEY / DEEPSEEK_API_KEY_FILE，"
            "或先运行 `codex login` 后用 DDTUI_PROVIDER=codex。"
        ) from e
    app.run()


if __name__ == "__main__":
    main()
