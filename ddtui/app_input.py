"""Input box and slash-command dispatch mixin for AgentApp."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static, TextArea

from .providers import ProviderConfigError, build_provider, provider_names
from .widgets import MultilineInput, SlashPopup, SteerBubble, UserBubble


class AppInputMixin:
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        # The slash-command popup mirrors the user input; ignore Changed
        # events from any other TextArea that might exist in the tree.
        if getattr(event.text_area, "id", None) != "user-input":
            return
        try:
            popup = self.query_one("#slash-popup", SlashPopup)
        except Exception:
            return
        popup.update_for_text(event.text_area.text)

    def on_multiline_input_completion_requested(
        self, _event: "MultilineInput.CompletionRequested"
    ) -> None:
        """Tab: accept the slash popup's top row.

        With no popup showing there's nothing to complete, so Tab keeps
        its default meaning and moves focus onward.
        """
        try:
            popup = self.query_one("#slash-popup", SlashPopup)
            inp = self.query_one("#user-input", MultilineInput)
        except Exception:
            return
        completion = popup.completion()
        if completion is None:
            self.screen.focus_next()
            return
        if completion != inp.text:
            inp.text = completion
        # `text = ...` reloads the document and parks the cursor at the
        # top; put it back where the user is typing.
        inp.move_cursor(inp.document.end)
        # The reload posts Changed asynchronously; refresh the popup now
        # so the candidate list matches the completed text immediately.
        popup.update_for_text(completion)

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
        if text == "/resume" or text.startswith("/resume "):
            inp.text = ""
            arg = text[len("/resume"):].strip()
            if self._busy:
                self.notify(
                    "正在跑任务；先 ESC×2 中断或等结束再 /resume",
                    timeout=3,
                )
                return
            if arg:
                await self._load_conversation(arg)
            else:
                await self._resume_conversation_picker()
            return
        if text == "/remote" or text.startswith("/remote "):
            inp.text = ""
            arg = text[len("/remote"):].strip()
            parts = arg.split(maxsplit=1)
            cmd = parts[0].lower() if parts else "status"
            rest = parts[1] if len(parts) > 1 else ""
            if cmd in ("", "status"):
                await self._remote_status_notice()
                return
            if cmd == "on":
                await self._remote_start(rest)
                return
            if cmd == "off":
                await self._remote_stop()
                return
            if cmd == "snapshot":
                self._remote_emit_snapshot("slash_command")
                await self._mount_widget(Static(Text(
                    "已向远端推送当前 session snapshot。",
                    style="bold #66d9ef",
                )))
                return
            await self._mount_widget(Static(Text(
                "用法：/remote [status|on [url]|off|snapshot]",
                style="dim",
            )))
            return
        if text in ("/list-history", "/history", "/list_history"):
            inp.text = ""
            await self._list_history()
            return
        if text == "/provider" or text.startswith("/provider "):
            inp.text = ""
            arg = text[len("/provider"):].strip().lower()
            if not arg:
                await self._mount_widget(Static(Text(
                    f"当前 provider: {self.provider_name}\n"
                    f"可用 provider: {', '.join(provider_names())}\n"
                    "用法：/provider <deepseek|codex>  （切换后下一轮请求生效）",
                    style="dim",
                )))
                return
            if self._busy:
                self.notify(
                    "正在跑任务；先 ESC×2 中断或等结束再 /provider",
                    timeout=3,
                )
                return
            try:
                new_provider = build_provider(arg)
            except ProviderConfigError as e:
                await self._mount_widget(Static(Text(
                    f"❌ provider 切换失败：{e}\n"
                    f"当前仍使用 {self.provider_name} · {self.model}。",
                    style="bold #f92672",
                )))
                return
            old = self.provider_name
            self.provider = new_provider
            self.provider_name = new_provider.key
            self.model = new_provider.default_model
            self.effort = new_provider.default_effort
            self._update_subtitle()
            limit_note = ""
            limit = self._context_limit()
            if limit:
                limit_note = f", ctx={limit:,}"
            self._refresh_status()
            await self._mount_widget(Static(Text(
                f"⚙ provider: {old} → {self.provider_name}; "
                f"model={self.model}, effort={self.effort}{limit_note}",
                style="bold #66d9ef",
            )))
            return
        if text == "/model" or text.startswith("/model "):
            inp.text = ""
            arg = text[len("/model"):].strip()
            if not arg:
                await self._mount_widget(Static(self._model_choices_text()))
            else:
                old = self.model
                self.model = arg
                self._update_subtitle()
                self._refresh_status()
                limit_note = ""
                limit = self._context_limit()
                if limit:
                    limit_note = f"；ctx={limit:,}"
                body = Text(
                    f"⚙ model: {old} → {self.model}"
                    f"（下一轮请求生效{limit_note}）",
                    style="bold #66d9ef",
                )
                known = {c.id for c in self.provider.available_models()}
                # Unknown ids still go through as typed — the catalog can
                # lag the service — but flag it, since otherwise a typo
                # only surfaces as an API error mid-turn.
                if known and arg not in known:
                    body.append(
                        f"\n⚠ 不在 {self.provider_name} 候选列表里，"
                        "仍按原样发送；打错的话下一轮会报 API 错误。",
                        style="bold #fd971f",
                    )
                await self._mount_widget(Static(body))
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
                    style="bold #66d9ef",
                )))
            return
        if text == "/rewind":
            inp.text = ""
            if self._busy:
                self.notify(
                    "正在跑任务；先 ESC×2 中断或等结束再 /rewind",
                    timeout=3,
                )
                return
            await self._rewind_last_user()
            return
        if text == "/rethink":
            inp.text = ""
            if self._busy:
                self.notify(
                    "正在跑任务；先 ESC×2 中断或等结束再 /rethink",
                    timeout=3,
                )
                return
            await self._rethink_last()
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
            # Re-assert focus: pending-tray mount can trigger layout
            # recalculation that may steal focus from the input.
            # Use synchronous set_focus (not widget.focus() which defers
            # via call_later) so the assignment beats any async focus
            # shifts that the layout trigger might schedule.
            try:
                self.screen.set_focus(inp)
            except Exception:
                inp.focus()
            return

        # New turn: snap back to the bottom — the user just submitted,
        # so they want to see the response.
        self._follow_bottom = True
        await self._mount_widget(UserBubble(text))
        self._agent_worker = self.run_worker(
            self._agent_turn(text), exclusive=True
        )
        # Ensure input retains focus after mounting (the mount may
        # have laid out new widgets and shifted focus).
        try:
            self.screen.set_focus(inp)
        except Exception:
            inp.focus()

    def _slash_arg_candidates(self, name: str) -> list[tuple[str, str]]:
        """Argument candidates for the slash popup, resolved on demand.

        Only `/model` has a catalog to offer today; unknown commands
        return nothing, which hides the popup once a space is typed —
        the behaviour every command had before.
        """
        if name == "/model":
            return [
                (choice.id, choice.note)
                for choice in self.provider.available_models()
            ]
        return []

    def _model_choices_text(self) -> Text:
        """Render the `/model` candidate list for the active provider.

        Providers with no catalog (empty list) degrade to just the
        current model plus usage, which is what `/model` showed before.
        """
        t = Text()
        t.append("当前 model: ", style="dim")
        t.append(self.model, style="bold #a6e22e")
        choices = self.provider.available_models()
        if choices:
            t.append(f"\n{self.provider.label} 可选模型：", style="dim")
            width = max(len(choice.id) for choice in choices)
            for choice in choices:
                active = choice.id == self.model
                t.append("\n  ")
                t.append(
                    "●" if active else "○",
                    style="bold #a6e22e" if active else "dim",
                )
                t.append(" ")
                t.append(
                    choice.id.ljust(width),
                    style="bold #a6e22e" if active else "bold #66d9ef",
                )
                if choice.note:
                    t.append(f"  {choice.note}", style="dim")
        t.append(
            "\n用法：/model <id>  （下一轮请求开始生效）", style="dim"
        )
        return t

    async def on_multiline_input_steer_submitted(
        self, event: "MultilineInput.SteerSubmitted"
    ) -> None:
        """Cmd/Ctrl+Enter inside the input box: stash the text into the
        steer buffer; the next LLM round will see it as `[实时插话]`."""
        text = event.value.strip()
        if not text:
            return
        inp = self.query_one("#user-input", MultilineInput)
        inp.text = ""
        bubble = SteerBubble(text)
        await self._mount_pending(bubble)
        self._steer.append((text, bubble))
        self._refresh_status()
        # Restore focus synchronously (see queue-mode comment above).
        try:
            self.screen.set_focus(inp)
        except Exception:
            inp.focus()
