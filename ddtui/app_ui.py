"""UI lifecycle, sidebar, queue, and keyboard-action mixin for AgentApp."""

from __future__ import annotations

import time

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane

from .config import (
    COLLAPSED_HISTORY_KEEP,
    MAX_VISIBLE_HISTORY,
    SUBAGENT_IDLE_TIMEOUT_SEC,
    SUBAGENT_REAP_INTERVAL_SEC,
)
from .runtime_state import new_session_id
from .state import (
    SubagentSession,
    kill_all_bash_jobs,
    kill_all_tasks,
    kill_all_terminals,
)
from .tools_tasks import collect_task_completion_events
from .tools_terminal import drain_terminal_output
from .widgets import (
    BgJobsBlock,
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
    ToolCallBlock,
    UserBubble,
)


class AppUiMixin:
    def on_mount(self) -> None:
        self.title = "DeepSeek Agent"
        self._update_subtitle()
        self._refresh_status()
        self.query_one("#user-input", MultilineInput).focus()
        self.call_later(self._autosave_conversation)
        self.call_later(self._show_disconnect_recovery_notice)
        if self._startup_provider_error:
            self.call_later(self._show_startup_provider_error)
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
        # Mirror ctx.bash_jobs into the sidebar once a second. Cheap —
        # just iterates the dict (≤ ~10 entries) and re-renders one
        # widget.
        self.set_interval(1.0, self._refresh_bg_panel)
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

    def _poll_task_notifications(self) -> None:
        checkpoint_before = self.ctx.checkpoint
        events = collect_task_completion_events(self.ctx)
        if checkpoint_before is not self.ctx.checkpoint:
            self.call_later(self._sync_checkpoint_block)
            self.call_later(self._autosave_conversation)
        if events:
            self._task_events.extend(events)
            self.notify(
                f"{len(events)} 个异步任务完成，已通知 agent",
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
            queued=len(self._queued),
            steer=len(self._steer),
        )

    def _refresh_bg_panel(self) -> None:
        """Mirror ctx.bash_jobs into the sidebar widget. Called once
        per second from on_mount's set_interval. Panel visibility is
        gated on the presence of running jobs — finished jobs stay in
        the dict for bash_check/bash_list to find, but they don't keep
        the panel pinned in the sidebar."""
        jobs = list(self.ctx.bash_jobs.values())
        running = [j for j in jobs if j.proc.poll() is None]
        try:
            sidebar = self.query_one("#sidebar", Vertical)
        except Exception:
            return
        if not running:
            # Nothing in flight → drop the panel, then let the shared
            # visibility helper account for todo/checkpoint/subagents.
            if (
                self._bg_jobs_block is not None
                and self._bg_jobs_block.is_mounted
            ):
                self._bg_jobs_block.remove()
            self._bg_jobs_block = None
            self._refresh_sidebar_visibility()
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
        self._bg_jobs_block.render_jobs(list(self.ctx.bash_jobs.values()))

    def _refresh_task_panel(self) -> None:
        """Mirror ctx.tasks into the sidebar widget.

        The sidebar is a live monitor, so it only shows tasks that are
        currently running. Finished tasks remain available via task
        notifications and task_check/task_list.
        """
        tasks = list(self.ctx.tasks.values())
        running = [task for task in tasks if task.proc.poll() is None]
        try:
            sidebar = self.query_one("#sidebar", Vertical)
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
        sidebar.add_class("visible")

    async def _mount_task_block(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", Vertical)
        except Exception:
            return
        if self._tasks_block is None:
            return
        if not self._tasks_block.is_mounted:
            await sidebar.mount(self._tasks_block)
        sidebar.add_class("visible")
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
            sidebar = self.query_one("#sidebar", Vertical)
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
        sidebar.add_class("visible")
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
            sidebar = self.query_one("#sidebar", Vertical)
        except Exception:
            return
        if self._subagent_block is None:
            return
        if not self._subagent_block.is_mounted:
            await sidebar.mount(self._subagent_block)
        sidebar.add_class("visible")
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
                drain_terminal_output(term)
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

    async def _remove_terminal_tab(self, terminal_id: str) -> None:
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return
        try:
            await tabs.remove_pane(f"tab-{terminal_id}")
        except Exception:
            pass

    async def _sync_checkpoint_block(self) -> None:
        """Mirror ctx.checkpoint into the sidebar."""
        try:
            sidebar = self.query_one("#sidebar", Vertical)
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
        sidebar.add_class("visible")

    def _refresh_sidebar_visibility(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", Vertical)
        except Exception:
            return
        has_visible = any(child.is_mounted for child in sidebar.children)
        if has_visible:
            sidebar.add_class("visible")
        else:
            sidebar.remove_class("visible")

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
        pane = TabPane(sess.id, inner, id=pane_id)
        await tabs.add_pane(pane)

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

    def _reap_idle_subagents(self) -> None:
        """Drop sessions that have been quiescent longer than
        SUBAGENT_IDLE_TIMEOUT_SEC. Quiescent means phase ∈ {idle,
        ready, error} (i.e. no task in flight) and last_active_at
        hasn't been bumped recently. Reaping cancels any leftover
        task and kills bash jobs so a forgotten subagent doesn't pin
        background processes or context indefinitely. Sessions still
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
            kill_all_bash_jobs(sess.ctx.bash_jobs)
            kill_all_terminals(sess.ctx.terminals)
            kill_all_tasks(sess.ctx.tasks)
            self._live_subagents.pop(sid, None)
            self.call_later(self._remove_sub_tab, sid)

    def action_clear_chat(self) -> None:
        # Keep ALL leading system messages (core prompt + optional
        # AGENTS.md). Stop at the first non-system message — anything
        # after that is conversation history that should be wiped.
        keep = []
        for m in self.messages:
            if m["role"] == "system":
                keep.append(m)
            else:
                break
        self.messages = keep
        # Drop any messages that hadn't started running yet — both the
        # state buffers and their parked bubbles in #pending.
        self._drop_queue()
        self._drop_steer()
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
        self._reset_explore_state()
        if (
            self._checkpoint_block is not None
            and self._checkpoint_block.is_mounted
        ):
            self._checkpoint_block.remove()
        self._checkpoint_block = None
        # Background jobs are tied to the conversation that spawned
        # them, so a /clear nukes them too. The helper SIGTERMs the
        # whole process group, briefly waits, then SIGKILLs any
        # stragglers — ~100 ms cap so /clear stays snappy.
        kill_all_bash_jobs(self.ctx.bash_jobs)
        kill_all_terminals(self.ctx.terminals)
        kill_all_tasks(self.ctx.tasks)
        if self._bg_jobs_block is not None and self._bg_jobs_block.is_mounted:
            self._bg_jobs_block.remove()
        self._bg_jobs_block = None
        for tid in list(self._terminal_panes.keys()):
            self.call_later(self._remove_terminal_tab, tid)
        self._terminal_panes.clear()
        if self._tasks_block is not None and self._tasks_block.is_mounted:
            self._tasks_block.remove()
        self._tasks_block = None
        # Live subagent sessions: cancel any in-flight task, kill each
        # one's bash jobs, drop them. Tied to the parent conversation,
        # so /clear releases all. Tab removal scheduled for each id.
        sub_ids_to_drop = list(self._live_subagents.keys())
        for sess in list(self._live_subagents.values()):
            if sess.task is not None and not sess.task.done():
                sess.task.cancel()
            kill_all_bash_jobs(sess.ctx.bash_jobs)
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
        self.ctx.task_next_id = 1
        self._autosave_path = None
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
            self.messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or "",
                "content": cancel_note,
            })

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
        """Marker clicked → restore every widget we've hidden so far
        and drop the marker. Restoration cost is exactly what the
        original render would have been — fine because the user
        explicitly asked for it."""
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
            queued=len(self._queued),
            steer=len(self._steer),
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_status()

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
