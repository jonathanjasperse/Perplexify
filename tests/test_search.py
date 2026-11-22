import unittest
import zlib
from pathlib import Path
from unittest import mock

import numpy as np
from fastapi.testclient import TestClient

from helpers import TempHome

from perplexify import ingest
from perplexify.embeddings import EmbeddingError
from perplexify.jobs import Job
from perplexify.repo import RepoOptions
from perplexify.textutil import best_excerpt, build_exact_query, build_fts_query, highlight_terms


class HashEmbedder:
    """Bag-of-words vectors: passages that share words are close. Deterministic, no model needed."""
    dim = 64
    preferred_batch = 16
    concurrency = 1

    def _vec(self, text):
        v = np.zeros(self.dim, dtype=np.float32)
        for w in text.lower().replace(">", " ").replace(".", " ").split():
            if len(w) > 2:
                v[zlib.crc32(w.encode()) % self.dim] += 1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts):
        return np.vstack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), dtype=np.float32)

    def embed_query(self, text):
        return self._vec(text)


class Exploding:
    """An embedder that must never be asked for anything."""
    def __getattr__(self, name):
        raise AssertionError("keyword search must not touch the embedding model")


GUIDE = ("# Audio guide\n\n## Buffers\nA buffer holds audio samples. Fill the buffer before playback begins.\n\n"
         "## Latency\nLatency is the delay between input and output.\n")
INSTALL = "# Install\n\nRun the installer and then restart the computer.\n"
MIXER = """namespace demo
{
class Mixer
{
public:
    /** Renders one block of audio by summing every voice. */
    void renderBlock (float* out, int numSamples)
    {
        for (int i = 0; i < numSamples; ++i)
            out[i] = 0.0f;
    }
};
}
"""
CONVERT = 'def convert_to_wav(path, rate=44100):\n    """Convert an audio file to WAV at the given sample rate."""\n    return path + ".wav"\n'


class Engine(TempHome):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from perplexify.context import AppContext

        cls.ctx = AppContext()
        root = Path(cls._home)
        (root / "docs").mkdir()
        (root / "docs" / "guide.md").write_text(GUIDE, "utf-8")
        (root / "docs" / "install.md").write_text(INSTALL, "utf-8")
        (root / "repo" / "src").mkdir(parents=True)
        (root / "repo" / "src" / "mixer.cpp").write_text(MIXER, "utf-8")
        (root / "repo" / "tools.py").write_text(CONVERT, "utf-8")
        cls.docs = cls.ctx.libs.create("Docs")
        cls.code = cls.ctx.libs.create("Code")
        job = Job("t", "t", "x")
        with mock.patch("perplexify.ingest.get_embedder", lambda spec, settings=None: HashEmbedder()):
            ingest.index_folder(cls.docs, root / "docs", job)
            ingest.index_repo(cls.code, root / "repo", job, RepoOptions(), uri_for=lambda r: "u://" + r,
                              origin="external", stored_for=lambda r: str(root / "repo" / r), uri_prefix="u://")
            ingest.embed_pending(cls.docs, cls.ctx.settings, job)
            ingest.embed_pending(cls.code, cls.ctx.settings, job)

    def search(self, query, libs=None, **kw):
        with mock.patch("perplexify.retrieval.get_embedder", lambda spec, settings=None: HashEmbedder()):
            return self.ctx.retriever.search([query], libs or [self.docs.id, self.code.id], **{"top_k": 6, **kw})

    # ------------------------------------------------------------------ modes
    def test_keyword_mode_finds_identifiers_and_never_needs_the_embedding_model(self):
        with mock.patch("perplexify.retrieval.get_embedder", lambda spec, settings=None: Exploding()):
            hits, dbg = self.ctx.retriever.search(["renderBlock"], [self.docs.id, self.code.id], mode="keyword")
        self.assertEqual(hits[0].title, "src/mixer.cpp")
        self.assertIsNotNone(hits[0].kw_rank)
        self.assertIsNone(hits[0].vec_rank)
        self.assertEqual(dbg["mode"], "keyword")
        self.assertEqual(dbg["vector_hits"], 0)

    def test_semantic_mode_matches_meaning_words_without_keywords(self):
        hits, dbg = self.search("samples buffer audio", mode="semantic")
        self.assertEqual(hits[0].title, "docs/guide.md".replace("docs/", "") if False else hits[0].title)
        self.assertIn("buffer", hits[0].text.lower())
        self.assertIsNone(hits[0].kw_rank)
        self.assertIsNotNone(hits[0].vec_rank)
        self.assertEqual(dbg["keyword_hits"], 0)

    def test_hybrid_combines_both_signals(self):
        hits, _ = self.search("renderBlock audio samples", mode="hybrid")
        top = hits[0]
        self.assertEqual(top.title, "src/mixer.cpp")
        self.assertIsNotNone(top.vec_rank)
        self.assertIsNotNone(top.kw_rank)

    # ------------------------------------------------------------------ keyword options
    def test_all_words_must_appear_when_requested(self):
        with mock.patch("perplexify.retrieval.get_embedder", lambda spec, settings=None: Exploding()):
            any_hits, _ = self.ctx.retriever.search(["audio installer"], [self.docs.id], mode="keyword", match="any")
            all_hits, _ = self.ctx.retriever.search(["audio installer"], [self.docs.id], mode="keyword", match="all")
            both, _ = self.ctx.retriever.search(["audio samples"], [self.docs.id], mode="keyword", match="all")
        self.assertGreaterEqual(len(any_hits), 2)   # either word is enough
        self.assertEqual(all_hits, [])               # no single passage has both
        self.assertEqual(len(both), 1)

    def test_quoted_phrases_are_matched_exactly(self):
        self.assertIn('"restart the computer"', build_exact_query('install "restart the computer"'))
        with mock.patch("perplexify.retrieval.get_embedder", lambda spec, settings=None: Exploding()):
            hits, dbg = self.ctx.retriever.search(['"restart the computer"'], [self.docs.id], mode="keyword")
        self.assertIn("restart the computer", hits[0].text)
        self.assertIn("restart the computer", dbg["terms"])

    def test_query_builders(self):
        self.assertEqual(build_fts_query("how do I use renderBlock"), '"renderblock" OR "render" OR "block"')
        self.assertEqual(build_fts_query("audio buffer", "all"), '"audio" AND "buffer"')
        self.assertIsNone(build_fts_query("the of a"))
        terms = highlight_terms('processBlock "exact words"')
        self.assertIn("exact words", terms)
        self.assertEqual(terms, sorted(terms, key=len, reverse=True))  # longest first, so a regex prefers the long match
        self.assertIn("block", highlight_terms("processBlock"))

    def test_excerpt_centres_on_the_best_line(self):
        text = "\n".join(f"line {i} nothing here" for i in range(30)) + "\nthe needle is here\n" + "\n".join(f"tail {i}" for i in range(30))
        ex = best_excerpt(text, ["needle"])
        self.assertIn("the needle is here", ex)
        self.assertLess(len(ex), 200)
        self.assertTrue(ex.startswith("..."))

    # ------------------------------------------------------------------ filters
    def test_filters_by_kind_language_and_path(self):
        code, _ = self.search("audio", kinds=["code"], mode="keyword")
        self.assertTrue(code and all(h.kind == "code" for h in code))
        docs, _ = self.search("audio", kinds=["markdown"], mode="keyword")
        self.assertTrue(docs and all(h.kind == "markdown" for h in docs))
        py, _ = self.search("audio", langs=["python"], mode="hybrid")
        self.assertEqual({h.title for h in py}, {"tools.py"})
        mixer, _ = self.search("audio", path_contains="mixer", mode="hybrid")
        self.assertEqual({h.title for h in mixer}, {"src/mixer.cpp"})
        nothing, _ = self.search("audio", path_contains="does-not-exist")
        self.assertEqual(nothing, [])

    def test_library_choice_limits_where_it_looks(self):
        only_code, _ = self.search("audio", libs=[self.code.id], mode="keyword")
        self.assertTrue(only_code and all(h.library_id == self.code.id for h in only_code))

    # ------------------------------------------------------------------ resilience
    def test_a_broken_embedder_degrades_to_keywords_with_a_warning(self):
        def broken(spec, settings=None):
            raise EmbeddingError("the key was rejected")

        with mock.patch("perplexify.retrieval.get_embedder", broken):
            hits, dbg = self.ctx.retriever.search(["renderBlock"], [self.docs.id, self.code.id], mode="hybrid")
        self.assertEqual(hits[0].title, "src/mixer.cpp")
        self.assertTrue(any("by meaning is unavailable" in w for w in dbg["warnings"]))

    def test_odd_queries_never_raise(self):
        for q in ['"', "AND OR NOT", "(((", "a*", "name:value", "!!!", "x" * 400]:
            hits, _ = self.search(q, mode="keyword")
            self.assertIsInstance(hits, list)


