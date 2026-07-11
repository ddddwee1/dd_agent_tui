"""Parent agent turn driver: TurnEngine host + Textual observer.

The actual LLM tool loop lives in `ddtui.engine.TurnEngine` (shared
with subagents). This module owns the parent-specific glue:

- `ParentTurnObserver` renders engine events into the main conversation
  (thinking/answer bubbles, tool blocks, diffs), mirrors them to the
  remote relay, and persists history (autosave + turn journal).
- `_parent_meta_handler` serves the app-level meta tools that need live
  app state: the subagent four, the explore span three, and the
  compact_self redirect.
- `_agent_turn` (outer loop) is unchanged: it commits user messages,
  drains the queued-input tray between turns, and owns busy state,
  cancellation, and rollback.
"""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from .app_errors import _exception_block
from .config import SUBAGENT_RESULT_MAX_CHARS
from .engine import ToolOutcome, TurnEngine, TurnObserver
from .runtime_messages import runtime_task_event_message
from .tools import PARENT_TOOL_SCHEMAS
from .widgets import (
    AssistantMessage,
    DiffBlock,
    StatusBar,
    SteerBubble,
    ThinkingBlock,
    TodoBlock,
    TraceBlock,
    ToolRunBlock,
    UserBubble,
)

# Parent-visible schemas come straight from the registry (currently:
# everything except the subagent-only compact_self).
PARENT_TOOLS = PARENT_TOOL_SCHEMAS


