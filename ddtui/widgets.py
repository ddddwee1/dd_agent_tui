"""Textual widgets that compose the TUI.

Pure presentation layer — every class here only depends on textual /
rich, plus type / data classes from `ddtui.state`. They never reach
into `ddtui.tools` or `ddtui.app`. State is pushed in via constructor
arguments or method calls (e.g. `BgJobsBlock.render_jobs(jobs)`,
`StatusBar.render_status(counter, ...)`).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Collapsible, Static, TextArea

from .config import CTX_SAFE_LIMIT
from .state import BgJob, SubagentSession, TokenCounter, bg_job_status


# ───────── conversation bubbles ─────────

class UserBubble(Static):
    DEFAULT_CSS = """
    UserBubble {
        margin: 1 0 0 0;
        padding: 0 1;
        border-left: thick #89b4fa;
    }
    """

    def __init__(self, text: str) -> None:
        t = Text()
        t.append("you  ", style="bold blue")
        t.append(text)
        super().__init__(t)


class SteerBubble(Static):
    """A user 'steer' interjection — submitted via Alt/Cmd+Enter while a
    turn is running. Visually marked with ⚡ + golden border so it's
    obvious this isn't a normal turn-starting message; it'll be injected
    into the next LLM round as `[实时插话] <text>`."""

    DEFAULT_CSS = """
    SteerBubble {
        margin: 1 0 0 0;
        padding: 0 1;
        border-left: thick #f9e2af;
    }
    """

    def __init__(self, text: str) -> None:
        t = Text()
        t.append("⚡ steer  ", style="bold #f9e2af")
        t.append(text)
        super().__init__(t)


class AssistantMessage(Static):
    DEFAULT_CSS = """
    AssistantMessage {
        margin: 0 0 1 0;
        padding: 0 1;
        border-left: thick #a6e3a1;
    }
    """

    def __init__(self) -> None:
        self._buffer = ""
        super().__init__(self._build())

    def append_text(self, text: str) -> None:
        self._buffer += text
        self.update(self._build())

    @property
    def text(self) -> str:
        return self._buffer

    def _build(self) -> RenderableType:
        # Don't name this `_render` — that's a Textual Widget hook that
        # must return a Visual, not a Rich renderable.
        # Markdown lets the model produce tables, code fences, lists,
        # headings and have them render as such (instead of raw `| col |`
        # text).
        if not self._buffer:
            return Text("assistant  ", style="bold green")
        return Group(
            Text("assistant", style="bold green"),
            Markdown(self._buffer),
        )


class ThinkingBlock(Collapsible):
    """Streamed `reasoning_content`. Default expanded; click the header
    (or focus it and press Enter) to toggle this one. Ctrl+T from anywhere
    folds/unfolds every thinking block at once.

    Title shows the reasoning token count once the stream finishes.
    """

    DEFAULT_CSS = """
    ThinkingBlock { margin: 0 0 1 0; }
    ThinkingBlock > Contents { padding: 0 0 0 2; }
    ThinkingBlock #thinking-body { color: #585b70; }
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._body = Static("", id="thinking-body", markup=False)
        super().__init__(self._body, title="thinking · 流式中…", collapsed=False)

    def append_text(self, text: str) -> None:
        self._buffer += text
        self._body.update(self._buffer)

    def finalize(self, reasoning_tokens: int) -> None:
        if reasoning_tokens:
            self.title = f"thinking · {reasoning_tokens:,} tokens (点击展开/折叠)"
        else:
            self.title = "thinking · 完成 (点击展开/折叠)"

    @property
    def text(self) -> str:
        return self._buffer


