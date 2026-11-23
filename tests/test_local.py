"""Local models (llama.cpp): the engine locator and pins, the process manager, downloads, the provider, the API and the question flow.

No real llama-server and no real model are used here: tests/fake_llama_server.py speaks just enough of its HTTP API and is started
through PERPLEXIFY_LLAMA_SERVER by the real process-manager code. (The real thing is checked by hand, see the README.)
"""
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from helpers import TempHome

from perplexify import localllm, osutil, paths, providers, settings as settings_mod
from perplexify.jobs import Cancelled, Job, JobManager
from perplexify.llm import LLMError, make_provider
from perplexify.llm.local import LocalProvider
from perplexify.llm.openai_compat import OpenAICompatProvider as OpenAIProviderForTest
from perplexify.retrieval import Hit
from perplexify.settings import Settings

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FAKE = str(HERE / "fake_llama_server.py")
WINDOWS = os.name == "nt"
BASE_PYTHON = getattr(sys, "_base_executable", None) or sys.executable


def wait_until(fn, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return bool(fn())


def gguf_bytes(arch="qwen2", ctx=32768, name="Test Model", with_vocab=False) -> bytes:
    """A minimal but valid GGUF header (no tensors): what the app reads to learn a model's architecture and window."""
    kvs = [("general.architecture", 8, arch)]
    if name:
        kvs.append(("general.name", 8, name))
    kvs.append((f"{arch}.context_length", 4, ctx))
    if with_vocab:
        kvs.append(("tokenizer.ggml.tokens", 9, ["a", "b", "c"]))
    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", 0, len(kvs))
    for k, t, v in kvs:
        kb = k.encode()
        out += struct.pack("<Q", len(kb)) + kb + struct.pack("<I", t)
        if t == 8:
            vb = v.encode()
            out += struct.pack("<Q", len(vb)) + vb
        elif t == 4:
            out += struct.pack("<I", v)
        elif t == 9:
            out += struct.pack("<I", 8) + struct.pack("<Q", len(v)) + b"".join(struct.pack("<Q", len(s)) + s.encode() for s in v)
    return out


def make_pe(path: Path, imports: list[str]) -> None:
    """A tiny Windows executable image that only has an import table: enough for localllm._pe_imports."""
    data = bytearray(0x600)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, 0x44, 0x8664, 1, 0, 0, 0, 240, 0x2022)
    opt = 0x44 + 20
    struct.pack_into("<H", data, opt, 0x20B)
    struct.pack_into("<II", data, opt + 112 + 8, 0x1000, 20 * (len(imports) + 1))        # import directory
    struct.pack_into("<8sIIIIIIHHI", data, opt + 240, b".idata", 0x400, 0x1000, 0x400, 0x200, 0, 0, 0, 0, 0x40000040)
    names_at = 0x1000 + 20 * (len(imports) + 1)
    for i, name in enumerate(imports):
        struct.pack_into("<IIIII", data, 0x200 + 20 * i, 0, 0, 0, names_at, 0)
        raw = names_at - 0x1000 + 0x200
        data[raw:raw + len(name) + 1] = name.encode() + b"\0"
        names_at += len(name) + 1
    path.write_bytes(bytes(data))


class FakeEngineCase(TempHome):
    """A fresh model folder, the fake llama-server, and a fresh engine manager for every test."""

    ENV = ("PERPLEXIFY_LLAMA_SERVER", "PERPLEXIFY_LLAMA_DIR", "FAKE_LLAMA_LOG")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self.ENV}
        for k in [k for k in os.environ if k.startswith("FAKE_LLAMA_")]:
            del os.environ[k]
        os.environ.pop("PERPLEXIFY_LLAMA_DIR", None)
        os.environ["PERPLEXIFY_LLAMA_SERVER"] = FAKE
        self.fake_log = Path(self._home) / f"fake-{self.id().rsplit('.', 1)[-1]}.log"
        self.fake_log.unlink(missing_ok=True)
        os.environ["FAKE_LLAMA_LOG"] = str(self.fake_log)
        shutil.rmtree(paths.llm_dir(), ignore_errors=True)
        Path(paths.settings_path()).unlink(missing_ok=True)
        localllm._gguf_cache.clear()
        self.mgr = localllm.EngineManager()
        p = mock.patch.object(localllm, "_manager", self.mgr)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.mgr.stop)
        self.addCleanup(self._restore_env)
        self.model = self.make_model("test-model.gguf")

    def _restore_env(self):
        for k in [k for k in os.environ if k.startswith("FAKE_LLAMA_")]:
            del os.environ[k]
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def make_model(self, name="test-model.gguf", **kw) -> Path:
        p = paths.llm_dir() / name
        p.write_bytes(gguf_bytes(**kw))
        return p

    def spec(self, n_ctx=4096, gpu="auto", model=None) -> localllm.EngineSpec:
        return localllm.EngineSpec(str(model or self.model), n_ctx, gpu)

    def events(self, kind=None) -> list[dict]:
        if not self.fake_log.exists():
            return []
        rows = [json.loads(line) for line in self.fake_log.read_text("utf-8").splitlines() if line.strip().startswith("{")]
        return [r for r in rows if kind is None or r["event"] == kind]

    def connect(self, **kw) -> dict:
        """Connect the "Local model" profile to the test model (in the running app's settings when the test has one)."""
        s = self.ctx.settings if getattr(self, "ctx", None) is not None else Settings()
        return s.connect_local("test-model.gguf", "", kw.get("context_tokens", 4096), kw.get("gpu", "auto"))


# ------------------------------------------------------------------------------------------------------ pins, catalog, locator
REAL_LOG = """\
0.00.004.029 I load_backend: loaded RPC backend from C:\\llama\\ggml-rpc.dll
0.00.149.601 I load_backend: loaded Vulkan backend from C:\\llama\\ggml-vulkan.dll
0.00.161.360 I cmn  common_param: device_info:
0.00.162.109 I cmn  common_param:   - Vulkan0 : Intel(R) Arc(TM) 140V GPU (16GB) (28016 MiB, 27248 MiB free)
0.00.410.836 I llama_prepare_model_devices: using device Vulkan0 (Intel(R) Arc(TM) 140V GPU (16GB)) (unknown id) - 27247 MiB free
0.00.664.253 I load_tensors: offloading output layer to GPU
0.00.664.261 I load_tensors: offloading 23 repeating layers to GPU
0.00.664.261 I load_tensors: offloaded 25/25 layers to GPU
"""     # what llama-server b12359 wrote at -lv 4 while loading a model on this test PC's graphics card


class RealEngineOutput(unittest.TestCase):
    """The formats are those of the real llama.cpp build the installer ships (checked against it by hand)."""

    def test_the_version_of_the_real_and_of_older_builds_is_read(self):
        self.assertEqual(localllm.parse_version("version: 1.0.0-dev (build 12359, commit b1b770d90)\nbuilt with Clang 20.1.8 for Windows x86_64"),
                         "b12359 (b1b770d)")
        self.assertEqual(localllm.parse_version("version: 4567 (abc1234def)"), "b4567 (abc1234)")
        self.assertIsNone(localllm.parse_version("llama-server: something else"))

    def test_the_graphics_card_is_read_from_the_log_of_the_real_engine(self):
        g = localllm.parse_gpu(REAL_LOG)
        self.assertEqual((g["device"], g["layers"], g["layers_total"], g["active"]), ("Intel(R) Arc(TM) 140V GPU (16GB)", 25, 25, True))
        cpu = localllm.parse_gpu("0.00.664.261 I load_tensors: offloaded 0/25 layers to GPU")
        self.assertFalse(cpu["active"])
        self.assertEqual(localllm.parse_gpu("0.01.865.050 I srv    load_model: initializing, n_slots = 1"), {"active": False})
        older = localllm.parse_gpu("ggml_vulkan: 0 = NVIDIA GeForce RTX 3060 (NVIDIA) | uma: 0 | fp16: 1\nload_tensors: offloaded 33/33 layers to GPU")
        self.assertEqual((older["device"], older["layers"]), ("NVIDIA GeForce RTX 3060", 33))

    def test_the_arguments_ask_for_the_log_level_that_names_the_card_and_keep_the_cache_off(self):
        args = localllm.build_args("m.gguf", 1234, "k", 4096)
        self.assertEqual(args[args.index("-lv") + 1], "4")
        for flag in ("--no-webui", "--cache-ram", "--no-cache-idle-slots", "--jinja", "-np"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("--cache-ram") + 1], "0")
        self.assertNotIn("--device", args)
        off = localllm.build_args("m", 1, "k", 1, "off")
        self.assertEqual(off[off.index("--device") + 1], "none")
        self.assertEqual(off[off.index("-ngl") + 1], "0")
        self.assertIn("--no-jinja", localllm.build_args("m", 1, "k", 1, "auto", jinja=False))

    def test_file_sizes_are_shown_the_way_hugging_face_shows_them(self):
        self.assertEqual(localllm._fmt_bytes(491_400_032), "491 MB")
        self.assertEqual(localllm._fmt_bytes(5_347_917_952), "5.3 GB")


class RequestScopeTests(unittest.TestCase):
    class Conn:
        def __init__(self): self.closed = 0
        def close(self): self.closed += 1

    def test_cancelling_closes_every_connection_of_the_question(self):
        scope, a, b = localllm.RequestScope(), self.Conn(), self.Conn()
        scope.add(a)
        scope.add(b)
        scope.discard(b)                       # a finished connection is not closed again
        scope.cancel()
        scope.cancel()
        self.assertEqual((a.closed, b.closed, scope.cancelled), (1, 0, True))

    def test_a_connection_made_after_the_question_was_stopped_is_closed_at_once(self):
        scope, late = localllm.RequestScope(), self.Conn()
        scope.cancel()
        scope.add(late)
        self.assertEqual(late.closed, 1)

    def test_a_connection_that_fails_to_close_does_not_stop_the_others(self):
        class Broken(self.Conn):
            def close(self): raise OSError("already gone")
        scope, ok = localllm.RequestScope(), self.Conn()
        scope.add(Broken())
        scope.add(ok)
        scope.cancel()
        self.assertEqual(ok.closed, 1)


