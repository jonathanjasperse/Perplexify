from __future__ import annotations

import numpy as np


class EmbeddingError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


def normalize(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat[None, :]
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class Embedder:
    """Turns text into unit-length float32 vectors. One instance per embedding spec."""

    spec: dict
    dim: int | None = None
    preferred_batch = 32   # passages per call
    concurrency = 1        # calls that may run at the same time (cloud services take several)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError

    def embed_query(self, text: str) -> np.ndarray:
        raise NotImplementedError

    @property
    def label(self) -> str:
        return f"{self.spec.get('provider')}:{self.spec.get('model')}"
