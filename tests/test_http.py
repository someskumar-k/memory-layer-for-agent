import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from membuddy.config import Config
from membuddy.http import serve_http, validate_port
from membuddy.server import create_server
from membuddy.setup import configure, setup


class FakeEmbeddings:
    identity = "http-tests"

    def load(self):
        pass

    def encode(self, texts):
        return [[1., 0., 0.] for _ in texts]


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        setup(self.config_path, self.root / "data")
        self.config = Config.load(self.config_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_configuration_backs_up_preserves_other_entries_and_can_revert(self):
        path = self.root / ".copilot" / "mcp-config.json"
        path.parent.mkdir()
        original = {"mcpServers": {"other": {"command": "keep"},
                                   "membuddy": {"type": "http", "headers": {"Authorization": "old"}}}}
        path.write_text(json.dumps(original), encoding="utf-8")
        instructions = path.parent / "copilot-instructions.md"
        instructions.write_text("unchanged", encoding="utf-8")
        configure(self.config_path, "cli", self.root, transport="http", mcp_only=True)
        config = json.loads(path.read_text(encoding="utf-8"))
        entry = config["mcpServers"]["membuddy"]
        self.assertEqual(entry, {"type": "http", "url": "http://127.0.0.1:8765/mcp", "tools": ["*"]})
        self.assertEqual(config["mcpServers"]["other"], original["mcpServers"]["other"])
        self.assertEqual(instructions.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((self.config.data_dir / "http-token").exists())
        backups = list(path.parent.glob("mcp-config.json.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)
        configure(self.config_path, "cli", self.root, transport="http", mcp_only=True)
        self.assertEqual(len(list(path.parent.glob("mcp-config.json.*.bak"))), 1)
        configure(self.config_path, "cli", self.root, transport="stdio", mcp_only=True)
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["membuddy"]
        self.assertEqual(entry["type"], "local")
        self.assertNotIn("headers", entry)

    def test_vscode_http_configuration(self):
        configure(self.config_path, "vscode", self.root, transport="http", port=9876, mcp_only=True)
        path = self.root / ".vscode" / "mcp.json"
        entry = json.loads(path.read_text(encoding="utf-8"))["servers"]["membuddy"]
        self.assertEqual(entry, {"type": "http", "url": "http://127.0.0.1:9876/mcp"})

    def test_invalid_config_and_port_do_not_write_config(self):
        path = self.root / ".copilot" / "mcp-config.json"
        path.parent.mkdir()
        path.write_text("// jsonc not accepted", encoding="utf-8")
        with self.assertRaises(ValueError):
            configure(self.config_path, "cli", self.root, transport="http", mcp_only=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "// jsonc not accepted")
        for port in (0, 80, 65536, True, "8765"):
            with self.assertRaises(ValueError):
                validate_port(port)

    def test_server_forces_loopback_and_disables_proxy_headers(self):
        with mock.patch("membuddy.server.create_server") as create, mock.patch("uvicorn.run") as run:
            serve_http(self.config, 9876)
        self.assertEqual(create.call_args.kwargs, {"port": 9876})
        self.assertEqual(run.call_args.args[0], create.return_value.streamable_http_app.return_value)
        self.assertEqual(run.call_args.kwargs["host"], "127.0.0.1")
        self.assertFalse(run.call_args.kwargs["proxy_headers"])
        self.assertFalse(run.call_args.kwargs["access_log"])

    def test_http_host_origin_and_shared_tool_calls(self):
        asyncio.run(self.http_roundtrip())

    async def http_roundtrip(self):
        with mock.patch("membuddy.server.Embeddings", return_value=FakeEmbeddings()) as embeddings:
            server = create_server(self.config)
        self.assertEqual(embeddings.call_count, 1)
        headers = {"Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-03-26"}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "memory", "arguments": {"action": "metrics", "payload": {}}}}
        app = server.streamable_http_app()
        async with server.session_manager.run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8765",
            ) as client:
                response = await client.post("/mcp", headers={**headers, "Host": "evil.example"}, json=request)
                self.assertEqual(response.status_code, 421)
                response = await client.post("/mcp", headers={**headers, "Origin": "https://evil.example"}, json=request)
                self.assertEqual(response.status_code, 403)
                responses = await asyncio.gather(*[
                    client.post("/mcp", headers=headers, json=request) for _ in range(2)
                ])
                for response in responses:
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertFalse(response.json()["result"].get("isError", False))
                    report = json.loads(response.json()["result"]["content"][0]["text"])
                    self.assertEqual(report["days"], 7)
                self.assertEqual(embeddings.call_count, 1)


if __name__ == "__main__":
    unittest.main()
