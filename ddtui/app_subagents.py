"""Persistent subagent session mixin for AgentApp.

The subagent's LLM tool loop is the shared `ddtui.engine.TurnEngine`;
this module owns the subagent-specific glue: `SubagentTurnObserver`
renders engine events into the session's tab pane and tracks phase /
token stats, and the round's meta handler blocks unavailable tools and
serves compact_self.
"""

from __future__ import annotations

import asyncio
import time

from . import explore_core
from .app_support import build_env_block, load_agents_md
from .config import (
    COMPACT_KEEP_RECENT_TURNS,
    MAX_LIVE_SUBAGENTS,
    POST_SYSTEM_PROMPT,
    SUBAGENT_READY_NOTIFY_DELAY,
    SUBAGENT_RESULT_MAX_CHARS,
    SUBAGENT_SYSTEM_PROMPT,
)
from .engine import ToolOutcome, TurnEngine, TurnObserver
from .state import (
    SubagentSession,
    ToolContext,
    async_task_status,
    kill_all_tasks,
    kill_all_terminals,
)
from .tools import SUBAGENT_BLOCKED_TOOLS, SUBAGENT_TOOL_SCHEMAS
from .tools_tasks import collect_task_events
from .widgets import SubagentTabPane


class SubagentParked(Exception):
    """Internal control-flow signal: a subagent round entered waiting."""


class SubagentTurnObserver(TurnObserver):
    """Bridges TurnEngine events into a SubagentSession: live pane
    rendering, phase transitions, and token accounting. Every pane call
    is defensive — the pane may be None (headless) or mid-teardown."""

    def __init__(self, app, sess: SubagentSession) -> None:
        self.app = app
        self.sess = sess
        self._thinking_started = False
        self._answer_started = False

    async def on_reasoning_delta(self, text: str) -> None:
        pane = self.sess.pane
        if pane is not None:
            try:
                if not self._thinking_started:
                    pane.start_thinking()
                    self._thinking_started = True
                pane.append_thinking(text)
            except Exception:
                pass

    async def on_content_delta(self, text: str) -> None:
        self.sess.phase = "answering"
        pane = self.sess.pane
        if pane is not None:
            try:
                if not self._answer_started:
                    pane.start_answer()
                    self._answer_started = True
                pane.append_answer(text)
            except Exception:
                pass

    def on_stream_aborted(self) -> None:
        pane = self.sess.pane
        if pane is not None:
            try:
                pane.remove_live_blocks()
            except Exception:
                pass
        self._thinking_started = False
        self._answer_started = False

    async def on_usage(self, usage) -> None:
        # Roll subagent usage into the parent counter so the status bar
        # reflects the latest context pressure; also accumulate
        # per-session counts for the sidebar widget.
        self.app.counter.add(usage)
        if usage is not None:
            self.sess.tokens_in += getattr(usage, "prompt_tokens", 0) or 0
            self.sess.tokens_out += (
                getattr(usage, "completion_tokens", 0) or 0
            )
            # THIS call's prompt tokens (not cumulative) so the sidebar
            # + agent_check gauge reflect how full the next request is.
            self.sess.last_prompt_tokens = (
                getattr(usage, "prompt_tokens", 0) or 0
            )
        self.app._refresh_status()

    async def on_assistant_message(self, msg: dict) -> None:
        pane = self.sess.pane
        if pane is not None:
            try:
                pane.finalize_thinking(self.app.counter.last_reasoning)
                pane.finalize_answer(keep_trace=bool(msg.get("tool_calls") or []))
                pane.mark_round_committed()
            except Exception:
                pass
        self._thinking_started = False
        self._answer_started = False

    async def on_tool_started(self, tc_id: str, name: str, args: dict) -> None:
        self.sess.phase = "tool"
        self.sess.last_tool = name
        pane = self.sess.pane
        if pane is not None:
            try:
                pane.add_tool_block(tc_id, name, args)
            except Exception:
                pass

    async def on_tool_result(
        self, tc_id: str, name: str, args: dict, outcome: ToolOutcome
    ) -> None:
        pane = self.sess.pane
        if pane is not None:
            try:
                pane.set_tool_result(
                    tc_id, outcome.content, blocked=not outcome.ok
                )
            except Exception:
                pass

    async def on_history_appended(self, msg: dict, kind: str) -> None:
        if kind == "tool" and self.sess.pane is not None:
            try:
                self.sess.pane.mark_round_committed()
            except Exception:
                pass


