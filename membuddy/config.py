import json
import os
from dataclasses import dataclass, field
from pathlib import Path

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
DIMENSIONS = 384


def default_history_store():
    return Path.home() / ".copilot" / "session-store.db"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    model_dir: Path
    visibility: str = "silent"
    budget_chars: int = 2000
    max_results: int = 5
    history_store: Path = field(default_factory=default_history_store)

    @classmethod
    def load(cls, path=None):
        config_path = Path(path or os.environ.get(
            "MEMBUDDY_CONFIG", Path.home() / "MemBuddy" / "config.json"
        )).expanduser().resolve()
        data = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        allowed = {"data_dir", "model_dir", "visibility", "budget_chars", "max_results", "history_store"}
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError(f"Unknown configuration keys; allowed: {sorted(allowed)}")

        def location(value):
            p = Path(value).expanduser()
            return (config_path.parent / p).resolve() if not p.is_absolute() else p.resolve()

        data_dir = location(data.get("data_dir", str(Path.home() / "MemBuddy")))
        config = cls(
            data_dir, location(data.get("model_dir", str(data_dir / "model"))),
            data.get("visibility", "silent"), data.get("budget_chars", 2000),
            data.get("max_results", 5),
            location(data["history_store"]) if "history_store" in data else default_history_store(),
        )
        if config.visibility not in ("silent", "brief"):
            raise ValueError("visibility must be silent or brief")
        if type(config.budget_chars) is not int or not 256 <= config.budget_chars <= 16000:
            raise ValueError("budget_chars must be an integer from 256 to 16000")
        if type(config.max_results) is not int or not 1 <= config.max_results <= 20:
            raise ValueError("max_results must be an integer from 1 to 20")
        return config
