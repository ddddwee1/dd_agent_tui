"""Offline handoff integration: real Git, durable state, and OS claim locks."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ddtui.collab import HandoffService
from ddtui.collab_git import GitTransport, git, repository_url
from ddtui.collab_protocol import CollabError, request_message, result_message, shared_text
from ddtui.collab_store import ClaimLock


def commit(directory, message="source"):
    git(directory, "add", "--all")
    git(directory, "-c", "user.name=fixture", "-c", "user.email=fixture@localhost", "commit", "-m", message)
    return git(directory, "rev-parse", "HEAD").stdout.decode().strip()


@pytest.fixture
def handoff(tmp_path):
    remote, seed, home = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "state"
    git(None, "init", "--bare", "--initial-branch=main", str(remote))
    git(None, "init", "--initial-branch=main", str(seed))
    (seed / "src").mkdir()
    (seed / "src/kernel.py").write_text("value = 1\n")
    (seed / "AGENTS.md").write_text("Do not access real devices in tests.\n")
    sha = commit(seed)
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "main")
    service = HandoffService(home)
    config = service.dispatch({"action": "bind", "binding": "remote", "url": str(remote), "worker_id": "dt32"})
    transport = GitTransport(home, config)
    request = {"schema_version": 1, "task_id": "op-a", "worker_id": "dt32", "revision": 1,
               "source_commit": sha, "paths": ["src"], "instructions": "Review and improve the source."}
    transport.publish_request(request)
    return service, transport, request, remote


def receive(service):
    service.dispatch({"action": "on", "binding": "remote"})
    service.tick(force=True)


def acquire(service, task_id="op-a", owner="session-a"):
    lock = ClaimLock(service.home, "remote", task_id).acquire()
    try:
        task = service.dispatch({"action": "claim", "binding": "remote", "task_id": task_id,
                                 "owner": owner, "pid": os.getpid(), "lease_id": lock.lease_id})
    except BaseException:
        lock.close()
        raise
    return lock, task


def owned(task, action, **kwargs):
    return {"action": action, "binding": "remote", "task_id": task["task_id"], "token": task["token"], **kwargs}


@pytest.mark.parametrize("path", ["../secret", "/tmp/secret", "src/../secret", "private/key", ".ssh/id", "src/.env"])
def test_request_rejects_private_or_escaping_paths(handoff, path):
    request = dict(handoff[2], paths=[path])
    with pytest.raises(CollabError):
        request_message(request)


def test_url_uses_credential_helpers():
    for value in ("https://user:secret@example.test/repo", "ext::run-command", "--upload-pack=bad"):
        with pytest.raises(CollabError):
            repository_url(value)
    assert repository_url("git@example.test:team/clean.git") == "git@example.test:team/clean.git"


def test_shared_text_checks_do_not_confuse_code_or_relative_paths_with_secrets():
    shared_text(b'api_key=load_deepseek_api_key(); path="tests/data/cases"')
    for content in (b'path="/home/private-user/source"', b'-----BEGIN RSA PRIVATE KEY-----',
                    b'access_token="abcdefghijklmnopqrstuv"', b'abc\x00def'):
        with pytest.raises(CollabError):
            shared_text(content)


def test_opt_in_receive_immutable_revision_and_worker_routing(handoff):
    service, transport, request, _ = handoff
    service.tick(force=True)
    assert service.store.tasks("remote") == []
    receive(service)
    task = service.store.task("remote", "op-a")
    assert task["state"] == "received" and task["owner"] is None
    assert not service.store.ingest("remote", request)
    assert not service.store.ingest("remote", dict(request, task_id="other", worker_id="another-worker"))
    with pytest.raises(CollabError, match="immutable"):
        transport.publish_request(dict(request, instructions="different"))
    transport.publish_request(dict(request, revision=2, instructions="next revision"))
    service.dispatch({"action": "off", "binding": "remote"})
    service.tick(force=True)
    assert service.store.task("remote", "op-a")["revision"] == 1
    receive(service)
    assert service.store.task("remote", "op-a")["revision"] == 2


def test_one_owner_per_task_one_task_per_session_and_stale_release(handoff):
    service, transport, request, _ = handoff
    transport.publish_request(dict(request, task_id="op-b"))
    receive(service)
    lock, task = acquire(service)
    try:
        with pytest.raises(CollabError, match="occupied"):
            ClaimLock(service.home, "remote", "op-a").acquire()
        with pytest.raises(CollabError, match="only one"):
            acquire(service, "op-b", "session-a")
        other_lock, other = acquire(service, "op-b", "session-b")
        other_lock.close()
        service.dispatch(owned(other, "release"))
    finally:
        lock.close()
    service.dispatch(owned(task, "release"))
    assert service.dispatch(owned(task, "release"))["state"] == "paused"
    newer_lock, newer = acquire(service, owner="session-new")
    newer_lock.close()
    try:
        with pytest.raises(CollabError, match="Stale"):
            service.dispatch(owned(task, "release"))
        assert service.store.task("remote", "op-a")["token"] == newer["token"]
    finally:
        service.dispatch(owned(newer, "release"))


def test_surviving_child_blocks_manual_release(handoff):
    service, _, _, _ = handoff
    receive(service)
    lock, task = acquire(service)
    child = subprocess.Popen([sys.executable, "-c", "import sys,time;print('ready',flush=True);time.sleep(30)"],
                             pass_fds=(lock.fd,), stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        lock.close()
        with pytest.raises(CollabError, match="occupied"):
            service.dispatch(owned(task, "release"))
    finally:
        child.terminate()
        child.wait(timeout=5)
        child.stdout.close()
    assert service.dispatch(owned(task, "release"))["state"] == "paused"


def test_restart_retains_outbox_and_does_not_execute_or_reclaim(handoff, monkeypatch):
    service, _, _, _ = handoff
    receive(service)
    lock, task = acquire(service)
    lock.close()
    service.dispatch(owned(task, "release"))
    calls = []
    original = GitTransport.publish_receipt

    def unavailable(self, payload):
        calls.append(payload)
        raise CollabError("offline")

    monkeypatch.setattr(GitTransport, "publish_receipt", unavailable)
    service.tick(force=True)
    assert service.store.pending()[0]["error"] == "offline"
    restarted = HandoffService(service.home)
    assert restarted.store.task("remote", "op-a")["state"] == "paused"
    monkeypatch.setattr(GitTransport, "publish_receipt", original)
    restarted.tick(force=True)
    assert restarted.store.pending() == []
    assert calls[0]["released"] is True


def test_result_is_frozen_and_published_after_pause(handoff):
    service, transport, request, remote = handoff
    receive(service)
    lock, task = acquire(service)
    service.dispatch(owned(task, "admit", revision=1))
    workspace = Path(task["work_dir"])
    (workspace / "src/kernel.py").write_text("value = 2\n")
    candidate = commit(workspace, "candidate")
    proof = workspace / ".local/proof.json"
    proof.parent.mkdir()
    proof.write_text('{"passed":true}')
    manifest = workspace / ".ddtui-collab/result.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"task_id": "op-a", "revision": 1, "status": "completed",
                                    "summary": "Independent check passed", "evidence": [".local/proof.json"],
                                    "metrics": {"passed_cases": 1, "elapsed_us": 2.3}}))
    assert service.dispatch(owned(task, "turn_done"))["state"] == "publishing"
    # A subsequent local edit cannot change the already queued candidate identity.
    (workspace / "src/kernel.py").write_text("value = 3\n")
    lock.close()
    service.dispatch(owned(task, "release"))
    service.dispatch({"action": "off", "binding": "remote"})
    HandoffService(service.home).tick(force=True)
    assert git(remote, "rev-parse", "refs/heads/codex/op-a-dt32").stdout.decode().strip() == candidate
    receipt = json.loads(git(remote, "show", "refs/heads/codex/collab-dt32:collab/results/op-a.json").stdout)
    assert receipt["state"] == "paused"
    assert receipt["result"]["candidate_commit"] == candidate
    assert receipt["result"]["metrics"]["passed_cases"] == 1
    assert receipt["result"]["validation"] == "executor_report_requires_review"
    assert service.store.publications() == []


def test_candidate_history_scope_checked_even_if_file_removed(handoff):
    service, transport, request, _ = handoff
    receive(service)
    lock, task = acquire(service)
    try:
        workspace = Path(task["work_dir"])
        (workspace / "unrelated.txt").write_text("not task source")
        commit(workspace, "out of scope")
        (workspace / "unrelated.txt").unlink()
        commit(workspace, "remove")
        with pytest.raises(CollabError, match="outside"):
            transport.prepare_result(request_message(request), workspace,
                                     {"task_id": "op-a", "revision": 1, "status": "blocked",
                                      "summary": "Needs review", "evidence": []})
    finally:
        lock.close()
        service.dispatch(owned(task, "release"))


def test_ordinary_turn_is_not_task_completion(handoff):
    service, _, request, _ = handoff
    receive(service)
    lock, task = acquire(service)
    try:
        service.dispatch(owned(task, "admit", revision=1))
        service.dispatch(owned(task, "turn_done"))
        assert service.store.task("remote", "op-a")["result"] is None
        assert service.store.task("remote", "op-a")["state"] == "awaiting_input"
        with pytest.raises(CollabError, match="evidence"):
            result_message({"task_id": "op-a", "revision": 1, "status": "completed", "summary": "done"},
                           request, request["source_commit"])
    finally:
        lock.close()
        service.dispatch(owned(task, "release"))


def test_new_request_can_use_published_candidate_without_overwriting_workspace(handoff):
    service, transport, request, _ = handoff
    receive(service)
    lock, task = acquire(service)
    try:
        service.dispatch(owned(task, "admit", revision=1))
        workspace = Path(task["work_dir"])
        (workspace / "src/kernel.py").write_text("value = 2\n")
        candidate = commit(workspace, "candidate")
        transport.publish_candidate(request_message(request), workspace, candidate)
        transport.publish_request(dict(request, revision=2, source_commit=candidate, instructions="next stage"))
        receive(service)
        admitted = service.dispatch(owned(task, "admit", revision=2))
        assert admitted["active_request"]["source_commit"] == candidate
        assert (workspace / "src/kernel.py").read_text() == "value = 2\n"
        assert admitted["delivered_revision"] == 2
    finally:
        lock.close()
        service.dispatch(owned(task, "release"))


def test_disconnected_claim_is_rejected_after_source_preparation(handoff, monkeypatch):
    service, _, _, _ = handoff
    receive(service)
    lease = ClaimLock(service.home, "remote", "op-a").acquire()
    original = GitTransport.workspace

    def disconnected(transport, request):
        workspace = original(transport, request)
        lease.close()
        return workspace

    monkeypatch.setattr(GitTransport, "workspace", disconnected)
    with pytest.raises(CollabError, match="no longer holds"):
        service.dispatch({"action": "claim", "binding": "remote", "task_id": "op-a", "owner": "cancelled",
                          "pid": os.getpid(), "lease_id": lease.lease_id})
    assert service.store.task("remote", "op-a")["owner"] is None
