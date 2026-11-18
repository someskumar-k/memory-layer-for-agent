"""Local, content-free operation measurements and explicitly reported pilot events."""

import json
import math
import re
import sqlite3
import statistics
import threading
import time
import uuid
from contextlib import contextmanager

from .privacy import redact


def identifier(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value)
            or redact(value) != value):
        raise ValueError("Use a secret-free opaque identifier (1-120 letters, digits, _ . : -)")
    return value


def distribution(values):
    values = sorted(values)
    return {
        "samples": len(values),
        "median_ms": round(statistics.median(values), 3) if values else None,
        "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 3) if values else None,
    }


class Metrics:
    def __init__(self, data_dir):
        self.path = data_dir / "metrics.db"
        self._lock, self._ready = threading.Lock(), False

    def _initialize(self, db):
        # Switching journal mode is not covered by the busy timeout, so do it once, with retries.
        with self._lock:
            if self._ready:
                return
            for attempt in range(50):
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError:
                    if attempt == 49:
                        raise
                    time.sleep(0.1)
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY, timestamp REAL NOT NULL, run_id TEXT,
                    kind TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS metrics_time ON events(timestamp);
                CREATE INDEX IF NOT EXISTS metrics_run ON events(run_id);
            """)
            self._ready = True

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            self._initialize(db)
            with db:
                yield db
        finally:
            db.close()

    def record(self, db, event_id, run_id, kind, data):
        encoded = json.dumps(data, sort_keys=True)
        old = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if old:
            if (old["run_id"], old["kind"], old["data"]) != (run_id, kind, encoded):
                raise ValueError("Event ID already exists with different content")
            return {"status": "recorded", "new_event": False}
        db.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                   (event_id, time.time(), run_id, kind, encoded))
        return {"status": "recorded", "new_event": True}

    def check_run(self, db, run_id, require_open=True):
        rows = db.execute("SELECT kind FROM events WHERE run_id=?", (run_id,)).fetchall()
        if not any(r["kind"] == "task_started" for r in rows):
            raise ValueError("Unknown run_id; report task_started first")
        if require_open and any(r["kind"] == "task_finished" for r in rows):
            raise ValueError("Task is already finished")

    def event(self, event_id, run_id, kind, data):
        identifier(event_id)
        identifier(run_id)
        if not isinstance(data, dict):
            raise ValueError("event data must be an object")
        allowed = {
            "task_started": {"host", "agent", "category", "project", "model", "host_version",
                             "memory_mode", "parent_run_id"},
            "task_finished": {"outcome"},
            "error_observed": {"category", "fingerprint", "note_id"},
            "error_resolved": {"error_event_id", "note_id"},
        }
        required = {
            "task_started": {"host", "agent", "category", "project", "memory_mode"},
            "task_finished": {"outcome"},
            "error_observed": {"category", "fingerprint"},
            "error_resolved": {"error_event_id"},
        }
        if kind not in allowed or set(data) - allowed[kind] or required[kind] - set(data):
            raise ValueError("Invalid event kind or fields; see documented telemetry schema")
        for key, value in data.items():
            if key == "note_id":
                if type(value) is not int or value < 1:
                    raise ValueError("note_id must be a positive integer")
            else:
                identifier(value)
        if kind == "task_started" and data["memory_mode"] not in ("enabled", "disabled"):
            raise ValueError("memory_mode must be enabled or disabled")
        if kind == "task_finished" and data["outcome"] not in ("success", "failed", "cancelled"):
            raise ValueError("outcome must be success, failed or cancelled")
        if kind == "error_observed":
            if data["category"] not in ("implementation", "tool", "infrastructure", "correction", "unknown"):
                raise ValueError("Invalid error category")
            if not re.fullmatch("[0-9a-f]{64}", data["fingerprint"]):
                raise ValueError("fingerprint must be a SHA-256 of a sanitized error signature")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
                return self.record(db, event_id, run_id, kind, data)
            if kind == "task_started":
                if db.execute("SELECT 1 FROM events WHERE run_id=?", (run_id,)).fetchone():
                    raise ValueError("run_id already exists")
                if "parent_run_id" in data:
                    self.check_run(db, data["parent_run_id"])
            else:
                self.check_run(db, run_id)
            if kind == "error_resolved":
                error = db.execute("SELECT * FROM events WHERE id=? AND run_id=? AND kind='error_observed'",
                                   (data["error_event_id"], run_id)).fetchone()
                if not error:
                    raise ValueError("Resolution must reference an error in the same run")
                resolutions = db.execute(
                    "SELECT data FROM events WHERE run_id=? AND kind='error_resolved'", (run_id,))
                if any(json.loads(r["data"])["error_event_id"] == data["error_event_id"] for r in resolutions):
                    raise ValueError("Error already resolved")
            return self.record(db, event_id, run_id, kind, data)

    def operation(self, action, payload, result, duration_ms, successful, run_id=None):
        # Only derived counts and IDs cross this boundary, never queries, notes or exception text.
        data = {"action": action, "duration_ms": duration_ms, "successful": successful}
        if successful and isinstance(result, dict):
            if action == "recall":
                data.update(note_ids=[n["id"] for n in result["notes"]],
                            notes_chars=result["notes_chars"])
            elif action == "feedback":
                data.update(note_id=payload["note_id"], new_event=result["new_event"],
                            helpful=payload["successful"])
            elif action in ("remember", "commit"):
                data["status"] = result["status"]
        with self.connect() as db:
            if run_id is not None:
                self.check_run(db, identifier(run_id))
            self.record(db, uuid.uuid4().hex, run_id, "operation", data)

    def report(self, days=7):
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError("days must be an integer from 1 to 365")
        cutoff = time.time() - days * 86400
        with self.connect() as db:
            rows = db.execute("SELECT rowid AS sequence, * FROM events ORDER BY rowid").fetchall()
        events = [{**dict(r), "data": json.loads(r["data"])} for r in rows]
        recent = [e for e in events if e["timestamp"] >= cutoff]
        operations = [e for e in recent if e["kind"] == "operation"]
        actions = {}
        for action in sorted({e["data"]["action"] for e in operations}):
            values = [e["data"] for e in operations if e["data"]["action"] == action]
            actions[action] = {
                **distribution([v["duration_ms"] for v in values]),
                "failures": sum(not v["successful"] for v in values),
            }
        recalls = [e for e in operations if e["data"]["action"] == "recall" and e["data"]["successful"]]
        helpful = {}
        for e in operations:
            d = e["data"]
            if d["action"] == "feedback" and d["successful"] and d["new_event"] and d["helpful"]:
                key = str(d["note_id"])
                helpful[key] = helpful.get(key, 0) + 1
        by_run = {}
        by_id = {e["id"]: e for e in events}
        projects = {e["run_id"]: e["data"]["project"] for e in events if e["kind"] == "task_started"}
        recalled_by_note = {}
        for e in events:
            by_run.setdefault(e["run_id"], []).append(e)
        for e in recalls:
            for note_id in e["data"]["note_ids"]:
                key = str(note_id)
                recalled_by_note[key] = recalled_by_note.get(key, 0) + 1
        runs = []
        for start in recent:
            if start["kind"] != "task_started":
                continue
            linked = by_run[start["run_id"]]
            end = next((e for e in linked if e["kind"] == "task_finished"), None)
            errors = [e for e in linked if e["kind"] == "error_observed"]
            resolved = {e["data"]["error_event_id"] for e in linked if e["kind"] == "error_resolved"}
            runs.append({
                "run_id": start["run_id"], **start["data"],
                "outcome": end["data"]["outcome"] if end else "incomplete",
                "elapsed_ms": round((end["timestamp"] - start["timestamp"]) * 1000, 3) if end else None,
                "errors": len(errors), "unresolved_errors": sum(e["id"] not in resolved for e in errors),
                "recall_calls": sum(e["kind"] == "operation" and e["data"]["action"] == "recall" for e in linked),
            })
        fingerprints, lessons, errors_after_lesson = {}, {}, []
        for e in events:
            if e["kind"] == "error_resolved" and "note_id" in e["data"]:
                error = by_id[e["data"]["error_event_id"]]
                signature = (projects[error["run_id"]], error["data"]["category"], error["data"]["fingerprint"])
                lessons[signature] = e["data"]["note_id"]
            if e["kind"] == "error_observed":
                signature = (projects[e["run_id"]], e["data"]["category"], e["data"]["fingerprint"])
                if e["timestamp"] < cutoff:
                    continue
                fingerprints[signature] = fingerprints.get(signature, 0) + 1
                if signature in lessons:
                    note_id = lessons[signature]
                    recalled = any(
                        r["sequence"] < e["sequence"] and r["kind"] == "operation"
                        and r["data"]["action"] == "recall" and r["data"]["successful"]
                        and note_id in r["data"]["note_ids"] for r in by_run[e["run_id"]])
                    errors_after_lesson.append({
                        "event_id": e["id"], "run_id": e["run_id"], "note_id": note_id,
                        "lesson_recalled_before_error": recalled,
                    })
        return {
            "days": days, "operations": actions, "helpful_feedback_by_note": helpful,
            "recall_calls": sum(e["data"]["action"] == "recall" for e in operations),
            "successful_recall_calls": len(recalls),
            "mean_recalled_chars": round(statistics.mean(e["data"]["notes_chars"] for e in recalls), 1) if recalls else None,
            "recalled_by_note": recalled_by_note,
            "feedback_assessments": sum(e["data"]["action"] == "feedback" and e["data"]["successful"]
                                        and e["data"]["new_event"] for e in operations),
            "runs": runs,
            "repeated_errors": [
                {"project": project, "category": category, "fingerprint": fp, "occurrences": count}
                for (project, category, fp), count in sorted(fingerprints.items()) if count > 1
            ],
            "errors_after_linked_lesson": errors_after_lesson,
            "limitations": [
                "Task/error events are explicitly reported, not an automatic host watcher.",
                "Operation duration excludes store/model initialization, transport and telemetry writes.",
                "Runs are selected by start time; operations/errors by observation time.",
                "Parent and child runs are separate; do not sum overlapping durations.",
                "Matching fingerprints are repeated observations, not proof of the same root cause or prevented errors.",
                "No speedup estimate or automatic baseline comparison; unreported work is unknown.",
            ],
        }
