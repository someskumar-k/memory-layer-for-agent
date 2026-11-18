import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from membuddy import supervisor
from membuddy.config import Config

LAUNCHER = r'"C:\venv\python.exe" -m membuddy --config C:\c.json serve --transport http --port 8765'
CHILD = r'"C:\Python\python.exe" -m membuddy --config C:\c.json serve --transport http --port 8765'


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name), Path(self.tmp.name) / "model")
        self.cfg_path = Path(self.tmp.name) / "config.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_ensure(self, healthy, pids=(), port_open=False, wait=(True,)):
        with mock.patch.object(supervisor, "healthy", return_value=healthy), \
             mock.patch.object(supervisor, "server_processes", return_value=list(pids)), \
             mock.patch.object(supervisor, "wait_healthy", side_effect=list(wait)) as wait_mock, \
             mock.patch.object(supervisor, "port_open", return_value=port_open), \
             mock.patch.object(supervisor, "kill") as kill, \
             mock.patch.object(supervisor, "start", return_value=4242) as start, \
             mock.patch.object(supervisor.time, "sleep"):
            result = supervisor.ensure(self.cfg_path, self.config, 8765, grace=1, startup_timeout=1)
        return result, kill, start, wait_mock

    def test_healthy_server_is_left_alone(self):
        result, kill, start, _ = self.run_ensure(healthy=True)
        self.assertEqual(result["status"], "healthy")
        kill.assert_not_called()
        start.assert_not_called()

    def test_missing_server_is_started(self):
        result, kill, start, _ = self.run_ensure(healthy=False)
        self.assertEqual(result, {"status": "started", "port": 8765, "pid": 4242, "killed": []})
        kill.assert_not_called()
        start.assert_called_once_with(self.cfg_path, self.config, 8765)
        self.assertFalse((self.config.data_dir / "ensure.lock").exists())

    def test_starting_server_is_given_grace(self):
        result, kill, start, _ = self.run_ensure(healthy=False, pids=[11], wait=(True,))
        self.assertEqual(result["status"], "healthy")
        kill.assert_not_called()
        start.assert_not_called()

    def test_hung_server_is_killed_and_restarted(self):
        result, kill, start, _ = self.run_ensure(healthy=False, pids=[11], wait=(False, True))
        self.assertEqual(result["status"], "restarted")
        self.assertEqual(result["killed"], [11])
        kill.assert_called_once_with([11])
        start.assert_called_once()

    def test_foreign_port_owner_is_not_killed(self):
        with self.assertRaisesRegex(ValueError, "not a MemBuddy server"):
            self.run_ensure(healthy=False, port_open=True)
        self.assertFalse((self.config.data_dir / "ensure.lock").exists())

    def test_failed_start_is_reported(self):
        with self.assertRaisesRegex(ValueError, "did not become healthy"):
            self.run_ensure(healthy=False, wait=(False,))

    def test_concurrent_run_is_busy(self):
        (self.config.data_dir / "ensure.lock").touch()
        result, kill, start, _ = self.run_ensure(healthy=False)
        self.assertEqual(result["status"], "busy")
        start.assert_not_called()

    def test_stale_lock_is_replaced(self):
        lock = self.config.data_dir / "ensure.lock"
        lock.touch()
        old = supervisor.time.time() - supervisor.LOCK_STALE_SECONDS - 5
        import os
        os.utime(lock, (old, old))
        result, _, start, _ = self.run_ensure(healthy=False)
        self.assertEqual(result["status"], "started")

    def test_server_processes_returns_tree_roots_for_port(self):
        rows = [
            (10, 1, LAUNCHER), (11, 10, CHILD),
            (20, 1, LAUNCHER.replace("8765", "9000")),
            (30, 1, r'python -m membuddy --config C:\c.json ensure --port 8765'),
            (40, 1, r'python -m membuddy serve'),
            (50, 1, "notepad.exe"),
        ]
        self.assertEqual(supervisor.server_processes(8765, rows), [10])
        self.assertEqual(supervisor.server_processes(9000, rows), [20])

    def test_healthy_sends_unauthenticated_initialize(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen["method"] = body["method"]
                seen["authorization"] = self.headers.get("Authorization")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"m"}}}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            self.assertTrue(supervisor.healthy(port))
            self.assertEqual(seen["method"], "initialize")
            self.assertIsNone(seen["authorization"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(supervisor.healthy(port, timeout=1))


if __name__ == "__main__":
    unittest.main()
