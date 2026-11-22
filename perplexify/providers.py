"""The catalog of cloud providers the app can connect to, and how to recognise a pasted API key.

Chat goes through `kind`: "anthropic" uses the official Anthropic SDK, "openai" speaks the OpenAI-compatible
chat API that OpenAI, Gemini, DeepSeek and most other cloud services offer. Model names change often, so the
defaults here are only a pre-selection: the app always asks the provider for its live model list.
"""
from __future__ import annotations

import os
import re

# The order here is the order of the tiles in the "Connect a model" dialog, and the first one is pre-selected.
# "name" is the product people know (ChatGPT, Claude, Gemini), "company" who runs the API behind it.
CHAT_PROVIDERS: list[dict] = [
    {
        "id": "openai", "name": "ChatGPT", "company": "OpenAI", "kind": "openai",
        "base_url": "https://api.openai.com/v1", "key_url": "https://platform.openai.com/api-keys",
        "env": ["OPENAI_API_KEY"], "key_hint": "sk-proj-...",
        "default_model": "gpt-6.2-sol",
        "preferred": ["gpt-6.2-sol", "gpt-6.1-terra", "gpt-6-luna", "gpt-6.2-astra"],
        "context_default": 1_050_000,
        "model_note": "",
        "embeddings": "openai",
    },
    {
        "id": "anthropic", "name": "Claude", "company": "Anthropic", "kind": "anthropic",
        "base_url": "", "key_url": "https://platform.claude.com/settings/keys",
        "env": ["ANTHROPIC_API_KEY"], "key_hint": "sk-ant-...",
        "default_model": "claude-opus-5-5",
        "preferred": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
        "context_default": 200_000,
        "model_note": "Sonnet and Haiku are cheaper and plenty for most questions about a manual.",
        "embeddings": None,
        "embeddings_note": "Anthropic has no embeddings API, so the built-in embedding model is used (Voyage AI is the recommended cloud alternative).",
    },
    {
        "id": "gemini", "name": "Gemini", "company": "Google", "kind": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_url": "https://aistudio.google.com/apikey",
        "env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"], "key_hint": "AIza...",
        "default_model": "gemini-3.8-flash",
        "preferred": ["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"],
        "context_default": 1_048_576,
        "model_note": "Flash models are fast and inexpensive; the larger models reason more deeply.",
        "embeddings": "gemini",
    },
    {
        "id": "deepseek", "name": "DeepSeek", "company": "DeepSeek AI", "kind": "openai",
        "base_url": "https://api.deepseek.com",
        "key_url": "https://platform.deepseek.com/api_keys",
        "env": ["DEEPSEEK_API_KEY"], "key_hint": "sk-...",
        "default_model": "deepseek-flash",
        "preferred": ["deepseek-flash", "deepseek-v4-pro"],
        "context_default": 1_048_576,
        "model_note": "Reasoning is on by default and billed as output; turn it off in Settings for quicker, cheaper answers.",
        "embeddings": None,
        "embeddings_note": "DeepSeek has no embeddings API, so the built-in embedding model is used.",
    },
    {
        "id": "custom", "name": "Other", "company": "OpenAI-compatible service", "kind": "openai",
        "base_url": "", "key_url": "", "env": [], "key_hint": "your service's key",
        "default_model": "", "preferred": [], "context_default": 128_000,
        "model_note": "Any cloud service with an OpenAI-compatible chat API (OpenRouter, Groq, xAI, Mistral, Together...).",
        "embeddings": None,
        "quick_fill": [
            {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1"},
            {"name": "Groq", "base_url": "https://api.groq.com/openai/v1"},
            {"name": "xAI", "base_url": "https://api.x.ai/v1"},
            {"name": "Mistral", "base_url": "https://api.mistral.ai/v1"},
            {"name": "Together", "base_url": "https://api.together.xyz/v1"},
        ],
    },
]