class AppSubagentMixin:
    async def _run_subagent_round(self, sess: SubagentSession) -> str:
        """Pump the shared TurnEngine until the subagent emits a final
        answer (assistant message with no tool_calls), and return that
        text.

        Caller is responsible for having appended the user message to
        sess.messages before calling. The engine owns streaming, tool
        dispatch, confirmation, diff peeling, and parallel read
        batching; this wrapper owns the subagent-specific bits — pane
        rendering (via SubagentTurnObserver), phase transitions, the
        blocked-tool / compact_self meta handler, and terminal phase
        bookkeeping. No internal turn cap — the idle reaper,
        MAX_LIVE_SUBAGENTS, and SUBAGENT_RESULT_MAX_CHARS are the soft
        guards.
        """

        async def before_round() -> None:
            if sess.park_requested:
                sess.park_requested = False
                sess.phase = "waiting"
                if sess.pane is not None:
                    try:
                        sess.pane.refresh_from_session()
                    except Exception:
                        pass
                raise SubagentParked()

            if sess.pending_events:
                events = sess.pending_events
                sess.pending_events = []
                for event in events:
                    sess.messages.append({"role": "user", "content": event})

            sess.turn += 1
            sess.phase = "thinking"
            sess.last_tool = None
            # Render any newly-appended user message (spawn prompt or
            # chat_agent follow-up) BEFORE live thinking mounts, so DOM
            # order in the pane stays user → thinking → answer → tools.
            if sess.pane is not None:
                try:
                    sess.pane.refresh_from_session()
                except Exception:
                    pass

        async def meta(
            tc_id: str, name: str, args: dict, batch_size: int
        ) -> str | None:
            if name in SUBAGENT_BLOCKED_TOOLS:
                # Defense in depth — schemas are filtered but models
                # sometimes try anyway. spawn_* would nest; checkpoint_*
                # has no consumer for a subagent.
                return f"Error: {name} is not available to subagents."
            if name == "task_pause":
                return self._subagent_task_pause(sess, args)
            if name in ("explore_start", "explore_end", "explore_cancel"):
                # Same core as the parent, bound to THIS session's state
                # and message list. No main-pane widget: the summary
                # lands in sess.messages and the tool result says where
                # the raw span was archived.
                try:
                    if name == "explore_start":
                        return await explore_core.explore_start(
                            sess.explore, sess.messages, args, batch_size
                        )
                    if name == "explore_cancel":
                        return await explore_core.explore_cancel(
                            sess.explore, args, batch_size
                        )
                    result, _summary = await explore_core.explore_end(
                        sess.explore,
                        sess.messages,
                        args,
                        batch_size,
                        provider=self.provider,
                        model=sess.model or self.model,
                        effort=sess.effort or self.effort,
                        session_id=self._session_id,
                        agent_id=sess.id,
                    )
                    return result
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    return f"Error: {name} failed: {type(e).__name__}: {e}"
            if name == "compact_self":
                # Subagent-only meta-tool: rewrite the session's own
                # messages with a summary. Reuses _compact_messages so
                # /compact and compact_self share the same prompt / cut
                # policy. No tool_confirm — memory-only, no side effects.
                if sess.explore.active is not None:
                    return (
                        "Error: an exploration span is open and pins "
                        "message indices. Call explore_end or "
                        "explore_cancel before compact_self."
                    )
                try:
                    new_messages, stats = await self._compact_messages(
                        sess.messages
                    )
                except ValueError as e:
                    return f"Error: {e}"
                except Exception as e:
                    return (
                        f"Error: compact_self failed: "
                        f"{type(e).__name__}: {e}"
                    )
                # Slice-assign, NOT rebind: the engine holds a reference
                # to this exact list object.
                sess.messages[:] = new_messages
                return (
                    f"已压缩：{stats['before_n']} → "
                    f"{stats['after_n']} 条消息，约 "
                    f"-{stats['saved']:,} 字符（保留最近 "
                    f"{COMPACT_KEEP_RECENT_TURNS} 轮原文）。"
                )
            return None

        engine = TurnEngine(
            provider=self.provider,
            ctx=sess.ctx,
            messages=sess.messages,
            tools=sess.sub_tools,
            model=sess.model or self.model,
            effort=sess.effort or self.effort,
            observer=SubagentTurnObserver(self, sess),
            tool_confirm=self.tool_confirm,
            meta_handler=meta,
            never_parallel=SUBAGENT_BLOCKED_TOOLS,
            before_round=before_round,
        )
        try:
            try:
                await engine.run_turn()
            except asyncio.CancelledError:
                # Pane is usually being torn down (/clear, end_agent),
                # but clean up live widgets defensively so a survivor
                # pane doesn't show a forever-streaming ThinkingBlock.
                if sess.pane is not None:
                    try:
                        sess.pane.remove_live_blocks()
                    except Exception:
                        pass
                raise
            except SubagentParked:
                raise
            except Exception as e:
                sess.phase = "error"
                if sess.pane is not None:
                    try:
                        sess.pane.remove_live_blocks()
                        sess.pane.mount_exception_notice(
                            f"⛔ stream failed: {type(e).__name__}: {e}"
                        )
                    except Exception:
                        pass
                return f"❌ subagent stream failed: {e}"
            # Final answer for THIS round. Session stays alive at
            # "idle" — the parent can chat_agent again later.
            sess.phase = "idle"
            answer = ""
            if sess.messages and sess.messages[-1].get("role") == "assistant":
                answer = sess.messages[-1].get("content") or ""
            return answer or "(subagent returned no answer)"
        finally:
            # Idle / error are terminal-for-this-round; anything else
            # (cancelled, unexpected exit) becomes "error" so the panel
            # signals the user that something went wrong.
            if sess.phase not in ("idle", "waiting", "error"):
                sess.phase = "error"
            sess.last_active_at = time.monotonic()

    async def _run_subagent_task(self, sess: SubagentSession) -> None:
        """Background driver for one subagent round.

        Wraps `_run_subagent_round` so its return value is captured on
        the session (`last_result`) instead of returned to a caller.
        Cancellation, stream errors, and unexpected crashes all become
        a `last_result` string + phase=error so agent_check has
        something concrete to return. `task` is cleared in `finally`
        so chat_agent / agent_check / the reaper can tell the round
        has ended."""
        try:
            answer = await self._run_subagent_round(sess)
        except SubagentParked:
            sess.last_result = None
            sess.phase = "waiting"
            sess.last_active_at = time.monotonic()
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

    def _subagent_task_pause(self, sess: SubagentSession, args: dict) -> str:
        """Subagent-only park primitive used after task_start.

        The tool result is committed to history first. Then the next
        before_round hook raises SubagentParked so the engine stops
        before another model request. Task events later wake this same
        session and are injected as user messages.
        """
        raw_ids = args.get("task_ids") if isinstance(args, dict) else None
        if isinstance(raw_ids, str):
            task_ids = [raw_ids]
        elif isinstance(raw_ids, list):
            task_ids = [str(x) for x in raw_ids if str(x).strip()]
        else:
            task_ids = []

        if not task_ids:
            task_ids = [
                tid for tid, task in sess.ctx.tasks.items()
                if async_task_status(task)[0] == "running"
                and task.notify_on_complete
            ]
        unknown = [tid for tid in task_ids if tid not in sess.ctx.tasks]
        if unknown:
            return "Error: unknown task_id(s): " + ", ".join(unknown)
        if not task_ids:
            return (
                "Error: no notified running tasks to wait for. Use "
                "task_start(..., notify_on_complete=true) first, or "
                "task_check/task_read if the task may already be done."
            )

        sess.waiting_refs = task_ids
        sess.waiting_reason = str(args.get("reason") or "").strip()
        sess.waiting_next_action = str(args.get("next_action") or "").strip()
        sess.park_requested = True
        sess.phase = "waiting"
        refs = ", ".join(task_ids)
        detail = (
            f" reason={sess.waiting_reason!r}"
            if sess.waiting_reason else ""
        )
        return (
            f"[Subagent waiting] parked on task event(s): {refs}.{detail} "
            "The runtime will wake this subagent with [Async task notice] "
            "or [Async task complete]."
        )

    def _spawn_subagent(
        self,
        prompt: str,
        system: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> str:
        """Create a fresh SubagentSession and schedule its first round
        as a background task. Returns immediately with the assigned
        session_id; the answer auto-delivers or can be checked with
        agent_check.

        Each session gets its own ToolContext (independent task/terminal
        tables) and the parent's work_dir and provider. Model / effort
        default to the parent's current values but can be overridden per
        spawn (same provider only) — cheap search tasks don't need the
        parent's full model. Token usage rolls up into the parent
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

        sub_model = (model or "").strip() or self.model
        sub_effort = (effort or "").strip() or self.effort
        sub_id = f"sub-{self._subagent_next_id}"
        self._subagent_next_id += 1
        # is_subagent=True lets task_start return subagent-specific
        # park/wake guidance. This ctx has its own task event stream.
        sub_ctx = ToolContext(
            work_dir=self.ctx.work_dir,
            session_id=f"{self._session_id}-{sub_id}",
            is_subagent=True,
        )
        # Subagents get their OWN framework prompt — they must know they
        # are subagents (output returns to the parent, truncated) and
        # must not be taught the meta tools they can't call. AGENTS.md
        # below still applies: same repo, same project conventions.
        sub_messages: list = [
            {
                "role": "system",
                "content": SUBAGENT_SYSTEM_PROMPT
                + "\n"
                + build_env_block(
                    sub_ctx.work_dir, self.provider.label, sub_model
                ),
            },
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

        # Subagent-visible schemas come straight from the registry:
        # no spawn_* (no recursive forking), no explore_* (the impl
        # compacts the PARENT's history), no checkpoint_* (no consumer
        # for a subagent). task_* and terminal_* ARE available (own ctx
        # tables, cleaned up by end_agent/reaper); task events wake a
        # parked subagent through task_pause.
        sub_tools = list(SUBAGENT_TOOL_SCHEMAS)

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
            model=sub_model,
            effort=sub_effort,
        )
        # Resolve the model's context window once so the sidebar +
        # agent_check return can render a ctx% gauge. None if the
        # provider doesn't know (we just skip the gauge in that case).
        try:
            sess.context_limit = self.provider.context_limit_for_model(
                sub_model
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
            "It will auto-deliver [Subagent result] when ready; use "
            f"agent_check(session_id='{sub_id}') for a non-blocking status "
            "check."
        )

    def _chat_subagent(self, sid: str, prompt: str) -> str:
        """Append `prompt` to an existing session and schedule the
        next round as a background task. Returns immediately; the
        parent receives the answer via auto-delivery or agent_check.

        Errors if the session is busy (a previous round still running)
        or if there's an unread result still waiting — the parent must
        agent_check the previous round's answer before sending another.
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
                "round. Use agent_check for status."
            )
        if sess.phase == "waiting":
            return (
                f"Error: session {sid} is waiting on task event(s) "
                f"{sess.waiting_refs}. Use agent_check for status or "
                "end_agent to cancel it."
            )
        if sess.last_result is not None:
            return (
                f"Error: session {sid} has an unread result. Call "
                "agent_check first to consume it."
            )
        sess.messages.append({"role": "user", "content": prompt})
        sess.last_active_at = time.monotonic()
        sess.task = asyncio.create_task(self._run_subagent_task(sess))
        return (
            f"[session_id={sid}, status=running] chat sent. It will "
            "auto-deliver [Subagent result] when ready; use "
            f"agent_check(session_id='{sid}') for a non-blocking status "
            "check."
        )

    async def _await_subagent(
        self, sid: str, timeout: float | int | None
    ) -> str:
        """Deprecated compatibility wrapper for old conversations.

        await_agent used to block. New subagent flow is task-like:
        spawn/chat are fire-and-forget, result auto-delivers, and
        agent_check is the non-blocking status/result inspection path.
        """
        return (
            "Deprecated: await_agent no longer blocks. Use agent_check "
            "for non-blocking status/output checks.\n"
            + self._check_subagent(sid)
        )

    def _check_subagent(self, sid: str) -> str:
        """Non-blocking status/result check for a subagent session.

        If a result is ready, this consumes it so chat_agent can send a
        follow-up. Running/waiting states are status-only.
        """
        if not sid:
            return "Error: agent_check requires a 'session_id' argument."
        sess = self._live_subagents.get(sid)
        if sess is None:
            return (
                f"Error: unknown session_id '{sid}' (already ended or "
                "idle-reaped)."
            )

        if sess.task is not None and not sess.task.done():
            return (
                f"[session_id={sid}, status=running, phase={sess.phase}, "
                f"turn={sess.turn}, last_tool={sess.last_tool}] "
                "Result is not ready yet. Keep working or wait for "
                "[Subagent result]."
            )
        if sess.phase == "waiting":
            refs = ", ".join(sess.waiting_refs) or "(unspecified)"
            reason = (
                f", reason={sess.waiting_reason!r}"
                if sess.waiting_reason else ""
            )
            return (
                f"[session_id={sid}, status=waiting, turn={sess.turn}, "
                f"tasks={refs}{reason}] Waiting for task notice/complete; "
                "the runtime will wake the subagent automatically."
            )
        if sess.last_result is None:
            return (
                f"[session_id={sid}, status={sess.phase}, turn={sess.turn}] "
                "No pending result. Send chat_agent for a follow-up or "
                "end_agent if the session is no longer needed."
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
        header = self._subagent_ctx_gauge(sess)
        prefix = f"[session_id={sid}, status=ready, turn={sess.turn}]\n"
        return prefix + (header + result if header else result)

    @staticmethod
    def _subagent_ctx_gauge(sess: SubagentSession) -> str:
        """Context-pressure header shared by agent_check returns and
        auto-delivered result notifications. Empty when the provider
        doesn't expose a context_limit."""
        if sess.context_limit and sess.last_prompt_tokens:
            pct = sess.last_prompt_tokens * 100.0 / sess.context_limit
            return (
                f"[session_id={sess.id}, ctx={pct:.0f}% "
                f"({sess.last_prompt_tokens:,}/{sess.context_limit:,})]\n"
            )
        return ""

    def _poll_subagent_task_events(self) -> int:
        """Collect task notices/completions for live subagents.

        Running subagents receive the events at the next model-round
        boundary. Parked subagents are woken immediately by scheduling
        a fresh round with the events already queued in `pending_events`.
        Returns the number of task events collected for UI notification.
        """
        n_events = 0
        for sess in self._live_subagents.values():
            if sess.phase not in ("thinking", "answering", "tool", "waiting"):
                continue
            events = collect_task_events(sess.ctx)
            if not events:
                continue
            n_events += len(events)
            sess.pending_events.extend(events)
            if (
                sess.phase == "waiting"
                and sess.task is None
                and sess.last_result is None
            ):
                sess.phase = "thinking"
                sess.task = asyncio.create_task(self._run_subagent_task(sess))
        return n_events

    def _collect_ready_subagent_events(self) -> list[str]:
        """Package idle-ready subagent results as notification events,
        consuming them (ready → idle) — task-completion-style delivery.

        Called by the 1 Hz notification poller. Results younger than
        SUBAGENT_READY_NOTIFY_DELAY are skipped so an in-flight
        agent_check can consume first; only results the parent left
        unread get pushed. The session stays alive for chat_agent
        follow-ups.
        """
        events: list[str] = []
        now = time.monotonic()
        for sess in self._live_subagents.values():
            if sess.phase != "ready" or sess.last_result is None:
                continue
            if now - sess.last_active_at < SUBAGENT_READY_NOTIFY_DELAY:
                continue
            result = sess.last_result
            sess.last_result = None
            sess.phase = "idle"
            sess.last_active_at = now
            if len(result) > SUBAGENT_RESULT_MAX_CHARS:
                result = (
                    result[:SUBAGENT_RESULT_MAX_CHARS]
                    + f"\n…[+{len(result) - SUBAGENT_RESULT_MAX_CHARS} chars truncated]"
                )
            gauge = self._subagent_ctx_gauge(sess)
            events.append(
                f"[Subagent result] session_id={sess.id} (turn "
                f"{sess.turn}) finished; its answer is delivered below — "
                "do NOT call agent_check for it (already consumed). The "
                "session stays alive for chat_agent follow-ups; end_agent "
                "it when no longer needed.\n"
                f"{gauge}{result}"
            )
        return events

    def _end_subagent(self, sid: str) -> str:
        """Tear down a session: cancel any in-flight round, kill its
        tasks/terminals, drop it from the live registry. Idempotent (returns
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
        kill_all_terminals(sess.ctx.terminals)
        kill_all_tasks(sess.ctx.tasks)
        self.call_later(self._remove_sub_tab, sid)
        return (
            f"Session {sid} ended (turns={sess.turn}, "
            f"in={sess.tokens_in:,}, out={sess.tokens_out:,})."
        )