class ToolCallBlock(Collapsible):
    """One tool call with its result. Default collapsed; click the
    header to toggle. Title shows `● <name> <short args> <status>`;
    the body holds the pretty-printed args and the (truncated) result.
    """

    DEFAULT_CSS = """
    ToolCallBlock { margin: 0 0 1 0; }
    ToolCallBlock > Contents { padding: 0 0 0 2; }
    ToolCallBlock #toolcall-body { color: $text-muted; }
    """

    def __init__(self, name: str, args: dict) -> None:
        self._tool_name = name  # not _name — Widget already uses that
        self._args = args
        self._result: str | None = None
        self._blocked = False
        # markup=False so result text containing '[' or ']' won't be parsed.
        self._body = Static(self._build_body(), id="toolcall-body", markup=False)
        super().__init__(self._body, title=self._build_title(), collapsed=True)

    def set_result(self, result: str, blocked: bool = False) -> None:
        self._result = result
        self._blocked = blocked
        self._body.update(self._build_body())
        self.title = self._build_title()

    def _short_args(self) -> str:
        try:
            preview = json.dumps(self._args, ensure_ascii=False)
        except Exception:
            preview = str(self._args)
        if len(preview) > 80:
            preview = preview[:80] + "…"
        return preview

    def _build_title(self) -> str:
        if self._result is None:
            status = "执行中…"
        elif self._blocked:
            status = "⊘ 已拦截"
        else:
            status = "✓"
        return f"● {self._tool_name}  {self._short_args()}  {status}"

    def _build_body(self) -> RenderableType:
        try:
            args_pretty = json.dumps(self._args, ensure_ascii=False, indent=2)
        except Exception:
            args_pretty = str(self._args)
        parts: list[RenderableType] = [Text(args_pretty), Text("")]

        if self._result is None:
            parts.append(Text("执行中…", style="italic dim"))
        else:
            # For edit_file / edit_lines / multi_edit the diff is already
            # stripped before set_result is called (see AgentApp), so a
            # uniform snippet path works for every tool.
            snippet = (
                self._result
                if len(self._result) <= 4000
                else self._result[:4000] + f"\n…[+{len(self._result) - 4000} chars]"
            )
            parts.append(Text(snippet))
        return Group(*parts)


class DiffBlock(Collapsible):
    """Standalone diff view shown right after a successful edit_file or
    edit_lines call. Default expanded — the change is the headline; the
    user shouldn't have to dig for it. Title format: `📝 path  +N -M`.
    """

    DEFAULT_CSS = """
    DiffBlock { margin: 0 0 1 0; }
    DiffBlock > Contents { padding: 0 0 0 2; }
    """

    def __init__(self, path: str, diff_text: str) -> None:
        self._diff_text = diff_text
        body = Static(self._render_diff(diff_text), markup=False)
        super().__init__(
            body, title=self._make_title(path, diff_text), collapsed=False
        )

    @staticmethod
    def _make_title(path: str, diff_text: str) -> str:
        plus = minus = 0
        for line in diff_text.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                plus += 1
            elif line.startswith("-") and not line.startswith("---"):
                minus += 1
        return f"📝 {path}  +{plus} -{minus}"

    @staticmethod
    def _render_diff(diff_text: str) -> Text:
        """Color a unified diff: + lines bright-white-fg on dark-green-bg,
        - lines bright-white-fg on dark-red-bg. The dark backgrounds
        (#0d3b18 / #3d0a0a) keep the bar low-key on Ubuntu's purple, while
        bright_white keeps the text crisp against them."""
        t = Text()
        for line in diff_text.splitlines(keepends=True):
            head = line.rstrip("\n")
            if head.startswith("+++") or head.startswith("---"):
                t.append(line, style="bold")
            elif head.startswith("@@"):
                t.append(line, style="bold cyan")
            elif head.startswith("+"):
                t.append(line, style="bright_white on #0d3b18")
            elif head.startswith("-"):
                t.append(line, style="bright_white on #3d0a0a")
            else:
                t.append(line)
        return t


# ───────── sidebar ─────────

