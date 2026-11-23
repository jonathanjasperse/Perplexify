"""Local models: the llama.cpp engine that ships with the app, the process that runs it, and the model files it loads.

A local model runs on this computer through `llama-server`, the server program of the llama.cpp project. Nothing is sent
anywhere. This module is everything about local models except the chat protocol (perplexify/llm/local.py) and the web API
(perplexify/local_api.py):

* the engine: where `llama-server` is (the installer puts it in <install folder>\\llama), which build is pinned and how it is
  fetched and checked when the app runs from the source tree (`python -m perplexify download-llama`);
* the process: started on demand on a free port that only this computer can reach, protected by a secret key, one at a time,
  reused while model and settings stay the same, stopped when the app quits and tied to this process with a Windows job object,
  so it cannot outlive the app even if the app is killed;
* the models: .gguf files the user downloaded (<data folder>/llm) or chose from elsewhere (used where they are, never copied), a
  short curated list with exact sizes and checksums, resumable verified downloads and hints about memory and graphics card.
"""
from __future__ import annotations

import atexit
import contextvars
import ctypes
import fnmatch
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from . import __version__, osutil, paths
from .jobs import Cancelled, Job, JobManager
from .llm.base import LLMError

log = logging.getLogger("perplexify.local")

IS_WINDOWS = os.name == "nt"
ENGINE_EXE = "llama-server.exe" if IS_WINDOWS else "llama-server"
ENGINE_MARKER = ".perplexify-engine.json"      # written next to a downloaded engine: which pinned build it is
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL_PRIORITY = 0x00004000             # inference uses every core: other programs (and this window) must stay responsive
LOG_LEVEL = "4"                                # llama-server's -lv: see build_args
GIB = 1024 ** 3

# ------------------------------------------------------------------------------------------------------ the pinned engine
# The llama.cpp build the app ships and downloads. installer/pins.json is the one source of truth: the build reads it from there
# (installer/build.ps1 calls `download-llama`), and so does the app while it runs from the source tree. The installed app has no
# installer folder, so this copy is what it falls back to; tests/test_local.py keeps the two equal.
# Why this build: v1.0.0 is the release llama.cpp itself calls stable (a tag with no files; its nightly-tag.txt names the build
# below, b12359). The Vulkan build contains every CPU variant (the CPU back end picks the best instruction set at run time) and
# in addition ggml-vulkan.dll for graphics cards, for 13 MB more download than the CPU-only zip. Its files are authentic: GitHub
# attests that the zip was published by ggml-org/llama.cpp's release workflow, and the binaries report build 12359, commit
# b1b770d90, the commit tag b12359 points to.
EMBEDDED_PIN: dict = {
    "version": "v1.0.0",
    "tag": "b12359",
    "commit": "b1b770d90d94cad9e9ea5b993bdbc383a522fb28",
    "asset": "llama-b12359-bin-win-vulkan-x64.zip",
    "url": "https://github.com/ggml-org/llama.cpp/releases/download/b12359/llama-b12359-bin-win-vulkan-x64.zip",
    "sha256": "e8d18c6e4210cca87e7307c2ea78363ac8d158e4e1397358ec96ab70b53a0d5e",
    "size": 38090643,
    "license": "MIT",
    # Only the server is needed: drop the other command line tools (and the libraries only they use). Whatever
    # llama-server.exe itself imports is always kept (see prune_engine), so a future build cannot lose a needed file here.
    "keep": ["llama-server.exe", "llama-server-impl.dll", "llama-common.dll", "llama.dll", "mtmd.dll", "ggml*.dll",
             "libomp.dll", "LICENSE*"],
}


def engine_pin(pins_file: Path | str | None = None) -> dict:
    """The pinned llama.cpp build: the `llama` block of `pins_file` when one is given (the installer build passes its own
    installer/pins.json, so that file alone decides what is shipped), else installer/pins.json when it exists (running from the source
    tree), else the copy above. A pins file that is named but unusable is an error, not a reason to fall back silently."""
    if pins_file is not None:
        try:
            pin = json.loads(Path(pins_file).read_text("utf-8")).get("llama")
        except (OSError, ValueError, AttributeError) as e:
            raise DownloadError(f"Cannot read the pinned engine from {pins_file}: {e}") from e
        if not (isinstance(pin, dict) and all(pin.get(k) for k in ("tag", "url", "sha256", "size", "asset"))):
            raise DownloadError(f"{pins_file} has no complete 'llama' block (tag, url, sha256, size and asset are needed).")
        return pin
    try:
        data = json.loads((paths.package_dir().parent / "installer" / "pins.json").read_text("utf-8"))
        pin = data.get("llama")
        if isinstance(pin, dict) and all(pin.get(k) for k in ("tag", "url", "sha256", "size", "asset")):
            return pin
    except (OSError, ValueError, AttributeError):
        pass
    return dict(EMBEDDED_PIN)


class LocalLLMError(LLMError):
    """A problem with local models whose message is safe to show. `code` tells the interface what to offer:
    engine_missing, model_missing, engine_failed, engine_died, engine_busy."""

    def __init__(self, message: str, code: str = "engine_failed", detail: str = ""):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ------------------------------------------------------------------------------------------------------ finding the engine
@dataclass(frozen=True)
class Engine:
    exe: Path
    dir: Path
    source: str          # "override" | "environment" | "installed" | "data"

    @property
    def is_script(self) -> bool:
        return self.exe.suffix.lower() == ".py"      # tests run a fake server written in Python through PERPLEXIFY_LLAMA_SERVER

    def command(self, *args: str) -> list[str]:
        # a script is run by the base interpreter: a virtual environment's python.exe is only a launcher that starts the real one
        python = getattr(sys, "_base_executable", None) or sys.executable
        return ([python, "-u", str(self.exe)] if self.is_script else [str(self.exe)]) + list(args)


def installed_layout() -> bool:
    """True when this is the installed app: <install folder>\\app\\perplexify next to <install folder>\\runtime."""
    if paths.bundle_resources() is not None:         # macOS: running from Perplexify.app
        return True
    app = paths.package_dir().parent
    return app.name.lower() == "app" and (app.parent / "runtime").is_dir()


def installed_engine_dir() -> Path:
    """The folder `llama` the installer ships: next to the runtime on Windows, inside Contents/Resources in the macOS app."""
    res = paths.bundle_resources()
    return res / "llama" if res is not None else paths.package_dir().parent.parent / "llama"


def engine_search_dirs() -> list[tuple[str, Path]]:
    """Where llama-server is looked for, in this order: PERPLEXIFY_LLAMA_DIR, the folder `llama` next to the app (the installed
    layout), then <data folder>\\llama (where `download-llama` puts it when running from the source)."""
    out: list[tuple[str, Path]] = []
    env = paths.env("LLAMA_DIR")
    if env:
        out.append(("environment", Path(env).expanduser()))
    if installed_layout():
        out.append(("installed", installed_engine_dir()))
    out.append(("data", paths.llama_dir()))
    return out


def locate_engine() -> Engine | None:
    override = paths.env("LLAMA_SERVER")      # an explicit program (tests, or a build of your own)
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return Engine(p, p.parent, "override")
    for source, folder in engine_search_dirs():
        exe = folder / ENGINE_EXE
        if exe.is_file():
            return Engine(exe, folder, source)
    return None


def engine_missing_message() -> str:
    if installed_layout():
        return (f"The local model engine (llama.cpp) is missing from this installation (it belongs in {installed_engine_dir()}). "
                "Reinstall Perplexify to restore it, or use Download engine to fetch it into your data folder.")
    return ("The local model engine (llama.cpp) is not installed. The installer ships it; when running from the source, "
            "use Download engine here or run `python -m perplexify download-llama` once.")


_version_cache: dict[str, str | None] = {}
_VERSION_RES = (
    re.compile(r"version:\s*\S+\s*\(build\s*(\d+),\s*commit\s*([0-9a-fA-F]+)\)"),     # b12359: "version: 1.0.0-dev (build 12359, commit b1b770d90)"
    re.compile(r"version:\s*(\d+)\s*\(([0-9a-fA-F]+)\)"),                             # older builds: "version: 4567 (abc1234)"
)


