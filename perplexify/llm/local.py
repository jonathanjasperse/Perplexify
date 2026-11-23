"""Local models: chat with a llama.cpp server running on this computer.

llama.cpp's server speaks the same chat API as OpenAI, so this reuses the OpenAI-compatible client. What is different is
where it talks to: the engine is started on demand (perplexify/localllm.py) and listens on a new free port, behind a new secret key,
every time, so the address and the key are looked up when a question is asked and not stored in the profile.
"""
from __future__ import annotations

import re
from typing import Iterator

import httpx

from .. import localllm, providers
from .base import Delta, LLMError
from .openai_compat import OpenAICompatProvider

# A documentation assistant should stick to the excerpts; llama.cpp's own default (0.8) makes small models wander.
DEFAULT_TEMPERATURE = 0.3
_TOO_LONG = re.compile(r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)")


class _TrackedClient(httpx.Client):
    """An httpx client that the web request's scope can close from outside (see localllm.RequestScope): when the browser stops
    reading an answer the connection to llama.cpp is closed and the engine stops generating."""

    def __init__(self, scope: "localllm.RequestScope | None", **kw):
        self._scope = scope
        super().__init__(**kw)
        if scope is not None:
            scope.add(self)

    def close(self) -> None:
        super().close()
        if self._scope is not None:
            self._scope.discard(self)


class LocalProvider(OpenAICompatProvider):
    kind = "openai"
    # The short helper calls (a standalone search query, extra phrasings) need a line or three. A small model that does not stop
    # would otherwise be left to write 2000 tokens before the question could even be searched.
    QUICK_MAX_TOKENS = 192

    def __init__(self, profile: dict, api_key: str = ""):
        super().__init__(profile, api_key)
        self._handle: localllm.EngineHandle | None = None

    @property
    def label(self) -> str:
        return "The local model"

    # ------------------------------------------------------------------ the engine behind this profile
    def spec(self) -> localllm.EngineSpec:
        return localllm.spec_for_profile(self.profile)

    def begin(self) -> localllm._Attempt:
        """Start the engine if it is not running (without waiting). The attempt says when it is ready or why it failed."""
        return localllm.manager().start(self.spec())

    def engine(self) -> localllm.EngineHandle:
        return self.begin().result()

    @property
    def base(self) -> str:
        return self._handle.base_url if self._handle else ""

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._handle.api_key}"} if self._handle else {}

    def _client(self, read: float = 3600) -> httpx.Client:
        # reading a long prompt on a CPU can take many minutes before the first word arrives; and never go through a proxy
        return _TrackedClient(localllm.current_scope.get(), timeout=httpx.Timeout(connect=10, read=max(read, 3600), write=30, pool=5),
                              trust_env=False)

    def _net_error(self, e: Exception) -> LLMError:
        scope = localllm.current_scope.get()
        if scope is not None and scope.cancelled:
            return LLMError("The question was stopped.")          # the browser closed the stream: this is not a crash of the engine
        if isinstance(e, (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)):
            localllm.manager().note_dead()
            return localllm.LocalLLMError("The local model stopped responding (llama.cpp may have crashed or run out of memory). "
                                          "Ask again to start it afresh; if it keeps happening choose a smaller model or a smaller "
                                          f"context size. Log: {localllm.manager().status().get('log', '')}", "engine_died")
        if isinstance(e, httpx.TimeoutException):
            return LLMError("The local model took too long to respond.")
        return LLMError(f"Request to the local model failed: {e}")

    # ------------------------------------------------------------------ request building
    def _reasoning(self, quick: bool) -> dict:
        """No reasoning or effort options for a local model. These are the settings of a local request instead: a low temperature,
        and for the short helper calls (rewriting a query) no thinking phase for models that have one."""
        extra: dict = {"temperature": DEFAULT_TEMPERATURE}
        if quick:
            extra["chat_template_kwargs"] = {"enable_thinking": False}
        return extra

    def stream(self, messages: list[dict], system: str | None = None, *, quick: bool = False) -> Iterator[Delta]:
        self._handle = self.engine()                  # starts llama.cpp the first time; raises LocalLLMError with a plain message
        try:
            yield from super().stream(messages, system, quick=quick)
        except LLMError as e:
            m = _TOO_LONG.search(str(e))
            if m:
                raise LLMError(f"The question and its excerpts ({int(m.group(1)):,} tokens) do not fit the model's window "
                               f"({int(m.group(2)):,} tokens). Choose a larger context size in Settings > Models, or lower "
                               "Passages per question in Settings > Search.") from e
            raise
        except RuntimeError as e:
            scope = localllm.current_scope.get()
            if scope is not None and scope.cancelled:               # httpx: "the client has been closed" (the browser stopped reading)
                raise LLMError("The question was stopped.") from e
            raise

    def count_tokens(self, text: str) -> int | None:
        """How many tokens the running engine makes of `text` (None when it is not running or cannot say)."""
        try:
            h = localllm.manager().current(self.spec())
            if h is None:
                return None
            r = httpx.post(f"http://127.0.0.1:{h.port}/tokenize", json={"content": text, "add_special": False},
                           headers={"Authorization": f"Bearer {h.api_key}"}, timeout=30, trust_env=False)
            if r.status_code != 200:
                return None
            toks = r.json().get("tokens")
            return len(toks) if isinstance(toks, list) else None
        except (LLMError, httpx.HTTPError, ValueError):
            return None

    # ------------------------------------------------------------------ models: the user's files
    def list_models(self) -> list[dict]:
        out = []
        entries = localllm.installed_models([])
        own = localllm.resolve_model_path(self.profile)
        if own is not None and all(e["path"] != str(own) for e in entries):
            entries.append(localllm.model_entry(own, linked=True))
        for e in entries:
            if e["valid"]:
                out.append({"id": e["id"], "name": e["title"], "context_window": e["context_length"], "max_output": None, "size": e["size"]})
        return out

    def info(self) -> dict:
        prov = providers.get_chat_provider("local") or {}
        out = {"kind": self.kind, "provider": "local", "model": self.model, "context_window": int(prov.get("context_default") or 8192),
               "max_context": None, "max_output": None, "context_source": "automatic", "local": True, "engine": "stopped"}
        try:
            spec = self.spec()
        except LLMError as e:
            out["error"] = str(e)
            return out
        h = localllm.manager().current(spec)
        out["context_window"] = h.n_ctx if h else spec.n_ctx            # what the engine reports, else what it will be started with
        out["max_context"] = localllm.read_gguf_info(spec.model_path).get("context_length")
        out["context_source"] = "setting" if self.profile.get("context_tokens") else "automatic"
        out["engine"] = "running" if h else "stopped"
        return out

    def ping(self) -> str:
        n = len(self.list_models())
        return f"{n} local model file{'s' if n != 1 else ''} found."
