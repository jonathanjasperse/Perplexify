"""Speech to text (dictation) that runs on this computer: a Whisper model through faster-whisper (CTranslate2).

The browser's own speech recognition is no use inside the app's window: Chromium sends the audio to a Google service
that an embedded WebView2 cannot use, which is why it used to answer "needs an internet connection". Here the page records
the microphone, sends plain 16 kHz mono samples to /api/dictation/transcribe and gets the words back. Nothing leaves the
computer, no key is needed, and it works offline once the model (about 145 MB) is on disk. The installer ships it; from the
source it is downloaded once on first use.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from . import paths

log = logging.getLogger("perplexify.dictation")

HF_REPO = "Systran/faster-whisper-base"      # multilingual, int8-friendly, 145 MB
MODEL_NAME = "whisper-base"                  # its folder inside the models folder
MODEL_MB = 145
SAMPLE_RATE = 16000
MAX_SECONDS = 120
IDLE_UNLOAD_SECONDS = 300                    # the model (about 250 MB in memory) is let go after five idle minutes

_lock = threading.Lock()
_model = None
_last_used = 0.0
_unload_timer: threading.Timer | None = None


class DictationError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


def available() -> bool:
    """True when the speech engine (the faster-whisper package) is installed."""
    import importlib.util

    try:                                          # looked up, not imported: importing loads the whole engine (slow, and memory)
        return importlib.util.find_spec("faster_whisper") is not None and importlib.util.find_spec("ctranslate2") is not None
    except (ImportError, ValueError):             # a broken installation just switches the feature off
        return False


def model_dir() -> Path:
    return paths.models_dir() / MODEL_NAME


def model_ready() -> bool:
    """The model files are on disk (shipped with the installer, or downloaded earlier)."""
    from .models import seed_bundled_models

    try:
        seed_bundled_models()
    except OSError:
        log.debug("could not copy the bundled models", exc_info=True)
    d = model_dir()
    return (d / "model.bin").is_file() and (d / "config.json").is_file()


def download_model(dest: Path | None = None, progress=None) -> Path:
    """Fetch the model from Hugging Face into `dest` (default: the models folder) and return its folder."""
    from huggingface_hub import snapshot_download

    os.environ.pop("HF_HUB_OFFLINE", None)
    root = dest or paths.models_dir()
    root.mkdir(parents=True, exist_ok=True)
    target, tmp = root / MODEL_NAME, root / (MODEL_NAME + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    if progress:
        progress(f"Downloading the speech model (about {MODEL_MB} MB, once)")
    snapshot_download(HF_REPO, local_dir=str(tmp), allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*",
                                                                   "preprocessor_config.json"])
    shutil.rmtree(tmp / ".cache", ignore_errors=True)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    return target


def status() -> dict:
    return {"available": available(), "model_ready": available() and model_ready(), "model_mb": MODEL_MB,
            "max_seconds": MAX_SECONDS, "sample_rate": SAMPLE_RATE}


def _load():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        threads = max(2, (os.cpu_count() or 4) - 2)       # leave some CPU for the window
        started = time.time()
        _model = WhisperModel(str(model_dir()), device="cpu", compute_type="int8", cpu_threads=threads, local_files_only=True)
        log.info("speech model loaded in %.1f s", time.time() - started)
    return _model


def _schedule_unload() -> None:
    global _unload_timer
    if _unload_timer is not None:
        _unload_timer.cancel()
    _unload_timer = threading.Timer(IDLE_UNLOAD_SECONDS, unload)
    _unload_timer.daemon = True
    _unload_timer.start()


def unload() -> None:
    global _model
    with _lock:
        if _model is not None and time.time() - _last_used >= IDLE_UNLOAD_SECONDS - 1:
            _model = None
            log.info("speech model unloaded (idle)")


def samples_from_bytes(data: bytes) -> np.ndarray:
    """Little-endian float32 mono samples at 16 kHz, as the page sends them."""
    if len(data) < 4 or len(data) % 4:
        raise DictationError("The recording was not understood.")
    x = np.frombuffer(data, dtype="<f4")
    if len(x) > MAX_SECONDS * SAMPLE_RATE:
        raise DictationError(f"That recording is longer than {MAX_SECONDS} seconds. Dictate in shorter pieces.")
    if not np.isfinite(x).all():
        raise DictationError("The recording was not understood.")
    return np.clip(x, -1.0, 1.0).astype(np.float32)


def transcribe(samples: np.ndarray, language: str | None = None, hint: str = "") -> str:
    """The words in `samples` (float32, mono, 16 kHz). `language` is a two-letter code or None to detect it; `hint` is a
    sentence of context (names the question is likely to contain)."""
    global _last_used
    if not available():
        raise DictationError("Dictation is not installed in this copy of Perplexify.")
    if len(samples) < SAMPLE_RATE * 0.25:
        return ""
    if not model_ready():
        raise DictationError("The speech model is not on this computer yet.")
    lang = language.lower()[:2] if language and language.strip() else None
    with _lock:                                   # one recording at a time: the model is not shared between threads
        try:
            model = _load()
            if lang and lang not in set(model.supported_languages):
                lang = None                       # a language Whisper does not know: let it detect one
            segments, _info = model.transcribe(
                samples, language=lang, beam_size=1, vad_filter=True, condition_on_previous_text=False,
                initial_prompt=(hint or None))
            text = " ".join(s.text.strip() for s in segments).strip()
        except DictationError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("transcription failed")
            raise DictationError(f"Speech recognition failed: {e}") from e
        _last_used = time.time()
    _schedule_unload()
    return text
