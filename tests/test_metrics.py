import asyncio
import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from membuddy.api import execute
from membuddy.cli import main
from membuddy.config import Config
from membuddy.metrics import Metrics, distribution
from membuddy.server import create_server
from membuddy.store import Store


class TestEmbeddings:
    identity = "metrics-test"

    def load(self):
        pass

    def encode(self, texts):
        return [[1., 0., 0.] for _ in texts]


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config(self.root, self.root / "model")
        self.store = Store(self.config, TestEmbeddings())
        self.metrics = Metrics(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def start(self, run_id="run1", **extra):
        data = {"host": "vscode", "agent": "pilot", "category": "test",
                "project": "test-project", "memory_mode": "enabled", **extra}
        return execute(self.store, "telemetry", {
            "event_id": run_id + ":start", "run_id": run_id, "kind": "task_started", "data": data,
        })

    def event(self, event_id, kind, data, run_id="run1"):
        return execute(self.store, "telemetry", {
            "event_id": event_id, "run_id": run_id, "kind": kind, "data": data,
        })

    def save(self):
        return execute(self.store, "remember", {
            "source": "test:metrics", "note": {"kind": "mistake", "text": "Use the verified fix."},
        })["id"]

    def error(self, event_id="error1", run_id="run1"):
        return self.event(event_id, "error_observed", {
            "category": "tool", "fingerprint": hashlib.sha256(b"sanitized-signature-v1").hexdigest(),
        }, run_id)

    def test_exact_distributions_and_empty_values(self):
        self.assertEqual(distribution([]), {"samples": 0, "median_ms": None, "p95_ms": None})
        self.assertEqual(distribution(list(range(1, 21))),
                         {"samples": 20, "median_ms": 10.5, "p95_ms": 19})

    def test_operations_record_counts_without_query_or_note_content(self):
        note_id = self.save()
        execute(self.store, "recall", {"query": "PRIVATE_QUERY", "startup": True})
        report = execute(self.store, "metrics", {})
        self.assertEqual(report["recalled_by_note"], {str(note_id): 1})
        self.assertEqual(report["helpful_feedback_by_note"], {})
        self.assertEqual(report["operations"]["remember"]["samples"], 1)
        with self.metrics.connect() as db:
            data = " ".join(r[0] for r in db.execute("SELECT data FROM events"))
        self.assertNotIn("PRIVATE_QUERY", data)
        self.assertNotIn("Use the verified fix", data)

    def test_feedback_retries_and_negative_feedback_do_not_count_as_help(self):
        note_id = self.save()
        payload = {"note_id": note_id, "event": "test:help", "successful": True}
        execute(self.store, "feedback", payload)
        execute(self.store, "feedback", payload)
        execute(self.store, "feedback", {**payload, "event": "test:nohelp", "successful": False})
        report = self.metrics.report()
        self.assertEqual(report["helpful_feedback_by_note"], {str(note_id): 1})
        self.assertEqual(report["feedback_assessments"], 2)
        self.assertEqual(self.store.list_notes()["notes"][0]["successes"], 1)

    def test_failed_action_is_recorded_and_original_exception_survives(self):
        with self.assertRaisesRegex(ValueError, "Invalid memory kind"):
            execute(self.store, "remember", {"source": "test:x", "note": {"kind": "bad"}})
        self.assertEqual(self.metrics.report()["operations"]["remember"]["failures"], 1)

    def test_telemetry_failure_does_not_change_success_or_original_error(self):
        err = io.StringIO()
        with mock.patch.object(Metrics, "operation", side_effect=sqlite3.OperationalError("SECRET")), contextlib.redirect_stderr(err):
            note_id = self.save()
            with self.assertRaisesRegex(ValueError, "Invalid memory kind"):
                execute(self.store, "remember", {"source": "test:x", "note": {"kind": "bad"}})
        self.assertIn("telemetry write failed", err.getvalue())
        self.assertNotIn("SECRET", err.getvalue())
        self.assertEqual(self.store.list_notes()["notes"][0]["id"], note_id)

    def test_replayed_events_and_conflicting_ids(self):
        self.assertTrue(self.start()["new_event"])
        self.assertFalse(self.start()["new_event"])
        with self.assertRaisesRegex(ValueError, "different content"):
            self.start(category="different")
        self.event("end", "task_finished", {"outcome": "success"})
        self.assertFalse(self.event("end", "task_finished", {"outcome": "success"})["new_event"])
        self.assertFalse(self.start()["new_event"])
        with self.assertRaisesRegex(ValueError, "already finished"):
            self.error()

    def test_task_lifecycle_errors_and_lesson_recurrence(self):
        note_id = self.save()
        with mock.patch("membuddy.metrics.time.time", return_value=100):
            self.start()
            self.error()
            self.event("fixed", "error_resolved", {"error_event_id": "error1", "note_id": note_id})
        with mock.patch("membuddy.metrics.time.time", return_value=102):
            self.event("end", "task_finished", {"outcome": "success"})
        # A later occurrence is linked only to a previously reported verified resolution.
        with mock.patch("membuddy.metrics.time.time", return_value=103):
            self.start("run2")
            execute(self.store, "recall", {"query": "verified fix", "startup": True}, run_id="run2")
            self.error("error2", "run2")
            report = self.metrics.report()
        self.assertEqual(report["runs"][1]["outcome"], "incomplete")
        self.assertEqual(report["runs"][1]["unresolved_errors"], 1)
        self.assertEqual(report["errors_after_linked_lesson"], [{
            "event_id": "error2", "run_id": "run2", "note_id": note_id,
            "lesson_recalled_before_error": True,
        }])
        self.assertEqual(report["repeated_errors"][0]["occurrences"], 2)
        self.assertEqual(report["runs"][0]["elapsed_ms"], 2000)
        self.assertEqual(report["runs"][0]["unresolved_errors"], 0)

    def test_error_resolution_does_not_require_creating_a_memory(self):
        self.start()
        self.error()
        self.event("fixed", "error_resolved", {"error_event_id": "error1"})
        report = self.metrics.report()
        self.assertEqual(report["runs"][0]["unresolved_errors"], 0)
        self.assertEqual(report["errors_after_linked_lesson"], [])
        self.assertEqual(self.store.list_notes()["notes"], [])

    def test_report_window_and_lesson_not_recalled(self):
        note_id = self.save()
        with mock.patch("membuddy.metrics.time.time", return_value=100):
            self.start()
            self.error()
            self.event("fixed", "error_resolved", {"error_event_id": "error1", "note_id": note_id})
        with mock.patch("membuddy.metrics.time.time", return_value=100 + 8 * 86400):
            self.start("run2")
            self.error("later-error", "run2")
            report = self.metrics.report(days=7)
        self.assertEqual([r["run_id"] for r in report["runs"]], ["run2"])
        self.assertEqual(report["repeated_errors"], [])
        self.assertFalse(report["errors_after_linked_lesson"][0]["lesson_recalled_before_error"])

    def test_error_links_do_not_cross_projects(self):
        note_id = self.save()
        self.start()
        self.error()
        self.event("fixed", "error_resolved", {"error_event_id": "error1", "note_id": note_id})
        self.start("different-project", project="project-b")
        self.error("different-error", "different-project")
        report = self.metrics.report()
        self.assertEqual(report["repeated_errors"], [])
        self.assertEqual(report["errors_after_linked_lesson"], [])

    def test_resolution_requires_same_run_real_note_and_unique_resolution(self):
        note_id = self.save()
        self.start()
        self.error()
        with self.assertRaises(ValueError):
            self.event("bad-note", "error_resolved", {"error_event_id": "error1", "note_id": 999})
        self.start("run2")
        with self.assertRaisesRegex(ValueError, "same run"):
            self.event("wrong-run", "error_resolved", {"error_event_id": "error1", "note_id": note_id}, "run2")
        self.event("fixed", "error_resolved", {"error_event_id": "error1", "note_id": note_id})
        with self.assertRaisesRegex(ValueError, "already resolved"):
            self.event("fixed-again", "error_resolved", {"error_event_id": "error1", "note_id": note_id})

    def test_invalid_events_reject_raw_content_and_invalid_lifecycle(self):
        for data in (
            {"outcome": "success", "raw_log": "private"},
            {"outcome": "unknown"},
        ):
            with self.assertRaises(ValueError):
                self.event("x", "task_finished", data)
        with self.assertRaisesRegex(ValueError, "Unknown run_id"):
            self.event("end", "task_finished", {"outcome": "success"})
        self.start()
        with self.assertRaises(ValueError):
            self.event("x", "error_observed", {"category": "tool", "fingerprint": "raw error text"})
        with self.assertRaises(ValueError):
            self.start("other", agent="C:\\private\\path")
        with self.assertRaises(ValueError):
            self.start("other", parent_run_id="nonexistent")
        for days in (0, 366, True, "7"):
            with self.assertRaises(ValueError):
                self.metrics.report(days)

    def test_parent_child_runs_are_explicit_and_unfinished_remains_incomplete(self):
        self.start()
        self.start("child", parent_run_id="run1")
        runs = self.metrics.report()["runs"]
        self.assertEqual(runs[1]["parent_run_id"], "run1")
        self.assertTrue(all(r["elapsed_ms"] is None and r["outcome"] == "incomplete" for r in runs))

    def test_concurrent_event_retries_count_once(self):
        def write(_):
            return self.metrics.event("start", "concurrent", "task_started", {
                "host": "vscode", "agent": "pilot", "category": "test",
                "project": "test-project", "memory_mode": "enabled",
            })
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(write, range(12)))
        self.assertEqual(sum(r["new_event"] for r in results), 1)

    def test_cli_and_mcp_share_metrics_actions(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({"data_dir": str(self.root)}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("membuddy.api.Embeddings", return_value=TestEmbeddings()):
            self.assertEqual(main(["--config", str(config_path), "metrics"]), 0)
        self.assertEqual(json.loads(out.getvalue())["operations"], {})

        async def check():
            with mock.patch("membuddy.server.Embeddings", return_value=TestEmbeddings()):
                server = create_server(self.config)
            await server.call_tool("memory", {
                "action": "telemetry", "payload": {
                    "event_id": "mcp:start", "run_id": "mcp-run", "kind": "task_started",
                    "data": {"host": "vscode", "agent": "pilot", "category": "test",
                             "project": "test-project", "memory_mode": "enabled"},
                },
            })
            await server.call_tool("memory", {"action": "list", "payload": {}, "run_id": "mcp-run"})
            result = await server.call_tool("memory", {"action": "metrics", "payload": {}})
            blocks = result[0] if isinstance(result, tuple) else result
            report = json.loads(blocks[0].text)
            self.assertEqual(report["operations"]["list"]["samples"], 1)
            self.assertEqual(report["runs"][0]["run_id"], "mcp-run")
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
