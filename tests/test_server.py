import json
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from helpers import TempHome

from perplexify.llm.base import Delta, LLMError, LLMProvider


class FakeLLM(LLMProvider):
    kind = "openai"

    def __init__(self, profile, key="", models=None, fail=None, tokens=("Use ", "the mixer [1]."), **_):
        super().__init__(profile, key)
        self._models, self._fail, self._tokens = models or [], fail, tokens

    def list_models(self):
        if self._fail:
            raise LLMError(self._fail)
        return self._models

    def stream(self, messages, system=None, *, quick=False):
        self.last = (messages, system)
        for t in self._tokens:
            yield Delta("text", t)
        yield Delta("usage", data={"prompt_tokens": 900, "completion_tokens": 7})

    def info(self):
        return {"kind": "openai", "provider": self.provider_id, "model": self.model, "context_window": 1048576,
                "max_context": 1048576, "max_output": 65536, "context_source": "api"}


class FakeEmbedder:
    dim = 4
    preferred_batch = 8
    concurrency = 1

    def embed_query(self, text):
        return np.zeros(4, dtype=np.float32)

    def embed_documents(self, texts):
        return np.zeros((len(texts), 4), dtype=np.float32)


def factory(**kw):
    return lambda profile, key="": FakeLLM(profile, key, **kw)


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


