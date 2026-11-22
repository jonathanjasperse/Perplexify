"""Indexing pipeline: files -> passages (parsed in parallel) -> SQLite/FTS -> embeddings.

Every step is incremental and resumable: unchanged files are skipped by content hash, and embedding
only works on passages that do not have a vector yet, so an interrupted run simply continues.
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from .candidate import Candidate
from .chunking import embedding_text
from .embeddings import get_embedder
from .jobs import Job
from .library import Library
from .loaders import supported_extensions
from .parse_worker import parse_file
from .repo import RepoOptions, RepoScan, safe_extract_zip, scan_repo
from .store import ChunkIn
from .textutil import sha1_file

log = logging.getLogger("perplexify.ingest")

SKIP_DIRS = {".git", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}


@dataclass
class IngestStats:
    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    duplicates: int = 0
    empty: int = 0
    failed: int = 0
    chunks: int = 0
    redactions: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.indexed} indexed ({self.chunks} passages)"]
        for label, n in (("unchanged", self.unchanged), ("duplicate", self.duplicates), ("empty", self.empty),
                         ("failed", self.failed)):
            if n:
                parts.append(f"{n} {label}")
        if self.redactions:
            parts.append(f"{self.redactions} possible secrets redacted")
        return ", ".join(parts)


def compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    out = []
    for p in patterns:
        if p and p.strip():
            try:
                out.append(re.compile(p, re.I))
            except re.error:
                log.warning("ignoring invalid pattern %r", p)
    return out


# --------------------------------------------------------------------------- candidate discovery
def iter_folder(root: Path, exts: set[str], skip: list[re.Pattern]) -> Iterator[Candidate]:
    root = root.resolve()
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            p = Path(dirpath) / name
            if p.suffix.lower() not in exts:
                continue
            rel = p.relative_to(root).as_posix()
            if any(s.search(rel) for s in skip):
                continue
            yield Candidate(p, p.as_uri(), "external", str(p))


def mirror_candidates(lib: Library, skip: list[re.Pattern]) -> list[Candidate]:
    exts = supported_extensions()
    out: list[Candidate] = []
    for row in lib.store.crawl_rows():
        rel = row["relpath"]
        if not rel or Path(rel).suffix.lower() not in exts:
            continue
        if any(s.search(row["url"]) for s in skip):
            continue
        p = lib.source_dir / rel
        if p.is_file():
            out.append(Candidate(p, row["url"], "mirror", rel))
    return out


# --------------------------------------------------------------------------- parsing (parallel)
def _parse_parallel(items: list, workers: int, job: Job) -> Iterator[tuple]:
    """Yield (item, parse_result) as results arrive. Falls back to in-process parsing if the
    worker pool cannot start or breaks."""
    def run(it) -> dict:
        return parse_file(str(it[0].path), it[0].rel)

    if workers <= 1 or len(items) < 8:
        for it in items:
            job.check()
            yield it, run(it)
        return
    finished: set[int] = set()
    try:
        pool = ProcessPoolExecutor(max_workers=workers)
    except (OSError, ValueError, NotImplementedError):
        pool = None
    if pool is not None:
        pending: dict = {}
        nxt = 0
        try:
            while nxt < len(items) or pending:
                while nxt < len(items) and len(pending) < workers * 4:
                    pending[pool.submit(parse_file, str(items[nxt][0].path), items[nxt][0].rel)] = nxt
                    nxt += 1
                job.check()
                done, _ = wait(list(pending), timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    idx = pending.pop(fut)
                    try:
                        res = fut.result()
                    except BrokenProcessPool:
                        raise
                    except Exception as e:  # noqa: BLE001
                        res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    finished.add(idx)
                    yield items[idx], res
            pool.shutdown(wait=True)
            return
        except BrokenProcessPool:
            log.warning("parser worker pool failed; continuing in-process")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    for idx, it in enumerate(items):
        if idx not in finished:
            job.check()
            yield it, run(it)


# --------------------------------------------------------------------------- indexing
def index_candidates(lib: Library, candidates: list[Candidate], job: Job) -> IngestStats:
    store = lib.store
    stats = IngestStats(seen=len(candidates))
    todo: list[tuple[Candidate, str, int]] = []
    job.update(phase="Scanning", current=0, total=len(candidates), message="Looking for new and changed files")
    for i, cand in enumerate(candidates):
        job.check()
        try:
            h = sha1_file(cand.path)
            size = cand.path.stat().st_size
        except OSError as e:
            stats.failed += 1
            stats.errors.append(f"{cand.stored_path}: {e}")
            continue
        row = store.doc_by_uri(cand.uri)
        if row is not None and row["content_hash"] == h:
            stats.unchanged += 1
        else:
            todo.append((cand, h, size))
        if i % 250 == 0:
            job.update(current=i)
    if not todo:
        return stats

    job.update(phase="Reading", current=0, total=len(todo), message="")
    workers = max(1, min(8, (os.cpu_count() or 4) - 2))
    done = 0
    for item, res in _parse_parallel(todo, workers, job):
        job.check()
        cand, h, size = item
        done += 1
        if not res["ok"]:
            stats.failed += 1
            if len(stats.errors) < 50:
                stats.errors.append(f"{cand.stored_path}: {res['error']}")
        else:
            rows = res["chunks"]
            title = res["title"] or cand.rel or cand.path.stem
            lang = res.get("lang", "")
            stats.redactions += res.get("redactions", 0)
            if not rows:
                with store.tx():
                    store.replace_document(cand.uri, title, cand.stored_path, cand.origin, res["kind"], h, "", size, [], lang)
                stats.empty += 1
            else:
                dup = store.doc_by_text_hash(res["text_hash"])
                if dup is not None and dup["uri"] != cand.uri:
                    with store.tx():  # remember it so it is not parsed again next time
                        store.replace_document(cand.uri, title, cand.stored_path, cand.origin, res["kind"], h, "", size, [], lang)
                    stats.duplicates += 1
                else:
                    with store.tx():
                        store.replace_document(cand.uri, title, cand.stored_path, cand.origin, res["kind"], h,
                                               res["text_hash"], size, [ChunkIn(*c) for c in rows], lang)
                    stats.indexed += 1
                    stats.chunks += len(rows)
        if done % 20 == 0 or done == len(todo):
            job.update(current=done, message=cand.stored_path)
    return stats


def _prune(lib: Library, keep: set[str], uri_prefix: str) -> int:
    """Remove documents under `uri_prefix` that are no longer present (deleted upstream or excluded now)."""
    store = lib.store
    removed = 0
    for row in list(store.iter_documents()):
        if row["uri"].startswith(uri_prefix) and row["uri"] not in keep:
            with store.tx():
                store.delete_document(row["id"])
            removed += 1
    return removed


def index_folder(lib: Library, root: Path, job: Job, *, skip_patterns: list[str] | None = None,
                 prune: bool = True) -> IngestStats:
    """Index the documents (HTML, PDF, Markdown, text) in a folder."""
    root = root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"Folder not found: {root}")
    job.update(phase="Scanning", message=f"Listing files in {root}")
    cands = list(iter_folder(root, supported_extensions(), compile_patterns(skip_patterns or [])))
    stats = index_candidates(lib, cands, job)
    if prune:
        _prune(lib, {c.uri for c in cands}, root.as_uri())
    return stats


def index_repo(lib: Library, root: Path, job: Job, opts: RepoOptions, *, uri_for: Callable[[str], str], origin: str,
               stored_for: Callable[[str], str], uri_prefix: str) -> tuple[IngestStats, RepoScan]:
    """Index a code repository: source files (split along functions and classes), docs and config."""
    job.update(phase="Scanning", current=0, total=0, message="Listing source files")
    scan = scan_repo(root, opts, uri_for=uri_for, origin=origin, stored_for=stored_for)
    if scan.secret_files:
        job.log(f"Skipped {len(scan.secret_files)} file(s) that are meant to hold secrets, e.g. {scan.secret_files[0]}")
    if scan.too_big:
        job.log(f"Skipped {scan.too_big} file(s) over the size limit.")
    if scan.truncated:
        job.log(f"Stopped listing at {opts.max_files} files. Narrow the include / exclude patterns for the rest.")
    langs = ", ".join(f"{k} {v}" for k, v in scan.languages.most_common(6))
    job.log(f"{len(scan.candidates)} files to index ({langs}).")
    stats = index_candidates(lib, scan.candidates, job)
    if not scan.truncated:
        removed = _prune(lib, {c.uri for c in scan.candidates}, uri_prefix)
        if removed:
            job.log(f"Removed {removed} file(s) that no longer exist or are now excluded.")
    return stats, scan


def index_mirror(lib: Library, job: Job, skip_patterns: list[str] | None = None) -> IngestStats:
    job.update(phase="Scanning", message="Listing downloaded pages")
    return index_candidates(lib, mirror_candidates(lib, compile_patterns(skip_patterns or [])), job)


def index_uploads(lib: Library, job: Job) -> IngestStats:
    """Index everything in the library's uploads folder (extracting zip files first)."""
    up = lib.source_dir / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    for z in sorted(up.glob("*.zip")):
        job.check()
        target = up / z.stem
        if not target.exists():
            job.update(phase="Extracting", message=z.name)
            safe_extract_zip(z, target)
    exts = supported_extensions()
    cands = []
    for p in sorted(up.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            rel = p.relative_to(lib.source_dir).as_posix()
            cands.append(Candidate(p, f"upload://{p.relative_to(up).as_posix()}", "mirror", rel))
    return index_candidates(lib, cands, job)


# --------------------------------------------------------------------------- embeddings
def embed_pending(lib: Library, settings, job: Job) -> int:
    """Create vectors for every passage that does not have one yet. Safe to interrupt and re-run.

    Cloud embedders take several batches at once (their latency is network time); the built-in model
    already uses all CPU cores, so it runs one batch at a time."""
    store = lib.store
    total = store.pending_count()
    if total == 0:
        return 0
    job.update(phase="Embedding", current=0, total=total, message="Loading the embedding model")
    emb = get_embedder(lib.embedder_spec, settings)
    job.update(message="")
    batch = max(1, int(emb.preferred_batch))
    workers = max(1, int(emb.concurrency))
    done = 0

    def embed(part: list) -> tuple[list[int], object]:
        return [r["id"] for r in part], emb.embed_documents([embedding_text(r["heading"], r["text"]) for r in part])

    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    try:
        while True:
            job.check()
            rows = store.pending_chunks(max(256, batch * workers * 2))
            if not rows:
                break
            rows.sort(key=lambda r: len(r["text"]))  # similar lengths per batch = less padding = faster
            parts = [rows[i:i + batch] for i in range(0, len(rows), batch)]
            results = pool.map(embed, parts) if pool else map(embed, parts)
            for ids, vecs in results:
                job.check()
                store.set_vectors(ids, vecs)
                done += len(ids)
                job.update(current=done)
    finally:
        if pool:
            pool.shutdown(wait=False, cancel_futures=True)
    if emb.dim and lib.meta["embedder"].get("dim") != emb.dim:
        lib.meta["embedder"]["dim"] = emb.dim
        lib.save_meta()
    return done


def reembed(lib: Library, settings, new_spec: dict, job: Job) -> int:
    """Rebuild all vectors with a different embedding model (passages are kept; nothing is re-downloaded)."""
    job.update(phase="Embedding", message="Loading the embedding model")
    get_embedder(new_spec, settings)  # fail early, before the old vectors are discarded
    lib.set_embedder(new_spec)
    lib.store.clear_vectors()
    return embed_pending(lib, settings, job)
