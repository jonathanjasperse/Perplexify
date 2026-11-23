"""OpenAI-compatible chat endpoints: OpenAI, DeepSeek, Gemini (compat layer) and other cloud services."""
from __future__ import annotations

import json
from typing import Iterator

import httpx

from .base import Delta, LLMError, LLMProvider, ThinkFilter

_NON_CHAT = ("embedding", "embed", "whisper", "tts", "dall-e", "moderation", "davinci", "babbage", "transcribe",
             "image", "audio", "realtime", "sora", "rerank")
_CTX_KEYS = ("context_window", "context_length", "max_context_length", "inputTokenLimit", "max_model_len", "n_ctx")
_OUT_KEYS = ("max_output_tokens", "max_completion_tokens", "outputTokenLimit", "max_output")


def http_message(status: int, body: str, label: str) -> str:
    detail = body.strip()
    try:
        j = json.loads(body)
        err = j.get("error", j) if isinstance(j, dict) else j[0].get("error", j[0])
        detail = err.get("message", detail) if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError, IndexError, TypeError):
        pass
    low = detail.lower()
    if status in (401, 403) or (status == 400 and "api key" in low and ("not valid" in low or "invalid" in low)):
        return f"{label} rejected the API key. Check that it was copied completely and has not been revoked."
    if status == 402:
        return f"{label} says the account has no remaining balance (402). Add credit and try again."
    if status == 404:
        return f"{label} could not find that model or endpoint (404). {detail[:200]}"
    if status == 429:
        return f"{label} rate limit or quota reached (429). Wait a moment, or check your plan. {detail[:200]}"
    return f"{label} returned {status}: {detail[:400]}"


def _pick(d: dict, keys: tuple[str, ...]) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, int) and v > 0:
            return v
    return None


class OpenAICompatProvider(LLMProvider):
    kind = "openai"
    QUICK_MAX_TOKENS: int | None = None     # a subclass can cap the short helper calls (query rewriting) lower than the default below

    @property
    def base(self) -> str:
        return (self.profile.get("base_url") or "").rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _client(self, read: float = 600) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(connect=10, read=read, write=30, pool=5))

    def _net_error(self, e: Exception) -> LLMError:
        if isinstance(e, httpx.ConnectError):
            return LLMError(f"Could not reach {self.label}. Check your internet connection.")
        if isinstance(e, httpx.TimeoutException):
            return LLMError(f"{self.label} took too long to respond.")
        return LLMError(f"Request to {self.label} failed: {e}")

    # ------------------------------------------------------------------ request building
    def _reasoning(self, quick: bool) -> dict:
        r = self.profile.get("reasoning")
        if self.provider_id == "deepseek":
            if quick or r == "off":
                return {"thinking": {"type": "disabled"}}
            if r in ("low", "medium", "high", "max"):
                return {"reasoning_effort": "high" if r == "medium" else r}
            return {}
        if quick:
            return {"reasoning_effort": "low"} if self.provider_id in ("openai", "gemini") else {}
        if r in ("low", "medium", "high"):
            return {"reasoning_effort": r}
        if r == "max":
            return {"reasoning_effort": "high"}
        return {}

    @staticmethod
    def _adjust(body: dict, err: str) -> bool:
        """Services disagree about optional parameters; drop or rename whatever this one rejects."""
        e = err.lower()
        for key in ("reasoning_effort", "thinking", "stream_options"):
            if key in body and key in e:
                body.pop(key)
                return True
        if "max_tokens" in body and ("max_tokens" in e or "max_completion_tokens" in e):
            body["max_completion_tokens"] = body.pop("max_tokens")
            return True
        if "max_completion_tokens" in body and "max_completion_tokens" in e:
            body.pop("max_completion_tokens")
            return True
        if "temperature" in body and "temperature" in e:
            body.pop("temperature")
            return True
        return False

    def stream(self, messages: list[dict], system: str | None = None, *, quick: bool = False) -> Iterator[Delta]:
        p = self.profile
        if not self.model:
            raise LLMError("No model is selected. Choose one in Settings > Models.")
        if not self.base:
            raise LLMError("No server address is set for this service. Add it in Settings > Models.")
        body: dict = {"model": self.model, "stream": True, "stream_options": {"include_usage": True},
                      "messages": ([{"role": "system", "content": system}] if system else []) + messages}
        body.update(self._reasoning(quick))
        if quick:
            # reasoning models spend part of the cap on thinking, so keep generous room for the visible answer
            body["max_tokens"] = self.QUICK_MAX_TOKENS or (300 if self.provider_id == "deepseek" else 2000)
        else:
            if p.get("temperature") is not None:
                body["temperature"] = p["temperature"]
            if p.get("max_tokens"):
                body["max_tokens"] = int(p["max_tokens"])
        tf = ThinkFilter()
        attempts = 0
        try:
            with self._client() as c:
                while True:
                    with c.stream("POST", f"{self.base}/chat/completions", json=body, headers=self._headers()) as r:
                        if r.status_code >= 400:
                            text = r.read().decode("utf-8", "replace")
                            if r.status_code == 400 and attempts < 5 and self._adjust(body, text):
                                attempts += 1
                                continue
                            raise LLMError(http_message(r.status_code, text, self.label))
                        for line in r.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                obj = json.loads(payload)
                            except ValueError:
                                continue
                            if obj.get("usage"):
                                u = obj["usage"]
                                yield Delta("usage", data={"prompt_tokens": u.get("prompt_tokens"),
                                                           "completion_tokens": u.get("completion_tokens")})
                            for choice in obj.get("choices") or []:
                                d = choice.get("delta") or {}
                                think = d.get("reasoning_content") or d.get("reasoning")
                                if think:
                                    yield Delta("thinking", think)
                                if d.get("content"):
                                    yield from tf.feed(d["content"])
                                if choice.get("finish_reason") == "length":
                                    yield Delta("info", "The answer was cut off at the model's output limit.")
                        yield from tf.flush()
                        return
        except httpx.HTTPError as e:
            raise self._net_error(e) from e

    # ------------------------------------------------------------------ models
    def list_models(self) -> list[dict]:
        if not self.base:
            raise LLMError("No server address is set for this service.")
        try:
            with self._client(20) as c:
                r = c.get(f"{self.base}/models", headers=self._headers())
        except httpx.HTTPError as e:
            raise self._net_error(e) from e
        if r.status_code >= 400:
            raise LLMError(http_message(r.status_code, r.text, self.label))
        out = []
        for m in r.json().get("data", []):
            mid = str(m.get("id", ""))
            if mid.startswith("models/"):
                mid = mid[len("models/"):]
            if mid and not any(h in mid.lower() for h in _NON_CHAT):
                out.append({"id": mid, "name": m.get("name") or mid,
                            "context_window": _pick(m, _CTX_KEYS), "max_output": _pick(m, _OUT_KEYS)})
        return sorted(out, key=lambda m: m["id"].lower())

    def info(self) -> dict:
        out = super().info()
        try:
            for m in self.list_models():
                if m["id"] == self.model:
                    if m.get("context_window"):
                        out["max_context"] = m["context_window"]
                        if not self.profile.get("context_tokens"):
                            out["context_window"], out["context_source"] = m["context_window"], "api"
                    out["max_output"] = m.get("max_output")
                    break
        except LLMError:
            pass
        return out
