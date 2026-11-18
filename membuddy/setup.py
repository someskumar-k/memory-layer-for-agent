import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from .config import Config
from .embeddings import install_model
from .http import DEFAULT_PORT, validate_port

SKILLS = ("membuddy", "add-to-mem", "show-summary", "remove-memory", "optimize-memory")


def write_with_backup(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") == content:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        shutil.copy2(path, path.with_name(path.name + "." + stamp + ".bak"))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def setup(config_path, data_dir=None, download_model=False):
    path = Path(config_path).expanduser().resolve()
    if path.exists() and data_dir:
        old = Config.load(path)
        if old.data_dir != Path(data_dir).expanduser().resolve():
            raise ValueError("Changing data_dir does not migrate an existing database; copy a backup first and edit config explicitly")
    if not path.exists():
        location = Path(data_dir).expanduser().resolve() if data_dir else Path.home() / "MemBuddy"
        write_with_backup(path, json.dumps({"data_dir": str(location), "visibility": "silent",
                                           "budget_chars": 2000, "max_results": 5}, indent=2) + "\n")
    config = Config.load(path)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    if download_model:
        install_model(config.model_dir)
    return {"config": str(path), "data_dir": str(config.data_dir),
            "model_installed": (config.model_dir / "membuddy-model.json").is_file()}


def configure(config_path, client, target, transport="stdio", port=DEFAULT_PORT, mcp_only=False):
    """Explicitly target a CLI home or VS Code workspace, never discover unrelated configs."""
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.is_file():
        raise ValueError("Run setup with this config path before configuring a client")
    target = Path(target).expanduser().resolve()
    if not target.is_dir():
        raise ValueError("target must be an existing directory")
    if client == "cli":
        path, root_key = target / ".copilot" / "mcp-config.json", "mcpServers"
        entry = {"type": "local", "command": sys.executable,
                 "args": ["-m", "membuddy", "--config", str(config_path), "serve"], "tools": ["*"]}
        skills = target / ".copilot" / "skills"
        instructions = target / ".copilot" / "copilot-instructions.md"
    elif client == "vscode":
        path, root_key = target / ".vscode" / "mcp.json", "servers"
        entry = {"type": "stdio", "command": sys.executable,
                 "args": ["-m", "membuddy", "--config", str(config_path), "serve"]}
        skills = target / ".github" / "skills"
        instructions = target / ".github" / "instructions" / "membuddy.instructions.md"
    else:
        raise ValueError("client must be cli or vscode")
    if transport not in ("stdio", "http"):
        raise ValueError("transport must be stdio or http")
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(config, dict) or not isinstance(config.get(root_key, {}), dict):
        raise ValueError("Existing MCP configuration is not an object")
    writes = []
    for name in SKILLS:
        body = files("membuddy").joinpath("assets", name, "SKILL.md").read_text(encoding="utf-8")
        if not mcp_only:
            writes.append((skills / name / "SKILL.md", body))
    body = files("membuddy").joinpath("assets", "instructions.md").read_text(encoding="utf-8")
    if client == "cli" and not mcp_only:
        existing = instructions.read_text(encoding="utf-8") if instructions.exists() else ""
        start, end = "<!-- membuddy:start -->", "<!-- membuddy:end -->"
        block = start + "\n" + body + "\n" + end
        if start in existing:
            if existing.count(start) != 1 or existing.count(end) != 1 or existing.index(end) < existing.index(start):
                raise ValueError("Malformed MemBuddy block in existing instructions; repair before configuring")
            before, after = existing.split(start)
            existing = before + block + after.split(end, 1)[1]
        elif not mcp_only:
            existing = existing.rstrip() + "\n\n" + block + "\n"
        if not mcp_only:
            writes.append((instructions, existing))
    else:
        if not mcp_only:
            writes.append((instructions, '---\napplyTo: "**"\n---\n\n' + body))
    if transport == "http":
        validate_port(port)
        entry = {"type": "http", "url": f"http://127.0.0.1:{port}/mcp"}
        if client == "cli":
            entry["tools"] = ["*"]
    config.setdefault(root_key, {})["membuddy"] = entry
    writes.insert(0, (path, json.dumps(config, indent=2) + "\n"))
    for destination, content in writes:
        write_with_backup(destination, content)
    result = {"mcp_config": str(path),
              "notice": "Restart/reload the client, trust the MCP server, and verify tools are discovered."}
    if not mcp_only:
        result.update(skills=str(skills), instructions=str(instructions))
    if transport == "http":
        result["notice"] = (
            "Start one shared 'serve --transport http' process on the configured port, "
            "then reconnect the client. Any local process can access this server.")
    return result