class TodoBlock(Static):
    """Persistent TODO list maintained by the model via `todo_tool`.

    Only ONE instance lives in the conversation at a time — every call
    overwrites its contents in place, so the user sees a single panel
    that ticks items off as work progresses (rather than a stream of
    snapshots).
    """

    DEFAULT_CSS = """
    TodoBlock {
        margin: 1 0;
        padding: 0 1;
        border: round #cba6f7;
    }
    TodoBlock.all-done { border: round #f9e2af; }
    """

    def __init__(self) -> None:
        self._items: list[dict] = []
        super().__init__(self._build())

    def set_items(self, items: list[dict]) -> None:
        self._items = items
        self.update(self._build())

    def _build(self) -> Text:
        n = len(self._items)
        n_done = sum(1 for i in self._items if i.get("status") == "completed")
        n_prog = sum(1 for i in self._items if i.get("status") == "in_progress")
        n_pend = n - n_done - n_prog

        t = Text()
        t.append("TODO  ", style="bold #cba6f7")
        t.append(
            f"({n_prog} 进行中 · {n_done} 完成 · {n_pend} 待办)",
            style="dim",
        )
        if not self._items:
            t.append("\n  (empty)", style="dim italic")
            return t
        for item in self._items:
            status = item.get("status", "pending")
            content = item.get("content", "")
            t.append("\n")
            if status == "completed":
                t.append("  [", style="dim")
                t.append("✓", style="bold green")
                t.append("] ", style="dim")
                t.append(content, style="strike dim")
            elif status == "in_progress":
                t.append("  [", style="bold")
                t.append("▶", style="bold cyan")
                t.append("] ", style="bold")
                t.append(content, style="bold cyan")
            else:  # pending
                t.append("  [ ] ", style="dim")
                t.append(content, style="dim")
        return t


class SubagentsBlock(Static):
    """Sidebar widget showing live `SubagentSession`s.

    Mirrors `AgentApp._live_subagents` once a second. A session lives
    until the parent calls `end_agent` or the idle reaper kicks it out,
    so the panel is visible whenever at least one session exists —
    including ones currently sitting at `idle` between rounds.
    """

    DEFAULT_CSS = """
    SubagentsBlock {
        margin: 1 0;
        padding: 0 1;
        border: round #f5c2e7;
    }
    """

    # phase → (icon, style). Distinct hues so the eye can tell at a
    # glance whether the subagent is spending tokens (thinking /
    # answering), burning wallclock on a tool, has an answer waiting
    # for await_agent (ready), or is between rounds (idle).
    _PHASE_GLYPH = {
        "thinking":  ("◐", "bold #cba6f7"),  # mauve
        "answering": ("▶", "bold cyan"),
        "tool":      ("⚙", "bold #fab387"),  # peach
        "ready":     ("▣", "bold #a6e3a1"),  # green — answer waiting
        "idle":      ("○", "bold #6c7086"),  # surface2 gray
        "done":      ("✓", "bold green"),
        "error":     ("✗", "bold red"),
    }

    def render_sessions(self, sessions: list[SubagentSession]) -> None:
        t = Text()
        t.append("Subagents  ", style="bold #f5c2e7")
        n_busy = sum(
            1 for s in sessions
            if s.phase in ("thinking", "answering", "tool")
        )
        n_ready = sum(1 for s in sessions if s.phase == "ready")
        n_idle = sum(1 for s in sessions if s.phase == "idle")
        t.append(f"({n_busy} 跑 · {n_ready} 待领 · {n_idle} 闲)", style="dim")
        if not sessions:
            t.append("\n  (empty)", style="dim italic")
            self.update(t)
            return
        now = time.monotonic()
        for s in sessions:
            icon, style = self._PHASE_GLYPH.get(s.phase, ("·", "dim"))
            t.append("\n  ")
            t.append(f"{icon} ", style=style)
            t.append(f"{s.id}", style=style)
            t.append(f"  轮 {s.turn}", style="dim")
            if s.phase == "idle":
                idle_for = now - s.last_active_at
                t.append(f"  闲 {idle_for:5.1f}s", style="dim")
            else:
                elapsed = now - s.started_at
                t.append(f"  {elapsed:5.1f}s", style="dim")
            if s.last_tool and s.phase != "idle":
                t.append(f"  · {s.last_tool}", style="#fab387")
            if s.tokens_in or s.tokens_out:
                t.append(
                    f"\n    [{s.tokens_in:,}↓ / {s.tokens_out:,}↑]",
                    style="dim",
                )
            if s.prompt_preview:
                t.append(f'\n    "{s.prompt_preview}"', style="dim italic")
        self.update(t)


