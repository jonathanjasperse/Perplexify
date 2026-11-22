"""Common interface for chat models (all cloud providers)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .. import providers


class LLMError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


@dataclass
class Delta:
    kind: str  # "text" | "thinking" | "usage" | "refusal" | "info"
    text: str = ""
    data: dict | None = None


class ThinkFilter:
    """Splits <think>...</think> reasoning out of a text stream (some services inline it)."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.in_think = False
        self._strip_lead = False

    def _emit(self, text: str, out: list[Delta]) -> None:
        if not text:
            return
        if self.in_think:
            out.append(Delta("thinking", text))
            return
        if self._strip_lead:
            text = text.lstrip("\r\n")
            if not text:
                return
            self._strip_lead = False
        out.append(Delta("text", text))

    def feed(self, s: str) -> list[Delta]:
        self.buf += s
        out: list[Delta] = []
        while True:
            tag = self.CLOSE if self.in_think else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                self._emit(self.buf[:i], out)
                self.buf = self.buf[i + len(tag):]
                self.in_think = not self.in_think
                self._strip_lead = not self.in_think
                continue
            keep = 0
            for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
                if self.buf.endswith(tag[:k]):
                    keep = k
                    break
            self._emit(self.buf[:len(self.buf) - keep], out)
            self.buf = self.buf[len(self.buf) - keep:]
            return out

    def flush(self) -> list[Delta]:
        out: list[Delta] = []
        self._emit(self.buf, out)
        self.buf = ""
        return out


class LLMProvider:
    kind = "base"

    def __init__(self, profile: dict, api_key: str = ""):
        self.profile = profile
        self.api_key = api_key

    @property
    def model(self) -> str:
        return self.profile.get("model") or ""

    @property
    def provider_id(self) -> str:
        return self.profile.get("provider") or ""

    @property
    def label(self) -> str:
        prov = providers.get_chat_provider(self.provider_id)
        return (prov or {}).get("name") or self.profile.get("name") or "the provider"

    def stream(self, messages: list[dict], system: str | None = None, *, quick: bool = False) -> Iterator[Delta]:
        """Yield Deltas. `messages` are user/assistant turns only; `quick=True` is a short utility call
        (query rewriting) that should skip reasoning and answer in a few tokens."""
        raise NotImplementedError

    def complete(self, messages: list[dict], system: str | None = None, *, quick: bool = True) -> str:
        return "".join(d.text for d in self.stream(messages, system, quick=quick) if d.kind == "text").strip()

    def list_models(self) -> list[dict]:
        """[{"id", "name", "context_window"?, "max_output"?}] straight from the provider."""
        raise NotImplementedError

    def info(self) -> dict:
        """What the context gauge needs: the effective context window and the model's own maximum."""
        prov = providers.get_chat_provider(self.provider_id) or {}
        override = self.profile.get("context_tokens")
        window = override or prov.get("context_default") or 128_000
        return {"kind": self.kind, "provider": self.provider_id, "model": self.model,
                "context_window": int(window), "max_context": None, "max_output": None,
                "context_source": "setting" if override else "default"}

    def ping(self) -> str:
        n = len(self.list_models())
        return f"Connected to {self.label}. {n} model{'s' if n != 1 else ''} available."
