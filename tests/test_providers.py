import json
import unittest
from pathlib import Path

import httpx

from helpers import TempHome, sse

from perplexify import providers
from perplexify.llm import LLMError, make_provider
from perplexify.llm.base import ThinkFilter
from perplexify.llm.openai_compat import OpenAICompatProvider
from perplexify.settings import sanitize_profile


def profile(provider: str, **kw) -> dict:
    return sanitize_profile({"id": provider, "provider": provider, "model": kw.pop("model", "m1"), **kw})


def with_transport(provider, handler):
    provider._client = lambda read=600: httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    return provider


class ProviderCatalog(unittest.TestCase):
    def test_key_detection_never_guesses_between_lookalikes(self):
        self.assertEqual(providers.detect_provider("sk-ant-api03-abcdefghijklmnop"), "anthropic")
        self.assertEqual(providers.detect_provider("AIzaSyA1234567890123456789012345678901"), "gemini")
        self.assertEqual(providers.detect_provider("sk-proj-abcdefghijklmnopqrstuvwxyz"), "openai")
        self.assertEqual(providers.detect_provider("sk-" + "a1" * 16), "deepseek")
        self.assertIsNone(providers.detect_provider("sk-short"))
        self.assertIsNone(providers.detect_provider("totally-unknown"))

    def test_default_model_prefers_catalog_choice_then_first(self):
        self.assertEqual(providers.pick_default_model("deepseek", ["zzz", "deepseek-v4-pro", "deepseek-flash"]), "deepseek-flash")
        self.assertEqual(providers.pick_default_model("deepseek", ["a", "b"]), "a")
        self.assertEqual(providers.pick_default_model("anthropic", []), "claude-opus-5-5")

    def test_chatgpt_comes_first_and_is_credited_to_openai(self):
        first = providers.CHAT_PROVIDERS[0]
        self.assertEqual((first["id"], first["name"], first["company"]), ("openai", "ChatGPT", "OpenAI"))
        self.assertEqual([p["name"] for p in providers.CHAT_PROVIDERS][:4], ["ChatGPT", "Claude", "Gemini", "DeepSeek"])
        for p in providers.CHAT_PROVIDERS:  # no tile may read "OpenAI / OpenAI" or any other repeated label
            self.assertNotEqual(p["name"], p["company"])

    def test_providers_are_found_by_id_or_by_the_name_shown_in_the_app(self):
        for query, want in [("openai", "openai"), ("ChatGPT", "openai"), ("chatgpt", "openai"), ("Claude", "anthropic"),
                            ("anthropic", "anthropic"), (" gemini ", "gemini"), ("custom", "custom")]:
            self.assertEqual(providers.get_chat_provider(query)["id"], want, query)
        self.assertIsNone(providers.get_chat_provider("nonsense"))
        self.assertIsNone(providers.get_chat_provider(""))

    def test_saved_profiles_show_the_catalog_name_but_other_services_keep_their_own(self):
        self.assertEqual(sanitize_profile({"provider": "openai", "name": "OpenAI"})["name"], "ChatGPT")  # saved before the rename
        self.assertEqual(sanitize_profile({"provider": "anthropic"})["name"], "Claude")
        self.assertEqual(sanitize_profile({"provider": "custom", "name": "groq.com", "base_url": "https://api.groq.com/openai/v1"})["name"], "groq.com")

    def test_claude_and_deepseek_declare_no_embeddings_api(self):
        by_id = {p["id"]: p for p in providers.CHAT_PROVIDERS}
        self.assertIsNone(by_id["anthropic"]["embeddings"])
        self.assertIsNone(by_id["deepseek"]["embeddings"])
        self.assertEqual(by_id["gemini"]["embeddings"], "gemini")


