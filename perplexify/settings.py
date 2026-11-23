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
    "top_k": 30,                  # passages handed to the model
    "candidates": 20,             # candidates pulled from each of vector search and keyword search
    "max_per_doc": 10,            # at most this many passages from one page / file
    "max_excerpt_tokens": 30000,  # hard cap on excerpt size (also capped by the model's window)
    "rerank": True,               # cross-encoder reranking (the small model ships with the installer)
    "reranker_model": "Xenova/ms-marco-MiniLM-L-6-v2",
    "query_rewrite": True,        # turn follow-up questions into standalone search queries
    "multi_query": True,          # ask the model for extra search phrasings (one more model call)
}

# Earlier default sets. The settings file stores every value it was saved with, so a file that still holds one of these
# complete sets was never customised: it follows the current defaults instead of freezing yesterday's.
PREVIOUS_RETRIEVAL_DEFAULTS: tuple[dict[str, Any], ...] = (
    {"top_k": 8, "candidates": 40, "max_per_doc": 3, "max_excerpt_tokens": 12000, "rerank": False, "query_rewrite": True, "multi_query": False},
    {"top_k": 10, "candidates": 20, "max_per_doc": 10, "max_excerpt_tokens": 30000, "rerank": True, "query_rewrite": True, "multi_query": True},
)

PROFILE_FIELDS = ("name", "provider", "kind", "base_url", "model", "context_tokens", "temperature", "max_tokens",
                  "reasoning", "effort", "show_thinking", "use_env_key", "model_path", "gpu")
GPU_MODES = ("auto", "off")     # local models: let llama.cpp use the graphics card when it can, or never


def default_settings() -> dict:
    return {
        "version": 2,
        "active_profile": None,
        "profiles": [],
        "retrieval": dict(RETRIEVAL_DEFAULTS),
        "embedder": dict(DEFAULT_EMBEDDER),
        "embedding_keys": {},
        "server": {"port": 8765, "open_browser": True},
        "local": {"linked": []},     # .gguf files the user chose from elsewhere on the disk (used where they are)
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
    if providers.is_local(provider_id):
        # a model file on this computer: no server address, no key, no reasoning option; the engine is started on demand
        out["base_url"] = ""
        out["use_env_key"] = False
        out["reasoning"] = None
        out["model_path"] = str(p.get("model_path") or "").strip()
        out["gpu"] = p.get("gpu") if p.get("gpu") in GPU_MODES else "auto"
    elif p.get("api_key_enc"):
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


def _clean_local(d: dict) -> dict:
    linked: list[str] = []
    for p in d.get("linked") or []:
        if isinstance(p, str) and p.strip() and p.strip() not in linked:
            linked.append(p.strip())
    return {"linked": linked[:200]}


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
        self._upgrade_keys()

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
            stored = _clean_retrieval({**data["retrieval"], **raw["retrieval"]})
            if any(all(stored[k] == v for k, v in old.items()) for old in PREVIOUS_RETRIEVAL_DEFAULTS):
                stored = dict(RETRIEVAL_DEFAULTS)
            data["retrieval"] = stored
        if isinstance(raw.get("embedder"), dict):
            data["embedder"] = clean_embedder(raw["embedder"])
        if isinstance(raw.get("embedding_keys"), dict):
            data["embedding_keys"] = {k: v for k, v in raw["embedding_keys"].items() if isinstance(v, str)}
        if isinstance(raw.get("server"), dict):
            data["server"].update({k: raw["server"][k] for k in ("port", "open_browser") if k in raw["server"]})
        if isinstance(raw.get("local"), dict):
            data["local"] = _clean_local(raw["local"])
        ids = [p["id"] for p in data["profiles"]]
        ap = raw.get("active_profile")
        data["active_profile"] = ap if ap in ids else (ids[0] if ids else None)
        return data

    def _upgrade_keys(self) -> None:
        """Keys an earlier version stored as readable base64 (`plain:`) move into the OS secret storage."""
        changed = False
        holders = [(p, "api_key_enc") for p in self._data["profiles"]] + [(self._data["embedding_keys"], k) for k in self._data["embedding_keys"]]
        for holder, name in holders:
            old = holder.get(name) or ""
            new = osutil.upgrade(old)
            if new != old:
                holder[name] = new
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            # Created owner-only from the start (a chmod afterwards would leave a window where others can read it).
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(self._data, indent=2))
            os.replace(tmp, self._path)
            try:
                os.chmod(self._path, 0o600)       # also tightens a file an older version left readable
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
            # "has_key" is what the interface reads as "connected": a local model needs no key, so it always counts
            p["has_key"] = bool(p.pop("api_key_enc", "")) or bool(p.get("use_env_key") and env) or providers.is_local(p)
        d["embedding_keys"] = {k: True for k, v in d["embedding_keys"].items() if v}
        return d

    def can_chat(self, profile: dict | None) -> bool:
        """Is this profile usable for a question? Cloud providers need a key; a local model needs nothing but its file."""
        if not profile:
            return False
        return providers.is_local(profile) or bool(self.api_key(profile))

    def is_ready(self) -> bool:
        return any(self.can_chat(p) for p in self.get()["profiles"])

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
                token = osutil.protect(api_key.strip())       # first: if this fails the old key must survive
                osutil.discard(merged.get("api_key_enc", ""))
                merged["api_key_enc"] = token
            profile = sanitize_profile(merged)
            self._data["profiles"] = [p for p in self._data["profiles"] if p["id"] != pid] + [profile]
            self._data["active_profile"] = pid
            self.save()
            return copy.deepcopy(profile)

    def connect_local(self, model: str, model_path: str = "", context_tokens: int | None = None, gpu: str = "auto") -> dict:
        """Create or update the "Local model" profile and make it the active one. `model` is the file name the app shows;
        `model_path` is where the file is when it is not in the app's own model folder (a file the user chose)."""
        with self._lock:
            existing = next((p for p in self._data["profiles"] if p["id"] == "local"), {})
            merged = dict(existing)
            merged.update({"id": "local", "provider": "local", "model": model, "model_path": model_path,
                           "context_tokens": context_tokens, "gpu": gpu})
            profile = sanitize_profile(merged)
            self._data["profiles"] = [p for p in self._data["profiles"] if p["id"] != "local"] + [profile]
            self._data["active_profile"] = "local"
            self.save()
            return copy.deepcopy(profile)

    def linked_models(self) -> list[str]:
        with self._lock:
            return list(self._data["local"]["linked"])

    def link_model(self, path: str) -> None:
        """Remember a .gguf file that stays where it is (it is never copied)."""
        with self._lock:
            if path not in self._data["local"]["linked"]:
                self._data["local"]["linked"].append(path)
                self.save()

    def unlink_model(self, path: str) -> None:
        with self._lock:
            if path in self._data["local"]["linked"]:
                self._data["local"]["linked"].remove(path)
                self.save()

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

    def service_key(self, service: str) -> str:
        """A key saved for a service that is not a chat model (Voyage embeddings, Tavily / Brave web search).
        They share the encrypted `embedding_keys` store, which predates the web search keys."""
        with self._lock:
            return osutil.unprotect(self._data["embedding_keys"].get(service, ""))

    def set_embedding_key(self, provider_id: str, key: str) -> None:
        with self._lock:
            token = osutil.protect(key.strip()) if key.strip() else ""
            osutil.discard(self._data["embedding_keys"].get(provider_id, ""))
            self._data["embedding_keys"][provider_id] = token
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
