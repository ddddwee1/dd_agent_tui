"""Persistent subagent session mixin for AgentApp."""

from __future__ import annotations

import asyncio
import json
import time

from .app_support import _merge_tool_call_delta, load_agents_md
from .config import (
    COMPACT_KEEP_RECENT_TURNS,
    MAX_LIVE_SUBAGENTS,
    POST_SYSTEM_PROMPT,
    SUBAGENT_DEFAULT_AWAIT_TIMEOUT,
    SUBAGENT_MAX_AWAIT_TIMEOUT,
    SYSTEM_PROMPT,
)
from .state import (
    SubagentSession,
    ToolContext,
    kill_all_bash_jobs,
    kill_all_tasks,
    kill_all_terminals,
)
from .tools import TOOLS, execute_tool
from .widgets import SubagentTabPane


class AppSubagentMixin:
    async def _run_subagent_round(self, sess: SubagentSession) -> str:
        """Pump the LLM tool-loop until the subagent emits a final
        answer (assistant message with no tool_calls), and return that
        text.

        Caller is responsible for having appended the user message to
        sess.messages before calling. No internal turn cap — a wedged
        or looping subagent will keep burning tokens until the user
        cancels via end_agent / /clear (or ESC×2 on the parent if the
        parent is currently awaiting). The idle reaper, MAX_LIVE_SUB-
        AGENTS, and SUBAGENT_RESULT_MAX_CHARS still apply as soft
        guards.

        Phase transitions: thinking → (answering | tool)* → idle on
        success, error on stream failure. last_active_at is bumped in
        `finally` so the idle reaper sees the right time even on the
        cancelled / errored paths.

        Streams reasoning/content tokens into `sess.pane` as they
        arrive (mirroring the main `_stream_one` UX), and mounts the
        ToolCallBlock for each tool call live so the user sees
        execution start without waiting for the 1 Hz fallback refresh.
        `reasoning_content` is preserved on every assistant message —
        DeepSeek thinking-mode 400s if it's missing on the next call.
        """
        pane = sess.pane
        try:
            while True:
                sess.turn += 1
                sess.phase = "thinking"
                sess.last_tool = None
                content = ""
                reasoning = ""
                tool_calls: dict[int, dict] = {}
                final_usage = None
                thinking_started = False
                answer_started = False

                # Render any newly-appended user message (spawn prompt
                # or chat_agent follow-up) BEFORE live thinking mounts,
                # so DOM order in the pane stays user → thinking →
                # answer → tools.
                if pane is not None:
                    try:
                        pane.refresh_from_session()
                    except Exception:
                        pass

                try:
                    async for event in self.provider.stream(
                        sess.messages,
                        sess.sub_tools,
                        self.model,
                        self.effort,
                    ):
                        if event.usage is not None:
                            final_usage = event.usage
                        if event.reasoning:
                            reasoning += event.reasoning
                            if pane is not None:
                                if not thinking_started:
                                    pane.start_thinking()
                                    thinking_started = True
                                pane.append_thinking(event.reasoning)
                        if event.content:
                            content += event.content
                            sess.phase = "answering"
                            if pane is not None:
                                if not answer_started:
                                    pane.start_answer()
                                    answer_started = True
                                pane.append_answer(event.content)
                        if event.tool_call is not None:
                            _merge_tool_call_delta(tool_calls, event.tool_call)
                            sess.phase = "answering"
                except Exception as e:
                    sess.phase = "error"
                    if pane is not None:
                        try:
                            pane.remove_live_blocks()
                            pane.mount_exception_notice(
                                f"⛔ stream failed: {type(e).__name__}: {e}"
                            )
                        except Exception:
                            pass
                    return f"❌ subagent stream failed: {e}"

                # Record subagent usage in the parent counter so the
                # status bar reflects the latest context pressure; also
                # accumulate per-session counts for the sidebar widget.
                self.counter.add(final_usage)
                if final_usage is not None:
                    sess.tokens_in += getattr(final_usage, "prompt_tokens", 0) or 0
                    sess.tokens_out += getattr(final_usage, "completion_tokens", 0) or 0
                    # Track THIS call's prompt tokens (not cumulative) so
                    # the sidebar + await_agent gauge reflects how full
                    # the next request would be.
                    sess.last_prompt_tokens = (
                        getattr(final_usage, "prompt_tokens", 0) or 0
                    )
                self._refresh_status()

                # Finalize the live widgets — counter.last_reasoning was
                # just bumped by counter.add(final_usage) above, so it
                # carries THIS round's reasoning token count.
                if pane is not None:
                    try:
                        pane.finalize_thinking(self.counter.last_reasoning)
                        pane.finalize_answer()
                    except Exception:
                        pass

                msg: dict = {"role": "assistant", "content": content}
                if reasoning:
                    # Mandatory for DeepSeek thinking mode on the next
                    # request — preserved verbatim.
                    msg["reasoning_content"] = reasoning
                if tool_calls:
                    msg["tool_calls"] = [
                        tool_calls[i] for i in sorted(tool_calls)
                    ]
                sess.messages.append(msg)
                if pane is not None:
                    pane.mark_round_committed()

                if not tool_calls:
                    # Final answer for THIS round. Session stays alive
                    # at "idle" — the parent can chat_agent again later.
                    sess.phase = "idle"
                    return content or "(subagent returned no answer)"

                for tc in msg["tool_calls"]:
                    name = tc["function"]["name"]
                    sess.phase = "tool"
                    sess.last_tool = name
                    raw = tc["function"].get("arguments") or "{}"
                    try:
                        args = json.loads(raw)
                    except json.JSONDecodeError:
                        if pane is not None:
                            try:
                                pane.add_tool_block(
                                    tc["id"], name, {"_raw": raw}
                                )
                            except Exception:
                                pass
                        result = f"Error: bad JSON args: {raw}"
                        if pane is not None:
                            try:
                                pane.set_tool_result(
                                    tc["id"], result, blocked=True
                                )
                            except Exception:
                                pass
                    else:
                        if pane is not None:
                            try:
                                pane.add_tool_block(tc["id"], name, args)
                            except Exception:
                                pass
                        if name in (
                            "spawn_agent", "chat_agent",
                            "await_agent", "end_agent",
                        ):
                            # Defense in depth — schemas are filtered
                            # but models sometimes try anyway.
                            result = (
                                f"Error: subagents cannot call {name} "
                                "(no nested subagents)."
                            )
                            if pane is not None:
                                try:
                                    pane.set_tool_result(
                                        tc["id"], result, blocked=True
                                    )
                                except Exception:
                                    pass
                        elif name == "compact_self":
                            # Subagent-only meta-tool: rewrite the
                            # session's own messages with a summary.
                            # Reuses _compact_messages from
                            # AppHistoryMixin so /compact and
                            # compact_self share the exact same prompt
                            # / cut policy. No tool_confirm — this only
                            # mutates in-process memory, no files /
                            # shell side effects.
                            try:
                                new_messages, stats = (
                                    await self._compact_messages(
                                        sess.messages
                                    )
                                )
                            except ValueError as e:
                                result = f"Error: {e}"
                            except Exception as e:
                                result = (
                                    f"Error: compact_self failed: "
                                    f"{type(e).__name__}: {e}"
                                )
                            else:
                                sess.messages = new_messages
                                result = (
                                    f"已压缩：{stats['before_n']} → "
                                    f"{stats['after_n']} 条消息，约 "
                                    f"-{stats['saved']:,} 字符（保留最近 "
                                    f"{COMPACT_KEEP_RECENT_TURNS} 轮原文）。"
                                )
                            if pane is not None:
                                try:
                                    pane.set_tool_result(
                                        tc["id"], result,
                                        blocked=result.startswith("Error"),
                                    )
                                except Exception:
                                    pass
                        else:
                            allowed = await self.tool_confirm(name, args)
                            if allowed:
                                result = await asyncio.to_thread(
                                    execute_tool, sess.ctx, name, args
                                )
                                # Strip the diff so a 1000-line patch
                                # doesn't eat the subagent's context.
                                if name in (
                                    "apply_patch",
                                    "edit_file",
                                    "edit_lines",
                                    "multi_edit",
                                ):
                                    head, sep, diff = result.partition("\n\n")
                                    if sep and "@@" in diff:
                                        result = head
                                if pane is not None:
                                    try:
                                        pane.set_tool_result(tc["id"], result)
                                    except Exception:
                                        pass
                            else:
                                result = "Tool execution blocked by user policy / confirmation dialog."
                                if pane is not None:
                                    try:
                                        pane.set_tool_result(
                                            tc["id"], result, blocked=True
                                        )
                                    except Exception:
                                        pass
                    sess.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
                    if pane is not None:
                        pane.mark_round_committed()
        except asyncio.CancelledError:
            # Pane is usually being torn down (/clear, end_agent), but
            # clean up live widgets defensively so a survivor pane
            # doesn't show a forever-streaming ThinkingBlock.
            if pane is not None:
                try:
                    pane.remove_live_blocks()
                except Exception:
                    pass
            raise
        finally:
            # Idle / error are terminal-for-this-round; anything else
            # (cancelled, unexpected exit) becomes "error" so the panel
            # signals the user that something went wrong.
            if sess.phase not in ("idle", "error"):
                sess.phase = "error"
            sess.last_active_at = time.monotonic()

    async def _run_subagent_task(self, sess: SubagentSession) -> None:
        """Background driver for one subagent round.

        Wraps `_run_subagent_round` so its return value is captured on
        the session (`last_result`) instead of returned to a caller.
        Cancellation, stream errors, and unexpected crashes all become
        a `last_result` string + phase=error so await_agent has
        something concrete to return. `task` is cleared in `finally`
        so chat_agent / await_agent / the reaper can tell the round
        has ended."""
        try:
            answer = await self._run_subagent_round(sess)
        except asyncio.CancelledError:
            sess.last_result = "⛔ subagent task cancelled."
            sess.phase = "error"
            sess.last_active_at = time.monotonic()
            raise
        except Exception as e:
            sess.last_result = f"❌ subagent task crashed: {e}"
            sess.phase = "error"
            sess.last_active_at = time.monotonic()
        else:
            sess.last_result = answer
            # _run_subagent_round leaves phase as "idle" on the happy
            # path; promote to "ready" since we have an unread answer.
            if sess.phase == "idle":
                sess.phase = "ready"
        finally:
            sess.task = None

    def _spawn_subagent(self, prompt: str, system: str) -> str:
        """Create a fresh SubagentSession and schedule its first round
        as a background task. Returns immediately with the assigned
        session_id; the parent calls `await_agent` to fetch the answer.

        Each session gets its own ToolContext (independent bash job
        table) but inherits the parent's work_dir, OpenAI client, and
        model / effort settings. Token usage rolls up into the parent
        counter; the cap is MAX_LIVE_SUBAGENTS so a runaway parent
        can't fork unbounded sessions.
        """
        if not prompt.strip():
            return "Error: spawn_agent requires a non-empty 'prompt' argument."
        if len(self._live_subagents) >= MAX_LIVE_SUBAGENTS:
            return (
                f"Error: too many live subagents "
                f"({len(self._live_subagents)}/{MAX_LIVE_SUBAGENTS}). "
                "Call end_agent on an existing session first."
            )

        sub_ctx = ToolContext(work_dir=self.ctx.work_dir)
        sub_messages: list = [
            {"role": "system", "content": SYSTEM_PROMPT + sub_ctx.work_dir},
        ]
        # Project-level guidance (AGENTS.md) applies to subagents too —
        # they're working in the same repo as the parent and should
        # follow the same conventions. Re-read here rather than copying
        # from self.messages so it picks up edits made since startup.
        sub_agents_md = load_agents_md(sub_ctx.work_dir)
        if sub_agents_md:
            sub_messages.append({
                "role": "system",
                "content": f"# Project guidelines (from AGENTS.md)\n\n{sub_agents_md}",
            })
        if POST_SYSTEM_PROMPT:
            sub_messages.append({
                "role": "system",
                "content": POST_SYSTEM_PROMPT,
            })
        if system.strip():
            sub_messages.append({
                "role": "system",
                "content": f"# Subagent role / constraints\n\n{system.strip()}",
            })
        sub_messages.append({"role": "user", "content": prompt})

        # Hide every meta-tool so the subagent can't recurse.
        sub_tools = [
            t for t in TOOLS
            if t["function"]["name"] not in (
                "spawn_agent",
                "chat_agent",
                "await_agent",
                "end_agent",
                "explore_start",
                "explore_end",
                "explore_cancel",
                "checkpoint_tool",
                "checkpoint_get",
                "checkpoint_clear",
                "task_start",
                "task_check",
                "task_read",
                "task_wait",
                "task_kill",
                "task_list",
                "terminal_start",
                "terminal_send",
                "terminal_read",
                "terminal_interrupt",
                "terminal_close",
                "terminal_list",
            )
        ]

        sub_id = f"sub-{self._subagent_next_id}"
        self._subagent_next_id += 1
        preview = prompt.strip().replace("\n", " ")
        if len(preview) > 40:
            preview = preview[:40] + "…"
        now = time.monotonic()
        sess = SubagentSession(
            id=sub_id,
            prompt_preview=preview,
            started_at=now,
            last_active_at=now,
            messages=sub_messages,
            ctx=sub_ctx,
            sub_tools=sub_tools,
        )
        # Resolve the model's context window once so the sidebar +
        # await_agent return can render a ctx% gauge. None if the
        # provider doesn't know (we just skip the gauge in that case).
        try:
            sess.context_limit = self.provider.context_limit_for_model(
                self.model
            )
        except Exception:
            sess.context_limit = None
        # Construct the pane synchronously so the round task can push
        # live widgets into it via sess.pane the moment the first
        # stream chunk arrives — before _add_sub_tab finishes mounting
        # the TabPane into TabbedContent (the pane buffers via
        # _pending_mounts until on_mount flushes them).
        sess.pane = SubagentTabPane(sess)
        sess.task = asyncio.create_task(self._run_subagent_task(sess))
        self._live_subagents[sub_id] = sess
        # Top-level tab for the new session — read-only transcript;
        # the parent's input box still routes to the main conversation.
        self.call_later(self._add_sub_tab, sess)
        return (
            f"[session_id={sub_id}, status=running] subagent spawned. "
            f"Call await_agent(session_id='{sub_id}') to fetch the answer."
        )

    def _chat_subagent(self, sid: str, prompt: str) -> str:
        """Append `prompt` to an existing session and schedule the
        next round as a background task. Returns immediately; the
        parent calls `await_agent` to fetch the answer.

        Errors if the session is busy (a previous round still running)
        or if there's an unread result still waiting — the parent must
        await_agent the previous round's answer before sending another.
        """
        if not sid or not prompt.strip():
            return (
                "Error: chat_agent requires non-empty 'session_id' "
                "and 'prompt'."
            )
        sess = self._live_subagents.get(sid)
        if sess is None:
            return (
                f"Error: unknown session_id '{sid}' (already ended or "
                "idle-reaped). Use spawn_agent to start a new session."
            )
        if sess.task is not None and not sess.task.done():
            return (
                f"Error: session {sid} is still running an earlier "
                "round. Call await_agent first."
            )
        if sess.last_result is not None:
            return (
                f"Error: session {sid} has an unread result. Call "
                "await_agent first to consume it."
            )
        sess.messages.append({"role": "user", "content": prompt})
        sess.last_active_at = time.monotonic()
        sess.task = asyncio.create_task(self._run_subagent_task(sess))
        return (
            f"[session_id={sid}, status=running] chat sent. Call "
            f"await_agent(session_id='{sid}') to fetch the answer."
        )

    async def _await_subagent(
        self, sid: str, timeout: float | int | None
    ) -> str:
        """Wait for a session's pending round (up to `timeout`s) and
        return its answer, consuming `last_result`. timeout=0 polls
        without blocking. If the round is still running when the
        timeout expires, returns a 'still running' notice WITHOUT
        cancelling the task (the parent can await_agent again or
        end_agent to give up).
        """
        if not sid:
            return "Error: await_agent requires a 'session_id' argument."
        sess = self._live_subagents.get(sid)
        if sess is None:
            return (
                f"Error: unknown session_id '{sid}' (already ended or "
                "idle-reaped)."
            )
        # Resolve timeout — clamp to [0, max].
        if timeout is None:
            wait_secs: float = float(SUBAGENT_DEFAULT_AWAIT_TIMEOUT)
        else:
            try:
                wait_secs = float(timeout)
            except (TypeError, ValueError):
                wait_secs = float(SUBAGENT_DEFAULT_AWAIT_TIMEOUT)
        if wait_secs < 0:
            wait_secs = 0.0
        if wait_secs > SUBAGENT_MAX_AWAIT_TIMEOUT:
            wait_secs = float(SUBAGENT_MAX_AWAIT_TIMEOUT)

        # Wait for the task to finish if one is in flight. asyncio.wait
        # does NOT cancel pending tasks on timeout — exactly what we
        # want: the subagent keeps running, the parent can poll again.
        if sess.task is not None and not sess.task.done():
            done, _pending = await asyncio.wait(
                {sess.task}, timeout=wait_secs
            )
            if not done:
                return (
                    f"[session_id={sid}, status=running] still running "
                    f"after {wait_secs:.0f}s ({sess.phase}, turn "
                    f"{sess.turn}). Call await_agent again."
                )

        # Task is done (or there was none). Surface the pending result.
        if sess.last_result is None:
            return (
                f"Error: session {sid} has no pending result. Send "
                "chat_agent before await_agent."
            )
        result = sess.last_result
        sess.last_result = None
        if sess.phase == "ready":
            sess.phase = "idle"
        sess.last_active_at = time.monotonic()
        # Prefix the result with a context-usage hint so the parent
        # can decide whether to chat_agent the subagent into a
        # compact_self call before the next round. Skipped when the
        # provider doesn't expose a context_limit.
        if sess.context_limit and sess.last_prompt_tokens:
            pct = sess.last_prompt_tokens * 100.0 / sess.context_limit
            header = (
                f"[session_id={sid}, ctx={pct:.0f}% "
                f"({sess.last_prompt_tokens:,}/{sess.context_limit:,})]\n"
            )
            return header + result
        return result

    def _end_subagent(self, sid: str) -> str:
        """Tear down a session: cancel any in-flight round, kill its
        bash jobs, drop it from the live registry. Idempotent (returns
        an Error string if the id is unknown — easier for the model to
        spot than a silent no-op)."""
        sess = self._live_subagents.pop(sid, None)
        if sess is None:
            return (
                f"Error: unknown session_id '{sid}' (already ended or "
                "idle-reaped)."
            )
        if sess.task is not None and not sess.task.done():
            sess.task.cancel()
        kill_all_bash_jobs(sess.ctx.bash_jobs)
        kill_all_terminals(sess.ctx.terminals)
        kill_all_tasks(sess.ctx.tasks)
        self.call_later(self._remove_sub_tab, sid)
        return (
            f"Session {sid} ended (turns={sess.turn}, "
            f"in={sess.tokens_in:,}, out={sess.tokens_out:,})."
        )
