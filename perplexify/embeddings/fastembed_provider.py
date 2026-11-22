"""Local embeddings with fastembed (ONNX, CPU). The default: works offline, no server, no API key."""
from __future__ import annotations

import os

import numpy as np

from .. import paths
from ..models import seed_bundled_models
from .base import Embedder, EmbeddingError, normalize

# Windows without Developer Mode cannot symlink; the cache falls back to copies, which is fine here.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def list_models() -> list[dict]:
    from fastembed import TextEmbedding

    out = []
    for m in TextEmbedding.list_supported_models():
        out.append({"model": m["model"], "dim": m["dim"], "size_gb": m.get("size_in_GB"),
                    "description": m.get("description", ""), "license": m.get("license", "")})
    return sorted(out, key=lambda x: (x["size_gb"] or 0))


class FastEmbedEmbedder(Embedder):
    preferred_batch = 16  # measured fastest on CPU
    concurrency = 1       # one ONNX session already uses the CPU cores

    def __init__(self, model: str):
        self.spec = {"provider": "fastembed", "model": model}
        seed_bundled_models()
        try:
            from fastembed import TextEmbedding
        except ImportError as e:  # pragma: no cover
            raise EmbeddingError("The fastembed package is not installed.") from e
        threads = max(2, (os.cpu_count() or 4) - 2)  # leave some CPU for the UI while indexing
        try:
            self._m = self._load(TextEmbedding, model, threads)
        except ValueError as e:
            raise EmbeddingError(f"'{model}' is not a model fastembed supports: {e}") from e
        except Exception as e:
            raise EmbeddingError(
                f"Could not load the embedding model '{model}'. The first use needs an internet connection "
                f"to download it (about 70 MB for the default). Details: {e}") from e
        info = next((m for m in TextEmbedding.list_supported_models() if m["model"] == model), None)
        self.dim = int(info["dim"]) if info else None

    @staticmethod
    def _load(text_embedding, model: str, threads: int):
        """Load from disk when the files are already there (shipped with the installer, or downloaded earlier), so
        starting never waits on the network; otherwise download the model, which needs internet once."""
        cache = paths.models_dir()
        name = model.split("/")[-1].lower()
        if any(name in d.name.lower() for d in cache.iterdir() if d.is_dir()):
            try:
                return text_embedding(model_name=model, cache_dir=str(cache), threads=threads, local_files_only=True)
            except Exception:  # noqa: BLE001 - an incomplete copy: let fastembed fetch what is missing
                pass
        return text_embedding(model_name=model, cache_dir=str(cache), threads=threads)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        vecs = list(self._m.passage_embed(texts, batch_size=32))
        return normalize(np.asarray(vecs, dtype=np.float32))

    def embed_query(self, text: str) -> np.ndarray:
        vec = next(iter(self._m.query_embed(text)))
        return normalize(np.asarray(vec, dtype=np.float32))[0]
