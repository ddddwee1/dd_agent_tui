"""UI lifecycle, sidebar, queue, and keyboard-action mixin for AgentApp."""

from __future__ import annotations

import time

from rich.text import Text
from rich.cells import cell_len
from textual.containers import Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import Button, Static, TabbedContent, TabPane

from .config import (
    COLLAPSED_HISTORY_KEEP,
    CONFIG_FILE,
    CONFIG_FILE_ERROR,
    CONFIG_FILE_KEY_COUNT,
    CONFIG_FILE_UNUSED_KEYS,
    MAX_VISIBLE_HISTORY,
    SUBAGENT_IDLE_TIMEOUT_SEC,
    SUBAGENT_REAP_INTERVAL_SEC,
)
from .history_store import stamp_message
from .runtime_state import new_session_id
from .thinking_summary import cancel_thinking_summaries
from .timeline_view import TimelineView, format_duration
from .ui_theme import UI
from .state import (
    SubagentSession,
    TokenCounter,
    kill_all_tasks,
    kill_all_terminals,
)
from .tools_tasks import collect_task_events
from .tools_terminal import drain_terminal_output
from .widgets import (
    CheckpointBlock,
    CollapsedHistoryMarker,
    EditPendingScreen,
    MultilineInput,
    StatusBar,
    SteerBubble,
    SubagentsBlock,
    SubagentTabPane,
    TasksBlock,
    TerminalTabPane,
    ThinkingBlock,
    ThinkingSummaryPanel,
    ToolCallBlock,
    UserBubble,
    WorkbenchOverview,
)


