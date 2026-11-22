"""Gemini: chat through Google's OpenAI-compatible endpoint, model list and token limits from the native API."""
from __future__ import annotations

import httpx

from .base import LLMError
from .openai_compat import OpenAICompatProvider, http_message

NATIVE = "https://generativelanguage.googleapis.com/v1beta"
_SKIP = ("embedding", "aqa", "imagen", "veo", "tts", "native-audio", "live", "image", "robotics", "computer-use")


class GeminiProvider(OpenAICompatProvider):
    def list_models(self) -> list[dict]:
        try:
            with self._client(20) as c:
                r = c.get(f"{NATIVE}/models", params={"pageSize": 1000}, headers={"x-goog-api-key": self.api_key})
        except httpx.HTTPError:
            return super().list_models()  # the native list is a nicety; the compat endpoint still works
        if r.status_code in (400, 401, 403):
            raise LLMError(http_message(r.status_code, r.text, self.label))
        if r.status_code != 200:
            return super().list_models()
        out = []
        for m in r.json().get("models", []):
            name = str(m.get("name", "")).removeprefix("models/")
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            if not name or any(h in name for h in _SKIP):
                continue
            out.append({"id": name, "name": m.get("displayName") or name,
                        "context_window": m.get("inputTokenLimit"), "max_output": m.get("outputTokenLimit")})
        return sorted(out, key=lambda m: m["id"], reverse=True) if out else super().list_models()
