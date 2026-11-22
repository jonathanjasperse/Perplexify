import json
import unittest
from unittest import mock

import httpx
import numpy as np

from helpers import TempHome

from perplexify.embeddings import EmbeddingError, get_embedder, spec_key
from perplexify.embeddings.cloud import GeminiEmbedder, OpenAIEmbedder, VoyageEmbedder


def mocked(emb, handler):
    emb._http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    return emb


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


class OpenAIEmbeddings(unittest.TestCase):
    def test_payload_order_and_normalisation(self):
        seen = {}

        def handler(request):
            seen["body"], seen["auth"] = json.loads(request.content), request.headers["authorization"]
            # returned out of order on purpose
            return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 3, 4]}, {"index": 0, "embedding": [3, 0, 4]}]})

        emb = mocked(OpenAIEmbedder({"provider": "openai", "model": "text-embedding-3-large", "dimensions": 1024}, "sk-x"), handler)
        vecs = emb.embed_documents(["a", "b"])
        self.assertEqual(seen["body"], {"model": "text-embedding-3-large", "input": ["a", "b"], "dimensions": 1024})
        self.assertEqual(seen["auth"], "Bearer sk-x")
        np.testing.assert_allclose(vecs[0], unit([3, 0, 4]), rtol=1e-5)
        np.testing.assert_allclose(vecs[1], unit([0, 3, 4]), rtol=1e-5)
        self.assertEqual(emb.dim, 3)

    def test_missing_key_explains_what_to_do(self):
        with self.assertRaises(EmbeddingError) as cm:
            OpenAIEmbedder({"provider": "openai", "model": "text-embedding-3-small"}, "")
        self.assertIn("Connect ChatGPT", str(cm.exception))

    def test_rate_limit_is_retried_with_backoff(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(429, headers={"retry-after": "1"}, json={"error": "slow down"})
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})

        emb = mocked(OpenAIEmbedder({"provider": "openai", "model": "m"}, "k"), handler)
        with mock.patch("perplexify.embeddings.cloud.time.sleep") as sleep:
            emb.embed_query("x")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_bad_key_is_a_clear_error_and_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(401, json={"error": {"message": "bad"}})

        emb = mocked(OpenAIEmbedder({"provider": "openai", "model": "m"}, "k"), handler)
        with self.assertRaises(EmbeddingError) as cm:
            emb.embed_query("x")
        self.assertIn("rejected the API key", str(cm.exception))
        self.assertEqual(len(calls), 1)


class GeminiEmbeddings(unittest.TestCase):
    def run_one(self, spec, query=False, texts=("alpha", "beta")):
        seen = {}

        def handler(request):
            seen["url"], seen["key"], seen["body"] = str(request.url), request.headers["x-goog-api-key"], json.loads(request.content)
            n = len(seen["body"]["requests"])
            return httpx.Response(200, json={"embeddings": [{"values": [1.0, 2.0, 2.0]} for _ in range(n)]})

        emb = mocked(GeminiEmbedder(spec, "g-key"), handler)
        out = emb.embed_query(texts[0]) if query else emb.embed_documents(list(texts))
        return emb, seen, out

    def test_embedding_001_uses_task_types_and_one_item_per_text(self):
        _emb, seen, vecs = self.run_one({"provider": "gemini", "model": "gemini-embedding-001", "dimensions": 768})
        self.assertTrue(seen["url"].endswith("/v1beta/models/gemini-embedding-001:batchEmbedContents"))
        self.assertEqual(seen["key"], "g-key")
        reqs = seen["body"]["requests"]
        self.assertEqual(len(reqs), 2)
        self.assertEqual(reqs[0]["taskType"], "RETRIEVAL_DOCUMENT")
        self.assertEqual(reqs[0]["outputDimensionality"], 768)
        self.assertEqual(reqs[0]["model"], "models/gemini-embedding-001")
        self.assertEqual(reqs[1]["content"], {"parts": [{"text": "beta"}]})
        self.assertAlmostEqual(float(np.linalg.norm(vecs[0])), 1.0, places=5)  # truncated sizes need our normalisation

    def test_embedding_001_query_and_code_flavour(self):
        _e, seen, _v = self.run_one({"provider": "gemini", "model": "gemini-embedding-001"}, query=True)
        self.assertEqual(seen["body"]["requests"][0]["taskType"], "RETRIEVAL_QUERY")
        _e, seen, _v = self.run_one({"provider": "gemini", "model": "gemini-embedding-001", "flavor": "code"}, query=True)
        self.assertEqual(seen["body"]["requests"][0]["taskType"], "CODE_RETRIEVAL_QUERY")

    def test_embedding_2_has_no_task_type_and_writes_the_task_into_the_text(self):
        _e, seen, _v = self.run_one({"provider": "gemini", "model": "gemini-embedding-2"})
        item = seen["body"]["requests"][0]
        self.assertNotIn("taskType", item)
        self.assertEqual(item["content"]["parts"][0]["text"], "title: none | text: alpha")
        _e, seen, _v = self.run_one({"provider": "gemini", "model": "gemini-embedding-2"}, query=True, texts=("find x",))
        self.assertEqual(seen["body"]["requests"][0]["content"]["parts"][0]["text"], "task: search result | query: find x")
        _e, seen, _v = self.run_one({"provider": "gemini", "model": "gemini-embedding-2", "flavor": "code"}, query=True, texts=("q",))
        self.assertEqual(seen["body"]["requests"][0]["content"]["parts"][0]["text"], "task: code retrieval | query: q")

    def test_wrong_number_of_vectors_is_refused(self):
        emb = mocked(GeminiEmbedder({"provider": "gemini", "model": "gemini-embedding-2"}, "k"),
                     lambda r: httpx.Response(200, json={"embeddings": [{"values": [1, 0]}]}))  # one for two texts
        with self.assertRaises(EmbeddingError):
            emb.embed_documents(["a", "b"])


class VoyageEmbeddings(unittest.TestCase):
    def test_input_type_and_dimension(self):
        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            n = len(bodies[-1]["input"])
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(n)]})

        emb = mocked(VoyageEmbedder({"provider": "voyage", "model": "voyage-code-4", "dimensions": 512}, "pa-k"), handler)
        emb.embed_documents(["a"])
        emb.embed_query("q")
        self.assertEqual(bodies[0]["input_type"], "document")
        self.assertEqual(bodies[1]["input_type"], "query")
        self.assertEqual(bodies[0]["output_dimension"], 512)
        self.assertEqual(bodies[0]["model"], "voyage-code-4")


class Cache(TempHome):
    def test_changing_the_key_creates_a_new_embedder(self):
        from perplexify.settings import Settings

        s = Settings()
        spec = {"provider": "openai", "model": "text-embedding-3-small"}
        s.connect("openai", "sk-one", "gpt-x")
        a = get_embedder(spec, s)
        self.assertIs(a, get_embedder(spec, s))
        s.connect("openai", "sk-two", "gpt-x")
        self.assertIsNot(a, get_embedder(spec, s))
        self.assertEqual(spec_key(spec), spec_key(dict(spec)))

    def test_missing_cloud_key_never_falls_back_silently(self):
        from perplexify.settings import Settings

        with self.assertRaises(EmbeddingError):
            get_embedder({"provider": "voyage", "model": "voyage-4"}, Settings())


if __name__ == "__main__":
    unittest.main()
