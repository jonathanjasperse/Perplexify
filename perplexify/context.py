"""Shared application state (settings, libraries, jobs, retrieval) used by the server, CLI and MCP server."""
from __future__ import annotations

import logging
import threading
import time

from . import paths
from .embeddings import get_embedder, spec_key
from .jobs import JobManager
from .library import LibraryManager
from .llm import LLMProvider, make_provider
from .rag import RagEngine
from .retrieval import Retriever
from .settings import Settings

log = logging.getLogger("perplexify.context")


class AppContext:
    def __init__(self) -> None:
        self.settings = Settings()
        self.libs = LibraryManager()
        self.jobs = JobManager()
        self.retriever = Retriever(self.libs, self.settings)
        self.engine = RagEngine(self)
        self._info: dict[str, tuple[float, dict]] = {}
        self._info_lock = threading.Lock()

    def warm_up(self) -> None:
        """Load the (built-in) embedding model and each library's vector matrix before the first question, in a
        background thread, so the first search does not pay for them. Cloud embedders are never called here."""
        def work() -> None:
            try:
                time.sleep(0.3)
                seen: set[str] = set()
                for lib in self.libs.list():
                    try:
                        lib.store._vectors()
                    except Exception:  # noqa: BLE001
                        log.debug("could not preload vectors for %s", lib.id, exc_info=True)
                    spec = lib.embedder_spec
                    if spec.get("provider") == "fastembed" and spec_key(spec) not in seen:
                        seen.add(spec_key(spec))
                        get_embedder(spec, self.settings).embed_query("warm up")
            except Exception:  # noqa: BLE001 - warming up is only an optimisation
                log.debug("warm-up failed", exc_info=True)

        if not paths.env("NO_WARMUP"):
            threading.Thread(target=work, name="warmup", daemon=True).start()

    def provider(self, profile_id: str | None = None) -> LLMProvider | None:
        profile = self.settings.profile(profile_id)
        if profile is None:
            return None
        return make_provider(profile, self.settings.api_key(profile))

    def llm_info(self, profile: dict, max_age: float = 300.0, force: bool = False) -> dict:
        """Context window of the model behind a profile (cached; feeds the context gauge)."""
        key = f"{profile['id']}|{profile.get('model')}|{profile.get('base_url')}|{profile.get('context_tokens')}"
        with self._info_lock:
            hit = self._info.get(key)
            if hit and not force and time.time() - hit[0] < max_age:
                return hit[1]
        info = make_provider(profile, self.settings.api_key(profile)).info()
        with self._info_lock:
            self._info[key] = (time.time(), info)
        return info
