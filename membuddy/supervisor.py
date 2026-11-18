"""One-shot check-and-repair for the shared HTTP server; the caller exits afterwards."""

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .http import validate_port

SERVE = re.compile(r"membuddy\b.*\sserve\s.*--transport\s+http\b")
PORT = re.compile(r"--port\s+(\d+)")
LOCK_STALE_SECONDS = 600


def healthy(port, timeout=10):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "membuddy-ensure", "version": "1"}}}).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200 and b'"serverInfo"' in response.read()
    except (OSError, urllib.error.URLError):
        return False


def port_open(port):
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def list_processes():
    if os.name == "nt":
        script = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine"
                  " | ConvertTo-Json -Compress")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, check=True,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        rows = json.loads(out or "[]")
        rows = rows if isinstance(rows, list) else [rows]
        return [(r["ProcessId"], r["ParentProcessId"], r.get("CommandLine") or "") for r in rows]
    out = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True, check=True).stdout
    return [(int(p), int(pp), cmd) for p, pp, cmd in (line.split(None, 2) for line in out.splitlines()
                                                     if len(line.split(None, 2)) == 3)]


def server_processes(port, processes=None):
    found = {}
    for pid, ppid, cmd in processes if processes is not None else list_processes():
        match = PORT.search(cmd)
        if SERVE.search(cmd) and int(match.group(1) if match else 8765) == port:
            found[pid] = ppid
    # Only tree roots: a venv launcher and its interpreter child are one server.
    return sorted(pid for pid, ppid in found.items() if ppid not in found)


def kill(pids):
    for pid in pids:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass


def start(config_path, config, port):
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    command = [sys.executable, "-m", "membuddy", "--config", str(config_path),
               "serve", "--transport", "http", "--port", str(port)]
    log = open(config.data_dir / "http-server.log", "w", encoding="utf-8")
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, close_fds=True)
    try:
        if os.name != "nt":
            return subprocess.Popen(command, start_new_session=True, **kwargs).pid
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # Survive the end of a Task Scheduler job when breakaway is permitted.
            return subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                                    **kwargs).pid
        except OSError:
            return subprocess.Popen(command, creationflags=flags, **kwargs).pid
    finally:
        log.close()


def wait_healthy(port, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if healthy(port):
            return True
        time.sleep(2)
    return False


def acquire_lock(path):
    try:
        if time.time() - path.stat().st_mtime > LOCK_STALE_SECONDS:
            path.unlink()
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def ensure(config_path, config, port, grace=60, startup_timeout=120):
    validate_port(port)
    if healthy(port):
        return {"status": "healthy", "port": port}
    lock = config.data_dir / "ensure.lock"
    if not acquire_lock(lock):
        return {"status": "busy", "port": port, "detail": "another ensure run is in progress"}
    try:
        pids = server_processes(port)
        # A server may still be loading its model; give it time before treating it as hung.
        if pids and wait_healthy(port, grace):
            return {"status": "healthy", "port": port}
        if pids:
            kill(pids)
            time.sleep(2)
        if port_open(port):
            raise ValueError(f"Port {port} is held by a process that is not a MemBuddy server; not killed")
        pid = start(config_path, config, port)
        if not wait_healthy(port, startup_timeout):
            raise ValueError(f"Server (pid {pid}) did not become healthy; see "
                             f"{config.data_dir / 'http-server.log'}")
        return {"status": "restarted" if pids else "started", "port": port, "pid": pid,
                "killed": pids}
    finally:
        lock.unlink(missing_ok=True)