def parse_version(text: str) -> str | None:
    """The build and commit from the output of `llama-server --version` as "b12359 (b1b770d)", or None when it says neither."""
    for rx in _VERSION_RES:
        m = rx.search(text)
        if m:
            return f"b{m.group(1)} ({m.group(2)[:7]})"
    return None


def engine_version(engine: Engine, timeout: float = 25.0) -> str | None:
    """`llama-server --version` as "b12359 (b1b770d)", or None when the program does not run. Cached per file."""
    try:
        st = engine.exe.stat()
    except OSError:
        return None
    key = f"{engine.exe}|{st.st_mtime_ns}|{st.st_size}"
    if key in _version_cache:
        return _version_cache[key]
    version: str | None = None
    try:
        r = subprocess.run(engine.command("--version"), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           cwd=str(engine.dir), timeout=timeout, creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
        text = r.stdout.decode("utf-8", "replace")
        if r.returncode == 0:
            version = parse_version(text) or (text.strip().splitlines() or ["unknown"])[0][:60]
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("llama-server --version failed: %s", e)
    _version_cache[key] = version
    return version


def read_marker(folder: Path) -> dict:
    try:
        return json.loads((folder / ENGINE_MARKER).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def gpu_backends(folder: Path) -> list[str]:
    """Graphics back ends that are part of this engine folder (ggml-vulkan.dll -> "vulkan"). CPU variants are not listed."""
    out: list[str] = []
    names = [p.stem[len("ggml-"):] for p in sorted(folder.glob("ggml-*.dll"))]                       # Windows
    names += [p.name[len("libggml-"):].split(".")[0] for p in sorted(folder.glob("libggml-*.dylib"))]  # macOS: libggml-metal.1.dylib -> "metal"
    for name in names:
        if name in ("base", "rpc", "blas") or name.startswith("cpu") or name in out:
            continue
        out.append(name)
    return out


def vulkan_loader_present() -> bool:
    """Windows keeps the Vulkan loader (installed with the graphics driver) in System32. Without it the engine simply runs on the CPU."""
    if not IS_WINDOWS:
        return False
    return (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "vulkan-1.dll").is_file()


def engine_info(check_version: bool = True) -> dict:
    """What the interface shows about the engine: found or not, which build, where, whether it can use a graphics card."""
    pin = engine_pin()
    out: dict = {"found": False, "pinned": {"tag": pin["tag"], "version": pin.get("version"), "size": pin["size"]},
                 "installed_layout": installed_layout(), "can_download": IS_WINDOWS}
    eng = locate_engine()
    if eng is None:
        out["message"] = engine_missing_message()
        return out
    backends = gpu_backends(eng.dir)
    marker = read_marker(eng.dir)
    out.update({"found": True, "path": str(eng.exe), "dir": str(eng.dir), "source": eng.source, "gpu_backends": backends,
                "tag": marker.get("tag")})
    if check_version:
        out["version"] = engine_version(eng)
        if out["version"] is None:
            out["found"] = False
            out["message"] = f"{eng.exe} did not start (its --version check failed). Download the engine again."
    return out


# ------------------------------------------------------------------------------------------------------ memory and graphics
def memory_info() -> dict:
    """Installed and currently free memory, in bytes (0 when it cannot be read)."""
    total = avail = 0
    try:
        if IS_WINDOWS:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                total, avail = int(st.ullTotalPhys), int(st.ullAvailPhys)
        elif sys.platform == "darwin":
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip() or 0)
            avail = total // 2
        else:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            avail = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        pass
    return {"total": total, "available": avail}


def hardware() -> dict:
    mem = memory_info()
    eng = locate_engine()
    backends = gpu_backends(eng.dir) if eng else []
    loader = vulkan_loader_present()
    if backends and "vulkan" in backends and not loader:
        note = "The engine can use a graphics card through Vulkan, but this PC has no Vulkan driver, so models run on the CPU."
    elif backends:
        note = "Automatic uses the graphics card for as much of the model as fits in its memory; Off keeps everything on the processor."
    else:
        note = "This engine build has no graphics-card support: models run on the CPU."
    return {"ram_total": mem["total"], "ram_available": mem["available"], "cpu_threads": os.cpu_count() or 0,
            "gpu": {"backends": backends, "driver": loader, "note": note}}


def estimate_ram_need(model_bytes: int, n_ctx: int) -> int:
    """Rough memory a model needs while it runs: the file itself, the attention cache (about 30 KB per token for every GB of
    model; it is smaller for most models) and some working space."""
    kv = int(model_bytes / GIB * 30_000 * max(n_ctx, 0))
    return int(model_bytes * 1.06) + kv + 400 * 1024 ** 2


def auto_context(model_bytes: int, ctx_train: int | None, ram_total: int) -> int:
    """What "Automatic" means for the context size: as much as the memory comfortably allows, up to 16384 tokens. A bigger window
    holds more passages but costs memory and makes every answer slower to start (the whole prompt is read first)."""
    headroom = ram_total - model_bytes
    n = 8192 if not ram_total else 16384 if headroom >= 12 * GIB else 8192 if headroom >= 5 * GIB else 4096
    if ctx_train:
        n = min(n, max(2048, int(ctx_train)))
    return n


def fit_hint(model_bytes: int, n_ctx: int) -> dict:
    """Will this model probably fit? level: ok | tight (free memory is short right now) | too_big (more than this PC has)."""
    mem = memory_info()
    need = estimate_ram_need(model_bytes, n_ctx)
    level, msg = "ok", ""
    if mem["total"] and need > mem["total"] * 0.92:
        level = "too_big"
        msg = (f"This model needs about {need / GIB:.1f} GB of memory and this computer has {mem['total'] / GIB:.1f} GB, "
               "so it will probably not run. Choose a smaller model.")
    elif mem["available"] and need > mem["available"]:
        level = "tight"
        msg = (f"This model needs about {need / GIB:.1f} GB of memory and only {mem['available'] / GIB:.1f} GB is free right now. "
               "Close other programs, or choose a smaller model or a smaller context size.")
    return {"level": level, "need": need, "message": msg}


# ------------------------------------------------------------------------------------------------------ reading .gguf files
_GGUF_SCALARS = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1),
                 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}
_gguf_cache: dict[str, dict] = {}