class PinsAndCatalog(unittest.TestCase):
    def test_the_embedded_pin_equals_installer_pins_json(self):
        pins = json.loads((REPO / "installer" / "pins.json").read_text("utf-8"))["llama"]
        for key in ("tag", "commit", "asset", "url", "sha256", "size", "keep", "version"):
            self.assertEqual(localllm.EMBEDDED_PIN[key], pins[key], key)
        self.assertEqual(localllm.engine_pin()["sha256"], pins["sha256"])       # the source tree reads pins.json itself

    def test_the_pin_is_a_checked_github_release_asset(self):
        pin = localllm.EMBEDDED_PIN
        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(pin["url"].startswith("https://github.com/ggml-org/llama.cpp/releases/download/" + pin["tag"] + "/"))
        self.assertTrue(pin["url"].endswith(pin["asset"]))
        self.assertIn("win", pin["asset"])
        self.assertGreater(pin["size"], 10_000_000)

    def test_catalog_entries_are_complete_and_verifiable(self):
        cat = localllm.CATALOG
        self.assertTrue(5 <= len(cat) <= 6)
        self.assertEqual(len({c["id"] for c in cat}), len(cat))
        self.assertEqual([c["size"] for c in cat], sorted(c["size"] for c in cat), "listed from tiny to large")
        for c in cat:
            self.assertRegex(c["revision"], r"^[0-9a-f]{40}$", "the URL names a repository commit, so the file cannot change")
            self.assertRegex(c["sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(c["file"].endswith(".gguf"))
            self.assertTrue(localllm.catalog_url(c).startswith(f"https://huggingface.co/{c['repo']}/resolve/{c['revision']}/{c['file']}"))
            self.assertGreater(c["size"], 100_000_000)
            self.assertTrue(c["summary"].strip() and c["name"] and c["params"] and c["quant"] and isinstance(c["ram_gb"], int))
        self.assertLess(cat[0]["size"], 600_000_000, "the first model is the tiny one for a quick test")
        self.assertIn("quick test", cat[0]["tags"])
        self.assertLessEqual(max(c["ram_gb"] for c in cat), 8)


class Locator(TempHome):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("PERPLEXIFY_LLAMA_SERVER", "PERPLEXIFY_LLAMA_DIR")}
        for k in self._saved:
            os.environ.pop(k, None)
        self.addCleanup(self._restore)
        self.work = Path(self._home) / "locator"
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir()
        shutil.rmtree(paths.llama_dir(), ignore_errors=True)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def exe_in(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / localllm.ENGINE_EXE
        p.write_bytes(b"x")
        return p

    def test_nothing_installed(self):
        self.assertIsNone(localllm.locate_engine())
        self.assertFalse(localllm.engine_info()["found"])
        self.assertIn("download-llama", localllm.engine_missing_message())

    def test_data_folder_is_the_last_resort(self):
        exe = self.exe_in(paths.llama_dir())
        found = localllm.locate_engine()
        self.assertEqual((found.exe, found.source), (exe, "data"))

    def test_environment_folder_comes_first(self):
        self.exe_in(paths.llama_dir())
        exe = self.exe_in(self.work / "mine")
        os.environ["PERPLEXIFY_LLAMA_DIR"] = str(self.work / "mine")
        found = localllm.locate_engine()
        self.assertEqual((found.exe, found.source), (exe, "environment"))

    def test_the_installed_layout_looks_next_to_the_app(self):
        # <install>\runtime, <install>\app\perplexify, <install>\llama
        (self.work / "runtime").mkdir()
        (self.work / "app" / "perplexify").mkdir(parents=True)
        exe = self.exe_in(self.work / "llama")
        self.exe_in(paths.llama_dir())
        with mock.patch.object(paths, "package_dir", lambda: self.work / "app" / "perplexify"):
            self.assertTrue(localllm.installed_layout())
            found = localllm.locate_engine()
            self.assertEqual((found.exe, found.source), (exe, "installed"))
            os.environ["PERPLEXIFY_LLAMA_DIR"] = str(self.work / "elsewhere")      # missing: falls through to the next place
            self.assertEqual(localllm.locate_engine().source, "installed")
        shutil.rmtree(self.work / "llama")
        with mock.patch.object(paths, "package_dir", lambda: self.work / "app" / "perplexify"):
            os.environ.pop("PERPLEXIFY_LLAMA_DIR")
            self.assertEqual(localllm.locate_engine().source, "data", "a missing installed engine falls back to the data folder (Download engine puts it there)")
            shutil.rmtree(paths.llama_dir(), ignore_errors=True)
            self.assertIsNone(localllm.locate_engine())
            msg = localllm.engine_missing_message()
            self.assertIn("Reinstall Perplexify", msg)
            self.assertIn("Download engine", msg)
            self.assertIn(str(self.work / "llama"), msg, "says where the installer should have put it")

    def test_a_source_checkout_is_not_an_installed_layout(self):
        self.assertFalse(localllm.installed_layout())

    def test_an_explicit_program_beats_every_folder(self):
        self.exe_in(paths.llama_dir())
        fake = self.work / "fake.py"
        fake.write_text("print('hi')")
        os.environ["PERPLEXIFY_LLAMA_SERVER"] = str(fake)
        self.assertEqual(localllm.locate_engine().source, "override")
        os.environ["PERPLEXIFY_LLAMA_SERVER"] = str(self.work / "nope.py")      # does not exist: ignored
        self.assertEqual(localllm.locate_engine().source, "data")

    def test_version_check_runs_the_program_once_and_caches(self):
        os.environ["PERPLEXIFY_LLAMA_SERVER"] = FAKE
        eng = localllm.locate_engine()
        localllm._version_cache.clear()
        self.assertEqual(localllm.engine_version(eng), "b12359 (b1b770d)")
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("must be cached")):
            self.assertEqual(localllm.engine_version(eng), "b12359 (b1b770d)")
        self.assertTrue(localllm.engine_info()["found"])
        self.assertEqual(localllm.engine_info()["version"], "b12359 (b1b770d)")

    def test_a_program_that_does_not_run_is_not_found_by_the_info(self):
        broken = self.work / "broken.py"
        broken.write_text("import sys; sys.exit(3)")
        os.environ["PERPLEXIFY_LLAMA_SERVER"] = str(broken)
        info = localllm.engine_info()
        self.assertFalse(info["found"])
        self.assertIn("did not start", info["message"])

    def test_graphics_back_ends_are_listed(self):
        d = self.work / "eng"
        d.mkdir()
        for n in ("ggml-vulkan.dll", "ggml-cpu-haswell.dll", "ggml-base.dll", "ggml-rpc.dll", "llama.dll"):
            (d / n).write_bytes(b"x")
        self.assertEqual(localllm.gpu_backends(d), ["vulkan"])


# ------------------------------------------------------------------------------------------------------ reading models, hardware
class GgufReader(TempHome):
    def setUp(self):
        self.dir = Path(self._home) / "gguf"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir()
        localllm._gguf_cache.clear()

    def test_reads_architecture_name_and_window(self):
        p = self.dir / "a.gguf"
        p.write_bytes(gguf_bytes("llama", 131072, "Llama 3.2 1B"))
        g = localllm.read_gguf_info(p)
        self.assertTrue(g["ok"])
        self.assertEqual((g["architecture"], g["context_length"], g["name"], g["version"]), ("llama", 131072, "Llama 3.2 1B", 3))

    def test_stops_at_the_vocabulary_and_still_has_the_window(self):
        p = self.dir / "a.gguf"
        p.write_bytes(gguf_bytes("qwen2", 8192, name="", with_vocab=True))
        g = localllm.read_gguf_info(p)
        self.assertTrue(g["ok"])
        self.assertEqual((g["architecture"], g["context_length"]), ("qwen2", 8192))

    def test_other_files_are_refused_with_a_reason(self):
        text = self.dir / "notes.gguf"
        text.write_text("this is not a model")
        self.assertFalse(localllm.read_gguf_info(text)["ok"])
        self.assertIn("not a GGUF", localllm.read_gguf_info(text)["error"])
        empty = self.dir / "empty.gguf"
        empty.write_bytes(b"")
        self.assertFalse(localllm.read_gguf_info(empty)["ok"])
        old = self.dir / "old.gguf"
        old.write_bytes(b"GGUF" + struct.pack("<I", 1) + b"\0" * 16)
        self.assertIn("version 1", localllm.read_gguf_info(old)["error"])
        self.assertFalse(localllm.read_gguf_info(self.dir / "missing.gguf")["ok"])

    def test_model_entries_name_catalog_models(self):
        c = localllm.CATALOG[0]
        p = self.dir / c["file"]
        p.write_bytes(gguf_bytes())
        e = localllm.model_entry(p, linked=True)
        self.assertEqual((e["catalog_id"], e["title"], e["linked"], e["valid"]), (c["id"], c["name"], True, True))


class HardwareAndContext(unittest.TestCase):
    GB = localllm.GIB

    def test_automatic_context_follows_the_memory_and_the_model(self):
        self.assertEqual(localllm.auto_context(int(0.5 * self.GB), 131072, 32 * self.GB), 16384)
        self.assertEqual(localllm.auto_context(5 * self.GB, 131072, 16 * self.GB), 8192)
        self.assertEqual(localllm.auto_context(5 * self.GB, 131072, 8 * self.GB), 4096)
        self.assertEqual(localllm.auto_context(1 * self.GB, 8192, 32 * self.GB), 8192, "never above what the model was trained for")
        self.assertEqual(localllm.auto_context(1 * self.GB, None, 0), 8192)

    def test_a_model_that_is_too_big_is_flagged(self):
        with mock.patch.object(localllm, "memory_info", lambda: {"total": 8 * self.GB, "available": 2 * self.GB}):
            self.assertEqual(localllm.fit_hint(7 * self.GB, 8192)["level"], "too_big")
            tight = localllm.fit_hint(int(1.8 * self.GB), 8192)
            self.assertEqual(tight["level"], "tight")
            self.assertIn("free right now", tight["message"])
            self.assertEqual(localllm.fit_hint(300 * 1024 ** 2, 4096)["level"], "ok")

    def test_this_computer_reports_its_memory(self):
        mem = localllm.memory_info()
        self.assertGreater(mem["total"], 256 * 1024 ** 2)
        self.assertLessEqual(mem["available"], mem["total"])
        hw = localllm.hardware()
        self.assertEqual(set(hw), {"ram_total", "ram_available", "cpu_threads", "gpu"})

    def test_the_memory_estimate_grows_with_the_window(self):
        a = localllm.estimate_ram_need(2 * self.GB, 4096)
        b = localllm.estimate_ram_need(2 * self.GB, 32768)
        self.assertGreater(b, a)
        self.assertGreater(a, 2 * self.GB)


# ------------------------------------------------------------------------------------------------------ the engine process
class ProcessManager(FakeEngineCase):
    def test_start_reuse_and_stop(self):
        h1 = self.mgr.ensure(self.spec())
        h2 = self.mgr.ensure(self.spec())
        self.assertEqual((h1.pid, h1.port, h1.api_key), (h2.pid, h2.port, h2.api_key))
        self.assertEqual(len(self.events("start")), 1, "the same settings share one process")
        st = self.mgr.status()
        self.assertEqual((st["state"], st["n_ctx"], st["pid"]), ("ready", 4096, h1.pid))
        self.assertTrue(osutil.pid_alive(h1.pid))
        self.mgr.stop()
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(h1.pid)))
        self.assertEqual(self.mgr.status()["state"], "stopped")
        self.mgr.stop()                       # a second stop is harmless
        h3 = self.mgr.ensure(self.spec())
        self.assertNotEqual(h3.pid, h1.pid)

    def test_it_listens_on_localhost_only_behind_a_key(self):
        h = self.mgr.ensure(self.spec())
        args = self.events("start")[0]["args"]
        self.assertEqual(args[args.index("--host") + 1], "127.0.0.1")
        self.assertEqual(args[args.index("--port") + 1], str(h.port))
        self.assertEqual(args[args.index("--api-key") + 1], h.api_key)
        self.assertGreaterEqual(len(h.api_key), 24)
        self.assertEqual(args[args.index("-c") + 1], "4096")
        for needed in ("--no-webui", "--jinja", "-np", "-m"):
            self.assertIn(needed, args)
        self.assertEqual(h.base_url, f"http://127.0.0.1:{h.port}/v1")
        url = f"http://127.0.0.1:{h.port}/props"
        self.assertEqual(httpx.get(url, trust_env=False).status_code, 401)
        self.assertEqual(httpx.get(url, headers={"Authorization": "Bearer wrong"}, trust_env=False).status_code, 401)
        ok = httpx.get(url, headers={"Authorization": f"Bearer {h.api_key}"}, trust_env=False)
        self.assertEqual(ok.json()["default_generation_settings"]["n_ctx"], 4096)

    def test_every_start_gets_a_new_key_and_the_log_never_shows_it(self):
        h1 = self.mgr.ensure(self.spec())
        self.mgr.stop()
        h2 = self.mgr.ensure(self.spec())
        self.assertNotEqual(h1.api_key, h2.api_key)
        log = Path(self.mgr.status()["log"]).read_text("utf-8", "replace")
        self.assertIn("<key>", log)
        self.assertNotIn(h1.api_key, log)
        self.assertNotIn(h2.api_key, log)

    def test_other_settings_restart_the_engine(self):
        h1 = self.mgr.ensure(self.spec(4096))
        h2 = self.mgr.ensure(self.spec(8192))
        self.assertNotEqual(h1.pid, h2.pid)
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(h1.pid)), "only one engine runs at a time")
        self.assertEqual((h2.n_ctx, len(self.events("start"))), (8192, 2))
        other = self.make_model("other.gguf")
        h3 = self.mgr.ensure(self.spec(8192, model=other))
        self.assertNotEqual(h3.pid, h2.pid)
        self.assertEqual(self.mgr.status()["model"], "other.gguf")

    def test_gpu_off_never_starts_the_graphics_back_end(self):
        self.mgr.ensure(self.spec(gpu="off"))
        args = self.events("start")[0]["args"]
        self.assertEqual(args[args.index("--device") + 1], "none")
        self.assertEqual(args[args.index("-ngl") + 1], "0")
        self.mgr.ensure(self.spec(gpu="auto"))
        auto = self.events("start")[1]["args"]
        self.assertNotIn("--device", auto)
        self.assertNotIn("-ngl", auto)

    def test_the_graphics_card_is_reported_from_the_engine_log(self):
        os.environ["FAKE_LLAMA_GPU"] = "1"
        h = self.mgr.ensure(self.spec())
        self.assertEqual((h.gpu["layers"], h.gpu["layers_total"], h.gpu["device"], h.gpu["active"]), (25, 25, "Fake GPU 9000 (16GB)", True))

    def test_concurrent_requests_share_one_start(self):
        os.environ["FAKE_LLAMA_LOAD_DELAY"] = "0.6"
        out, errors = [], []

        def go():
            try:
                out.append(self.mgr.ensure(self.spec()))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual({h.pid for h in out}, {out[0].pid})
        self.assertEqual(len(self.events("start")), 1)

    def test_start_does_not_block_and_the_status_says_loading(self):
        os.environ["FAKE_LLAMA_LOAD_DELAY"] = "1.0"
        t0 = time.time()
        att = self.mgr.start(self.spec())
        self.assertLess(time.time() - t0, 0.5)
        self.assertFalse(att.done.is_set())
        st = self.mgr.status()
        self.assertEqual((st["state"], st["model"], st["n_ctx"]), ("loading", "test-model.gguf", 4096))
        self.assertIs(self.mgr.start(self.spec()), att, "a second asker joins the running start")
        self.assertIsNone(self.mgr.current())
        h = att.result()
        self.assertEqual(self.mgr.status()["state"], "ready")
        self.assertGreaterEqual(h.load_seconds, 0.9)
        self.assertIs(self.mgr.current(self.spec()), h)
        self.assertIsNone(self.mgr.current(self.spec(8192)), "the window is part of the settings")

    def test_a_request_for_other_settings_waits_for_the_running_start_then_restarts(self):
        os.environ["FAKE_LLAMA_LOAD_DELAY"] = "0.8"
        first = self.mgr.start(self.spec(4096))
        second = self.mgr.start(self.spec(8192))
        self.assertEqual(first.result().n_ctx, 4096)
        self.assertEqual(second.result().n_ctx, 8192)
        self.assertEqual(self.mgr.current().n_ctx, 8192)

    def test_load_failures_are_explained_in_plain_words(self):
        cases = [("oom", "not enough free memory", "failed to allocate"), ("arch", "zorblax", "unknown model architecture"),
                 ("corrupt", "damaged or incomplete", "invalid magic"), ("other", "stopped while loading", "went wrong")]
        for kind, in_message, in_log in cases:
            os.environ["FAKE_LLAMA_FAIL"] = kind
            with self.assertRaises(localllm.LocalLLMError) as cm:
                self.mgr.ensure(self.spec())
            self.assertIn(in_message, str(cm.exception).lower().replace("  ", " "), kind)
            self.assertEqual(cm.exception.code, "engine_failed")
            self.assertIn(in_log, cm.exception.detail, "the tail of the engine log comes with the error")
            st = self.mgr.status()
            self.assertEqual((st["state"], st["error_code"]), ("error", "engine_failed"))
            self.assertIn("test-model.gguf", str(cm.exception))
        del os.environ["FAKE_LLAMA_FAIL"]
        self.mgr.ensure(self.spec())
        self.assertEqual(self.mgr.status()["state"], "ready", "the next start clears the error")

    def test_a_chat_template_the_engine_cannot_use_falls_back(self):
        os.environ["FAKE_LLAMA_FAIL"] = "template"
        h = self.mgr.ensure(self.spec())
        starts = self.events("start")
        self.assertEqual(len(starts), 2)
        self.assertIn("--jinja", starts[0]["args"])
        self.assertIn("--no-jinja", starts[1]["args"])
        self.assertTrue(osutil.pid_alive(h.pid))

    def test_a_broken_graphics_driver_falls_back_to_the_cpu(self):
        os.environ["FAKE_LLAMA_FAIL"] = "gpu"
        h = self.mgr.ensure(self.spec(gpu="auto"))
        starts = self.events("start")
        self.assertEqual(len(starts), 2)
        self.assertNotIn("--device", starts[0]["args"])
        self.assertEqual(starts[1]["args"][starts[1]["args"].index("--device") + 1], "none")
        self.assertTrue(h.gpu.get("fell_back"))
        self.mgr.stop()
        os.environ["FAKE_LLAMA_FAIL"] = "gpu"        # with the card switched off by the user there is nothing to fall back from
        h2 = self.mgr.ensure(self.spec(gpu="off"))
        self.assertTrue(osutil.pid_alive(h2.pid))

    def test_a_taken_port_is_retried_with_another(self):
        os.environ["FAKE_LLAMA_FAIL"] = "bind:2"
        h = self.mgr.ensure(self.spec())
        starts = self.events("start")
        self.assertEqual(len(starts), 3)
        self.assertEqual(h.port, int(starts[2]["args"][starts[2]["args"].index("--port") + 1]))

    def test_a_crashed_engine_is_noticed_and_started_again(self):
        os.environ["FAKE_LLAMA_CRASH_AFTER"] = "0.4"
        h = self.mgr.ensure(self.spec())
        self.assertTrue(wait_until(lambda: self.mgr.current() is None, 10))
        st = self.mgr.status()
        self.assertEqual((st["state"], st["error_code"]), ("error", "engine_died"))
        self.assertIn("stopped unexpectedly", st["error"])
        del os.environ["FAKE_LLAMA_CRASH_AFTER"]
        h2 = self.mgr.ensure(self.spec())
        self.assertNotEqual(h2.pid, h.pid)
        self.assertEqual(self.mgr.status()["state"], "ready")

    def test_a_missing_engine_says_what_to_do(self):
        del os.environ["PERPLEXIFY_LLAMA_SERVER"]
        with self.assertRaises(localllm.LocalLLMError) as cm:
            self.mgr.ensure(self.spec())
        self.assertEqual(cm.exception.code, "engine_missing")
        self.assertIn("download-llama", str(cm.exception))
        with mock.patch.object(localllm, "installed_layout", lambda: True):
            self.assertIn("Reinstall", localllm.engine_missing_message())

    def test_a_missing_or_damaged_model_file_is_refused_before_anything_starts(self):
        with self.assertRaises(localllm.LocalLLMError) as cm:
            self.mgr.ensure(self.spec(model=paths.llm_dir() / "nothing.gguf"))
        self.assertEqual(cm.exception.code, "model_missing")
        bad = paths.llm_dir() / "bad.gguf"
        bad.write_text("not a model")
        with self.assertRaises(localllm.LocalLLMError) as cm:
            self.mgr.ensure(self.spec(model=bad))
        self.assertIn("not a usable model file", str(cm.exception))
        self.assertEqual(self.events("start"), [])

    def test_stopping_while_it_loads_ends_the_start_and_the_process(self):
        os.environ["FAKE_LLAMA_LOAD_DELAY"] = "4"
        att = self.mgr.start(self.spec())
        self.assertTrue(wait_until(lambda: self.events("start"), 10))
        pid = self.events("start")[0]["pid"]
        self.mgr.stop()
        with self.assertRaises(localllm.LocalLLMError):
            att.result()
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(pid)))

    def test_the_state_file_follows_the_process(self):
        h = self.mgr.ensure(self.spec())
        files = list(paths.engine_state_dir().glob("*.json"))
        self.assertEqual(len(files), 1)
        info = json.loads(files[0].read_text())
        self.assertEqual((info["pid"], info["owner_pid"]), (h.pid, os.getpid()))
        self.mgr.stop()
        self.assertEqual(list(paths.engine_state_dir().glob("*.json")), [])