class SubagentTabPane(VerticalScroll):
    """Read-only transcript view for one SubagentSession.

    Lives inside a TabPane in the top-level TabbedContent. Renders
    `sess.messages` as the same widget vocabulary the main conversation
    uses (UserBubble / ThinkingBlock / AssistantMessage / ToolCallBlock)
    so the parent's "what did the subagent do?" view feels familiar.

    Stateful: tracks how many messages have been rendered so a 1 Hz
    refresh loop can `mount` only the new ones — re-rendering the
    whole transcript every tick would flicker badly. `_tool_blocks`
    maps tool_call_id → ToolCallBlock so a later `role=tool` message
    can attach its result to the block that requested it.

    No streaming: `sess.messages` only grows when a round commits an
    assistant or tool message, so the view always lags the live stream
    by one round-stage. Acceptable for a read-only "what did it say"
    panel; the right-side SubagentsBlock shows the live phase.
    """

    DEFAULT_CSS = """
    SubagentTabPane { padding: 0 1; }
    """

    def __init__(self, sess: SubagentSession) -> None:
        super().__init__()
        self.sess = sess
        self._rendered_idx = 0
        self._tool_blocks: dict[str, "ToolCallBlock"] = {}

    def refresh_from_session(self) -> None:
        """Append widgets for any messages that arrived since last call.
        Cheap when nothing is new (early-out)."""
        msgs = self.sess.messages
        if self._rendered_idx >= len(msgs):
            return
        for m in msgs[self._rendered_idx:]:
            self._render_one(m)
        self._rendered_idx = len(msgs)
        # Keep the view glued to the bottom while the subagent is
        # actively producing output — same UX as the main conversation.
        self.scroll_end(animate=False)

    def _render_one(self, m: dict) -> None:
        role = m.get("role")
        if role == "system":
            # The framework prompt + optional role spec — long, static,
            # not interesting in a transcript view.
            return
        if role == "user":
            self.mount(UserBubble(m.get("content") or ""))
            return
        if role == "assistant":
            rc = m.get("reasoning_content")
            if rc:
                tb = ThinkingBlock()
                tb.append_text(rc)
                # Historical reasoning has no live token count; pass 0
                # and let `finalize` produce a "完成" title without a
                # number. Start collapsed so the transcript opens with
                # the answer prominent, not a wall of thoughts.
                tb.finalize(0)
                tb.collapsed = True
                self.mount(tb)
            content = m.get("content") or ""
            if content:
                am = AssistantMessage()
                am.append_text(content)
                self.mount(am)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name", "?")
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {"_raw": raw_args}
                blk = ToolCallBlock(name, args)
                self.mount(blk)
                tcid = tc.get("id") or ""
                if tcid:
                    self._tool_blocks[tcid] = blk
            return
        if role == "tool":
            tcid = m.get("tool_call_id") or ""
            content = m.get("content") or ""
            blk = self._tool_blocks.get(tcid)
            if blk is not None:
                blk.set_result(content)
            else:
                # Shouldn't happen — every tool message follows an
                # assistant tool_call with the same id. Render a
                # standalone fallback rather than dropping the data.
                self.mount(Static(Text(f"[orphan tool result] {content}")))
            return


class BgJobsBlock(Static):
    """Sidebar widget showing live & recently-finished background bash
    jobs. Re-rendered on a 1-second timer driven by the App."""

    DEFAULT_CSS = """
    BgJobsBlock {
        margin: 1 0;
        padding: 0 1;
        border: round #94e2d5;
    }
    """

    def render_jobs(self, jobs: list[BgJob]) -> None:
        t = Text()
        t.append("Background  ", style="bold #94e2d5")
        n_run = sum(1 for j in jobs if j.proc.poll() is None)
        n_done = len(jobs) - n_run
        t.append(f"({n_run} 跑 · {n_done} 完)", style="dim")
        if not jobs:
            t.append("\n  (empty)", style="dim italic")
            self.update(t)
            return
        for j in jobs:
            status, rc, elapsed = bg_job_status(j)
            cmd = j.command if len(j.command) <= 26 else j.command[:23] + "…"
            t.append("\n  ")
            if status == "running":
                t.append("▶ ", style="bold cyan")
                t.append(f"{j.id}", style="bold cyan")
            elif rc == 0:
                t.append("✓ ", style="bold green")
                t.append(f"{j.id}", style="green")
            else:
                t.append("✗ ", style="bold red")
                t.append(f"{j.id}", style="red")
            t.append(f"  {elapsed:5.1f}s ", style="dim")
            t.append(cmd)
        self.update(t)


