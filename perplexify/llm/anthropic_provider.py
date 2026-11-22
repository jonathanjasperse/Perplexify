"""Claude through the official Anthropic SDK.

Notes that matter for current Claude models: no sampling parameters are sent (they are rejected),
thinking is left to the model's default (optionally shown as a summary), `effort` is passed through
`output_config`, and a refusal ends the turn with stop_reason == "refusal".
"""
from __future__ import annotations

import inspect
import os
from typing import Iterator

from .base import Delta, LLMError, LLMProvider

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models for which Anthropic recommends opting in to server-side refusal fallbacks.
_FALLBACK_MODELS = ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5-5")


def _wants_fallbacks(model: str) -> bool:
    return model.startswith(_FALLBACK_MODELS)


class AnthropicProvider(LLMProvider):
    kind = "anthropic"

    def _client(self):
        import anthropic

        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not key:
            raise LLMError("No Anthropic API key is set. Add one in Settings > Models.")
        return anthropic.Anthropic(api_key=key, timeout=600.0, max_retries=2)

    def _friendly(self, e: Exception) -> LLMError:
        import anthropic

        if isinstance(e, anthropic.AuthenticationError):
            return LLMError("Anthropic rejected the API key. Check it in Settings > Models.")
        if isinstance(e, anthropic.PermissionDeniedError):
            return LLMError("This API key is not allowed to use that model or feature.")
        if isinstance(e, anthropic.NotFoundError):
            return LLMError(f"Model '{self.model}' was not found for this API key.")
        if isinstance(e, anthropic.RateLimitError):
            return LLMError("Anthropic rate limit reached. Wait a moment and try again.")
        if isinstance(e, anthropic.APIConnectionError):
            return LLMError("Could not reach api.anthropic.com. Check your internet connection.")
        if isinstance(e, anthropic.APIStatusError):
            return LLMError(f"Anthropic API error {e.status_code}: {getattr(e, 'message', e)}")
        return LLMError(f"Anthropic request failed: {e}")

    def stream(self, messages: list[dict], system: str | None = None, *, quick: bool = False) -> Iterator[Delta]:
        import anthropic

        p = self.profile
        if not self.model:
            raise LLMError("No Claude model is selected. Pick one in Settings > Models.")
        client = self._client()
        kwargs: dict = {"model": self.model, "messages": messages,
                        "max_tokens": 300 if quick else int(p.get("max_tokens") or 16000)}
        if system:
            kwargs["system"] = system
        if quick:
            kwargs["output_config"] = {"effort": "low"}
        else:
            if p.get("effort"):
                kwargs["output_config"] = {"effort": p["effort"]}
            if p.get("show_thinking"):
                kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}

        use_beta = (not quick and _wants_fallbacks(self.model)
                    and "fallbacks" in inspect.signature(client.beta.messages.stream).parameters)
        try:
            if use_beta:
                ctx = client.beta.messages.stream(betas=[_FALLBACK_BETA], fallbacks="default", **kwargs)
            else:
                ctx = client.messages.stream(**kwargs)
            with ctx as stream:
                for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    d = event.delta
                    if d.type == "text_delta":
                        yield Delta("text", d.text)
                    elif d.type == "thinking_delta":
                        yield Delta("thinking", d.thinking)
                final = stream.get_final_message()
        except anthropic.APIError as e:
            raise self._friendly(e) from e

        u = getattr(final, "usage", None)
        if u is not None:
            yield Delta("usage", data={"prompt_tokens": getattr(u, "input_tokens", None),
                                       "completion_tokens": getattr(u, "output_tokens", None)})
        if final.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            cat = getattr(details, "category", None) if details else None
            yield Delta("refusal", getattr(details, "explanation", None) or
                        "The model declined to answer this request." + (f" (category: {cat})" if cat else ""))
        elif final.stop_reason == "max_tokens":
            yield Delta("info", "The answer was cut off at the maximum output length.")

    def list_models(self) -> list[dict]:
        import anthropic

        try:
            out = []
            for m in self._client().models.list(limit=100):
                out.append({"id": m.id, "name": getattr(m, "display_name", None) or m.id,
                            "context_window": getattr(m, "max_input_tokens", None),
                            "max_output": getattr(m, "max_tokens", None)})
                if len(out) >= 200:
                    break
            return out
        except anthropic.APIError as e:
            raise self._friendly(e) from e

    def info(self) -> dict:
        out = super().info()
        try:
            m = self._client().models.retrieve(self.model)
            mi = getattr(m, "max_input_tokens", None)
            if mi:
                out["max_context"] = int(mi)
                if not self.profile.get("context_tokens"):
                    out["context_window"], out["context_source"] = int(mi), "api"
            out["max_output"] = getattr(m, "max_tokens", None)
        except Exception:  # noqa: BLE001 - the gauge falls back to the catalog default
            pass
        return out
