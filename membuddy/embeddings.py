import gc
import json
import os
import shutil
import tempfile
from pathlib import Path
from threading import Lock

from .config import DIMENSIONS, MODEL_ID, MODEL_REVISION

IDENTITY = f"{MODEL_ID}@{MODEL_REVISION}:{DIMENSIONS}:normalized"
MARKER = "membuddy-model.json"


def is_installed(path):
    try:
        return json.loads((Path(path) / MARKER).read_text(encoding="utf-8")) == {"identity": IDENTITY}
    except (OSError, ValueError):
        return False


def check_dimension(model):
    getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    if getter() != DIMENSIONS:
        raise ValueError("Embedding dimensions do not match MemBuddy's model identity")


class Embeddings:
    identity = IDENTITY

    def __init__(self, path):
        self.path = Path(path)
        self._model = None
        self._lock = Lock()

    def encode(self, texts):
        with self._lock:
            self._load()
            return self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()

    def load(self):
        with self._lock:
            self._load()

    def _load(self):
        if self._model is None:
            if not is_installed(self.path):
                raise ValueError("Embedding model not installed or incompatible. Run membuddy setup --download-model.")
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(str(self.path), local_files_only=True, device="cpu")
            check_dimension(model)
            self._model = model


def install_model(path):
    """Only invoked by explicit setup; network access downloads model weights, not chats.

    The model is built and verified in a sibling staging folder, then moved into place.
    model_dir therefore either holds a complete install or does not exist, and an
    interrupted install can be retried without manual cleanup.
    """
    from sentence_transformers import SentenceTransformer
    path = Path(path)
    if is_installed(path):
        return
    if path.exists() and any(path.iterdir()):
        raise ValueError(
            f"{path} exists but is not a complete MemBuddy model install. Move or delete it, "
            "then run setup --download-model again, or set model_dir to another folder.")
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=path.name + ".partial-", dir=path.parent))
    try:
        model = SentenceTransformer(MODEL_ID, revision=MODEL_REVISION, device="cpu")
        model.save(str(staging))
        reloaded = SentenceTransformer(str(staging), local_files_only=True, device="cpu")
        check_dimension(reloaded)
        # Release file handles first: Windows cannot rename a folder holding open model files.
        del model, reloaded
        gc.collect()
        (staging / MARKER).write_text(json.dumps({"identity": IDENTITY}), encoding="utf-8")
        if path.exists():
            path.rmdir()
        os.replace(staging, path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
