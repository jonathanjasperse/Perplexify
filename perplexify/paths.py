"""Where things live on disk.

User data (libraries, downloaded manuals, settings, embedding models, local language models) lives under one "home" folder:
    Windows   %LOCALAPPDATA%\\Perplexify
    macOS     ~/Library/Application Support/Perplexify
    (override with the PERPLEXIFY_HOME environment variable; MANUALRAG_HOME, the old name, still works)

Program files (installed or in the repo) are never written to, so the app also works when it is
installed somewhere read-only such as C:\\Program Files or /Applications.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def web_dir() -> Path:
    return package_dir() / "web"


def env(name: str) -> str:
    """An environment setting: PERPLEXIFY_<name>, or MANUALRAG_<name>, the name it had before the app was renamed."""
    return os.environ.get("PERPLEXIFY_" + name) or os.environ.get("MANUALRAG_" + name) or ""


def _default_home() -> tuple[Path, Path]:
    """(where the data lives, where it lived before the app was renamed from Manual RAG to Perplexify)"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Perplexify", base / "ManualRAG"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
        return base / "Perplexify", base / "ManualRAG"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "perplexify", base / "manualrag"


def home(create: bool = True) -> Path:
    """The data folder. Reading code (`perplexify stop`, which the uninstaller runs) passes create=False so that
    merely looking never leaves an empty folder behind on a PC where the app has not been used."""
    override = env("HOME")
    if override:
        p = Path(override).expanduser()
    else:
        p, legacy = _default_home()
        if create and not p.exists() and legacy.is_dir():
            try:
                legacy.rename(p)  # keep the libraries and settings made while the app was still called Manual RAG
            except OSError:
                p = legacy        # in use or not movable: carry on with the old folder rather than lose sight of the data
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def _sub(name: str) -> Path:
    p = home() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def libraries_dir() -> Path:
    return _sub("libraries")


def models_dir() -> Path:
    """Writable cache for embedding / reranker models."""
    return _sub("models")


def logs_dir() -> Path:
    return _sub("logs")


def llm_dir() -> Path:
    """Language models (.gguf files) downloaded inside the app, for local models (llama.cpp). Files the user chooses from
    elsewhere on the disk are used where they are and never copied here."""
    return _sub("llm")


def llama_dir() -> Path:
    """Where `python -m perplexify download-llama` puts the llama.cpp engine when running from source. (The installer ships
    it as the folder `llama` next to the app instead, see localllm.engine_search_dirs.) Not created here."""
    return home() / "llama"


def engine_state_dir() -> Path:
    """One small file per running llama-server (who started it), so that a copy left behind by a crashed app can be found and
    ended the next time the app starts."""
    return _sub("engines")


def settings_path() -> Path:
    return home() / "settings.json"


def run_info_path(create: bool = True) -> Path:
    return home(create) / "run.json"


def bundle_resources() -> Path | None:
    """macOS: the Resources folder of Perplexify.app when the app runs from the bundle (PyInstaller; the executable is
    Perplexify.app/Contents/MacOS/Perplexify). The DMG build puts the embedding models and the llama.cpp engine there.
    None everywhere else (Windows installs use the layout described in bundled_models_dir)."""
    if not getattr(sys, "frozen", False):
        return None
    res = Path(sys.executable).resolve().parent.parent / "Resources"
    return res if res.is_dir() else None


def bundled_models_dir() -> Path | None:
    """Models shipped inside the installer. They are copied into models_dir() on first use."""
    candidates: list[Path] = []
    bundled = env("BUNDLED_MODELS")
    if bundled:
        candidates.append(Path(bundled))
    res = bundle_resources()
    if res is not None:
        candidates.append(res / "models")
    root = package_dir().parent  # <install>\app when installed, the repo root in development
    candidates += [root.parent / "models", root / "build" / "stage" / "models"]
    for c in candidates:
        try:
            if c.is_dir() and any(c.iterdir()):
                return c
        except OSError:
            continue
    return None
