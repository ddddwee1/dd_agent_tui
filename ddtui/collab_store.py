"""Local durable inbox, outbox and fenced task ownership.

Claim locks belong to the executing TUI and are inherited by tool runners.
Closing the TUI's descriptor does not unlock a still-running child.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .collab_protocol import CollabError, encoded, identifier, request_message


def collab_home() -> Path:
    return Path(os.environ.get("DDTUI_COLLAB_HOME", "~/.ddtui/collab")).expanduser().resolve()


class ClaimLock:
    def __init__(self, home: Path, binding: str, task_id: str):
        directory = home / "locks" / identifier(binding)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / (identifier(task_id) + ".lock")
        self.fd: int | None = None
        self.lease_id = uuid.uuid4().hex

    def acquire(self):
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            raise CollabError("Task is occupied by a session or a surviving tool process") from error
        self.fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, self.lease_id.encode())
        return self

    def verify(self, lease_id):
        """Reject an RPC whose client disappeared while Git was preparing source."""
        fd = os.open(self.path, os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if not lease_id or os.read(fd, 128).decode() != lease_id:
                    raise CollabError("Claim lease changed before admission")
            else:
                raise CollabError("Executing session no longer holds the task lease")
        finally:
            os.close(fd)

    def close(self):
        if self.fd is not None:
            # LOCK_UN would also unlock inherited descriptors in child processes.
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()


class Store:
    def __init__(self, home: Path):
        self.home = home.resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.home.chmod(0o700)
        self.path = self.home / "state.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS bindings (
                    name TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, worker_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0, interval REAL NOT NULL DEFAULT 15,
                    last_poll REAL, last_error TEXT, last_attempt REAL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    binding TEXT NOT NULL, task_id TEXT NOT NULL, request TEXT NOT NULL,
                    revision INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'received',
                    owner TEXT, owner_pid INTEGER, token TEXT, generation INTEGER NOT NULL DEFAULT 0,
                    active_request TEXT, delivered_revision INTEGER NOT NULL DEFAULT 0,
                    work_dir TEXT, history_name TEXT, activity TEXT NOT NULL DEFAULT 'idle',
                    children TEXT NOT NULL DEFAULT '[]', result TEXT, updated REAL NOT NULL,
                    PRIMARY KEY(binding, task_id)
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    binding TEXT NOT NULL, task_id TEXT NOT NULL, payload TEXT NOT NULL,
                    version TEXT NOT NULL, error TEXT, PRIMARY KEY(binding, task_id)
                );
                CREATE TABLE IF NOT EXISTS publications (
                    binding TEXT NOT NULL, task_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, error TEXT,
                    PRIMARY KEY(binding, task_id, revision)
                );
            """)
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def bind(self, name, url, worker_id, interval=15):
        identifier(name)
        identifier(worker_id)
        if not isinstance(interval, (int, float)) or not 5 <= interval <= 3600:
            raise CollabError("Poll interval must be between 5 and 3600 seconds")
        with self.db() as db:
            old = db.execute("SELECT * FROM bindings WHERE name=?", (name,)).fetchone()
            if old and (old["url"] != url or old["worker_id"] != worker_id):
                raise CollabError("A binding has a fixed repository and worker; use another binding name")
            try:
                db.execute("INSERT INTO bindings(name,url,worker_id,interval) VALUES(?,?,?,?) "
                           "ON CONFLICT(name) DO UPDATE SET interval=excluded.interval",
                           (name, url, worker_id, interval))
            except sqlite3.IntegrityError as error:
                raise CollabError("This repository already has a local binding") from error
        return self.binding(name)

    def binding(self, name):
        with self.db() as db:
            row = db.execute("SELECT * FROM bindings WHERE name=?", (identifier(name),)).fetchone()
        if not row:
            raise CollabError("Unknown binding; use /collab bind or ddtui-collab bind first")
        return dict(row)

    def bindings(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM bindings ORDER BY name")]

    def listening(self, name, enabled):
        self.binding(name)
        with self.db() as db:
            db.execute("UPDATE bindings SET enabled=? WHERE name=?", (int(enabled), name))
        return self.binding(name)

    def poll_status(self, name, error=None):
        with self.db() as db:
            db.execute("UPDATE bindings SET last_attempt=?,last_error=?,last_poll=CASE WHEN ? IS NULL "
                       "THEN ? ELSE last_poll END WHERE name=?", (time.time(), error, error, time.time(), name))

    @staticmethod
    def _task(row):
        if row is None:
            raise CollabError("Unknown task")
        value = dict(row)
        for key in ("request", "active_request", "children", "result"):
            value[key] = json.loads(value[key]) if value[key] else None
        return value

    def task(self, binding, task_id):
        with self.db() as db:
            return self._task(db.execute("SELECT * FROM tasks WHERE binding=? AND task_id=?",
                                         (binding, identifier(task_id))).fetchone())

    def tasks(self, binding):
        with self.db() as db:
            rows = db.execute("SELECT * FROM tasks WHERE binding=? ORDER BY updated,task_id", (binding,))
            tasks = [self._task(row) for row in rows]
        return sorted(tasks, key=lambda task: (-task["request"]["priority"], task["updated"]))

    def _receipt(self, db, task, state, **extra):
        result = task.get("result")
        if isinstance(result, str):
            result = json.loads(result)
        active = task.get("active_request")
        if isinstance(active, str):
            active = json.loads(active)
        payload = {"schema_version": 1, "task_id": task["task_id"], "revision": task["revision"],
                   "worker_id": db.execute("SELECT worker_id FROM bindings WHERE name=?",
                                           (task["binding"],)).fetchone()[0],
                   "state": state, "generation": task["generation"],
                   "executing_revision": active["revision"] if active else None,
                   **({"result": result} if result else {}), **extra}
        db.execute("INSERT INTO outbox(binding,task_id,payload,version) VALUES(?,?,?,?) "
                   "ON CONFLICT(binding,task_id) DO UPDATE SET payload=excluded.payload,"
                   "version=excluded.version,error=NULL",
                   (task["binding"], task["task_id"], encoded(payload).decode(), uuid.uuid4().hex))

    def ingest(self, binding, payload):
        request = request_message(payload)
        config = self.binding(binding)
        if request["worker_id"] != config["worker_id"]:
            return False
        with self.db() as db:
            row = db.execute("SELECT * FROM tasks WHERE binding=? AND task_id=?",
                             (binding, request["task_id"])).fetchone()
            if row and request["revision"] <= row["revision"]:
                if request["revision"] == row["revision"] and json.loads(row["request"]) != request:
                    raise CollabError("A published task revision is immutable; increment revision")
                return False
            state = "retired" if request["state"] == "retired" else "received"
            if row and row["owner"]:
                state = row["state"]
            elif row and row["state"] == "paused" and state != "retired":
                state = "paused"
            db.execute("INSERT INTO tasks(binding,task_id,request,revision,state,updated) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(binding,task_id) DO UPDATE SET request=excluded.request,"
                       "revision=excluded.revision,state=excluded.state,updated=excluded.updated",
                       (binding, request["task_id"], encoded(request).decode(), request["revision"], state, time.time()))
            task = dict(db.execute("SELECT * FROM tasks WHERE binding=? AND task_id=?",
                                   (binding, request["task_id"])).fetchone())
            self._receipt(db, task, "received", requested_state=request["state"])
        return True

    def claim(self, binding, task_id, owner, pid, work_dir):
        identifier(owner)
        if type(pid) is not int or pid <= 0:
            raise CollabError("An executing session needs a valid local process ID")
        with self.db() as db:
            task = self._task(db.execute("SELECT * FROM tasks WHERE binding=? AND task_id=?",
                                         (binding, task_id)).fetchone())
            if task["owner"]:
                raise CollabError("Task is already claimed; pause or reconcile its existing session")
            if task["request"]["state"] == "retired":
                raise CollabError("Task is retired")
            if db.execute("SELECT 1 FROM tasks WHERE owner=?", (owner,)).fetchone():
                raise CollabError("A session can hold only one collaboration task")
            for dependency in task["request"]["depends_on"]:
                dep = db.execute("SELECT result,revision FROM tasks WHERE binding=? AND task_id=?", (binding, dependency)).fetchone()
                if (not dep or not dep[0] or json.loads(dep[0]).get("status") != "completed"
                        or json.loads(dep[0]).get("revision") != dep[1]):
                    raise CollabError(f"Waiting for dependency {dependency}")
            token = uuid.uuid4().hex
            generation = task["generation"] + 1
            history = task["history_name"] or f"collab-{binding}-{task_id}"
            db.execute("UPDATE tasks SET owner=?,owner_pid=?,token=?,generation=?,work_dir=?,history_name=?,"
                       "active_request=request,state='claimed',activity='idle',updated=? "
                       "WHERE binding=? AND task_id=?", (owner, pid, token, generation, work_dir, history,
                                                         time.time(), binding, task_id))
            task.update(revision=task["request"]["revision"], generation=generation)
            self._receipt(db, task, "claimed")
        return self.task(binding, task_id)

    def update_owned(self, binding, task_id, token, *, state=None, activity=None, children=None, delivered=None):
        with self.db() as db:
            task = self._task(db.execute("SELECT * FROM tasks WHERE binding=? AND task_id=?",
                                         (binding, task_id)).fetchone())
            if not token or task["token"] != token:
                raise CollabError("Stale claim; this session no longer owns the task")
            if delivered is not None:
                if delivered != task["request"]["revision"]:
                    raise CollabError("The request changed before admission; retry from the latest revision")
                db.execute("UPDATE tasks SET active_request=request,delivered_revision=? WHERE binding=? AND task_id=?",
                           (delivered, binding, task_id))
            db.execute("UPDATE tasks SET state=?,activity=?,children=?,updated=? WHERE binding=? AND task_id=?",
                       (state or task["state"], activity or task["activity"],
                        json.dumps(task["children"] if children is None else children), time.time(), binding, task_id))
            if state and state != task["state"]:
                fresh = self._task(db.execute("SELECT * FROM tasks WHERE binding=? AND task_id=?",
                                              (binding, task_id)).fetchone())
                self._receipt(db, fresh, state)
        return self.task(binding, task_id)

    def release(self, binding, task_id, token=None):
        # A lease inherited by a surviving bash/task/terminal prevents release.
        with ClaimLock(self.home, binding, task_id):
            with self.db() as db:
                task = self._task(db.execute("SELECT * FROM tasks WHERE binding=? AND task_id=?",
                                             (binding, task_id)).fetchone())
                if token and task["token"] and token != task["token"]:
                    raise CollabError("Stale release cannot clear a newer claim")
                if not task["owner"]:
                    return task
                # Task-runner locks cover live children; saved PIDs additionally
                # catch runners started by older clients without descriptor passing.
                from .runtime_state import pid_alive
                if any(pid_alive(p) for p in task["children"] or []):
                    raise CollabError("Owned tools are still running; wait for them before releasing")
                db.execute("UPDATE tasks SET owner=NULL,owner_pid=NULL,token=NULL,state='paused',"
                           "activity='idle',children='[]',updated=? WHERE binding=? AND task_id=?",
                           (time.time(), binding, task_id))
                self._receipt(db, task, "paused", released=True)
        return self.task(binding, task_id)

    def queue_publication(self, binding, task_id, token, payload):
        with self.db() as db:
            task = self._task(db.execute("SELECT * FROM tasks WHERE binding=? AND task_id=?",
                                         (binding, task_id)).fetchone())
            if task["token"] != token or payload["result"]["revision"] != task["active_request"]["revision"]:
                raise CollabError("Stale result publication")
            revision = payload["result"]["revision"]
            old = db.execute("SELECT payload FROM publications WHERE binding=? AND task_id=? AND revision=?",
                             (binding, task_id, revision)).fetchone()
            if old and json.loads(old[0]) != payload:
                raise CollabError("A pending result is immutable; publish a new request revision")
            db.execute("INSERT OR IGNORE INTO publications(binding,task_id,revision,payload) VALUES(?,?,?,?)",
                       (binding, task_id, revision, encoded(payload).decode()))
            db.execute("UPDATE tasks SET state='publishing',activity='idle',updated=? WHERE binding=? AND task_id=?",
                       (time.time(), binding, task_id))

    def publications(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM publications ORDER BY revision")]

    def published(self, item, error=None):
        with self.db() as db:
            key = (item["binding"], item["task_id"], item["revision"])
            if error:
                db.execute("UPDATE publications SET error=? WHERE binding=? AND task_id=? AND revision=?", (error, *key))
                return
            task = self._task(db.execute("SELECT * FROM tasks WHERE binding=? AND task_id=?", key[:2]).fetchone())
            result = json.loads(item["payload"])["result"]
            state = "awaiting_input" if task["owner"] else "paused"
            db.execute("UPDATE tasks SET result=?,state=?,updated=? WHERE binding=? AND task_id=?",
                       (encoded(result).decode(), state, time.time(), *key[:2]))
            task["result"] = result
            self._receipt(db, task, state, event="result_ready")
            db.execute("DELETE FROM publications WHERE binding=? AND task_id=? AND revision=?", key)

    def pending(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox")]

    def sent(self, item, error=None):
        with self.db() as db:
            if error:
                db.execute("UPDATE outbox SET error=? WHERE binding=? AND task_id=? AND version=?",
                           (error, item["binding"], item["task_id"], item["version"]))
            else:
                db.execute("DELETE FROM outbox WHERE binding=? AND task_id=? AND version=?",
                           (item["binding"], item["task_id"], item["version"]))