def read_gguf_info(path: Path | str) -> dict:
    """Header of a .gguf file: {"ok", "error", "architecture", "context_length", "name", "size_label", "version", "size"}.
    Only the start of the file is read. "ok" is False when it is not a GGUF file (a renamed or half-downloaded file)."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as e:
        return {"ok": False, "error": f"cannot read the file ({e.strerror or e})", "size": 0}
    key = f"{p}|{st.st_mtime_ns}|{st.st_size}"
    hit = _gguf_cache.get(key)
    if hit is not None:
        return hit
    info: dict = {"ok": False, "error": "", "architecture": None, "context_length": None, "name": None, "size_label": None,
                  "version": None, "size": st.st_size}
    try:
        with open(p, "rb") as f:
            if f.read(4) != b"GGUF":
                info["error"] = "this is not a GGUF model file (it does not start with GGUF)"
                return _gguf_remember(key, info)
            version = struct.unpack("<I", f.read(4))[0]
            info["version"] = version
            if version < 2 or version > 3:
                info["error"] = f"unsupported GGUF version {version}"
                return _gguf_remember(key, info)
            _tensors, kv_count = struct.unpack("<QQ", f.read(16))
            info["ok"] = True
            wanted: dict[str, object] = {}
            for _ in range(min(kv_count, 4000)):
                (klen,) = struct.unpack("<Q", f.read(8))
                if klen > 512:
                    break
                k = f.read(klen).decode("utf-8", "replace")
                (vtype,) = struct.unpack("<I", f.read(4))
                if vtype == 9 and k.startswith("tokenizer."):
                    break                                       # the big vocabulary arrays: nothing we need comes after them
                value = _gguf_value(f, vtype)
                if k in ("general.architecture", "general.name", "general.size_label") or k.endswith(".context_length"):
                    wanted[k] = value
                arch = wanted.get("general.architecture")
                if arch and f"{arch}.context_length" in wanted and "general.name" in wanted:
                    break
            info["architecture"] = wanted.get("general.architecture")
            info["name"] = wanted.get("general.name")
            info["size_label"] = wanted.get("general.size_label")
            if info["architecture"]:
                ctx = wanted.get(f"{info['architecture']}.context_length")
                info["context_length"] = int(ctx) if isinstance(ctx, int) and ctx > 0 else None
    except (OSError, struct.error, ValueError, UnicodeError) as e:
        if not info["ok"]:
            info["error"] = f"cannot read the file header ({e})"
        # a header that ends early after the magic still is a GGUF file: keep what was read
    return _gguf_remember(key, info)


def _gguf_remember(key: str, info: dict) -> dict:
    if len(_gguf_cache) > 200:
        _gguf_cache.clear()
    _gguf_cache[key] = info
    return info


def _gguf_value(f, vtype: int):
    if vtype in _GGUF_SCALARS:
        fmt, n = _GGUF_SCALARS[vtype]
        return struct.unpack(fmt, f.read(n))[0]
    if vtype == 8:
        (n,) = struct.unpack("<Q", f.read(8))
        if n > 4 * 1024 * 1024:
            raise ValueError("string too long")
        return f.read(n).decode("utf-8", "replace")
    if vtype == 9:
        (etype,) = struct.unpack("<I", f.read(4))
        (count,) = struct.unpack("<Q", f.read(8))
        if count > 1_000_000:
            raise ValueError("array too long")
        return [_gguf_value(f, etype) for _ in range(count)]
    raise ValueError(f"unknown value type {vtype}")


# ------------------------------------------------------------------------------------------------------ the curated models
# All of these were checked against Hugging Face (HEAD request on the URL; size and sha256 from the API, where the LFS oid is
# the file's sha256). The URL names the repository commit, so the file behind it can never change under the checksum.
# ram_gb: about what the model needs with the default 8k window. Sizes in bytes.
CATALOG: list[dict] = [
    {"id": "qwen2.5-0.5b", "name": "Qwen2.5 0.5B Instruct", "params": "0.5B", "quant": "Q4_K_M", "license": "Apache-2.0",
     "repo": "Qwen/Qwen2.5-0.5B-Instruct-GGUF", "revision": "9217f5db79a29953eb74d5343926648285ec7e67",
     "file": "qwen2.5-0.5b-instruct-q4_k_m.gguf", "size": 491400032,
     "sha256": "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db", "ram_gb": 1,
     "summary": "Tiny and instant. Good for checking that local models work on this PC; its answers are basic.",
     "tags": ["quick test"]},
    {"id": "qwen2.5-1.5b", "name": "Qwen2.5 1.5B Instruct", "params": "1.5B", "quant": "Q4_K_M", "license": "Apache-2.0",
     "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF", "revision": "91cad51170dc346986eccefdc2dd33a9da36ead9",
     "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf", "size": 1117320736,
     "sha256": "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e", "ram_gb": 2,
     "summary": "Small and quick. Fine for simple questions about a manual on modest hardware.", "tags": []},
    {"id": "granite-4.2-3b", "name": "Granite 4.2 3B", "params": "3B", "quant": "Q4_K_M", "license": "Apache-2.0",
     "repo": "ibm-granite/granite-4.2-3b-GGUF", "revision": "c40945d71cd90f249a56985e8155551a9188dc30",
     "file": "granite-4.2-3b-Q4_K_M.gguf", "size": 2244011552,
     "sha256": "e0406663965846ae22a403456eb826ccce5f450840491f71952f18a7cb78e7d5", "ram_gb": 4,
     "summary": "IBM's compact model, tuned for answering from documents you give it. A good first real model.", "tags": []},
    {"id": "gemma-4-e2b", "name": "Gemma 4 E2B", "params": "2B", "quant": "Q4_0 (QAT)", "license": "Apache-2.0",
     "repo": "google/gemma-4-E2B-it-qat-q4_0-gguf", "revision": "675cff42a74c774d6cb76f76d8eacb49b48c9b93",
     "file": "gemma-4-E2B_q4_0-it.gguf", "size": 3349516256,
     "sha256": "fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634", "ram_gb": 5,
     "summary": "Google's efficient model, trained for 4-bit use, so it keeps its quality at this size.", "tags": []},
    {"id": "qwen2.5-7b", "name": "Qwen2.5 7B Instruct", "params": "7B", "quant": "Q4_K_M", "license": "Apache-2.0",
     "repo": "bartowski/Qwen2.5-7B-Instruct-GGUF", "revision": "8911e8a47f92bac19d6f5c64a2e2095bd2f7d031",
     "file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf", "size": 4683074240,
     "sha256": "65b8fcd92af6b4fefa935c625d1ac27ea29dcb6ee14589c55a8f115ceaaa1423", "ram_gb": 7,
     "summary": "A strong all-rounder. Needs a PC with 8 GB or more of free memory.", "tags": []},
    {"id": "granite-4.2-8b", "name": "Granite 4.2 8B", "params": "8B", "quant": "Q4_K_M", "license": "Apache-2.0",
     "repo": "ibm-granite/granite-4.2-8b-GGUF", "revision": "93f3f6a8938ee922b784cf4e5b4203cd3428df8f",
     "file": "granite-4.2-8b-Q4_K_M.gguf", "size": 5347917952,
     "sha256": "16a9369d0805f80b7377d25d87f937a90c05dc04ad79173a52001e42c9aab311", "ram_gb": 8,
     "summary": "The most capable model in this list, best at answering from documents. Needs about 8 GB of free memory.",
     "tags": []},
]


def catalog_url(item: dict) -> str:
    return f"https://huggingface.co/{item['repo']}/resolve/{item['revision']}/{item['file']}"


def catalog_item(item_id: str) -> dict | None:
    return next((c for c in CATALOG if c["id"] == item_id), None)


def models_dir() -> Path:
    return paths.llm_dir()


def catalog_state(item: dict) -> dict:
    """installed (the verified file is in the model folder), partial (an interrupted download that can resume) or available."""
    final = models_dir() / item["file"]
    try:
        if final.is_file() and final.stat().st_size == item["size"]:
            return {"state": "installed", "bytes": item["size"]}
    except OSError:
        pass
    part = final.with_name(final.name + ".part")
    try:
        if part.is_file() and 0 < part.stat().st_size <= item["size"]:      # all of it but not yet checked counts as unfinished too
            return {"state": "partial", "bytes": part.stat().st_size}
    except OSError:
        pass
    return {"state": "available", "bytes": 0}


# ------------------------------------------------------------------------------------------------------ the user's model files
def model_entry(path: Path, linked: bool, model_id: str | None = None) -> dict:
    """One model file as the interface lists it."""
    try:
        size = path.stat().st_size
        exists = True
    except OSError:
        size, exists = 0, False
    name = path.name
    cat = next((c for c in CATALOG if c["file"].lower() == name.lower()), None)
    g = read_gguf_info(path) if exists else {"ok": False, "error": "the file is not there any more"}
    return {"id": model_id or name, "file": name, "path": str(path), "size": size, "linked": linked, "exists": exists,
            "valid": bool(g.get("ok")), "error": g.get("error") or "", "architecture": g.get("architecture"),
            "context_length": g.get("context_length"), "catalog_id": cat["id"] if cat else None,
            "title": cat["name"] if cat else (g.get("name") or Path(name).stem)}


def installed_models(linked_paths: list[str]) -> list[dict]:
    """Downloaded files (<data folder>/llm) and the files the user chose from elsewhere. Ids are file names; a chosen file whose
    name is already taken by another one is listed under its full path so every id stays unique."""
    out: list[dict] = []
    seen: set[str] = set()
    folder = models_dir()
    for p in sorted(folder.glob("*.gguf"), key=lambda x: x.name.lower()):
        if p.is_file():
            out.append(model_entry(p, False))
            seen.add(p.name.lower())
    for s in linked_paths:
        p = Path(s)
        key = p.name.lower()
        out.append(model_entry(p, True, model_id=p.name if key not in seen else str(p)))
        seen.add(key)
    return out


def resolve_model_path(profile: dict) -> Path | None:
    """The file a local profile points at: its own model_path, else the model folder, else a path stored in `model`."""
    mp = str(profile.get("model_path") or "").strip()
    if mp and Path(mp).is_file():
        return Path(mp)
    name = str(profile.get("model") or "").strip()
    if not name:
        return None
    cand = models_dir() / Path(name).name
    if cand.is_file():
        return cand
    p = Path(name)
    return p if p.is_absolute() and p.is_file() else None


CONTEXT_CHOICES = (4096, 8192, 16384, 32768)


def resolve_context(profile: dict, model_path: Path) -> int:
    """The context window the engine is started with: the user's choice (not above what the model was trained for) or Automatic."""
    g = read_gguf_info(model_path)
    train = g.get("context_length")
    try:
        size = model_path.stat().st_size
    except OSError:
        size = 0
    want = profile.get("context_tokens")
    if want:
        n = int(want)
        return min(n, int(train)) if train else n
    return auto_context(size, train, memory_info()["total"])


