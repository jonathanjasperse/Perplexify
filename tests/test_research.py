"""Research mode: the model plans web searches, the app reads the pages and the answer cites them."""
import unittest
from unittest import mock

from helpers import TempHome

from perplexify import rag, websearch
from perplexify.llm.base import Delta
from perplexify.retrieval import Hit

PROFILE = {"id": "p1", "provider": "openai", "kind": "openai", "model": "gpt-test", "name": "ChatGPT"}


class FakeProvider:
    """Plans two searches when asked to, then answers citing [1]."""

    def __init__(self):
        self.prompts = []
        self.streams = []

    def complete(self, messages, system=None, **kw):
        self.prompts.append((system, messages))
        return "rust 1.80 release date\n- rust 1.80 new features" if system == rag.WEB_PLAN_SYSTEM else ""

    def stream(self, messages, system=None, **kw):
        self.streams.append((system, messages))
        yield Delta("text", "Rust 1.80 came out in July 2024 [1].")


def web_hit(i, url):
    return Hit("web", url.split("/")[2], -i, -i, f"Page {i}", url, f"Page {i}", None,
               f"Web passage {i}. " + "facts " * 60, "web", None, "web", score=1 - i / 10)


def lib_hit(i):
    return Hit("lib", "Manual", i, i, f"p{i}.html", f"https://docs.example.org/p{i}", f"Manual > {i}", None,
               f"Library passage {i}. " + "words " * 60, "mirror", None, "html", score=1 - i / 10)


