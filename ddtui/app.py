"""DeepSeek thinking-mode TUI agent — UI orchestrator.

`AgentApp` owns:
- the message list (`self.messages`)
- the `ToolContext` threaded through every tool call
- the `TokenCounter` (cumulative + last-call usage)
- the streaming agent loop (`_stream_one`, `_run_one_turn`, `_agent_turn`)
- slash-command dispatch (/clear, /compact, /save, /load, /help, …)
- the queue / steer / ESC×2-interrupt state machine
- the right-hand sidebar widgets (TodoBlock + BgJobsBlock)

Tool implementations and pure UI widgets live in `ddtui.tools` and
`ddtui.widgets` respectively; this module is the glue.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from openai import AsyncOpenAI
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Static

from .config import (
    AGENTS_MD_FILENAME,
    AGENTS_MD_MAX_CHARS,
    API_KEY_PATH,
    BASE_URL,
    COMPACT_KEEP_RECENT_TURNS,
    COMPACT_TOOL_SNIPPET_CHARS,
    HISTORY_DIR,
    MODEL,
    REASONING_EFFORT,
    SUBAGENT_MAX_TURNS,
    SUBAGENT_RESULT_MAX_CHARS,
    SYSTEM_PROMPT,
)
from .state import TokenCounter, ToolContext, kill_all_bash_jobs
from .tools import TOOLS, execute_tool
from .widgets import (
    AssistantMessage,
    BgJobsBlock,
    DiffBlock,
    MultilineInput,
    StatusBar,
    SteerBubble,
    ThinkingBlock,
    TodoBlock,
    ToolCallBlock,
    UserBubble,
)


# ───────── /compact helpers ─────────

def _render_history_for_summary(messages: list) -> str:
    """Flatten a slice of message history to plain text for the
    summarizer prompt. Drops reasoning_content (only useful at the time
    it was produced) and truncates tool results."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user":
            lines.append(f"=== USER ===\n{content}")
        elif role == "assistant":
            block = ["=== ASSISTANT ==="]
            if content:
                block.append(content)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name", "?")
                args_str = (fn.get("arguments") or "")[:200]
                block.append(f"  [tool_call] {name}({args_str})")
            lines.append("\n".join(block))
        elif role == "tool":
            snippet = content[:COMPACT_TOOL_SNIPPET_CHARS]
            if len(content) > COMPACT_TOOL_SNIPPET_CHARS:
                snippet += " …[truncated]"
            lines.append(f"=== TOOL RESULT ===\n{snippet}")
        # role == "system" intentionally skipped — system messages are
        # preserved across the compaction, so they don't belong in the
        # "what happened" recap.
    return "\n\n".join(lines)


# ───────── tool-confirm hook ─────────

ToolConfirmHook = Callable[[str, dict], Awaitable[bool]]


async def always_allow(name: str, args: dict) -> bool:
    """Default policy: every tool call is allowed.

    Replace `app.tool_confirm = my_hook` to gate execution on a modal,
    an allowlist, or per-tool rules. Returning False makes the runner
    inject a "Tool execution blocked" tool result instead of invoking
    the function — the model sees this and can adapt.
    """
    return True


# ───────── app ─────────

