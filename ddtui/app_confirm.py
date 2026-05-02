"""Tool confirmation modal and hook types."""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from rich.text import Text
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Static


ToolConfirmHook = Callable[[str, dict], Awaitable[bool]]

WRITE_CONFIRM_TOOLS = frozenset({"write_file", "edit_file", "edit_lines", "multi_edit"})


class ToolConfirmDialog(ModalScreen[bool]):
    """Modal confirmation for write/edit tool calls."""

    CSS = """
    ToolConfirmDialog {
        align: center middle;
    }
    ToolConfirmDialog > #confirm-box {
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        border: round #f9e2af;
        background: #1e1e2e;
    }
    ToolConfirmDialog #confirm-title {
        margin: 0 0 1 0;
        color: #f9e2af;
        text-style: bold;
    }
    ToolConfirmDialog #confirm-body {
        height: auto;
        max-height: 18;
        margin: 0 0 1 0;
    }
    ToolConfirmDialog #confirm-buttons {
        height: auto;
        align-horizontal: right;
    }
    ToolConfirmDialog Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消", show=False),
        Binding("y", "allow", "允许", show=False),
        Binding("n", "cancel", "拒绝", show=False),
    ]

    def __init__(self, name: str, args: dict) -> None:
        super().__init__()
        self.name = name
        self.args = args

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Static(f"确认执行写操作：{self.name}", id="confirm-title")
            yield Static(Text(self._body_text(), style="white"), id="confirm-body", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("拒绝 (N/Esc)", id="deny", variant="error")
                yield Button("允许 (Y)", id="allow", variant="success")

    def _body_text(self) -> str:
        path = self.args.get("path") if isinstance(self.args, dict) else None
        preview_args = self.args
        try:
            raw = json.dumps(preview_args, ensure_ascii=False, indent=2)
        except Exception:
            raw = str(preview_args)
        if len(raw) > 3000:
            raw = raw[:3000] + f"\n…[+{len(raw) - 3000} chars]"
        lines = [
            "模型请求执行会修改文件的工具调用。",
            "请确认这是你期望的改动；拒绝后模型会收到工具被拦截的结果。",
        ]
        if path:
            lines.append(f"目标路径: {path}")
        lines.extend(["", "参数:", raw])
        return "\n".join(lines)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "allow")

    def action_allow(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


async def always_allow(name: str, args: dict) -> bool:
    """Default policy: every tool call is allowed.

    Replace `app.tool_confirm = my_hook` to gate execution on a modal,
    an allowlist, or per-tool rules. Returning False makes the runner
    inject a "Tool execution blocked" tool result instead of invoking
    the function — the model sees this and can adapt.
    """
    return True
