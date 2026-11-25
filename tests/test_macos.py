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


class Dictation(unittest.TestCase):
    """Dictation needs the microphone and the speech model inside the app; without these a Mac refuses or the feature is missing."""

    def test_the_app_explains_the_microphone_and_is_allowed_to_use_it(self):
        spec = (REPO / "installer" / "macos" / "Perplexify.spec").read_text("utf-8")
        self.assertIn("NSMicrophoneUsageDescription", spec)
        self.assertIn("com.apple.security.device.audio-input", (REPO / "installer" / "macos" / "entitlements.plist").read_text("utf-8"))

    def test_the_speech_engine_and_model_are_bundled_and_tried_in_the_smoke_test(self):
        spec = (REPO / "installer" / "macos" / "Perplexify.spec").read_text("utf-8")
        for pkg in ("faster_whisper", "ctranslate2", "av"):
            self.assertIn(f'"{pkg}"', spec)
        wf = (REPO / ".github" / "workflows" / "macos-installer.yml").read_text("utf-8")
        self.assertIn("--whisper", wf)
        self.assertIn("/api/dictation/transcribe", wf)


class OldestMacOS(unittest.TestCase):
    def load(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("min_macos", REPO / "installer" / "macos" / "min_macos.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    OTOOL = """Load command 9
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 1
    minos 13.3
      sdk 14.0
Load command 10
      cmd LC_SOURCE_VERSION
Load command 3
      cmd LC_VERSION_MIN_MACOSX
  cmdsize 16
  version 10.9
      sdk 10.9
Load command 11
      cmd LC_BUILD_VERSION
  cmdsize 32
 platform 2
    minos 16.0
      sdk 17.0
"""

    def test_the_versions_a_file_asks_for_are_read_and_other_platforms_are_ignored(self):
        m = self.load()
        self.assertEqual(sorted(m.versions_in(self.OTOOL)), [(10, 9), (13, 3)])      # the iOS entry (platform 2) is not counted
        self.assertEqual(m.newest(m.versions_in(self.OTOOL)), (13, 3))
        self.assertIsNone(m.newest([]))
        self.assertEqual(m.parse_version("11"), (11, 0))
        self.assertGreater(m.parse_version("13.3"), m.parse_version("12.7"))     # compared as numbers, not as text

    def test_the_workflow_runs_it_before_signing(self):
        wf = (REPO / ".github" / "workflows" / "macos-installer.yml").read_text("utf-8")
        self.assertLess(wf.index("min_macos.py"), wf.index("- name: Sign the app"))


if __name__ == "__main__":
    unittest.main()
