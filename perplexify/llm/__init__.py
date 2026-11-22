"""Pluggable cloud chat back ends. A "profile" in settings selects one."""
from __future__ import annotations

from .base import Delta, LLMError, LLMProvider, ThinkFilter


def make_provider(profile: dict, api_key: str = "") -> LLMProvider:
    if profile.get("kind") == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider(profile, api_key)
    if profile.get("provider") == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(profile, api_key)
    from .openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(profile, api_key)


__all__ = ["Delta", "LLMError", "LLMProvider", "ThinkFilter", "make_provider"]