class Research(TempHome):
    def setUp(self):
        from perplexify.context import AppContext

        self.ctx = AppContext()
        self.ctx.settings.update({"retrieval": {"rerank": False, "multi_query": False, "query_rewrite": False}})
        self.provider = FakeProvider()
        self.lib_calls = []
        self.web_calls = []
        self.ctx.retriever.search = lambda queries, ids, **kw: (self.lib_calls.append(ids) or [lib_hit(1), lib_hit(2)],
                                                              {"timing_ms": {"total": 1}, "pending": 0})
        self.ctx.llm_info = lambda profile: {"context_window": 128000}
        self.web = [web_hit(1, "https://blog.rust-lang.org/2024/07/25/Rust-1.80.0.html"), web_hit(2, "https://en.wikipedia.org/wiki/Rust")]
        patches = [mock.patch.object(self.ctx.settings, "profile", lambda pid=None: dict(PROFILE)),
                   mock.patch.object(self.ctx.settings, "can_chat", lambda p: True),
                   mock.patch.object(self.ctx.settings, "api_key", lambda p: "k"),
                   mock.patch("perplexify.rag.make_provider", lambda profile, key: self.provider),
                   mock.patch("perplexify.websearch.research", self.fake_research)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_research(self, queries, settings=None, **kw):
        self.web_calls.append(queries)
        return list(self.web), {"engine": "tavily", "results": 2, "pages": 2}

    def ask(self, libraries=(), research=True):
        return list(self.ctx.engine.answer_stream("When did Rust 1.80 come out?", [], list(libraries), research=research))

    def test_research_works_with_no_library_and_cites_the_pages(self):
        evs = self.ask()
        kinds = [e["type"] for e in evs]
        self.assertNotIn("error", kinds, evs)
        self.assertEqual(self.lib_calls, [], "no library is searched when none is ticked")
        plan = next(e for e in evs if e["type"] == "web_search")
        self.assertEqual(plan["queries"], ["rust 1.80 release date", "rust 1.80 new features"], "the model chose the searches")
        self.assertEqual(self.web_calls, [plan["queries"]])
        src = next(e for e in evs if e["type"] == "sources")
        self.assertEqual([s["kind"] for s in src["sources"]], ["web", "web"])
        self.assertEqual(src["sources"][0]["online_url"], self.web[0].uri)
        self.assertEqual(src["debug"]["web"]["engine"], "tavily")
        system, messages = self.provider.streams[0]
        self.assertIn("research assistant", system)
        self.assertIn("[1] Page 1 (https://blog.rust-lang.org/2024/07/25/Rust-1.80.0.html)", messages[-1]["content"])
        self.assertIn("[1]", "".join(e["text"] for e in evs if e["type"] == "token"))

    def test_research_with_libraries_mixes_web_pages_and_passages(self):
        evs = self.ask(libraries=["lib"])
        self.assertEqual(self.lib_calls, [["lib"]])
        src = next(e for e in evs if e["type"] == "sources")["sources"]
        self.assertEqual([s["kind"] for s in src], ["web", "html", "web", "html"], "interleaved, best of each first")
        self.assertIn("the user's own libraries (Manual)", self.provider.streams[0][0])

    def test_without_research_a_library_is_still_required(self):
        evs = self.ask(research=False)
        self.assertEqual(evs[-1]["type"], "error")
        self.assertIn("turn on Research", evs[-1]["message"])
        self.assertEqual(self.web_calls, [])

    def test_a_failed_web_search_is_reported_and_answered_honestly(self):
        def refuse(*a, **k):
            raise websearch.WebSearchError("DuckDuckGo is refusing automated searches from this connection right now.")
        with mock.patch("perplexify.websearch.research", refuse):
            evs = self.ask()
        self.assertTrue(any(e["type"] == "notice" and "DuckDuckGo" in e["text"] for e in evs))
        self.assertIn("couldn't find anything relevant on the web", "".join(e.get("text", "") for e in evs if e["type"] == "token"))
        self.assertEqual(self.provider.streams, [], "no answer is made up without sources")

    def test_the_search_key_endpoints(self):
        from fastapi.testclient import TestClient

        from perplexify.server import create_app

        client = TestClient(create_app(self.ctx), base_url="http://127.0.0.1")
        self.assertEqual(client.post("/api/search-keys", json={"provider": "google", "key": "x"}).status_code, 400)
        r = client.post("/api/search-keys", json={"provider": "tavily", "key": "tvly-test"}).json()
        self.assertEqual(r["engine"], "tavily")
        self.assertTrue(client.get("/api/settings").json()["embedding_keys"]["tavily"])
        self.assertNotIn("tvly-test", client.get("/api/settings").text, "the key itself never reaches the page")
        client.post("/api/search-keys", json={"provider": "tavily", "key": ""})
        self.assertEqual(websearch.engine_name(self.ctx.settings), "duckduckgo")


class WebHelpers(unittest.TestCase):
    def test_only_public_addresses_are_opened(self):
        for url in ("http://127.0.0.1:8790/", "http://localhost/x", "http://192.168.1.10/", "http://10.0.0.1/",
                    "http://[::1]/", "http://169.254.169.254/latest/meta-data", "file:///C:/Windows/win.ini",
                    "ftp://example.com/", "http://printer.local/"):
            self.assertFalse(websearch.is_public_url(url), url)

    def test_a_redirect_to_this_computer_is_not_followed(self):
        import httpx

        def handler(request):
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8790/api/settings"})
        with mock.patch("perplexify.websearch.is_public_url", lambda u: "127.0.0.1" not in u), \
                httpx.Client(transport=httpx.MockTransport(handler)) as c:
            self.assertIsNone(websearch.fetch_page("https://example.com/", c))

    def test_pages_keep_the_passages_that_match_the_query(self):
        html = ("<html><head><title>Release notes</title></head><body><main><h1>Rust 1.80.0</h1>"
                "<p>" + "Intro about the release and the team. " * 12 + "</p>"
                "<h2>Cooking</h2><p>" + "Unrelated recipes for bread and soup. " * 40 + "</p>"
                "<h2>LazyCell</h2><p>" + "LazyCell and LazyLock are now stable in Rust 1.80. " * 10 + "</p></main></body></html>").encode()
        title, passages = websearch.page_passages(html, "https://blog.example.org/rust-1.80")
        self.assertEqual(title, "Rust 1.80.0")
        text = websearch._best_text(passages, ["lazycell", "stable"], 900)
        self.assertIn("LazyCell", text)
        self.assertNotIn("bread", text)

    def test_the_mcp_server_offers_web_search(self):
        from perplexify import mcp_server

        self.assertIn("web_search", [t["name"] for t in mcp_server.TOOLS])


if __name__ == "__main__":
    unittest.main()
