import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def redact(text):
    if not isinstance(text, str):
        raise ValueError("Memory fields must be strings")
    text = re.sub(
        r"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----.*?-----END (?:[A-Z ]*PRIVATE KEY)-----",
        "[REDACTED PRIVATE KEY]", text, flags=re.S,
    )
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-=]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)\b(authorization\s*:\s*)Bearer\s+\[REDACTED\]",
                  r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)\b(password|passwd|secret|token|pat|api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"authorization|[a-z][a-z0-9_]*_(?:pat|token))\b([\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1\2[REDACTED]", text,
    )
    text = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|"
        r"glpat-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b",
        "[REDACTED]", text,
    )
    return re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", text)


def normalize_remote(remote):
    remote = remote.strip()
    if "://" in remote:
        u = urlsplit(remote)
        if u.scheme not in ("https", "http", "ssh", "git") or not u.hostname:
            raise ValueError("Unsupported Git remote URL")
        port = f":{u.port}" if u.port and u.port not in (22, 80, 443) else ""
        result = u.hostname.lower() + port + "/" + u.path.strip("/")
    else:
        match = re.fullmatch(r"(?:[^@/\s]+@)?([^:/\s]+):(.+)", remote)
        if not match:
            raise ValueError("Use a network Git URL, not a local filesystem remote")
        result = match[1].lower() + "/" + match[2].strip("/")
    if result.endswith(".git"):
        result = result[:-4]
    return "repo:" + result


def project_scope(folder=None, remote=None):
    if remote:
        return normalize_remote(remote)
    root = Path(folder or Path.cwd()).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Project folder does not exist: {root}")
    # git must not inherit the MCP server's stdin: that pipe carries the protocol stream.
    result = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        return normalize_remote(result.stdout)
    if result.returncode not in (1, 128):
        raise ValueError(f"Git repository lookup failed: {result.stderr.strip()}")
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    folder_path = Path(top.stdout.strip()).resolve() if top.returncode == 0 else root
    # normcase makes C:\Repo and c:\repo the same project on Windows; it is a no-op elsewhere.
    return "folder:" + os.path.normcase(str(folder_path))
