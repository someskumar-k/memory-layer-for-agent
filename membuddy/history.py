import re
import sqlite3
from pathlib import Path

from .privacy import redact
from .store import now

CORRECTION = re.compile(
    r"\b(wrong|incorrect|instead|remember|prefer|from now on|do not|don'?t|never|"
    r"you forgot|mistake|always|not what i)\b", re.I
)


def scan(store, limit=100):
    """Explicit, read-only Copilot CLI history adapter. Candidates are not active memory.

    The history file comes from configuration (history_store), never from the caller:
    an agent must not be able to point this at an arbitrary SQLite file.
    """
    path = Path(store.config.history_store).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"History store not found: {path}. Set history_store in the MemBuddy config.")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be 1-500")
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        rows = source.execute(
            "SELECT session_id,turn_index,user_message,assistant_response FROM turns "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        source.close()
    candidates = {}
    for session, turn, user, reply in rows:
        if not user or not CORRECTION.search(user):
            continue
        key = "copilot-cli:" + session
        store.validate_source(key)
        # Candidates are bounded evidence for the host agent, never authoritative lessons.
        excerpt = f"Turn {turn}\nUser: {redact(user)[:650]}\nAgent: {redact(reply or '')[:650]}"
        candidates.setdefault(key, []).append(excerpt)
    with store.transaction():
        for key, excerpts in candidates.items():
            store.db.execute("INSERT OR REPLACE INTO candidates VALUES(?,?,?)",
                             (key, "\n".join(excerpts)[:4000], now()))
    return {"candidates": len(candidates),
            "notice": "Heuristic candidates only. Read context, extract verified lessons, then preview and confirm."}


def list_candidates(store, limit=10, offset=0):
    if type(limit) is not int or not 1 <= limit <= 20 or type(offset) is not int or offset < 0:
        raise ValueError("Use limit 1-20 and a non-negative offset")
    return {"candidates": [dict(r) for r in store.db.execute(
        "SELECT source,text,updated FROM candidates ORDER BY source LIMIT ? OFFSET ?", (limit, offset))]}
