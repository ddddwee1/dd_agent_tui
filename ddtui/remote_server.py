"""VPS relay server for ddtui remote control."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .remote_protocol import now_iso

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    FastAPI = None
    WebSocket = None
    WebSocketDisconnect = None
    FileResponse = None
    StaticFiles = None


EVENT_CACHE_SIZE = 500
WEB_DIR = Path(__file__).with_name("remote_web")


@dataclass
class SessionRecord:
    session_id: str
    ws: Any | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    online: bool = False
    connected_at: str = ""
    updated_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    events: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=EVENT_CACHE_SIZE)
    )
    subscribers: set[Any] = field(default_factory=set)


class RelayState:
    def __init__(self, *, token: str) -> None:
        self.token = token
        self.sessions: dict[str, SessionRecord] = {}
        self.controls: set[Any] = set()
        self.lock = asyncio.Lock()

    def check_token(self, token: str | None) -> bool:
        candidate = (token or "").strip()
        return bool(self.token) and secrets.compare_digest(candidate, self.token)

    def session_summaries(self) -> list[dict[str, Any]]:
        items = []
        for record in self.sessions.values():
            if not record.online:
                continue
            meta = record.meta or {}
            items.append(
                {
                    "session_id": record.session_id,
                    "online": record.online,
                    "connected_at": record.connected_at,
                    "updated_at": record.updated_at,
                    "cwd": meta.get("cwd") or meta.get("session", {}).get("cwd"),
                    "provider": meta.get("provider"),
                    "model": meta.get("model"),
                    "busy": meta.get("busy"),
                    "queued": meta.get("queued"),
                    "steer": meta.get("steer"),
                }
            )
        items.sort(key=lambda i: str(i.get("updated_at") or ""), reverse=True)
        return items


def _load_token(args: argparse.Namespace) -> str:
    if args.token:
        return args.token
    if args.token_file:
        try:
            token = Path(args.token_file).read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass
    token = os.environ.get("DDTUI_REMOTE_TOKEN", "").strip()
    if token:
        return token
    token_file = Path.home() / ".ddtui" / "remote_token"
    try:
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    except Exception:
        pass
    raise SystemExit(
        "remote relay requires a token. Set DDTUI_REMOTE_TOKEN or pass "
        "--token-file ~/.ddtui/remote_token"
    )


def _fingerprint(value: str | None) -> str:
    if value is None:
        return "none"
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:12]


def _websocket_token(ws) -> str:
    token = ws.query_params.get("token")
    if token:
        return str(token).strip()
    raw = ws.scope.get("query_string") or b""
    try:
        parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    except Exception:
        return ""
    values = parsed.get("token") or []
    return str(values[0]).strip() if values else ""


async def _send_json(ws, message: dict[str, Any]) -> None:
    await ws.send_text(json.dumps(message, ensure_ascii=False))


async def _send_json_safe(ws, message: dict[str, Any]) -> bool:
    try:
        await _send_json(ws, message)
        return True
    except Exception:
        return False


def create_app(*, token: str):
    if FastAPI is None or WebSocket is None or WebSocketDisconnect is None:
        raise RuntimeError(
            "remote relay requires FastAPI and uvicorn; install with "
            "`pip install -e .[remote]`"
        )

    app = FastAPI(title="ddtui remote relay")
    state = RelayState(token=token)

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "time": now_iso()}

    async def broadcast_sessions() -> None:
        message = {
            "kind": "sessions.list",
            "sent_at": now_iso(),
            "sessions": state.session_summaries(),
        }
        stale = []
        for ws in list(state.controls):
            ok = await _send_json_safe(ws, message)
            if not ok:
                stale.append(ws)
        for ws in stale:
            state.controls.discard(ws)

    async def broadcast_to_subscribers(
        record: SessionRecord, message: dict[str, Any]
    ) -> None:
        stale = []
        for ws in list(record.subscribers):
            ok = await _send_json_safe(ws, message)
            if not ok:
                stale.append(ws)
        for ws in stale:
            record.subscribers.discard(ws)

    @app.websocket("/ws/session")
    async def session_socket(ws: WebSocket):
        await ws.accept()
        token = _websocket_token(ws)
        if not state.check_token(token):
            print(
                "remote auth failed role=session "
                f"got={_fingerprint(token)} expected={_fingerprint(state.token)}",
                flush=True,
            )
            await _send_json(ws, {"kind": "error", "message": "unauthorized"})
            await ws.close(code=4401)
            return
        session_id = ws.query_params.get("session_id") or ""
        record: SessionRecord | None = None
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                kind = message.get("kind")
                if kind == "hello":
                    session_id = str(message.get("session_id") or session_id)
                    if not session_id:
                        session_id = f"session-{secrets.token_hex(4)}"
                    async with state.lock:
                        record = state.sessions.get(session_id)
                        if record is None:
                            record = SessionRecord(session_id=session_id)
                            state.sessions[session_id] = record
                        record.ws = ws
                        record.online = True
                        record.connected_at = now_iso()
                        record.updated_at = record.connected_at
                        payload = message.get("payload") or {}
                        session_meta = payload.get("session") or {}
                        record.meta = (
                            session_meta if isinstance(session_meta, dict) else {}
                        )
                    await broadcast_sessions()
                    continue
                if kind == "event":
                    sid = str(message.get("session_id") or session_id)
                    if not sid:
                        continue
                    async with state.lock:
                        record = state.sessions.get(sid)
                        if record is None:
                            record = SessionRecord(session_id=sid)
                            state.sessions[sid] = record
                        record.ws = ws
                        record.online = True
                        record.updated_at = now_iso()
                        if message.get("type") == "status.changed":
                            payload = message.get("payload") or {}
                            status = payload.get("status") or {}
                            if isinstance(status, dict):
                                record.meta.update(status)
                        if message.get("type") == "session.snapshot":
                            payload = message.get("payload") or {}
                            status = payload.get("status") or {}
                            if isinstance(status, dict):
                                record.meta.update(status)
                        record.events.append(message)
                    await broadcast_to_subscribers(record, message)
                    if message.get("type") in ("status.changed", "session.snapshot"):
                        await broadcast_sessions()
                    continue
                if kind == "command_ack":
                    sid = str(message.get("session_id") or session_id)
                    record = state.sessions.get(sid)
                    if record is not None:
                        await broadcast_to_subscribers(record, message)
                    continue
                if kind == "goodbye":
                    break
        except WebSocketDisconnect:
            pass
        finally:
            if session_id:
                subscribers = []
                async with state.lock:
                    record = state.sessions.get(session_id)
                    if record is not None and record.ws is ws:
                        record.online = False
                        record.ws = None
                        record.updated_at = now_iso()
                        subscribers = list(record.subscribers)
                        state.sessions.pop(session_id, None)
                for sub in subscribers:
                    await _send_json_safe(
                        sub,
                        {
                            "kind": "event",
                            "type": "session.offline",
                            "session_id": session_id,
                            "sent_at": now_iso(),
                            "payload": {"session_id": session_id},
                        },
                    )
                await broadcast_sessions()

    @app.websocket("/ws/control")
    async def control_socket(ws: WebSocket):
        await ws.accept()
        token = _websocket_token(ws)
        if not state.check_token(token):
            print(
                "remote auth failed role=control "
                f"got={_fingerprint(token)} expected={_fingerprint(state.token)}",
                flush=True,
            )
            await _send_json(ws, {"kind": "error", "message": "unauthorized"})
            await ws.close(code=4401)
            return
        state.controls.add(ws)
        subscriptions: set[str] = set()
        await _send_json(
            ws,
            {
                "kind": "sessions.list",
                "sent_at": now_iso(),
                "sessions": state.session_summaries(),
            },
        )
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                kind = message.get("kind")
                if kind == "list_sessions":
                    await _send_json(
                        ws,
                        {
                            "kind": "sessions.list",
                            "sent_at": now_iso(),
                            "sessions": state.session_summaries(),
                        },
                    )
                    continue
                if kind == "subscribe":
                    sid = str(message.get("session_id") or "")
                    if not sid:
                        continue
                    record = state.sessions.get(sid)
                    if record is None:
                        await _send_json(
                            ws,
                            {
                                "kind": "error",
                                "message": f"unknown session {sid}",
                            },
                        )
                        continue
                    record.subscribers.add(ws)
                    subscriptions.add(sid)
                    after_seq = message.get("after_seq")
                    try:
                        after_seq_int = int(after_seq)
                    except Exception:
                        after_seq_int = -1
                    for event in record.events:
                        try:
                            seq = int(event.get("seq", 0))
                        except Exception:
                            seq = 0
                        if seq > after_seq_int:
                            await _send_json(ws, event)
                    continue
                if kind == "unsubscribe":
                    sid = str(message.get("session_id") or "")
                    record = state.sessions.get(sid)
                    if record is not None:
                        record.subscribers.discard(ws)
                    subscriptions.discard(sid)
                    continue
                if kind == "command":
                    sid = str(message.get("session_id") or "")
                    record = state.sessions.get(sid)
                    if record is None or not record.online or record.ws is None:
                        await _send_json(
                            ws,
                            {
                                "kind": "command_ack",
                                "session_id": sid,
                                "command_id": message.get("command_id") or "",
                                "status": "error",
                                "message": "session is offline",
                            },
                        )
                        continue
                    command_id = str(message.get("command_id") or secrets.token_hex(8))
                    outbound = {
                        "kind": "command",
                        "session_id": sid,
                        "command_id": command_id,
                        "type": message.get("type"),
                        "payload": message.get("payload") or {},
                        "client_id": message.get("client_id") or "",
                        "sent_at": now_iso(),
                    }
                    async with record.send_lock:
                        ok = await _send_json_safe(record.ws, outbound)
                    if not ok:
                        record.online = False
                        record.ws = None
                        await _send_json(
                            ws,
                            {
                                "kind": "command_ack",
                                "session_id": sid,
                                "command_id": command_id,
                                "status": "error",
                                "message": "failed to forward command to session",
                            },
                        )
                        await broadcast_sessions()
                    continue
        except WebSocketDisconnect:
            pass
        finally:
            state.controls.discard(ws)
            for sid in subscriptions:
                record = state.sessions.get(sid)
                if record is not None:
                    record.subscribers.discard(ws)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ddtui remote relay.")
    parser.add_argument("--host", default=os.environ.get("DDTUI_REMOTE_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DDTUI_REMOTE_PORT", "10000")),
    )
    parser.add_argument("--token", default="")
    parser.add_argument("--token-file", default=os.environ.get("DDTUI_REMOTE_TOKEN_FILE", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    token = _load_token(args)
    app = create_app(token=token)
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "uvicorn is required for the remote relay; install with "
            "`pip install -e .[remote]`"
        ) from exc
    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "websockets",
        "websockets.server",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        log_config=None,
        log_level="critical",
    )


if __name__ == "__main__":
    main()
