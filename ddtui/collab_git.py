"""Small Git control messages and isolated, source-pinned task checkouts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from .collab_protocol import (
    CollabError, MAX_MESSAGE_BYTES, REQUEST_REF, allowed_path, encoded,
    identifier, relative_path, request_message, result_message,
    shared_text,
)


def repository_url(value: str) -> str:
    if not value or value.startswith("-") or "\n" in value or "\x00" in value or "::" in value:
        raise CollabError("Invalid Git repository URL")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "ssh", "file") or parsed.password or parsed.query or parsed.fragment:
            raise CollabError("Use HTTPS/SSH without inline credentials, or a local test repository")
        if parsed.scheme == "https" and parsed.username:
            raise CollabError("Use Git's credential helper; do not embed credentials in the URL")
    elif not re.fullmatch(r"[\w.-]+@[\w.-]+:[\w./~-]+", value):
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise CollabError("Local repository paths must be absolute")
        value = str(path.resolve())
    return value


def git(cwd: Path | None, *args: str, input=None, env=None, timeout=60, check=True):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    environment.update(env or {})
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", *args],
            cwd=cwd, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise CollabError("Git operation timed out; delivery remains pending") from error
    if check and result.returncode:
        raise CollabError(result.stderr.decode(errors="replace")[-2000:].strip() or "Git operation failed")
    return result


class GitTransport:
    def __init__(self, home: Path, binding: dict):
        self.home, self.binding = home, binding
        self.cache = home / "repos" / (identifier(binding["name"]) + ".git")
        self.url = repository_url(binding["url"])

    def ensure_cache(self):
        if not self.cache.exists():
            self.cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            git(None, "clone", "--bare", "--no-hardlinks", "--", self.url, str(self.cache))
        git(self.cache, "config", "core.hooksPath", "/dev/null")

    def fetch_ref(self, ref):
        self.ensure_cache()
        remote = git(self.cache, "ls-remote", "--refs", "origin", ref).stdout.decode().strip()
        if not remote:
            return None
        sha = remote.split()[0]
        local = git(self.cache, "rev-parse", "--verify", ref, check=False).stdout.decode().strip()
        if local != sha:
            git(self.cache, "fetch", "--no-tags", "origin", f"{ref}:{ref}")
        return sha

    def ensure_commit(self, sha):
        self.ensure_cache()
        if git(self.cache, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode:
            git(self.cache, "fetch", "--no-tags", "origin", sha)
        git(self.cache, "cat-file", "-e", f"{sha}^{{commit}}")

    def read_json(self, sha, path):
        size = int(git(self.cache, "cat-file", "-s", f"{sha}:{path}").stdout)
        if size > MAX_MESSAGE_BYTES:
            raise CollabError("Control message exceeds 128 KiB")
        try:
            return json.loads(git(self.cache, "show", f"{sha}:{path}").stdout)
        except (ValueError, UnicodeError) as error:
            raise CollabError("Invalid JSON control message") from error

    def requests(self):
        sha = self.fetch_ref(REQUEST_REF)
        if not sha:
            return []
        names = git(self.cache, "ls-tree", "-r", "--name-only", "-z", sha, "--", "collab/tasks").stdout
        paths = [p.decode() for p in names.split(b"\0") if p]
        if len(paths) > 512:
            raise CollabError("More than 512 active task files; retire completed work")
        requests = []
        for path in paths:
            if not re.fullmatch(r"collab/tasks/[A-Za-z0-9_-]+\.json", path):
                raise CollabError("Unexpected file in collab/tasks")
            request = request_message(self.read_json(sha, path))
            if path != f"collab/tasks/{request['task_id']}.json":
                raise CollabError("Task filename and task_id disagree")
            requests.append(request)
        return requests

    def write_control(self, ref, path, payload):
        """Normal fast-forward pushes; retries recognize already-published content."""
        self.ensure_cache()
        shared_text(encoded(payload))
        base = self.fetch_ref(ref)
        if base:
            old = git(self.cache, "show", f"{base}:{path}", check=False)
            if old.returncode == 0 and old.stdout == encoded(payload):
                return base
        else:
            base = git(self.cache, "rev-parse", "HEAD").stdout.decode().strip()
        fd, index = tempfile.mkstemp(prefix="collab-index-", dir=self.home)
        os.close(fd)
        os.unlink(index)
        environment = {"GIT_INDEX_FILE": index, "GIT_AUTHOR_NAME": "ddtui handoff",
                       "GIT_AUTHOR_EMAIL": "handoff@localhost", "GIT_COMMITTER_NAME": "ddtui handoff",
                       "GIT_COMMITTER_EMAIL": "handoff@localhost"}
        try:
            git(self.cache, "read-tree", base, env=environment)
            blob = git(self.cache, "hash-object", "-w", "--stdin", input=encoded(payload)).stdout.decode().strip()
            git(self.cache, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=environment)
            tree = git(self.cache, "write-tree", env=environment).stdout.decode().strip()
            commit = git(self.cache, "commit-tree", tree, "-p", base, "-m", "Update current handoff state",
                         env=environment).stdout.decode().strip()
            git(self.cache, "push", "origin", f"{commit}:{ref}")
            git(self.cache, "update-ref", ref, commit)
            return commit
        finally:
            Path(index).unlink(missing_ok=True)
            Path(index + ".lock").unlink(missing_ok=True)

    def publish_request(self, payload):
        request = request_message(payload)
        self.ensure_commit(request["source_commit"])
        current = self.fetch_ref(REQUEST_REF)
        if current:
            path = f"collab/tasks/{request['task_id']}.json"
            previous = git(self.cache, "show", f"{current}:{path}", check=False)
            if previous.returncode == 0:
                old = request_message(json.loads(previous.stdout))
                if request["revision"] < old["revision"] or (
                    request["revision"] == old["revision"] and request != old
                ):
                    raise CollabError("Published revisions are immutable; increment revision")
        return self.write_control(REQUEST_REF, f"collab/tasks/{request['task_id']}.json", request)

    def publish_receipt(self, payload):
        worker = identifier(self.binding["worker_id"])
        return self.write_control(f"refs/heads/codex/collab-{worker}",
                                  f"collab/results/{identifier(payload['task_id'])}.json", payload)

    def workspace(self, request):
        self.ensure_commit(request["source_commit"])
        directory = self.home / "workspaces" / self.binding["name"] / request["task_id"]
        branch = f"codex/{request['task_id']}-{self.binding['worker_id']}"
        if not directory.exists():
            directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            git(None, "clone", "--no-hardlinks", "--no-checkout", "--", str(self.cache), str(directory))
            git(directory, "config", "core.hooksPath", "/dev/null")
            git(directory, "remote", "set-url", "origin", self.url)
            git(directory, "checkout", "-b", branch, request["source_commit"])
            exclude = directory / ".git/info/exclude"
            with exclude.open("a") as stream:
                stream.write("\n.local/\n.ddtui-collab/\n")
        else:
            git(directory, "fetch", "--no-tags", str(self.cache), request["source_commit"])
            if git(directory, "symbolic-ref", "--short", "HEAD").stdout.decode().strip() != branch:
                raise CollabError("Task checkout changed branches; reconcile it before resuming")
            source = request["source_commit"]
            if git(directory, "merge-base", "--is-ancestor", source, "HEAD", check=False).returncode:
                if git(directory, "status", "--porcelain").stdout:
                    raise CollabError("New source requires a clean task checkout; preserve and commit current work first")
                git(directory, "merge", "--ff-only", source)
        # Reject known private trees even when a too-broad clean baseline was supplied.
        names = git(directory, "ls-tree", "-r", "--name-only", "-z", "HEAD").stdout
        for name in names.split(b"\0"):
            if name:
                relative_path(name.decode())
        return directory

    def prepare_result(self, request, workspace: Path, payload):
        head = git(workspace, "rev-parse", "HEAD").stdout.decode().strip()
        result = result_message(payload, request, head)
        if git(workspace, "status", "--porcelain").stdout:
            raise CollabError("Commit task source before publishing a result")
        git(workspace, "merge-base", "--is-ancestor", request["source_commit"], head)
        commits = git(workspace, "rev-list", f"{request['source_commit']}..{head}").stdout.decode().split()
        if len(commits) > 128:
            raise CollabError("Candidate exceeds 128 commits; review and publish a smaller source change")
        inspected, total = set(), 0
        for commit in commits:
            # All reachable candidate commits, including merged side branches,
            # must obey the task's file scope, not just the final tree diff.
            names = git(workspace, "diff-tree", "--root", "-m", "--no-renames", "--no-commit-id", "-r", "--name-only", "-z", commit).stdout
            for raw in names.split(b"\0"):
                if raw and not allowed_path(raw.decode(), request["paths"]):
                    raise CollabError("Candidate history changes files outside the task scope")
                if not raw:
                    continue
                entry = git(workspace, "ls-tree", commit, "--", raw.decode()).stdout
                if not entry:
                    continue
                mode, kind, blob = entry.split(b"\t", 1)[0].split()
                if mode not in (b"100644", b"100755") or kind != b"blob":
                    raise CollabError("Candidate must contain ordinary source files, not links or submodules")
                if blob in inspected:
                    continue
                inspected.add(blob)
                size = int(git(workspace, "cat-file", "-s", blob.decode()).stdout)
                total += size
                if size > 2 * 1024 * 1024 or total > 32 * 1024 * 1024:
                    raise CollabError("Candidate exceeds the small-source budget (2 MiB/file, 32 MiB total)")
                shared_text(git(workspace, "cat-file", "blob", blob.decode()).stdout)
        for path in result["evidence"]:
            evidence = (workspace / path).resolve()
            if not evidence.is_relative_to(workspace.resolve()) or not evidence.is_file():
                raise CollabError("Evidence must refer to an existing file inside the task checkout")
        return result

    def publish_candidate(self, request, workspace: Path, commit):
        git(workspace, "cat-file", "-e", f"{commit}^{{commit}}")
        branch = f"refs/heads/codex/{request['task_id']}-{self.binding['worker_id']}"
        advertised = git(workspace, "ls-remote", "--refs", "origin", branch).stdout.decode().strip()
        if advertised:
            tip = advertised.split()[0]
            if tip == commit:
                return
            git(workspace, "fetch", "--no-tags", "origin", tip)
            if not git(workspace, "merge-base", "--is-ancestor", commit, tip, check=False).returncode:
                # The exact queued candidate is already reachable. Do not move
                # a branch backwards after an ambiguous push or a later commit.
                return
        git(workspace, "push", "origin", f"{commit}:{branch}")
