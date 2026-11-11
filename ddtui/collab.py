"""Optional local Git handoff daemon and terminal recovery commands.

The daemon uses only Git and the standard library. It never starts an LLM,
executes repository commands, or depends on the TUI staying connected.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid

from .collab_git import GitTransport, repository_url
from .collab_protocol import CollabError, MAX_MESSAGE_BYTES, identifier
from .collab_store import ClaimLock, Store, collab_home


class HandoffService:
    def __init__(self, home: Path):
        self.store = Store(home)
        self.home = self.store.home
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.git_lock = threading.RLock()
        self.retry_after = {}
        self.failures = {}

    def defer(self, name):
        count = min(6, self.failures.get(name, 0) + 1)
        self.failures[name] = count
        self.retry_after[name] = time.time() + min(300, 5 * 2 ** (count - 1))

    def transport(self, binding):
        return GitTransport(self.home, self.store.binding(binding))

    @staticmethod
    def task_summary(task):
        return {key: task[key] for key in ("task_id", "revision", "state", "owner", "generation")} | {
            "request": {"priority": task["request"]["priority"]}}

    def dispatch(self, message):
        action = message.get("action")
        binding = message.get("binding", "default")
        if action == "ping":
            return {"pid": os.getpid(), "protocol": 1}
        if action == "bind":
            worker = message.get("worker_id")
            if not worker:
                existing = next((item for item in self.store.bindings() if item["name"] == binding), None)
                worker = existing["worker_id"] if existing else "worker-" + uuid.uuid4().hex[:8]
            return self.store.bind(binding, repository_url(message["url"]),
                                   worker,
                                   message.get("interval", 15))
        if action in ("on", "off"):
            result = self.store.listening(binding, action == "on")
            self.wake.set()
            return result
        if action == "status":
            config = self.store.binding(binding)
            return {"binding": config, "tasks": [self.task_summary(t) for t in self.store.tasks(binding)],
                    "pending_receipts": [{"task_id": p["task_id"], "error": p["error"]}
                                         for p in self.store.pending() if p["binding"] == binding],
                    "pending_results": [{"task_id": p["task_id"], "revision": p["revision"], "error": p["error"]}
                                        for p in self.store.publications() if p["binding"] == binding]}
        if action == "tasks":
            return [self.task_summary(t) for t in self.store.tasks(binding)]
        if action == "shutdown":
            if any(task["owner"] for cfg in self.store.bindings() for task in self.store.tasks(cfg["name"])):
                raise CollabError("Release claimed tasks before stopping the daemon")
            self.stopping.set()
            return {"state": "stopping"}
        if action == "publish":
            with self.git_lock:
                commit = self.transport(binding).publish_request(message["request"])
            self.wake.set()
            return {"request_commit": commit}
        task_id = identifier(message.get("task_id"))
        task = self.store.task(binding, task_id)
        if action == "task":
            return task
        if action == "claim":
            if task["owner"]:
                raise CollabError("Task already has an owner; release or reconcile it first")
            lease = ClaimLock(self.home, binding, task_id)
            lease.verify(message.get("lease_id"))
            with self.git_lock:
                workspace = self.transport(binding).workspace(task["request"])
            lease.verify(message.get("lease_id"))
            return self.store.claim(binding, task_id, message["owner"], message["pid"], str(workspace))
        if action == "release":
            result = self.store.release(binding, task_id, message.get("token"))
            self.wake.set()
            return result
        token = message.get("token")
        if not token or task["token"] != token:
            raise CollabError("Stale claim; this session no longer owns the task")
        if action == "pause":
            return self.store.update_owned(binding, task_id, token, state="pausing")
        if action == "activity":
            return self.store.update_owned(binding, task_id, token, activity=message["activity"],
                                           children=message.get("children"))
        if action == "turn_failed":
            return self.store.update_owned(binding, task_id, token, state="needs_attention", activity="idle")
        if action == "admit":
            if task["state"] in ("pausing", "orphaned") or task["request"]["state"] == "retired":
                raise CollabError("Task is paused, retired or needs reconciliation")
            if any(p["binding"] == binding and p["task_id"] == task_id for p in self.store.publications()):
                raise CollabError("Previous result is still publishing; no new experiment will be started")
            with self.git_lock:
                self.transport(binding).workspace(task["request"])
            return self.store.update_owned(binding, task_id, token, state="running", activity="turn",
                                           delivered=message["revision"])
        if action == "turn_done":
            path = Path(task["work_dir"]) / ".ddtui-collab/result.json"
            if path.is_file():
                if path.stat().st_size > MAX_MESSAGE_BYTES:
                    raise CollabError("Result manifest exceeds 128 KiB")
                payload = json.loads(path.read_bytes())
                if payload.get("revision") == task["active_request"]["revision"]:
                    with self.git_lock:
                        result = self.transport(binding).prepare_result(task["active_request"], Path(task["work_dir"]), payload)
                    if task["result"] and task["result"]["revision"] == result["revision"]:
                        if task["result"] != result:
                            raise CollabError("A published result is immutable; request a new revision")
                        return {"state": "awaiting_input", "already_published": True}
                    self.store.queue_publication(binding, task_id, token,
                                                 {"request": task["active_request"], "result": result,
                                                  "work_dir": task["work_dir"]})
                    self.wake.set()
                    return {"state": "publishing"}
            # An ordinary assistant final answer is only a turn boundary.
            return self.store.update_owned(binding, task_id, token, state="awaiting_input", activity="idle")
        raise CollabError("Unknown handoff action")

    def tick(self, force=False):
        for config in self.store.bindings():
            name = config["name"]
            if not force and time.time() < self.retry_after.get(name, 0):
                continue
            if config["enabled"] and (force or not config["last_attempt"] or
                                      time.time() - config["last_attempt"] >= config["interval"]):
                try:
                    with self.git_lock:
                        requests = self.transport(name).requests()
                    for request in requests:
                        self.store.ingest(name, request)
                    self.store.poll_status(name)
                except (CollabError, OSError, ValueError) as error:
                    self.store.poll_status(name, str(error))
                    self.defer(name)
        # Publication and receipt retries keep working when incoming listening is off.
        for item in self.store.publications():
            if not force and time.time() < self.retry_after.get(item["binding"], 0):
                continue
            try:
                payload = json.loads(item["payload"])
                with self.git_lock:
                    self.transport(item["binding"]).publish_candidate(
                        payload["request"], Path(payload["work_dir"]), payload["result"]["candidate_commit"])
                self.store.published(item)
                self.failures[item["binding"]] = 0
            except (CollabError, OSError, ValueError) as error:
                self.store.published(item, str(error))
                self.defer(item["binding"])
        for item in self.store.pending():
            if not force and time.time() < self.retry_after.get(item["binding"], 0):
                continue
            try:
                with self.git_lock:
                    self.transport(item["binding"]).publish_receipt(json.loads(item["payload"]))
                self.store.sent(item)
                self.failures[item["binding"]] = 0
            except (CollabError, OSError, ValueError) as error:
                self.store.sent(item, str(error))
                self.defer(item["binding"])
        from .runtime_state import pid_alive
        for config in self.store.bindings():
            for task in self.store.tasks(config["name"]):
                if task["owner"] and not pid_alive(task["owner_pid"]) and task["state"] != "orphaned":
                    self.store.update_owned(config["name"], task["task_id"], task["token"], state="orphaned")

    def run(self):
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception:
                # SQLite or local I/O failures are retried without discarding durable work.
                import logging
                logging.exception("Handoff poll failed")
            self.wake.wait(1)
            self.wake.clear()


def socket_path(home):
    path = home / "control.sock"
    if len(os.fsencode(path)) >= 104:
        raise CollabError("Collaboration home is too long for a Unix socket; set a shorter DDTUI_COLLAB_HOME")
    return path


def rpc(home: Path, action: str, *, timeout=90, **payload):
    request = {"action": action, **payload}
    wire = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    if len(wire) > MAX_MESSAGE_BYTES:
        raise CollabError("IPC request exceeds 128 KiB")
    # Local status may contain bounded manifests for multiple tasks.
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(socket_path(home)))
            sock.sendall(wire)
            with sock.makefile("rb") as stream:
                raw = stream.readline(4 * 1024 * 1024 + 1)
        except (OSError, TimeoutError) as error:
            raise CollabError("Handoff daemon unavailable; local claims and pending deliveries are retained") from error
    if len(raw) > 4 * 1024 * 1024:
        raise CollabError("Daemon response too large")
    try:
        response = json.loads(raw)
    except ValueError as error:
        raise CollabError("Invalid daemon response") from error
    if not response.get("ok"):
        raise CollabError(response.get("error", "Handoff operation failed"))
    return response["result"]


def ensure_daemon(home: Path):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        return rpc(home, "ping", timeout=0.5)
    except CollabError:
        pass
    logfile = home / "daemon.log"
    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen([sys.executable, "-m", "ddtui.collab", "--home", str(home), "serve"],
                         cwd=Path(__file__).resolve().parent.parent,
                         stdin=subprocess.DEVNULL, stdout=fd, stderr=fd, start_new_session=True)
    finally:
        os.close(fd)
    for _ in range(60):
        try:
            return rpc(home, "ping", timeout=0.2)
        except CollabError:
            time.sleep(0.05)
    raise CollabError(f"Cannot start handoff daemon; inspect {logfile}")


def serve(home):
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(home / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        service = HandoffService(home)

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(90)
                raw = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
                try:
                    if len(raw) > MAX_MESSAGE_BYTES:
                        raise CollabError("IPC request too large")
                    request = json.loads(raw)
                    if not isinstance(request, dict):
                        raise CollabError("IPC request must be an object")
                    result = service.dispatch(request)
                    response = {"ok": True, "result": result}
                except (CollabError, OSError, ValueError, KeyError, TypeError) as error:
                    response = {"ok": False, "error": str(error)}
                self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        path = socket_path(home)
        path.unlink(missing_ok=True)
        with Server(str(path), Handler) as server:
            path.chmod(0o600)
            server.timeout = 0.5
            thread = threading.Thread(target=service.run, daemon=True)
            thread.start()
            try:
                while not service.stopping.is_set():
                    server.handle_request()
            finally:
                service.stopping.set()
                service.wake.set()
                thread.join(timeout=65)
                path.unlink(missing_ok=True)
    finally:
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Optional Git handoff daemon and task recovery")
    parser.add_argument("--home", type=Path, default=collab_home())
    parser.add_argument("--binding", default="default")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    bind = commands.add_parser("bind")
    bind.add_argument("url")
    bind.add_argument("--worker-id")
    bind.add_argument("--interval", type=float, default=15)
    for name in ("on", "off", "status", "tasks", "stop"):
        commands.add_parser(name)
    task = commands.add_parser("task").add_subparsers(dest="task_command", required=True)
    for name in ("status", "release"):
        task.add_parser(name).add_argument("task_id")
    task.add_parser("publish").add_argument("file", type=Path)
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    try:
        if args.command == "serve":
            serve(home)
            return 0
        ensure_daemon(home)
        options = {"binding": args.binding}
        if args.command == "bind":
            result = rpc(home, "bind", **options, url=args.url, worker_id=args.worker_id, interval=args.interval)
        elif args.command == "task":
            if args.task_command == "publish":
                if args.file.stat().st_size > MAX_MESSAGE_BYTES:
                    raise CollabError("Request file exceeds 128 KiB")
                result = rpc(home, "publish", **options, request=json.loads(args.file.read_bytes()))
            else:
                result = rpc(home, "task" if args.task_command == "status" else "release",
                             **options, task_id=args.task_id)
        else:
            result = rpc(home, "shutdown" if args.command == "stop" else args.command, **options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (CollabError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
