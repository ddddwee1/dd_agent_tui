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
                    style="bold red",
                )))
                return
            old = self.provider_name
            self.provider = new_provider
            self.provider_name = new_provider.key
            self.model = new_provider.default_model
            self.effort = new_provider.default_effort
            self._update_subtitle()
            await self._mount_widget(Static(Text(
                f"⚙ provider: {old} → {self.provider_name}; "
                f"model={self.model}, effort={self.effort}",
                style="bold cyan",
            )))
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