# ───────── input ─────────

class SlashPopup(Static):
    """Auto-shown command hint above the input box.

    Pure display: appears when the input starts with ``/`` and contains
    no whitespace, listing every slash command whose name still matches
    the current prefix. Vanishes the moment the user types a space (or
    away from ``/...`` form), since at that point they've moved on to
    arguments. There's no keyboard navigation — the user keeps typing
    in the input and reads the popup as a hint.
    """

    DEFAULT_CSS = """
    SlashPopup {
        height: auto;
        padding: 0 1;
        border: round #94e2d5;
        background: #300A24;
        display: none;
    }
    """

    # (display form, description). Display form may include an
    # ``<arg>`` placeholder so the user sees the expected shape at a
    # glance; the matcher only looks at the first whitespace-delimited
    # token so the placeholder doesn't break prefix matching.
    COMMANDS: tuple[tuple[str, str], ...] = (
        ("/clear", "清空对话（保留 system prompt + AGENTS.md）"),
        ("/compact", "压缩历史，保留最近两轮原文"),
        ("/save <name>", "保存到 ~/.ddtui/history/<name>.json"),
        ("/load <name>", "读回保存的对话"),
        ("/list-history", "列出已保存对话"),
        ("/model [<id>]", "查看 / 切换模型（下一轮请求生效）"),
        ("/effort [<level>]", "查看 / 切换 reasoning effort"),
        ("/rewind", "退回上一条用户消息（文本回填输入框）"),
        ("/rethink", "删除最近一轮思考（含其后内容）"),
        ("/help", "显示帮助"),
        ("/exit", "退出"),
    )

    def update_for_text(self, text: str) -> None:
        """Show/hide and filter based on current input text."""
        if not text.startswith("/") or any(c.isspace() for c in text):
            self.display = False
            return
        matches = [
            (cmd, desc) for cmd, desc in self.COMMANDS
            if cmd.split(" ", 1)[0].startswith(text)
        ]
        if not matches:
            self.display = False
            return
        # Align descriptions on a single column so the list reads as a
        # table rather than a ragged left edge.
        width = max(len(cmd) for cmd, _ in matches)
        t = Text()
        t.append("Slash 命令", style="bold #94e2d5")
        t.append("  (继续输入过滤 · Enter 发送)", style="dim")
        for cmd, desc in matches:
            t.append("\n  ")
            t.append(cmd.ljust(width), style="bold cyan")
            t.append("  ")
            t.append(desc, style="dim")
        self.update(t)
        self.display = True


