"""Coordinator receive/continue/ack loop over real local Git repositories."""

import json
from pathlib import Path
import sys
import time

import pytest

from ddtui.collab import HandoffService
from ddtui.collab_git import GitTransport, git
from ddtui.collab_protocol import CollabError
from ddtui.collab_store import ClaimLock
from tests.test_collab import handoff, receive, acquire, owned, commit


@pytest.fixture
def pair(handoff, tmp_path, monkeypatch):
    worker, _, request, remote = handoff
    coordinator = HandoffService(tmp_path / "coordinator")
    coordinator.dispatch({"action": "bind", "binding": "local", "url": str(remote), "worker_id": "planner", "role": "coordinator"})
    coordinator.dispatch({"action": "on", "binding": "local"})
    receive(worker)
    coordinator.tick(force=True)
    fake = tmp_path / "fake-codex"
    fake.write_text(f'''#!{sys.executable}
import json, os, pathlib, sys
data = json.loads(sys.stdin.read().split("\\nHANDOFF_DATA_JSON\\n", 1)[1])
request = data["authorized_task"]
action = os.environ.get("FAKE_CODEX_ACTION", "await")
if action == "two-stage":
    action = "request" if request["revision"] == 1 else "close"
output = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output.write_text(json.dumps({{"task_id":request["task_id"],"revision":request["revision"],"action":action,
                            "summary":"fixture review","instructions":"Verify the next bounded case."}}))
with open(os.environ["FAKE_CODEX_CALLS"],"a") as stream:
    stream.write(json.dumps({{"revision":request["revision"],"resume":"resume" in sys.argv}})+"\\n")
print(json.dumps({{"type":"thread.started","thread_id":"fixture-thread"}}))
print(json.dumps({{"type":"turn.started"}}))
if not os.environ.get("FAKE_CODEX_NO_COMPLETION"):
    print(json.dumps({{"type":"turn.completed"}}))
''')
    fake.chmod(0o700)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("DDTUI_CODEX_BIN", str(fake))
    monkeypatch.setenv("FAKE_CODEX_CALLS", str(calls))
    return coordinator, worker, request, remote, calls


def worker_result(worker, task, revision):
    request = worker.store.task("remote", task["task_id"])["request"]
    worker.dispatch(owned(task, "admit", revision=revision))
    directory = Path(task["work_dir"])
    (directory / "src/kernel.py").write_text(f"value = {revision + 1}\n")
    sha = commit(directory, f"candidate {revision}")
    proof = directory / ".local/proof.json"
    proof.parent.mkdir(exist_ok=True)
    proof.write_text('{"passed":true}')
    output = directory / ".ddtui-collab/result.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({"task_id": task["task_id"], "revision": revision, "status": "completed",
                                 "summary": "Control-plane fixture passed", "evidence": [".local/proof.json"],
                                 "metrics": {"passed_cases": 1}}))
    worker.dispatch(owned(task, "turn_done"))
    worker.tick(force=True)
    return sha


