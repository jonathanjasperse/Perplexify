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

    def test_each_provider_gets_the_research_prompt_meant_for_it(self):
        for provider_id, expected in (("anthropic", "senior research partner"), ("openai", "senior research partner"),
                                      ("gemini", "senior research partner"), ("deepseek", "careful research assistant"),
                                      ("custom", "careful research assistant")):
            class Named(FakeProvider):
                pass
            Named.provider_id = provider_id
            self.provider = Named()
            evs = self.ask()
            self.assertNotIn("error", [e["type"] for e in evs], (provider_id, evs))
            system = self.provider.streams[0][0]
            self.assertIn(expected, system, provider_id)
            self.assertIn("Rust 1.80", "".join(e["text"] for e in evs if e["type"] == "token"))

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
        self.assertEqual(websearch.engine_name(self.ctx.settings), "open")           # no key: the open sources


def open_found(per_query, used=("Wikipedia",), errors=None):
    from perplexify import opensources

    r = opensources.OpenSearch(per_query=per_query, used=list(used), counts={u: 1 for u in used}, errors=errors or {}, asked=list(used))
    return r


class NoKeys(TempHome):
    """With nothing set up Research asks the open sources; a saved key takes over, and the open sources step in when it fails."""

    def setUp(self):
        import os

        from perplexify import paths
        from perplexify.context import AppContext

        (paths.home() / "settings.json").unlink(missing_ok=True)          # every test starts with nothing set up
        self.ctx = AppContext()
        self.settings = self.ctx.settings
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("TAVILY_API_KEY", None)
        os.environ.pop("BRAVE_API_KEY", None)

    def result(self, n, source="PubMed", text=""):
        return websearch.WebResult(f"Paper {n}", f"https://pubmed.ncbi.nlm.nih.gov/{n}/", "snippet " + str(n), source, text)

    def test_without_a_key_the_open_sources_answer(self):
        self.assertEqual(websearch.engine_name(self.settings), "open")
        w = self.result(1)
        with mock.patch("perplexify.opensources.search_many", lambda *a, **k: open_found([[w]], ("PubMed",))) as m:
            results, engine = websearch.search("ecg arrhythmia", self.settings, 4)
        self.assertEqual((results, engine), ([w], "PubMed"))

    def test_nothing_found_says_why(self):
        from perplexify import opensources

        empty = open_found([[]], (), {"DuckDuckGo": "is refusing automated searches from this connection right now"})
        empty.asked = ["DuckDuckGo"]
        with mock.patch("perplexify.opensources.search_many", lambda *a, **k: empty):
            with self.assertRaises(websearch.WebSearchError) as e:
                websearch.search("ecg arrhythmia", self.settings, 4)
        self.assertIn("DuckDuckGo is refusing automated searches", str(e.exception))

    def test_a_saved_key_is_used_and_the_open_sources_are_not_asked(self):
        self.settings.set_embedding_key("tavily", "tvly-test")
        w = websearch.WebResult("Page", "https://example.org/a", "s")
        with mock.patch("perplexify.websearch._tavily", lambda q, key, n: [w]), \
                mock.patch("perplexify.opensources.search_many", side_effect=AssertionError("the open sources must not be asked")):
            self.assertEqual(websearch.search("anything", self.settings, 3), ([w], "tavily"))

    def test_when_the_keyed_service_fails_the_open_sources_step_in(self):
        self.settings.set_embedding_key("tavily", "tvly-test")

        def limit(q, key, n):
            raise websearch.WebSearchError("Tavily's search limit was reached. Wait a moment, or check your plan.")
        abstract = "Background: " + "x" * 300
        w = self.result(7, text=abstract)
        with mock.patch("perplexify.websearch._tavily", limit), \
                mock.patch("perplexify.opensources.search_many", lambda *a, **k: open_found([[w]], ("PubMed",))):
            hits, info = websearch.research(["ecg arrhythmia"], self.settings, question="ecg")
        self.assertEqual([h.library_name for h in hits], ["PubMed"])
        self.assertEqual(info["engine"], "PubMed")
        self.assertIn("Tavily's search limit", info["errors"][0], "the reason the key was not used is kept in the trace")

    def test_results_that_arrive_with_their_text_are_not_downloaded_and_the_others_are(self):
        abstract = "Background: " + "an abstract sentence. " * 30
        paper = self.result(1, "PubMed", abstract)
        wiki = websearch.WebResult("PowerShell", "https://en.wikipedia.org/wiki/PowerShell", "a shell", "Wikipedia")
        fetched = []
        html = ("<html><head><title>PowerShell</title></head><body><main><h1>PowerShell</h1><p>" + "PowerShell is a shell with objects in a pipeline. " * 12
                + "</p></main></body></html>").encode()

        def fake_fetch(url, client):
            fetched.append(url)
            return url, html
        found = open_found([[paper, wiki]], ("PubMed", "Wikipedia"), {"DuckDuckGo": "is refusing automated searches from this connection right now"})
        found.counts = {"PubMed": 1, "Wikipedia": 1}
        with mock.patch("perplexify.opensources.search_many", lambda *a, **k: found), mock.patch("perplexify.websearch.fetch_page", fake_fetch):
            hits, info = websearch.research(["powershell pipeline"], self.settings, question="powershell pipeline objects")
        self.assertEqual(fetched, ["https://en.wikipedia.org/wiki/PowerShell"], "only the page that needed reading was downloaded")
        self.assertEqual([h.library_name for h in hits], ["PubMed", "Wikipedia"], "each result carries the name of its source")
        self.assertEqual(hits[0].text, abstract.strip())
        self.assertIn("PowerShell is a shell", hits[1].text)
        self.assertEqual((info["engine"], info["pages"]), ("PubMed, Wikipedia", 2))
        self.assertEqual(info["sources"], {"PubMed": 1, "Wikipedia": 1})
        self.assertIn("DuckDuckGo", info["unavailable"])

    def test_a_source_the_user_switched_off_is_not_asked(self):
        self.settings.update({"web_sources": {"github": False, "duckduckgo": False}})
        self.assertEqual(websearch.enabled_sources(self.settings), {"wikipedia", "pubmed", "arxiv", "openalex", "stackoverflow"})
        seen = {}

        def spy(queries, enabled=None, per_query=6, client=None):
            seen["enabled"] = enabled
            return open_found([[self.result(1)]], ("PubMed",))
        with mock.patch("perplexify.opensources.search_many", spy):
            websearch.search("ecg", self.settings, 3)
        self.assertNotIn("github", seen["enabled"])

    def test_the_test_button_reports_which_sources_answered(self):
        found = open_found([[self.result(1), self.result(2, "arXiv")], []], ("PubMed", "arXiv"), {"DuckDuckGo": "is refusing automated searches from this connection right now"})
        found.counts = {"PubMed": 1, "arXiv": 1}
        with mock.patch("perplexify.opensources.search_many", lambda *a, **k: found):
            r = websearch.test_search(self.settings)
        self.assertEqual((r["engine"], r["results"], r["sources"]), ("open sources", 2, {"PubMed": 1, "arXiv": 1}))
        self.assertIn("DuckDuckGo", r["unavailable"])

    def test_the_endpoints_and_the_settings_page(self):
        from fastapi.testclient import TestClient

        from perplexify.server import create_app

        client = TestClient(create_app(self.ctx), base_url="http://127.0.0.1")
        s = client.get("/api/settings").json()
        self.assertEqual([i["id"] for i in s["web_source_info"]][:3], ["wikipedia", "pubmed", "arxiv"])
        self.assertTrue(all(i["name"] and i["about"] for i in s["web_source_info"]))
        self.assertTrue(all(s["web_sources"].values()), "every source is on until the user switches it off")
        s = client.put("/api/settings", json={"web_sources": {"github": False}}).json()
        self.assertFalse(s["web_sources"]["github"])
        self.assertTrue(s["web_sources"]["pubmed"])
        found = open_found([[self.result(1)]], ("PubMed",))
        found.counts = {"PubMed": 1}
        with mock.patch("perplexify.opensources.search_many", lambda *a, **k: found):
            r = client.post("/api/search-keys/test").json()
        self.assertEqual((r["ok"], r["sources"]), (True, {"PubMed": 1}))
        with mock.patch("perplexify.opensources.search_many", lambda *a, **k: open_found([[]], (), {})):
            self.assertEqual(client.post("/api/search-keys/test").status_code, 400)


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