class AppUiMixin:
    def on_mount(self) -> None:
        self.title = "ddtui"
        self._update_subtitle()
        self._refresh_status()
        self._refresh_workspace_tabs()
        self.query_one("#user-input", MultilineInput).focus()
        self.call_later(self._autosave_conversation)
        self.call_later(self._show_disconnect_recovery_notice)
        if self._startup_provider_error:
            self.call_later(self._show_startup_provider_error)
        if CONFIG_FILE_ERROR or CONFIG_FILE_KEY_COUNT:
            self.call_later(self._show_config_file_notice)
        # Watch the conversation's scroll position: scrolling up
        # disables auto-follow; scrolling back to the bottom re-enables
        # it. The watcher fires on every scroll_y change, including
        # ones we cause via scroll_end() — that's fine: after
        # scroll_end, scroll_y == max_scroll_y, so the reading is still
        # "at bottom".
        view = self.query_one("#conversation", VerticalScroll)
        self.watch(view, "scroll_y", self._handle_scroll_change, init=False)
        # Drive the StatusBar's indeterminate scrolling progress while
        # busy. ~80ms per frame; the bar has 12 frames so a full bounce
        # is ~1s.
        self.set_interval(0.08, self._tick_progress_bar)
        # Managed async task monitor. Completion notification delivery
        # is handled separately below; this is only the visual sidebar.
        self.set_interval(1.0, self._refresh_task_panel)
        # Live PTY terminal tabs. These are long-lived interactive
        # sessions such as ssh/tmux/repl.
        self.set_interval(0.5, self._refresh_terminal_tabs)
        # Same cadence for the subagent monitor — phase changes happen
        # on millisecond scales but the user only needs a coarse "what's
        # it doing" pulse.
        self.set_interval(1.0, self._refresh_subagent_panel)
        # Managed async tasks can notify the agent when they complete.
        # Polling is cheap and avoids a thread per task.
        self.set_interval(1.0, self._poll_task_notifications)
        # Idle-reaper: end_agent any session that hasn't seen a
        # chat_agent call in SUBAGENT_IDLE_TIMEOUT_SEC. Coarse interval
        # — the only work to do is "kill timed-out sessions", not worth
        # running often.
        self.set_interval(
            SUBAGENT_REAP_INTERVAL_SEC, self._reap_idle_subagents
        )
        if hasattr(self, "_maybe_start_remote_from_config"):
            self.call_later(self._maybe_start_remote_from_config)

    def on_unmount(self) -> None:
        cancel_thinking_summaries(self)
        client = getattr(self, "_remote_client", None)
        if client is not None:
            client.stop()

    def _poll_task_notifications(self) -> None:
        checkpoint_before = self.ctx.checkpoint
        events = collect_task_events(self.ctx)
        if checkpoint_before is not self.ctx.checkpoint:
            self.call_later(self._sync_checkpoint_block)
            self.call_later(self._autosave_conversation)
        if events:
            self._task_events.extend(events)
            self.notify(
                f"{len(events)} 个异步任务事件，已通知 agent",
                timeout=4,
            )
        sub_task_events = self._poll_subagent_task_events()
        if sub_task_events:
            self.notify(
                f"{sub_task_events} 个子 agent 任务事件，已唤醒对应会话",
                timeout=4,
            )
        # Unread subagent results ride the same delivery pipeline as
        # task completions: injected at round boundaries while busy, or
        # waking an idle parent via _start_task_event_turn below.
        sub_events = self._collect_ready_subagent_events()
        if sub_events:
            self._task_events.extend(sub_events)
            self.notify(
                f"{len(sub_events)} 个子 agent 结果已投递给 agent",
                timeout=4,
            )
        if not self._task_events:
            return
        worker_alive = (
            self._agent_worker is not None and not self._agent_worker.is_finished
        )
        if self._busy or worker_alive or self._task_event_wake_scheduled:
            return
        self._task_event_wake_scheduled = True
        self.call_later(self._start_task_event_turn)

    async def _show_startup_provider_error(self) -> None:
        await self._mount_widget(Static(Text(
            f"⚠ {self._startup_provider_error}",
            style="bold yellow",
        )))

    async def _show_config_file_notice(self) -> None:
        if CONFIG_FILE_ERROR:
            await self._mount_widget(Static(Text(
                f"⚠ 配置文件 {CONFIG_FILE} 解析失败：{CONFIG_FILE_ERROR}"
                "（本次忽略，仅使用环境变量与内置默认）",
                style="bold yellow",
            )))
            return
        await self._mount_widget(Static(Text(
            f"⚙ 已加载配置文件 {CONFIG_FILE}（{CONFIG_FILE_KEY_COUNT} 项）",
            style="dim",
        )))
        if CONFIG_FILE_UNUSED_KEYS:
            await self._mount_widget(Static(Text(
                "⚠ 配置文件中有未识别的键（检查拼写或是否已废弃）："
                + ", ".join(CONFIG_FILE_UNUSED_KEYS),
                style="bold yellow",
            )))

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
            context_limit=self._context_limit(),
            busy=True,
            compacting=self._compacting,
            queued=len(self._queued),
            steer=len(self._steer),
            explore=self._active_explore is not None,
        )
        self._refresh_workbench_caption()

    def _refresh_task_panel(self) -> None:
        """Mirror ctx.tasks into the sidebar widget.

        The sidebar is a live monitor, so it only shows tasks that are
        currently running. Finished tasks remain available via task
        notifications and task_check/task_list.
        """
        tasks = list(self.ctx.tasks.values())
        running = [task for task in tasks if task.proc.poll() is None]
        try:
            sidebar = self.query_one("#sidebar", VerticalScroll)
        except Exception:
            return
        if not running:
            if (
                self._tasks_block is not None
                and self._tasks_block.is_mounted
            ):
                self._tasks_block.remove()
            self._tasks_block = None
            self._refresh_sidebar_visibility()
            return
        if (
            self._tasks_block is None
            or not self._tasks_block.is_mounted
        ):
            self._tasks_block = TasksBlock()
            self.call_later(self._mount_task_block)
            return
        self._tasks_block.render_tasks(running)
        self._refresh_sidebar_visibility()

    async def _mount_task_block(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", VerticalScroll)
        except Exception:
            return
        if self._tasks_block is None:
            return
        if not self._tasks_block.is_mounted:
            await sidebar.mount(self._tasks_block)
        self._refresh_sidebar_visibility()
        running = [
            task for task in self.ctx.tasks.values()
            if task.proc.poll() is None
        ]
        self._tasks_block.render_tasks(running)

    def _refresh_subagent_panel(self) -> None:
        """Mirror _live_subagents into the sidebar widget. Panel exists
        iff at least one session is alive (running OR idle waiting for
        the next chat_agent call). end_agent / the idle reaper remove
        sessions from the dict, which is what unmounts the panel."""
        sessions = list(self._live_subagents.values())
        try:
            sidebar = self.query_one("#sidebar", VerticalScroll)
        except Exception:
            return
        if not sessions:
            if (
                self._subagent_block is not None
                and self._subagent_block.is_mounted
            ):
                self._subagent_block.remove()
            self._subagent_block = None
            self._refresh_sidebar_visibility()
            return
        if (
            self._subagent_block is None
            or not self._subagent_block.is_mounted
        ):
            self._subagent_block = SubagentsBlock()
            self.call_later(self._mount_subagent_block)
            return
        self._subagent_block.render_sessions(sessions)
        self._refresh_sidebar_visibility()
        # Same cadence: walk every mounted SubagentTabPane and append
        # any new messages that landed since the last refresh. Cheap
        # when nothing's new (early-out inside refresh_from_session).
        for pane in self.query(SubagentTabPane):
            try:
                pane.refresh_from_session()
            except Exception:
                pass

    async def _mount_subagent_block(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", VerticalScroll)
        except Exception:
            return
        if self._subagent_block is None:
            return
        if not self._subagent_block.is_mounted:
            await sidebar.mount(self._subagent_block)
        self._refresh_sidebar_visibility()
        self._subagent_block.render_sessions(
            list(self._live_subagents.values())
        )

    def _refresh_terminal_tabs(self) -> None:
        """Drain PTY output and keep terminal tabs in sync."""
        active_ids = set(self.ctx.terminals.keys())
        for tid in list(self._terminal_panes.keys()):
            if tid not in active_ids:
                self._terminal_panes.pop(tid, None)
                self.call_later(self._remove_terminal_tab, tid)

        for term in list(self.ctx.terminals.values()):
            try:
                drained = drain_terminal_output(term)
            except Exception:
                drained = 0
                pass
            if drained:
                try:
                    self._remote_emit(
                        "terminal.tail",
                        {
                            "terminal_id": term.id,
                            "output_size": term.output_size,
                            "tail": term.tail[-4000:],
                        },
                    )
                except Exception:
                    pass
            pane = self._terminal_panes.get(term.id)
            if pane is None:
                pane = term.pane if term.pane is not None else TerminalTabPane(term)
                term.pane = pane
                self._terminal_panes[term.id] = pane
                self.call_later(self._add_terminal_tab, term.id)
                continue
            if pane.is_mounted:
                try:
                    pane.refresh_from_session()
                except Exception:
                    pass

    async def _add_terminal_tab(self, terminal_id: str) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return
        term = self.ctx.terminals.get(terminal_id)
        pane = self._terminal_panes.get(terminal_id)
        if term is None or pane is None:
            return
        pane_id = f"tab-{terminal_id}"
        try:
            existing = tabs.query_one(f"#{pane_id}", TabPane)
        except Exception:
            existing = None
        if existing is not None:
            return
        label = f"{terminal_id}:{term.name[:12]}"
        await tabs.add_pane(TabPane(label, pane, id=pane_id))
        self._refresh_workspace_tabs()

    async def _remove_terminal_tab(self, terminal_id: str) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return
        try:
            await tabs.remove_pane(f"tab-{terminal_id}")
        except Exception:
            pass
        self._refresh_workspace_tabs()

    async def _sync_checkpoint_block(self) -> None:
        """Mirror ctx.checkpoint into the sidebar."""
        try:
            sidebar = self.query_one("#sidebar", VerticalScroll)
        except Exception:
            return
        checkpoint = self.ctx.checkpoint
        if not checkpoint:
            if (
                self._checkpoint_block is not None
                and self._checkpoint_block.is_mounted
            ):
                self._checkpoint_block.remove()
            self._checkpoint_block = None
            self._refresh_sidebar_visibility()
            return
        if (
            self._checkpoint_block is None
            or not self._checkpoint_block.is_mounted
        ):
            self._checkpoint_block = CheckpointBlock()
            await sidebar.mount(self._checkpoint_block)
        self._checkpoint_block.set_checkpoint(checkpoint)
        self._refresh_sidebar_visibility()

    def _refresh_sidebar_visibility(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", VerticalScroll)
        except Exception:
            return
        try:
            self.query_one("#workbench-overview", WorkbenchOverview)
            body = self.query_one("#body")
        except Exception:
            sidebar.set_class(any(child.is_mounted and child.display for child in sidebar.children), "visible")
            return
        compact = getattr(self, "_workbench_width", self.size.width) < 100
        opened = self._sidebar_open_compact if compact else self._sidebar_open_wide
        sidebar.set_class(opened, "visible")
        body.set_class(compact and opened, "sidebar-only")
        empty = self.query_one("#workbench-empty", Static)
        empty.display = not any(
            child.is_mounted and child.display
            and child.id not in {"workbench-overview", "workbench-empty"}
            for child in sidebar.children
        )
        toggle = self.query_one("#toggle-sidebar", Button)
        timeline = getattr(self, "_timeline_view", None)
        back = "返回时间线" if timeline is not None and timeline.display else "返回对话"
        label = (back if compact else "收起工作台") if opened else "工作台"
        if str(toggle.label) != label:
            toggle.label = label

    def action_toggle_sidebar(self) -> None:
        if getattr(self, "_workbench_width", self.size.width) < 100:
            self._sidebar_open_compact = not self._sidebar_open_compact
        else:
            self._sidebar_open_wide = not self._sidebar_open_wide
        self._refresh_workbench()
        self.query_one("#user-input", MultilineInput).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "toggle-sidebar":
            event.stop()
            self.action_toggle_sidebar()
        elif event.button.id == "timeline-toggle":
            event.stop()
            view = self._timeline_view
            if view.timeline is not None:
                self._set_timeline_open(not self._timeline_is_visible())

    def _timeline_is_visible(self) -> bool:
        tabs = self.query_one("#tabs", TabbedContent)
        return self._timeline_view.display and tabs.active == "tab-main" and not self._sidebar_open_compact

    def _refresh_timeline_control(self) -> None:
        view = getattr(self, "_timeline_view", None)
        if view is None or not view.is_attached:
            return
        button = self.query_one("#timeline-toggle", Button)
        timeline = view.timeline
        button.display = timeline is not None
        if timeline is not None:
            elapsed = format_duration(timeline.elapsed(time.monotonic()))
            label = "返回对话" if self._timeline_is_visible() else f"耗时 {elapsed}"
            if str(button.label) != label:
                button.label = label

    def _set_timeline_open(self, opened: bool) -> None:
        view = self._timeline_view
        conversation = self.query_one("#conversation", VerticalScroll)
        if opened:
            self._sidebar_open_compact = False
            self.query_one("#tabs", TabbedContent).active = "tab-main"
        conversation.display = not opened
        view.display = opened
        self._refresh_workbench()
        if opened:
            view.refresh_timeline()
            view.table.focus()
        else:
            self.query_one("#user-input", MultilineInput).focus()

    def _clear_timeline(self) -> None:
        view = getattr(self, "_timeline_view", None)
        if view is not None:
            view.set_timeline(None)
            if view.display:
                self._set_timeline_open(False)
            self._refresh_timeline_control()

    def on_timeline_view_jump_requested(self, event: TimelineView.JumpRequested) -> None:
        target = event.target
        if not target.is_attached:
            return
        if target in self._collapsed_widgets:
            self._expand_collapsed_history()
        if isinstance(target, ToolCallBlock):
            target.collapsed = False
        self._follow_bottom = False
        self._set_timeline_open(False)
        self.call_after_refresh(self._scroll_to_timeline_record, target)

    def _scroll_to_timeline_record(self, target) -> None:
        if target.is_attached:
            if isinstance(target, (ThinkingBlock, ToolCallBlock)):
                target.query_one("CollapsibleTitle").focus(scroll_visible=False)
            target.scroll_visible(animate=False, top=True, immediate=True)
            self._follow_bottom = False

    def on_resize(self, event: Resize) -> None:
        self._workbench_width = event.size.width
        self._workbench_height = event.size.height
        compact = event.size.width < 100
        if compact != getattr(self, "_workbench_compact", None):
            self._sidebar_open_compact = False
            self._workbench_compact = compact
        self._refresh_workbench()
        self.call_after_refresh(self._refresh_workbench)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tabbed_content.id == "tabs":
            self._sidebar_open_compact = False
            self._refresh_workbench()

    def _refresh_workspace_tabs(self) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
            tabs.set_class(len(tabs.query(TabPane)) <= 1, "single-pane")
        except Exception:
            return
        self._refresh_workbench()

    def _set_workbench_phase(self, phase: str, detail: str = "") -> None:
        if (phase, detail) != (getattr(self, "_workbench_phase", ""), getattr(self, "_workbench_detail", "")):
            self._workbench_phase_started = time.monotonic()
        self._workbench_phase, self._workbench_detail = phase, detail
        self._refresh_workbench()

    def _refresh_workbench_caption(self) -> None:
        try:
            caption = self.query_one("#workbench-phase", Static)
            active_tab = self.query_one("#tabs", TabbedContent).active
        except Exception:
            return
        busy = self._busy
        phase = getattr(self, "_workbench_phase", "idle")
        if active_tab != "tab-main":
            label = "观察中 · 输入发送至主会话"
        elif self.size.width < 100 and self._sidebar_open_compact:
            label = "工作台 · 输入发送至主会话"
        elif self._compacting:
            label = "压缩上下文中"
        elif busy:
            labels = {"waiting": "等待模型", "thinking": "思考中", "answering": "回答中", "tool": "执行工具"}
            label = labels.get(phase, "处理中")
            if phase == "tool" and self.size.width >= 70:
                label += f" · {self._workbench_detail}"
            elapsed = int(time.monotonic() - getattr(self, "_workbench_phase_started", time.monotonic()))
            label += f" · {elapsed}s"
        else:
            label = "就绪"
        if self._queued:
            label += f" · 排队 {len(self._queued)}"
        if self._steer:
            label += f" · 插话 {len(self._steer)}"
        value = ("● " + label, busy)
        if value != getattr(self, "_workbench_caption", None):
            self._workbench_caption = value
            caption.update(Text(value[0], style=UI.accent if busy else UI.muted, no_wrap=True, overflow="ellipsis"))
        self._refresh_timeline_control()

    def _refresh_workbench(self) -> None:
        try:
            overview = self.query_one("#workbench-overview", WorkbenchOverview)
            hint = self.query_one("#hint", Static)
            summary = self.query_one("#thinking-summary", ThinkingSummaryPanel)
        except Exception:
            return
        goal = next((
            str(message.get("content") or "") for message in reversed(self.messages)
            if message.get("role") == "user" and not message.get("ddtui_kind")
            and not str(message.get("content") or "").startswith("[实时插话] ")
        ), "")
        height = getattr(self, "_workbench_height", self.size.height)
        input_rows = max(3, min(12, height // 3))
        pending_rows = max(1, min(8, height // 6))
        self.query_one("#pending").styles.max_height = pending_rows
        self.query_one("#slash-popup").styles.max_height = max(3, min(8, height - input_rows - pending_rows - 6))
        self.query_one("#user-input", MultilineInput)._fit_height()
        hint.display = height >= 16
        overview.set_goal(goal)
        text = getattr(self, "_thinking_summary_text", "")
        summary.set_summary(text, active=self._busy and self._workbench_phase == "thinking")
        summary.display = bool(text)
        pieces = (["Enter 排队", "Esc×2 中断", "Cmd+Enter 插话", "Ctrl+X 取消待发", "Ctrl+B 工作台"]
                  if self._busy else ["Enter 发送", "Option+Enter 换行", "Ctrl+B 工作台", "/help 更多"])
        selected = []
        for piece in pieces:
            if cell_len(" · ".join(selected + [piece])) <= max(10, self.size.width - 2):
                selected.append(piece)
        hint_text = " · ".join(selected)
        if hint_text != getattr(self, "_workbench_hint", None):
            self._workbench_hint = hint_text
            hint.update(hint_text)
        self._refresh_sidebar_visibility()
        self._refresh_workbench_caption()

    def _set_thinking_summary(self, text: str) -> None:
        self._thinking_summary_text = text
        try:
            panel = self.query_one("#thinking-summary", ThinkingSummaryPanel)
            panel.set_summary(text, active=self._busy and getattr(self, "_workbench_phase", "thinking") == "thinking")
            panel.display = bool(text)
            self._refresh_sidebar_visibility()
        except Exception:
            pass

    async def _add_sub_tab(self, sess: SubagentSession) -> None:
        """Mount a TabPane(SubagentTabPane) for this session as a
        top-level tab. Pane id mirrors the session id ('tab-sub-N')
        so removal is straightforward. Idempotent: if a pane with
        the same id already exists (shouldn't, but defensive), skip.

        Uses the SubagentTabPane that `_spawn_subagent` already
        constructed and attached to `sess.pane`, so any live widgets
        the round task queued during the call_later window flush in
        on_mount once we mount the TabPane below.
        """
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return
        pane_id = f"tab-{sess.id}"
        try:
            existing = tabs.query_one(f"#{pane_id}", TabPane)
        except Exception:
            existing = None
        if existing is not None:
            return
        inner = sess.pane if sess.pane is not None else SubagentTabPane(sess)
        if sess.pane is None:
            sess.pane = inner
        pane = TabPane(f"子 agent · {sess.id}", inner, id=pane_id)
        await tabs.add_pane(pane)
        self._refresh_workspace_tabs()

    async def _remove_sub_tab(self, sid: str) -> None:
        """Remove the TabPane for `sid`. If the tab being removed is
        the currently active one, TabbedContent will fall back to the
        previous tab automatically."""
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return
        pane_id = f"tab-{sid}"
        try:
            await tabs.remove_pane(pane_id)
        except Exception:
            # Pane may have already been removed (e.g. /clear racing
            # against end_agent) — silent no-op.
            pass
        self._refresh_workspace_tabs()

    def _reap_idle_subagents(self) -> None:
        """Drop sessions that have been quiescent longer than
        SUBAGENT_IDLE_TIMEOUT_SEC. Quiescent means phase ∈ {idle,
        ready, error} (i.e. no task in flight) and last_active_at
        hasn't been bumped recently. Reaping cancels any leftover
        task and kills its tasks/terminals so a forgotten subagent
        doesn't pin background processes or context indefinitely. Sessions still
        running (thinking/answering/tool) are never reaped — the user
        can ESC×2 / end_agent if they really want to stop them."""
        now = time.monotonic()
        for sid, sess in list(self._live_subagents.items()):
            if sess.phase not in ("idle", "ready", "error"):
                continue
            if now - sess.last_active_at < SUBAGENT_IDLE_TIMEOUT_SEC:
                continue
            if sess.task is not None and not sess.task.done():
                sess.task.cancel()
            kill_all_terminals(sess.ctx.terminals)
            kill_all_tasks(sess.ctx.tasks)
            self._live_subagents.pop(sid, None)
            self.call_later(self._remove_sub_tab, sid)

    def action_clear_chat(self) -> None:
        cancel_thinking_summaries(self)
        self._set_thinking_summary("")
        self._clear_timeline()
        # Rebuild the system prefix from scratch instead of keeping the
        # startup copy: /clear means "fresh conversation", so it re-reads
        # AGENTS.md and re-snapshots the environment (date, git branch).
        # This is what lets an external controller edit the project
        # guidelines and reset the session to try again.
        self.messages = self._build_system_prefix()
        # Drop any messages that hadn't started running yet — both the
        # state buffers and their parked bubbles in #pending.
        self._drop_queue()
        self._drop_steer()
        self.counter = TokenCounter()
        self._refresh_status()
        view = self.query_one("#conversation", VerticalScroll)
        for child in list(view.children):
            child.remove()
        # The marker + the display=False widgets it tracked were just
        # removed by the children sweep above; drop our references so
        # the next fold starts fresh.
        self._collapsed_widgets.clear()
        self._history_marker = None
        # Defensive: if anything else got into #pending out of band,
        # nuke it too — _drop_{queue,steer} only knows about widgets
        # it tracked via _queued/_steer.
        try:
            tray = self.query_one("#pending", Vertical)
            for child in list(tray.children):
                child.remove()
        except Exception:
            pass
        self._cancel_dismiss()
        if self._todo_block is not None and self._todo_block.is_mounted:
            self._todo_block.remove()
        self._todo_block = None
        self.ctx.checkpoint = None
        self.ctx.doc_receipts.clear()
        self.ctx.doc_receipt_next_id = 1
        self.ctx.experiments.clear()
        self.ctx.experiment_next_id = 1
        self._reset_explore_state()
        if (
            self._checkpoint_block is not None
            and self._checkpoint_block.is_mounted
        ):
            self._checkpoint_block.remove()
        self._checkpoint_block = None
        # Terminals and managed tasks are tied to the conversation that
        # spawned them, so a /clear tears them down too. The helper
        # SIGTERMs the whole process group, briefly waits, then SIGKILLs
        # any stragglers — ~100 ms cap so /clear stays snappy.
        kill_all_terminals(self.ctx.terminals)
        kill_all_tasks(self.ctx.tasks)
        for tid in list(self._terminal_panes.keys()):
            self.call_later(self._remove_terminal_tab, tid)
        self._terminal_panes.clear()
        if self._tasks_block is not None and self._tasks_block.is_mounted:
            self._tasks_block.remove()
        self._tasks_block = None
        # Live subagent sessions: cancel any in-flight task, kill each
        # one's tasks/terminals, drop them. Tied to the parent conversation,
        # so /clear releases all. Tab removal scheduled for each id.
        sub_ids_to_drop = list(self._live_subagents.keys())
        for sess in list(self._live_subagents.values()):
            if sess.task is not None and not sess.task.done():
                sess.task.cancel()
            kill_all_terminals(sess.ctx.terminals)
            kill_all_tasks(sess.ctx.tasks)
        self._live_subagents.clear()
        self._subagent_next_id = 1
        for sid in sub_ids_to_drop:
            self.call_later(self._remove_sub_tab, sid)
        if (
            self._subagent_block is not None
            and self._subagent_block.is_mounted
        ):
            self._subagent_block.remove()
        self._subagent_block = None
        try:
            self.query_one("#sidebar").remove_class("visible")
        except Exception:
            pass
        # `/clear` starts a fresh conversation, so don't overwrite the
        # autosave file that held the just-cleared transcript.
        self._clear_turn_journal()
        self._session_id = new_session_id()
        self.ctx.session_id = self._session_id
        self.ctx.context_last_prompt = 0
        self.ctx.context_last_estimate = 0
        self.ctx.compact_retry_after = 0
        self.ctx.task_next_id = 1
        self._autosave_path = None
        if hasattr(self, "_remote_on_session_reset"):
            self._remote_on_session_reset()
        self.call_later(self._autosave_conversation)

    def _start_dismiss(self) -> None:
        """Fade the TodoBlock out over 1.5s, then unmount and hide
        sidebar. Triggered when every TODO item is marked completed."""
        if self._todo_block is None:
            return
        self._todo_block.add_class("all-done")
        self._todo_block.styles.animate("opacity", value=0.0, duration=1.5)
        self._todo_dismiss_timer = self.set_timer(1.5, self._do_dismiss)

    def _cancel_dismiss(self) -> None:
        """Abort a pending fade-out — happens when a fresh todo_tool
        call revives the list before it has finished disappearing."""
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
        self._refresh_sidebar_visibility()

    def _handle_scroll_change(self, scroll_y: float) -> None:
        """Reactive watcher: keep `_follow_bottom` in sync with where
        the user has scrolled to. +1 tolerance covers float rounding."""
        try:
            view = self.query_one("#conversation", VerticalScroll)
        except Exception:
            return
        self._follow_bottom = scroll_y + 1 >= view.max_scroll_y

    def action_copy_selection(self) -> None:
        """Ctrl+C: copy the current selection to the system clipboard.

        Two selection sources, checked in order:

        1. Screen-wide widget selection — what the user dragged out of
           a Static/Markdown bubble in the conversation. Cleared after
           copy so the highlight goes away.
        2. The input box's own TextArea selection.

        Copy is delivered via App.copy_to_clipboard, which writes an
        OSC 52 escape. iTerm2 requires Preferences → General → Selection
        → "Applications in terminal may access clipboard" to actually
        put the text on the system pasteboard; the notify() line lets
        the user tell whether the copy fired even if iTerm2 swallowed
        the escape.

        With nothing selected, surface a hint about Ctrl+Q rather than
        exit silently — Ctrl+C used to quit, so muscle-memory presses
        should land on a visible "did nothing" instead of a no-op.
        """
        try:
            sel = self.screen.get_selected_text()
        except Exception:
            sel = None
        if sel:
            self.copy_to_clipboard(sel)
            try:
                self.screen.clear_selection()
            except Exception:
                pass
            self.notify(f"已复制 {len(sel)} 字符到剪贴板", timeout=2)
            return
        try:
            inp = self.query_one("#user-input", MultilineInput)
            ta_sel = inp.selected_text
        except Exception:
            ta_sel = ""
        if ta_sel:
            self.copy_to_clipboard(ta_sel)
            self.notify(f"已复制 {len(ta_sel)} 字符到剪贴板", timeout=2)
            return
        self.notify("没有选中文本可复制（退出请用 Ctrl+Q）", timeout=2)

    def action_toggle_thinking(self) -> None:
        """Toggle reasoning originals without changing individual tools."""
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
        self._drop_queue()
        self._drop_steer()
        self._refresh_status()
        self.notify(
            f"已取消 {n_q} 条排队 + {n_s} 条 steer",
            timeout=2.5,
        )

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
        self._drop_queue()
        self._drop_steer()
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
        """Drop the trailing in-flight user turn from message history
        so retrying doesn't leave a half-finished assistant/tool sequence."""
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                del self.messages[i:]
                self._drop_explore_if_truncated()
                return

    def _pair_orphan_tool_calls(
        self,
        cancel_note: str = "⛔ Tool execution cancelled by user (ESC×2)",
    ) -> int:
        """ESC×2 in the middle of tool execution can leave `self.messages`
        ending in `assistant(tool_calls=[A,B,...])` with only some (or
        zero) matching `tool` results appended. The OpenAI / DeepSeek
        Chat Completions protocol requires every tool_call id to be
        followed by a corresponding `tool` message before the next user
        turn — otherwise the next request fails with 400.

        Scan back to the most recent assistant message; for every
        unpaired tool_call id, append a stub `tool` message marking it
        cancelled. Also flip any still-pending `ToolCallBlock` widget to
        a blocked state so the UI doesn't keep showing 「执行中…」.

        Returns the number of stub messages appended.
        """
        last_assistant_idx: int | None = None
        for i in range(len(self.messages) - 1, -1, -1):
            role = self.messages[i].get("role")
            if role == "assistant":
                last_assistant_idx = i
                break
            if role == "user":
                return 0
        if last_assistant_idx is None:
            return 0

        tool_calls = self.messages[last_assistant_idx].get("tool_calls") or []
        if not tool_calls:
            return 0

        completed_ids = {
            m.get("tool_call_id")
            for m in self.messages[last_assistant_idx + 1:]
            if m.get("role") == "tool"
        }
        missing = [
            tc for tc in tool_calls
            if tc.get("id") and tc.get("id") not in completed_ids
        ]
        if not missing:
            return 0

        for tc in missing:
            self.messages.append(stamp_message({
                "role": "tool",
                "tool_call_id": tc.get("id") or "",
                "content": cancel_note,
            }))

        # Flip still-pending tool widgets to a blocked state. Walk back
        # from the bottom; stop once we hit the boundary (UserBubble /
        # SteerBubble) so we don't accidentally retroactively rewrite
        # blocks from earlier turns.
        try:
            view = self.query_one("#conversation", VerticalScroll)
            for child in reversed(list(view.children)):
                if isinstance(child, (UserBubble, SteerBubble)):
                    break
                if isinstance(child, ToolCallBlock) and child.is_pending:
                    try:
                        child.set_result(cancel_note, blocked=True)
                    except Exception:
                        pass
        except Exception:
            pass

        return len(missing)

    def _wipe_orphaned_turn_widgets(self) -> None:
        """Drop the UI widgets for the in-flight turn — the trailing
        UserBubble / SteerBubble plus everything mounted after it
        (thinking, answer, tool blocks). Pairs with
        `_rollback_last_user_turn` so the conversation view matches
        `self.messages` after a cancel; without this the bubbles
        linger as ghost content that's no longer in context."""
        try:
            view = self.query_one("#conversation", VerticalScroll)
        except Exception:
            return
        children = list(view.children)
        cut = -1
        for i in range(len(children) - 1, -1, -1):
            if isinstance(children[i], (UserBubble, SteerBubble)):
                cut = i
                break
        if cut < 0:
            return
        for c in children[cut:]:
            try:
                c.remove()
            except Exception:
                pass

    async def _mount_widget(self, widget) -> None:
        view = self.query_one("#conversation", VerticalScroll)
        await view.mount(widget)
        if self._follow_bottom:
            view.scroll_end(animate=False)

    async def _mount_pending(self, widget) -> None:
        """Mount a bubble into the #pending tray above the input box.
        The `pending` class arms the bubble's on_click → EditRequested
        path; bubbles that later get migrated into #conversation are
        re-created without the class, so only the parked copies are
        editable."""
        try:
            tray = self.query_one("#pending", Vertical)
        except Exception:
            # Fall back to the main conversation if the tray went
            # missing for some reason — better than dropping the bubble.
            await self._mount_widget(widget)
            return
        try:
            widget.add_class("pending")
        except Exception:
            pass
        await tray.mount(widget)

    def on_user_bubble_edit_requested(
        self, event: "UserBubble.EditRequested"
    ) -> None:
        self._open_pending_edit(event.bubble, kind="queue")

    def on_steer_bubble_edit_requested(
        self, event: "SteerBubble.EditRequested"
    ) -> None:
        self._open_pending_edit(event.bubble, kind="steer")

    def _open_pending_edit(self, bubble, kind: str) -> None:
        """Look up `bubble` in the matching pending list and push the
        edit modal. If the bubble has already been consumed by the turn
        between click and dispatch, silently ignore — there's nothing
        to edit anymore."""
        target_list = self._queued if kind == "queue" else self._steer
        entry = next(
            ((i, t) for i, (t, w) in enumerate(target_list) if w is bubble),
            None,
        )
        if entry is None:
            return
        _, original = entry

        def on_close(result: dict | None) -> None:
            if result is None:
                return
            # Re-resolve the index: the turn loop may have popped this
            # entry while the modal was open. Bail out cleanly if so.
            idx = next(
                (
                    j
                    for j, (_, w) in enumerate(target_list)
                    if w is bubble
                ),
                None,
            )
            if idx is None:
                self.notify("该条已被消费，无需编辑", timeout=2.0)
                return
            action = result.get("action")
            if action == "delete":
                target_list.pop(idx)
                try:
                    bubble.remove()
                except Exception:
                    pass
                self._refresh_status()
                tag = "steer" if kind == "steer" else "排队消息"
                self.notify(f"已删除 1 条{tag}", timeout=2.0)
            elif action == "save":
                new_text = result.get("text", "")
                target_list[idx] = (new_text, bubble)
                try:
                    bubble.set_text(new_text)
                except Exception:
                    pass
            elif action == "convert":
                new_text = result.get("text", "")
                target_list.pop(idx)
                try:
                    bubble.remove()
                except Exception:
                    pass
                if kind == "queue":
                    new_bubble = SteerBubble(new_text)
                    self._steer.append((new_text, new_bubble))
                    tag_from, tag_to = "排队消息", "steer"
                else:
                    new_bubble = UserBubble(new_text)
                    self._queued.append((new_text, new_bubble))
                    tag_from, tag_to = "steer", "排队消息"
                self.run_worker(self._mount_pending(new_bubble))
                self._refresh_status()
                self.notify(
                    f"已将 1 条{tag_from}转为 {tag_to}", timeout=2.0
                )

        self.push_screen(EditPendingScreen(original, kind), on_close)

    async def _maybe_collapse_history(self) -> None:
        """If #conversation has too many visible children, hide the
        oldest with display=False and surface a CollapsedHistoryMarker
        the user can click to restore them. Called from the agent
        turn's finally block — never mid-stream — so the user doesn't
        watch widgets vanish while reading. Idempotent: re-folding just
        extends the existing marker's count."""
        try:
            view = self.query_one("#conversation", VerticalScroll)
        except Exception:
            return
        # Skip the marker itself; only count real history children.
        visible = [
            c for c in view.children
            if c is not self._history_marker and c.display
        ]
        if len(visible) <= MAX_VISIBLE_HISTORY:
            return
        cut = len(visible) - COLLAPSED_HISTORY_KEEP
        to_collapse = visible[:cut]
        for w in to_collapse:
            try:
                w.display = False
            except Exception:
                continue
            self._collapsed_widgets.append(w)
        # First-ever fold: mount the marker immediately before the
        # first widget that's still visible so it reads as "everything
        # above this line is collapsed". Subsequent folds reuse the
        # existing marker (Textual lets a display=False sibling sit
        # between the marker and the new first-visible without any
        # visual gap).
        if self._history_marker is None or not self._history_marker.is_mounted:
            self._history_marker = CollapsedHistoryMarker()
            anchor = next(
                (c for c in view.children if c.display), None
            )
            try:
                if anchor is not None:
                    await view.mount(self._history_marker, before=anchor)
                else:
                    await view.mount(self._history_marker)
            except Exception:
                # Marker mount failed — abort folding so we don't leak
                # display=False widgets with no way to restore them.
                for w in to_collapse:
                    try:
                        w.display = True
                    except Exception:
                        pass
                    if self._collapsed_widgets and self._collapsed_widgets[-1] is w:
                        self._collapsed_widgets.pop()
                self._history_marker = None
                return
        self._history_marker.set_count(len(self._collapsed_widgets))

    def on_collapsed_history_marker_expand_requested(
        self, event: "CollapsedHistoryMarker.ExpandRequested"
    ) -> None:
        self._expand_collapsed_history()

    def _expand_collapsed_history(self) -> None:
        """Restore the hidden conversation widgets and remove their marker."""
        n = len(self._collapsed_widgets)
        for w in self._collapsed_widgets:
            try:
                w.display = True
            except Exception:
                pass
        self._collapsed_widgets.clear()
        if self._history_marker is not None:
            try:
                self._history_marker.remove()
            except Exception:
                pass
            self._history_marker = None
        if n:
            self.notify(f"已展开 {n} 条折叠历史", timeout=2.0)

    def _drop_queue(self) -> int:
        """Remove every parked queue bubble and clear _queued. Returns
        the count for status / notification messages."""
        n = len(self._queued)
        for _t, w in self._queued:
            try:
                w.remove()
            except Exception:
                pass
        self._queued.clear()
        return n

    def _drop_steer(self) -> int:
        n = len(self._steer)
        for _t, w in self._steer:
            try:
                w.remove()
            except Exception:
                pass
        self._steer.clear()
        return n

    def _refresh_status(self) -> None:
        try:
            bar = self.query_one("#status", StatusBar)
        except Exception:
            return
        bar.render_status(
            self.counter,
            context_limit=self._context_limit(),
            busy=self._busy,
            compacting=self._compacting,
            queued=len(self._queued),
            steer=len(self._steer),
            explore=self._active_explore is not None,
        )
        self._refresh_workbench()
        try:
            self._remote_emit_status("status")
        except Exception:
            pass

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self._set_thinking_summary("")
        self._workbench_phase = "waiting" if busy else "idle"
        self._workbench_phase_started = time.monotonic()
        self._refresh_status()
        try:
            self._remote_emit_status("busy", force=True)
        except Exception:
            pass

    def _set_compacting(self, compacting: bool) -> None:
        self._compacting = compacting
        self._refresh_status()
        try:
            self._remote_emit_status("compaction", force=True)
        except Exception:
            pass

    def _context_limit(self) -> int | None:
        try:
            limit = self.provider.context_limit_for_model(self.model)
        except Exception:
            return None
        return limit if limit and limit > 0 else None

    def _update_subtitle(self) -> None:
        sub = f"{self.provider.label} · {self.model} · effort={self.effort}"
        limit = self._context_limit()
        if limit:
            sub += f" · ctx={limit:,}"
        if self._agents_md_loaded:
            sub += " · AGENTS.md ✓"
        self.sub_title = sub
        try:
            label = f"{self.model} · {self.effort}"
            self.query_one("#workbench-model", Static).update(Text(label, no_wrap=True, overflow="ellipsis"))
        except Exception:
            pass
