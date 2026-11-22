"""Where the data lives, and what happens to data (and settings) made before the app was renamed to Perplexify."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (puts the repository on sys.path)

from perplexify import paths


def clean_environment():
    """A patch of os.environ with every data-folder setting removed (restored afterwards)."""
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    for name in ("PERPLEXIFY_HOME", "MANUALRAG_HOME", "PERPLEXIFY_NO_WARMUP", "MANUALRAG_NO_WARMUP"):
        os.environ.pop(name, None)
    return patcher


class Environment(unittest.TestCase):
    def setUp(self):
        self.patch = clean_environment()
        self.addCleanup(self.patch.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="perplexify_paths_"))

    def test_the_new_variable_names_the_data_folder(self):
        os.environ["PERPLEXIFY_HOME"] = str(self.tmp / "new")
        self.assertEqual(paths.home(), self.tmp / "new")
        self.assertTrue((self.tmp / "new").is_dir())

    def test_the_old_variable_name_still_works(self):
        os.environ["MANUALRAG_HOME"] = str(self.tmp / "old-style")
        self.assertEqual(paths.home(), self.tmp / "old-style")

    def test_the_new_variable_wins_when_both_are_set(self):
        os.environ["MANUALRAG_HOME"] = str(self.tmp / "old-style")
        os.environ["PERPLEXIFY_HOME"] = str(self.tmp / "new")
        self.assertEqual(paths.home(), self.tmp / "new")

    def test_other_settings_accept_both_names_too(self):
        os.environ["MANUALRAG_NO_WARMUP"] = "1"
        self.assertEqual(paths.env("NO_WARMUP"), "1")
        os.environ["PERPLEXIFY_NO_WARMUP"] = "2"
        self.assertEqual(paths.env("NO_WARMUP"), "2")
        self.assertEqual(paths.env("SOMETHING_UNSET"), "")


class FolderFromTheOldName(unittest.TestCase):
    """Libraries made while the app was called Manual RAG must not be lost to the rename."""

    def setUp(self):
        self.patch = clean_environment()
        self.addCleanup(self.patch.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="perplexify_paths_"))
        self.new, self.old = self.tmp / "Perplexify", self.tmp / "ManualRAG"
        self.defaults = mock.patch.object(paths, "_default_home", return_value=(self.new, self.old))
        self.defaults.start()
        self.addCleanup(self.defaults.stop)

    def old_folder_with_data(self):
        (self.old / "libraries").mkdir(parents=True)
        (self.old / "settings.json").write_text('{"x": 1}', "utf-8")

    def test_it_is_moved_across_once_with_everything_in_it(self):
        self.old_folder_with_data()
        self.assertEqual(paths.home(), self.new)
        self.assertEqual((self.new / "settings.json").read_text("utf-8"), '{"x": 1}')
        self.assertTrue((self.new / "libraries").is_dir())
        self.assertFalse(self.old.exists())
        self.assertEqual(paths.home(), self.new)  # and asking again changes nothing

    def test_an_existing_new_folder_is_never_overwritten_by_the_old_one(self):
        self.old_folder_with_data()
        self.new.mkdir()
        (self.new / "settings.json").write_text('{"x": 2}', "utf-8")
        self.assertEqual(paths.home(), self.new)
        self.assertEqual((self.new / "settings.json").read_text("utf-8"), '{"x": 2}')
        self.assertTrue(self.old.exists())

    def test_merely_looking_neither_creates_nor_moves_anything(self):
        self.old_folder_with_data()
        self.assertEqual(paths.home(create=False), self.new)
        self.assertFalse(self.new.exists())
        self.assertTrue(self.old.exists())

    def test_if_it_cannot_be_moved_the_old_folder_keeps_being_used(self):
        self.old_folder_with_data()
        with mock.patch.object(Path, "rename", side_effect=OSError("in use")):
            self.assertEqual(paths.home(), self.old)
        self.assertTrue((self.old / "settings.json").exists())

    def test_a_fresh_pc_gets_the_new_folder(self):
        self.assertEqual(paths.home(), self.new)
        self.assertTrue(self.new.is_dir())
        self.assertFalse(self.old.exists())


if __name__ == "__main__":
    unittest.main()
