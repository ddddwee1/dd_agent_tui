"""UI-agnostic turn engine: stream → append → dispatch → repeat.

`TurnEngine` owns the LLM tool loop that used to live twice — inlined
in the parent's `_run_one_turn` and near-duplicated in the subagent's
`_run_subagent_round`. It knows nothing about Textual: hosts observe
progress through `TurnObserver` callbacks and render however they like
(main conversation widgets, a subagent pane, or nothing at all for a
future headless runner).

Division of labor:
- engine: streaming, assembling the assistant message, appending to the
  (caller-owned) message list, parsing tool arguments, confirmation
  gating, dispatching stateless tools, concurrent execution of adjacent
  parallel-safe calls, diff peeling, ok/error classification.
- host (via `meta_handler`): app-level meta tools that need live app
  state — subagent management, explore spans, compact_self — plus any
  policy the host wants to inject (e.g. blocking tools per session).
- host (via `TurnObserver`): widget mounting, autosave, turn journal,
  remote mirroring, token accounting.

Cancellation: CancelledError is never swallowed. A cancel during a
SERIAL tool call appends a "⛔ cancelled" stub result first (keeping the
history protocol-valid); a cancel during streaming or a parallel batch
propagates as-is and the host's orphan-pairing fallback repairs the
history. `on_stream_aborted` is deliberately synchronous so it can run
inside an except path without awaiting on an already-cancelled task.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

from .app_support import _merge_tool_call_delta
from .state import ToolContext
from .tools import (
    APP_DISPATCHED_TOOLS,
    CONFIRM_TOOLS,
    DIFF_STRIP_TOOLS,
    PARALLEL_SAFE_TOOLS,
    execute_tool,
)


# Single authority for "did this tool call fail" — the string-prefix
# convention previously re-implemented at four call sites.
_ERROR_PREFIXES = ("Error:", "Blocked:", "⛔")

BLOCKED_BY_CONFIRM = (
    "Tool execution blocked by user policy / confirmation dialog."
)


@dataclass
class ToolOutcome:
    """Structured result of one tool call.

    `content` is exactly what goes back to the model; `diff` is a
    UI-only unified diff peeled off edit-tool results (never sent to
    the model); `ok` is the single-point error classification.
    """

    ok: bool
    content: str
    diff: str | None = None


def outcome_from_result(name: str, result: str) -> ToolOutcome:
    """Classify a raw tool-result string and peel the UI diff."""
    diff = None
    if name in DIFF_STRIP_TOOLS:
        head, sep, tail = result.partition("\n\n")
        if sep and "@@" in tail:
            diff = tail
            result = head
    return ToolOutcome(
        ok=not result.startswith(_ERROR_PREFIXES), content=result, diff=diff
    )


class TurnObserver:
    """No-op observer base; hosts override the callbacks they need."""

    async def on_reasoning_delta(self, text: str) -> None:
        pass

    async def on_content_delta(self, text: str) -> None:
        pass

    def on_stream_aborted(self) -> None:
        """Stream died mid-flight (cancel or error) — tear down any
        half-rendered UI. Synchronous on purpose (see module docstring).
        """

    async def on_usage(self, usage) -> None:
        pass

    async def on_assistant_message(self, msg: dict) -> None:
        pass

    async def on_tool_started(self, tc_id: str, name: str, args: dict) -> None:
        pass

    async def on_tool_result(
        self, tc_id: str, name: str, args: dict, outcome: ToolOutcome
    ) -> None:
        pass

    async def on_history_appended(self, msg: dict, kind: str) -> None:
        """A message landed in the caller-owned list. kind is
        "assistant" or "tool". Hosts hang autosave / journal / remote
        mirroring here."""


# Returns the result text for an app-level meta tool, or None to send
# the call down the generic dispatch path. Receives (tc_id, name, args,
# batch_size) — batch_size is how many calls the assistant message
# carried, needed by tools that demand a solo batch (explore_*).
MetaHandler = Callable[[str, str, dict, int], Awaitable[str | None]]

# Confirmation hook: False → the call is blocked with BLOCKED_BY_CONFIRM.
ConfirmHook = Callable[[str, dict], Awaitable[bool]]


async def _always_allow(name: str, args: dict) -> bool:
    return True


class TurnEngine:
    """Drives one user→assistant→(tools→assistant)* turn.

    The caller appends the user message to `messages` first, then
    awaits `run_turn()`. The engine appends every assistant and tool
    message it produces (notifying the observer), and returns when the
    model answers without tool calls.
    """

    def __init__(
        self,
        *,
        provider,
        ctx: ToolContext,
        messages: list[dict],
        tools: list[dict],
        model: str,
        effort: str,
        observer: TurnObserver | None = None,
        tool_confirm: ConfirmHook | None = None,
        meta_handler: MetaHandler | None = None,
        never_parallel: frozenset[str] | None = None,
        before_round: Callable[[], Awaitable[None]] | None = None,
        executor: Callable[[ToolContext, str, dict], str] = execute_tool,
    ) -> None:
        self.provider = provider
        self.ctx = ctx
        self.messages = messages
        self.tools = tools
        self.model = model
        self.effort = effort
        self.observer = observer or TurnObserver()
        self.tool_confirm = tool_confirm or _always_allow
        self.meta_handler = meta_handler
        # Host-declared serial-only tools. The parallel fast path skips
        # meta_handler and tool_confirm, so any tool the host's handler
        # might intercept (e.g. subagent-blocked names) must be listed
        # here to stay on the serial path.
        self.never_parallel = never_parallel or frozenset()
        # Called before every model round — the host drains steer
        # interjections and async-task events into `messages` here.
        self.before_round = before_round
        self.executor = executor

    async def run_turn(self) -> None:
        while True:
            if self.before_round is not None:
                await self.before_round()
            msg = await self._stream_once()
            self.messages.append(msg)
            await self.observer.on_assistant_message(msg)
            await self.observer.on_history_appended(msg, "assistant")
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return
            await self._run_tool_batch(tool_calls)

    # ── streaming ──

    async def _stream_once(self) -> dict:
        content = ""
        reasoning = ""
        tool_calls: dict[int, dict] = {}
        usage = None
        try:
            async for ev in self.provider.stream(
                self.messages, self.tools, self.model, self.effort
            ):
                if ev.usage is not None:
                    usage = ev.usage
                if ev.reasoning:
                    reasoning += ev.reasoning
                    await self.observer.on_reasoning_delta(ev.reasoning)
                if ev.content:
                    content += ev.content
                    await self.observer.on_content_delta(ev.content)
                if ev.tool_call is not None:
                    _merge_tool_call_delta(tool_calls, ev.tool_call)
        except BaseException:
            # Cancel or stream error: nothing has been appended yet, so
            # the observer just drops its half-rendered widgets.
            self.observer.on_stream_aborted()
            raise
        if usage is not None:
            await self.observer.on_usage(usage)
        msg: dict = {"role": "assistant", "content": content}
        if reasoning:
            self.provider.attach_reasoning(msg, reasoning)
        if tool_calls:
            msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        return msg

    # ── tool execution ──

    async def _run_tool_batch(self, tool_calls: list[dict]) -> None:
        """Execute one assistant message's tool calls.

        Adjacent parallel-safe calls (read-only per the registry) run
        concurrently; everything else runs serially in order. Results
        are appended in the original call order either way.
        """
        self._batch_size = len(tool_calls)
        parsed: list[tuple[dict, str, dict, bool]] = []
        for tc in tool_calls:
            name = tc["function"]["name"]
            raw = tc["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
                args_ok = isinstance(args, dict)
                if not args_ok:
                    args = {"_raw": raw}
            except json.JSONDecodeError:
                args = {"_raw": raw}
                args_ok = False
            parsed.append((tc, name, args, args_ok))

        idx = 0
        while idx < len(parsed):
            tc, name, args, args_ok = parsed[idx]
            run = [parsed[idx]]
            j = idx + 1
            if args_ok and self._is_parallel(name):
                while j < len(parsed):
                    _tc, nname, _a, aok = parsed[j]
                    if aok and self._is_parallel(nname):
                        run.append(parsed[j])
                        j += 1
                    else:
                        break
            if len(run) > 1:
                await self._exec_parallel(run)
                idx = j
            else:
                await self._exec_serial(tc, name, args, args_ok)
                idx += 1

    def _is_parallel(self, name: str) -> bool:
        """Only registry-marked read-only tools may batch — and never
        one that needs the confirm hook, host handling, or that the
        host declared serial-only, because the parallel fast path
        skips those hooks entirely."""
        return (
            name in PARALLEL_SAFE_TOOLS
            and name not in CONFIRM_TOOLS
            and name not in APP_DISPATCHED_TOOLS
            and name not in self.never_parallel
        )

    async def _exec_parallel(self, run: list[tuple[dict, str, dict, bool]]) -> None:
        # Announce all, execute concurrently, then append in order.
        for tc, name, args, _ in run:
            await self.observer.on_tool_started(tc.get("id"), name, args)
        results = await asyncio.gather(
            *[
                asyncio.to_thread(self.executor, self.ctx, name, args)
                for _tc, name, args, _ in run
            ]
        )
        for (tc, name, args, _), result in zip(run, results):
            outcome = outcome_from_result(name, result)
            await self._commit(tc, name, args, outcome)

    async def _exec_serial(
        self, tc: dict, name: str, args: dict, args_ok: bool
    ) -> None:
        await self.observer.on_tool_started(tc.get("id"), name, args)

        if not args_ok:
            outcome = ToolOutcome(
                ok=False,
                content=(
                    "Error: could not parse arguments JSON: "
                    f"{args.get('_raw', '')}"
                ),
            )
            await self._commit(tc, name, args, outcome)
            return

        try:
            if self.meta_handler is not None:
                meta_result = await self.meta_handler(
                    tc.get("id"), name, args, self._batch_size
                )
                if meta_result is not None:
                    outcome = outcome_from_result(name, meta_result)
                    await self._commit(tc, name, args, outcome)
                    return

            allowed = await self.tool_confirm(name, args)
            if not allowed:
                outcome = ToolOutcome(ok=False, content=BLOCKED_BY_CONFIRM)
                await self._commit(tc, name, args, outcome)
                return

            result = await asyncio.to_thread(self.executor, self.ctx, name, args)
        except asyncio.CancelledError:
            # Keep the history protocol-valid: this tool_call gets a
            # stub result before the cancel propagates.
            outcome = ToolOutcome(ok=False, content=f"⛔ {name} cancelled")
            await self._commit(tc, name, args, outcome)
            raise
        outcome = outcome_from_result(name, result)
        await self._commit(tc, name, args, outcome)

    async def _commit(
        self, tc: dict, name: str, args: dict, outcome: ToolOutcome
    ) -> None:
        msg = {
            "role": "tool",
            "tool_call_id": tc.get("id"),
            "content": outcome.content,
        }
        self.messages.append(msg)
        await self.observer.on_tool_result(tc.get("id"), name, args, outcome)
        await self.observer.on_history_appended(msg, "tool")
