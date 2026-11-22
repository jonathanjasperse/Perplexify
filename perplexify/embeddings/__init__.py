"""Pluggable embedding back ends. A library remembers which one built its index (its "spec")."""
from __future__ import annotations

import hashlib
import json
import threading

from .base import Embedder, EmbeddingError

_cache: dict[str, Embedder] = {}
_lock = threading.Lock()


def spec_key(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True)


def _key_for(spec: dict, settings) -> str:
    provider = spec.get("provider", "fastembed")
    return settings.provider_key(provider) if (settings is not None and provider != "fastembed") else ""


def get_embedder(spec: dict, settings=None) -> Embedder:
    """Return a (cached) embedder for `spec`; loading a model can take a moment the first time."""
    key = _key_for(spec, settings)
    cache_key = spec_key(spec) + "|" + hashlib.sha1(key.encode()).hexdigest()[:8]
    with _lock:
        emb = _cache.get(cache_key)
        if emb is None:
            emb = _create(spec, key)
            _cache[cache_key] = emb
        return emb


def _create(spec: dict, key: str) -> Embedder:
    provider = spec.get("provider", "fastembed")
    if provider == "fastembed":
        from .fastembed_provider import FastEmbedEmbedder

        return FastEmbedEmbedder(spec["model"])
    if provider in ("openai", "gemini", "voyage"):
        from . import cloud

        return cloud.create(provider, spec, key)
    raise EmbeddingError(f"Unknown embedding provider '{provider}'")


def validate_embedder(spec: dict, settings=None) -> dict:
    """Embed one short text to prove the model and key work; returns {'dim': n}."""
    emb = get_embedder(spec, settings)
    vec = emb.embed_query("connection test")
    return {"dim": int(len(vec))}


def list_fastembed_models() -> list[dict]:
    from .fastembed_provider import list_models

    return list_models()


__all__ = ["Embedder", "EmbeddingError", "get_embedder", "list_fastembed_models", "spec_key", "validate_embedder"]
