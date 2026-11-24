"""Local model files: seeding the copy shipped in the installer, and build-time downloads."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import paths


def seed_bundled_models() -> int:
    """Copy models that shipped inside the installer into the user's writable model cache.

    Returns the number of model folders copied. Does nothing when there is no bundled copy or the
    folder already exists."""
    src = paths.bundled_models_dir()
    if src is None:
        return 0
    dst_root = paths.models_dir()
    copied = 0
    for d in src.iterdir():
        target = dst_root / d.name
        if not d.is_dir() or target.exists():
            continue
        tmp = dst_root / (d.name + ".partial")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(d, tmp, symlinks=False)
        tmp.rename(target)
        copied += 1
    return copied


def flatten_symlinks(root: Path) -> None:
    """Replace symlinks (Hugging Face cache layout) by real files so the folder can be shipped in an
    installer / app bundle, then drop the now-unused blob store."""
    for p in list(root.rglob("*")):
        if p.is_symlink():
            target = p.resolve()
            p.unlink()
            if target.is_file():
                shutil.copy2(target, p)
    for blobs in list(root.rglob("blobs")):
        if blobs.is_dir():
            shutil.rmtree(blobs, ignore_errors=True)


def download_model(model_name: str, dest: Path, reranker: str | None = None, whisper: bool = False) -> None:
    """Download an embedding model (and optionally a reranker and the speech model) into `dest` (used by the installer builds)."""
    from fastembed import TextEmbedding

    dest.mkdir(parents=True, exist_ok=True)
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # we flatten the links ourselves below
    emb = TextEmbedding(model_name=model_name, cache_dir=str(dest))
    list(emb.embed(["warm-up"]))  # also proves the downloaded files load
    if reranker:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        ce = TextCrossEncoder(model_name=reranker, cache_dir=str(dest))
        list(ce.rerank("warm-up", ["warm-up"]))
    if whisper:
        from . import dictation

        dictation.download_model(dest)
    flatten_symlinks(dest)