@dataclass(frozen=True)
class EngineSpec:
    """What an engine process is started with. Two equal specs share one process; a different one restarts it."""
    model_path: str
    n_ctx: int
    gpu: str = "auto"       # "auto": llama.cpp uses a graphics card for what fits; "off": the CPU only


def spec_for_profile(profile: dict) -> EngineSpec:
    """The engine settings a "Local model" profile asks for. Raises LocalLLMError (model_missing) when no usable file is set."""
    name = str(profile.get("model") or "").strip()
    if not name and not profile.get("model_path"):
        raise LocalLLMError("No local model is chosen yet. Open Settings > Models, or Connect a model > Local model, and pick one.",
                            "model_missing")
    path = resolve_model_path(profile)
    if path is None:
        raise LocalLLMError(f"The model file '{name or profile.get('model_path')}' was not found. Choose another model in "
                            "Settings > Models, or download it again.", "model_missing")
    gpu = profile.get("gpu") if profile.get("gpu") in ("auto", "off") else "auto"
    return EngineSpec(str(path), resolve_context(profile, path), gpu)


def pick_model_file(title: str = "Choose a model file (.gguf)") -> str | None:
    """A native file dialog that lists .gguf files (the app's own file dialog is limited to documents). None when cancelled."""
    if IS_WINDOWS:
        flt = "GGUF models (*.gguf)|*.gguf|All files (*.*)|*.*"
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$o = New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; "
            "$d = New-Object System.Windows.Forms.OpenFileDialog; $d.Multiselect = $false; "
            f"$d.Title = {osutil._ps_quote(title)}; $d.Filter = {osutil._ps_quote(flt)}; "
            "if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) { $d.FileName }"
        )
        return osutil._run_ps(script) or None
    if osutil.IS_MAC:
        out = osutil._osascript(["tell current application to activate",
                                 f'POSIX path of (choose file with prompt {osutil._as_quote(title)})'])
        return out or None
    return None


# ------------------------------------------------------------------------------------------------------ downloads
class DownloadError(RuntimeError):
    """A download that failed for a reason the user can act on (the message says what)."""


def _fmt_bytes(n: float) -> str:
    """A file size the way the Hugging Face page shows it (decimal units), so it can be compared with the download page."""
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _sha256_of(path: Path, progress: Callable[[int, int], None] | None = None, check: Callable[[], None] | None = None) -> str:
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if check:
                check()
            if progress:
                progress(done, total)
    return h.hexdigest()