@unittest.skipUnless(WINDOWS, "job objects and process identities are a Windows feature")
class Orphans(FakeEngineCase):
    """Whatever happens to the app, llama-server must not stay behind."""

    def spawn_fake(self, port=None):
        port = port or localllm._free_port()
        p = subprocess.Popen([BASE_PYTHON, "-u", FAKE, "--port", str(port), "--api-key", "k", "-c", "4096", "-m", "x"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=localllm.CREATE_NO_WINDOW)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.assertTrue(wait_until(lambda: self._up(port), 15), "the fake engine did not come up")
        return p

    @staticmethod
    def _up(port):
        try:
            return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1, trust_env=False).status_code == 200
        except httpx.HTTPError:
            return False

    def state_file(self, engine, owner_pid, owner_created, **over):
        ident = localllm._proc_identity(engine.pid)
        info = {"pid": engine.pid, "image": ident[0], "created": ident[1], "owner_pid": owner_pid, "owner_created": owner_created, **over}
        f = paths.engine_state_dir() / f"{owner_pid}.json"
        f.write_text(json.dumps(info))
        return f

    def test_an_engine_whose_app_is_gone_is_ended_at_the_next_start(self):
        engine = self.spawn_fake()
        dead = subprocess.Popen([BASE_PYTHON, "-c", "pass"])
        dead.wait()
        f = self.state_file(engine, dead.pid, 12345)
        self.assertEqual(localllm.sweep_orphans(), 1)
        self.assertTrue(wait_until(lambda: engine.poll() is not None))
        self.assertFalse(f.exists())

    def test_an_engine_whose_app_still_runs_is_left_alone(self):
        engine = self.spawn_fake()
        owner = subprocess.Popen([BASE_PYTHON, "-c", "import time; time.sleep(60)"])
        self.addCleanup(owner.kill)
        self.assertTrue(wait_until(lambda: localllm._proc_identity(owner.pid) is not None))
        f = self.state_file(engine, owner.pid, localllm._proc_identity(owner.pid)[1])
        self.assertEqual(localllm.sweep_orphans(), 0)
        time.sleep(0.3)
        self.assertIsNone(engine.poll())
        self.assertTrue(f.exists())

    def test_a_recycled_process_id_is_never_touched(self):
        innocent = subprocess.Popen([BASE_PYTHON, "-c", "import time; time.sleep(60)"])
        self.addCleanup(innocent.kill)
        self.assertTrue(wait_until(lambda: localllm._proc_identity(innocent.pid) is not None))
        dead = subprocess.Popen([BASE_PYTHON, "-c", "pass"])
        dead.wait()
        ident = localllm._proc_identity(innocent.pid)
        f = paths.engine_state_dir() / "1.json"
        f.write_text(json.dumps({"pid": innocent.pid, "image": ident[0], "created": ident[1] + 12345, "owner_pid": dead.pid, "owner_created": 1}))
        self.assertEqual(localllm.sweep_orphans(), 0)
        self.assertIsNone(innocent.poll())
        self.assertFalse(f.exists(), "the stale record is dropped")

    def test_a_damaged_state_file_is_dropped(self):
        f = paths.engine_state_dir() / "2.json"
        f.write_text("{not json")
        self.assertEqual(localllm.sweep_orphans(), 0)
        self.assertFalse(f.exists())

    def test_killing_the_app_hard_takes_the_engine_down_with_it(self):
        code = (f"import os, sys, time\nsys.path.insert(0, {str(REPO)!r})\nfrom perplexify import localllm\n"
                f"h = localllm.ensure_engine(localllm.EngineSpec({str(self.model)!r}, 4096, 'auto'))\n"
                "print('UP', os.getpid(), h.pid, flush=True)\ntime.sleep(300)\n")
        env = dict(os.environ, PERPLEXIFY_NO_WARMUP="1")
        app = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        self.addCleanup(lambda: app.poll() is None and app.kill())
        line = app.stdout.readline()
        self.assertTrue(line.startswith("UP"), line + app.stderr.read() if app.poll() is not None else line)
        _, app_pid, engine_pid = line.split()
        app_pid, engine_pid = int(app_pid), int(engine_pid)
        self.assertTrue(osutil.pid_alive(engine_pid))
        os.kill(app_pid, signal.SIGTERM)             # TerminateProcess: no atexit, no shutdown hooks, nothing runs
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(app_pid), 10))
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(engine_pid), 10), "the job object must end the engine with the app")

    def test_the_engine_is_in_its_own_job_so_stopping_ends_all_of_its_processes(self):
        h = self.mgr.ensure(self.spec())
        self.assertIsNotNone(self.mgr._job)
        self.mgr.stop()
        self.assertIsNone(self.mgr._job)
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(h.pid)))


