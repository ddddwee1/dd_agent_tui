"""Plugin discovery and native MCP stdio use the existing handoff core."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from ddtui.collab import rpc
from tests.conftest import REPO_ROOT
from tests.test_collab import handoff, receive, acquire, owned
from tests.test_collab_coordinator import pair, worker_result


class Client:
    def __init__(self, home):
        env = dict(os.environ, DDTUI_COLLAB_HOME=str(home))
        self.proc = subprocess.Popen([sys.executable, str(REPO_ROOT / "plugins/git-collab/server/main.py")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, env=env)
        self.number = 0

    def call(self, method, params=None):
        self.number += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.number, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        value = json.loads(self.proc.stdout.readline())
        assert value["id"] == self.number and "error" not in value, value
        return value["result"]

    def tool(self, name, **args):
        value = self.call("tools/call", {"name": name, "arguments": {"binding": "local", **args}})
        assert not value.get("isError"), value
        return value["structuredContent"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.proc.stderr.close()


def test_plugin_manifests_and_stdio_protocol(tmp_path):
    plugin = REPO_ROOT / "plugins/git-collab"
    portable = json.loads((plugin / "plugin.json").read_text())
    legacy = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
    assert portable["name"] == legacy["name"] == "git-collab"
    assert json.loads((plugin / "mcp.json").read_text())["mcpServers"]["git_collab"]["type"] == "stdio"
    assert (plugin / "skills/git-collab/SKILL.md").is_file()
    client = Client(tmp_path)
    try:
        response = client.call("initialize", {"protocolVersion": "2025-06-18"})
        assert response["serverInfo"]["name"] == "git-collab"
        tools = client.call("tools/list")["tools"]
        assert {"collab_publish_task", "collab_get_result", "collab_codex_enable"} <= {t["name"] for t in tools}
        assert any(t["annotations"]["readOnlyHint"] for t in tools)
    finally:
        client.close()


def test_mcp_git_daemon_codex_and_remote_ack_closed_loop(pair):
    _, worker, request, remote, calls = pair
    with tempfile.TemporaryDirectory(prefix="gcc-", dir="/tmp") as folder:
        home = Path(folder)
        client = Client(home)
        lease = None
        try:
            client.call("initialize", {"protocolVersion": "2025-06-18"})
            client.tool("collab_bind", repo_url=str(remote), worker_id="planner", interval=5)
            client.tool("collab_publish_task", request=request)
            client.tool("collab_listening", enabled=True)
            deadline = time.monotonic() + 10
            while not client.tool("collab_list_tasks")["tasks"]:
                assert time.monotonic() < deadline
                time.sleep(0.1)
            client.tool("collab_codex_enable", task_id="op-a", max_turns=2)
            lease, task = acquire(worker)
            worker_result(worker, task, 1)
            deadline = time.monotonic() + 15
            while True:
                worker.tick(force=True)
                if worker.store.task("remote", "op-a")["result_ack_state"] == "processed":
                    break
                assert time.monotonic() < deadline, client.tool("collab_status")
                time.sleep(0.1)
            events = client.tool("collab_list_results", task_id="op-a")["results"]
            assert len(events) == 1 and events[0]["state"] == "processed"
            assert len(calls.read_text().splitlines()) == 1
            client.close()
            assert rpc(home, "ping")["pid"] > 0
            rpc(home, "shutdown")
        finally:
            if client.proc.poll() is None:
                client.close()
            try:
                rpc(home, "shutdown")
            except ValueError:
                pass
            if lease:
                lease.close()
                worker.dispatch(owned(task, "release"))
