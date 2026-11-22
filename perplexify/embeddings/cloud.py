"""Cloud embedding back ends: Gemini, OpenAI(-compatible) and Voyage AI.

Claude and DeepSeek have no embeddings API; Anthropic recommends Voyage AI, which is offered here, and the
built-in model covers everything else. A library remembers which embedder built its index, so all of these
are selected per library and queries are embedded with the same model.
"""
from __future__ import annotations

import re
import time

import httpx
import numpy as np

from .base import Embedder, EmbeddingError, normalize

_RETRY_STATUS = (429, 500, 502, 503, 504)


def _describe(status: int, text: str, label: str) -> str:
    low = text.lower()
    if status in (401, 403) or (status == 400 and "api key" in low and "not valid" in low):
        return f"{label} rejected the API key."
    if status == 404:
        return f"{label} could not find that embedding model (404)."
    if status == 429:
        return f"{label} rate limit or quota reached (429). Wait a moment, check your plan, or use the built-in model."
    snippet = re.sub(r"\s+", " ", text).strip()[:300]
    return f"{label} returned {status}: {snippet}"


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("retry-after", "")
    if v.replace(".", "", 1).isdigit():
        return float(v)
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', r.text)  # Gemini puts it in the error body
    return float(m.group(1)) if m else None


class _CloudEmbedder(Embedder):
    label_name = "The embeddings service"
    preferred_batch = 64
    concurrency = 4

    def __init__(self, spec: dict, api_key: str):
        self.spec = dict(spec)
        self.model = spec["model"]
        self.key = api_key
        self.dimensions = spec.get("dimensions")
        self.code = spec.get("flavor") == "code"
        self._http = httpx.Client(timeout=httpx.Timeout(120, connect=10))

    def _post(self, url: str, payload: dict, headers: dict, attempts: int = 7) -> dict:
        delay = 2.0
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                r = self._http.post(url, json=payload, headers=headers)
            except httpx.HTTPError as e:
                if last:
                    raise EmbeddingError(f"Could not reach {self.label_name}. Check your internet connection.") from e
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in _RETRY_STATUS and not last:
                time.sleep(min(_retry_after(r) or delay, 60))
                delay = min(delay * 2, 30)
                continue
            raise EmbeddingError(_describe(r.status_code, r.text, self.label_name))
        raise EmbeddingError(f"{self.label_name} did not respond.")

    def _vectors(self, texts: list[str], query: bool) -> np.ndarray:
        raise NotImplementedError

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim or 0), dtype=np.float32)
        parts = [self._vectors(texts[i:i + self.preferred_batch], False) for i in range(0, len(texts), self.preferred_batch)]
        return np.vstack(parts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._vectors([text], True)[0]


class OpenAIEmbedder(_CloudEmbedder):
    label_name = "OpenAI"
    preferred_batch = 96

    def __init__(self, spec: dict, api_key: str):
        super().__init__(spec, api_key)
        if not api_key:
            raise EmbeddingError("OpenAI embeddings need an OpenAI API key. Connect ChatGPT (OpenAI) in Settings, or use the built-in model.")
        self.base = (spec.get("base_url") or "https://api.openai.com/v1").rstrip("/")

    def _vectors(self, texts: list[str], query: bool) -> np.ndarray:
        payload: dict = {"model": self.model, "input": texts}
        if self.dimensions and self.model.startswith("text-embedding-3"):
            payload["dimensions"] = int(self.dimensions)
        data = self._post(f"{self.base}/embeddings", payload, {"Authorization": f"Bearer {self.key}"})
        rows = sorted(data["data"], key=lambda d: d["index"])
        vecs = normalize(np.asarray([d["embedding"] for d in rows], dtype=np.float32))
        self.dim = vecs.shape[1]
        return vecs


class GeminiEmbedder(_CloudEmbedder):
    label_name = "Gemini"
    preferred_batch = 50
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"

    def __init__(self, spec: dict, api_key: str):
        super().__init__(spec, api_key)
        if not api_key:
            raise EmbeddingError("Gemini embeddings need a Gemini API key. Connect Gemini in Settings, or use the built-in model.")
        self.dimensions = int(spec.get("dimensions") or 768)

    def _item(self, text: str, query: bool) -> dict:
        item: dict = {"model": f"models/{self.model}", "outputDimensionality": self.dimensions}
        if self.model.startswith("gemini-embedding-001"):
            item["taskType"] = ("CODE_RETRIEVAL_QUERY" if self.code else "RETRIEVAL_QUERY") if query else "RETRIEVAL_DOCUMENT"
            item["content"] = {"parts": [{"text": text}]}
        else:  # newer models take no task type; the task is written into the text instead
            body = (f"task: {'code retrieval' if self.code else 'search result'} | query: {text}" if query
                    else f"title: none | text: {text}")
            item["content"] = {"parts": [{"text": body}]}
        return item

    def _vectors(self, texts: list[str], query: bool) -> np.ndarray:
        # one request item per text, so every text gets its own vector (never one aggregated vector)
        data = self._post(self.URL.format(model=self.model),
                          {"requests": [self._item(t, query) for t in texts]},
                          {"x-goog-api-key": self.key})
        rows = data.get("embeddings") or []
        if len(rows) != len(texts):
            raise EmbeddingError(f"Gemini returned {len(rows)} embeddings for {len(texts)} texts.")
        vecs = normalize(np.asarray([r["values"] for r in rows], dtype=np.float32))  # truncated sizes are not normalised
        self.dim = vecs.shape[1]
        return vecs


class VoyageEmbedder(_CloudEmbedder):
    label_name = "Voyage AI"
    preferred_batch = 64

    def __init__(self, spec: dict, api_key: str):
        super().__init__(spec, api_key)
        if not api_key:
            raise EmbeddingError("Voyage embeddings need a Voyage AI key. Add one in Settings > Embeddings, or use the built-in model.")
        self.base = (spec.get("base_url") or "https://api.voyageai.com/v1").rstrip("/")

    def _vectors(self, texts: list[str], query: bool) -> np.ndarray:
        payload: dict = {"input": texts, "model": self.model, "input_type": "query" if query else "document",
                         "truncation": True}
        if self.dimensions:
            payload["output_dimension"] = int(self.dimensions)
        data = self._post(f"{self.base}/embeddings", payload, {"Authorization": f"Bearer {self.key}"})
        rows = sorted(data["data"], key=lambda d: d["index"])
        vecs = normalize(np.asarray([d["embedding"] for d in rows], dtype=np.float32))
        self.dim = vecs.shape[1]
        return vecs


def create(provider: str, spec: dict, api_key: str) -> Embedder:
    cls = {"openai": OpenAIEmbedder, "gemini": GeminiEmbedder, "voyage": VoyageEmbedder}[provider]
    return cls(spec, api_key)