class OpenAICompat(unittest.TestCase):
    def test_streaming_text_usage_and_request_shape(self):
        seen = {}

        def handler(request: httpx.Request):
            seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, content=sse(
                {"choices": [{"delta": {"content": "Hel"}}]},
                {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2}}))

        p = with_transport(make_provider(profile("openai", model="gpt-x"), "sk-test"), handler)
        deltas = list(p.stream([{"role": "user", "content": "hi"}], "be brief"))
        self.assertEqual("".join(d.text for d in deltas if d.kind == "text"), "Hello")
        usage = next(d for d in deltas if d.kind == "usage")
        self.assertEqual((usage.data["prompt_tokens"], usage.data["completion_tokens"]), (11, 2))
        self.assertEqual(seen["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(seen["auth"], "Bearer sk-test")
        self.assertEqual(seen["body"]["messages"][0], {"role": "system", "content": "be brief"})
        self.assertTrue(seen["body"]["stream"])

    def test_deepseek_reasoning_is_separated_and_quick_calls_disable_thinking(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, content=sse(
                {"choices": [{"delta": {"reasoning_content": "thinking..."}}]},
                {"choices": [{"delta": {"content": "42"}}]}))

        p = with_transport(make_provider(profile("deepseek", model="deepseek-flash"), "k"), handler)
        deltas = list(p.stream([{"role": "user", "content": "q"}]))
        self.assertEqual([d.kind for d in deltas], ["thinking", "text"])
        list(p.stream([{"role": "user", "content": "q"}], quick=True))
        self.assertEqual(bodies[1]["thinking"], {"type": "disabled"})
        self.assertNotIn("thinking", bodies[0])
        p.profile["reasoning"] = "max"
        list(p.stream([{"role": "user", "content": "q"}]))
        self.assertEqual(bodies[2]["reasoning_effort"], "max")

    def test_rejected_optional_parameters_are_dropped_and_retried(self):
        calls = []

        def handler(request):
            body = json.loads(request.content)
            calls.append(sorted(body))
            if "stream_options" in body:
                return httpx.Response(400, json={"error": {"message": "Unknown parameter: 'stream_options'."}})
            if "max_tokens" in body:
                return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."}})
            return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "ok"}}]}))

        p = with_transport(make_provider(profile("openai", model="o", max_tokens=500), "k"), handler)
        self.assertEqual("".join(d.text for d in p.stream([{"role": "user", "content": "x"}])), "ok")
        self.assertEqual(len(calls), 3)
        self.assertIn("max_completion_tokens", calls[-1])

    def test_inline_think_tags_become_thinking(self):
        f = ThinkFilter()
        out = f.feed("<thi") + f.feed("nk>plan</think>\n\nAnswer") + f.flush()
        self.assertEqual([(d.kind, d.text) for d in out], [("thinking", "plan"), ("text", "Answer")])

    def test_friendly_errors(self):
        p = with_transport(make_provider(profile("openai"), "bad"), lambda r: httpx.Response(401, json={"error": {"message": "nope"}}))
        with self.assertRaises(LLMError) as cm:
            list(p.stream([{"role": "user", "content": "x"}]))
        self.assertIn("rejected the API key", str(cm.exception))
        p = with_transport(make_provider(profile("openai"), "k"), lambda r: httpx.Response(402, json={"error": {"message": "x"}}))
        with self.assertRaises(LLMError) as cm:
            p.list_models()
        self.assertIn("balance", str(cm.exception))

    def test_deepseek_model_list_provides_context_window(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/models")
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "deepseek-flash", "context_window": 1048576, "max_output_tokens": 393216},
                {"id": "deepseek-v4-pro", "context_window": 1048576, "max_output_tokens": 393216}]})

        p = with_transport(make_provider(profile("deepseek", model="deepseek-flash"), "k"), handler)
        info = p.info()
        self.assertEqual(info["context_window"], 1048576)
        self.assertEqual(info["context_source"], "api")
        self.assertEqual(info["max_output"], 393216)

    def test_user_context_override_wins(self):
        p = with_transport(make_provider(profile("deepseek", model="deepseek-flash", context_tokens=32000), "k"),
                           lambda r: httpx.Response(200, json={"data": [{"id": "deepseek-flash", "context_window": 1048576}]}))
        info = p.info()
        self.assertEqual((info["context_window"], info["context_source"]), (32000, "setting"))


