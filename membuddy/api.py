import sqlite3
import sys
import time

from . import history
from .embeddings import Embeddings
from .metrics import Metrics
from .store import Store


def open_store(config, embedder=None):
    return Store(config, embedder if embedder is not None else Embeddings(config.model_dir))


def scan_history(store, limit=100, **unexpected):
    if unexpected:
        raise ValueError("scan-history takes only 'limit'; it reads the history_store set in the MemBuddy config")
    return history.scan(store, limit=limit)


def execute(store, action, payload, run_id=None):
    """One bounded action vocabulary shared by CLI and MCP."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    metrics = Metrics(store.config.data_dir)
    if action == "metrics":
        return metrics.report(**payload)
    if action == "telemetry":
        if "note_id" in payload.get("data", {}):
            store.fetch_ids([payload["data"]["note_id"]])
        return metrics.event(**payload)
    actions = {
        "remember": store.remember,
        "recall": store.recall,
        "list": store.list_notes,
        "preview": store.preview,
        "commit": store.commit,
        "feedback": store.feedback,
        "history": store.history,
        "backup": store.backup,
        "scan-history": lambda **kw: scan_history(store, **kw),
        "candidates": lambda **kw: history.list_candidates(store, **kw),
    }
    if action not in actions:
        raise ValueError(f"Unknown action; choose from {sorted(actions)}")
    started = time.perf_counter()
    result, successful = None, False
    try:
        result = actions[action](**payload)
        successful = True
        return result
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        try:
            metrics.operation(action, payload, result, duration_ms, successful, run_id)
        except (sqlite3.Error, OSError, ValueError) as exc:
            # The memory operation may already be committed; never invite a destructive replay.
            print(f"MemBuddy telemetry write failed ({type(exc).__name__}); "
                  "memory operation outcome is unchanged; metrics may be incomplete.", file=sys.stderr)