class AgentApp(App):
    CSS = """
    Screen { layout: vertical; background: #300A24; }
    Header { height: 1; }
    #body { height: 1fr; layout: horizontal; }
    #main { width: 1fr; layout: vertical; }
    #sidebar {
        width: 36;
        layout: vertical;
        border-left: solid #cba6f7;
        padding: 0 1;
        background: #300A24;
        display: none;
    }
    #sidebar.visible { display: block; }
    #conversation { height: 1fr; padding: 0 1; background: #300A24; }
    #hint { height: 1; padding: 0 1; color: $text-muted; background: #300A24; }
    /* Pending tray: parks queued/steer bubbles above the input box
       until the turn consumes them. height: auto + max-height keeps
       it invisible when empty. */
    #pending {
        height: auto;
        max-height: 8;
        padding: 0 1;
        background: #4a2a52;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出"),
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

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self.client = AsyncOpenAI(api_key=api_key, base_url=BASE_URL)
        # Mutable copies of the module-level defaults — `/model` and
        # `/effort` slash commands rebind these mid-session so the user
        # can switch without restarting.
        self.model = MODEL
        self.effort = REASONING_EFFORT
        # Per-conversation runtime state: work_dir + bash job table +
        # id allocator. Threaded explicitly through every execute_tool
        # call so a future subagent can be handed its own ctx.
        self.ctx = ToolContext(work_dir=os.getcwd())
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
        self.counter = TokenCounter()
        # Hook: replace this attribute (or override) to gate tools.
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
        # Single BgJobsBlock that mirrors ctx.bash_jobs into the sidebar.
        # Driven by a 1-second timer started in on_mount.
        self._bg_jobs_block: BgJobsBlock | None = None
        # Message queue: messages submitted while a turn is in flight
        # land here and are consumed in FIFO order after the current
        # turn finishes. Wiped on /clear and on API error (so the user
        # can decide what to do next instead of being railroaded).
        # Each entry pairs the text with its bubble widget parked in
        # the #pending tray, so consumption / cancel can remove it.
        self._queued: list[tuple[str, "UserBubble"]] = []
        # Steer: messages submitted via alt/cmd+enter that should be
        # injected mid-turn at the next LLM round boundary (rather than
        # waiting for the whole turn to finish like _queued does).
        # Drained by `_run_one_turn` before each `_stream_one()` call.
        self._steer: list[tuple[str, "SteerBubble"]] = []
        # Currently-running agent turn worker — kept so ESC×2 can
        # cancel it. None when idle.
        self._agent_worker = None
        # ESC×2 to interrupt: first press arms, second press within
        # ESC_DOUBLE_WINDOW seconds fires _interrupt_now. The disarm
        # timer auto-resets the armed flag if no second press lands.
        self._esc_armed = False
        self._esc_disarm_timer = None

    # ─ compose ─

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield VerticalScroll(id="conversation")
                yield Static(
                    "Enter 发送 · Option+Enter 换行 · Cmd+Enter steer · ESC×2 中断 · Ctrl+X 取消 · Ctrl+L 清空 · Ctrl+C 退出",
                    id="hint",
                )
                yield Vertical(id="pending")
                yield MultilineInput(id="user-input")
                yield StatusBar(self.ctx.work_dir, id="status")
            yield Vertical(id="sidebar")

    def on_mount(self) -> None:
        self.title = "DeepSeek Agent"
        self._update_subtitle()
        self._refresh_status()
        self.query_one("#user-input", MultilineInput).focus()
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

    # ─ key bindings / actions ─

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
        try:
            self.query_one("#sidebar").remove_class("visible")
        except Exception:
            pass

    # ─ todo sidebar lifecycle ─

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

    # ─ ESC×2 interrupt ─

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

    # ─ input handler ─

    async def on_multiline_input_submitted(
        self, event: "MultilineInput.Submitted"
    ) -> None:
        text = event.value.strip()
        inp = self.query_one("#user-input", MultilineInput)
        if not text:
            return
        if text in ("/exit", "/quit"):
            self.exit()
            return
        if text == "/clear":
            self.action_clear_chat()
            inp.text = ""
            return
        if text == "/compact":
            inp.text = ""
            if self._busy:
                self.notify(
                    "正在跑任务；先 ESC×2 中断或等结束再 /compact",
                    timeout=3,
                )
                return
            # Run on its own worker group so it doesn't fight the
            # agent turn group's exclusive=True semantics.
            self.run_worker(
                self._compact_worker(), exclusive=True, group="compact"
            )
            return
        if text == "/save" or text.startswith("/save "):
            inp.text = ""
            arg = text[5:].strip()
            if not arg:
                await self._mount_widget(Static(Text(
                    "用法：/save <filename>  → 保存到 "
                    "~/.ddtui/history/<filename>.json",
                    style="dim",
                )))
                return
            await self._save_conversation(arg)
            return
        if text == "/load" or text.startswith("/load "):
            inp.text = ""
            arg = text[5:].strip()
            if not arg:
                await self._mount_widget(Static(Text(
                    "用法：/load <name>  → 从 "
                    "~/.ddtui/history/<name>.json 读回对话\n"
                    "可先用 /list-history 查看已保存对话。",
                    style="dim",
                )))
                return
            if self._busy:
                self.notify(
                    "正在跑任务；先 ESC×2 中断或等结束再 /load",
                    timeout=3,
                )
                return
            await self._load_conversation(arg)
            return
        if text in ("/list-history", "/history", "/list_history"):
            inp.text = ""
            await self._list_history()
            return
        if text == "/model" or text.startswith("/model "):
            inp.text = ""
            arg = text[len("/model"):].strip()
            if not arg:
                await self._mount_widget(Static(Text(
                    f"当前 model: {self.model}\n"
                    "用法：/model <id>  （下一轮请求开始生效）",
                    style="dim",
                )))
            else:
                old = self.model
                self.model = arg
                self._update_subtitle()
                await self._mount_widget(Static(Text(
                    f"⚙ model: {old} → {self.model}（下一轮请求生效）",
                    style="bold cyan",
                )))
            return
        if text == "/effort" or text.startswith("/effort "):
            inp.text = ""
            arg = text[len("/effort"):].strip()
            if not arg:
                await self._mount_widget(Static(Text(
                    f"当前 effort: {self.effort}\n"
                    "用法：/effort <level>  例如 max / medium / low / minimal",
                    style="dim",
                )))
            else:
                old = self.effort
                self.effort = arg
                self._update_subtitle()
                await self._mount_widget(Static(Text(
                    f"⚙ effort: {old} → {self.effort}（下一轮请求生效）",
                    style="bold cyan",
                )))
            return
        if text == "/help":
            inp.text = ""
            await self._show_help()
            return
        inp.text = ""

        if self._busy:
            # Queue mode: park the bubble in the #pending tray above
            # the input box so the user can see "this hasn't been sent
            # yet" at a glance. The turn picks it up at the next round
            # boundary, at which point the bubble migrates into the
            # main conversation flow.
            bubble = UserBubble(text)
            await self._mount_pending(bubble)
            self._queued.append((text, bubble))
            self._refresh_status()
            return

        # New turn: snap back to the bottom — the user just submitted,
        # so they want to see the response.
        self._follow_bottom = True
        await self._mount_widget(UserBubble(text))
        self._agent_worker = self.run_worker(
            self._agent_turn(text), exclusive=True
        )

    async def on_multiline_input_steer_submitted(
        self, event: "MultilineInput.SteerSubmitted"
    ) -> None:
        """Cmd/Ctrl+Enter inside the input box: stash the text into the
        steer buffer; the next LLM round will see it as `[实时插话]`."""
        text = event.value.strip()
        if not text:
            return
        self.query_one("#user-input", MultilineInput).text = ""
        bubble = SteerBubble(text)
        await self._mount_pending(bubble)
        self._steer.append((text, bubble))
        self._refresh_status()

    # ─ helpers ─

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
            busy=self._busy,
            queued=len(self._queued),
            steer=len(self._steer),
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_status()

    def _update_subtitle(self) -> None:
        sub = f"{self.model} · effort={self.effort}"
        if self._agents_md_loaded:
            sub += " · AGENTS.md ✓"
        self.sub_title = sub

    # ─ /compact ─

    async def _compact_worker(self) -> None:
        """Replace the early portion of message history with a single
        summary message, leaving COMPACT_KEEP_RECENT_TURNS user→assistant
        turns intact at the tail. System messages are preserved.

        On error, the original history is left untouched and a red
        message is mounted asking the user to retry.
        """
        self._set_busy(True)
        try:
            # Find the leading run of system messages — those are kept verbatim.
            prefix_end = 0
            for i, m in enumerate(self.messages):
                if m.get("role") == "system":
                    prefix_end = i + 1
                else:
                    break
            user_idxs = [
                i
                for i, m in enumerate(self.messages)
                if m.get("role") == "user"
            ]
            if len(user_idxs) <= COMPACT_KEEP_RECENT_TURNS:
                await self._mount_widget(
                    Static(Text(
                        f"对话还不够长，无需压缩（user 消息数 ≤ "
                        f"{COMPACT_KEEP_RECENT_TURNS}）。",
                        style="dim",
                    ))
                )
                return
            cut_at = user_idxs[-COMPACT_KEEP_RECENT_TURNS]
            to_compact = self.messages[prefix_end:cut_at]
            keep_recent = self.messages[cut_at:]
            if not to_compact:
                await self._mount_widget(
                    Static(Text("没有可压缩的历史段。", style="dim"))
                )
                return

            await self._mount_widget(
                Static(Text("📦 正在压缩历史…", style="bold cyan"))
            )

            rendered = _render_history_for_summary(to_compact)
            try:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "你是一个对话历史摘要助手。直接输出中文摘要，"
                                "不要执行任何工具调用，不要回答用户问题，"
                                "不要假装继续对话。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                "下面是一段 agent 与用户的对话历史，需要被压缩成简明摘要，"
                                "供后续轮次作为上下文使用。请按以下结构输出（用 markdown 标题）：\n"
                                "## 用户目标\n"
                                "## 已完成的工作\n"
                                "## 改动过的文件 / 关键决定\n"
                                "## 悬而未决的问题或下一步\n\n"
                                "要求：保留具体函数名、文件路径、行号等可定位的事实；"
                                "不要复述完整代码、完整命令输出；"
                                "不要遗漏待办事项。\n\n"
                                f"---\n{rendered}\n---"
                            ),
                        },
                    ],
                    stream=False,
                )
                summary = (resp.choices[0].message.content or "").strip()
                if not summary:
                    raise RuntimeError("摘要返回为空")
            except Exception as e:
                await self._mount_widget(
                    Static(Text(
                        f"❌ 压缩失败：{e}\n请稍后再试 /compact。",
                        style="bold red",
                    ))
                )
                return

            before_n = len(self.messages)
            before_chars = sum(
                len(m.get("content") or "") for m in self.messages
            )
            self.messages = (
                self.messages[:prefix_end]
                + [
                    {
                        "role": "system",
                        "content": (
                            "# 历史摘要（来自 /compact，覆盖此前若干轮对话）\n\n"
                            f"{summary}"
                        ),
                    }
                ]
                + keep_recent
            )
            after_n = len(self.messages)
            after_chars = sum(
                len(m.get("content") or "") for m in self.messages
            )
            saved = max(0, before_chars - after_chars)
            await self._mount_widget(
                Static(Text(
                    f"📦 已压缩：{before_n} → {after_n} 条消息，"
                    f"约 -{saved:,} 字符（保留最近 "
                    f"{COMPACT_KEEP_RECENT_TURNS} 轮原文）",
                    style="bold cyan",
                ))
            )
        finally:
            self._set_busy(False)
            self._refresh_status()

    # ─ /save ─

    async def _save_conversation(self, raw_name: str) -> None:
        """Dump self.messages (plus a small metadata header) to
        HISTORY_DIR / <name>.json. Overwrites silently — the user
        already provided a name, second-guessing them would be annoying."""
        name = raw_name.strip()
        if name.endswith(".json"):
            name = name[:-5]
        name = name.strip()
        if not name:
            await self._mount_widget(Static(Text(
                "错误：文件名不能为空。", style="bold red"
            )))
            return
        # Cheap sanity check before letting Path see the string. This
        # rejects most accidental path-injection patterns with a clearer
        # error than the resolve()/relative_to guard below.
        if "/" in name or "\\" in name or ".." in name:
            await self._mount_widget(Static(Text(
                f"错误：文件名 {raw_name!r} 不能含 / \\ 或 ..",
                style="bold red",
            )))
            return

        try:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            target = HISTORY_DIR / f"{name}.json"
            # Defense in depth: even if the cheap check missed
            # something, make sure the resolved path is still inside
            # HISTORY_DIR.
            resolved = target.resolve()
            try:
                resolved.relative_to(HISTORY_DIR.resolve())
            except ValueError:
                await self._mount_widget(Static(Text(
                    f"错误：解析后路径逃出 history 目录：{resolved}",
                    style="bold red",
                )))
                return

            existed = target.exists()
            payload = {
                "version": 1,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "model": self.model,
                "messages": self.messages,
            }
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            size = target.stat().st_size
        except Exception as e:
            await self._mount_widget(Static(Text(
                f"❌ 保存失败:{e}", style="bold red"
            )))
            return

        try:
            display = "~/" + str(target.relative_to(Path.home()))
        except ValueError:
            display = str(target)
        note = "（已覆盖原文件）" if existed else ""
        await self._mount_widget(Static(Text(
            f"💾 已保存 {len(self.messages)} 条消息（{size:,} 字节）"
            f"到 {display}{note}",
            style="bold green",
        )))

    # ─ /load · /list-history · /help ─

    async def _list_history(self) -> None:
        """Render every *.json under HISTORY_DIR, newest first."""
        if not HISTORY_DIR.exists():
            await self._mount_widget(Static(Text(
                f"目录还不存在：{HISTORY_DIR}\n"
                "用 /save <name> 保存第一段对话来创建它。",
                style="dim",
            )))
            return
        try:
            files = sorted(
                HISTORY_DIR.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception as e:
            await self._mount_widget(Static(Text(
                f"列出 {HISTORY_DIR} 失败：{e}", style="bold red"
            )))
            return
        if not files:
            await self._mount_widget(Static(Text(
                f"{HISTORY_DIR} 还没保存过对话", style="dim"
            )))
            return
        t = Text()
        t.append(f"📂 {HISTORY_DIR}（{len(files)} 个对话）\n", style="bold")
        for p in files:
            try:
                stat = p.stat()
                ts = datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M"
                )
                t.append(f"  {p.stem}", style="bold green")
                t.append(f"  ({stat.st_size:,} 字节, {ts})\n", style="dim")
            except Exception:
                t.append(f"  {p.stem}\n", style="bold green")
        t.append("用 /load <name> 读回。", style="dim italic")
        await self._mount_widget(Static(t))

    async def _load_conversation(self, raw_name: str) -> None:
        """Replace `self.messages` with a saved JSON and replay every
        bubble / tool-call / diff into the conversation view so the
        history "looks lived in" rather than appearing as a single
        opaque blob."""
        name = raw_name.strip()
        if name.endswith(".json"):
            name = name[:-5]
        name = name.strip()
        if not name:
            await self._mount_widget(Static(Text(
                "错误：文件名不能为空。", style="bold red"
            )))
            return
        if "/" in name or "\\" in name or ".." in name:
            await self._mount_widget(Static(Text(
                f"错误：文件名 {raw_name!r} 不能含 / \\ 或 ..",
                style="bold red",
            )))
            return
        target = HISTORY_DIR / f"{name}.json"
        try:
            resolved = target.resolve()
            resolved.relative_to(HISTORY_DIR.resolve())
        except ValueError:
            await self._mount_widget(Static(Text(
                f"错误：解析后路径逃出 history 目录：{resolved}",
                style="bold red",
            )))
            return
        if not target.exists():
            await self._mount_widget(Static(Text(
                f"错误：找不到 {target}\n用 /list-history 看可用列表。",
                style="bold red",
            )))
            return
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception as e:
            await self._mount_widget(Static(Text(
                f"❌ 读取失败：{e}", style="bold red"
            )))
            return
        msgs = payload.get("messages")
        if not isinstance(msgs, list) or not all(
            isinstance(m, dict) and "role" in m for m in msgs
        ):
            await self._mount_widget(Static(Text(
                "❌ 文件格式不识别（缺少合法 messages 数组）",
                style="bold red",
            )))
            return

        # Wipe the current view + buffers; then drop in the loaded
        # messages and replay them as widgets. Background bash jobs
        # are NOT killed — they belong to the wall-clock-current shell,
        # not the conversation that just got swapped in.
        self.messages = msgs
        self._drop_queue()
        self._drop_steer()
        view = self.query_one("#conversation", VerticalScroll)
        for child in list(view.children):
            child.remove()
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

        await self._replay_messages_to_view()

        # Reset token counter — saved file has no original usage info,
        # and pretending the previous turns "cost zero" would lie about
        # the live context window.
        self.counter = TokenCounter()
        self._refresh_status()

        saved_at = payload.get("saved_at", "?")
        saved_model = payload.get("model", "?")
        diff_note = ""
        if saved_model and saved_model != self.model:
            diff_note = f"（保存时 model={saved_model}，当前 model={self.model}）"
        await self._mount_widget(Static(Text(
            f"📂 已加载 {len(msgs)} 条消息（saved={saved_at}）{diff_note}\n"
            "Token 计数已重置；从此处续聊即可。",
            style="bold green",
        )))

    async def _replay_messages_to_view(self) -> None:
        """Render `self.messages` back into the conversation view as
        widgets. Mirrors the live agent loop (user → thinking → answer
        → tool-call → diff) but without re-running anything."""
        # tool_call_id → tool result, looked up while walking the
        # assistant's tool_calls list.
        tool_results: dict[str, str] = {}
        for m in self.messages:
            if m.get("role") == "tool":
                tcid = m.get("tool_call_id")
                if tcid:
                    tool_results[tcid] = m.get("content") or ""

        for m in self.messages:
            role = m.get("role")
            if role in ("system", "tool"):
                continue
            content = m.get("content") or ""
            if role == "user":
                if content.startswith("[实时插话] "):
                    await self._mount_widget(
                        SteerBubble(content[len("[实时插话] "):])
                    )
                else:
                    await self._mount_widget(UserBubble(content))
                continue
            if role != "assistant":
                continue

            rc = m.get("reasoning_content")
            if rc:
                tb = ThinkingBlock()
                tb.append_text(rc)
                tb.finalize(0)
                # Default-collapse on replay — a freshly loaded
                # conversation shouldn't bury the user under walls of
                # thought from earlier turns.
                tb.collapsed = True
                await self._mount_widget(tb)
            if content:
                am = AssistantMessage()
                am.append_text(content)
                await self._mount_widget(am)

            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                name = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = {"_raw": fn.get("arguments") or ""}

                if name == "todo_tool":
                    # Rebuild the sidebar TodoBlock — last call wins
                    # since each todo_tool invocation overwrites.
                    items = (
                        args.get("items", [])
                        if isinstance(args, dict)
                        else []
                    )
                    try:
                        sidebar = self.query_one("#sidebar", Vertical)
                    except Exception:
                        sidebar = None
                    if sidebar is not None and items:
                        if (
                            self._todo_block is None
                            or not self._todo_block.is_mounted
                        ):
                            self._todo_block = TodoBlock()
                            await sidebar.mount(self._todo_block)
                        self._todo_block.set_items(items)
                        sidebar.add_class("visible")
                    continue

                block = ToolCallBlock(name, args)
                await self._mount_widget(block)
                result = tool_results.get(tc.get("id"))
                if result is None:
                    continue
                diff_text = None
                if name in ("edit_file", "edit_lines", "multi_edit"):
                    head, sep, diff = result.partition("\n\n")
                    if sep and "@@" in diff:
                        diff_text = diff
                        result = head
                block.set_result(result)
                if diff_text:
                    await self._mount_widget(
                        DiffBlock(args.get("path", "?"), diff_text)
                    )

    async def _show_help(self) -> None:
        md = (
            "### Slash 命令\n"
            "- `/clear` 清空对话（保留 system prompt + AGENTS.md）\n"
            "- `/compact` 压缩历史，保留最近两轮原文\n"
            "- `/save <name>` 保存到 `~/.ddtui/history/<name>.json`\n"
            "- `/load <name>` 读回保存的对话\n"
            "- `/list-history` 列出已保存对话\n"
            "- `/model [<id>]` 查看 / 切换模型（下一轮请求生效）\n"
            "- `/effort [<level>]` 查看 / 切换 reasoning effort\n"
            "- `/help` 显示这个帮助\n"
            "- `/exit` `/quit` 退出\n\n"
            "### 快捷键\n"
            "- `Enter` 发送当前输入\n"
            "- `Option+Enter` 插入换行\n"
            "- `Cmd+Enter` 实时插话 steer（需 iTerm2 把 ⌘Return 转义为 CSI u；"
            "macOS 自带 Terminal.app 不支持，先 Option+Enter 换行后再 Enter 也可）\n"
            "- `ESC × 2` 中断当前任务\n"
            "- `Ctrl+X` 取消队列 / steer\n"
            "- `Ctrl+T` 展开 / 折叠所有思考块\n"
            "- `Ctrl+L` 清空对话\n"
            "- `Ctrl+C` 退出\n\n"
            "### 工具(模型可调用)\n"
            "`bash` `bash_start/check/wait/kill/list` "
            "`read_file` `write_file` `edit_file` `edit_lines` `multi_edit` "
            "`list_files` `glob_files` `search_content` `web_fetch` "
            "`todo_tool` `spawn_agent`\n"
            "\n"
            f"`spawn_agent` 让模型派一个隔离的子 agent 跑自包含子任务，"
            f"主对话只看到子的最终回答（最多 {SUBAGENT_MAX_TURNS} 轮，"
            f"返回截断到 {SUBAGENT_RESULT_MAX_CHARS:,} 字符）。"
            "子 agent 不能再嵌套子 agent，但 token 计入主对话 status bar。\n"
        )
        await self._mount_widget(Static(Markdown(md)))

    # ─ agent loop ─

    async def _agent_turn(self, user_text: str) -> None:
        """Outer loop: drives one or more user turns end-to-end.

        Drains the queue between turns — every iteration commits one
        user message to history, runs `_run_one_turn` to completion (or
        error), and then either pops the next queued message or returns.
        """
        self._set_busy(True)
        pending = user_text
        try:
            while True:
                self.messages.append({"role": "user", "content": pending})
                ok = await self._run_one_turn()
                if not ok:
                    # API error already surfaced + rolled back.
                    # Wipe the queue: don't railroad the user into
                    # more failing turns; let them re-submit if they want.
                    self._drop_queue()
                    self._refresh_status()
                    return
                if not self._queued:
                    return
                pending, parked = self._queued.pop(0)
                # Migrate the parked bubble into the conversation flow:
                # remove it from #pending and mount a fresh one in
                # #conversation so the user sees it "drop" out of the
                # pending tray as the turn picks it up.
                try:
                    parked.remove()
                except Exception:
                    pass
                self._follow_bottom = True
                await self._mount_widget(UserBubble(pending))
                self._refresh_status()
        except asyncio.CancelledError:
            # ESC×2 cancellation: roll back the in-flight turn so retry
            # doesn't see a dangling user/assistant/tool tail, then
            # mark the spot in the conversation. Re-raise so the worker
            # is marked CANCELLED rather than COMPLETED.
            self._rollback_last_user_turn()
            try:
                await self._mount_widget(
                    Static(Text("⛔ 已中断", style="bold yellow"))
                )
            except Exception:
                pass
            raise
        finally:
            self._set_busy(False)
            self._agent_worker = None
            # Refresh location after the turn — the agent may have run
            # `git checkout` or otherwise moved through bash. Off the
            # event loop so a slow git invocation can't stall UI.
            try:
                bar = self.query_one("#status", StatusBar)
                await asyncio.to_thread(bar.refresh_location)
            except Exception:
                pass
            self._refresh_status()

    async def _run_one_turn(self) -> bool:
        """Inner loop: one user→assistant→(tools→assistant)+ turn.

        Returns True if the turn completed normally, False if an API
        error was caught (the error is shown to the UI and the trailing
        user/tool messages are rolled back so the user can retry).
        """
        try:
            while True:
                # Drain any steer messages submitted since the last
                # round. Tagged with [实时插话] so the model can tell
                # this is a mid-turn interjection from the user (and
                # reconsider its plan accordingly), not a fresh user
                # request.
                if self._steer:
                    pending_steer = self._steer
                    self._steer = []
                    for s, parked in pending_steer:
                        self.messages.append(
                            {
                                "role": "user",
                                "content": f"[实时插话] {s}",
                            }
                        )
                        # Move the bubble out of the pending tray and
                        # into the conversation so the user can scroll
                        # back through their interjections later.
                        try:
                            parked.remove()
                        except Exception:
                            pass
                        await self._mount_widget(SteerBubble(s))
                    self._refresh_status()

                assistant_msg = await self._stream_one()
                self.messages.append(assistant_msg)
                tool_calls = assistant_msg.get("tool_calls") or []
                if not tool_calls:
                    return True
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw = tc["function"].get("arguments") or "{}"
                    try:
                        args = json.loads(raw)
                    except json.JSONDecodeError:
                        block = ToolCallBlock(name, {"_raw": raw})
                        await self._mount_widget(block)
                        result = f"Error: could not parse arguments JSON: {raw}"
                        block.set_result(result, blocked=True)
                    else:
                        # Special-case todo_tool: maintain a single
                        # TodoBlock widget in the right-hand sidebar.
                        # Fade out + unmount when every item is completed.
                        if name == "todo_tool":
                            items = (
                                args.get("items", [])
                                if isinstance(args, dict)
                                else []
                            )
                            sidebar = self.query_one("#sidebar", Vertical)
                            self._cancel_dismiss()
                            if (
                                self._todo_block is None
                                or not self._todo_block.is_mounted
                            ):
                                self._todo_block = TodoBlock()
                                await sidebar.mount(self._todo_block)
                            self._todo_block.set_items(items)
                            sidebar.add_class("visible")
                            if items and all(
                                i.get("status") == "completed" for i in items
                            ):
                                self._start_dismiss()
                            result = await asyncio.to_thread(
                                execute_tool, self.ctx, name, args
                            )
                            self.messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": result,
                                }
                            )
                            continue

                        if name == "spawn_agent":
                            # Subagent: needs LLM access, so it can't
                            # go through `execute_tool` / asyncio.to_thread
                            # like sync tools — run it inline on this
                            # event loop. ESC×2 cancels the parent
                            # worker; CancelledError propagates here
                            # and aborts the subagent's await chain.
                            block = ToolCallBlock(name, args)
                            await self._mount_widget(block)
                            prompt = args.get("prompt") or ""
                            sub_system = args.get("system") or ""
                            if not prompt.strip():
                                result = (
                                    "Error: spawn_agent requires a "
                                    "non-empty 'prompt' argument."
                                )
                                block.set_result(result, blocked=True)
                            else:
                                try:
                                    result = await self._run_subagent(
                                        prompt, sub_system
                                    )
                                except asyncio.CancelledError:
                                    result = "⛔ subagent cancelled (parent ESC×2)"
                                    block.set_result(result, blocked=True)
                                    self.messages.append({
                                        "role": "tool",
                                        "tool_call_id": tc["id"],
                                        "content": result,
                                    })
                                    raise
                                if len(result) > SUBAGENT_RESULT_MAX_CHARS:
                                    result = (
                                        result[:SUBAGENT_RESULT_MAX_CHARS]
                                        + f"\n…[+{len(result) - SUBAGENT_RESULT_MAX_CHARS} chars truncated]"
                                    )
                                block.set_result(result)
                            self.messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result,
                            })
                            continue

                        block = ToolCallBlock(name, args)
                        await self._mount_widget(block)
                        allowed = await self.tool_confirm(name, args)
                        if allowed:
                            result = await asyncio.to_thread(
                                execute_tool, self.ctx, name, args
                            )
                            # For edit_*, peel the diff out of the result
                            # before it goes into message history: the
                            # diff is shown to the user as a DiffBlock,
                            # but sending it back to the model wastes
                            # tokens (the head summary already says
                            # what changed).
                            diff_text = None
                            if name in ("edit_file", "edit_lines", "multi_edit"):
                                head, sep, diff = result.partition("\n\n")
                                if sep and "@@" in diff:
                                    diff_text = diff
                                    result = head
                            block.set_result(result)
                            if diff_text:
                                await self._mount_widget(
                                    DiffBlock(args.get("path", "?"), diff_text)
                                )
                        else:
                            result = "Tool execution blocked by user policy."
                            block.set_result(result, blocked=True)
                    self.messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": result}
                    )
        except Exception as e:
            err = Static(Text(f"API error: {e}", style="bold red"))
            await self._mount_widget(err)
            # roll back the trailing user/tool turn so the user can retry
            for i in range(len(self.messages) - 1, -1, -1):
                if self.messages[i]["role"] == "user":
                    del self.messages[i:]
                    break
            return False

    async def _stream_one(self) -> dict:
        thinking: ThinkingBlock | None = None
        answer: AssistantMessage | None = None
        tool_calls: dict[int, dict] = {}
        final_usage = None

        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=TOOLS,
            tool_choice="auto",
            stream=True,
            stream_options={"include_usage": True},
            reasoning_effort=self.effort,
            extra_body={"thinking": {"type": "enabled"}},
        )

        # `_mount_widget` scrolls to the end on mount, but widgets that
        # grow via `update()` (the streaming case) don't trigger scroll
        # on their own — so explicitly follow the bottom on every chunk.
        view = self.query_one("#conversation", VerticalScroll)

        async for chunk in stream:
            if getattr(chunk, "usage", None):
                final_usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            rc = getattr(delta, "reasoning_content", None)
            if rc:
                if thinking is None:
                    thinking = ThinkingBlock()
                    await self._mount_widget(thinking)
                thinking.append_text(rc)
                if self._follow_bottom:
                    view.scroll_end(animate=False)

            text = getattr(delta, "content", None)
            if text:
                if answer is None:
                    answer = AssistantMessage()
                    await self._mount_widget(answer)
                answer.append_text(text)
                if self._follow_bottom:
                    view.scroll_end(animate=False)

            for tc in getattr(delta, "tool_calls", None) or []:
                idx = tc.index if tc.index is not None else 0
                slot = tool_calls.setdefault(
                    idx,
                    {"id": "", "type": "function",
                     "function": {"name": "", "arguments": ""}},
                )
                if tc.id:
                    slot["id"] = tc.id
                if tc.type:
                    slot["type"] = tc.type
                if tc.function:
                    if tc.function.name:
                        slot["function"]["name"] += tc.function.name
                    if tc.function.arguments:
                        slot["function"]["arguments"] += tc.function.arguments

        self.counter.add(final_usage)
        if thinking is not None:
            thinking.finalize(self.counter.last_reasoning)
        self._refresh_status()
        # After the last chunk, max_scroll_y may still grow in the next
        # layout pass — defer a final scroll so we land on the real bottom.
        if self._follow_bottom:
            view.call_after_refresh(view.scroll_end, animate=False)

        msg: dict = {"role": "assistant", "content": answer.text if answer else ""}
        if thinking is not None and thinking.text:
            msg["reasoning_content"] = thinking.text
        if tool_calls:
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return msg

    # ─ subagent ─

    async def _run_subagent(self, prompt: str, system: str) -> str:
        """Run an isolated subagent and return its final assistant text.

        The subagent gets a fresh `ToolContext` (so its bash jobs don't
        appear in the parent's sidebar and don't count against the
        parent's BG_MAX_CONCURRENT) but inherits the parent's work_dir,
        OpenAI client, and model / effort settings. Token usage rolls
        up into the parent counter so the status-bar gradient reflects
        the combined cost.

        The subagent's tool list excludes `spawn_agent` itself (no
        nesting). All tool calls still walk through `self.tool_confirm`
        so a user policy applies everywhere, not just to the parent.

        `reasoning_content` is preserved on the assistant message and
        written back. DeepSeek's thinking-mode API rejects requests
        that strip it from the prior assistant turn ("The
        `reasoning_content` in the thinking mode must be passed back
        to the API."), so this is mandatory, not optional.
        """
        sub_ctx = ToolContext(work_dir=self.ctx.work_dir)
        sub_messages: list = [
            {"role": "system", "content": SYSTEM_PROMPT + sub_ctx.work_dir},
        ]
        if system.strip():
            sub_messages.append({
                "role": "system",
                "content": f"# Subagent role / constraints\n\n{system.strip()}",
            })
        sub_messages.append({"role": "user", "content": prompt})

        # Hide spawn_agent from the subagent so it can't recurse.
        sub_tools = [
            t for t in TOOLS if t["function"]["name"] != "spawn_agent"
        ]

        last_content = ""
        try:
            for _turn in range(SUBAGENT_MAX_TURNS):
                try:
                    stream = await self.client.chat.completions.create(
                        model=self.model,
                        messages=sub_messages,
                        tools=sub_tools,
                        tool_choice="auto",
                        stream=True,
                        stream_options={"include_usage": True},
                        reasoning_effort=self.effort,
                        extra_body={"thinking": {"type": "enabled"}},
                    )
                except Exception as e:
                    return f"❌ subagent stream failed: {e}"

                content = ""
                reasoning = ""
                tool_calls: dict[int, dict] = {}
                final_usage = None
                async for chunk in stream:
                    if getattr(chunk, "usage", None):
                        final_usage = chunk.usage
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        reasoning += rc
                    text = getattr(delta, "content", None)
                    if text:
                        content += text
                    for tc in getattr(delta, "tool_calls", None) or []:
                        idx = tc.index if tc.index is not None else 0
                        slot = tool_calls.setdefault(
                            idx,
                            {"id": "", "type": "function",
                             "function": {"name": "", "arguments": ""}},
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.type:
                            slot["type"] = tc.type
                        if tc.function:
                            if tc.function.name:
                                slot["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                slot["function"]["arguments"] += tc.function.arguments

                # Roll subagent usage into parent counter so the status
                # bar reflects combined spend.
                self.counter.add(final_usage)
                self._refresh_status()

                last_content = content
                msg: dict = {"role": "assistant", "content": content}
                if reasoning:
                    # Mandatory: DeepSeek thinking mode 400s if the
                    # prior assistant turn's reasoning_content is
                    # missing on the next request.
                    msg["reasoning_content"] = reasoning
                if tool_calls:
                    msg["tool_calls"] = [
                        tool_calls[i] for i in sorted(tool_calls)
                    ]
                sub_messages.append(msg)

                if not tool_calls:
                    return content or "(subagent returned no answer)"

                for tc in msg["tool_calls"]:
                    name = tc["function"]["name"]
                    raw = tc["function"].get("arguments") or "{}"
                    try:
                        args = json.loads(raw)
                    except json.JSONDecodeError:
                        result = f"Error: bad JSON args: {raw}"
                    else:
                        if name == "spawn_agent":
                            # Defense in depth — the schema is hidden
                            # but models occasionally still try anyway.
                            result = (
                                "Error: subagents cannot spawn further "
                                "subagents."
                            )
                        else:
                            allowed = await self.tool_confirm(name, args)
                            if allowed:
                                result = await asyncio.to_thread(
                                    execute_tool, sub_ctx, name, args
                                )
                                # Strip the diff so a 1000-line patch
                                # doesn't eat the subagent's context.
                                if name in (
                                    "edit_file", "edit_lines", "multi_edit"
                                ):
                                    head, sep, diff = result.partition("\n\n")
                                    if sep and "@@" in diff:
                                        result = head
                            else:
                                result = "Tool execution blocked by user policy."
                    sub_messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

            return (
                f"⚠ subagent reached the {SUBAGENT_MAX_TURNS}-turn cap "
                f"without a final answer. Last partial assistant text:"
                f"\n\n{last_content}"
            )
        finally:
            # Subagent is done (returned, raised, or cancelled). Don't
            # leak background bash jobs it may have started.
            kill_all_bash_jobs(sub_ctx.bash_jobs)


# ───────── entry ─────────

def load_api_key() -> str:
    """Resolve the DeepSeek API key.

    Lookup order:
      1. $DEEPSEEK_API_KEY env var (preferred — works without a file).
      2. The file at $DEEPSEEK_API_KEY_FILE, or ~/deepseek_apikey.txt.
    """
    env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        key = Path(API_KEY_PATH).read_text().strip()
    except Exception as e:
        raise SystemExit(
            f"No DEEPSEEK_API_KEY env var, and cannot read API key from "
            f"{API_KEY_PATH}: {e}"
        )
    if not key:
        raise SystemExit(f"API key file is empty: {API_KEY_PATH}")
    return key


def load_agents_md(cwd: str) -> str | None:
    """Read `<cwd>/AGENTS.md` and return its content (or None if absent).

    AGENTS.md hosts project-specific guidance for the agent — coding
    conventions, do-not-touch areas, deployment quirks, etc. Loaded
    once at startup; restart the app (or Ctrl+L preserves system
    messages so a re-launch isn't required for in-session reset) to
    pick up changes.

    Truncated at AGENTS_MD_MAX_CHARS to keep the system prompt bounded.
    """
    p = Path(cwd) / AGENTS_MD_FILENAME
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not text:
        return None
    if len(text) > AGENTS_MD_MAX_CHARS:
        text = (
            text[:AGENTS_MD_MAX_CHARS]
            + f"\n\n…[truncated at {AGENTS_MD_MAX_CHARS} chars]"
        )
    return text


def main() -> None:
    AgentApp(api_key=load_api_key()).run()


if __name__ == "__main__":
    main()