class Gemini(unittest.TestCase):
    def test_native_model_list_filters_and_reports_token_limits(self):
        def handler(request):
            self.assertIn("/v1beta/models", str(request.url))
            self.assertEqual(request.headers.get("x-goog-api-key"), "g-key")
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash", "inputTokenLimit": 1048576,
                 "outputTokenLimit": 65536, "supportedGenerationMethods": ["generateContent", "countTokens"]},
                {"name": "models/gemini-embedding-2", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-4-hydrogen", "displayName": "Gemini 4 Hydrogen", "inputTokenLimit": 1048576,
                 "outputTokenLimit": 65536, "supportedGenerationMethods": ["generateContent"]}]})

        p = with_transport(make_provider(profile("gemini", model="gemini-3.8-flash"), "g-key"), handler)
        models = p.list_models()
        self.assertEqual([m["id"] for m in models], ["gemini-4-hydrogen", "gemini-3.8-flash"])
        self.assertEqual(p.info()["context_window"], 1048576)

    def test_invalid_key_message(self):
        p = with_transport(make_provider(profile("gemini"), "bad"), lambda r: httpx.Response(
            400, json={"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}))
        with self.assertRaises(LLMError) as cm:
            p.list_models()
        self.assertIn("rejected the API key", str(cm.exception))

    def test_chat_uses_compat_endpoint(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, content=sse({"choices": [{"delta": {"content": "hi"}}]}))

        p = with_transport(make_provider(profile("gemini", model="gemini-3.8-flash"), "g"), handler)
        list(p.stream([{"role": "user", "content": "x"}]))
        self.assertEqual(seen["url"], "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")


ANTHROPIC_SSE = "\n".join([
    'event: message_start',
    'data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"claude-opus-5-5","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":25,"output_tokens":1}}}',
    '', 'event: content_block_start',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    '', 'event: content_block_delta',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Use "}}',
    '', 'event: content_block_delta',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"prepareToPlay."}}',
    '', 'event: content_block_stop', 'data: {"type":"content_block_stop","index":0}',
    '', 'event: message_delta',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":9}}',
    '', 'event: message_stop', 'data: {"type":"message_stop"}', '', ''])


class Anthropic(unittest.TestCase):
    def provider(self, handler, **kw):
        import anthropic
        import httpx2

        p = make_provider(profile("anthropic", model=kw.pop("model", "claude-sonnet-5-5"), **kw), "sk-ant-test")
        client = anthropic.Anthropic(api_key="sk-ant-test", max_retries=0,
                                     http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
        p._client = lambda: client
        return p

    def test_stream_text_usage_and_request(self):
        import httpx2

        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=ANTHROPIC_SSE.encode())

        p = self.provider(handler)
        deltas = list(p.stream([{"role": "user", "content": "How?"}], "system text"))
        self.assertEqual("".join(d.text for d in deltas if d.kind == "text"), "Use prepareToPlay.")
        usage = next(d for d in deltas if d.kind == "usage").data
        self.assertEqual(usage["completion_tokens"], 9)
        body = seen["body"]
        self.assertEqual(body["system"], "system text")
        self.assertTrue(body["stream"])
        for forbidden in ("temperature", "top_p", "top_k", "thinking"):
            self.assertNotIn(forbidden, body)  # current Claude models reject sampling parameters

    def test_effort_and_summarised_thinking_are_opt_in(self):
        import httpx2

        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=ANTHROPIC_SSE.encode())

        p = self.provider(handler, effort="low", show_thinking=True)
        list(p.stream([{"role": "user", "content": "x"}]))
        self.assertEqual(bodies[0]["output_config"], {"effort": "low"})
        self.assertEqual(bodies[0]["thinking"], {"type": "adaptive", "display": "summarized"})

    def test_opus_requests_opt_into_refusal_fallback(self):
        import httpx2

        seen = {}

        def handler(request):
            seen["beta"] = request.headers.get("anthropic-beta")
            seen["body"] = json.loads(request.content)
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=ANTHROPIC_SSE.encode())

        p = self.provider(handler, model="claude-opus-5-5")
        list(p.stream([{"role": "user", "content": "x"}]))
        self.assertIn("server-side-fallback", seen["beta"] or "")
        self.assertEqual(seen["body"].get("fallbacks"), "default")

    def test_refusal_is_reported(self):
        import httpx2

        refusal = ANTHROPIC_SSE.replace('"stop_reason":"end_turn"', '"stop_reason":"refusal"')
        p = self.provider(lambda r: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=refusal.encode()))
        kinds = [d.kind for d in p.stream([{"role": "user", "content": "x"}])]
        self.assertIn("refusal", kinds)

    def test_model_list_carries_context_window(self):
        import httpx2

        p = self.provider(lambda r: httpx2.Response(200, json={
            "data": [{"id": "claude-opus-5-5", "type": "model", "display_name": "Claude Opus 5.5",
                      "created_at": "2026-01-01T00:00:00Z", "max_input_tokens": 1000000, "max_tokens": 128000}],
            "has_more": False, "first_id": "claude-opus-5-5", "last_id": "claude-opus-5-5"}))
        models = p.list_models()
        self.assertEqual(models[0]["context_window"], 1000000)

    def test_auth_error_is_friendly(self):
        import httpx2

        p = self.provider(lambda r: httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
        with self.assertRaises(LLMError) as cm:
            list(p.stream([{"role": "user", "content": "x"}]))
        self.assertIn("rejected the API key", str(cm.exception))


class SettingsKeys(TempHome):
    def test_connect_stores_encrypted_key_and_hides_it_from_the_browser(self):
        from perplexify.settings import Settings

        s = Settings()
        self.assertFalse(s.is_ready())
        s.connect("gemini", "AIza-secret-value", "gemini-3.8-flash")
        self.assertTrue(s.is_ready())
        self.assertEqual(s.provider_key("gemini"), "AIza-secret-value")
        text = Path(self._home, "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("AIza-secret-value", text)
        pub = s.public()
        self.assertTrue(pub["profiles"][0]["has_key"])
        self.assertNotIn("api_key_enc", pub["profiles"][0])
        self.assertEqual(s.profile()["model"], "gemini-3.8-flash")

    def test_environment_key_is_used_without_being_stored(self):
        import os

        from perplexify.settings import Settings

        os.environ["DEEPSEEK_API_KEY"] = "sk-env-key-123"
        try:
            s = Settings()
            s.connect("deepseek", None, "deepseek-flash", use_env_key=True)
            self.assertEqual(s.api_key(s.profile("deepseek")), "sk-env-key-123")
            self.assertNotIn("sk-env-key-123", Path(self._home, "settings.json").read_text(encoding="utf-8"))
        finally:
            del os.environ["DEEPSEEK_API_KEY"]


if __name__ == "__main__":
    unittest.main()
