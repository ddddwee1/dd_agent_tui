"""Conversation history, persistence, replay, and help mixin."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from rich.markdown import Markdown
from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from .app_support import _render_history_for_summary
from .config import (
    AUTO_COMPACT_THRESHOLD,
    COMPACT_KEEP_RECENT_TURNS,
    HISTORY_DIR,
    MAX_LIVE_SUBAGENTS,
    SUBAGENT_IDLE_TIMEOUT_SEC,
    SUBAGENT_RESULT_MAX_CHARS,
)
from .runtime_state import (
    atomic_write_json,
    clear_path,
    list_turn_journals,
    new_session_id,
    read_json,
    turn_journal_path,
)
from .state import TokenCounter
from .tools import DIFF_STRIP_TOOLS
from .tools_tasks import recover_tasks_for_session
from .tools_checkpoint import tool_checkpoint_clear, tool_checkpoint_tool
from .widgets import (
    AssistantMessage,
    DiffBlock,
    ExploreSummaryBlock,
    MultilineInput,
    ResumeConversationScreen,
    SteerBubble,
    ThinkingBlock,
    TodoBlock,
    TraceBlock,
    ToolRunBlock,
    UserBubble,
    _sanitize_table_pipes,
)


class AppHistoryMixin:
    def _history_payload(self) -> dict:
        return {
            "version": 1,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "session_id": self._session_id,
            "cwd": self.ctx.work_dir,
            "model": self.model,
            "checkpoint": self.ctx.checkpoint,
            "active_explore": self._explore_payload(),
            # list(): HistoryStore is deliberately not JSON-serializable.
            "messages": list(self.messages),
        }

    def _turn_journal_path(self) -> Path:
        return turn_journal_path(self._session_id)

    def _write_turn_journal(self, **updates) -> None:
        existing = read_json(self._turn_journal_path()) or {}
        now = datetime.now().isoformat(timespec="seconds")
        payload = {
            **existing,
            **updates,
            "version": 1,
            "session_id": self._session_id,
            "updated_at": now,
        }
        payload.setdefault("created_at", now)
        if self._autosave_path is not None:
            payload["autosave_path"] = str(self._autosave_path)
            payload["autosave_name"] = self._autosave_path.stem
        atomic_write_json(self._turn_journal_path(), payload)

    def _clear_turn_journal(self) -> None:
        clear_path(self._turn_journal_path())

    def _recover_messages_from_journal(
        self, msgs: list[dict], session_id: str
    ) -> tuple[list[dict], str | None, str | None, bool]:
        journal = read_json(turn_journal_path(session_id))
        if not journal:
            return msgs, None, None, False
        phase = str(journal.get("phase") or "unknown")
        pending = str(journal.get("pending_user_text") or "")
        before_raw = journal.get("message_len_before_turn")
        try:
            before = int(before_raw)
        except Exception:
            before = None
        tool_started = bool(journal.get("tool_started")) or phase in (
            "tool",
            "after_tool",
        )
        modified = False
        input_text: str | None = None
        if not tool_started and before is not None and 0 <= before <= len(msgs):
            msgs = msgs[:before]
            input_text = pending or None
            notice = (
                "检测到上次会话在模型流式输出期间断开；已回滚到上一条稳定消息，"
                "并把当时的用户输入放回输入框。"
            )
            modified = True
        else:
            notice = (
                "检测到上次会话在工具执行后/执行中断开；不会自动重跑工具。"
                "已尽量保持历史协议合法，请检查状态后继续。"
            )
        clear_path(turn_journal_path(session_id))
        return msgs, notice, input_text, modified

    def _normalize_history_name(self, raw_name: str) -> tuple[str | None, str | None]:
        name = raw_name.strip()
        if name.endswith(".json"):
            name = name[:-5]
        name = name.strip()
        if not name:
            return None, "文件名不能为空。"
        # Cheap sanity check before letting Path see the string. This
        # rejects most accidental path-injection patterns with a clearer
        # error than the resolve()/relative_to guard below.
        if "/" in name or "\\" in name or ".." in name:
            return None, f"文件名 {raw_name!r} 不能含 / \\ 或 .."
        return name, None

    def _history_path_for_name(
        self, raw_name: str
    ) -> tuple[Path | None, str | None]:
        name, error = self._normalize_history_name(raw_name)
        if error is not None:
            return None, error
        assert name is not None
        target = HISTORY_DIR / f"{name}.json"
        resolved = target.resolve()
        try:
            resolved.relative_to(HISTORY_DIR.resolve())
        except ValueError:
            return None, f"解析后路径逃出 history 目录：{resolved}"
        return target, None

    def _allocate_autosave_path(self) -> Path:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"session-{stamp}"
        for i in range(100):
            suffix = "" if i == 0 else f"-{i:02d}"
            candidate = HISTORY_DIR / f"{base}{suffix}.json"
            if not candidate.exists():
                return candidate
        return HISTORY_DIR / f"{base}-{datetime.now().microsecond:06d}.json"

    def _ensure_autosave_path(self) -> Path:
        path = self._autosave_path
        if path is None:
            path = self._allocate_autosave_path()
            self._autosave_path = path
        return path

    def _write_conversation_snapshot(self, target: Path) -> int:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        payload = self._history_payload()
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        return target.stat().st_size

    async def _autosave_conversation(self) -> None:
        """Persist the current conversation without adding UI noise."""
        try:
            target = self._ensure_autosave_path()
            self._write_conversation_snapshot(target)
        except Exception as e:
            self.notify(f"自动保存失败：{e}", severity="error", timeout=4)

    async def _show_disconnect_recovery_notice(self) -> None:
        journals = list_turn_journals()
        if not journals:
            return
        journal = journals[0]
        name = journal.get("autosave_name")
        path = journal.get("autosave_path")
        phase = journal.get("phase") or "unknown"
        if name:
            hint = f"可用 /resume {name} 恢复（断在 {phase} 阶段）。"
        elif path:
            hint = f"发现未完成会话：{path}（断在 {phase} 阶段）。"
        else:
            hint = f"发现未完成会话（断在 {phase} 阶段）。"
        await self._mount_widget(Static(Text(
            f"🔌 检测到上次可能异常断线。{hint}",
            style="bold #e6db74",
        )))

    def _history_entries(self) -> list[dict]:
        if not HISTORY_DIR.exists():
            return []
        entries: list[dict] = []
        for path in HISTORY_DIR.glob("*.json"):
            try:
                stat = path.stat()
            except Exception:
                continue
            edited_at = datetime.fromtimestamp(stat.st_mtime)
            entries.append(
                {
                    "name": path.stem,
                    "path": path,
                    "size": stat.st_size,
                    "edited_at": edited_at,
                    "edited_at_label": edited_at.strftime("%Y-%m-%d %H:%M"),
                    "size_label": f"{stat.st_size:,} 字节",
                }
            )
        entries.sort(key=lambda e: e["edited_at"], reverse=True)
        return entries

    async def _compact_messages(
        self, messages: list[dict]
    ) -> tuple[list[dict], dict]:
        """Run /compact-style summarization on a list of messages and
        return (new_messages, stats).

        Leading system messages are kept verbatim; the last
        COMPACT_KEEP_RECENT_TURNS user→assistant turns are kept verbatim;
        everything in between is replaced by one summary system message
        produced by a single `provider.complete_text` call.

        Raises ValueError if there's nothing worth compacting (caller
        should leave the source untouched and tell the user). Raises
        any other exception from the summarizer (likewise — caller
        leaves the source untouched).

        Reused by the main /compact slash command and by the
        subagent-only `compact_self` tool, so it must not touch UI or
        `self.messages` directly.
        """
        prefix_end = 0
        for i, m in enumerate(messages):
            if m.get("role") == "system":
                prefix_end = i + 1
            else:
                break
        user_idxs = [
            i for i, m in enumerate(messages)
            if m.get("role") == "user"
        ]
        if len(user_idxs) <= COMPACT_KEEP_RECENT_TURNS:
            raise ValueError(
                f"对话还不够长，无需压缩（user 消息数 ≤ "
                f"{COMPACT_KEEP_RECENT_TURNS}）。"
            )
        cut_at = user_idxs[-COMPACT_KEEP_RECENT_TURNS]
        to_compact = messages[prefix_end:cut_at]
        keep_recent = messages[cut_at:]
        if not to_compact:
            raise ValueError("没有可压缩的历史段。")

        rendered = _render_history_for_summary(to_compact)
        summary = await self.provider.complete_text(
            [
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
                        "## 阅读过的文档及其路径\n"
                        "## 改动过的文件 / 关键决定\n"
                        "## 悬而未决的问题或下一步\n\n"
                        "要求：保留具体函数名、文件路径、行号等可定位的事实；"
                        "不要复述完整代码、完整命令输出；"
                        "不要遗漏待办事项。\n\n"
                        f"---\n{rendered}\n---"
                    ),
                },
            ],
            self.model,
            self.effort,
        )
        if not summary:
            raise RuntimeError("摘要返回为空")

        before_n = len(messages)
        before_chars = sum(
            len(m.get("content") or "") for m in messages
        )
        new_messages = (
            messages[:prefix_end]
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
        after_n = len(new_messages)
        after_chars = sum(
            len(m.get("content") or "") for m in new_messages
        )
        stats = {
            "before_n": before_n,
            "after_n": after_n,
            "before_chars": before_chars,
            "after_chars": after_chars,
            "saved": max(0, before_chars - after_chars),
        }
        return new_messages, stats

    async def _compact_worker(self) -> None:
        """Replace the early portion of message history with a single
        summary message, leaving COMPACT_KEEP_RECENT_TURNS user→assistant
        turns intact at the tail. System messages are preserved.

        On error, the original history is left untouched and a red
        message is mounted asking the user to retry.
        """
        self._set_busy(True)
        try:
            # Cheap pre-check so we don't flash "compacting…" when
            # there's nothing to do — the helper would raise
            # ValueError but the spinner UX would be misleading.
            user_count = sum(
                1 for m in self.messages if m.get("role") == "user"
            )
            if self._active_explore is not None:
                active_id = self._active_explore.get("id", "?")
                await self._mount_widget(
                    Static(Text(
                        f"探索 {active_id} 仍在进行中；先让 agent 调用 "
                        "explore_end 或 explore_cancel，再 /compact。",
                        style="dim",
                    ))
                )
                return
            if user_count <= COMPACT_KEEP_RECENT_TURNS:
                await self._mount_widget(
                    Static(Text(
                        f"对话还不够长，无需压缩（user 消息数 ≤ "
                        f"{COMPACT_KEEP_RECENT_TURNS}）。",
                        style="dim",
                    ))
                )
                return

            await self._mount_widget(
                Static(Text("📦 正在压缩历史…", style="bold #66d9ef"))
            )

            try:
                new_messages, stats = await self._compact_messages(
                    self.messages
                )
            except ValueError as e:
                await self._mount_widget(
                    Static(Text(str(e), style="dim"))
                )
                return
            except Exception as e:
                await self._mount_widget(
                    Static(Text(
                        f"❌ 压缩失败：{e}\n请稍后再试 /compact。",
                        style="bold #f92672",
                    ))
                )
                return

            self.messages = new_messages
            await self._autosave_conversation()
            await self._mount_widget(
                Static(Text(
                    f"📦 已压缩：{stats['before_n']} → {stats['after_n']} 条消息，"
                    f"约 -{stats['saved']:,} 字符（保留最近 "
                    f"{COMPACT_KEEP_RECENT_TURNS} 轮原文）",
                    style="bold #66d9ef",
                ))
            )
        finally:
            self._set_busy(False)
            self._refresh_status()

    async def _auto_compact_if_needed(self) -> None:
        """Opportunistic compaction at quiet turn boundaries.

        Called by `_agent_turn` right before its normal return — the
        worker still holds busy, so user input queues instead of racing
        the history swap. `counter.last_prompt` is the previous
        request's measured prompt size; after a compaction the next
        turn's request re-measures against the shrunken history, so
        pressure drops well below the threshold and this cannot
        re-trigger in a loop. Failures are cosmetic: the original
        history is left untouched and the conversation continues.
        """
        if AUTO_COMPACT_THRESHOLD <= 0:
            return
        limit = self._context_limit()
        if not limit or not self.counter.last_prompt:
            return
        pressure = self.counter.last_prompt / limit
        if pressure < AUTO_COMPACT_THRESHOLD:
            return
        if self._active_explore is not None:
            # An open explore span pins message indices — same reason
            # /compact refuses; try again after explore_end/cancel.
            return
        user_count = sum(1 for m in self.messages if m.get("role") == "user")
        if user_count <= COMPACT_KEEP_RECENT_TURNS:
            return

        await self._mount_widget(
            Static(Text(
                f"📦 上下文压力 {pressure:.0%} ≥ "
                f"{AUTO_COMPACT_THRESHOLD:.0%}，自动压缩历史…",
                style="bold #66d9ef",
            ))
        )
        try:
            new_messages, stats = await self._compact_messages(self.messages)
        except Exception as e:
            await self._mount_widget(
                Static(Text(
                    f"自动压缩失败（对话不受影响，可稍后手动 /compact）：{e}",
                    style="dim",
                ))
            )
            return
        # In-place: same rule as explore_end/compact_self — never rebind
        # self.messages anywhere a TurnEngine might share the list. The
        # current call site sits after the engine loop exits, but slice
        # assignment keeps this safe if that ever changes.
        self.messages[:] = new_messages
        await self._autosave_conversation()
        await self._mount_widget(
            Static(Text(
                f"📦 已自动压缩：{stats['before_n']} → "
                f"{stats['after_n']} 条消息，约 -{stats['saved']:,} 字符"
                f"（保留最近 {COMPACT_KEEP_RECENT_TURNS} 轮原文；"
                "阈值可用 DDTUI_AUTO_COMPACT_THRESHOLD 调整，0 关闭）",
                style="bold #66d9ef",
            ))
        )

    async def _save_conversation(self, raw_name: str) -> None:
        """Dump self.messages (plus a small metadata header) to
        HISTORY_DIR / <name>.json. Overwrites silently — the user
        already provided a name, second-guessing them would be annoying."""
        target, error = self._history_path_for_name(raw_name)
        if error is not None or target is None:
            await self._mount_widget(Static(Text(
                f"错误：{error}", style="bold #f92672"
            )))
            return
        try:
            existed = target.exists()
            size = self._write_conversation_snapshot(target)
        except Exception as e:
            await self._mount_widget(Static(Text(
                f"❌ 保存失败:{e}", style="bold #f92672"
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
            style="bold #a6e22e",
        )))

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
            entries = self._history_entries()
        except Exception as e:
            await self._mount_widget(Static(Text(
                f"列出 {HISTORY_DIR} 失败：{e}", style="bold #f92672"
            )))
            return
        if not entries:
            await self._mount_widget(Static(Text(
                f"{HISTORY_DIR} 还没保存过对话", style="dim"
            )))
            return
        t = Text()
        t.append(f"📂 {HISTORY_DIR}（{len(entries)} 个对话）\n", style="bold")
        for entry in entries:
            t.append(f"  {entry['name']}", style="bold #a6e22e")
            t.append(
                f"  ({entry['size_label']}, 最后编辑 {entry['edited_at_label']})\n",
                style="dim",
            )
        t.append("用 /resume <name> 读回；/resume 可打开选择窗口。", style="dim italic")
        await self._mount_widget(Static(t))

    async def _resume_conversation_picker(self) -> None:
        entries = self._history_entries()

        def on_close(name: str | None) -> None:
            if not name:
                return
            self.run_worker(self._load_conversation(name))

        self.push_screen(ResumeConversationScreen(entries), on_close)

    async def _load_conversation(self, raw_name: str) -> None:
        """Replace `self.messages` with a saved JSON and replay every
        bubble / tool-call / diff into the conversation view so the
        history "looks lived in" rather than appearing as a single
        opaque blob."""
        target, error = self._history_path_for_name(raw_name)
        if error is not None or target is None:
            await self._mount_widget(Static(Text(
                f"错误：{error}", style="bold #f92672"
            )))
            return
        if not target.exists():
            await self._mount_widget(Static(Text(
                f"错误：找不到 {target}\n用 /list-history 看可用列表。",
                style="bold #f92672",
            )))
            return
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except Exception as e:
            await self._mount_widget(Static(Text(
                f"❌ 读取失败：{e}", style="bold #f92672"
            )))
            return
        msgs = payload.get("messages")
        if not isinstance(msgs, list) or not all(
            isinstance(m, dict) and "role" in m for m in msgs
        ):
            await self._mount_widget(Static(Text(
                "❌ 文件格式不识别（缺少合法 messages 数组）",
                style="bold #f92672",
            )))
            return
        session_id = str(payload.get("session_id") or "") or new_session_id()
        msgs, recovery_notice, recovery_input, recovered_modified = (
            self._recover_messages_from_journal(msgs, session_id)
        )

        # Wipe the current view + buffers; then drop in the loaded
        # messages and replay them as widgets. Managed tasks are NOT
        # killed — they belong to the wall-clock-current runtime, not
        # the conversation that just got swapped in.
        self._clear_turn_journal()
        self._session_id = session_id
        self.ctx.session_id = session_id
        self.ctx.tasks.clear()
        self.ctx.task_next_id = 1
        recover_tasks_for_session(self.ctx, session_id)
        self.messages = msgs
        self._drop_queue()
        self._drop_steer()
        view = self.query_one("#conversation", VerticalScroll)
        for child in list(view.children):
            child.remove()
        # /load wipes the view, so any marker we had on the prior
        # conversation is gone — drop the bookkeeping or the next fold
        # would try to extend a stale, removed marker.
        self._collapsed_widgets.clear()
        self._history_marker = None
        try:
            tray = self.query_one("#pending", Vertical)
            for child in list(tray.children):
                child.remove()
        except Exception:
            pass
        self._cancel_dismiss()
        self._restore_explore_payload(payload.get("active_explore"))
        if self._todo_block is not None and self._todo_block.is_mounted:
            self._todo_block.remove()
        self._todo_block = None
        self.ctx.checkpoint = None
        if (
            self._checkpoint_block is not None
            and self._checkpoint_block.is_mounted
        ):
            self._checkpoint_block.remove()
        self._checkpoint_block = None

        # If the previous process died during a tool call, complete any
        # orphan assistant tool_calls with an explicit unknown-result stub
        # so the next model request is protocol-valid and does not rerun
        # side-effecting tools by accident.
        if recovery_notice and not recovered_modified:
            self._pair_orphan_tool_calls(
                "⛔ Tool result unknown: previous TUI disconnected during recovery."
            )
            recovered_modified = True

        await self._replay_messages_to_view()
        if "checkpoint" in payload:
            snapshot = payload.get("checkpoint")
            self.ctx.checkpoint = snapshot if isinstance(snapshot, dict) else None
            await self._sync_checkpoint_block()
        self._autosave_path = target
        if recovery_input:
            try:
                self.query_one("#user-input", MultilineInput).text = recovery_input
            except Exception:
                pass
        if recovery_notice:
            await self._mount_widget(Static(Text(
                f"🔌 {recovery_notice}", style="bold #e6db74"
            )))
        if recovered_modified:
            await self._autosave_conversation()

        # Reset token counter — saved file has no original usage info,
        # and pretending the previous turns "cost zero" would lie about
        # the live context window.
        self.counter = TokenCounter()
        self._refresh_status()
        if hasattr(self, "_remote_on_session_reset"):
            self._remote_on_session_reset()

        saved_at = payload.get("saved_at", "?")
        saved_model = payload.get("model", "?")
        diff_note = ""
        if saved_model and saved_model != self.model:
            diff_note = f"（保存时 model={saved_model}，当前 model={self.model}）"
        await self._mount_widget(Static(Text(
            f"📂 已加载 {len(msgs)} 条消息（saved={saved_at}）{diff_note}\n"
            "Token 计数已重置；从此处续聊即可。",
            style="bold #a6e22e",
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

        trace: TraceBlock | None = None
        for m in self.messages:
            role = m.get("role")
            if role == "system":
                if m.get("ddtui_kind") == "explore_summary":
                    await self._mount_widget(ExploreSummaryBlock(m))
                continue
            if role == "tool":
                continue
            content = m.get("content") or ""
            if role == "user":
                trace = None
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
            has_tool_calls = bool(m.get("tool_calls") or [])
            if (rc or has_tool_calls) and trace is None:
                trace = TraceBlock()
                await self._mount_widget(trace)
            if rc:
                tb = ThinkingBlock()
                tb.append_text(rc)
                tb.finalize(0)
                # ThinkingBlock is collapsed by default, so a freshly
                # loaded conversation won't bury the user under walls of
                # thought from earlier turns.
                await trace.mount_trace_widget(tb)
            if content:
                am = AssistantMessage()
                am.append_text(content)
                await self._mount_widget(am)

            tool_run: ToolRunBlock | None = None
            diff_blocks: list[DiffBlock] = []
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

                if name == "checkpoint_tool":
                    if isinstance(args, dict) and "_raw" not in args:
                        try:
                            result = tool_checkpoint_tool(self.ctx, **args)
                        except TypeError:
                            result = "Error: bad checkpoint_tool arguments"
                        if not result.startswith("Error:"):
                            await self._sync_checkpoint_block()
                    continue

                if name == "checkpoint_clear":
                    if isinstance(args, dict) and "_raw" not in args:
                        try:
                            result = tool_checkpoint_clear(self.ctx, **args)
                        except TypeError:
                            result = "Error: bad checkpoint_clear arguments"
                        if not result.startswith("Error:"):
                            await self._sync_checkpoint_block()
                    continue

                if tool_run is None:
                    tool_run = ToolRunBlock()
                    if trace is None:
                        trace = TraceBlock()
                        await self._mount_widget(trace)
                    await trace.mount_trace_widget(tool_run)
                tcid = tc.get("id") or ""
                tool_run.add_call(tcid, name, args)
                if trace is not None:
                    trace.refresh_summary()
                result = tool_results.get(tcid)
                if result is None:
                    continue
                diff_text = None
                if name in DIFF_STRIP_TOOLS:
                    head, sep, diff = result.partition("\n\n")
                    if sep and "@@" in diff:
                        diff_text = diff
                        result = head
                tool_run.set_result(tcid, result)
                if trace is not None:
                    trace.refresh_summary()
                if diff_text:
                    diff_blocks.append(DiffBlock(args.get("path", "?"), diff_text))
            for diff_block in diff_blocks:
                await self._mount_widget(diff_block)
            if not has_tool_calls:
                trace = None

    async def _show_help(self) -> None:
        md = (
            "### Slash 命令\n"
            "- `/clear` 清空对话（保留 system prompt + AGENTS.md）\n"
            "- `/compact` 压缩历史，保留最近两轮原文\n"
            "- `/save <name>` 保存到 `~/.ddtui/history/<name>.json`\n"
            "- `/load <name>` 读回保存的对话\n"
            "- `/resume [name]` 恢复对话；不带 name 时打开选择窗口\n"
            "- 断线后启动会提示可恢复会话；用 `/resume <name>` 接回\n"
            "- `/remote [status|on [url]|off|snapshot]` 管理 VPS 远控连接\n"
            "- `/list-history` 列出已保存对话\n"
            "- `/provider [deepseek|codex]` 查看 / 切换 provider\n"
            "- `/model [<id>]` 查看 / 切换模型（下一轮请求生效）\n"
            "- `/effort [<level>]` 查看 / 切换 reasoning effort\n"
            "- `/rewind` 退回上一条用户消息（文本回填输入框）\n"
            "- `/rethink` 删除最近一轮助手回复，让模型重新思考\n"
            "- `/help` 显示这个帮助\n"
            "- `/exit` `/quit` 退出\n\n"
            "### 快捷键\n"
            "- `Enter` 发送当前输入\n"
            "- `Option+Enter` 插入换行\n"
            "- `Cmd+Enter` 实时插话 steer（需 iTerm2 把 ⌘Return 转义为 CSI u；"
            "macOS 自带 Terminal.app 不支持，先 Option+Enter 换行后再 Enter 也可）\n"
            "- `ESC × 2` 中断当前任务\n"
            "- `Ctrl+X` 取消所有队列 / steer\n"
            "- 鼠标点击输入框上方的排队/steer 气泡 → 编辑或删除该条"
            "（Ctrl+S 保存 · Ctrl+D 删除 · Esc 取消）\n"
            "- `Ctrl+T` 展开 / 折叠所有思考块\n"
            "- `Ctrl+L` 清空对话\n"
            "- `Ctrl+C` 退出\n\n"
            "### 工具(模型可调用)\n"
            "`bash` "
            "`terminal_start/send/read/interrupt/close/list` "
            "`task_start/check/read/kill/list` "
            "`explore_start/end/cancel` "
            "`checkpoint_tool/get/clear` "
            "`project_note_add/search/list/read/update/delete` "
            "`read_file` `write_file` `edit_file` `edit_lines` `multi_edit` "
            "`list_files` `glob_files` `search_content` `web_fetch` `web_search` "
            "`todo_tool` `spawn_agent` `chat_agent` `agent_check` `end_agent`\n"
            "\n"
            f"子 agent（异步并发）：`spawn_agent` / `chat_agent` "
            f"立即返回 `session_id`，子 agent 在后台跑；结果就绪会自动投递，"
            f"也可用 `agent_check(session_id)` 做非阻塞状态/结果检查；"
            f"`end_agent` 释放。并发用法：一次发多个 `spawn_agent` → "
            f"自己继续做别的事 → 等 `[Subagent result]` 或按需 "
            f"`agent_check`。\n"
            f"\n"
            f"上限：返回截断 {SUBAGENT_RESULT_MAX_CHARS:,} 字符；同时存活会话 "
            f"上限 {MAX_LIVE_SUBAGENTS} 个；闲置 {SUBAGENT_IDLE_TIMEOUT_SEC // 60} "
            f"分钟自动回收。子 agent 不能嵌套子 agent；token 计入主对话。\n"
        )
        await self._mount_widget(Static(Markdown(_sanitize_table_pipes(md))))

    async def _rewind_last_user(self) -> None:
        """Drop the most recent user message (and everything after it)
        from history, then restore its text to the input box for
        editing. Steer markers are stripped so the restored text is
        clean — the user can re-steer with Cmd+Enter if they want."""
        last_user_idx = -1
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx < 0:
            self.notify("没有可退回的用户消息", timeout=2.5)
            return
        raw_content = self.messages[last_user_idx].get("content") or ""
        if raw_content.startswith("[实时插话] "):
            raw_content = raw_content[len("[实时插话] "):]
        del self.messages[last_user_idx:]
        self._drop_explore_if_truncated()
        await self._rebuild_conversation_view()
        await self._autosave_conversation()
        inp = self.query_one("#user-input", MultilineInput)
        inp.text = raw_content
        inp.focus()
        self.notify("已退回到上一条用户消息（已写回输入框）", timeout=2.5)

    async def _rethink_last(self) -> None:
        """Drop everything after the last non-assistant message —
        i.e. the entire trailing assistant turn — so the model
        rethinks from a clean role boundary. ESC×2 drops half-streamed
        thinking before it lands in `self.messages`, so the cut sits
        on the boundary whether or not the last turn was interrupted."""
        cut = len(self.messages)
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") != "assistant":
                cut = i + 1
                break
        if cut >= len(self.messages):
            self.notify(
                "最近一条已经是用户消息或工具结果，无需 rethink",
                timeout=2.5,
            )
            return
        del self.messages[cut:]
        self._drop_explore_if_truncated()
        await self._rebuild_conversation_view()
        await self._autosave_conversation()
        self.notify("已删除最近一轮助手回复", timeout=2.5)

    async def _rebuild_conversation_view(self) -> None:
        """Clear the main conversation view and replay it from
        `self.messages` after a truncation. Leaves queued/steer
        bubbles, bg jobs, and live subagents alone — they're
        independent of message-history state."""
        view = self.query_one("#conversation", VerticalScroll)
        for child in list(view.children):
            child.remove()
        self._cancel_dismiss()
        if self._todo_block is not None and self._todo_block.is_mounted:
            self._todo_block.remove()
        self._todo_block = None
        await self._replay_messages_to_view()
        self._refresh_status()