def pump(coordinator, condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        coordinator.tick(force=True)
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(coordinator.coordinator.routes())


def test_result_delivery_is_deduplicated_and_acknowledged(pair):
    coordinator, worker, _, remote, _ = pair
    lock, task = acquire(worker)
    try:
        worker_result(worker, task, 1)
        coordinator.tick(force=True)
        events = coordinator.dispatch({"action": "results", "binding": "local"})
        assert len(events) == 1 and events[0]["state"] == "received"
        # Different receipt state commits containing the same result are not new events.
        worker.dispatch(owned(task, "pause"))
        worker.tick(force=True)
        coordinator.tick(force=True)
        assert len(coordinator.coordinator.events("local")) == 1
        worker.tick(force=True)
        assert worker.store.task("remote", "op-a")["result_ack_state"] == "received"
        coordinator.dispatch({"action": "ack", "binding": "local", "event_id": events[0]["event_id"]})
        coordinator.tick(force=True)
        worker.tick(force=True)
        assert worker.store.task("remote", "op-a")["result_ack_state"] == "processed"
    finally:
        lock.close()
        worker.dispatch(owned(task, "release"))


def test_auto_continuation_preserves_scope_and_resumes_same_codex_thread(pair, monkeypatch):
    coordinator, worker, request, remote, calls = pair
    monkeypatch.setenv("FAKE_CODEX_ACTION", "two-stage")
    coordinator.dispatch({"action": "codex_configure", "binding": "local", "task_id": "op-a", "max_turns": 3})
    lock, task = acquire(worker)
    try:
        first = worker_result(worker, task, 1)
        pump(coordinator, lambda: coordinator.coordinator.request("local", "op-a")["revision"] == 2)
        next_request = coordinator.coordinator.request("local", "op-a")
        assert next_request["source_commit"] == first
        assert next_request["worker_id"] == request["worker_id"] and next_request["paths"] == request["paths"]
        assert request["instructions"] in next_request["instructions"]
        worker.tick(force=True)
        worker_result(worker, task, 2)
        pump(coordinator, lambda: coordinator.coordinator.request("local", "op-a")["state"] == "retired")
        rows = [json.loads(line) for line in calls.read_text().splitlines()]
        assert rows == [{"revision": 1, "resume": False}, {"revision": 2, "resume": True}]
        assert coordinator.coordinator.route("local", "op-a")["thread_id"] == "fixture-thread"
        for _ in range(2):
            coordinator.tick(force=True)
        assert len(calls.read_text().splitlines()) == 2
    finally:
        lock.close()
        worker.dispatch(owned(task, "release"))


def test_delivery_retry_uses_saved_decision_without_another_codex_turn(pair, monkeypatch):
    coordinator, worker, _, _, calls = pair
    monkeypatch.setenv("FAKE_CODEX_ACTION", "request")
    coordinator.dispatch({"action": "codex_configure", "binding": "local", "task_id": "op-a", "max_turns": 3})
    lock, task = acquire(worker)
    original = GitTransport.publish_request

    def unavailable(transport, request):
        if request["revision"] == 2:
            raise CollabError("offline")
        return original(transport, request)

    try:
        worker_result(worker, task, 1)
        monkeypatch.setattr(GitTransport, "publish_request", unavailable)
        pump(coordinator, lambda: bool(coordinator.coordinator.events("local")) and
             coordinator.coordinator.events("local")[0]["state"] == "decision_ready")
        coordinator.tick(force=True)
        assert len(calls.read_text().splitlines()) == 1
        monkeypatch.setattr(GitTransport, "publish_request", original)
        restarted = HandoffService(coordinator.home)
        pump(restarted, lambda: restarted.coordinator.request("local", "op-a")["revision"] == 2)
        assert len(calls.read_text().splitlines()) == 1
    finally:
        lock.close()
        worker.dispatch(owned(task, "release"))


def test_pause_and_resume_delivers_saved_decision_without_resetting_budget(pair, monkeypatch):
    coordinator, worker, _, _, calls = pair
    monkeypatch.setenv("FAKE_CODEX_ACTION", "request")
    coordinator.dispatch({"action": "codex_configure", "binding": "local", "task_id": "op-a", "max_turns": 3})
    lock, task = acquire(worker)
    try:
        worker_result(worker, task, 1)
        coordinator.tick(force=True)
        coordinator.dispatch({"action": "codex_pause", "binding": "local", "task_id": "op-a"})
        pump(coordinator, lambda: coordinator.coordinator.events("local")[0]["state"] == "decision_ready")
        assert coordinator.coordinator.request("local", "op-a")["revision"] == 1
        remaining = coordinator.coordinator.route("local", "op-a")["remaining"]
        coordinator.dispatch({"action": "codex_resume", "binding": "local", "task_id": "op-a"})
        pump(coordinator, lambda: coordinator.coordinator.request("local", "op-a")["revision"] == 2)
        assert coordinator.coordinator.route("local", "op-a")["remaining"] == remaining
        assert len(calls.read_text().splitlines()) == 1
    finally:
        lock.close()
        worker.dispatch(owned(task, "release"))


def test_invalid_source_receipt_never_reaches_codex(pair):
    coordinator, worker, _, _, calls = pair
    lock, task = acquire(worker)
    try:
        worker_result(worker, task, 1)
        result = worker.store.task("remote", "op-a")["result"]
        forged = dict(result, source_commit="0" * 40)
        GitTransport(worker.home, worker.store.binding("remote")).publish_receipt(
            {"schema_version": 1, "task_id": "op-a", "worker_id": "dt32", "result": forged})
        coordinator.dispatch({"action": "codex_configure", "binding": "local", "task_id": "op-a"})
        coordinator.tick(force=True)
        assert not coordinator.coordinator.events("local")
        assert "different source" in coordinator.store.binding("local")["last_error"]
        assert not calls.exists()
    finally:
        lock.close()
        worker.dispatch(owned(task, "release"))


def test_invalid_task_result_does_not_block_other_tasks(pair):
    coordinator, worker, request, _, _ = pair
    second = dict(request, task_id="op-b")
    coordinator.dispatch({"action": "publish", "binding": "local", "request": second})
    worker.tick(force=True)
    locks = []
    try:
        for task_id, owner in (("op-a", "session-a"), ("op-b", "session-b")):
            lock, task = acquire(worker, task_id, owner)
            locks.append((lock, task))
            worker_result(worker, task, 1)
        bad = worker.store.task("remote", "op-a")["result"]
        GitTransport(worker.home, worker.store.binding("remote")).publish_receipt(
            {"schema_version": 1, "task_id": "op-a", "worker_id": "dt32",
             "result": dict(bad, source_commit="0" * 40)})
        coordinator.tick(force=True)
        assert [e["task_id"] for e in coordinator.coordinator.events("local")] == ["op-b"]
        assert "op-a" in coordinator.store.binding("local")["last_error"]
        with pytest.raises(CollabError, match="fixed role"):
            coordinator.dispatch({"action": "bind", "binding": "local", "url": coordinator.store.binding("local")["url"],
                                  "worker_id": "planner", "role": "worker"})
    finally:
        for lock, task in locks:
            lock.close()
            worker.dispatch(owned(task, "release"))


def test_restart_during_unknown_turn_never_automatically_replays(pair):
    coordinator, worker, _, _, calls = pair
    lock, task = acquire(worker)
    try:
        worker_result(worker, task, 1)
        coordinator.tick(force=True)
        event = coordinator.coordinator.events("local")[0]
        coordinator.coordinator.configure("local", "op-a")
        assert coordinator.coordinator.start("local", event["event_id"])
        restarted = HandoffService(coordinator.home)
        restarted.tick(force=True)
        assert restarted.coordinator.event("local", event["event_id"])["state"] == "needs_attention"
        assert not calls.exists()
    finally:
        lock.close()
        worker.dispatch(owned(task, "release"))


def test_exit_zero_without_turn_completion_is_not_success_and_never_replayed(pair, monkeypatch):
    coordinator, worker, _, _, calls = pair
    monkeypatch.setenv("FAKE_CODEX_NO_COMPLETION", "1")
    coordinator.dispatch({"action": "codex_configure", "binding": "local", "task_id": "op-a"})
    lock, task = acquire(worker)
    try:
        worker_result(worker, task, 1)
        pump(coordinator, lambda: bool(coordinator.coordinator.events("local")) and
             coordinator.coordinator.events("local")[0]["state"] == "needs_attention")
        restarted = HandoffService(coordinator.home)
        for _ in range(2):
            restarted.tick(force=True)
        assert len(calls.read_text().splitlines()) == 1
        event = restarted.coordinator.events("local")[0]
        with pytest.raises(CollabError, match="Confirm"):
            restarted.dispatch({"action": "codex_retry", "binding": "local", "event_id": event["event_id"]})
        restarted.dispatch({"action": "ack", "binding": "local", "event_id": event["event_id"]})
    finally:
        lock.close()
        worker.dispatch(owned(task, "release"))


@pytest.mark.parametrize("mode", ["blocked-input", "missing-output"])
def test_codex_input_timeout_and_stale_output_are_not_success(pair, monkeypatch, tmp_path, mode):
    coordinator, worker, _, _, _ = pair
    lock, task = acquire(worker)
    try:
        worker_result(worker, task, 1)
        coordinator.tick(force=True)
        event = coordinator.coordinator.events("local")[0]
        route = coordinator.coordinator.configure("local", "op-a")
        assert coordinator.coordinator.start("local", event["event_id"])
        fake = tmp_path / "non-reading-codex"
        fake.write_text(f'''#!{sys.executable}
import json, sys, time
if {mode!r} == "blocked-input":
    time.sleep(30)
else:
    sys.stdin.read()
    for kind in ("thread.started", "turn.started", "turn.completed"):
        print(json.dumps({{"type":kind,"thread_id":"fixture-thread"}}))
''')
        fake.chmod(0o700)
        monkeypatch.setenv("DDTUI_CODEX_BIN", str(fake))
        directory = coordinator.home / "codex/local/op-a" / event["event_id"]
        directory.mkdir(parents=True)
        (directory / "decision.json").write_text(json.dumps({"task_id": "op-a", "revision": 1,
            "action": "close", "summary": "stale", "instructions": ""}))
        # Exceed the pipe buffer while keeping the protocol's 128 KiB input limit.
        event["request"]["instructions"] = "x" * 100000
        route["timeout"] = 0.3 if mode == "blocked-input" else 2
        started = time.monotonic()
        coordinator.codex.run(route, event, ClaimLock(coordinator.home, "local", "op-a").acquire())
        assert time.monotonic() - started < 5
        outcome = coordinator.coordinator.event("local", event["event_id"])
        assert outcome["state"] == "needs_attention"
        expected = "budget" if mode == "blocked-input" else "structured decision"
        assert expected in outcome["error"], outcome["error"]
    finally:
        lock.close()
        worker.dispatch(owned(task, "release"))

def test_recent_results_and_pending_work_remain_visible_after_many_revisions(pair):
    coordinator, _, request, _, _ = pair
    for revision in range(1, 131):
        current = dict(request, revision=revision)
        result = {"revision": revision, "candidate_commit": request["source_commit"]}
        coordinator.coordinator.ingest("local", current, {"result": result, "receipt_commit": request["source_commit"]})
        event = coordinator.coordinator.events("local", "op-a")[0]
        coordinator.coordinator.acknowledge("local", event["event_id"])
    newest = coordinator.coordinator.events("local", "op-a")[0]
    assert newest["revision"] == 130
    with coordinator.store.db() as db:
        db.execute("UPDATE result_inbox SET state='received' WHERE revision=1")
    assert [e["revision"] for e in coordinator.coordinator.events("local", "op-a", state="received")] == [1]
