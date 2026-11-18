import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from .api import execute, open_store
from .config import Config
from .privacy import project_scope
from .http import DEFAULT_PORT
from .setup import configure, setup


def parser():
    p = argparse.ArgumentParser(description="MemBuddy: local lessons and preferences shared across agents")
    p.add_argument("--config", default=os.environ.get(
        "MEMBUDDY_CONFIG", str(Path.home() / "MemBuddy" / "config.json")))
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("setup", help="Create config; download the pinned embedding model only with the flag")
    a.add_argument("--data-dir")
    a.add_argument("--download-model", action="store_true")
    a = sub.add_parser("configure", help="Explicitly configure a CLI home or VS Code workspace; backups precede changes")
    a.add_argument("--client", choices=("cli", "vscode"), required=True)
    a.add_argument("--target", required=True)
    a.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    a.add_argument("--port", type=int, default=DEFAULT_PORT)
    a.add_argument("--mcp-only", action="store_true", help="Update only MCP config, not installed skills/instructions")
    a = sub.add_parser("serve", help="Run stdio MCP or one shared localhost HTTP server")
    a.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    a.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = sub.add_parser("ensure", help="Check the shared HTTP server; restart it if unhealthy, then exit")
    a.add_argument("--port", type=int, default=DEFAULT_PORT)
    a.add_argument("--grace", type=int, default=60, help="Seconds a starting server may take before it is killed")
    a = sub.add_parser("scope", help="Identify a repository by Git URL, otherwise absolute folder")
    a.add_argument("--folder")
    a.add_argument("--remote")
    for name in ("remember", "recall", "list", "preview", "commit", "feedback", "history",
                 "backup", "scan-history", "candidates", "metrics", "telemetry"):
        a = sub.add_parser(name, help="Pass a JSON object, @file, or - for stdin; see README")
        a.add_argument("payload", nargs="?", default="{}")
        a.add_argument("--run-id", help="Link a memory operation to an explicitly started telemetry run")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == "setup":
            result = setup(args.config, args.data_dir, args.download_model)
        elif args.command == "configure":
            result = configure(args.config, args.client, args.target,
                               transport=args.transport, port=args.port, mcp_only=args.mcp_only)
        elif args.command == "ensure":
            from .supervisor import ensure
            result = ensure(Path(args.config).expanduser().resolve(), Config.load(args.config),
                            args.port, grace=args.grace)
        elif args.command == "scope":
            result = {"scope": project_scope(args.folder, args.remote)}
        elif args.command == "serve":
            config = Config.load(args.config)
            if args.transport == "http":
                from .http import serve_http
                serve_http(config, args.port)
            else:
                from .server import create_server
                create_server(config).run(transport="stdio")
            return 0
        else:
            raw = args.payload
            if raw == "-":
                raw = sys.stdin.read()
            elif raw.startswith("@"):
                raw = Path(raw[1:]).read_text(encoding="utf-8")
            payload = json.loads(raw)
            store = open_store(Config.load(args.config))
            try:
                result = execute(store, args.command, payload, run_id=args.run_id)
            finally:
                store.close()
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
