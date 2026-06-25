"""Detached runner used by task_start.

The TUI process starts this helper in its own session. The helper then
runs the requested shell command, streams merged stdout/stderr to the
task output file, and writes the final status even if the TUI exits.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


_child: subprocess.Popen | None = None
_terminating = False


def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).isoformat(timespec="seconds")


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _status_payload(
    spec: dict[str, Any],
    status: str,
    return_code: int | None,
    *,
    command_pid: int | None = None,
    finished_at: float | None = None,
) -> dict[str, Any]:
    started_wall = float(spec.get("started_wall") or time.time())
    end = finished_at or time.time()
    return {
        "task_id": spec.get("task_id"),
        "session_id": spec.get("session_id"),
        "name": spec.get("name"),
        "status": status,
        "return_code": return_code,
        "runner_pid": os.getpid(),
        "pid": os.getpid(),
        "command_pid": command_pid,
        "command": spec.get("command"),
        "workdir": spec.get("workdir"),
        "started_at": _iso(started_wall),
        "finished_at": _iso(end) if status != "running" else None,
        "duration_sec": round(end - started_wall, 3),
        "output_path": spec.get("output_path"),
        "status_path": spec.get("status_path"),
        "notify_on_complete": bool(spec.get("notify_on_complete", True)),
    }


def _handle_signal(signum: int, _frame) -> None:
    global _terminating
    _terminating = True
    if _child is not None and _child.poll() is None:
        try:
            _child.terminate()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    global _child
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: task_runner.py <spec.json>", file=sys.stderr)
        return 2
    spec_path = Path(args[0])
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"could not read task spec: {e}", file=sys.stderr)
        return 2

    status_path = Path(str(spec["status_path"]))
    output_path = Path(str(spec["output_path"]))
    started_wall = float(spec.get("started_wall") or time.time())
    spec["started_wall"] = started_wall

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    rc = 1
    command_pid: int | None = None
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "ab", buffering=0) as log_fh:
            _child = subprocess.Popen(
                str(spec["command"]),
                shell=True,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                cwd=str(spec["workdir"]),
            )
            command_pid = _child.pid
            _write_status(
                status_path,
                _status_payload(
                    spec, "running", None, command_pid=command_pid
                ),
            )
            rc = _child.wait()
        finished = time.time()
        if _terminating:
            status = "killed"
        else:
            status = "success" if rc == 0 else "failed"
        _write_status(
            status_path,
            _status_payload(
                spec,
                status,
                rc,
                command_pid=command_pid,
                finished_at=finished,
            ),
        )
        return rc if isinstance(rc, int) else 1
    except BaseException as e:
        finished = time.time()
        try:
            with open(output_path, "ab", buffering=0) as log_fh:
                log_fh.write(f"\n[task_runner error] {e}\n".encode())
        except Exception:
            pass
        _write_status(
            status_path,
            _status_payload(
                spec,
                "killed" if _terminating else "failed",
                rc,
                command_pid=command_pid,
                finished_at=finished,
            ),
        )
        return rc if _terminating and isinstance(rc, int) else 1


if __name__ == "__main__":
    raise SystemExit(main())
