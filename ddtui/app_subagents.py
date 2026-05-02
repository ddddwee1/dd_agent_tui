"""Persistent subagent session mixin for AgentApp."""

from __future__ import annotations

import asyncio
import json
import time

from .app_support import _merge_tool_call_delta, load_agents_md
from .config import (
    MAX_LIVE_SUBAGENTS,
    SUBAGENT_DEFAULT_AWAIT_TIMEOUT,
    SUBAGENT_MAX_AWAIT_TIMEOUT,
    SUBAGENT_MAX_TURNS,
    SYSTEM_PROMPT,
)
from .state import SubagentSession, ToolContext, kill_all_bash_jobs
from .tools import TOOLS, execute_tool


class AppSubagentMixin:
    async def _run_subagent_round(self, sess: SubagentSession) -> str:
        """Pump the LLM tool-loop until the subagent emits a final
        answer (assistant message with no tool_calls), and return that
        text.

        Caller is responsible for having appended the user message to
        sess.messages before calling. SUBAGENT_MAX_TURNS bounds the
        inner loop *per round* — a wedged or looping subagent can't
        burn unbounded tokens before control returns to the parent.

        Phase transitions: thinking → (answering | tool)* → idle on
        success, error on stream failure or turn-cap. last_active_at
        is bumped in `finally` so the idle reaper sees the right time
        even on the cancelled / errored paths.

        `reasoning_content` is preserved on every assistant message —
        DeepSeek thinking-mode 400s if it's missing on the next call.
        """
        last_content = ""
        base_turn = sess.turn
        try:
            for inner in range(SUBAGENT_MAX_TURNS):
                sess.turn = base_turn + inner + 1
                sess.phase = "thinking"
                sess.last_tool = None
                content = ""
                reasoning = ""
                tool_calls: dict[int, dict] = {}
                final_usage = None
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
                        if event.content:
                            content += event.content
                            sess.phase = "answering"
                        if event.tool_call is not None:
                            _merge_tool_call_delta(tool_calls, event.tool_call)
                            sess.phase = "answering"
                except Exception as e:
                    sess.phase = "error"
                    return f"❌ subagent stream failed: {e}"

                # Roll subagent usage into parent counter so the status
                # bar reflects combined spend; also accumulate per-
                # session counts for the sidebar widget.
                self.counter.add(final_usage)
                if final_usage is not None:
                    sess.tokens_in += getattr(final_usage, "prompt_tokens", 0) or 0
                    sess.tokens_out += getattr(final_usage, "completion_tokens", 0) or 0
                self._refresh_status()

                last_content = content
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
                        result = f"Error: bad JSON args: {raw}"
                    else:
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
                        else:
                            allowed = await self.tool_confirm(name, args)
                            if allowed:
                                result = await asyncio.to_thread(
                                    execute_tool, sess.ctx, name, args
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
                                result = "Tool execution blocked by user policy / confirmation dialog."
                    sess.messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

            sess.phase = "error"
            return (
                f"⚠ subagent reached the {SUBAGENT_MAX_TURNS}-turn "
                f"per-round cap without a final answer. Last partial "
                f"assistant text:\n\n{last_content}"
            )
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
                "spawn_agent", "chat_agent", "await_agent", "end_agent"
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
        self.call_later(self._remove_sub_tab, sid)
        return (
            f"Session {sid} ended (turns={sess.turn}, "
            f"in={sess.tokens_in:,}, out={sess.tokens_out:,})."
        )
