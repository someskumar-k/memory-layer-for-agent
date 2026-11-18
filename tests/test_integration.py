import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from membuddy.config import Config
from membuddy.setup import setup


@unittest.skipUnless(os.environ.get("MEMBUDDY_REAL_MODEL") == "1",
                     "Set MEMBUDDY_REAL_MODEL=1 to run offline model and stdio MCP integration tests")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.config_path = cls.root / "config.json"
        setup(cls.config_path, cls.root / "data", download_model=True)
        cls.config = Config.load(cls.config_path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def cli(self, action, payload):
        result = subprocess.run(
            [sys.executable, "-m", "membuddy", "--config", str(self.config_path),
             action, json.dumps(payload)],
            text=True, capture_output=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_real_embeddings_cli_persistence_and_semantic_recall(self):
        stored = self.cli("remember", {
            "source": "integration:cli-chat", "note": {
                "kind": "mistake", "text": "Windows PowerShell 5 rejects && command chaining.\nUse semicolons and if ($?) for dependent commands.",
                "conditions": "Windows PowerShell 5", "origin": "correction",
                "scope": "global", "reusable": True,
            },
        })
        self.assertEqual(stored["status"], "stored")
        recalled = self.cli("recall", {"query": "How do I chain commands in older PowerShell?"})
        self.assertTrue(recalled["notes"], recalled)
        self.assertIn("PowerShell", recalled["notes"][0]["text"])
        self.assertEqual(recalled["notes"][0]["id"], stored["id"])
        self.assertLessEqual(recalled["notes_chars"], 2000)

    def test_real_mcp_stdio_roundtrip_confirmation_and_errors(self):
        asyncio.run(self.mcp_roundtrip())

    async def mcp_roundtrip(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "membuddy", "--config", str(self.config_path), "serve"],
            env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=90)) as session:
                await session.initialize()
                tools = await session.list_tools()
                self.assertEqual({t.name for t in tools.tools}, {"memory", "identify_project"})
                scoped = await session.call_tool("identify_project", {"remote": "git@example.com:owner/repo.git"})
                self.assertFalse(scoped.isError)
                self.assertIn("repo:example.com/owner/repo", scoped.content[0].text)
                # The server's own cwd is not the agent's project: omitting the folder must fail.
                unscoped = await session.call_tool("identify_project", {})
                self.assertTrue(unscoped.isError)
                folder_scope = await session.call_tool("identify_project", {"folder": str(self.root)})
                self.assertFalse(folder_scope.isError, folder_scope)

                async def memory(action, payload):
                    result = await session.call_tool("memory", {"action": action, "payload": payload})
                    self.assertFalse(result.isError, result)
                    return json.loads(result.content[0].text)

                preview = await memory("preview", {"operation": "conversation", "payload": {
                    "source": "integration:mcp-chat",
                    "notes": [{"kind": "preference", "text": "Explain the plan before editing files.", "origin": "user"}],
                }})
                self.assertEqual(preview["status"], "confirmation_required")
                rejected = await session.call_tool("memory", {"action": "commit", "payload": {
                    "proposal_id": preview["proposal_id"], "confirmed": False,
                }})
                self.assertTrue(rejected.isError)
                committed = await memory("commit", {"proposal_id": preview["proposal_id"], "confirmed": True})
                self.assertEqual(committed["status"], "committed")
                recalled = await memory("recall", {"query": "Before changing source code", "startup": True})
                self.assertTrue(any("Explain the plan" in n["text"] for n in recalled["notes"]))
                await memory("feedback", {"note_id": committed["ids"][0], "event": "integration:action1", "successful": True})
                listed = await memory("list", {})
                self.assertTrue(any(n["successes"] == 1 for n in listed["notes"]))


if __name__ == "__main__":
    unittest.main()