# Embedding options. "builtin" needs nothing; the others use a cloud key (reusing the connected provider's key
# where there is one). Dimensions are kept moderate: bigger vectors cost memory and search time for little gain.
EMBEDDING_OPTIONS: list[dict] = [
    {"id": "builtin", "provider": "fastembed", "model": "BAAI/bge-small-en-v1.5", "name": "Built-in (automatic)",
     "key": None, "note": "Runs inside the app: no key, no cost, works offline. Slower for very large libraries."},
    {"id": "gemini-embedding-2", "provider": "gemini", "model": "gemini-embedding-2", "dimensions": 768,
     "name": "Gemini Embedding 2", "key": "gemini", "note": "Fast and accurate. Uses your Gemini key."},
    {"id": "gemini-embedding-001", "provider": "gemini", "model": "gemini-embedding-001", "dimensions": 768,
     "name": "Gemini Embedding 001", "key": "gemini", "note": "Text-only model with a code-retrieval mode."},
    {"id": "openai-small", "provider": "openai", "model": "text-embedding-3-small", "name": "OpenAI text-embedding-3-small",
     "key": "openai", "note": "Inexpensive. Uses your ChatGPT (OpenAI) key."},
    {"id": "openai-large", "provider": "openai", "model": "text-embedding-3-large", "dimensions": 1024,
     "name": "OpenAI text-embedding-3-large", "key": "openai", "note": "Higher quality. Uses your ChatGPT (OpenAI) key."},
    {"id": "voyage-4", "provider": "voyage", "model": "voyage-4", "name": "Voyage 4", "key": "voyage",
     "note": "Recommended by Anthropic for Claude users. Needs a Voyage AI key (free tier available)."},
    {"id": "voyage-code-4", "provider": "voyage", "model": "voyage-code-4", "name": "Voyage Code 4", "key": "voyage",
     "note": "Tuned for source code. Needs a Voyage AI key."},
]

VOYAGE = {"id": "voyage", "name": "Voyage AI", "key_url": "https://dashboard.voyageai.com/organization/api-keys",
          "env": ["VOYAGE_API_KEY"], "key_hint": "pa-..."}

_PREFIX_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^sk-ant-"), "anthropic"),
    (re.compile(r"^AIza[0-9A-Za-z_\-]{20,}$"), "gemini"),
    (re.compile(r"^sk-(proj|svcacct|admin)-"), "openai"),
    (re.compile(r"^sk-[0-9a-f]{32}$"), "deepseek"),
]


def get_chat_provider(provider_id: str) -> dict | None:
    """Find a provider by its id ("openai") or by the name the app shows ("ChatGPT", "Claude"), in any letter case."""
    key = (provider_id or "").strip().lower()
    return (next((p for p in CHAT_PROVIDERS if p["id"] == key), None)
            or next((p for p in CHAT_PROVIDERS if p["name"].lower() == key), None))


def detect_provider(key: str) -> str | None:
    """Best guess from the key's shape. Never sends the key anywhere; None when it is ambiguous."""
    key = key.strip()
    for pattern, pid in _PREFIX_RULES:
        if pattern.search(key):
            return pid
    return None


def env_key(provider_id: str) -> tuple[str, str] | None:
    """(variable name, value) when the provider's usual environment variable is set."""
    prov = get_chat_provider(provider_id) or (VOYAGE if provider_id == "voyage" else None)
    for name in (prov or {}).get("env", []):
        value = os.environ.get(name, "").strip()
        if value:
            return name, value
    return None


def pick_default_model(provider_id: str, available: list[str]) -> str:
    """Choose a sensible model from a live list: the catalog's preference if offered, else the first."""
    prov = get_chat_provider(provider_id) or {}
    for want in [prov.get("default_model", "")] + list(prov.get("preferred", [])):
        if want and want in available:
            return want
    return available[0] if available else prov.get("default_model", "")


def public_catalog() -> list[dict]:
    """What the browser needs to draw the provider picker (no secrets; says which keys the environment has)."""
    out = []
    for p in CHAT_PROVIDERS:
        env = env_key(p["id"])
        out.append({**{k: v for k, v in p.items() if k not in ("env",)}, "env_var": env[0] if env else None})
    return out