# ------------------------------------------------------------------------------------------------------ downloads
class RangeServer:
    """A local web server with HTTP Range support, for the download tests."""

    def __init__(self, payload: bytes, drop_after: int | None = None, ignore_range: bool = False):
        self.payload, self.drop_after, self.ignore_range = payload, drop_after, ignore_range
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append({"path": self.path, "range": self.headers.get("Range")})
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/file.bin")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path in ("/missing", "/gated"):
                    code = 404 if self.path == "/missing" else 403
                    self.send_response(code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = 0
                rng = self.headers.get("Range")
                if rng and not outer.ignore_range:
                    start = int(rng.split("=")[1].split("-")[0])
                if start >= len(outer.payload):
                    self.send_response(416)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = outer.payload[start:]
                self.send_response(206 if (rng and not outer.ignore_range) else 200)
                if rng and not outer.ignore_range:
                    self.send_header("Content-Range", f"bytes {start}-{len(outer.payload) - 1}/{len(outer.payload)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                first = len(outer.requests) == 1
                try:
                    if outer.drop_after is not None and first:
                        self.wfile.write(body[:outer.drop_after])
                        self.wfile.flush()
                        self.connection.shutdown(socket.SHUT_RDWR)       # the connection dies half way through the body
                        return
                    for i in range(0, len(body), 65536):
                        self.wfile.write(body[i:i + 65536])
                except OSError:
                    pass

        class Quiet(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):       # a client that hangs up is part of what is tested
                pass

        self.httpd = Quiet(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Downloads(TempHome):
    PAYLOAD = os.urandom(3 * 1024 * 1024 + 123)
    SHA = hashlib.sha256(PAYLOAD).hexdigest()

    def setUp(self):
        self.dir = Path(self._home) / "dl"
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir()
        self.dest = self.dir / "model.gguf"
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def serve(self, **kw) -> RangeServer:
        s = RangeServer(self.PAYLOAD, **kw)
        self.servers.append(s)
        return s

    def get(self, server, path="/file.bin", **kw):
        calls = []
        kw.setdefault("size", len(self.PAYLOAD))
        kw.setdefault("sha256", self.SHA)
        localllm.download_file(server.base + path, self.dest, progress=lambda *a: calls.append(a), label="the test file", **kw)
        return calls

    def test_a_download_is_checked_and_gets_its_final_name(self):
        s = self.serve()
        calls = self.get(s)
        self.assertEqual(self.dest.read_bytes(), self.PAYLOAD)
        self.assertFalse(self.dest.with_name("model.gguf.part").exists())
        self.assertEqual({c[0] for c in calls}, {"download", "verify"})
        self.assertEqual(calls[-1][0], "verify")
        self.assertTrue(any(c[0] == "download" and c[1] == len(self.PAYLOAD) for c in calls))
        self.assertEqual(self.get(self.serve(), "/redirect") and self.dest.read_bytes(), self.PAYLOAD, "redirects are followed")

    def test_an_interrupted_download_resumes_where_it_stopped(self):
        s = self.serve(drop_after=1_200_000)
        self.get(s)
        self.assertEqual(self.dest.read_bytes(), self.PAYLOAD)
        ranges = [r["range"] for r in s.requests]
        self.assertIsNone(ranges[0])
        start = int(ranges[1].split("=")[1].rstrip("-"))
        self.assertTrue(1_000_000 <= start <= 1_200_000, f"the second request asks only for the rest, not for {ranges[1]}")
        self.assertEqual(len(ranges), 2)

    def test_a_part_file_from_an_earlier_run_is_continued(self):
        part = self.dest.with_name("model.gguf.part")
        part.write_bytes(self.PAYLOAD[:1_000_000])
        s = self.serve()
        self.get(s)
        self.assertEqual(self.dest.read_bytes(), self.PAYLOAD)
        self.assertEqual([r["range"] for r in s.requests], ["bytes=1000000-"])

    def test_a_server_that_ignores_range_still_gives_the_whole_file(self):
        self.dest.with_name("model.gguf.part").write_bytes(self.PAYLOAD[:500_000])
        s = self.serve(ignore_range=True)
        self.get(s)
        self.assertEqual(self.dest.read_bytes(), self.PAYLOAD)

    def test_stopping_keeps_the_part_and_a_second_run_finishes(self):
        s = self.serve()
        seen = []

        def check():
            seen.append(1)
            part = self.dest.with_name("model.gguf.part")
            if part.exists() and part.stat().st_size > 1_000_000:
                raise Cancelled()

        with self.assertRaises(Cancelled):
            localllm.download_file(s.base + "/file.bin", self.dest, size=len(self.PAYLOAD), sha256=self.SHA, check=check)
        part = self.dest.with_name("model.gguf.part")
        self.assertTrue(part.exists())
        self.assertFalse(self.dest.exists())
        have = part.stat().st_size
        self.assertTrue(0 < have < len(self.PAYLOAD))
        self.get(s)
        self.assertEqual(self.dest.read_bytes(), self.PAYLOAD)
        self.assertEqual(s.requests[-1]["range"], f"bytes={have}-")

    def test_a_wrong_checksum_deletes_the_file(self):
        s = self.serve()
        with self.assertRaises(localllm.DownloadError) as cm:
            self.get(s, sha256="0" * 64)
        self.assertIn("damaged", str(cm.exception))
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name("model.gguf.part").exists())

    def test_a_wrong_size_is_refused(self):
        s = self.serve()
        with self.assertRaises(localllm.DownloadError):
            self.get(s, size=len(self.PAYLOAD) + 10, sha256=None)
        self.assertFalse(self.dest.exists())

    def test_missing_and_refused_files_are_explained(self):
        s = self.serve()
        with self.assertRaises(localllm.DownloadError) as cm:
            self.get(s, "/missing")
        self.assertIn("404", str(cm.exception))
        with self.assertRaises(localllm.DownloadError) as cm:
            self.get(s, "/gated")
        self.assertIn("refused", str(cm.exception))

    def test_not_enough_disk_space_is_said_before_downloading(self):
        s = self.serve()
        Usage = type("Usage", (), {"free": 1024})
        with mock.patch.object(shutil, "disk_usage", lambda p: Usage()):
            with self.assertRaises(localllm.DownloadError) as cm:
                self.get(s)
        self.assertIn("free disk space", str(cm.exception))
        self.assertEqual(s.requests, [])

    def test_downloads_appear_in_the_job_list_without_waiting_in_the_indexing_queue(self):
        jobs = JobManager()
        gate = threading.Event()
        blocker = jobs.submit("crawl", "a long indexing job", "lib", lambda j: gate.wait(20))     # holds the single worker
        s = self.serve()
        item = {"id": "tiny-test", "name": "Tiny test", "file": "tiny.gguf", "size": len(self.PAYLOAD), "sha256": self.SHA,
                "repo": "x/y", "revision": "0" * 40, "params": "1B", "quant": "Q4", "license": "MIT", "ram_gb": 1, "summary": "s", "tags": []}
        with mock.patch.object(localllm, "CATALOG", [item]), mock.patch.object(localllm, "catalog_url", lambda i: s.base + "/file.bin"):
            job = localllm.start_model_download(jobs, "tiny-test")
            self.assertIn(job.id, [j["id"] for j in jobs.list()], "the sidebar lists it like any other job")
            self.assertIs(localllm.start_model_download(jobs, "tiny-test"), job, "asking again joins the running download")
            self.assertTrue(wait_until(lambda: job.status in ("done", "error"), 30), job.to_dict())
            self.assertEqual(job.status, "done", job.error)
            self.assertEqual((paths.llm_dir() / "tiny.gguf").read_bytes(), self.PAYLOAD)
            self.assertEqual(localllm.catalog_state(item)["state"], "installed")
            self.assertEqual(blocker.status, "running", "the indexing job was not held up or touched")
            with self.assertRaises(ValueError):
                localllm.start_model_download(jobs, "tiny-test")           # already installed
            (paths.llm_dir() / "tiny.gguf").unlink()
        gate.set()

    def test_a_download_job_can_be_stopped_from_the_sidebar_and_resumed(self):
        jobs = JobManager()
        s = self.serve()
        item = {"id": "tiny-test2", "name": "Tiny test", "file": "tiny2.gguf", "size": len(self.PAYLOAD), "sha256": self.SHA,
                "repo": "x/y", "revision": "0" * 40, "params": "1B", "quant": "Q4", "license": "MIT", "ram_gb": 1, "summary": "s", "tags": []}
        part = paths.llm_dir() / "tiny2.gguf.part"
        part.unlink(missing_ok=True)

        def slow(job):
            def check():
                job.check()
                time.sleep(0.05)               # slow enough that Stop arrives while the download is still running
            localllm.download_file(s.base + "/file.bin", paths.llm_dir() / "tiny2.gguf", size=item["size"], sha256=item["sha256"],
                                   progress=localllm.job_progress(job), check=check)

        job = localllm.start_job(jobs, "llm:tiny-test2", "slow", slow)
        self.assertTrue(wait_until(lambda: part.exists() and part.stat().st_size > 200_000, 20))
        self.assertTrue(jobs.cancel(job.id))
        self.assertTrue(wait_until(lambda: job.status == "cancelled", 20), job.to_dict())
        self.assertIn("resumes", job.message)
        self.assertEqual(localllm.catalog_state({**item, "file": "tiny2.gguf"})["state"], "partial")
        part.unlink(missing_ok=True)

    def test_jobs_that_fail_report_the_message(self):
        jobs = JobManager()

        def boom(job):
            raise localllm.DownloadError("The server refused the download.")

        job = localllm.start_job(jobs, "llm:x", "x", boom)
        self.assertTrue(wait_until(lambda: job.status == "error", 5))
        self.assertEqual(job.error, "The server refused the download.")

    def test_only_downloaded_models_can_be_deleted(self):
        keep = Path(self._home) / "elsewhere.gguf"
        keep.write_bytes(gguf_bytes())
        with self.assertRaises(ValueError):
            localllm.delete_model_file(keep)
        self.assertTrue(keep.exists())
        mine = paths.llm_dir() / "mine.gguf"
        mine.write_bytes(gguf_bytes())
        mine.with_name("mine.gguf.part").write_bytes(b"x")
        self.assertTrue(localllm.delete_model_file(mine))
        self.assertFalse(mine.exists() or mine.with_name("mine.gguf.part").exists())


class EngineDownload(TempHome):
    """The pinned engine zip is fetched, checked, unpacked, pruned and only then put in place (here from a local server)."""

    def setUp(self):
        self.work = Path(self._home) / "engine-dl"
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def make_zip(self, extra=None) -> bytes:
        path = self.work / "built.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(localllm.ENGINE_EXE, b"stub")
            z.writestr("llama-server-impl.dll", b"impl")
            z.writestr("ggml-cpu-haswell.dll", b"cpu")
            z.writestr("LICENSE-LLVM-OpenMP", "license text")
            z.writestr("llama-cli.exe", b"a tool the app does not need")
            z.writestr("llama-bench-impl.dll", b"nor this")
            for name, data in (extra or {}).items():
                z.writestr(name, data)
        return path.read_bytes()

    def pin_for(self, blob: bytes, server) -> dict:
        return {"tag": "bTEST", "version": "v0", "commit": "c" * 40, "asset": "engine.zip", "url": server.base + "/file.bin",
                "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob), "keep": localllm.EMBEDDED_PIN["keep"]}

    def test_download_verify_unpack_prune_and_install(self):
        blob = self.make_zip()
        s = RangeServer(blob)
        self.servers.append(s)
        pin = self.pin_for(blob, s)
        dest, cache = self.work / "llama", self.work / "cache"
        marker = localllm.download_engine(dest, cache, run_check=False, pin=pin)
        self.assertEqual(marker["tag"], "bTEST")
        names = sorted(p.name for p in dest.iterdir())
        self.assertEqual(names, sorted([localllm.ENGINE_EXE, "llama-server-impl.dll", "ggml-cpu-haswell.dll", "LICENSE-LLVM-OpenMP", localllm.ENGINE_MARKER]))
        self.assertEqual(localllm.read_marker(dest)["sha256"], pin["sha256"])
        self.assertTrue((cache / "engine.zip").is_file())
        n = len(s.requests)
        localllm.download_engine(dest, cache, run_check=False, pin=pin)           # again: the cached zip is checked, not fetched
        self.assertEqual(len(s.requests), n)
        (cache / "engine.zip").write_bytes(b"damaged" * 10)                          # a damaged cache is fetched again
        localllm.download_engine(dest, cache, run_check=False, pin=pin)
        self.assertGreater(len(s.requests), n)
        self.assertFalse((self.work / "llama.new").exists())

    def test_the_command_reads_the_pin_from_the_file_it_is_given_and_fails_loudly(self):
        """What installer\\build.ps1 runs: download-llama --pins installer\\pins.json --dest <stage cache> --cache <downloads>."""
        import contextlib
        import io

        from perplexify import cli

        blob = self.make_zip()
        s = RangeServer(blob)
        self.servers.append(s)
        pins = self.work / "pins.json"
        pins.write_text(json.dumps({"llama": self.pin_for(blob, s)}), "utf-8")
        dest, cache = self.work / "llama", self.work / "cache"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["download-llama", "--pins", str(pins), "--dest", str(dest), "--cache", str(cache), "--no-run-check"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue((dest / localllm.ENGINE_EXE).is_file())
        self.assertEqual(localllm.read_marker(dest)["tag"], "bTEST", "the pin came from the given file, not from the embedded copy")
        # a checksum that does not match stops it with exit code 1 and nothing is installed
        pins.write_text(json.dumps({"llama": {**self.pin_for(blob, s), "sha256": "2" * 64, "asset": "other.zip"}}), "utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["download-llama", "--pins", str(pins), "--dest", str(self.work / "llama2"), "--cache", str(cache), "--no-run-check"])
        self.assertEqual(code, 1)
        self.assertIn("checksum", err.getvalue())
        self.assertFalse((self.work / "llama2").exists())
        self.assertFalse((cache / "other.zip").exists(), "the file that failed the check is deleted")
        # a pins file that cannot be read or has no llama block is an error, never a silent fallback to the embedded pin
        for bad in (self.work / "missing.json", pins):
            if bad == pins:
                pins.write_text(json.dumps({"python": {}}), "utf-8")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = cli.main(["download-llama", "--pins", str(bad), "--dest", str(self.work / "llama3"), "--cache", str(cache)])
            self.assertEqual(code, 1, bad.name)
            self.assertIn("Error:", err.getvalue())

    def test_the_app_does_not_keep_the_zip_after_unpacking_it(self):
        blob = self.make_zip()
        s = RangeServer(blob)
        self.servers.append(s)
        dest, cache = self.work / "llama", self.work / "cache"
        localllm.download_engine(dest, cache, run_check=False, pin=self.pin_for(blob, s), keep_archive=False)
        self.assertTrue((dest / localllm.ENGINE_EXE).is_file())
        self.assertFalse((cache / "engine.zip").exists(), "the Download engine button does not leave 38 MB behind")

    def test_a_zip_with_the_wrong_checksum_is_never_installed(self):
        blob = self.make_zip()
        s = RangeServer(blob)
        self.servers.append(s)
        pin = {**self.pin_for(blob, s), "sha256": "1" * 64}
        dest = self.work / "llama"
        dest.mkdir()
        (dest / "keep.txt").write_text("the old engine")
        with self.assertRaises(localllm.DownloadError):
            localllm.download_engine(dest, self.work / "cache", run_check=False, pin=pin)
        self.assertEqual((dest / "keep.txt").read_text(), "the old engine", "a failed download leaves what was there")

    def test_an_archive_with_an_unsafe_path_is_refused(self):
        blob = self.make_zip({"../evil.txt": b"x"})
        s = RangeServer(blob)
        self.servers.append(s)
        with self.assertRaises(localllm.DownloadError) as cm:
            localllm.download_engine(self.work / "llama", self.work / "cache", run_check=False, pin=self.pin_for(blob, s))
        self.assertIn("unsafe path", str(cm.exception))
        self.assertFalse((self.work / "evil.txt").exists())

    def test_an_archive_without_the_server_is_refused(self):
        path = self.work / "nothing.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("readme.txt", "no server in here")
        blob = path.read_bytes()
        s = RangeServer(blob)
        self.servers.append(s)
        with self.assertRaises(localllm.DownloadError) as cm:
            localllm.download_engine(self.work / "llama", self.work / "cache", run_check=False, pin=self.pin_for(blob, s))
        self.assertIn(localllm.ENGINE_EXE, str(cm.exception))

    def test_the_engine_must_run_before_it_replaces_the_old_one(self):
        blob = self.make_zip()
        s = RangeServer(blob)
        self.servers.append(s)
        dest = self.work / "llama"
        with mock.patch.object(localllm, "engine_version", lambda e, timeout=25.0: None):
            with self.assertRaises(localllm.DownloadError) as cm:
                localllm.download_engine(dest, self.work / "cache", run_check=True, pin=self.pin_for(blob, s))
        self.assertIn("does not start", str(cm.exception))
        self.assertFalse(dest.exists())
        with mock.patch.object(localllm, "engine_version", lambda e, timeout=25.0: "b1 (abc)"):
            marker = localllm.download_engine(dest, self.work / "cache", run_check=True, pin=self.pin_for(blob, s))
        self.assertEqual(marker["run_version"], "b1 (abc)")

    @unittest.skipUnless(WINDOWS, "reads Windows executable files")
    def test_pruning_keeps_everything_the_server_needs(self):
        d = self.work / "pe"
        d.mkdir()
        make_pe(d / localllm.ENGINE_EXE, ["llama-server-impl.dll", "kernel32.dll"])
        make_pe(d / "llama-server-impl.dll", ["ggml-base.dll", "needed-by-impl.dll", "msvcp140.dll"])
        make_pe(d / "needed-by-impl.dll", [])
        make_pe(d / "ggml-base.dll", ["libomp.dll"])
        (d / "libomp.dll").write_bytes(b"x")
        (d / "ggml-cpu-zen4.dll").write_bytes(b"x")
        (d / "LICENSE-LLVM-OpenMP").write_text("x")
        make_pe(d / "llama-cli.exe", ["llama-cli-impl.dll"])
        make_pe(d / "llama-cli-impl.dll", ["ggml-base.dll"])
        (d / "llama-connect.exe").write_bytes(b"x")
        keep = [k for k in localllm.EMBEDDED_PIN["keep"] if k != "libomp.dll"]       # not listed: kept only because it is imported
        removed = localllm.prune_engine(d, keep)
        self.assertEqual(sorted(removed), ["llama-cli-impl.dll", "llama-cli.exe", "llama-connect.exe"])
        self.assertTrue((d / "needed-by-impl.dll").exists(), "an import the patterns do not list is kept")
        self.assertTrue((d / "libomp.dll").exists())
        self.assertEqual(localllm._pe_imports(d / "llama-server-impl.dll"), {"ggml-base.dll", "needed-by-impl.dll", "msvcp140.dll"})
        self.assertEqual(localllm._pe_imports(d / "ggml-cpu-zen4.dll"), set(), "a file that is not an executable has no imports")


# ------------------------------------------------------------------------------------------------------ the provider
class LocalProviderTests(FakeEngineCase):
    def provider(self, **kw) -> LocalProvider:
        p = make_provider(self.connect(**kw), "")
        self.assertIsInstance(p, LocalProvider)
        return p

    def test_the_provider_is_in_the_catalog_after_the_cloud_ones(self):
        ids = [p["id"] for p in providers.CHAT_PROVIDERS]
        self.assertEqual(ids[:4], ["openai", "anthropic", "gemini", "deepseek"])
        self.assertEqual(ids[-2:], ["local", "custom"], "Other stays last")
        local = providers.get_chat_provider("local")
        self.assertEqual((local["name"], local["company"], local["kind"]), ("Local model", "llama.cpp, on this computer", "openai"))
        self.assertTrue(providers.is_local("local") and providers.is_local({"provider": "local"}))
        self.assertFalse(providers.is_local({"provider": "openai"}) or providers.is_local(None))
        self.assertIsNone(providers.env_key("local"))
        self.assertTrue(next(p for p in providers.public_catalog() if p["id"] == "local")["local"])

    def test_streaming_text_usage_thinking_and_the_request(self):
        os.environ["FAKE_LLAMA_REASONING"] = "1"
        p = self.provider()
        deltas = list(p.stream([{"role": "user", "content": "hi"}], "be brief"))
        self.assertEqual([d.kind for d in deltas][:2], ["thinking", "text"])
        self.assertEqual("".join(d.text for d in deltas if d.kind == "text"), "w0 w1 w2 w3 w4 w5 ")
        self.assertEqual(next(d for d in deltas if d.kind == "thinking").text, "Let me think about the excerpts.")
        usage = next(d for d in deltas if d.kind == "usage").data
        self.assertEqual(usage["completion_tokens"], 6)
        self.assertGreater(usage["prompt_tokens"], 0)
        req = self.events("chat")[0]
        h = self.mgr.current()
        self.assertEqual(req["auth"], f"Bearer {h.api_key}", "the key of this run is sent as a bearer token")
        self.assertEqual(req["body"]["messages"][0], {"role": "system", "content": "be brief"})
        self.assertTrue(req["body"]["stream"])
        self.assertEqual(req["body"]["model"], "test-model.gguf")
        self.assertEqual(req["body"]["temperature"], 0.3, "a documentation assistant sticks to the excerpts")
        for cloud_only in ("reasoning_effort", "thinking", "max_tokens", "max_completion_tokens"):
            self.assertNotIn(cloud_only, req["body"])

    def test_cloud_options_are_ignored_for_a_local_profile(self):
        s = Settings()
        s.connect_local("test-model.gguf", "", None, "auto")
        s.update({"profiles": [{"id": "local", "reasoning": "high", "effort": "max", "use_env_key": True, "temperature": 0.9, "max_tokens": 50}]})
        prof = s.profile("local")
        self.assertIsNone(prof["reasoning"])
        self.assertFalse(prof["use_env_key"])
        self.assertNotIn("effort", prof)
        p = make_provider(prof, "")
        list(p.stream([{"role": "user", "content": "q"}]))
        body = self.events("chat")[0]["body"]
        self.assertEqual((body["temperature"], body["max_tokens"]), (0.9, 50), "the user's own sampling settings still apply")
        self.assertNotIn("reasoning_effort", body)

    def test_helper_calls_do_not_start_a_thinking_phase(self):
        p = self.provider()
        self.assertEqual(p.complete([{"role": "user", "content": "x"}], "s"), "w0 w1 w2 w3 w4 w5")
        body = self.events("chat")[0]["body"]
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["max_tokens"], LocalProvider.QUICK_MAX_TOKENS, "a small model that never stops must not stall the search for 2000 tokens")
        self.assertLessEqual(LocalProvider.QUICK_MAX_TOKENS, 256)
        self.assertEqual(OpenAIProviderForTest.QUICK_MAX_TOKENS, None, "cloud providers keep their own caps")

    def test_the_window_is_the_engines_when_it_runs_and_the_setting_before(self):
        p = self.provider(context_tokens=4096)
        before = p.info()
        self.assertEqual((before["context_window"], before["context_source"], before["engine"], before["max_context"]), (4096, "setting", "stopped", 32768))
        list(p.stream([{"role": "user", "content": "q"}]))
        after = p.info()
        self.assertEqual((after["context_window"], after["engine"]), (4096, "running"))
        auto = self.provider(context_tokens=None)
        self.assertEqual(auto.info()["context_source"], "automatic")
        self.assertGreaterEqual(auto.info()["context_window"], 2048)
        self.assertTrue(auto.info()["local"])

    def test_a_model_without_a_file_says_so(self):
        s = Settings()
        s.connect_local("gone.gguf", "", None, "auto")
        p = make_provider(s.profile("local"), "")
        with self.assertRaises(LLMError) as cm:
            list(p.stream([{"role": "user", "content": "q"}]))
        self.assertIn("gone.gguf", str(cm.exception))
        self.assertIn("error", p.info())
        s.connect_local("", "", None, "auto")
        with self.assertRaises(LLMError) as cm:
            make_provider(s.profile("local"), "").begin()
        self.assertIn("No local model is chosen", str(cm.exception))

    def test_a_prompt_that_does_not_fit_gets_a_helpful_message(self):
        os.environ["FAKE_LLAMA_CHARS_PER_TOKEN"] = "1"
        p = self.provider(context_tokens=4096)
        with self.assertRaises(LLMError) as cm:
            list(p.stream([{"role": "user", "content": "x" * 6000}]))
        self.assertIn("do not fit the model's window (4,096 tokens)", str(cm.exception))
        self.assertIn("Passages per question", str(cm.exception))

    def test_an_engine_that_died_is_explained_and_comes_back_on_the_next_question(self):
        p = self.provider()
        list(p.stream([{"role": "user", "content": "q"}]))
        pid = self.mgr.current().pid
        os.kill(pid, signal.SIGTERM)
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(pid)))
        self.mgr._handle.port = 9          # what a request that races the crash sees: nothing listens there
        with self.assertRaises(LLMError) as cm:
            p._handle = self.mgr._handle
            list(super(LocalProvider, p).stream([{"role": "user", "content": "q"}]))
        self.assertIn("stopped responding", str(cm.exception))
        self.assertEqual("".join(d.text for d in p.stream([{"role": "user", "content": "q"}]) if d.kind == "text"), "w0 w1 w2 w3 w4 w5 ")
        self.assertNotEqual(self.mgr.current().pid, pid)

    def test_model_files_are_the_models(self):
        self.make_model("second.gguf", arch="llama", ctx=4096)
        (paths.llm_dir() / "junk.gguf").write_text("not a model")
        p = self.provider()
        listed = {m["id"]: m for m in p.list_models()}
        self.assertEqual(set(listed), {"test-model.gguf", "second.gguf"}, "files that are not GGUF are not offered")
        self.assertEqual(listed["second.gguf"]["context_window"], 4096)
        self.assertIn("2 local model files", p.ping())

    def test_tokens_are_counted_by_the_engine_when_it_runs(self):
        p = self.provider()
        self.assertIsNone(p.count_tokens("hello"), "nothing runs yet")
        p.engine()
        self.assertEqual(p.count_tokens("x" * 400), 100)


# ------------------------------------------------------------------------------------------------------ settings and the web API
class SettingsAndApi(FakeEngineCase):
    def setUp(self):
        super().setUp()
        from perplexify.context import AppContext
        from perplexify.server import create_app

        self.ctx = AppContext()
        self.app = create_app(self.ctx)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")

    def wait_state(self, state, timeout=20):
        end = time.time() + timeout
        st = {}
        while time.time() < end:
            st = self.client.get("/api/local/status").json()["running"]
            if st["state"] == state:
                return st
            time.sleep(0.1)
        self.fail(f"never reached {state}: {st}")

    def test_a_local_model_needs_no_key_anywhere(self):
        s = self.ctx.settings
        self.assertFalse(s.is_ready())
        prof = s.connect_local("test-model.gguf", "", None, "auto")
        self.assertTrue(s.is_ready())
        self.assertTrue(s.can_chat(prof))
        self.assertEqual(s.api_key(prof), "")
        pub = s.public()
        local = next(p for p in pub["profiles"] if p["id"] == "local")
        self.assertTrue(local["has_key"], "the interface reads has_key as connected")
        self.assertNotIn("api_key_enc", local)
        self.assertEqual((local["name"], local["base_url"], local["gpu"]), ("Local model", "", "auto"))
        self.assertEqual(s.profile()["id"], "local")
        self.assertTrue(self.client.get("/api/health").json()["ready"])
        boot = self.client.get("/api/bootstrap").json()
        self.assertTrue(boot["ready"])
        self.assertIn("local", [p["id"] for p in boot["providers"]])
        self.assertEqual(boot["settings"]["active_profile"], "local")
        self.assertEqual(self.client.get("/api/llm/info").json()["context_window"] > 0, True)

    def test_connecting_a_model_for_a_cloud_provider_is_unchanged(self):
        s = Settings()
        s.connect("openai", "sk-proj-x" * 4, "gpt-x")
        s.connect_local("test-model.gguf", "", None, "off")
        profs = {p["id"]: p for p in s.get()["profiles"]}
        self.assertEqual(set(profs), {"openai", "local"})
        self.assertNotIn("gpu", profs["openai"])
        self.assertEqual(profs["local"]["gpu"], "off")
        self.assertEqual(s.get()["active_profile"], "local")

    def test_chosen_files_are_remembered_and_never_copied(self):
        s = Settings()
        s.link_model(r"C:\models\a.gguf")
        s.link_model(r"C:\models\a.gguf")
        s.link_model(r"C:\models\b.gguf")
        self.assertEqual(Settings().linked_models(), [r"C:\models\a.gguf", r"C:\models\b.gguf"])
        s.unlink_model(r"C:\models\a.gguf")
        self.assertEqual(Settings().linked_models(), [r"C:\models\b.gguf"])
        self.assertEqual(Settings().public()["local"]["linked"], [r"C:\models\b.gguf"])

    def test_link_connect_and_use_a_file_where_it_is(self):
        outside = Path(self._home) / "elsewhere" / "My Model.gguf"
        outside.parent.mkdir(exist_ok=True)
        outside.write_bytes(gguf_bytes("llama", 4096))
        r = self.client.post("/api/local/models/link", json={"path": str(outside)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model"]["linked"], True)
        self.assertFalse((paths.llm_dir() / "My Model.gguf").exists(), "a chosen file is never copied")
        listed = self.client.get("/api/local/models").json()
        self.assertIn("My Model.gguf", [m["id"] for m in listed["installed"]])
        r = self.client.post("/api/local/connect", json={"model": "My Model.gguf", "context_tokens": 4096, "gpu": "off"})
        self.assertEqual(r.status_code, 200, r.text)
        prof = r.json()["profile"]
        self.assertEqual((prof["model"], prof["model_path"], prof["gpu"], prof["context_tokens"]), ("My Model.gguf", str(outside), "off", 4096))
        self.assertEqual(r.json()["info"]["context_window"], 4096)
        self.assertIn("built-in embedding", r.json()["embeddings_note"])
        self.assertEqual(self.client.get("/api/settings").json()["active_profile"], "local")
        # asking uses the file where it lives
        self.client.post("/api/local/start")
        st = self.wait_state("ready")
        self.assertEqual(st["model_path"], str(outside))
        args = self.events("start")[0]["args"]
        self.assertEqual(args[args.index("-m") + 1], str(outside))
        self.assertIn("--device", args)

    def test_link_refuses_bad_files_and_name_clashes(self):
        self.assertEqual(self.client.post("/api/local/models/link", json={"path": str(Path(self._home) / "nope.gguf")}).status_code, 400)
        text = Path(self._home) / "notes.gguf"
        text.write_text("hello")
        r = self.client.post("/api/local/models/link", json={"path": str(text)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a GGUF", r.json()["detail"])
        other = Path(self._home) / "clash" / "test-model.gguf"
        other.parent.mkdir(exist_ok=True)
        other.write_bytes(gguf_bytes())
        r = self.client.post("/api/local/models/link", json={"path": str(other)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already in the list", r.json()["detail"])

    def test_the_file_dialog_is_used_when_no_path_is_given(self):
        outside = Path(self._home) / "dialog.gguf"
        outside.write_bytes(gguf_bytes())
        with mock.patch.object(localllm, "pick_model_file", lambda *a, **k: str(outside)):
            self.assertEqual(self.client.post("/api/local/models/link", json={}).json()["model"]["file"], "dialog.gguf")
        with mock.patch.object(localllm, "pick_model_file", lambda *a, **k: None):
            self.assertEqual(self.client.post("/api/local/models/link", json={}).json(), {"cancelled": True})

    def test_connect_refuses_what_is_not_there(self):
        self.assertEqual(self.client.post("/api/local/connect", json={"model": "nothing.gguf"}).status_code, 404)
        (paths.llm_dir() / "bad.gguf").write_text("junk")
        r = self.client.post("/api/local/connect", json={"model": "bad.gguf"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Settings().profile(), None)

    def test_status_models_and_the_curated_list(self):
        st = self.client.get("/api/local/status").json()
        self.assertTrue(st["engine"]["found"])
        self.assertEqual(st["engine"]["version"], "b12359 (b1b770d)")
        self.assertEqual(st["running"]["state"], "stopped")
        self.assertEqual(set(st["hardware"]), {"ram_total", "ram_available", "cpu_threads", "gpu"})
        self.assertEqual(st["context_choices"], [4096, 8192, 16384, 32768])
        self.assertIsNone(st["profile"])
        m = self.client.get("/api/local/models").json()
        self.assertEqual(m["dir"], str(paths.llm_dir()))
        self.assertEqual([c["id"] for c in m["catalog"]], [c["id"] for c in localllm.CATALOG])
        first = m["catalog"][0]
        self.assertEqual((first["state"], first["size"], first["fit"]["level"] in ("ok", "tight", "too_big")), ("available", localllm.CATALOG[0]["size"], True))
        self.assertTrue(all(k in first for k in ("name", "params", "quant", "license", "summary", "ram_gb", "file", "tags")))
        self.connect()
        prof = self.client.get("/api/local/status").json()["profile"]
        self.assertEqual((prof["model"], prof["active"], prof["model_found"], prof["resolved_context"]), ("test-model.gguf", True, True, 4096))
        self.assertEqual(prof["context_length"], 32768)

    def test_a_missing_engine_is_reported_with_the_way_to_get_it(self):
        del os.environ["PERPLEXIFY_LLAMA_SERVER"]
        st = self.client.get("/api/local/status").json()
        self.assertFalse(st["engine"]["found"])
        self.assertIn("download-llama", st["engine"]["message"])
        self.assertTrue(st["engine"]["can_download"])
        self.assertIsNone(st["engine_job"])

    def test_start_stop_and_test_the_model(self):
        self.connect()
        r = self.client.post("/api/local/start")
        self.assertEqual(r.status_code, 200, r.text)
        st = self.wait_state("ready")
        self.assertEqual((st["model"], st["n_ctx"]), ("test-model.gguf", 4096))
        r = self.client.post("/api/local/test")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["ok"], r.json()["reply"], r.json()["n_ctx"]), (True, "w0 w1 w2 w3 w4 w5", 4096))
        self.assertEqual(len(self.events("start")), 1, "the test reuses the running engine")
        self.assertEqual(self.client.post("/api/local/stop").json()["state"], "stopped")
        self.assertEqual(self.client.post("/api/local/test").status_code, 200, "and starts it when it is not running")

    def test_a_failed_start_is_reported(self):
        self.connect()
        os.environ["FAKE_LLAMA_FAIL"] = "oom"
        self.client.post("/api/local/start")
        st = self.wait_state("error")
        self.assertIn("not enough free memory", st["error"].lower())
        r = self.client.post("/api/local/test")
        self.assertEqual(r.status_code, 400)
        self.assertIn("memory", r.json()["detail"].lower())

    def test_changing_the_model_settings_unloads_the_engine_that_no_longer_matches(self):
        self.connect(context_tokens=4096)
        self.client.post("/api/local/start")
        h = self.wait_state("ready")
        r = self.client.post("/api/local/connect", json={"model": "test-model.gguf", "context_tokens": 8192})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/local/status").json()["running"]["state"], "stopped", "its memory is not held for nothing")
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(h["pid"])))
        r = self.client.post("/api/local/connect", json={"model": "test-model.gguf", "context_tokens": 8192})   # same settings: nothing to unload
        self.assertEqual(r.status_code, 200)

    def test_delete_removes_a_download_but_only_forgets_a_chosen_file(self):
        outside = Path(self._home) / "mine" / "chosen.gguf"
        outside.parent.mkdir(exist_ok=True)
        outside.write_bytes(gguf_bytes())
        self.client.post("/api/local/models/link", json={"path": str(outside)})
        r = self.client.post("/api/local/models/delete", json={"id": "chosen.gguf"}).json()
        self.assertEqual((r["deleted"], r["unlinked"]), (False, True))
        self.assertTrue(outside.exists(), "the user's own file is never deleted")
        self.assertNotIn("chosen.gguf", [m["id"] for m in self.client.get("/api/local/models").json()["installed"]])
        self.connect()
        self.client.post("/api/local/start")
        h = self.wait_state("ready")
        r = self.client.post("/api/local/models/delete", json={"id": "test-model.gguf"}).json()
        self.assertEqual((r["deleted"], r["unlinked"]), (True, False))
        self.assertFalse(self.model.exists())
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(h["pid"])), "a model in use is unloaded first")
        self.assertEqual(self.client.post("/api/local/models/delete", json={"id": "test-model.gguf"}).status_code, 404)
        prof = self.client.get("/api/local/status").json()["profile"]
        self.assertFalse(prof["model_found"])
        self.assertEqual(prof["model"], "", "the profile no longer names the model that was deleted")
        evs = list(self.ctx.engine.answer_stream("q", [], ["lib"]))
        self.assertEqual(evs[-1]["code"], "model_missing", "asking says what to do instead of failing in a confusing way")
        self.assertIn("No local model is chosen", evs[-1]["message"])
        # forgetting a chosen file that the profile uses clears it the same way, but another model's profile is left alone
        self.connect()
        self.client.post("/api/local/models/link", json={"path": str(outside)})
        self.ctx.settings.connect_local("chosen.gguf", str(outside), None, "auto")
        other = self.make_model("other-model.gguf")
        self.client.post("/api/local/models/delete", json={"id": "other-model.gguf"})
        self.assertEqual(self.ctx.settings.profile("local")["model"], "chosen.gguf", "deleting another model leaves the profile alone")
        self.client.post("/api/local/models/delete", json={"id": "chosen.gguf"})
        self.assertEqual(self.ctx.settings.profile("local")["model"], "")
        self.assertTrue(outside.exists())

    def test_download_endpoint_starts_a_job_and_refuses_unknown_and_installed(self):
        self.assertEqual(self.client.post("/api/local/models/download", json={"id": "nope"}).status_code, 404)
        c = localllm.CATALOG[0]
        (paths.llm_dir() / c["file"]).write_bytes(b"x" * c["size"] if c["size"] < 10_000 else b"")
        with mock.patch.object(localllm, "catalog_state", lambda item: {"state": "installed", "bytes": item["size"]}):
            self.assertEqual(self.client.post("/api/local/models/download", json={"id": c["id"]}).status_code, 409)
        (paths.llm_dir() / c["file"]).unlink(missing_ok=True)
        started = {}

        def fake_start(jobs, item_id):
            started["id"] = item_id
            return jobs.submit("llm:" + item_id, "x", None, lambda j: None)

        with mock.patch.object(localllm, "start_model_download", fake_start):
            r = self.client.post("/api/local/models/download", json={"id": c["id"]})
        self.assertEqual((r.status_code, started["id"]), (200, c["id"]))
        self.assertEqual(r.json()["job"]["kind"], "llm:" + c["id"])

    def test_the_engine_download_endpoint_runs_a_job(self):
        calls = []

        def fake_download(dest, cache, progress=None, check=None, **kw):
            calls.append((Path(dest), Path(cache)))
            progress("download", 5, 10, "1 MB/s")
            return {"tag": "bTEST", "files": []}

        with mock.patch.object(localllm, "download_engine", fake_download):
            r = self.client.post("/api/local/engine/download")
            self.assertEqual(r.status_code, 200)
            jid = r.json()["job"]["id"]
            self.assertTrue(wait_until(lambda: self.client.get(f"/api/jobs/{jid}").json()["status"] == "done", 10))
        self.assertEqual(calls[0][0], paths.llama_dir())
        self.assertEqual(self.client.get(f"/api/jobs/{jid}").json()["message"], "The engine is ready.")

    def test_the_engine_goes_down_with_the_web_server(self):
        self.connect()
        with TestClient(self.app, base_url="http://127.0.0.1") as c:       # startup ... shutdown, like uvicorn does
            c.post("/api/local/start")
            h = self.wait_state("ready")
            self.assertTrue(osutil.pid_alive(h["pid"]))
        self.assertTrue(wait_until(lambda: not osutil.pid_alive(h["pid"])), "the shutdown hook stops llama-server")
        self.assertEqual(self.mgr.status()["state"], "stopped")

    def test_api_calls_from_other_sites_are_refused_like_everywhere(self):
        self.assertEqual(self.client.post("/api/local/start", headers={"origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/local/status", headers={"host": "evil.example.com"}).status_code, 403)


# ------------------------------------------------------------------------------------------------------ the question flow
class QuestionFlow(FakeEngineCase):
    def setUp(self):
        super().setUp()
        from perplexify.context import AppContext

        self.ctx = AppContext()
        self.ctx.settings.update({"retrieval": {"rerank": False, "multi_query": False, "query_rewrite": False}})
        self.hits = self.make_hits(30)
        self.ctx.retriever.search = self.fake_search
        self.search_calls = []

    @staticmethod
    def make_hits(n, chars=2000):
        return [Hit("lib", "Manual", i, i, f"page{i}.html", f"https://example.org/p{i}", f"Page {i} > Section", None,
                    ("Passage %d. " % i) + "lorem ipsum dolor sit amet " * (chars // 27), "mirror", None, "html", score=1.0 - i / 100)
                for i in range(n)]

    def fake_search(self, queries, library_ids, **kw):
        self.search_calls.append((queries, kw))
        return list(self.hits), {"queries": queries, "timing_ms": {"total": 1}, "pending": 0, "vector_hits": 1, "keyword_hits": 1}

    def ask(self, question="How does it work?", history=None, **connect):
        self.connect(**connect)
        return list(self.ctx.engine.answer_stream(question, history or [], ["lib"]))

    def test_the_first_question_loads_the_model_and_says_so_the_second_does_not(self):
        os.environ["FAKE_LLAMA_LOAD_DELAY"] = "1.3"
        evs = self.ask()
        kinds = [e["type"] for e in evs]
        statuses = [e["text"] for e in evs if e["type"] == "status"]
        self.assertEqual(statuses[0], "searching")
        self.assertIn("loading_model", statuses)
        self.assertEqual(statuses[-2:], ["model_ready", "generating"])
        loading = [e for e in evs if e.get("text") == "loading_model"]
        self.assertEqual((loading[0]["model"], loading[0]["seconds"]), ("test-model.gguf", 0))
        self.assertGreaterEqual(loading[-1]["seconds"], 1, "a heartbeat every second while it loads")
        ready = next(e for e in evs if e.get("text") == "model_ready")
        self.assertEqual(ready["n_ctx"], 4096)
        for needed in ("sources", "context", "token", "usage", "done"):
            self.assertIn(needed, kinds)
        self.assertLess(kinds.index("model_ready" if False else "sources"), kinds.index("context"))
        self.assertLess(kinds.index("context"), kinds.index("token"))
        self.assertEqual("".join(e["text"] for e in evs if e["type"] == "token"), "w0 w1 w2 w3 w4 w5 ")
        usage = next(e for e in evs if e["type"] == "usage")
        self.assertEqual(usage["completion_tokens"], 6)
        again = list(self.ctx.engine.answer_stream("And then?", [], ["lib"]))
        self.assertNotIn("loading_model", [e.get("text") for e in again])
        self.assertEqual(len(self.events("start")), 1, "the engine started once and was reused")

    def test_with_extra_search_calls_the_model_is_awaited_before_they_run(self):
        self.ctx.settings.update({"retrieval": {"multi_query": True}})
        os.environ["FAKE_LLAMA_LOAD_DELAY"] = "0.8"
        evs = self.ask()
        texts = [e.get("text") for e in evs if e["type"] == "status"]
        self.assertEqual(texts[0], "searching")
        self.assertLess(texts.index("loading_model"), texts.index("model_ready"))
        self.assertTrue(any(e["type"] == "expand" for e in evs), "the extra phrasings were asked of the loaded model")
        self.assertEqual(self.events("chat")[0]["body"]["chat_template_kwargs"], {"enable_thinking": False})

    def test_the_context_window_reported_is_the_one_the_engine_has(self):
        evs = self.ask(context_tokens=8192)
        ctx_ev = next(e for e in evs if e["type"] == "context")
        self.assertEqual(ctx_ev["window"], 8192)
        self.assertEqual(self.mgr.current().n_ctx, 8192)
        self.assertEqual(ctx_ev["max_context"], 32768)
        self.assertEqual((ctx_ev["provider"], ctx_ev["model"]), ("local", "test-model.gguf"))

    def test_a_small_window_hands_the_model_fewer_passages_and_the_prompt_fits(self):
        counts = {}
        for window in (4096, 8192, 16384):
            self.mgr.stop()
            evs = self.ask(context_tokens=window)
            sources = next(e for e in evs if e["type"] == "sources")["sources"]
            ctx_ev = next(e for e in evs if e["type"] == "context")
            counts[window] = len(sources)
            room = window - max(384, int(window * 0.15))
            self.assertLessEqual(ctx_ev["estimated_prompt"], room, f"window {window}: the prompt must leave room for the answer")
            self.assertEqual(self.events("chat")[-1]["prompt_tokens"] <= window, True)
        self.assertLess(counts[4096], counts[8192])
        self.assertLess(counts[8192], counts[16384])
        self.assertLess(counts[16384], 30, "30 passages are asked for, the window decides how many fit")
        self.assertGreaterEqual(counts[4096], 1)

    def test_passages_per_question_is_the_search_setting_for_local_models_too(self):
        self.ask(context_tokens=16384)
        self.assertEqual(self.search_calls[0][1]["top_k"], 30)

    def test_a_tokenizer_that_makes_more_tokens_than_expected_is_caught(self):
        os.environ["FAKE_LLAMA_CHARS_PER_TOKEN"] = "1.5"       # the estimate assumes 3.6: this model needs more than twice as many
        evs = self.ask(context_tokens=4096)
        ctx_ev = next(e for e in evs if e["type"] == "context")
        self.assertLessEqual(ctx_ev["estimated_prompt"], 4096 - 614)
        self.assertEqual([e["type"] for e in evs if e["type"] == "error"], [], "no 'exceeds the available context size' from the engine")
        self.assertGreaterEqual(len(self.events("tokenize")), 2, "it was counted, shrunk and counted again")
        self.assertLessEqual(self.events("chat")[-1]["prompt_tokens"], 4096)

    def test_when_the_engine_cannot_be_started_the_error_says_why_and_what_to_offer(self):
        os.environ["FAKE_LLAMA_FAIL"] = "arch"
        evs = self.ask()
        err = [e for e in evs if e["type"] == "error"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["code"], "engine_failed")
        self.assertIn("zorblax", err[0]["message"])
        self.assertNotIn("done", [e["type"] for e in evs])
        del os.environ["FAKE_LLAMA_FAIL"]
        del os.environ["PERPLEXIFY_LLAMA_SERVER"]
        self.mgr.stop()
        err = [e for e in self.ask() if e["type"] == "error"]
        self.assertEqual(err[0]["code"], "engine_missing")
        self.assertIn("download-llama", err[0]["message"])

    def test_a_missing_model_file_is_an_error_event_not_a_crash(self):
        s = self.ctx.settings
        s.connect_local("vanished.gguf", "", None, "auto")
        err = [e for e in self.ctx.engine.answer_stream("q", [], ["lib"]) if e["type"] == "error"]
        self.assertEqual(err[0]["code"], "model_missing")
        self.assertIn("vanished.gguf", err[0]["message"])

    def test_no_model_connected_is_still_told_apart_from_a_local_one(self):
        evs = list(self.ctx.engine.answer_stream("q", [], ["lib"]))
        self.assertEqual((evs[0]["type"], evs[0]["code"]), ("error", "no_model"))
        self.assertIn("ChatGPT, Claude, Gemini or DeepSeek", evs[0]["message"])
        self.assertIn("local model", evs[0]["message"])

    def test_stopping_the_answer_stops_the_generation(self):
        os.environ["FAKE_LLAMA_TOKENS"] = "400"
        os.environ["FAKE_LLAMA_TOKEN_DELAY"] = "0.03"
        self.connect()
        gen = self.ctx.engine.answer_stream("q", [], ["lib"])
        got = 0
        for ev in gen:
            if ev["type"] == "token":
                got += 1
                if got == 3:
                    break
        gen.close()                                  # what the server does when the browser's Stop closes the stream
        self.assertTrue(wait_until(lambda: self.events("aborted"), 8), "the engine saw the connection close")
        self.assertLess(self.events("aborted")[0]["sent"], 400)
        self.assertEqual(self.events("finished"), [])

    def test_through_the_chat_endpoint_with_history_events_in_front(self):
        from perplexify.server import create_app

        self.connect()
        client = TestClient(create_app(self.ctx), base_url="http://127.0.0.1")
        r = client.post("/api/chat", json={"question": "How do I start?", "libraries": ["lib"], "history": []})
        self.assertEqual(r.status_code, 200)
        evs = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        types = [e["type"] for e in evs]
        self.assertIn("token", types)
        self.assertIn("done", types)
        self.assertNotIn("error", types)
        self.assertEqual(next(e for e in evs if e["type"] == "context")["window"], 4096)


class StoppedAnswer(FakeEngineCase):
    """The browser stops reading an answer (Stop, or the window closed). Checked over real HTTP with uvicorn: the web framework alone
    leaves the generator behind the stream suspended until the garbage collector gets round to it, and the engine generated the
    whole answer meanwhile (found with the real llama-server)."""

    def setUp(self):
        super().setUp()
        import uvicorn
        from perplexify.context import AppContext
        from perplexify.server import create_app

        self.ctx = AppContext()
        self.ctx.settings.update({"retrieval": {"rerank": False, "multi_query": False, "query_rewrite": False}})
        hits = QuestionFlow.make_hits(5)
        self.ctx.retriever.search = lambda queries, library_ids, **kw: (
            list(hits), {"queries": queries, "timing_ms": {"total": 1}, "pending": 0, "vector_hits": 1, "keyword_hits": 1})
        self.server = uvicorn.Server(uvicorn.Config(create_app(self.ctx), host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        self.assertTrue(wait_until(lambda: self.server.started, 15))
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(15)

    def read_tokens(self, count, **body):
        """Ask a question and walk away after `count` tokens (the connection closes when the with-block ends)."""
        got = 0
        payload = {"question": "How does it work?", "libraries": ["lib"], "history": [], **body}
        with httpx.stream("POST", f"http://127.0.0.1:{self.port}/api/chat", json=payload, timeout=30, trust_env=False) as r:
            self.assertEqual(r.status_code, 200)
            for line in r.iter_lines():
                if line.startswith("data:") and json.loads(line[5:]).get("type") == "token":
                    got += 1
                    if got >= count:
                        return got
        return got

    def test_closing_the_stream_stops_the_engine_within_a_moment(self):
        os.environ["FAKE_LLAMA_TOKENS"] = "800"
        os.environ["FAKE_LLAMA_TOKEN_DELAY"] = "0.02"             # a 16 second answer
        self.connect()
        t0 = time.time()
        self.assertEqual(self.read_tokens(4), 4)
        self.assertTrue(wait_until(lambda: self.events("aborted"), 5), "the engine saw its connection close")
        self.assertLess(time.time() - t0, 8, "long before the 16 seconds the whole answer would have taken")
        self.assertLess(self.events("aborted")[0]["sent"], 800)
        self.assertEqual(self.events("finished"), [], "the answer was not generated to the end")

    def test_stopping_while_the_engine_is_still_reading_the_prompt_ends_the_request_at_once(self):
        os.environ["FAKE_LLAMA_PROMPT_DELAY"] = "6"               # nothing comes back for six seconds
        p = make_provider(self.connect(), "")
        p.engine()                                                 # loaded first: the wait below is only for the answer
        scope = localllm.RequestScope()
        outcome = {}

        def ask():
            localllm.current_scope.set(scope)
            try:
                outcome["text"] = list(p.stream([{"role": "user", "content": "q"}]))
            except LLMError as e:
                outcome["error"] = str(e)

        t0 = time.time()
        th = threading.Thread(target=ask, daemon=True)
        th.start()
        self.assertTrue(wait_until(lambda: self.events("chat"), 5), "the request reached the engine")
        scope.cancel()                                             # what the web server does when the browser leaves
        th.join(4)
        self.assertFalse(th.is_alive(), "the thread waiting for the engine was freed, not left waiting for the prompt")
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(outcome.get("error"), "The question was stopped.", "said plainly, not as a crash of the engine")
        self.assertIsNotNone(self.mgr.current(), "and the engine itself is fine")

    def test_the_engine_stays_loaded_for_the_next_question(self):
        os.environ["FAKE_LLAMA_TOKENS"] = "800"
        os.environ["FAKE_LLAMA_TOKEN_DELAY"] = "0.02"
        self.connect()
        self.read_tokens(3)
        self.assertTrue(wait_until(lambda: self.events("aborted"), 5))
        self.assertIsNotNone(self.mgr.current(), "stopping an answer does not unload the model")
        self.assertEqual(len(self.events("start")), 1)
        self.assertEqual(self.mgr.status()["state"], "ready")

    def test_a_question_that_finishes_normally_is_not_cancelled(self):
        self.connect()
        r = httpx.post(f"http://127.0.0.1:{self.port}/api/chat", json={"question": "q", "libraries": ["lib"], "history": []}, timeout=30, trust_env=False)
        evs = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        self.assertEqual("".join(e["text"] for e in evs if e["type"] == "token"), "w0 w1 w2 w3 w4 w5 ")
        self.assertEqual(self.events("aborted"), [])
        self.assertEqual(len(self.events("finished")), 1)


class PassagesPerQuestion(TempHome):
    """The user asked for 30 passages per question."""

    def test_the_default_is_30_and_the_settings_page_can_hold_it(self):
        self.assertEqual(settings_mod.RETRIEVAL_DEFAULTS["top_k"], 30)
        self.assertEqual(Settings().retrieval()["top_k"], 30)
        self.assertEqual(settings_mod._clean_retrieval({"top_k": 30})["top_k"], 30)
        self.assertEqual(settings_mod._clean_retrieval({"top_k": 500})["top_k"], 40, "the field in Settings allows 1 to 40")
        self.assertEqual(settings_mod._clean_retrieval({})["top_k"], 30)


if __name__ == "__main__":
    unittest.main()