class ParentTurnObserver(TurnObserver):
    """Bridges TurnEngine events to the main-conversation UI, the
    remote relay, and history persistence (autosave + turn journal)."""

    # Sidebar-rendered tools: no ToolRunBlock in the conversation flow,
    # their UI lives in the sidebar (TodoBlock / CheckpointBlock).
    NO_BLOCK_TOOLS = frozenset({
        "todo_tool", "checkpoint_tool", "checkpoint_clear",
    })

    def __init__(self, app) -> None:
        self.app = app
        self._thinking: ThinkingBlock | None = None
        self._answer: AssistantMessage | None = None
        self._trace: TraceBlock | None = None
        self._tool_run: ToolRunBlock | None = None
        self._blocks: dict[str, ToolRunBlock] = {}

    def _scroll_view(self) -> VerticalScroll:
        return self.app.query_one("#conversation", VerticalScroll)

    def _emit(self, event: str, payload: dict) -> None:
        try:
            self.app._remote_emit(event, payload)
        except Exception:
            pass

    async def _ensure_trace(self) -> TraceBlock:
        if self._trace is None:
            self._trace = TraceBlock()
            await self.app._mount_widget(self._trace)
        return self._trace

    # ── streaming ──

    async def on_reasoning_delta(self, text: str) -> None:
        if self._thinking is None:
            self._thinking = ThinkingBlock()
            trace = await self._ensure_trace()
            await trace.mount_trace_widget(self._thinking)
        self._thinking.append_text(text)
        if self._trace is not None:
            self._trace.refresh_summary()
        self._emit("assistant.delta", {"kind": "reasoning", "text": text})
        if self.app._follow_bottom:
            self._scroll_view().scroll_end(animate=False)

    async def on_content_delta(self, text: str) -> None:
        if self._answer is None:
            self._answer = AssistantMessage()
            await self.app._mount_widget(self._answer)
        self._answer.append_text(text)
        self._emit("assistant.delta", {"kind": "content", "text": text})
        if self.app._follow_bottom:
            self._scroll_view().scroll_end(animate=False)

    def on_stream_aborted(self) -> None:
        # Half-streamed widgets are not yet backed by any message in
        # history (append happens after the stream finishes) — remove
        # them so no ghost bubbles linger after ESC×2 / stream errors.
        for w in (self._thinking, self._answer):
            if w is not None:
                try:
                    w.remove()
                except Exception:
                    pass
                if self._trace is not None:
                    self._trace.discard_trace_widget(w)
        self._thinking = None
        self._answer = None
        self._tool_run = None
        if self._trace is not None and self._trace.is_empty:
            try:
                self._trace.remove()
            except Exception:
                pass
        self._trace = None

    async def on_usage(self, usage) -> None:
        self.app.counter.add(usage)

    async def on_assistant_message(self, msg: dict) -> None:
        if (
            self._answer is not None
            and self._answer.text != (msg.get("content") or "")
        ):
            self._answer.set_text(msg.get("content") or "")
        if self._thinking is not None:
            self._thinking.finalize(self.app.counter.last_reasoning)
            if self._trace is not None:
                self._trace.refresh_summary()
        self.app._refresh_status()
        if self.app._follow_bottom:
            view = self._scroll_view()
            view.call_after_refresh(view.scroll_end, animate=False)
        self._emit("message.append", {"message": msg, "source": "assistant"})
        self._thinking = None
        self._answer = None
        self._tool_run = None
        if not (msg.get("tool_calls") or []):
            self._trace = None

    # ── tools ──

    async def on_tool_started(self, tc_id: str, name: str, args: dict) -> None:
        self._emit(
            "tool.started",
            {"tool_call_id": tc_id, "name": name, "arguments": args},
        )
        self.app._write_turn_journal(
            phase="tool",
            tool_started=True,
            active_tool_name=name,
            active_tool_call_id=tc_id,
            message_len_current=len(self.app.messages),
        )
        if name not in self.NO_BLOCK_TOOLS:
            if self._tool_run is None:
                self._tool_run = ToolRunBlock()
                trace = await self._ensure_trace()
                await trace.mount_trace_widget(self._tool_run)
            self._tool_run.add_call(tc_id, name, args)
            if self._trace is not None:
                self._trace.refresh_summary()
            self._blocks[tc_id or name] = self._tool_run

    async def on_tool_result(
        self, tc_id: str, name: str, args: dict, outcome: ToolOutcome
    ) -> None:
        block = self._blocks.pop(tc_id or name, None)
        if block is not None:
            block.set_result(tc_id, outcome.content, blocked=not outcome.ok)
            if self._trace is not None:
                self._trace.refresh_summary()
        if outcome.diff:
            await self.app._mount_widget(
                DiffBlock(args.get("path", "?"), outcome.diff)
            )
        if name == "todo_tool":
            await self.app._update_todo_sidebar(args)
        elif name in ("checkpoint_tool", "checkpoint_clear") and outcome.ok:
            await self.app._sync_checkpoint_block()

    async def on_history_appended(self, msg: dict, kind: str) -> None:
        await self.app._autosave_conversation()
        if kind == "tool":
            self.app._write_turn_journal(
                phase="after_tool",
                message_len_current=len(self.app.messages),
            )


