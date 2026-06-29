"""Outbound WebSocket client used by a local ddtui session."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .remote_protocol import now_iso, url_with_query


log = logging.getLogger(__name__)

CommandHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
SnapshotFactory = Callable[[], dict[str, Any]]
StatusFactory = Callable[[], dict[str, Any]]
StateCallback = Callable[[str, str], None]


class RemoteClient:
    """Persistent outbound connection from the local TUI to the relay."""

    def __init__(
        self,
        *,
        url: str,
        token: str,
        session_id: str,
        snapshot_factory: SnapshotFactory,
        status_factory: StatusFactory,
        command_handler: CommandHandler,
        on_state: StateCallback | None = None,
        queue_size: int = 500,
    ) -> None:
        self.url = url
        self.token = token
        self.session_id = session_id
        self.snapshot_factory = snapshot_factory
        self.status_factory = status_factory
        self.command_handler = command_handler
        self.on_state = on_state
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=queue_size
        )
        self._stop = asyncio.Event()
        self._seq = 0
        self.connected = False
        self.last_error = ""

    def stop(self) -> None:
        self._stop.set()
        self._put_nowait(
            {
                "kind": "goodbye",
                "session_id": self.session_id,
                "sent_at": now_iso(),
            }
        )

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        self._seq += 1
        message = {
            "kind": "event",
            "type": event_type,
            "session_id": self.session_id,
            "seq": self._seq,
            "sent_at": now_iso(),
            "payload": payload or {},
        }
        self._put_nowait(message)

    def emit_ack(
        self,
        *,
        command_id: str,
        status: str,
        message: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._seq += 1
        self._put_nowait(
            {
                "kind": "command_ack",
                "session_id": self.session_id,
                "seq": self._seq,
                "sent_at": now_iso(),
                "command_id": command_id,
                "status": status,
                "message": message,
                "payload": payload or {},
            }
        )

    def _put_nowait(self, message: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            # Prefer keeping the connection responsive over preserving
            # every intermediate progress event. The next snapshot
            # re-establishes current truth.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                self.stop()
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._notify_state("disconnected", self.last_error)
                log.debug("remote connection failed", exc_info=True)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    backoff = min(backoff * 1.7, 30.0)
        self.connected = False
        self._notify_state("stopped", "")

    async def _run_once(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "remote control requires the 'websockets' package; "
                "install with `pip install -e .[remote]`"
            ) from exc

        url = url_with_query(
            self.url,
            {
                "token": self.token,
                "role": "session",
                "session_id": self.session_id,
            },
        )
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self.connected = True
            self.last_error = ""
            self._notify_state("connected", self.url)
            await ws.send(json.dumps(self._hello_message(), ensure_ascii=False))
            await ws.send(
                json.dumps(
                    {
                        "kind": "event",
                        "type": "session.snapshot",
                        "session_id": self.session_id,
                        "seq": 0,
                        "sent_at": now_iso(),
                        "payload": self.snapshot_factory(),
                    },
                    ensure_ascii=False,
                )
            )
            sender = asyncio.create_task(self._send_loop(ws))
            receiver = asyncio.create_task(self._recv_loop(ws))
            heartbeat = asyncio.create_task(self._heartbeat_loop())
            done, pending = await asyncio.wait(
                {sender, receiver, heartbeat},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()

    def _hello_message(self) -> dict[str, Any]:
        status = self.status_factory()
        return {
            "kind": "hello",
            "role": "session",
            "session_id": self.session_id,
            "sent_at": now_iso(),
            "payload": {
                "session": status,
                "capabilities": [
                    "read",
                    "submit",
                    "steer",
                    "interrupt",
                    "terminal_read",
                    "terminal_send",
                ],
            },
        }

    async def _send_loop(self, ws) -> None:
        while not self._stop.is_set():
            message = await self._queue.get()
            await ws.send(json.dumps(message, ensure_ascii=False))

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("kind") != "command":
                continue
            command_id = str(message.get("command_id") or "")
            try:
                result = await self.command_handler(message)
            except Exception as exc:
                self.emit_ack(
                    command_id=command_id,
                    status="error",
                    message=f"{type(exc).__name__}: {exc}",
                )
                continue
            status = str(result.get("status") or "accepted")
            ack_message = str(result.get("message") or "")
            payload = result.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            self.emit_ack(
                command_id=command_id,
                status=status,
                message=ack_message,
                payload=payload,
            )

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(15)
            self.emit("heartbeat", self.status_factory())

    def _notify_state(self, state: str, detail: str) -> None:
        if self.on_state is None:
            return
        try:
            self.on_state(state, detail)
        except Exception:
            pass
