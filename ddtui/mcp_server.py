"""MCP bridge for controlling active ddtui sessions through the relay.

This module intentionally keeps the MCP transport small and dependency-free:
MCP stdio is JSON-RPC over newline-delimited UTF-8 JSON. The relay backend is
the only part that needs the optional ``websockets`` dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import (
    REMOTE_ADDR_FILE,
    REMOTE_PORT,
    REMOTE_TOKEN,
    REMOTE_TOKEN_FILE,
    REMOTE_URL,
    REMOTE_USE_TLS,
)
from .remote_protocol import (
    DEFAULT_CONTROL_PATH,
    DEFAULT_SESSION_PATH,
    normalize_remote_url,
    now_iso,
    read_optional_text,
    url_with_query,
)


SERVER_NAME = "ddtui-mcp"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
MAX_EVENT_CACHE = 500

# Observe detail levels. The controller LLM usually needs "is it busy
# and what has it concluded", not ddtui's working memory — full
# transcripts (reasoning, raw tool results) would flood its context.
OBSERVE_DETAIL_LEVELS = ("status", "chat", "full")
DEFAULT_OBSERVE_DETAIL = "chat"
DEFAULT_OBSERVE_MAX_MESSAGES = 20
DEFAULT_OBSERVE_MAX_CHARS = 800
RESULT_DEFAULT_MAX_CHARS = 4000


def _clip_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"...[+{len(text) - limit:,} chars]"


def _tool_call_names(message: dict[str, Any]) -> list[str]:
    names = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        names.append(str(fn.get("name") or "?"))
    return names


def _condense_messages(
    messages: list[Any],
    *,
    detail: str,
    max_messages: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    """Shape a snapshot transcript for a controller's context.

    status: no transcript at all (the view's last_assistant covers it).
    chat:   the conversation mainline — user/assistant content clipped,
            reasoning dropped, tool round-trips folded to one-line
            placeholders that name the tool and the payload size.
    full:   verbatim messages (still subject to max_messages).
    """
    if detail == "status":
        return []
    tail = [m for m in messages if isinstance(m, dict)]
    if max_messages > 0:
        tail = tail[-max_messages:]
    if detail == "full":
        return tail

    # id → tool name across the WHOLE snapshot (a fold window may start
    # after the assistant message that declared the call).
    call_names: dict[str, str] = {}
    for m in messages:
        if not isinstance(m, dict):
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            call_names[str(tc.get("id") or "")] = str(fn.get("name") or "?")

    condensed: list[dict[str, Any]] = []
    for m in tail:
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "tool":
            name = call_names.get(str(m.get("tool_call_id") or ""), "?")
            condensed.append({
                "role": "tool",
                "content": f"[tool {name} → {len(content):,} chars]",
            })
            continue
        if role == "assistant":
            text = _clip_text(content, max_chars)
            calls = _tool_call_names(m)
            if calls:
                suffix = f"[calls: {', '.join(calls)}]"
                text = f"{text}\n{suffix}" if text else suffix
            condensed.append({"role": "assistant", "content": text})
            continue
        condensed.append({
            "role": str(role or "?"),
            "content": _clip_text(content, max_chars),
        })
    return condensed


def _last_assistant_text(messages: list[Any], max_chars: int) -> str:
    for m in reversed(messages):
        if (
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and (m.get("content") or "")
        ):
            return _clip_text(str(m["content"]), max_chars)
    return ""


class BridgeError(RuntimeError):
    """Tool-facing error that should be returned as an MCP tool error."""


class ProtocolError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RelayBackend:
    """Long-lived control-socket connection to a ddtui relay."""

    url: str
    token: str
    client_id: str
    timeout: float = 10.0
    _ws: Any | None = None
    _recv_task: asyncio.Task | None = None
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _sessions_future: asyncio.Future | None = None
    _command_futures: dict[str, asyncio.Future] = field(default_factory=dict)
    _subscribed: set[str] = field(default_factory=set)
    _sessions: list[dict[str, Any]] = field(default_factory=list)
    _events: dict[str, deque[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(lambda: deque(maxlen=MAX_EVENT_CACHE))
    )
    _snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    _statuses: dict[str, dict[str, Any]] = field(default_factory=dict)
    _last_error: str = ""

    async def close(self) -> None:
        task = self._recv_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._recv_task = None
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def list_sessions(self) -> list[dict[str, Any]]:
        await self._ensure_connected()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._sessions_future = future
        await self._send({"kind": "list_sessions"})
        try:
            sessions = await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            if self._sessions_future is future:
                self._sessions_future = None
        return sessions

    async def observe(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        max_events: int = 30,
        refresh: bool = True,
        detail: str = DEFAULT_OBSERVE_DETAIL,
        max_messages: int = DEFAULT_OBSERVE_MAX_MESSAGES,
        max_chars: int = DEFAULT_OBSERVE_MAX_CHARS,
    ) -> dict[str, Any]:
        session_id = _require_text(session_id, "session_id")
        if detail not in OBSERVE_DETAIL_LEVELS:
            raise BridgeError(
                f"detail must be one of {', '.join(OBSERVE_DETAIL_LEVELS)}"
            )
        await self._ensure_connected()
        await self._subscribe(session_id, after_seq=after_seq)
        if refresh:
            ack = await self.command(session_id, "snapshot", {})
            if ack.get("status") == "error":
                raise BridgeError(str(ack.get("message") or "snapshot failed"))
        return self._session_view(
            session_id,
            max_events=max_events,
            detail=detail,
            max_messages=max_messages,
            max_chars=max_chars,
        )

    async def result(
        self,
        session_id: str,
        *,
        refresh: bool = True,
        max_chars: int = RESULT_DEFAULT_MAX_CHARS,
    ) -> dict[str, Any]:
        """Minimal 'is it done and what did it say' poll: busy flags plus
        the last assistant message, nothing else."""
        session_id = _require_text(session_id, "session_id")
        await self._ensure_connected()
        await self._subscribe(session_id)
        if refresh:
            ack = await self.command(session_id, "snapshot", {})
            if ack.get("status") == "error":
                raise BridgeError(str(ack.get("message") or "snapshot failed"))
        snapshot = self._snapshots.get(session_id, {})
        status = self._statuses.get(session_id) or snapshot.get("status") or {}
        messages = snapshot.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        return {
            "session_id": session_id,
            "busy": bool(status.get("busy")),
            "queued": status.get("queued", 0),
            "steer": status.get("steer", 0),
            "last_assistant": _last_assistant_text(messages, max_chars),
            "observed_at": now_iso(),
        }

    async def new_session(self, session_id: str) -> dict[str, Any]:
        """Reset a session to a fresh conversation (the TUI's /clear).

        The TUI rebuilds its system prefix — re-reading AGENTS.md and
        re-snapshotting the environment — then re-registers on the relay
        under a NEW session_id, so cached state for the old id is
        dropped here and the caller must list sessions again.
        """
        session_id = _require_text(session_id, "session_id")
        ack = await self.command(session_id, "clear", {})
        if ack.get("status") == "error":
            raise BridgeError(str(ack.get("message") or "clear failed"))
        self._snapshots.pop(session_id, None)
        self._statuses.pop(session_id, None)
        self._events.pop(session_id, None)
        self._subscribed.discard(session_id)
        return {
            "status": "accepted",
            "note": (
                "Session cleared. It re-registers under a NEW session_id "
                "with a fresh AGENTS.md + environment snapshot — call "
                "ddtui_list_sessions to find it before the next submit."
            ),
        }

    async def submit(self, session_id: str, text: str) -> dict[str, Any]:
        return await self.command(
            session_id,
            "submit",
            {"text": _require_text(text, "text")},
        )

    async def steer(self, session_id: str, text: str) -> dict[str, Any]:
        return await self.command(
            session_id,
            "steer",
            {"text": _require_text(text, "text")},
        )

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        return await self.command(session_id, "interrupt", {})

    async def terminal_read(
        self,
        session_id: str,
        terminal_id: str = "",
        offset: int = 0,
        max_chars: int = 12000,
    ) -> dict[str, Any]:
        session_id = _require_text(session_id, "session_id")
        terminal_id = terminal_id.strip()
        if not terminal_id:
            terminal_id = self._infer_terminal_id(session_id)
        return await self.command(
            session_id,
            "terminal_read",
            {
                "terminal_id": _require_text(terminal_id, "terminal_id"),
                "offset": max(0, int(offset or 0)),
                "max_chars": max(1, min(int(max_chars or 12000), 200000)),
            },
        )

    async def command(
        self,
        session_id: str,
        command_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        session_id = _require_text(session_id, "session_id")
        await self._ensure_connected()
        await self._subscribe(session_id)
        command_id = secrets.token_hex(8)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._command_futures[command_id] = future
        await self._send(
            {
                "kind": "command",
                "session_id": session_id,
                "command_id": command_id,
                "type": command_type,
                "payload": payload,
                "client_id": self.client_id,
            }
        )
        try:
            ack = await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            self._command_futures.pop(command_id, None)
        return ack

    async def _ensure_connected(self) -> None:
        if self._ws is not None:
            return
        if not self.url:
            raise BridgeError("missing relay URL; pass --relay or set DDTUI_REMOTE_URL")
        if not self.token:
            raise BridgeError(
                "missing relay token; pass --token/--token-file or set "
                "DDTUI_REMOTE_TOKEN"
            )
        try:
            import websockets
        except ImportError as exc:
            raise BridgeError(
                "ddtui-mcp relay backend requires websockets; install with "
                "`pip install -e '.[remote]'` or `pip install -r requirements-remote.txt`"
            ) from exc

        url = url_with_query(self.url, {"token": self.token})
        try:
            self._ws = await websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                max_size=8 * 1024 * 1024,
            )
        except Exception as exc:
            self._ws = None
            raise BridgeError(f"failed to connect relay {self.url}: {exc}") from exc
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def _subscribe(self, session_id: str, after_seq: int | None = None) -> None:
        if session_id in self._subscribed and after_seq is None:
            return
        await self._send(
            {
                "kind": "subscribe",
                "session_id": session_id,
                "after_seq": after_seq if after_seq is not None else -1,
            }
        )
        self._subscribed.add(session_id)

    async def _send(self, message: dict[str, Any]) -> None:
        await self._ensure_socket()
        async with self._send_lock:
            await self._ws.send(json.dumps(message, ensure_ascii=False))

    async def _ensure_socket(self) -> None:
        if self._ws is None:
            await self._ensure_connected()
        if self._ws is None:
            raise BridgeError("relay connection is not available")

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            for future in list(self._command_futures.values()):
                if not future.done():
                    future.set_exception(BridgeError(self._last_error))
            future = self._sessions_future
            if future is not None and not future.done():
                future.set_exception(BridgeError(self._last_error))
        finally:
            self._ws = None

    def _handle_message(self, message: dict[str, Any]) -> None:
        kind = message.get("kind")
        if kind == "sessions.list":
            sessions = message.get("sessions")
            self._sessions = sessions if isinstance(sessions, list) else []
            future = self._sessions_future
            if future is not None and not future.done():
                future.set_result(self._sessions)
            return
        if kind == "command_ack":
            command_id = str(message.get("command_id") or "")
            future = self._command_futures.get(command_id)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if kind == "event":
            session_id = str(message.get("session_id") or "")
            if session_id:
                self._events[session_id].append(message)
            event_type = message.get("type")
            payload = message.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            if event_type == "session.snapshot" and session_id:
                self._snapshots[session_id] = payload
                status = payload.get("status")
                if isinstance(status, dict):
                    self._statuses[session_id] = status
            elif event_type == "status.changed" and session_id:
                status = payload.get("status")
                if isinstance(status, dict):
                    self._statuses[session_id] = status
            return
        if kind == "error":
            self._last_error = str(message.get("message") or "relay error")

    def _session_view(
        self,
        session_id: str,
        *,
        max_events: int,
        detail: str = DEFAULT_OBSERVE_DETAIL,
        max_messages: int = DEFAULT_OBSERVE_MAX_MESSAGES,
        max_chars: int = DEFAULT_OBSERVE_MAX_CHARS,
    ) -> dict[str, Any]:
        events = list(self._events.get(session_id, ()))
        max_events = max(0, min(int(max_events or 0), 200))
        if max_events:
            events = events[-max_events:]
        else:
            events = []
        snapshot = self._snapshots.get(session_id, {})
        status = self._statuses.get(session_id) or snapshot.get("status") or {}
        raw_messages = snapshot.get("messages", [])
        if not isinstance(raw_messages, list):
            raw_messages = []
        return {
            "session_id": session_id,
            "status": status,
            "detail": detail,
            "system_message_count": snapshot.get("system_message_count", 0),
            "messages": _condense_messages(
                raw_messages,
                detail=detail,
                max_messages=max_messages,
                max_chars=max_chars,
            ),
            "last_assistant": _last_assistant_text(raw_messages, max_chars),
            "events": events,
            "last_error": self._last_error,
            "observed_at": now_iso(),
        }

    def _infer_terminal_id(self, session_id: str) -> str:
        status = self._statuses.get(session_id)
        if not status:
            snapshot = self._snapshots.get(session_id) or {}
            status = snapshot.get("status") if isinstance(snapshot, dict) else None
        terminals = []
        if isinstance(status, dict):
            raw = status.get("terminals")
            terminals = raw if isinstance(raw, list) else []
        if len(terminals) == 1 and isinstance(terminals[0], dict):
            return str(terminals[0].get("id") or "")
        if not terminals:
            raise BridgeError(
                "terminal_id is required because this session has no observed terminals"
            )
        raise BridgeError(
            "terminal_id is required because this session has multiple terminals"
        )


class McpStdioServer:
    def __init__(self, backend: RelayBackend) -> None:
        self.backend = backend

    async def run(self) -> None:
        try:
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if line == "":
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    await self._write(
                        self._error(None, -32700, f"Parse error: {exc}")
                    )
                    continue
                if isinstance(message, list):
                    for item in message:
                        await self._handle(item)
                else:
                    await self._handle(message)
        finally:
            await self.backend.close()

    async def _handle(self, message: Any) -> None:
        if not isinstance(message, dict):
            await self._write(self._error(None, -32600, "Invalid Request"))
            return
        message_id = message.get("id")
        method = message.get("method")
        if not method:
            if message_id is not None:
                await self._write(self._error(message_id, -32600, "Missing method"))
            return
        if message_id is None:
            return
        try:
            result = await self._dispatch(method, message.get("params") or {})
        except ProtocolError as exc:
            await self._write(self._error(message_id, exc.code, exc.message))
        except BridgeError as exc:
            await self._write(
                self._ok(
                    message_id,
                    _tool_text_result({"error": str(exc)}, is_error=True)
                    if method == "tools/call"
                    else {"error": str(exc)},
                )
            )
        except Exception as exc:
            await self._write(
                self._error(message_id, -32603, f"{type(exc).__name__}: {exc}")
            )
        else:
            await self._write(self._ok(message_id, result))

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION)
            return {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": "ddtui MCP Bridge",
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "Control an existing ddtui session. Use submit for a normal "
                    "next turn, steer only while the session is busy, and interrupt "
                    "only when the current turn is clearly wrong or stuck."
                ),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": _tool_definitions()}
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            data = await self._call_tool(name, arguments)
            return _tool_text_result(data, is_error=False)
        raise ProtocolError(-32601, f"Method not found: {method}")

    async def _call_tool(self, name: str, args: dict[str, Any]) -> Any:
        if name == "ddtui_list_sessions":
            return {"sessions": await self.backend.list_sessions()}
        if name == "ddtui_observe":
            return await self.backend.observe(
                str(args.get("session_id") or ""),
                after_seq=_optional_int(args.get("after_seq")),
                max_events=int(args.get("max_events") or 30),
                refresh=bool(args.get("refresh", True)),
                detail=str(args.get("detail") or DEFAULT_OBSERVE_DETAIL),
                max_messages=int(
                    args.get("max_messages") or DEFAULT_OBSERVE_MAX_MESSAGES
                ),
                max_chars=int(
                    args.get("max_chars") or DEFAULT_OBSERVE_MAX_CHARS
                ),
            )
        if name == "ddtui_result":
            return await self.backend.result(
                str(args.get("session_id") or ""),
                refresh=bool(args.get("refresh", True)),
                max_chars=int(
                    args.get("max_chars") or RESULT_DEFAULT_MAX_CHARS
                ),
            )
        if name == "ddtui_new_session":
            return await self.backend.new_session(
                str(args.get("session_id") or "")
            )
        if name == "ddtui_submit":
            return await self.backend.submit(
                str(args.get("session_id") or ""),
                str(args.get("text") or ""),
            )
        if name == "ddtui_steer":
            return await self.backend.steer(
                str(args.get("session_id") or ""),
                str(args.get("text") or ""),
            )
        if name == "ddtui_interrupt":
            return await self.backend.interrupt(str(args.get("session_id") or ""))
        if name == "ddtui_terminal_read":
            ack = await self.backend.terminal_read(
                str(args.get("session_id") or ""),
                str(args.get("terminal_id") or ""),
                int(args.get("offset") or 0),
                int(args.get("max_chars") or 12000),
            )
            return ack.get("payload") or ack
        raise ProtocolError(-32602, f"Unknown tool: {name}")

    async def _write(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
        sys.stdout.flush()

    def _ok(self, message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    def _error(
        self,
        message_id: Any,
        code: int,
        message: str,
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": code, "message": message},
        }


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "ddtui_list_sessions",
            "title": "List ddtui sessions",
            "description": "List active ddtui sessions connected to the relay.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "ddtui_observe",
            "title": "Observe ddtui session",
            "description": (
                "Fetch the latest status, transcript tail, and recent relay events "
                "for an active ddtui session. Default detail='chat' returns the "
                "conversation mainline only: reasoning is dropped and tool "
                "round-trips are folded to one-line placeholders, so a long "
                "session stays cheap to observe. Use detail='full' sparingly — "
                "verbatim messages include raw tool output and can be very large."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "after_seq": {
                        "type": "integer",
                        "description": "Only replay cached relay events after this seq.",
                    },
                    "max_events": {
                        "type": "integer",
                        "default": 30,
                        "minimum": 0,
                        "maximum": 200,
                    },
                    "refresh": {
                        "type": "boolean",
                        "default": True,
                        "description": "Ask the session for a fresh snapshot first.",
                    },
                    "detail": {
                        "type": "string",
                        "enum": list(OBSERVE_DETAIL_LEVELS),
                        "default": DEFAULT_OBSERVE_DETAIL,
                        "description": (
                            "status = no transcript (last_assistant only); "
                            "chat = clipped mainline, tools folded; "
                            "full = verbatim messages."
                        ),
                    },
                    "max_messages": {
                        "type": "integer",
                        "default": DEFAULT_OBSERVE_MAX_MESSAGES,
                        "minimum": 1,
                        "description": "Transcript tail length (chat/full).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": DEFAULT_OBSERVE_MAX_CHARS,
                        "minimum": 50,
                        "description": "Per-message clip length in chat detail.",
                    },
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ddtui_result",
            "title": "Poll ddtui result",
            "description": (
                "Cheapest 'is it done and what did it conclude' poll: busy/queued "
                "flags plus the last assistant message. Prefer this over "
                "ddtui_observe when waiting for a submitted turn to finish."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "refresh": {
                        "type": "boolean",
                        "default": True,
                        "description": "Ask the session for a fresh snapshot first.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "default": RESULT_DEFAULT_MAX_CHARS,
                        "minimum": 100,
                        "description": "Clip length for last_assistant.",
                    },
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ddtui_new_session",
            "title": "Reset ddtui to a fresh session",
            "description": (
                "Clear a session's conversation and start fresh (the TUI's "
                "/clear): its tasks/terminals/subagents are torn down and the "
                "system prompt is rebuilt with the CURRENT AGENTS.md and a "
                "fresh environment snapshot — edit the project docs, reset, "
                "and retry. Refused while the session is busy (interrupt "
                "first). The session re-registers under a NEW session_id; "
                "call ddtui_list_sessions afterwards."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ddtui_submit",
            "title": "Submit ddtui input",
            "description": (
                "Send normal user input to a ddtui session. If the session is busy, "
                "ddtui will queue it through the same path as the local UI."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["session_id", "text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ddtui_steer",
            "title": "Steer ddtui session",
            "description": (
                "Send a mid-turn steer/interjection to a busy ddtui session. Use "
                "submit instead when the session is idle."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["session_id", "text"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ddtui_interrupt",
            "title": "Interrupt ddtui session",
            "description": "Request interruption of the current ddtui turn.",
            "inputSchema": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "ddtui_terminal_read",
            "title": "Read ddtui terminal",
            "description": (
                "Read output from a persistent terminal owned by the ddtui session. "
                "This is read-only and does not send shell input."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "terminal_id": {
                        "type": "string",
                        "description": "Optional when the session has exactly one terminal.",
                    },
                    "offset": {"type": "integer", "default": 0, "minimum": 0},
                    "max_chars": {
                        "type": "integer",
                        "default": 12000,
                        "minimum": 1,
                        "maximum": 200000,
                    },
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
    ]


def _tool_text_result(data: Any, *, is_error: bool) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(data, ensure_ascii=False, indent=2),
            }
        ],
        "structuredContent": data,
        "isError": is_error,
    }


def _require_text(value: str, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BridgeError(f"{name} is required")
    return text


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _default_relay_url() -> str:
    if REMOTE_URL:
        return _normalize_control_url(REMOTE_URL)
    raw_addr = read_optional_text(REMOTE_ADDR_FILE)
    if not raw_addr:
        return ""
    return _normalize_control_url(raw_addr)


def _normalize_control_url(raw_url: str) -> str:
    url = normalize_remote_url(
        raw_url,
        role="control",
        secure=REMOTE_USE_TLS,
        port=REMOTE_PORT,
    )
    parsed = urlsplit(url)
    path = parsed.path or DEFAULT_CONTROL_PATH
    if path in ("/", DEFAULT_SESSION_PATH):
        path = DEFAULT_CONTROL_PATH
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def _read_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token.strip()
    if REMOTE_TOKEN:
        return REMOTE_TOKEN
    token_file = (
        Path(args.token_file).expanduser() if args.token_file else REMOTE_TOKEN_FILE
    )
    return read_optional_text(token_file)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an MCP stdio bridge for active ddtui sessions."
    )
    parser.add_argument(
        "--relay",
        default=os.environ.get("DDTUI_MCP_RELAY_URL", ""),
        help="Relay control WebSocket URL, e.g. ws://host:10000/ws/control.",
    )
    parser.add_argument("--token", default="", help="Relay auth token.")
    parser.add_argument(
        "--token-file",
        default=os.environ.get("DDTUI_REMOTE_TOKEN_FILE", str(REMOTE_TOKEN_FILE)),
        help="Path to relay token file.",
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("DDTUI_MCP_CLIENT_ID", ""),
        help="Client id recorded in relay command envelopes.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("DDTUI_MCP_TIMEOUT", "10")),
        help="Relay command timeout in seconds.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    raw_url = args.relay or _default_relay_url()
    url = _normalize_control_url(raw_url) if raw_url else ""
    backend = RelayBackend(
        url=url,
        token=_read_token(args),
        client_id=args.client_id or f"mcp-{secrets.token_hex(6)}",
        timeout=max(1.0, args.timeout),
    )
    asyncio.run(McpStdioServer(backend).run())


if __name__ == "__main__":
    main()