def download_file(url: str, dest: Path, *, size: int | None = None, sha256: str | None = None,
                  progress: Callable[[str, int, int, str], None] | None = None, check: Callable[[], None] | None = None,
                  label: str = "the file", retries: int = 8) -> Path:
    """Download `url` to `dest`: resumable (HTTP Range; a ".part" file is kept when the download is interrupted or stopped),
    checked against the expected size and SHA-256 before it gets its final name. `progress(phase, current, total, message)` is
    called while it runs, phase "download" or "verify". `check()` may raise Cancelled. Raises DownloadError with a plain message."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if size and have > size:
        part.unlink(missing_ok=True)
        have = 0
    if size:
        need = size - have + 64 * 1024 ** 2
        free = shutil.disk_usage(dest.parent).free
        if free < need:
            raise DownloadError(f"Not enough free disk space for {label}: it needs {_fmt_bytes(need)} and {_fmt_bytes(free)} is free "
                                f"on the drive of {dest.parent}.")
    total = size or 0
    t_start, start_have, last_report = time.monotonic(), have, 0.0
    failures, last_error = 0, ""
    timeout = httpx.Timeout(connect=20, read=60, write=30, pool=20)
    headers_base = {"User-Agent": f"Perplexify/{__version__}", "Accept-Encoding": "identity"}      # the file as it is, byte for byte

    def report(force: bool = False) -> None:
        nonlocal last_report
        now = time.monotonic()
        if progress is None or (not force and now - last_report < 0.25):
            return
        last_report = now
        elapsed = max(now - t_start, 1e-6)
        got = have - start_have
        speed = max(0.0, got / elapsed)
        if elapsed < 1.5 or got < 512 * 1024:       # too early for a speed that means anything (the connection is still being set up)
            msg = "starting..."
        else:
            msg = f"{speed / 1e6:.1f} MB/s"
            if speed > 1 and total and have < total:
                left = (total - have) / speed
                msg += f", about {int(left // 60)} min left" if left >= 90 else f", about {int(left)} s left"
        progress("download", have, total, msg)

    restarts = 0
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers_base) as client:
        while not (total and have >= total):
            if check:
                check()
            headers = {"Range": f"bytes={have}-"} if have else {}
            made_progress = False
            try:
                with client.stream("GET", url, headers=headers) as r:
                    if r.status_code == 416:      # "nothing there beyond this byte": the part is the whole file, or is longer than it
                        if (total and have == total) or restarts >= 2:
                            break                  # complete (or the server's file is shorter than expected: checked below)
                        restarts += 1
                        part.unlink(missing_ok=True)
                        have = 0
                        continue
                    if r.status_code in (401, 403):
                        raise DownloadError(f"The server refused the download of {label} (HTTP {r.status_code}). The file may need a login or an agreement on its page.")
                    if r.status_code == 404:
                        raise DownloadError(f"{label[:1].upper() + label[1:]} is no longer at {httpx.URL(url).host} (HTTP 404).")
                    if r.status_code >= 400:
                        raise DownloadError(f"The server answered HTTP {r.status_code} for {label}.")
                    if have and r.status_code == 200:       # it ignored the Range header: start from the beginning
                        have = 0
                    mode = "ab" if have else "wb"
                    begin = have
                    length = int(r.headers.get("content-length") or 0)
                    if not total:
                        total = have + length if length else 0
                    with open(part, mode) as f:
                        for chunk in r.iter_raw():          # as they arrive, so an interruption loses almost nothing
                            f.write(chunk)
                            have += len(chunk)
                            made_progress = True
                            if check:
                                check()
                            report()
                    if length and have - begin < length:
                        raise httpx.ReadError("the connection closed early")
                    break          # the response arrived completely: it was the rest of the file (a size that differs is reported below)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_error = str(e) or e.__class__.__name__
                have = part.stat().st_size if part.exists() else 0
                failures = 0 if made_progress else failures + 1
                if failures > retries:
                    raise DownloadError(f"The download of {label} keeps failing ({last_error}). Check the internet connection; "
                                        "starting it again resumes where it stopped.") from e
                if progress:
                    progress("download", have, total, f"connection problem, retrying ({last_error[:60]})")
                for _ in range(min(10, 2 * (failures + 1))):       # wait, but stay responsive to Stop
                    if check:
                        check()
                    time.sleep(0.5)
                continue
        report(force=True)

    got = part.stat().st_size if part.exists() else 0
    if size and got != size:
        raise DownloadError(f"The download of {label} ended with {got} bytes instead of {size}. Start it again to resume.")
    if sha256:
        def hashing(done: int, whole: int) -> None:
            if progress:
                progress("verify", done, whole, "checking the file")

        digest = _sha256_of(part, hashing, check)
        if digest.lower() != sha256.lower():
            part.unlink(missing_ok=True)
            raise DownloadError(f"{label[:1].upper() + label[1:]} was damaged in the download (its checksum does not match). The file was deleted: start the download again.")
    os.replace(part, dest)
    return dest


# ---- jobs that run beside the indexing queue
def start_job(jobs: JobManager, kind: str, title: str, fn: Callable[[Job], None]) -> Job:
    """Run `fn(job)` in its own thread, listed with the other jobs (sidebar, progress, Stop). The job manager runs one job at a time
    and a 5 GB model download must not hold up indexing (or the other way round), so these do not go through its queue."""
    job = Job(kind, title, None)
    with jobs._lock:
        jobs._jobs[job.id] = job
        while len(jobs._jobs) > 60:
            jobs._jobs.popitem(last=False)

    def run() -> None:
        job.status, job.started = "running", time.time()
        try:
            fn(job)
            job.status = "done"
        except Cancelled:
            job.status, job.message = "cancelled", "Stopped. What was downloaded so far is kept, so starting again resumes."
        except Exception as e:  # noqa: BLE001 - a job must never take the app down
            log.exception("job %s failed", job.id)
            job.status, job.error = "error", str(e) or e.__class__.__name__
        finally:
            job.finished = time.time()

    threading.Thread(target=run, name=f"job-{kind}", daemon=True).start()
    return job


_active_downloads: dict[str, Job] = {}
_dl_lock = threading.Lock()


def job_progress(job: Job) -> Callable[[str, int, int, str], None]:
    def update(phase: str, current: int, total: int, message: str) -> None:
        job.update(phase=phase, current=current, total=total, message=message)

    return update


def start_model_download(jobs: JobManager, item_id: str) -> Job:
    """Download a curated model into <data folder>/llm. One download per model at a time (asking again returns the running job)."""
    item = catalog_item(item_id)
    if item is None:
        raise ValueError("Unknown model")
    with _dl_lock:
        running = _active_downloads.get(item_id)
        if running is not None and running.status in ("queued", "running"):
            return running
        dest = models_dir() / item["file"]
        if catalog_state(item)["state"] == "installed":
            raise ValueError(f"{item['name']} is already downloaded.")

        def work(job: Job) -> None:
            try:
                download_file(catalog_url(item), dest, size=item["size"], sha256=item["sha256"], progress=job_progress(job),
                              check=job.check, label=item["name"])
                job.update(phase="done", message=f"{item['name']} is ready to use.")
            finally:
                with _dl_lock:
                    _active_downloads.pop(item_id, None)

        job = start_job(jobs, "llm:" + item_id, f"Download {item['name']} ({_fmt_bytes(item['size'])})", work)
        _active_downloads[item_id] = job
        return job


def active_download(item_id: str) -> Job | None:
    with _dl_lock:
        job = _active_downloads.get(item_id)
    return job if job is not None and job.status in ("queued", "running") else None


def delete_model_file(path: Path) -> bool:
    """Delete a downloaded model (only inside the model folder) and its unfinished part. True when something was removed."""
    path = Path(path)
    if path.parent.resolve() != models_dir().resolve():
        raise ValueError("Only models downloaded by this app can be deleted here.")
    removed = False
    for p in (path, path.with_name(path.name + ".part")):
        try:
            if p.is_file():
                p.unlink()
                removed = True
        except OSError as e:
            raise ValueError(f"Could not delete {p.name}: {e.strerror or e}. Is the model in use? Stop it first.") from e
    return removed


# ------------------------------------------------------------------------------------------------------ the engine download
def _pe_imports(path: Path) -> set[str]:
    """Names of the DLLs a Windows program or library imports (read from its file; nothing is run)."""
    try:
        d = path.read_bytes()
        if d[:2] != b"MZ":
            return set()
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        if d[pe:pe + 4] != b"PE\0\0":
            return set()
        nsec = struct.unpack_from("<H", d, pe + 6)[0]
        optsz = struct.unpack_from("<H", d, pe + 20)[0]
        opt = pe + 24
        dd = opt + (112 if struct.unpack_from("<H", d, opt)[0] == 0x20B else 96)
        secs = []
        for i in range(nsec):
            o = opt + optsz + i * 40
            vsize, va, rawsz, rawptr = struct.unpack_from("<IIII", d, o + 8)
            secs.append((va, max(vsize, rawsz), rawptr))

        def offset(rva: int) -> int | None:
            return next((raw + rva - va for va, sz, raw in secs if va <= rva < va + sz), None)

        def cstr(off: int) -> str:
            return d[off:d.index(b"\0", off)].decode("ascii", "replace").lower()

        out: set[str] = set()
        for slot, step in ((1, 20), (13, 32)):                # regular imports, delay-load imports
            rva = struct.unpack_from("<I", d, dd + slot * 8)[0]
            o = offset(rva) if rva else None
            while o is not None:
                name = struct.unpack_from("<I", d, o + (12 if step == 20 else 4))[0]
                if not name:
                    break
                n = offset(name)
                if n is not None:
                    out.add(cstr(n))
                o += step
        return out
    except (OSError, struct.error, ValueError):
        return set()


def prune_engine(folder: Path, keep: list[str], exe_name: str = ENGINE_EXE) -> list[str]:
    """Delete from an extracted engine everything the server does not need (the other command line tools and their libraries).
    `keep` are file name patterns. Every DLL that llama-server (transitively) imports is kept as well, whatever the patterns say.
    Returns the names removed."""
    files = {p.name.lower(): p for p in folder.iterdir() if p.is_file()}
    needed: set[str] = set()
    todo = [exe_name.lower()]
    while todo:
        cur = todo.pop()
        if cur in needed or cur not in files:
            continue
        needed.add(cur)
        todo.extend(i for i in _pe_imports(files[cur]) if i in files)
    removed = []
    for name, p in sorted(files.items()):
        if name in needed or any(fnmatch.fnmatch(name, pat.lower()) for pat in keep):
            continue
        try:
            p.unlink()
            removed.append(p.name)
        except OSError as e:
            log.warning("could not remove %s from the engine folder: %s", p, e)
    return removed


def _safe_extract(zip_path: Path, target: Path) -> None:
    root = target.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            out = (target / info.filename).resolve()
            if root != out and root not in out.parents:
                raise DownloadError(f"The engine archive contains an unsafe path ({info.filename}); it was not unpacked.")
        z.extractall(target)


def download_engine(dest: Path, cache: Path | None = None, *,
                    progress: Callable[[str, int, int, str], None] | None = None, check: Callable[[], None] | None = None,
                    run_check: bool = True, pin: dict | None = None, keep_archive: bool = True) -> dict:
    """Fetch the pinned llama.cpp build into `dest` (a folder that will contain llama-server.exe): download (cached in `cache`,
    resumable), verify the SHA-256 of the zip, unpack, remove what the server does not need, check that it runs (`--version`) and
    only then put it in place. Returns the marker written into the folder. The engine is never downloaded at run time by the app
    unless the user asks (Download engine): the installer ships it. The zip stays in `cache` (the installer build reuses it) unless
    `keep_archive` is false."""
    pin = pin or engine_pin()
    if not IS_WINDOWS and not paths.env("LLAMA_SERVER"):
        raise DownloadError("The pinned llama.cpp build is for Windows. On this system install llama-server yourself and set PERPLEXIFY_LLAMA_SERVER.")
    dest = Path(dest)
    cache = Path(cache) if cache else paths.home() / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    zip_path = cache / pin["asset"]
    size, sha = int(pin["size"]), str(pin["sha256"]).lower()
    cached_ok = False
    if zip_path.is_file() and zip_path.stat().st_size == size:
        if progress:
            progress("verify", 0, size, "checking the downloaded engine")
        cached_ok = _sha256_of(zip_path, check=check) == sha
        if not cached_ok:
            zip_path.unlink(missing_ok=True)
    if not cached_ok:
        download_file(pin["url"], zip_path, size=size, sha256=sha, progress=progress, check=check, label="the llama.cpp engine")
    if progress:
        progress("unpack", 0, 0, "unpacking")
    work = dest.with_name(dest.name + ".new")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        _safe_extract(zip_path, work)
        removed = prune_engine(work, list(pin.get("keep") or ["*"]))
        exe = work / ENGINE_EXE
        if not exe.is_file():
            raise DownloadError(f"The engine archive has no {ENGINE_EXE}.")
        marker = {"tag": pin["tag"], "version": pin.get("version"), "commit": pin.get("commit"), "asset": pin["asset"],
                  "sha256": sha, "removed": len(removed), "files": sorted(p.name for p in work.iterdir() if p.is_file() and p.name != ENGINE_MARKER)}
        (work / ENGINE_MARKER).write_text(json.dumps(marker, indent=2), "utf-8")
        if run_check:
            if progress:
                progress("verify", 0, 0, "starting the engine once to check it")
            version = engine_version(Engine(exe, work, "data"))
            if not version:
                raise DownloadError("The engine was downloaded and unpacked but does not start on this PC (llama-server --version failed). "
                                    "A missing Visual C++ runtime is the usual cause.")
            marker["run_version"] = version
        manager().require_not_running_from(dest)
        backup = dest.with_name(dest.name + ".old")
        shutil.rmtree(backup, ignore_errors=True)
        if dest.exists():
            os.replace(dest, backup)
        os.replace(work, dest)
        shutil.rmtree(backup, ignore_errors=True)
        if not keep_archive:
            zip_path.unlink(missing_ok=True)
        return marker
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------------------------------------ keeping the process tied to the app
@functools.lru_cache(maxsize=1)
def _win():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes

    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    return k


class _JobObject:
    """A Windows job object that kills every process in it when its last handle closes. This process holds the only handle, so
    when it ends in any way (also when it is killed from the task manager) Windows ends llama-server with it. There is one per
    engine process, so stopping an engine ends all of its processes (not only the first one) in one call."""

    KILL_ON_JOB_CLOSE = 0x2000
    EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                           "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self._k = _win()
        self.handle = self._k.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
        if not self._k.SetInformationJobObject(self.handle, self.EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, pid: int) -> bool:
        h = self._k.OpenProcess(0x0100 | 0x0001, False, pid)        # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not h:
            return False
        try:
            return bool(self._k.AssignProcessToJobObject(self.handle, h))
        finally:
            self._k.CloseHandle(h)

    def terminate(self) -> None:
        if self.handle:
            self._k.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        h, self.handle = self.handle, None
        if h:
            self._k.CloseHandle(h)          # the last handle: with KILL_ON_JOB_CLOSE this ends whatever is still in the job


def _proc_identity(pid: int) -> tuple[str, int] | None:
    """(image path, creation time) of a process that is still running, else None. Together they tell a process from a later
    one that was given the same id."""
    if not IS_WINDOWS or pid <= 0:
        return None
    from ctypes import wintypes

    k = _win()
    h = k.OpenProcess(0x1000, False, pid)                             # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:      # 259: STILL_ACTIVE
            return None
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(len(buf))
        path = buf.value if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        created = (c.dwHighDateTime << 32) | c.dwLowDateTime if k.GetProcessTimes(h, *(ctypes.byref(x) for x in (c, e, kt, ut))) else 0
        return path, created
    finally:
        k.CloseHandle(h)


def _kill_pid(pid: int) -> None:
    if IS_WINDOWS:
        k = _win()
        h = k.OpenProcess(0x0001, False, pid)                         # PROCESS_TERMINATE
        if h:
            try:
                k.TerminateProcess(h, 1)
            finally:
                k.CloseHandle(h)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def sweep_orphans() -> int:
    """End every llama-server that an earlier run of the app left behind (a crash, or a kill the job object could not cover).
    Each running engine has a small file naming its owner; only engines whose owner is gone are ended, and only when the process
    still is the one that was recorded (same program, same start time), so a recycled process id is never touched.
    Returns how many were ended."""
    ended = 0
    try:
        files = list(paths.engine_state_dir().glob("*.json"))
    except OSError:
        return 0
    for f in files:
        try:
            info = json.loads(f.read_text("utf-8"))
            pid, owner = int(info["pid"]), int(info["owner_pid"])
        except (OSError, ValueError, KeyError, TypeError):
            f.unlink(missing_ok=True)
            continue
        if owner == os.getpid():
            continue
        owner_now = _proc_identity(owner)
        if owner_now is not None and owner_now[1] == info.get("owner_created"):
            continue                                                  # its app still runs: not an orphan
        now = _proc_identity(pid)
        if now is not None and now[0].lower() == str(info.get("image", "")).lower() and now[1] == info.get("created"):
            log.warning("ending llama-server %s left behind by an earlier run", pid)
            _kill_pid(pid)
            ended += 1
        f.unlink(missing_ok=True)
    return ended


# ------------------------------------------------------------------------------------------------------ the engine process
@dataclass
class EngineHandle:
    """A running, ready engine: where to talk to it and what it was started with."""
    base_url: str          # http://127.0.0.1:<port>/v1
    api_key: str
    port: int
    pid: int
    n_ctx: int             # the window the engine reports (it asked for spec.n_ctx)
    spec: EngineSpec
    started: float
    load_seconds: float
    gpu: dict


class _Attempt:
    """One start of the engine (running in a background thread). Everyone who wants the same engine waits on `done`."""

    def __init__(self, spec: EngineSpec, generation: int, after: "_Attempt | None" = None):
        self.spec, self.generation, self.after = spec, generation, after
        self.t0 = time.monotonic()
        self.done = threading.Event()
        self.handle: EngineHandle | None = None
        self.error: LocalLLMError | None = None

    @classmethod
    def ready(cls, spec: EngineSpec, handle: EngineHandle) -> "_Attempt":
        a = cls(spec, 0)
        a.handle = handle
        a.done.set()
        return a

    def result(self) -> EngineHandle:
        self.done.wait()
        if self.error is not None:
            raise self.error
        assert self.handle is not None
        return self.handle

    @property
    def seconds(self) -> int:
        return int(time.monotonic() - self.t0)


class _EngineDied(Exception):
    def __init__(self, code: int | None):
        super().__init__(f"exit code {code}")
        self.code = code


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def build_args(model: str, port: int, key: str, n_ctx: int, gpu: str = "auto", jinja: bool = True) -> list[str]:
    """llama-server's arguments. It listens on 127.0.0.1 only, behind a key; one request at a time (a desktop app has one user);
    no web page of its own; no prompt cache in memory (the cache could grow to GBs and our prompts hardly ever repeat)."""
    args = ["-m", model, "--host", "127.0.0.1", "--port", str(port), "--api-key", key, "-c", str(n_ctx), "-np", "1",
            "--no-webui", "--cache-ram", "0", "--no-cache-idle-slots", "--jinja" if jinja else "--no-jinja",
            # Log level 4 is the only one at which this build says which graphics card it uses and how many layers it put there
            # (parse_gpu reads that from the log). It does not write prompts or answers to the log; tests/test_local.py keeps the
            # log lines this relies on, and a newer engine must be checked for that again before it is pinned.
            "-lv", LOG_LEVEL]
    if gpu == "off":
        args += ["--device", "none", "-ngl", "0"]          # --device none: the graphics back end is not even started
    return args


def classify_failure(tail: str) -> tuple[str, str]:
    """(kind, plain description) of why the engine stopped while loading, from the end of its log."""
    t = tail.lower()
    if re.search(r"couldn't bind|failed to bind|address already in use|only one usage of each socket", t):
        return "bind", "the port was taken"
    m = re.search(r"unknown model architecture:\s*'([^']+)'", tail, re.I)
    if m:
        return "arch", m.group(1)
    if re.search(r"out of memory|failed to allocate|bad_alloc|not enough memory|erroroutofdevicememory|erroroutofhostmemory|cudamalloc failed|unable to fit", t):
        return "memory", "memory"
    if re.search(r"invalid magic|failed to read magic|not a valid gguf|file is too small|data is not within the file bounds|premature end|unexpected end", t):
        return "corrupt", "the file is damaged"
    if re.search(r"jinja|chat template|minja|template parsing", t):
        return "template", "the chat template"
    if re.search(r"vulkan|vk::|device lost|errorinitializationfailed|ggml_backend_.*(init|load)", t):
        return "gpu", "the graphics back end"
    return "other", ""


def friendly_failure(kind: str, what: str, model_name: str, model_bytes: int, n_ctx: int, tag: str, tail: str) -> str:
    lines = [ln for ln in (_ANSI.sub("", ln).strip() for ln in tail.splitlines()) if ln]
    last = " | ".join(lines[-2:])[:300]
    if kind == "memory":
        return (f"There is not enough free memory to load {model_name} ({_fmt_bytes(model_bytes)}) with a window of {n_ctx:,} tokens. "
                "Close other programs, choose a smaller model, or a smaller context size in Settings > Models.")
    if kind == "arch":
        return (f"{model_name} uses a model type ('{what}') that the bundled llama.cpp ({tag}) does not know. "
                "Choose another model, or update the engine.")
    if kind == "corrupt":
        return f"{model_name} looks damaged or incomplete (llama.cpp could not read it). If you downloaded it, delete it and download it again."
    return f"The local model engine stopped while loading {model_name}. {last}".strip()


class EngineManager:
    """Owns the one llama-server process. Thread-safe: any number of requests may call start()/ensure() at once; they share
    one start, and a request for different settings waits for the running start, then restarts the engine."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._handle: EngineHandle | None = None
        self._spec: EngineSpec | None = None
        self._attempt: _Attempt | None = None
        self._last_error: tuple[EngineSpec, LocalLLMError] | None = None
        self._generation = 0
        self._job: _JobObject | None = None          # the job object holding the running process (Windows)
        self._job_failed = False
        self._swept = False
        self._state_file: Path | None = None
        self._log_path = ""

    # ---- asking for an engine
    def start(self, spec: EngineSpec) -> _Attempt:
        """Begin starting an engine for `spec` unless one runs already. Never blocks: wait on the returned attempt."""
        with self._lock:
            live = self._live()
            if live is not None and self._spec == spec:
                return _Attempt.ready(spec, live)
            cur = self._attempt
            if cur is not None and cur.spec == spec and not cur.done.is_set():
                return cur
            att = _Attempt(spec, self._generation, after=cur if cur is not None and not cur.done.is_set() else None)
            self._attempt = att
        threading.Thread(target=self._run, args=(att,), name="llama-start", daemon=True).start()
        return att

    def ensure(self, spec: EngineSpec) -> EngineHandle:
        """The engine for `spec`, started first if needed (blocks until it is ready; raises LocalLLMError)."""
        return self.start(spec).result()

    def current(self, spec: EngineSpec | None = None) -> EngineHandle | None:
        """The ready engine, when one runs (and, with a spec, when it was started with exactly these settings)."""
        with self._lock:
            live = self._live()
            return live if live is not None and (spec is None or self._spec == spec) else None

    def note_dead(self) -> None:
        """A request found the engine unreachable: forget it so the next request starts a new one."""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(0.5)
            except subprocess.TimeoutExpired:
                return
        with self._lock:
            self._live()

    def stop(self) -> None:
        """Stop the engine (also one that is still loading). Safe to call at any time, also twice."""
        with self._lock:
            self._generation += 1
            proc, self._proc = self._proc, None
            job, self._job = self._job, None
            self._handle, self._spec = None, None
        if proc is not None:
            self._terminate(proc, job)
        self._forget_state_file()

    def require_not_running_from(self, folder: Path) -> None:
        with self._lock:
            proc = self._proc
            spec_running = self._live() is not None
        if proc is not None and proc.poll() is None and spec_running:
            eng = locate_engine()
            if eng is not None and eng.dir.resolve() == Path(folder).resolve():
                raise DownloadError("The local model is running from that engine folder. Stop it first (Settings > Models > Stop).")

    # ---- what is going on
    def status(self) -> dict:
        with self._lock:
            live = self._live()
            att = self._attempt if self._attempt is not None and not self._attempt.done.is_set() else None
            err = self._last_error
            if att is not None:
                state, spec = "loading", att.spec
            elif live is not None:
                state, spec = "ready", self._spec
            else:
                state, spec = ("error", err[0]) if err else ("stopped", None)
            out: dict = {"state": state, "log": self._log_path}
            if spec is not None:
                out.update({"model": Path(spec.model_path).name, "model_path": spec.model_path, "n_ctx": spec.n_ctx, "gpu_mode": spec.gpu})
            if att is not None:
                out["loading_seconds"] = att.seconds
            if live is not None and state == "ready":
                out.update({"port": live.port, "pid": live.pid, "n_ctx": live.n_ctx, "load_seconds": round(live.load_seconds, 1),
                            "gpu": live.gpu, "uptime": int(time.time() - live.started)})
            if state == "error" and err is not None:
                out.update({"error": str(err[1]), "error_code": err[1].code})
            return out

    # ---- internals
    def _live(self) -> EngineHandle | None:
        """The ready handle if its process still runs; when it has died, forget it and remember why."""
        proc, handle = self._proc, self._handle
        if handle is None:
            return None
        if proc is not None and proc.poll() is None:
            return handle
        code = proc.returncode if proc is not None else None
        self._proc, self._handle = None, None
        job, self._job = self._job, None
        if job is not None:
            job.close()                            # ends anything the dead process left running
        spec, self._spec = self._spec, None
        if spec is not None:
            self._last_error = (spec, LocalLLMError(
                f"The local model stopped unexpectedly (exit code {code}). It is started again when you ask the next question. "
                f"The engine log is {self._log_path}.", "engine_died"))
        self._forget_state_file()
        return None

    def _run(self, att: _Attempt) -> None:
        try:
            if att.after is not None:
                att.after.done.wait()
            with self._lock:
                live = self._live()
                if live is not None and self._spec == att.spec:
                    att.handle = live
                    return
                if att.generation != self._generation:
                    raise LocalLLMError("The local model was stopped.", "engine_died")
            att.handle = self._launch(att)
        except LocalLLMError as e:
            att.error = e
        except Exception as e:  # noqa: BLE001
            log.exception("starting the local model failed")
            att.error = LocalLLMError(f"The local model could not be started: {e}", "engine_failed")
        finally:
            with self._lock:
                if att.error is not None:
                    self._last_error = (att.spec, att.error)
                elif att.handle is not None:
                    self._last_error = None
                if self._attempt is att:
                    self._attempt = None
            att.done.set()

    def _launch(self, att: _Attempt) -> EngineHandle:
        spec = att.spec
        engine = locate_engine()
        if engine is None:
            raise LocalLLMError(engine_missing_message(), "engine_missing")
        model = Path(spec.model_path)
        if not model.is_file():
            raise LocalLLMError(f"The model file {model.name} was not found.", "model_missing")
        gguf = read_gguf_info(model)
        if not gguf.get("ok"):
            raise LocalLLMError(f"{model.name} is not a usable model file: {gguf.get('error')}. If you downloaded it, delete it and download it again.",
                                "model_missing")
        if not self._swept:
            self._swept = True
            try:
                sweep_orphans()
            except Exception:  # noqa: BLE001 - tidying up must never stop a start
                log.debug("orphan sweep failed", exc_info=True)
        self._stop_process()                       # whatever runs now was started for other settings (or is dead)
        tag = (read_marker(engine.dir).get("tag")) or engine_pin()["tag"]
        gpu, jinja, fell_back = spec.gpu, True, False
        t0 = time.monotonic()
        tail = ""
        for attempt_no in range(1, 5):
            port, key = _free_port(), secrets.token_urlsafe(24)
            args = build_args(spec.model_path, port, key, spec.n_ctx, gpu, jinja)
            log_start = self._open_log(engine, args, key)
            try:
                proc, job = self._spawn(engine, args)
            except OSError as e:
                raise LocalLLMError(f"Could not start {engine.exe.name}: {e}", "engine_failed") from e
            with self._lock:
                if att.generation != self._generation:
                    self._terminate(proc, job)
                    raise LocalLLMError("The local model was stopped.", "engine_died")
                self._proc, self._job = proc, job
            self._remember_process(proc)
            try:
                self._wait_ready(att, proc, port, key)
            except _EngineDied as died:
                tail = self._log_tail(log_start)
                with self._lock:
                    if self._proc is proc:
                        self._proc, self._job = None, None
                if job is not None:
                    job.close()
                kind, what = classify_failure(tail)
                log.warning("llama-server stopped while loading (%s, exit %s)", kind, died.code)
                if kind == "bind" and attempt_no < 4:
                    continue
                if kind == "template" and jinja:
                    jinja = False                  # the model's own chat template could not be used: let llama.cpp use its plain one
                    continue
                if kind == "gpu" and gpu == "auto" and not fell_back:
                    gpu, fell_back = "off", True   # the graphics driver misbehaves: run on the CPU
                    continue
                raise LocalLLMError(friendly_failure(kind, what, model.name, gguf.get("size") or 0, spec.n_ctx, tag, tail),
                                    "engine_failed", detail=tail[-2000:]) from None
            except LocalLLMError:
                self._terminate(proc, job)
                with self._lock:
                    if self._proc is proc:
                        self._proc, self._job = None, None
                self._forget_state_file()
                raise
            tail = self._log_tail(log_start)
            n_ctx = _read_n_ctx(port, key) or spec.n_ctx
            gpu_info = parse_gpu(tail)
            gpu_info["mode"] = "off" if (gpu == "off") else "auto"
            if fell_back:
                gpu_info["fell_back"] = True
            handle = EngineHandle(f"http://127.0.0.1:{port}/v1", key, port, proc.pid, n_ctx, spec, time.time(), time.monotonic() - t0, gpu_info)
            with self._lock:
                self._handle, self._spec = handle, spec
            log.info("llama-server %s ready on port %s (window %s tokens, %.1f s)", proc.pid, port, n_ctx, handle.load_seconds)
            return handle
        raise LocalLLMError("The local model engine could not be started.", "engine_failed", detail=tail[-2000:])

    def _wait_ready(self, att: _Attempt, proc: subprocess.Popen, port: int, key: str, timeout: float = 900.0) -> None:
        started = time.monotonic()
        deadline = started + timeout
        url = f"http://127.0.0.1:{port}/health"
        headers = {"Authorization": f"Bearer {key}"}
        # a short connect timeout: before the server has bound its port Windows can take a second or two to refuse a connection
        with httpx.Client(timeout=httpx.Timeout(3.0, connect=0.4), trust_env=False) as c:
            while True:
                code = proc.poll()
                if code is not None:
                    raise _EngineDied(code)
                if att.generation != self._generation:
                    raise LocalLLMError("The local model was stopped.", "engine_died")
                try:
                    r = c.get(url, headers=headers)
                    if r.status_code == 200:
                        return                     # 503 means "still loading the model"
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise LocalLLMError("The local model did not finish loading in 15 minutes. Try a smaller model.", "engine_failed")
                time.sleep(min(0.25, 0.04 + (time.monotonic() - started) * 0.1))       # quick at first, then gentle

    def _spawn(self, engine: Engine, args: list[str]) -> tuple[subprocess.Popen, "_JobObject | None"]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("LLAMA_")}      # a setting in the environment must not widen access
        env["PYTHONUNBUFFERED"] = "1"
        flags = (CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY) if IS_WINDOWS else 0
        with open(self._log_path, "ab") as log_file:
            proc = subprocess.Popen(engine.command(*args), stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
                                    cwd=str(engine.dir), env=env, creationflags=flags)
        return proc, self._bind_to_app(proc)

    def _bind_to_app(self, proc: subprocess.Popen) -> "_JobObject | None":
        """Put the new process in a job object that ends it with this process (Windows). None when that is not possible: the exit
        hooks (atexit, the server's shutdown, the orphan sweep at the next start) still cover the normal ways of quitting."""
        if not IS_WINDOWS or self._job_failed:
            return None
        job = None
        try:
            job = _JobObject()
            if job.assign(proc.pid):
                return job
            log.warning("could not add llama-server to a job object (error %s); relying on the exit hooks", ctypes.get_last_error())
        except OSError as e:
            self._job_failed = True
            log.warning("no job object for llama-server (%s); relying on the exit hooks", e)
        if job is not None:
            job.close()
        return None

    def _open_log(self, engine: Engine, args: list[str], key: str) -> int:
        """Start of this run in the log file (so a failure is explained from its own lines, not an earlier run's)."""
        p = paths.logs_dir() / "llama-server.log"
        try:
            if p.exists() and p.stat().st_size > 4_000_000:
                os.replace(p, p.with_name("llama-server.log.1"))
        except OSError:
            pass
        shown = " ".join(a if a != key else "<key>" for a in engine.command(*args))
        with open(p, "ab") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {shown}\n".encode("utf-8", "replace"))
        self._log_path = str(p)
        return p.stat().st_size

    def _log_tail(self, start: int, limit: int = 12000) -> str:
        try:
            with open(self._log_path, "rb") as f:
                f.seek(max(start, os.path.getsize(self._log_path) - limit))
                return _ANSI.sub("", f.read().decode("utf-8", "replace"))
        except OSError:
            return ""

    def _remember_process(self, proc: subprocess.Popen) -> None:
        """Write the small file that lets a later run find this process again if the app dies without cleaning up."""
        ident = _proc_identity(proc.pid) or ("", 0)
        owner = _proc_identity(os.getpid()) or ("", 0)
        try:
            f = paths.engine_state_dir() / f"{os.getpid()}.json"
            f.write_text(json.dumps({"pid": proc.pid, "image": ident[0], "created": ident[1], "owner_pid": os.getpid(),
                                     "owner_created": owner[1]}), "utf-8")
            self._state_file = f
        except OSError:
            log.debug("could not write the engine state file", exc_info=True)

    def _forget_state_file(self) -> None:
        f, self._state_file = self._state_file, None
        if f is not None:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    def _stop_process(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
            job, self._job = self._job, None
            self._handle, self._spec = None, None
        if proc is not None:
            self._terminate(proc, job)
        self._forget_state_file()

    @staticmethod
    def _terminate(proc: subprocess.Popen, job: "_JobObject | None" = None) -> None:
        """End the process, and with the job object everything it started."""
        if job is not None:
            job.terminate()
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(5)
                except (OSError, subprocess.TimeoutExpired):
                    log.warning("llama-server %s did not end", proc.pid)
        if job is not None:
            job.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_n_ctx(port: int, key: str) -> int | None:
    """The context window the engine really has (GET /props)."""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/props", headers={"Authorization": f"Bearer {key}"}, timeout=10, trust_env=False)
        if r.status_code != 200:
            return None
        j = r.json()
        n = (j.get("default_generation_settings") or {}).get("n_ctx") or j.get("n_ctx")
        return int(n) if n else None
    except (httpx.HTTPError, ValueError, TypeError):
        return None


_GPU_DEVICE_RES = (
    re.compile(r"using device \w+ \((.+?)\)\s*(?:\(|-|$)", re.M),                        # "using device Vulkan0 (Intel(R) Arc(TM) 140V GPU (16GB)) (unknown id) - 27247 MiB free"
    re.compile(r"-\s+(?:Vulkan|CUDA|HIP|SYCL|Metal|OpenCL)\w*\s*:\s*(.+?)\s*\(\d+ MiB"),    # "common_param:   - Vulkan0 : Intel(R) Arc(TM) 140V GPU (16GB) (28016 MiB, 27248 MiB free)"
    re.compile(r"ggml_vulkan:\s*\d+\s*=\s*(.+?)\s*\("),                                  # older builds: "ggml_vulkan: 0 = NVIDIA GeForce RTX 3060 (NVIDIA) | ..."
)


def parse_gpu(log_text: str) -> dict:
    """What the engine said about the graphics card while loading: device name, layers offloaded. ("active" is true when at least
    one layer went to the card.) Empty when the log says nothing about a card."""
    out: dict = {}
    m = re.search(r"offloaded (\d+)/(\d+) layers to GPU", log_text)
    if m:
        out["layers"], out["layers_total"] = int(m.group(1)), int(m.group(2))
    for rx in _GPU_DEVICE_RES:
        m = rx.search(log_text)
        if m:
            out["device"] = m.group(1).strip()
            break
    out["active"] = bool(out.get("layers"))
    return out


# ------------------------------------------------------------------------------------------------------ a question that was stopped
class RequestScope:
    """The connections to the engine that belong to one question asked through the web server. When the browser closes the answer
    stream (the Stop button, a closed window) the server cancels the scope (local_api.py), which closes those connections:
    llama.cpp then stops generating at once. Without it an answer nobody reads would be generated to the end, because the web
    framework only closes the stream's generator whenever its garbage collector gets round to it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list = []
        self.cancelled = False

    def add(self, client) -> None:
        with self._lock:
            if not self.cancelled:
                self._clients.append(client)
                return
        client.close()                                   # the question was stopped before this connection was made

    def discard(self, client) -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            clients, self._clients = self._clients, []
        for c in clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001 - a connection that is already gone is what we want
                pass


current_scope: contextvars.ContextVar["RequestScope | None"] = contextvars.ContextVar("perplexify_request_scope", default=None)


_manager = EngineManager()


def manager() -> EngineManager:
    return _manager


def ensure_engine(spec: EngineSpec) -> EngineHandle:
    return _manager.ensure(spec)


def stop_engine() -> None:
    _manager.stop()


def shutdown() -> None:
    """The app is quitting (window closed, /api/shutdown, Ctrl+C): the engine goes with it."""
    _manager.stop()


atexit.register(shutdown)