class AppAgentLoopMixin:
    # ── async-task completion events ──

    def _pop_task_event_text(self) -> str | None:
        if not self._task_events:
            return None
        events = self._task_events
        self._task_events = []
        return "\n\n".join(events)

    async def _mount_task_event_notice(self, text: str) -> None:
        await self._mount_widget(Static(Text(text, style="bold #66d9ef")))

    async def _drain_task_events_to_messages(self) -> None:
        text = self._pop_task_event_text()
        if not text:
            return
        self.messages.append(runtime_task_event_message(text))
        await self._mount_task_event_notice(text)

    async def _start_task_event_turn(self) -> None:
        self._task_event_wake_scheduled = False
        if self._busy:
            return
        text = self._pop_task_event_text()
        if not text:
            return
        self._follow_bottom = True
        await self._mount_task_event_notice(text)
        self._agent_worker = self.run_worker(
            self._agent_turn(text, runtime_event=True), exclusive=True
        )

    # ── outer loop: one or more user turns ──

    async def _agent_turn(self, user_text: str, runtime_event: bool = False) -> None:
        """Outer loop: drives one or more user turns end-to-end.

        Drains the queue between turns — every iteration commits one
        user message to history, runs `_run_one_turn` to completion (or
        error), and then either pops the next queued message or returns.
        """
        self._set_busy(True)
        pending = user_text
        pending_is_runtime = runtime_event
        try:
            while True:
                self._ensure_autosave_path()
                self._write_turn_journal(
                    phase="turn_started",
                    pending_user_text=pending,
                    message_len_before_turn=len(self.messages),
                    tool_started=False,
                )
                user_message = (
                    runtime_task_event_message(pending)
                    if pending_is_runtime
                    else {"role": "user", "content": pending}
                )
                self.messages.append(user_message)
                try:
                    self._remote_emit(
                        "message.append",
                        {
                            "message": user_message,
                            "source": "turn",
                        },
                    )
                except Exception:
                    pass
                await self._autosave_conversation()
                ok = await self._run_one_turn()
                if not ok:
                    self._clear_turn_journal()
                    # Turn-level runtime error already surfaced + rolled back.
                    # Wipe the queue: don't railroad the user into
                    # more failing turns; let them re-submit if they want.
                    self._drop_queue()
                    self._refresh_status()
                    return
                if not self._queued:
                    self._clear_turn_journal()
                    # Quiet boundary: turn done, queue drained, busy
                    # still held — the only safe moment to auto-compact
                    # without racing user input.
                    await self._auto_compact_if_needed()
                    return
                self._clear_turn_journal()
                pending, parked = self._queued.pop(0)
                pending_is_runtime = False
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
            # ESC×2 cancellation: keep `self.messages` and the user's
            # bubble so the next submission picks up with full context.
            # The engine appends a "⛔ cancelled" stub for a tool that
            # was cancelled mid-execution; a cancel mid-parallel-batch
            # or mid-stream can still leave orphan tool_calls, so the
            # pairing fallback below keeps history protocol-valid.
            self._pair_orphan_tool_calls()
            self._clear_turn_journal()
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
            # Fold older widgets out of layout once the turn is fully
            # quiescent. Doing it here (not mid-stream) keeps the view
            # stable while the model is talking.
            try:
                await self._maybe_collapse_history()
            except Exception:
                pass
            await self._autosave_conversation()
            try:
                self._remote_emit_snapshot("turn_finished")
            except Exception:
                pass

    # ── inner loop: TurnEngine host ──

    async def _before_round(self) -> None:
        """Pre-round hook: deliver managed-task completions and steer
        interjections at model-call boundaries, then journal."""
        await self._drain_task_events_to_messages()

        if self._steer:
            pending_steer = self._steer
            self._steer = []
            for s, parked in pending_steer:
                self.messages.append(
                    {"role": "user", "content": f"[实时插话] {s}"}
                )
                try:
                    self._remote_emit(
                        "message.append",
                        {
                            "message": {
                                "role": "user",
                                "content": f"[实时插话] {s}",
                            },
                            "source": "steer",
                        },
                    )
                except Exception:
                    pass
                # Move the bubble out of the pending tray and into the
                # conversation so the user can scroll back through
                # their interjections later.
                try:
                    parked.remove()
                except Exception:
                    pass
                await self._mount_widget(SteerBubble(s))
            self._refresh_status()

        self._write_turn_journal(
            phase="streaming",
            message_len_current=len(self.messages),
        )

    async def _run_one_turn(self) -> bool:
        """One user→assistant→(tools→assistant)+ turn via TurnEngine.

        Returns True if the turn completed normally, False if an API
        error was caught (the error is shown to the UI and the trailing
        user/tool messages are rolled back so the user can retry).
        """
        engine = TurnEngine(
            provider=self.provider,
            ctx=self.ctx,
            messages=self.messages,
            tools=PARENT_TOOLS,
            model=self.model,
            effort=self.effort,
            observer=ParentTurnObserver(self),
            tool_confirm=self.tool_confirm,
            meta_handler=self._parent_meta_handler,
            before_round=self._before_round,
        )
        try:
            await engine.run_turn()
            return True
        except Exception as e:
            # Roll back the in-flight turn from history AND wipe its
            # widgets from the view, then surface the error in a fresh
            # slot. Otherwise the UserBubble + half-streamed thinking
            # would linger on screen as ghost content no longer in
            # `self.messages` — confusing on retry.
            self._rollback_last_user_turn()
            self._wipe_orphaned_turn_widgets()
            await self._mount_widget(
                _exception_block("本轮运行失败（已回滚）", e)
            )
            return False

    async def _parent_meta_handler(
        self, tc_id: str, name: str, args: dict, batch_size: int
    ) -> str | None:
        """App-level meta tools: subagents, explore spans, compact_self.

        Returns the result text, or None for anything that should go
        down the generic dispatch path. Exceptions other than
        CancelledError are converted to Error strings here so a broken
        meta tool degrades to a failed call, not a failed turn.
        """
        if name == "compact_self":
            # Defense in depth — PARENT_TOOLS already hides this from
            # the parent, but a model may recall it from elsewhere.
            return (
                "Error: compact_self is a subagent-only tool. The main "
                "agent should compress history via the /compact slash "
                "command (user-triggered), not as a tool call."
            )
        if name == "task_pause":
            return (
                "Error: task_pause is a subagent-only tool. The main agent "
                "should finish the current response and let task_start "
                "notifications wake the next turn."
            )

        if name in ("explore_start", "explore_end", "explore_cancel"):
            try:
                if name == "explore_start":
                    return await self._explore_start_tool(args, batch_size)
                if name == "explore_end":
                    return await self._explore_end_tool(args, batch_size)
                return await self._explore_cancel_tool(args, batch_size)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return f"Error: {name} failed: {type(e).__name__}: {e}"

        if name in (
            "spawn_agent", "chat_agent", "agent_check",
            "await_agent", "end_agent",
        ):
            # spawn_agent and chat_agent are fire-and-forget — they
            # schedule a task and return immediately. agent_check is
            # the non-blocking status/result path; await_agent remains
            # a compatibility stub for old conversations.
            if name == "spawn_agent":
                result = self._spawn_subagent(
                    args.get("prompt") or "",
                    args.get("system") or "",
                    model=args.get("model"),
                    effort=args.get("effort"),
                )
            elif name == "chat_agent":
                result = self._chat_subagent(
                    args.get("session_id") or "", args.get("prompt") or ""
                )
            elif name == "agent_check":
                result = self._check_subagent(args.get("session_id") or "")
            elif name == "await_agent":
                result = await self._await_subagent(
                    args.get("session_id") or "", args.get("timeout")
                )
            else:
                result = self._end_subagent(args.get("session_id") or "")
            if len(result) > SUBAGENT_RESULT_MAX_CHARS:
                result = (
                    result[:SUBAGENT_RESULT_MAX_CHARS]
                    + f"\n…[+{len(result) - SUBAGENT_RESULT_MAX_CHARS} chars truncated]"
                )
            return result

        return None

    # ── sidebar side-effects ──

    async def _update_todo_sidebar(self, args: dict) -> None:
        """Mirror a todo_tool call into the sidebar TodoBlock; fade it
        out once every item is completed."""
        items = args.get("items", []) if isinstance(args, dict) else []
        sidebar = self.query_one("#sidebar", Vertical)
        self._cancel_dismiss()
        if self._todo_block is None or not self._todo_block.is_mounted:
            self._todo_block = TodoBlock()
            await sidebar.mount(self._todo_block)
        self._todo_block.set_items(items)
        sidebar.add_class("visible")
        if items and all(i.get("status") == "completed" for i in items):
            self._start_dismiss()
