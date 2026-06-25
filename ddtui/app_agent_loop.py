"""Parent agent streaming and tool-loop mixin."""

from __future__ import annotations

import asyncio
import json

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from .app_errors import _exception_block
from .app_support import _merge_tool_call_delta
from .config import SUBAGENT_RESULT_MAX_CHARS
from .tools import TOOLS, execute_tool
from .widgets import (
    AssistantMessage,
    DiffBlock,
    StatusBar,
    SteerBubble,
    ThinkingBlock,
    TodoBlock,
    ToolCallBlock,
    UserBubble,
)

# compact_self is a subagent-only meta-tool: the parent has its own
# /compact slash command and shouldn't see compact_self in the
# advertised tool list. Filtered once at import.
PARENT_TOOLS = [
    t for t in TOOLS if t["function"]["name"] != "compact_self"
]


class AppAgentLoopMixin:
    def _pop_task_event_text(self) -> str | None:
        if not self._task_events:
            return None
        events = self._task_events
        self._task_events = []
        return "\n\n".join(events)

    async def _mount_task_event_notice(self, text: str) -> None:
        await self._mount_widget(Static(Text(text, style="bold #94e2d5")))

    async def _drain_task_events_to_messages(self) -> None:
        text = self._pop_task_event_text()
        if not text:
            return
        self.messages.append({"role": "user", "content": text})
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
            self._agent_turn(text), exclusive=True
        )

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
                self._ensure_autosave_path()
                self._write_turn_journal(
                    phase="turn_started",
                    pending_user_text=pending,
                    message_len_before_turn=len(self.messages),
                    tool_started=False,
                )
                self.messages.append({"role": "user", "content": pending})
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
                    return
                self._clear_turn_journal()
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
            # ESC×2 cancellation: keep `self.messages` and the user's
            # bubble so the next submission picks up with full context.
            # The half-streamed thinking/answer for a stream-time cancel
            # is already gone (`_stream_one` removed those widgets and
            # never appended the assistant turn). But a tool-time cancel
            # leaves `messages` ending in `assistant(tool_calls=[...])`
            # with some/all tool results missing — the OpenAI/DeepSeek
            # protocol then rejects the next request. Pair every orphan
            # tool_call with a stub `tool` result before re-raising so
            # the history stays protocol-valid.
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

    async def _run_one_turn(self) -> bool:
        """Inner loop: one user→assistant→(tools→assistant)+ turn.

        Returns True if the turn completed normally, False if an API
        error was caught (the error is shown to the UI and the trailing
        user/tool messages are rolled back so the user can retry).
        """
        try:
            while True:
                # Managed task completions are delivered at model-call
                # boundaries, never by fabricating tool results.
                await self._drain_task_events_to_messages()

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

                self._write_turn_journal(
                    phase="streaming",
                    message_len_current=len(self.messages),
                )
                assistant_msg = await self._stream_one()
                self.messages.append(assistant_msg)
                await self._autosave_conversation()
                tool_calls = assistant_msg.get("tool_calls") or []
                if not tool_calls:
                    return True
                for tc in tool_calls:
                    name = tc["function"]["name"]
                    raw = tc["function"].get("arguments") or "{}"
                    self._write_turn_journal(
                        phase="tool",
                        tool_started=True,
                        active_tool_name=name,
                        active_tool_call_id=tc.get("id"),
                        message_len_current=len(self.messages),
                    )
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
                            await self._autosave_conversation()
                            self._write_turn_journal(
                                phase="after_tool",
                                message_len_current=len(self.messages),
                            )
                            continue

                        if name in ("checkpoint_tool", "checkpoint_clear"):
                            result = await asyncio.to_thread(
                                execute_tool, self.ctx, name, args
                            )
                            if not result.startswith("Error:"):
                                await self._sync_checkpoint_block()
                            self.messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": result,
                                }
                            )
                            await self._autosave_conversation()
                            self._write_turn_journal(
                                phase="after_tool",
                                message_len_current=len(self.messages),
                            )
                            continue

                        if name == "compact_self":
                            # Defense in depth — PARENT_TOOLS already
                            # hides this from the parent, but a model
                            # may recall it from elsewhere. Redirect
                            # to the user-facing /compact command
                            # rather than silently failing.
                            block = ToolCallBlock(name, args)
                            await self._mount_widget(block)
                            result = (
                                "Error: compact_self is a subagent-only "
                                "tool. The main agent should compress "
                                "history via the /compact slash command "
                                "(user-triggered), not as a tool call."
                            )
                            block.set_result(result, blocked=True)
                            self.messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result,
                            })
                            await self._autosave_conversation()
                            self._write_turn_journal(
                                phase="after_tool",
                                message_len_current=len(self.messages),
                            )
                            continue

                        if name in (
                            "spawn_agent", "chat_agent",
                            "await_agent", "end_agent",
                        ):
                            # Subagent meta-tools need LLM client / app
                            # state, so they bypass the generic tool
                            # dispatch. spawn_agent and chat_agent are
                            # fire-and-forget — they schedule a task
                            # and return immediately. await_agent is
                            # the only one that blocks; it's cancellable
                            # via parent ESC×2 and will NOT cancel the
                            # underlying subagent task on its own
                            # timeout (use end_agent for that).
                            block = ToolCallBlock(name, args)
                            await self._mount_widget(block)
                            try:
                                if name == "spawn_agent":
                                    prompt = args.get("prompt") or ""
                                    sub_system = args.get("system") or ""
                                    result = self._spawn_subagent(
                                        prompt, sub_system
                                    )
                                elif name == "chat_agent":
                                    sid = args.get("session_id") or ""
                                    prompt = args.get("prompt") or ""
                                    result = self._chat_subagent(
                                        sid, prompt
                                    )
                                elif name == "await_agent":
                                    sid = args.get("session_id") or ""
                                    timeout = args.get("timeout")
                                    result = await self._await_subagent(
                                        sid, timeout
                                    )
                                else:
                                    sid = args.get("session_id") or ""
                                    result = self._end_subagent(sid)
                            except asyncio.CancelledError:
                                result = f"⛔ {name} cancelled (parent ESC×2)"
                                block.set_result(result, blocked=True)
                                self.messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": result,
                                })
                                await self._autosave_conversation()
                                self._write_turn_journal(
                                    phase="after_tool",
                                    message_len_current=len(self.messages),
                                )
                                raise
                            if len(result) > SUBAGENT_RESULT_MAX_CHARS:
                                result = (
                                    result[:SUBAGENT_RESULT_MAX_CHARS]
                                    + f"\n…[+{len(result) - SUBAGENT_RESULT_MAX_CHARS} chars truncated]"
                                )
                            blocked = result.startswith("Error:")
                            block.set_result(result, blocked=blocked)
                            self.messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": result,
                            })
                            await self._autosave_conversation()
                            self._write_turn_journal(
                                phase="after_tool",
                                message_len_current=len(self.messages),
                            )
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
                            if name in (
                                "apply_patch",
                                "edit_file",
                                "edit_lines",
                                "multi_edit",
                            ):
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
                            result = "Tool execution blocked by user policy / confirmation dialog."
                            block.set_result(result, blocked=True)
                    self.messages.append(
                        {"role": "tool", "tool_call_id": tc["id"], "content": result}
                    )
                    await self._autosave_conversation()
                    self._write_turn_journal(
                        phase="after_tool",
                        message_len_current=len(self.messages),
                    )
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

    async def _stream_one(self) -> dict:
        thinking: ThinkingBlock | None = None
        answer: AssistantMessage | None = None
        tool_calls: dict[int, dict] = {}
        final_usage = None

        # `_mount_widget` scrolls to the end on mount, but widgets that
        # grow via `update()` (the streaming case) don't trigger scroll
        # on their own — so explicitly follow the bottom on every chunk.
        view = self.query_one("#conversation", VerticalScroll)

        try:
            async for event in self.provider.stream(
                self.messages,
                PARENT_TOOLS,
                self.model,
                self.effort,
            ):
                if event.usage is not None:
                    final_usage = event.usage

                if event.reasoning:
                    if thinking is None:
                        thinking = ThinkingBlock()
                        await self._mount_widget(thinking)
                    thinking.append_text(event.reasoning)
                    if self._follow_bottom:
                        view.scroll_end(animate=False)

                if event.content:
                    if answer is None:
                        answer = AssistantMessage()
                        await self._mount_widget(answer)
                    answer.append_text(event.content)
                    if self._follow_bottom:
                        view.scroll_end(animate=False)

                if event.tool_call is not None:
                    _merge_tool_call_delta(tool_calls, event.tool_call)
        except BaseException:
            # ESC×2 cancellation or stream error: drop the half-streamed
            # ThinkingBlock / AssistantMessage from the view. Neither is
            # in `self.messages` yet (the append happens after the stream
            # finishes), so removing the widgets keeps the view in sync —
            # no ghost bubbles linger. Re-raise so the outer turn handler
            # still sees the original exception.
            if thinking is not None:
                try:
                    thinking.remove()
                except Exception:
                    pass
            if answer is not None:
                try:
                    answer.remove()
                except Exception:
                    pass
            raise

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