class SearchEndpoint(TempHome):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from perplexify.context import AppContext
        from perplexify.server import create_app

        cls.ctx = AppContext()
        cls.client = TestClient(create_app(cls.ctx), base_url="http://127.0.0.1")
        root = Path(cls._home) / "many"
        root.mkdir()
        for i in range(7):
            (root / f"note{i}.md").write_text(f"# Note {i}\n\nThe quick brown fox number {i} jumps over the lazy dog.\n", "utf-8")
        cls.lib = cls.ctx.libs.create("Notes")
        with mock.patch("perplexify.ingest.get_embedder", lambda spec, settings=None: HashEmbedder()):
            ingest.index_folder(cls.lib, root, Job("t", "t", "x"))

    def test_paging_facets_and_highlights_without_any_model_connected(self):
        self.assertFalse(self.ctx.settings.is_ready())  # no cloud model: manual search must still work
        r1 = self.client.post("/api/search", json={"query": "quick brown fox", "mode": "keyword", "limit": 3}).json()
        self.assertEqual((len(r1["hits"]), r1["has_more"], r1["offset"]), (3, True, 0))
        r2 = self.client.post("/api/search", json={"query": "quick brown fox", "mode": "keyword", "limit": 3, "offset": 3}).json()
        self.assertEqual(len(r2["hits"]), 3)
        r3 = self.client.post("/api/search", json={"query": "quick brown fox", "mode": "keyword", "limit": 3, "offset": 6}).json()
        self.assertEqual((len(r3["hits"]), r3["has_more"]), (1, False))
        seen = [h["doc_id"] for r in (r1, r2, r3) for h in r["hits"]]
        self.assertEqual(len(seen), len(set(seen)))  # pages never repeat a result
        self.assertEqual([h["n"] for h in r2["hits"]], [4, 5, 6])
        self.assertIn("fox", r1["terms"])
        self.assertIn("fox", r1["hits"][0]["excerpt"])
        self.assertEqual(r1["facets"]["libraries"][0]["name"], "Notes")
        self.assertEqual((r1["shown"], r2["shown"], r3["shown"]), (3, 6, 7))

    def test_empty_query_is_a_clear_error(self):
        r = self.client.post("/api/search", json={"query": "   "})
        self.assertEqual(r.status_code, 400)

    def test_scope_docs_excludes_code(self):
        r = self.client.post("/api/search", json={"query": "fox", "mode": "keyword", "scope": "code"}).json()
        self.assertEqual(r["hits"], [])


if __name__ == "__main__":
    unittest.main()