class MultilineInput(TextArea):
    """Multi-line prompt box.

    User-facing key bindings:
      Enter           → submit (start a new turn / queue if busy)
      Option+Enter    → insert literal newline   (textual: alt+enter)
      Cmd+Enter       → submit as STEER          (textual: ctrl+enter,
                        via iTerm2 escape `[13;5u` for ⌘ Return)
      Ctrl+X          → forward to App.action_cancel_pending

    The textual binding names below use `alt+enter` and `ctrl+enter`
    because textual identifies modifiers by Linux/X11 conventions —
    macOS Option physically *is* alt, and Cmd+Enter is converted by
    iTerm2 (or the user's terminal) into the CSI u escape that textual
    decodes as ctrl+enter. macOS Terminal.app cannot do this conversion
    so Cmd+Enter won't reach steer there; use Option+Enter only.
    """

    BINDINGS = [
        Binding("enter", "do_submit", show=False, priority=True),
        # alt+enter ≡ Option+Enter on macOS
        Binding("alt+enter", "do_newline", show=False, priority=True),
        # ctrl+enter ≡ Cmd+Enter once iTerm2 maps ⌘ Return → [13;5u
        Binding("ctrl+enter", "do_steer", show=False, priority=True),
        # Reclaim ctrl+x from TextArea's default `cut`. macOS users
        # use ⌘+X for cut anyway, so this is a low-cost reassignment.
        Binding("ctrl+x", "app.cancel_pending", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    MultilineInput {
        height: 3;
        min-height: 3;
        max-height: 12;
        border: solid $border-blurred;
    }
    MultilineInput:focus {
        border: solid $border;
    }
    """

    # Outer height = inner rows + 2 (top + bottom border).
    _MIN_ROWS = 3
    _MAX_ROWS = 12

    # Paste protection. Modern terminals send a paste as a single
    # textual.events.Paste event (bracketed paste) and Enter never
    # fires — but older / minimal terminals replay the paste as a
    # stream of key events, ending in a literal `\n` that hits the
    # Enter binding and auto-submits the half-pasted buffer. To catch
    # that case: if two text-changed events arrive within
    # _PASTE_DETECT_GAP seconds, mark "paste in progress"; for the
    # next _PASTE_BLOCK_WINDOW seconds, demote Enter from submit to
    # insert-newline. Real human typing never trips it (ms gap is far
    # below typing cadence).
    _PASTE_DETECT_GAP = 0.02
    _PASTE_BLOCK_WINDOW = 0.2

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    class SteerSubmitted(Message):
        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def action_do_submit(self) -> None:
        # Demote Enter to "insert newline" while a paste is in flight —
        # see the _PASTE_DETECT_GAP comment above.
        if (
            time.monotonic() - getattr(self, "_last_paste_at", 0.0)
            < self._PASTE_BLOCK_WINDOW
        ):
            self.insert("\n")
            return
        self.post_message(self.Submitted(self.text))

    def action_do_newline(self) -> None:
        # Mirrors what TextArea's built-in Enter handler does, but only
        # fires on Alt+Enter so plain Enter remains "submit".
        self.insert("\n")

    def action_do_steer(self) -> None:
        self.post_message(self.SteerSubmitted(self.text))

    def _fit_height(self) -> None:
        # Border eats two rows, so outer height = line_count + 2 to keep
        # every line visible. Past _MAX_ROWS the TextArea scrolls.
        rows = max(self._MIN_ROWS, min(self._MAX_ROWS, self.document.line_count + 2))
        self.styles.height = rows

    def on_mount(self) -> None:
        self._fit_height()
        self._last_change_at = 0.0
        self._last_paste_at = 0.0

    def on_text_area_changed(self, _event: TextArea.Changed) -> None:
        self._fit_height()
        now = time.monotonic()
        last = getattr(self, "_last_change_at", 0.0)
        # Two changed events within _PASTE_DETECT_GAP almost certainly
        # come from a paste — human typing rate is ~80ms/char minimum.
        if 0 < now - last < self._PASTE_DETECT_GAP:
            self._last_paste_at = now
        self._last_change_at = now

    def _on_paste(self, event) -> None:
        # Bracketed-paste path: terminal hands us the whole paste as
        # one event, no Enter keys ever fire — but stamp the timestamp
        # anyway so any trailing newline that might leak through is
        # also caught by action_do_submit.
        self._last_paste_at = time.monotonic()


# ───────── status bar ─────────

def _interp_rgb(
    c1: tuple[int, int, int], c2: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    """Linearly interpolate between two RGB triples; t clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    return (
        int(c1[0] + (c2[0] - c1[0]) * t),
        int(c1[1] + (c2[1] - c1[1]) * t),
        int(c1[2] + (c2[2] - c1[2]) * t),
    )


def _ctx_bg_color(ratio: float) -> str:
    """Pick a status-bar background hex from a green→amber→red gradient.

    *ratio* is last-call (prompt+completion) / CTX_SAFE_LIMIT; values
    above 1.0 saturate at the dark-red end. Colors are dark / desaturated
    so the foreground text (light $text) stays legible.
    """
    GREEN = (30, 48, 36)   # 接近 catppuccin green, 调到 ~18% 亮度
    AMBER = (58, 51, 37)   # 暗琥珀
    RED = (61, 30, 38)     # 暗红
    if ratio < 0.5:
        rgb = _interp_rgb(GREEN, AMBER, ratio / 0.5)
    else:
        rgb = _interp_rgb(AMBER, RED, (ratio - 0.5) / 0.5)
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def _short_cwd(cwd: str) -> str:
    """Compress $HOME to ~ for status-bar display."""
    home = str(Path.home())
    if cwd == home:
        return "~"
    if cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):]
    return cwd