class ServerAPI(TempHome):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from perplexify.context import AppContext
        from perplexify.server import create_app

        cls.ctx = AppContext()
        cls.ctx.settings.update({"retrieval": {"rerank": False}})   # the reranking model is downloaded on first use: not in a unit test
        cls.client = TestClient(create_app(cls.ctx), base_url="http://127.0.0.1")

    def wait(self, job, timeout=60):
        t = time.time()
        while time.time() - t < timeout:
            j = self.client.get(f"/api/jobs/{job['id']}").json()
            if j["status"] not in ("queued", "running"):
                return j
            time.sleep(0.1)
        self.fail("job did not finish")

    def make_repo(self):
        root = Path(self._home) / "demo_repo"
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "mixer.cpp").write_text(
            "namespace demo {\n/** Mixes voices. */\nclass Mixer {\npublic:\n    /** Renders a block. */\n"
            "    void renderBlock (float* out, int n) { for (int i = 0; i < n; ++i) out[i] = 0; }\n};\n}\n", "utf-8")
        (root / "README.md").write_text("# Demo\n\nThe demo mixes audio voices together.\n", "utf-8")
        return root

    # ------------------------------------------------------------------ connecting a model
    def test_01_connect_flow(self):
        boot = self.client.get("/api/bootstrap").json()
        self.assertFalse(boot["ready"])
        self.assertEqual([p["id"] for p in boot["providers"]], ["openai", "anthropic", "gemini", "deepseek", "local", "custom"])
        self.assertEqual([(p["name"], p["company"]) for p in boot["providers"]][:2], [("ChatGPT", "OpenAI"), ("Claude", "Anthropic")])
        self.assertTrue(any(e["id"] == "builtin" and e["available"] for e in boot["embedders"]))
        self.assertFalse(any(e["id"].startswith("voyage") and e["available"] for e in boot["embedders"]))

        bad = factory(fail="Claude rejected the API key. Check that it was copied completely.")
        with mock.patch("perplexify.server.make_provider", bad):
            r = self.client.post("/api/connect", json={"provider": "anthropic", "api_key": "sk-ant-wrong"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("rejected the API key", r.json()["detail"])
        self.assertFalse(self.client.get("/api/bootstrap").json()["ready"])

        good = factory(models=[{"id": "claude-haiku-5-5"}, {"id": "claude-opus-5-5"}])
        with mock.patch("perplexify.server.make_provider", good), mock.patch("perplexify.context.make_provider", good):
            r = self.client.post("/api/connect", json={"provider": "anthropic", "api_key": "sk-ant-secret-key-123456"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model"], "claude-opus-5-5")  # the catalog's preference, not just the first in the list
        self.assertEqual(r.json()["info"]["context_window"], 1048576)
        self.assertIn("no embeddings API", r.json()["embeddings_note"])
        self.assertTrue(self.client.get("/api/bootstrap").json()["ready"])
        everything = self.client.get("/api/settings").text + self.client.get("/api/bootstrap").text
        self.assertNotIn("sk-ant-secret-key-123456", everything)

    def test_02_profile_model_switch_and_removal(self):
        with mock.patch("perplexify.context.make_provider", factory()):
            r = self.client.patch("/api/profiles/anthropic", json={"model": "claude-sonnet-5-5"})
        self.assertEqual(r.json()["profile"]["model"], "claude-sonnet-5-5")
        self.assertEqual(self.client.get("/api/settings").json()["active_profile"], "anthropic")

    # ------------------------------------------------------------------ security
    def test_03_host_and_origin_guards(self):
        self.assertEqual(self.client.get("/api/health", headers={"host": "evil.example.com"}).status_code, 403)
        self.assertEqual(self.client.post("/api/jobs/x/cancel", headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/jobs/x/cancel", headers={"origin": "http://127.0.0.1"}).status_code, 200)
        self.assertEqual(self.client.post("/api/jobs/x/cancel", headers={"sec-fetch-site": "cross-site"}).status_code, 403)

    # ------------------------------------------------------------------ libraries, code import, search, chat
    def test_04_repo_import_search_source_and_chat(self):
        root = self.make_repo()
        lib = self.client.post("/api/libraries", json={"name": "Demo repo"}).json()
        with mock.patch("perplexify.tasks.ingest.embed_pending", lambda *a, **k: 0):
            r = self.client.post(f"/api/libraries/{lib['id']}/ingest/repo", json={"path": str(root)})
            self.assertEqual(r.status_code, 200, r.text)
            job = self.wait(r.json())
        self.assertEqual(job["status"], "done", job)
        # a second job on the same library while it is busy is refused, a finished one is not
        docs = self.client.get(f"/api/libraries/{lib['id']}/documents").json()
        self.assertEqual({d["title"] for d in docs["documents"]}, {"src/mixer.cpp", "README.md"})
        code_doc = next(d for d in docs["documents"] if d["kind"] == "code")
        self.assertEqual(code_doc["lang"], "cpp")

        src = self.client.get(f"/api/libraries/{lib['id']}/source", params={"doc": code_doc["id"]}).json()
        self.assertIn("class Mixer", src["text"])
        self.assertEqual(src["lang"], "cpp")

        with mock.patch("perplexify.retrieval.get_embedder", lambda spec, settings=None: FakeEmbedder()):
            hits = self.client.post("/api/search", json={"query": "renderBlock", "libraries": [lib["id"]]}).json()["hits"]
            self.assertEqual(hits[0]["title"], "src/mixer.cpp")
            self.assertEqual(hits[0]["kind"], "code")
            self.assertRegex(hits[0]["anchor"], r"^L\d+")

            llm = factory(tokens=("Use ", "renderBlock [1]."))
            with mock.patch("perplexify.rag.make_provider", llm), mock.patch("perplexify.context.make_provider", llm):
                r = self.client.post("/api/chat", json={"question": "How do I render a block?", "libraries": [lib["id"]], "history": []})
        evs = events(r)
        kinds = [e["type"] for e in evs]
        self.assertEqual(kinds[:2], ["chat", "status"])     # first which saved conversation this turn belongs to (history.py), then the work
        for needed in ("sources", "context", "token", "usage", "done"):
            self.assertIn(needed, kinds)
        self.assertLess(kinds.index("sources"), kinds.index("context"))
        self.assertLess(kinds.index("context"), kinds.index("token"))
        sources = next(e for e in evs if e["type"] == "sources")["sources"]
        self.assertTrue(any(s["kind"] == "code" and s["lang"] == "cpp" for s in sources))
        ctx_ev = next(e for e in evs if e["type"] == "context")
        self.assertEqual(ctx_ev["window"], 1048576)
        self.assertGreater(ctx_ev["estimated_prompt"], 0)
        usage = next(e for e in evs if e["type"] == "usage")
        self.assertEqual((usage["prompt_tokens"], usage["completion_tokens"]), (900, 7))
        self.assertEqual("".join(e["text"] for e in evs if e["type"] == "token"), "Use renderBlock [1].")

    def test_05_code_questions_get_the_code_prompt(self):
        lib = next(x for x in self.ctx.libs.list() if x.name == "Demo repo")
        seen = {}

        def llm(profile, key=""):
            p = FakeLLM(profile, key)
            orig = p.stream

            def spy(messages, system=None, *, quick=False):
                seen["system"], seen["user"] = system, messages[-1]["content"]
                return orig(messages, system, quick=quick)

            p.stream = spy
            return p

        with mock.patch("perplexify.retrieval.get_embedder", lambda spec, settings=None: FakeEmbedder()), \
                mock.patch("perplexify.rag.make_provider", llm), mock.patch("perplexify.context.make_provider", llm):
            self.client.post("/api/chat", json={"question": "renderBlock", "libraries": [lib.id]})
        self.assertIn("code assistant", seen["system"])
        self.assertIn("src/mixer.cpp:", seen["user"])
        self.assertIn("```cpp", seen["user"])

    def test_06_mirror_serves_sandboxed_and_blocks_traversal(self):
        lib = self.client.post("/api/libraries", json={"name": "Mirror test"}).json()
        folder = self.ctx.libs.get(lib["id"]).source_dir
        (folder / "page.html").write_text("<p>hi</p>", "utf-8")
        r = self.client.get(f"/mirror/{lib['id']}/page.html")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-security-policy"], "sandbox")
        self.assertEqual(self.client.get(f"/mirror/{lib['id']}/..%2Flibrary.json").status_code, 404)
        self.assertEqual(self.client.get(f"/mirror/{lib['id']}/%2e%2e/library.json").status_code, 404)

    def test_07_upload_then_index(self):
        lib = self.client.post("/api/libraries", json={"name": "Upload test"}).json()
        r = self.client.put(f"/api/libraries/{lib['id']}/upload", params={"name": "../../notes.md"},
                            content=b"# Notes\n\nThe quick brown fox jumps over the lazy dog.\n")
        self.assertEqual(r.json()["name"], "notes.md")  # path tricks are flattened to a file name
        with mock.patch("perplexify.tasks.ingest.embed_pending", lambda *a, **k: 0):
            job = self.wait(self.client.post(f"/api/libraries/{lib['id']}/ingest/uploads").json())
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(self.client.get(f"/api/libraries/{lib['id']}/documents").json()["total"], 1)

    def test_08_busy_library_refuses_a_second_job_and_delete(self):
        import threading

        lib = self.client.post("/api/libraries", json={"name": "Busy test"}).json()
        gate = threading.Event()

        def slow(ctx, library, params, job):
            gate.wait(10)

        job = self.ctx.jobs.submit("x", "slow", lib["id"], lambda j: slow(None, None, None, j))
        time.sleep(0.2)
        self.assertEqual(self.client.post(f"/api/libraries/{lib['id']}/refresh").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/libraries/{lib['id']}").status_code, 409)
        gate.set()
        self.wait(job.to_dict())
        self.assertEqual(self.client.delete(f"/api/libraries/{lib['id']}").status_code, 200)

    def test_09_a_busy_library_can_be_deleted_by_stopping_its_jobs(self):
        import threading

        running = self.client.post("/api/libraries", json={"name": "Running now"}).json()
        waiting = self.client.post("/api/libraries", json={"name": "Waiting behind it"}).json()
        gate, never = threading.Event(), threading.Event()

        def cooperative(job):                       # like a real crawl: only the Stop flag ends it
            while not never.is_set():
                job.check()
                time.sleep(0.02)

        blocker = self.ctx.jobs.submit("x", "other library", waiting["id"] + "-other", lambda j: gate.wait(10))
        job_a = self.ctx.jobs.submit("x", "running", running["id"], cooperative)
        job_b = self.ctx.jobs.submit("x", "queued", waiting["id"], cooperative)
        time.sleep(0.2)
        # without the flag it is still refused
        self.assertEqual(self.client.delete(f"/api/libraries/{running['id']}").status_code, 409)
        # a queued job (the worker is busy elsewhere) is cancelled on the spot and its library goes at once
        self.assertEqual(self.client.delete(f"/api/libraries/{waiting['id']}?stop_jobs=true").status_code, 200)
        self.assertEqual(self.client.get(f"/api/libraries/{waiting['id']}").status_code, 404)
        self.assertEqual(job_b.to_dict()["status"], "cancelled")
        gate.set()
        self.wait(blocker.to_dict())
        time.sleep(0.3)
        self.assertEqual(job_a.to_dict()["status"], "running")
        # a running job is stopped, then its library is deleted
        self.assertEqual(self.client.delete(f"/api/libraries/{running['id']}?stop_jobs=true").status_code, 200)
        self.assertEqual(self.client.get(f"/api/libraries/{running['id']}").status_code, 404)
        self.assertEqual(job_a.to_dict()["status"], "cancelled")
        self.assertEqual(self.client.delete("/api/libraries/does-not-exist?stop_jobs=true").status_code, 404)


class NoModel(TempHome):
    def test_chat_without_a_connected_model_says_what_to_do(self):
        from perplexify.context import AppContext
        from perplexify.server import create_app

        client = TestClient(create_app(AppContext()), base_url="http://127.0.0.1")
        lib = client.post("/api/libraries", json={"name": "x"}).json()
        evs = [e for e in events(client.post("/api/chat", json={"question": "hello", "libraries": [lib["id"]]})) if e["type"] != "chat"]
        self.assertEqual(evs[0]["type"], "error")
        self.assertEqual(evs[0]["code"], "no_model")
        self.assertIn("ChatGPT, Claude, Gemini or DeepSeek", evs[0]["message"])


if __name__ == "__main__":
    unittest.main()
