"""Remote-control integration for AgentApp."""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from rich.text import Text
from textual.widgets import Static

from .config import (
    REMOTE_ADDR_FILE,
    REMOTE_ALLOW_TERMINAL_SEND,
    REMOTE_AUTO_CONNECT,
    REMOTE_PORT,
    REMOTE_SNAPSHOT_MAX_MESSAGES,
    REMOTE_TOKEN,
    REMOTE_TOKEN_FILE,
    REMOTE_URL,
    REMOTE_USE_TLS,
)
from .remote_client import RemoteClient
from .remote_protocol import (
    normalize_remote_url,
    now_iso,
    read_optional_text,
    safe_json,
)
from .tools_terminal import (
    tool_terminal_interrupt,
    tool_terminal_read,
    tool_terminal_send,
)
from .widgets import SteerBubble, UserBubble


class AppRemoteMixin:
    def _init_remote_state(self) -> None:
        self._remote_client: RemoteClient | None = None
        self._remote_worker = None
        self._remote_state = "off"
        self._remote_detail = ""
        self._remote_last_status_emit = 0.0

    def _remote_token(self, *, create: bool = False) -> str:
        if REMOTE_TOKEN:
            return REMOTE_TOKEN
        token = read_optional_text(REMOTE_TOKEN_FILE)
        if token:
            return token
        if not create:
            return ""
        token = secrets.token_urlsafe(24)
        try:
            REMOTE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            REMOTE_TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
            try:
                REMOTE_TOKEN_FILE.chmod(0o600)
            except Exception:
                pass
        except Exception:
            return token
        return token

    def _remote_default_url(self) -> str:
        if REMOTE_URL:
            return normalize_remote_url(
                REMOTE_URL,
                role="session",
                secure=REMOTE_USE_TLS,
                port=REMOTE_PORT,
            )
        raw_addr = read_optional_text(REMOTE_ADDR_FILE)
        if not raw_addr:
            return ""
        return normalize_remote_url(
            raw_addr,
            role="session",
            secure=REMOTE_USE_TLS,
            port=REMOTE_PORT,
        )

    async def _maybe_start_remote_from_config(self) -> None:
        if not REMOTE_AUTO_CONNECT:
            return
        await self._remote_start()

    async def _remote_start(self, raw_url: str = "") -> None:
        url = normalize_remote_url(
            raw_url,
            role="session",
            secure=REMOTE_USE_TLS,
            port=REMOTE_PORT,
        ) if raw_url else self._remote_default_url()
        if not url:
            await self._remote_notice(
                "远控未启动：没有 DDTUI_REMOTE_URL，也无法从 "
                f"{REMOTE_ADDR_FILE} 推导 VPS 地址。",
                style="bold #f92672",
            )
            return
        try:
            import websockets  # noqa: F401
        except ImportError:
            await self._remote_notice(
                "远控未启动：缺少 websockets 依赖。\n"
                "安装：pip install -e '.[remote]' 或 pip install -r requirements-remote.txt",
                style="bold #f92672",
            )
            return
        if self._remote_client is not None:
            self._remote_client.stop()
        token = self._remote_token(create=True)
        self._remote_client = RemoteClient(
            url=url,
            token=token,
            session_id=self._session_id,
            snapshot_factory=self._remote_snapshot_payload,
            status_factory=self._remote_status_payload,
            command_handler=self._remote_handle_command,
            on_state=self._remote_state_changed,
        )
        self._remote_worker = self.run_worker(
            self._remote_client.run(),
            exclusive=False,
            group="remote",
            name="remote-control",
        )
        await self._remote_notice(
            f"远控连接中：{url}\n"
            f"登录 token 来源：{REMOTE_TOKEN_FILE if not REMOTE_TOKEN else 'DDTUI_REMOTE_TOKEN'}",
            style="bold #66d9ef",
        )

    async def _remote_stop(self) -> None:
        client = self._remote_client
        if client is not None:
            client.stop()
        self._remote_client = None
        self._remote_state = "off"
        self._remote_detail = ""
        await self._remote_notice("远控已关闭。", style="bold #e6db74")

    async def _remote_status_notice(self) -> None:
        url = self._remote_client.url if self._remote_client else self._remote_default_url()
        token_source = "DDTUI_REMOTE_TOKEN" if REMOTE_TOKEN else str(REMOTE_TOKEN_FILE)
        await self._remote_notice(
            "远控状态："
            f"{self._remote_state}\n"
            f"url: {url or '(未配置)'}\n"
            f"session: {self._session_id}\n"
            f"token: {token_source}\n"
            f"terminal_send: {'enabled' if REMOTE_ALLOW_TERMINAL_SEND else 'disabled'}\n"
            f"detail: {self._remote_detail or '-'}",
            style="bold #66d9ef",
        )

    async def _remote_notice(self, text: str, *, style: str) -> None:
        await self._mount_widget(Static(Text(text, style=style)))

    def _remote_state_changed(self, state: str, detail: str) -> None:
        self._remote_state = state
        self._remote_detail = detail
        self.call_later(self._remote_emit_status, "connection", True)

    def _remote_emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        client = self._remote_client
        if client is None:
            return
        client.emit(event_type, safe_json(payload or {}))

    def _remote_emit_status(self, reason: str = "status", force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._remote_last_status_emit < 1.5:
            return
        self._remote_last_status_emit = now
        self._remote_emit(
            "status.changed",
            {
                "reason": reason,
                "status": self._remote_status_payload(),
            },
        )

    def _remote_emit_snapshot(self, reason: str = "snapshot") -> None:
        self._remote_emit(
            "session.snapshot",
            {
                **self._remote_snapshot_payload(),
                "reason": reason,
            },
        )

    def _remote_status_payload(self) -> dict[str, Any]:
        terminals = []
        for term in self.ctx.terminals.values():
            rc = term.proc.poll()
            terminals.append(
                {
                    "id": term.id,
                    "name": term.name,
                    "command": term.command,
                    "workdir": term.workdir,
                    "status": "running" if rc is None and not term.closed else "exited",
                    "return_code": rc,
                    "output_size": term.output_size,
                    "tail": term.tail[-4000:],
                }
            )
        return {
            "session_id": self._session_id,
            "cwd": self.ctx.work_dir,
            "provider": self.provider_name,
            "model": self.model,
            "effort": self.effort,
            "busy": self._busy,
            "queued": len(self._queued),
            "steer": len(self._steer),
            # Context pressure + runtime counts for the web status bar —
            # the remote UI mirrors the TUI's "how close to the window"
            # gauge and the sidebar's running-work overview.
            "turns": self.counter.turns,
            "context_used": (
                self.counter.last_prompt + self.counter.last_completion
            ),
            "context_limit": self._context_limit(),
            "tasks_running": sum(
                1 for t in self.ctx.tasks.values() if t.proc.poll() is None
            ),
            "subagents": len(getattr(self, "_live_subagents", {})),
            "remote_state": self._remote_state,
            "remote_detail": self._remote_detail,
            "terminal_send_enabled": REMOTE_ALLOW_TERMINAL_SEND,
            "terminals": terminals,
            "updated_at": now_iso(),
        }

    def _remote_snapshot_payload(self) -> dict[str, Any]:
        visible_messages = [
            m for m in self.messages
            if isinstance(m, dict) and m.get("role") != "system"
        ]
        if REMOTE_SNAPSHOT_MAX_MESSAGES > 0:
            visible_messages = visible_messages[-REMOTE_SNAPSHOT_MAX_MESSAGES:]
        return {
            "status": self._remote_status_payload(),
            "system_message_count": sum(
                1 for m in self.messages
                if isinstance(m, dict) and m.get("role") == "system"
            ),
            "messages": safe_json(visible_messages),
            "updated_at": now_iso(),
        }

    async def _remote_handle_command(self, command: dict[str, Any]) -> dict[str, Any]:
        command_type = str(command.get("type") or "")
        payload = command.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        if command_type in ("submit", "command.submit"):
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"status": "error", "message": "empty submit text"}
            await self._remote_submit_text(text, as_steer=False)
            return {"status": "accepted", "message": "submitted"}
        if command_type in ("steer", "command.steer"):
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"status": "error", "message": "empty steer text"}
            await self._remote_submit_text(text, as_steer=True)
            return {"status": "accepted", "message": "steer queued"}
        if command_type in ("interrupt", "command.interrupt"):
            self._interrupt_now()
            self._remote_emit_status("interrupt", force=True)
            return {"status": "accepted", "message": "interrupt requested"}
        if command_type in ("clear", "command.clear"):
            # Same semantics as local /clear: wipe the conversation,
            # tear down its tasks/terminals/subagents, rebuild the
            # system prefix (fresh AGENTS.md + env snapshot), and
            # restart autosave under a NEW session_id — the session
            # re-registers on the relay under that id. Refuse while
            # busy: the engine would keep appending to the wiped
            # history mid-turn.
            if self._busy:
                return {
                    "status": "error",
                    "message": "session is busy; interrupt first, then clear",
                }
            # Deferred: action_clear_chat restarts the remote client,
            # which would kill this very connection before the ack got
            # written. call_later runs it on the next loop tick.
            self.call_later(self.action_clear_chat)
            return {
                "status": "accepted",
                "message": (
                    "clearing; the session re-registers under a new "
                    "session_id — list sessions to find it"
                ),
            }
        if command_type in ("snapshot", "command.snapshot"):
            self._remote_emit_snapshot("remote_request")
            return {"status": "accepted", "message": "snapshot sent"}
        if command_type in ("terminal_read", "command.terminal_read"):
            terminal_id = str(payload.get("terminal_id") or "")
            offset = payload.get("offset", 0)
            max_chars = payload.get("max_chars", 12000)
            result = await asyncio.to_thread(
                tool_terminal_read,
                self.ctx,
                terminal_id,
                offset,
                max_chars,
            )
            return {
                "status": "accepted",
                "message": "terminal read",
                "payload": {"result": result},
            }
        if command_type in ("terminal_send", "command.terminal_send"):
            if not REMOTE_ALLOW_TERMINAL_SEND:
                return {
                    "status": "error",
                    "message": (
                        "terminal_send is disabled; set "
                        "DDTUI_REMOTE_ALLOW_TERMINAL_SEND=1 locally to enable"
                    ),
                }
            terminal_id = str(payload.get("terminal_id") or "")
            text = str(payload.get("text") or "")
            wait_ms = payload.get("wait_ms", 200)
            result = await asyncio.to_thread(
                tool_terminal_send,
                self.ctx,
                terminal_id,
                text,
                wait_ms,
            )
            self._remote_emit_status("terminal_send", force=True)
            return {
                "status": "accepted",
                "message": "terminal send",
                "payload": {"result": result},
            }
        if command_type in ("terminal_interrupt", "command.terminal_interrupt"):
            if not REMOTE_ALLOW_TERMINAL_SEND:
                return {
                    "status": "error",
                    "message": (
                        "terminal_interrupt is disabled; set "
                        "DDTUI_REMOTE_ALLOW_TERMINAL_SEND=1 locally to enable"
                    ),
                }
            terminal_id = str(payload.get("terminal_id") or "")
            result = await asyncio.to_thread(
                tool_terminal_interrupt,
                self.ctx,
                terminal_id,
            )
            self._remote_emit_status("terminal_interrupt", force=True)
            return {
                "status": "accepted",
                "message": "terminal interrupt",
                "payload": {"result": result},
            }
        return {"status": "error", "message": f"unknown command {command_type!r}"}

    async def _remote_submit_text(self, text: str, *, as_steer: bool) -> None:
        self._follow_bottom = True
        if as_steer:
            bubble = SteerBubble(text)
            await self._mount_pending(bubble)
            self._steer.append((text, bubble))
            self._refresh_status()
            self._remote_emit(
                "remote.input",
                {"mode": "steer", "text": text, "accepted_at": now_iso()},
            )
            return

        if self._busy:
            bubble = UserBubble(text)
            await self._mount_pending(bubble)
            self._queued.append((text, bubble))
            self._refresh_status()
            self._remote_emit(
                "remote.input",
                {"mode": "queued", "text": text, "accepted_at": now_iso()},
            )
            return

        await self._mount_widget(UserBubble(text))
        self._agent_worker = self.run_worker(
            self._agent_turn(text), exclusive=True
        )
        self._refresh_status()

    def _remote_on_session_reset(self) -> None:
        client = self._remote_client
        if client is None:
            return
        url = client.url
        client.stop()
        self._remote_client = RemoteClient(
            url=url,
            token=self._remote_token(create=True),
            session_id=self._session_id,
            snapshot_factory=self._remote_snapshot_payload,
            status_factory=self._remote_status_payload,
            command_handler=self._remote_handle_command,
            on_state=self._remote_state_changed,
        )
        self._remote_worker = self.run_worker(
            self._remote_client.run(),
            exclusive=False,
            group="remote",
            name="remote-control",
        )