def _detect_git_branch(cwd: str) -> str | None:
    """Return branch name, ``detached@<short>``, or None if *cwd* isn't
    inside a git repo (or git is unavailable). Cheap (one subprocess);
    callers may cache."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    if name and name != "HEAD":
        return name
    # Detached HEAD: substitute the short commit hash so the user can
    # tell which detached state they're in at a glance.
    try:
        sh = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=2,
        )
        short = sh.stdout.strip()
    except Exception:
        short = ""
    return f"detached@{short}" if short else "detached"


class StatusBar(Static):
    # No background here — render_status() sets it inline based on the
    # current context-usage ratio (see _ctx_bg_color). Leaving CSS
    # without a default keeps the very first paint (before any turn
    # has run) from flashing $primary purple before the green baseline.
    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        color: $text;
        padding: 0 1;
    }
    """

    # Indeterminate scroll: a single block bounces left ↔ right across a
    # 7-cell track. 12 frames; ~80ms per frame → ~1s per round-trip.
    BAR_FRAMES = (
        "▰▱▱▱▱▱▱",
        "▱▰▱▱▱▱▱",
        "▱▱▰▱▱▱▱",
        "▱▱▱▰▱▱▱",
        "▱▱▱▱▰▱▱",
        "▱▱▱▱▱▰▱",
        "▱▱▱▱▱▱▰",
        "▱▱▱▱▱▰▱",
        "▱▱▱▱▰▱▱",
        "▱▱▱▰▱▱▱",
        "▱▱▰▱▱▱▱",
        "▱▰▱▱▱▱▱",
    )

    def __init__(self, work_dir: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._work_dir = work_dir
        self._frame = 0
        # Cached at construction; refreshed by refresh_location() after
        # each turn so an agent-initiated `git checkout` shows up.
        self._cwd_short = _short_cwd(work_dir)
        self._branch = _detect_git_branch(work_dir)

    def advance_frame(self) -> None:
        self._frame = (self._frame + 1) % len(self.BAR_FRAMES)

    def refresh_location(self) -> None:
        """Recompute cwd + branch. Cheap (one subprocess), but call from
        a background thread to avoid blocking the event loop."""
        self._cwd_short = _short_cwd(self._work_dir)
        self._branch = _detect_git_branch(self._work_dir)

    def _location(self) -> str:
        if self._branch:
            return f"{self._cwd_short} · ⎇ {self._branch}"
        return self._cwd_short

    def render_status(
        self,
        counter: TokenCounter,
        *,
        busy: bool = False,
        queued: int = 0,
        steer: int = 0,
    ) -> None:
        location = self._location()
        # Drive the background gradient off last-call tokens (what
        # matters is whether the *next* call still fits), not cumulative
        # billing. Pre-first-turn we sit at the green baseline.
        last_total = counter.last_prompt + counter.last_completion
        ratio = last_total / CTX_SAFE_LIMIT if counter.turns > 0 else 0.0
        self.styles.background = _ctx_bg_color(ratio)

        if counter.turns == 0:
            body = f"{location} · 等待第一轮…"
        else:
            total = counter.prompt_total + counter.completion_total
            thinking = (
                f" / Think {counter.reasoning_total:,}" if counter.reasoning_total else ""
            )
            # "Context" = prompt + completion of the most recent API call —
            # i.e. context-window usage, not cumulative billing. The
            # `(NN%)` suffix anchors the visual gradient to a number so
            # the user can tell at a glance how alarming the color is.
            pct = ratio * 100
            body = (
                f"{location} · Context {last_total:,} ({pct:.0f}%) "
                f"· 累计 {total:,} "
                f"(in {counter.prompt_total:,} / out {counter.completion_total:,}{thinking}) "
                f"· {counter.turns} 轮"
            )

        if queued:
            body += f" · 📋 排队 {queued}"
        if steer:
            body += f" · ⚡ steer {steer}"

        if busy:
            t = Text()
            for ch in self.BAR_FRAMES[self._frame]:
                t.append(ch, style="bold cyan" if ch == "▰" else "dim")
            t.append("  ")
            t.append(body)
            self.update(t)
        else:
            self.update(body)
