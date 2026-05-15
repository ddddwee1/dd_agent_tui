"""UI lifecycle, sidebar, queue, and keyboard-action mixin for AgentApp."""

from __future__ import annotations

import time

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane

from .config import SUBAGENT_IDLE_TIMEOUT_SEC, SUBAGENT_REAP_INTERVAL_SEC
from .state import SubagentSession, kill_all_bash_jobs
from .widgets import (
    BgJobsBlock,
    MultilineInput,
    StatusBar,
    SteerBubble,
    SubagentsBlock,
    SubagentTabPane,
    ThinkingBlock,
    UserBubble,
)


class AppUiMixin:
    def on_mount(self) -> None:
        self.title = "DeepSeek Agent"
        self._update_subtitle()
        self._refresh_status()
        self.query_one("#user-input", MultilineInput).focus()
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
        # Same cadence for the subagent monitor — phase changes happen
        # on millisecond scales but the user only needs a coarse "what's
        # it doing" pulse.
        self.set_interval(1.0, self._refresh_subagent_panel)
        # Idle-reaper: end_agent any session that hasn't seen a
        # chat_agent call in SUBAGENT_IDLE_TIMEOUT_SEC. Coarse interval
        # — the only work to do is "kill timed-out sessions", not worth
        # running often.
        self.set_interval(
            SUBAGENT_REAP_INTERVAL_SEC, self._reap_idle_subagents
        )

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
        self._bg_jobs_block.render_jobs(list(self.ctx.bash_jobs.values()))

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

    async def _add_sub_tab(self, sess: SubagentSession) -> None:
        """Mount a TabPane(SubagentTabPane) for this session as a
        top-level tab. Pane id mirrors the session id ('tab-sub-N')
        so removal is straightforward. Idempotent: if a pane with
        the same id already exists (shouldn't, but defensive), skip."""
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
        pane = TabPane(sess.id, SubagentTabPane(sess), id=pane_id)
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
        # Background jobs are tied to the conversation that spawned
        # them, so a /clear nukes them too. The helper SIGTERMs the
        # whole process group, briefly waits, then SIGKILLs any
        # stragglers — ~100 ms cap so /clear stays snappy.
        kill_all_bash_jobs(self.ctx.bash_jobs)
        if self._bg_jobs_block is not None and self._bg_jobs_block.is_mounted:
            self._bg_jobs_block.remove()
        self._bg_jobs_block = None
        # Live subagent sessions: cancel any in-flight task, kill each
        # one's bash jobs, drop them. Tied to the parent conversation,
        # so /clear releases all. Tab removal scheduled for each id.
        sub_ids_to_drop = list(self._live_subagents.keys())
        for sess in list(self._live_subagents.values()):
            if sess.task is not None and not sess.task.done():
                sess.task.cancel()
            kill_all_bash_jobs(sess.ctx.bash_jobs)
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
        try:
            self.query_one("#sidebar").remove_class("visible")
        except Exception:
            pass

    def _handle_scroll_change(self, scroll_y: float) -> None:
        """Reactive watcher: keep `_follow_bottom` in sync with where
        the user has scrolled to. +1 tolerance covers float rounding."""
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
                return

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
        """Mount a bubble into the #pending tray above the input box."""
        try:
            tray = self.query_one("#pending", Vertical)
        except Exception:
            # Fall back to the main conversation if the tray went
            # missing for some reason — better than dropping the bubble.
            await self._mount_widget(widget)
            return
        await tray.mount(widget)

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
