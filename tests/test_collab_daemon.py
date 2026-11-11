"""A real separate daemon serves multiple CLI clients without a live TUI."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from ddtui.collab import rpc
from ddtui.collab_store import ClaimLock
from tests.test_collab import handoff


def eventually(function, timeout=10):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = function()
            if value:
                return value
        except (ValueError, OSError) as error:
            last = error
        time.sleep(0.05)
    raise AssertionError(f"condition did not become true: {last}")


def test_real_daemon_cli_release_and_recovery(handoff):
    _, _, _, remote = handoff
    with tempfile.TemporaryDirectory(prefix="dc-", dir="/tmp") as folder:
        home = Path(folder)
        daemon = subprocess.Popen([sys.executable, "-m", "ddtui.collab", "--home", folder, "serve"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        lease = None
        try:
            eventually(lambda: rpc(home, "ping", timeout=0.2))
            rpc(home, "bind", binding="remote", url=str(remote), worker_id="dt32", interval=5)
            # Binding another TUI does not silently change worker identity.
            assert rpc(home, "bind", binding="remote", url=str(remote))["worker_id"] == "dt32"
            assert rpc(home, "tasks", binding="remote") == []
            rpc(home, "on", binding="remote")
            eventually(lambda: rpc(home, "tasks", binding="remote"))
            lease = ClaimLock(home, "remote", "op-a").acquire()
            task = rpc(home, "claim", binding="remote", task_id="op-a", owner="tui-test", pid=os.getpid(),
                       lease_id=lease.lease_id)
            result = subprocess.run([sys.executable, "-m", "ddtui.collab", "--home", folder,
                                     "--binding", "remote", "task", "release", "op-a"],
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 1 and "occupied" in result.stderr
            rpc(home, "pause", binding="remote", task_id="op-a", token=task["token"])
            lease.close()
            result = subprocess.run([sys.executable, "-m", "ddtui.collab", "--home", folder,
                                     "--binding", "remote", "task", "release", "op-a"],
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout)["state"] == "paused"
            assert rpc(home, "ping")["pid"] == daemon.pid
            # Killing the bridge itself preserves state; no running task is replayed.
            daemon.terminate()
            daemon.wait(timeout=5)
            daemon.stderr.close()
            daemon = subprocess.Popen([sys.executable, "-m", "ddtui.collab", "--home", folder, "serve"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            eventually(lambda: rpc(home, "ping", timeout=0.2))
            assert rpc(home, "task", binding="remote", task_id="op-a")["owner"] is None
            assert rpc(home, "task", binding="remote", task_id="op-a")["state"] == "paused"
            rpc(home, "shutdown")
            daemon.wait(timeout=5)
        finally:
            if lease:
                lease.close()
            if daemon.poll() is None:
                daemon.terminate()
                daemon.wait(timeout=5)
            daemon.stderr.close()


def test_task_runner_keeps_claim_after_session_closes(handoff, monkeypatch, tmp_path):
    from ddtui.state import ToolContext
    import ddtui.tools_tasks as tasks
    import ddtui.runtime_state as runtime

    service, _, _, _ = handoff
    service.store.listening("remote", True)
    service.tick(force=True)
    lease = ClaimLock(service.home, "remote", "op-a").acquire()
    task = service.dispatch({"action": "claim", "binding": "remote", "task_id": "op-a",
                             "owner": "session", "pid": os.getpid(), "lease_id": lease.lease_id})
    monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(tasks, "TASK_OUTPUT_DIR", tmp_path / "logs")
    ctx = ToolContext(work_dir=task["work_dir"], session_id="fixture", collab_lock_fd=lease.fd)
    response = tasks.tool_task_start(ctx, f"{sys.executable} -c 'import time; time.sleep(30)'", notice_time=0)
    assert not response.startswith("Error"), response
    runner = next(iter(ctx.tasks.values()))
    try:
        lease.close()
        try:
            service.dispatch({"action": "release", "binding": "remote", "task_id": "op-a"})
        except ValueError as error:
            assert "occupied" in str(error)
        else:
            raise AssertionError("surviving task runner must keep the claim occupied")
    finally:
        os.killpg(runner.proc.pid, signal.SIGTERM)
        runner.proc.wait(timeout=5)
    assert service.dispatch({"action": "release", "binding": "remote", "task_id": "op-a"})["state"] == "paused"
