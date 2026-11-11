"""Durable coordinator result inbox and acknowledgement outbox."""

from __future__ import annotations

import json
import time

from .collab_protocol import CollabError, encoded, request_message, result_id


class Coordinator:
    def __init__(self, store):
        self.store = store
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS coord_requests (
                    binding TEXT NOT NULL, task_id TEXT NOT NULL, request TEXT NOT NULL,
                    PRIMARY KEY(binding,task_id)
                );
                CREATE TABLE IF NOT EXISTS result_inbox (
                    binding TEXT NOT NULL, event_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, request TEXT NOT NULL, result TEXT NOT NULL,
                    receipt_commit TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'received',
                    decision TEXT, error TEXT, updated REAL NOT NULL,
                    PRIMARY KEY(binding,event_id), UNIQUE(binding,task_id,revision)
                );
                CREATE TABLE IF NOT EXISTS ack_outbox (
                    binding TEXT NOT NULL, task_id TEXT NOT NULL, payload TEXT NOT NULL,
                    error TEXT, PRIMARY KEY(binding,task_id)
                );
                CREATE TABLE IF NOT EXISTS codex_routes (
                    binding TEXT NOT NULL, task_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                    workspace TEXT, thread_id TEXT, model TEXT, timeout INTEGER NOT NULL DEFAULT 120,
                    remaining INTEGER NOT NULL DEFAULT 10, pid INTEGER, active_event TEXT, error TEXT,
                    PRIMARY KEY(binding,task_id)
                );
            """)
            if "contract" not in {row[1] for row in db.execute("PRAGMA table_info(coord_requests)")}:
                db.execute("ALTER TABLE coord_requests ADD COLUMN contract TEXT")
                for row in db.execute("SELECT binding,task_id,request FROM coord_requests").fetchall():
                    db.execute("UPDATE coord_requests SET contract=? WHERE binding=? AND task_id=?",
                               (json.loads(row["request"])["instructions"], row["binding"], row["task_id"]))

    def require(self, binding):
        config = self.store.binding(binding)
        if config["role"] != "coordinator":
            raise CollabError("This operation requires a coordinator binding")
        return config

    def register(self, binding, payload):
        self.require(binding)
        request = request_message(payload)
        with self.store.db() as db:
            old = db.execute("SELECT request FROM coord_requests WHERE binding=? AND task_id=?",
                             (binding, request["task_id"])).fetchone()
            if old:
                previous = json.loads(old[0])
                if request["revision"] < previous["revision"]:
                    raise CollabError("Coordinator request revision moved backwards")
                if request["revision"] == previous["revision"] and request != previous:
                    raise CollabError("Coordinator request revision is immutable")
            db.execute("INSERT INTO coord_requests(binding,task_id,request,contract) VALUES(?,?,?,?) ON CONFLICT(binding,task_id) "
                       "DO UPDATE SET request=excluded.request",
                       (binding, request["task_id"], encoded(request).decode(), request["instructions"]))
        return request

    def request(self, binding, task_id):
        self.require(binding)
        with self.store.db() as db:
            row = db.execute("SELECT request FROM coord_requests WHERE binding=? AND task_id=?", (binding, task_id)).fetchone()
        if not row:
            raise CollabError("Publish or receive the coordinator task before configuring it")
        return json.loads(row[0])

    def requests(self, binding):
        with self.store.db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT request FROM coord_requests WHERE binding=?", (binding,))]

    def contract(self, binding, task_id):
        with self.store.db() as db:
            row = db.execute("SELECT contract FROM coord_requests WHERE binding=? AND task_id=?", (binding, task_id)).fetchone()
        if not row:
            raise CollabError("Unknown task contract")
        return row[0]

    def _ack(self, db, event, phase):
        request = event["request"]
        payload = {"schema_version": 1, "task_id": event["task_id"], "revision": event["revision"],
                   "worker_id": request["worker_id"], "event_id": event["event_id"],
                   "candidate_commit": event["result"]["candidate_commit"], "phase": phase}
        db.execute("INSERT INTO ack_outbox(binding,task_id,payload) VALUES(?,?,?) "
                   "ON CONFLICT(binding,task_id) DO UPDATE SET payload=excluded.payload,error=NULL",
                   (event["binding"], event["task_id"], encoded(payload).decode()))

    @staticmethod
    def decode(row):
        if row is None:
            raise CollabError("Unknown result event")
        event = dict(row)
        for key in ("request", "result", "decision"):
            event[key] = json.loads(event[key]) if event.get(key) else None
        return event

    def ingest(self, binding, request, incoming):
        result = incoming["result"]
        event_id = result_id(request["worker_id"], result)
        with self.store.db() as db:
            old = db.execute("SELECT event_id FROM result_inbox WHERE binding=? AND task_id=? AND revision=?",
                             (binding, request["task_id"], request["revision"])).fetchone()
            if old:
                if old[0] != event_id:
                    raise CollabError("A remote result changed inside an immutable request revision")
                return False
            db.execute("INSERT INTO result_inbox(binding,event_id,task_id,revision,request,result,receipt_commit,updated) "
                       "VALUES(?,?,?,?,?,?,?,?)", (binding, event_id, request["task_id"], request["revision"],
                       encoded(request).decode(), encoded(result).decode(), incoming["receipt_commit"], time.time()))
            event = self.decode(db.execute("SELECT * FROM result_inbox WHERE binding=? AND event_id=?", (binding, event_id)).fetchone())
            self._ack(db, event, "received")
        return True

    def events(self, binding, task_id=None, *, state=None):
        self.require(binding)
        with self.store.db() as db:
            query = "SELECT * FROM result_inbox WHERE binding=?"
            params = [binding]
            if task_id:
                query += " AND task_id=?"
                params.append(task_id)
            if state:
                query += " AND state=?"
                params.append(state)
            return [self.decode(row) for row in db.execute(query + " ORDER BY updated DESC LIMIT 128", params)]

    def event(self, binding, event_id):
        self.require(binding)
        with self.store.db() as db:
            return self.decode(db.execute("SELECT * FROM result_inbox WHERE binding=? AND event_id=?", (binding, event_id)).fetchone())

    def acknowledge(self, binding, event_id):
        with self.store.db() as db:
            event = self.decode(db.execute("SELECT * FROM result_inbox WHERE binding=? AND event_id=?", (binding, event_id)).fetchone())
            if event["state"] in ("running", "decision_ready"):
                raise CollabError("The automatic continuation must finish or be reconciled before manual acknowledgement")
            db.execute("UPDATE result_inbox SET state='processed',updated=? WHERE binding=? AND event_id=?",
                       (time.time(), binding, event_id))
            db.execute("UPDATE codex_routes SET active_event=NULL,pid=NULL WHERE binding=? AND task_id=? AND active_event=?",
                       (binding, event["task_id"], event_id))
            self._ack(db, event, "processed")
        return {"event_id": event_id, "state": "processed"}

    def acks(self):
        with self.store.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM ack_outbox")]

    def ack_sent(self, item, error=None):
        with self.store.db() as db:
            key = (item["binding"], item["task_id"], item["payload"])
            if error:
                db.execute("UPDATE ack_outbox SET error=? WHERE binding=? AND task_id=? AND payload=?", (error, *key))
            else:
                db.execute("DELETE FROM ack_outbox WHERE binding=? AND task_id=? AND payload=?", key)

    def configure(self, binding, task_id, *, enabled=True, workspace=None, timeout=120, max_turns=10, model=None):
        self.request(binding, task_id)
        if type(timeout) is not int or not 10 <= timeout <= 3600 or type(max_turns) is not int or not 1 <= max_turns <= 1000:
            raise CollabError("Codex timeout must be 10-3600 seconds and max_turns 1-1000")
        if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 128):
            raise CollabError("Invalid locally selected Codex model")
        if workspace is not None:
            from pathlib import Path
            workspace = str(Path(workspace).expanduser().resolve(strict=True))
            from .collab_git import git
            git(Path(workspace), "rev-parse", "--show-toplevel")
        with self.store.db() as db:
            old = db.execute("SELECT * FROM codex_routes WHERE binding=? AND task_id=?", (binding, task_id)).fetchone()
            if old and old["active_event"]:
                raise CollabError("Reconcile the current Codex turn before reconfiguring its route")
            if old:
                workspace = old["workspace"] if workspace is None else workspace
                model = old["model"] if model is None else model
            if old and (old["workspace"] != workspace or old["model"] != model):
                raise CollabError("A route's workspace and model are fixed; create another task for a different context")
            db.execute("INSERT INTO codex_routes(binding,task_id,enabled,workspace,timeout,remaining,model) "
                       "VALUES(?,?,?,?,?,?,?) ON CONFLICT(binding,task_id) DO UPDATE SET enabled=excluded.enabled,"
                       "timeout=excluded.timeout,remaining=excluded.remaining,error=NULL",
                       (binding, task_id, int(enabled), workspace, timeout, max_turns, model))
        return self.route(binding, task_id)

    def route(self, binding, task_id):
        with self.store.db() as db:
            row = db.execute("SELECT * FROM codex_routes WHERE binding=? AND task_id=?", (binding, task_id)).fetchone()
        if not row:
            raise CollabError("No Codex route configured for this task")
        return dict(row)

    def pause(self, binding, task_id):
        with self.store.db() as db:
            db.execute("UPDATE codex_routes SET enabled=0 WHERE binding=? AND task_id=?", (binding, task_id))
        return self.route(binding, task_id)

    def resume(self, binding, task_id):
        route = self.route(binding, task_id)
        if route["active_event"] and self.event(binding, route["active_event"])["state"] == "needs_attention":
            raise CollabError("Reconcile the uncertain turn with explicit retry or acknowledgement")
        with self.store.db() as db:
            db.execute("UPDATE codex_routes SET enabled=1 WHERE binding=? AND task_id=?", (binding, task_id))
        return self.route(binding, task_id)

    def routes(self):
        with self.store.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM codex_routes")]

    def retry(self, binding, event_id):
        event = self.event(binding, event_id)
        if event["state"] != "needs_attention":
            raise CollabError("Only a reconciled needs_attention event can be explicitly retried")
        with self.store.db() as db:
            db.execute("UPDATE result_inbox SET state='received',decision=NULL,error=NULL,updated=? WHERE binding=? AND event_id=?",
                       (time.time(), binding, event_id))
            db.execute("UPDATE codex_routes SET active_event=NULL,pid=NULL,enabled=1,error=NULL WHERE binding=? AND task_id=?",
                       (binding, event["task_id"]))
        return {"event_id": event_id, "state": "received"}

    def start(self, binding, event_id):
        with self.store.db() as db:
            event = self.decode(db.execute("SELECT * FROM result_inbox WHERE binding=? AND event_id=?", (binding, event_id)).fetchone())
            route = db.execute("SELECT * FROM codex_routes WHERE binding=? AND task_id=?", (binding, event["task_id"])).fetchone()
            if event["state"] != "received" or not route or not route["enabled"] or route["remaining"] <= 0 or route["active_event"]:
                return False
            db.execute("UPDATE result_inbox SET state='running',updated=? WHERE binding=? AND event_id=?", (time.time(), binding, event_id))
            db.execute("UPDATE codex_routes SET remaining=remaining-1,active_event=?,pid=NULL,error=NULL WHERE binding=? AND task_id=?",
                       (event_id, binding, event["task_id"]))
        return True

    def running_pid(self, binding, task_id, pid):
        with self.store.db() as db:
            db.execute("UPDATE codex_routes SET pid=? WHERE binding=? AND task_id=?", (pid, binding, task_id))

    def attention(self, binding, event_id, error):
        with self.store.db() as db:
            event = self.decode(db.execute("SELECT * FROM result_inbox WHERE binding=? AND event_id=?", (binding, event_id)).fetchone())
            db.execute("UPDATE result_inbox SET state='needs_attention',error=?,updated=? WHERE binding=? AND event_id=?",
                       (error, time.time(), binding, event_id))
            db.execute("UPDATE codex_routes SET enabled=0,error=? WHERE binding=? AND task_id=?", (error, binding, event["task_id"]))

    def save_decision(self, binding, event_id, decision, thread_id):
        with self.store.db() as db:
            event = self.decode(db.execute("SELECT * FROM result_inbox WHERE binding=? AND event_id=?", (binding, event_id)).fetchone())
            if event["state"] != "running":
                raise CollabError("Codex event was superseded before its decision was saved")
            db.execute("UPDATE result_inbox SET state='decision_ready',decision=?,updated=? WHERE binding=? AND event_id=?",
                       (encoded(decision).decode(), time.time(), binding, event_id))
            db.execute("UPDATE codex_routes SET thread_id=?,pid=NULL WHERE binding=? AND task_id=?", (thread_id, binding, event["task_id"]))

    def finish(self, binding, event_id, close=False):
        with self.store.db() as db:
            event = self.decode(db.execute("SELECT * FROM result_inbox WHERE binding=? AND event_id=?", (binding, event_id)).fetchone())
            db.execute("UPDATE result_inbox SET state='processed',error=NULL,updated=? WHERE binding=? AND event_id=?", (time.time(), binding, event_id))
            db.execute("UPDATE codex_routes SET active_event=NULL,pid=NULL,enabled=CASE WHEN ? THEN 0 ELSE enabled END,error=NULL "
                       "WHERE binding=? AND task_id=?", (int(close), binding, event["task_id"]))
            self._ack(db, event, "processed")
