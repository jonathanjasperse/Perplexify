"""The macOS app: how it finds what is bundled inside Perplexify.app, and that the build files agree with each other.
(The build itself runs on GitHub: .github/workflows/macos-installer.yml. These tests run anywhere.)"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempHome  # noqa: F401  (puts the repo on sys.path like the other tests)

from perplexify import localllm, paths

REPO = Path(__file__).resolve().parents[1]


class BundleLayout(unittest.TestCase):
    """Perplexify.app/Contents/MacOS/Perplexify is the running program; models and engine sit in Contents/Resources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        contents = Path(self.tmp.name) / "Perplexify.app" / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Resources" / "models" / "models--x").mkdir(parents=True)
        (contents / "Resources" / "llama").mkdir()
        (contents / "Resources" / "llama" / "llama-server").write_bytes(b"")
        (contents / "Resources" / "llama" / "libggml-metal.1.dylib").write_bytes(b"")
        (contents / "Resources" / "llama" / "libggml-metal.dylib").write_bytes(b"")
        (contents / "Resources" / "llama" / "libggml-cpu.1.dylib").write_bytes(b"")
        (contents / "Resources" / "llama" / "libggml-base.dylib").write_bytes(b"")
        self.exe = contents / "MacOS" / "Perplexify"
        self.exe.write_bytes(b"")
        self.res = contents / "Resources"
        self.addCleanup(self.tmp.cleanup)

    def frozen(self):
        return mock.patch.multiple(sys, frozen=True, executable=str(self.exe), create=True)

    def test_not_frozen_means_no_bundle(self):
        self.assertIsNone(paths.bundle_resources())

    def test_the_resources_folder_is_found_from_the_executable(self):
        with self.frozen():
            self.assertEqual(paths.bundle_resources().resolve(), self.res.resolve())

    def test_bundled_models_come_from_resources(self):
        with self.frozen(), mock.patch.dict("os.environ", {"PERPLEXIFY_BUNDLED_MODELS": ""}):
            self.assertEqual(paths.bundled_models_dir().resolve(), (self.res / "models").resolve())

    def test_the_engine_is_the_installed_one(self):
        with self.frozen(), mock.patch.dict("os.environ", {"PERPLEXIFY_LLAMA_DIR": "", "PERPLEXIFY_LLAMA_SERVER": ""}):
            self.assertTrue(localllm.installed_layout())
            self.assertEqual(localllm.installed_engine_dir().resolve(), (self.res / "llama").resolve())
            self.assertEqual([s for s, _ in localllm.engine_search_dirs()], ["installed", "data"])

    def test_metal_is_the_one_graphics_back_end_of_the_mac_engine(self):
        self.assertEqual(localllm.gpu_backends(self.res / "llama"), ["metal"])


class BuildFiles(unittest.TestCase):
    def test_the_workflow_builds_a_dmg_from_the_spec_and_checks_the_result(self):
        text = (REPO / ".github" / "workflows" / "macos-installer.yml").read_text("utf-8")
        for needle in ("pyinstaller", "installer/macos/Perplexify.spec", "fetch_llama.py", "hdiutil create", "selftest",
                       "workflow_dispatch", "tags:", "upload-artifact"):
            self.assertIn(needle, text, needle)

    def test_every_file_the_workflow_uses_exists(self):
        text = (REPO / ".github" / "workflows" / "macos-installer.yml").read_text("utf-8")
        for rel in sorted(set(re.findall(r"installer/macos/[\w.]+", text))):
            self.assertTrue((REPO / rel).is_file(), rel)

    def test_the_mac_engine_pins_name_both_architectures_with_checksums(self):
        pin = json.loads((REPO / "installer" / "pins.json").read_text("utf-8"))["llama_macos"]
        win = json.loads((REPO / "installer" / "pins.json").read_text("utf-8"))["llama"]
        self.assertEqual(pin["tag"], win["tag"], "Windows and macOS must ship the same llama.cpp build")
        for arch in ("arm64", "x64"):
            p = pin[arch]
            self.assertRegex(p["sha256"], r"^[0-9a-f]{64}$")
            self.assertIn(pin["tag"], p["url"])
            self.assertTrue(p["url"].endswith(p["asset"]))

    def test_the_spec_reads_the_version_and_ships_the_web_files(self):
        spec = (REPO / "installer" / "macos" / "Perplexify.spec").read_text("utf-8")
        self.assertIn("perplexify/web", spec)
        self.assertIn("com.perplexify.desktop", spec)
        self.assertIn("__version__", spec)

    def test_the_entry_point_supports_worker_processes(self):
        entry = (REPO / "installer" / "macos" / "entry.py").read_text("utf-8")
        self.assertIn("multiprocessing.freeze_support()", entry)
        self.assertIn("__main__", entry)


if __name__ == "__main__":
    unittest.main()
