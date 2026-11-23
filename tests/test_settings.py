"""The search settings a fresh installation starts with (the user's chosen defaults) and what a saved choice does to them."""
import json
import os
import unittest
from unittest import mock
from pathlib import Path

from helpers import TempHome

from perplexify import osutil, paths
from perplexify.settings import DEFAULT_EMBEDDER, RETRIEVAL_DEFAULTS, Settings


class SearchDefaults(TempHome):
    def setUp(self):
        (paths.home() / "settings.json").unlink(missing_ok=True)

    def test_a_fresh_installation_starts_with_the_chosen_search_settings(self):
        r = Settings().retrieval()
        self.assertEqual(r["top_k"], 30)                    # passages per question
        self.assertEqual(r["max_per_doc"], 10)              # at most per page or file
        self.assertEqual(r["candidates"], 20)               # candidates per method
        self.assertEqual(r["max_excerpt_tokens"], 30000)    # excerpt budget (tokens)
        self.assertIs(r["query_rewrite"], True)             # rewrite follow-up questions
        self.assertIs(r["multi_query"], True)               # search with extra phrasings
        self.assertIs(r["rerank"], True)                    # rerank with a cross-encoder
        for key in ("top_k", "max_per_doc", "candidates", "max_excerpt_tokens", "query_rewrite", "multi_query", "rerank"):
            self.assertEqual(r[key], RETRIEVAL_DEFAULTS[key])

    def write(self, retrieval):
        (paths.home() / "settings.json").write_text(json.dumps({"version": 2, "retrieval": retrieval}), "utf-8")

    def test_values_the_user_saved_are_kept(self):
        s = Settings()
        s.update({"retrieval": {"top_k": 12, "rerank": False, "multi_query": False}})
        r = Settings().retrieval()
        self.assertEqual((r["top_k"], r["rerank"], r["multi_query"], r["max_per_doc"]), (12, False, False, 10))
        saved = json.loads((paths.home() / "settings.json").read_text("utf-8"))
        self.assertEqual(saved["retrieval"]["top_k"], 12)

    def test_the_limits_still_hold(self):
        Settings().update({"retrieval": {"top_k": 500, "max_per_doc": 99, "candidates": 1}})
        r = Settings().retrieval()
        self.assertEqual((r["top_k"], r["max_per_doc"], r["candidates"]), (40, 10, 10))

    def test_a_file_that_still_holds_an_earlier_default_set_follows_the_new_defaults(self):
        # the settings file stores every value, so the old defaults were written to disk by an earlier version
        self.write({"top_k": 8, "candidates": 40, "max_per_doc": 3, "max_excerpt_tokens": 12000, "rerank": False,
                    "reranker_model": "Xenova/ms-marco-MiniLM-L-6-v2", "query_rewrite": True, "multi_query": False})
        self.assertEqual(Settings().retrieval()["top_k"], 30)
        self.write({"top_k": 10, "candidates": 20, "max_per_doc": 10, "max_excerpt_tokens": 30000, "rerank": True,
                    "query_rewrite": True, "multi_query": True})
        self.assertEqual(Settings().retrieval()["top_k"], 30)

    def test_a_customised_set_is_not_touched_by_the_migration(self):
        self.write({"top_k": 8, "candidates": 40, "max_per_doc": 3, "max_excerpt_tokens": 12000, "rerank": True,
                    "query_rewrite": True, "multi_query": False})                    # the old defaults, except that reranking was switched on
        r = Settings().retrieval()
        self.assertEqual((r["top_k"], r["candidates"], r["rerank"]), (8, 40, True))


class Bundled(unittest.TestCase):
    def test_the_installer_ships_the_models_the_app_uses_by_default(self):
        """Reranking is on by default, so its model must be in the installer or the first question would need a download."""
        pins = json.loads((Path(__file__).resolve().parent.parent / "installer" / "pins.json").read_text("utf-8"))
        self.assertEqual(pins["embedding_model"], DEFAULT_EMBEDDER["model"])
        self.assertEqual(pins["reranker_model"], RETRIEVAL_DEFAULTS["reranker_model"])


class KeyStorage(TempHome):
    def setUp(self):
        (paths.home() / "settings.json").unlink(missing_ok=True)

    def test_a_failed_encryption_never_falls_back_to_a_readable_key(self):
        with mock.patch.object(osutil, "IS_WINDOWS", True), mock.patch.object(osutil, "IS_MAC", False),                 mock.patch.object(osutil, "_dpapi", side_effect=OSError("no")):
            with self.assertRaises(osutil.SecretStorageError):
                osutil.protect("sk-secret")
        with mock.patch.object(osutil, "IS_WINDOWS", False), mock.patch.object(osutil, "IS_MAC", True),                 mock.patch.object(osutil, "_keychain_set", return_value=False):
            with self.assertRaises(osutil.SecretStorageError):
                osutil.protect("sk-secret")

    def test_a_failed_save_keeps_the_old_key_and_writes_nothing(self):
        s = Settings()
        with mock.patch.object(osutil, "protect", return_value="plain:b2xk"):
            s.connect("openai", "old-key", "gpt-x")
        before = (paths.home() / "settings.json").read_text("utf-8")
        with mock.patch.object(osutil, "protect", side_effect=osutil.SecretStorageError("no")):
            with self.assertRaises(osutil.SecretStorageError):
                s.connect("openai", "new-key", "gpt-x")
        self.assertEqual((paths.home() / "settings.json").read_text("utf-8"), before)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_the_settings_file_is_owner_only(self):
        Settings().update({"retrieval": {"top_k": 12}})
        self.assertEqual((paths.home() / "settings.json").stat().st_mode & 0o077, 0)

    def test_old_plain_keys_move_into_secure_storage_when_settings_load(self):
        (paths.home() / "settings.json").write_text(json.dumps(
            {"version": 2, "profiles": [{"id": "openai", "provider": "openai", "model": "m", "api_key_enc": "plain:" + "c2stb2xk"}],
             "embedding_keys": {"voyage": "plain:c2stb2xk"}}), "utf-8")
        with mock.patch.object(osutil, "IS_WINDOWS", True), mock.patch.object(osutil, "_dpapi", side_effect=lambda b, protect: b"E" + b):
            s = Settings()
        saved = json.loads((paths.home() / "settings.json").read_text("utf-8"))
        self.assertTrue(saved["profiles"][0]["api_key_enc"].startswith("dpapi:"))
        self.assertTrue(saved["embedding_keys"]["voyage"].startswith("dpapi:"))
        self.assertEqual(s.get()["profiles"][0]["id"], "openai")

    def test_a_failed_upgrade_leaves_the_old_key_working(self):
        with mock.patch.object(osutil, "IS_WINDOWS", True), mock.patch.object(osutil, "_dpapi", side_effect=OSError("no")):
            self.assertEqual(osutil.upgrade("plain:c2stb2xk"), "plain:c2stb2xk")


if __name__ == "__main__":
    unittest.main()
