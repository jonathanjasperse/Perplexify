"""User settings: connected cloud models, retrieval options, default embedding choice.

Stored as JSON in the data folder. API keys are encrypted with the operating system's secret storage
(Windows DPAPI / macOS Keychain) and are never sent back to the browser.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from . import osutil, paths, providers
from .textutil import slugify

PROFILE_KINDS = ("anthropic", "openai")
EFFORTS = (None, "low", "medium", "high", "xhigh", "max")
REASONING = (None, "off", "low", "medium", "high", "max")
DEFAULT_EMBEDDER = {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"}

RETRIEVAL_DEFAULTS: dict[str, Any] = {
    "top_k": 8,                   # passages handed to the model
    "candidates": 40,             # candidates pulled from each of vector search and keyword search
    "max_per_doc": 3,             # at most this many passages from one page / file
    "max_excerpt_tokens": 12000,  # hard cap on excerpt size
    "rerank": False,              # cross-encoder reranking (downloads a small model on first use)
    "reranker_model": "Xenova/ms-marco-MiniLM-L-6-v2",
    "query_rewrite": True,        # turn follow-up questions into standalone search queries
    "multi_query": False,         # ask the model for extra search phrasings (one more model call)
}

PROFILE_FIELDS = ("name", "provider", "kind", "base_url", "model", "context_tokens", "temperature", "max_tokens",
                  "reasoning", "effort", "show_thinking", "use_env_key")


def default_settings() -> dict:
    return {
        "version": 2,
        "active_profile": None,
        "profiles": [],
        "retrieval": dict(RETRIEVAL_DEFAULTS),
        "embedder": dict(DEFAULT_EMBEDDER),
        "embedding_keys": {},
        "server": {"port": 8765, "open_browser": True},
    }


def _num(v: Any, lo: float, hi: float, default: Any, integer: bool = False) -> Any:
    if v is None or v == "":
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    x = max(lo, min(hi, x))
    return int(x) if integer else x


def sanitize_profile(p: dict) -> dict:
    prov = providers.get_chat_provider(str(p.get("provider") or "")) or providers.get_chat_provider("custom")
    provider_id = prov["id"]
    kind = prov["kind"]
    base_url = str(p.get("base_url") or "").strip().rstrip("/") or prov["base_url"]
    out: dict[str, Any] = {
        "id": str(p.get("id") or (provider_id if provider_id != "custom" else "custom-" + slugify(p.get("name") or "service"))),
        "provider": provider_id,
        # a built-in provider always shows the catalog name (so a rename in the catalog also reaches profiles saved earlier);
        # only "Other" services carry a name of their own
        "name": prov["name"] if provider_id != "custom" else (str(p.get("name") or prov["name"]).strip()[:60] or prov["name"]),
        "kind": kind,
        "base_url": base_url if kind == "openai" else "",
        "model": str(p.get("model") or "").strip(),
        "context_tokens": _num(p.get("context_tokens"), 1024, 10_000_000, None, True),  # None = ask the provider
        "temperature": _num(p.get("temperature"), 0, 2, None) if kind == "openai" else None,
        "max_tokens": _num(p.get("max_tokens"), 16, 400_000, None, True),
        "use_env_key": bool(p.get("use_env_key")),
    }
    if kind == "openai":
        r = p.get("reasoning") or None
        out["reasoning"] = r if r in REASONING else None
    if kind == "anthropic":
        e = p.get("effort") or None
        out["effort"] = e if e in EFFORTS else None
        out["show_thinking"] = bool(p.get("show_thinking"))
    if p.get("api_key_enc"):
        out["api_key_enc"] = p["api_key_enc"]
    return out


def _clean_retrieval(r: dict) -> dict:
    d = RETRIEVAL_DEFAULTS
    return {
        "top_k": _num(r.get("top_k"), 1, 40, d["top_k"], True),
        "candidates": _num(r.get("candidates"), 10, 200, d["candidates"], True),
        "max_per_doc": _num(r.get("max_per_doc"), 1, 10, d["max_per_doc"], True),
        "max_excerpt_tokens": _num(r.get("max_excerpt_tokens"), 1000, 400_000, d["max_excerpt_tokens"], True),
        "rerank": bool(r.get("rerank", d["rerank"])),
        "reranker_model": str(r.get("reranker_model") or d["reranker_model"]),
        "query_rewrite": bool(r.get("query_rewrite", d["query_rewrite"])),
        "multi_query": bool(r.get("multi_query", d["multi_query"])),
    }


def clean_embedder(e: dict) -> dict:
    prov = e.get("provider") if e.get("provider") in ("fastembed", "openai", "gemini", "voyage") else "fastembed"
    out: dict[str, Any] = {"provider": prov, "model": str(e.get("model") or DEFAULT_EMBEDDER["model"]).strip()}
    if prov != "fastembed":
        if e.get("dimensions"):
            out["dimensions"] = int(_num(e["dimensions"], 64, 4096, 768, True))
        if e.get("flavor") in ("code", "text"):
            out["flavor"] = e["flavor"]
        if e.get("base_url"):
            out["base_url"] = str(e["base_url"]).strip().rstrip("/")
    return out


class Settings:
    def __init__(self, path: Path | None = None):
        self._path = path or paths.settings_path()
        self._lock = threading.RLock()
        self._data = self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> dict:
        data = default_settings()
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw.get("profiles"), list):
            data["profiles"] = [sanitize_profile(p) for p in raw["profiles"]
                                if isinstance(p, dict) and p.get("provider") in {x["id"] for x in providers.CHAT_PROVIDERS}]
        if isinstance(raw.get("retrieval"), dict):
            data["retrieval"] = _clean_retrieval({**data["retrieval"], **raw["retrieval"]})
        if isinstance(raw.get("embedder"), dict):
            data["embedder"] = clean_embedder(raw["embedder"])
        if isinstance(raw.get("embedding_keys"), dict):
            data["embedding_keys"] = {k: v for k, v in raw["embedding_keys"].items() if isinstance(v, str)}
        if isinstance(raw.get("server"), dict):
            data["server"].update({k: raw["server"][k] for k in ("port", "open_browser") if k in raw["server"]})
        ids = [p["id"] for p in data["profiles"]]
        ap = raw.get("active_profile")
        data["active_profile"] = ap if ap in ids else (ids[0] if ids else None)
        return data

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2), "utf-8")
            os.replace(tmp, self._path)
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    # ------------------------------------------------------------------ reads
    def get(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def public(self) -> dict:
        """Settings safe to send to the browser (no secrets)."""
        d = self.get()
        for p in d["profiles"]:
            env = providers.env_key(p["provider"])
            p["has_key"] = bool(p.pop("api_key_enc", "")) or bool(p.get("use_env_key") and env)
        d["embedding_keys"] = {k: True for k, v in d["embedding_keys"].items() if v}
        return d

    def is_ready(self) -> bool:
        return any(self.api_key(p) for p in self.get()["profiles"])

    def profile(self, profile_id: str | None = None) -> dict | None:
        with self._lock:
            profs = self._data["profiles"]
            if not profs:
                return None
            pid = profile_id or self._data["active_profile"]
            for p in profs:
                if p["id"] == pid:
                    return copy.deepcopy(p)
            return copy.deepcopy(profs[0])

    def api_key(self, profile: dict) -> str:
        key = osutil.unprotect(profile.get("api_key_enc", ""))
        if not key:
            env = providers.env_key(profile.get("provider", ""))
            if env:
                key = env[1]
        return key

    def provider_key(self, provider_id: str) -> str:
        """The key for a provider, whether it was typed in, comes from the environment or was saved for embeddings."""
        with self._lock:
            for p in self._data["profiles"]:
                if p["provider"] == provider_id:
                    k = self.api_key(p)
                    if k:
                        return k
            stored = osutil.unprotect(self._data["embedding_keys"].get(provider_id, ""))
        if stored:
            return stored
        env = providers.env_key(provider_id)
        return env[1] if env else ""

    def retrieval(self) -> dict:
        with self._lock:
            return dict(self._data["retrieval"])

    def embedder(self) -> dict:
        with self._lock:
            return dict(self._data["embedder"])

    # ------------------------------------------------------------------ writes
    def connect(self, provider_id: str, api_key: str | None, model: str, base_url: str = "", name: str = "",
                use_env_key: bool = False) -> dict:
        """Create or update the profile for a provider and make it the active one."""
        with self._lock:
            prov = providers.get_chat_provider(provider_id)
            if prov is None:
                raise ValueError("Unknown provider")
            pid = prov["id"] if prov["id"] != "custom" else "custom-" + slugify(name or base_url or "service")
            existing = next((p for p in self._data["profiles"] if p["id"] == pid), {})
            merged = dict(existing)
            merged.update({"id": pid, "provider": prov["id"], "name": name or existing.get("name") or prov["name"],
                           "base_url": base_url or existing.get("base_url") or prov["base_url"],
                           "model": model or existing.get("model") or prov["default_model"],
                           "use_env_key": use_env_key})
            if api_key:
                osutil.discard(merged.get("api_key_enc", ""))
                merged["api_key_enc"] = osutil.protect(api_key.strip())
            profile = sanitize_profile(merged)
            self._data["profiles"] = [p for p in self._data["profiles"] if p["id"] != pid] + [profile]
            self._data["active_profile"] = pid
            self.save()
            return copy.deepcopy(profile)

    def remove_profile(self, profile_id: str) -> None:
        with self._lock:
            for p in self._data["profiles"]:
                if p["id"] == profile_id:
                    osutil.discard(p.get("api_key_enc", ""))
            self._data["profiles"] = [p for p in self._data["profiles"] if p["id"] != profile_id]
            ids = [p["id"] for p in self._data["profiles"]]
            if self._data["active_profile"] not in ids:
                self._data["active_profile"] = ids[0] if ids else None
            self.save()

    def set_embedding_key(self, provider_id: str, key: str) -> None:
        with self._lock:
            osutil.discard(self._data["embedding_keys"].get(provider_id, ""))
            self._data["embedding_keys"][provider_id] = osutil.protect(key.strip()) if key.strip() else ""
            self.save()

    def update(self, body: dict) -> None:
        with self._lock:
            d = self._data
            if isinstance(body.get("retrieval"), dict):
                d["retrieval"] = _clean_retrieval({**d["retrieval"], **body["retrieval"]})
            if isinstance(body.get("embedder"), dict):
                d["embedder"] = clean_embedder(body["embedder"])
            if isinstance(body.get("server"), dict):
                s = body["server"]
                if "port" in s:
                    d["server"]["port"] = _num(s["port"], 1024, 65535, 8765, True)
                if "open_browser" in s:
                    d["server"]["open_browser"] = bool(s["open_browser"])
            if isinstance(body.get("profiles"), list):
                for p in body["profiles"]:
                    if not isinstance(p, dict):
                        continue
                    old = next((x for x in d["profiles"] if x["id"] == p.get("id")), None)
                    if old is None:
                        continue  # new connections go through connect() so the key is validated first
                    merged = dict(old)
                    merged.update({k: p[k] for k in PROFILE_FIELDS if k in p and k not in ("provider", "kind")})
                    d["profiles"][d["profiles"].index(old)] = sanitize_profile(merged)
            ids = [p["id"] for p in d["profiles"]]
            ap = body.get("active_profile", d["active_profile"])
            d["active_profile"] = ap if ap in ids else (ids[0] if ids else None)
            self.save()
