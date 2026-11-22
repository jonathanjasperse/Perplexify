"""The jobs the UI can start. Each takes (context, library, params, job)."""
from __future__ import annotations

import time
from pathlib import Path

from . import github, ingest, osutil
from .crawler import CrawlOptions, Crawler
from .jobs import Job
from .library import Library
from .presets import DOXYGEN_INDEX_SKIP
from .repo import RepoOptions


def _finish(job: Job, stats: ingest.IngestStats, extra: str = "") -> None:
    job.update(message=(extra + " " + stats.summary()).strip())
    for e in stats.errors[:5]:
        job.log(f"skipped: {e}")


# --------------------------------------------------------------------------- websites
def crawl_task(ctx, lib: Library, params: dict, job: Job) -> None:
    opts = CrawlOptions(**{k: v for k, v in params.items() if k in CrawlOptions.__dataclass_fields__})
    result = Crawler(lib, opts, job).run()
    skip = list(params.get("index_skip") or []) + (DOXYGEN_INDEX_SKIP if result.doxygen else [])
    lib.add_source({"type": "crawl", "url": opts.start_url, "doxygen": result.doxygen,
                    "options": {**opts.to_dict(), "refresh": False}, "index_skip": skip, "last_run": time.time()})
    stats = ingest.index_mirror(lib, job, skip)
    _finish(job, stats, f"{result.pages} pages downloaded.")
    ingest.embed_pending(lib, ctx.settings, job)


# --------------------------------------------------------------------------- code repositories
def _repo_options(p: dict) -> RepoOptions:
    return RepoOptions(include=[x for x in (p.get("include") or []) if x.strip()],
                       exclude=[x for x in (p.get("exclude") or []) if x.strip()],
                       use_gitignore=bool(p.get("use_gitignore", True)),
                       include_docs=bool(p.get("include_docs", True)))


def _run_repo(ctx, lib: Library, src: dict, job: Job) -> None:
    """Fetch (if remote) and index one repository source. `src` is the recorded source description."""
    opts = _repo_options(src)
    kind = src["kind"]
    if kind == "folder":
        root = Path(src["path"])
        if not root.is_dir():
            raise RuntimeError(f"Folder not found: {root}")
        uri_prefix = root.resolve().as_uri()
        uri_for = lambda rel: (root.resolve() / rel).as_uri()  # noqa: E731
        stored_for = lambda rel: str(root.resolve() / rel)  # noqa: E731
        origin = "external"
        label = root.name
    else:
        repos = lib.source_dir / "repos"
        if kind == "github":
            ref = github.parse_github(src["url"], src.get("ref"))
            if ref is None:
                raise RuntimeError("That doesn't look like a GitHub repository address.")
            token = osutil.unprotect(src.get("token_enc", ""))
            root = github.download_github(ref, repos, token, job)
            uri_for = ref.blob_url
            uri_prefix = f"{ref.web_url}/blob/"
            label = f"{ref.owner}/{ref.repo}"
        else:  # any other git host
            slug = "git__" + "".join(c if c.isalnum() else "_" for c in src["url"])[-60:]
            root = github.git_clone(src["url"], repos / slug, src.get("ref"), job)
            uri_for = lambda rel: f"repo://{slug}/{rel}"  # noqa: E731
            uri_prefix = f"repo://{slug}/"
            label = src["url"]
        rel_root = root.relative_to(lib.source_dir).as_posix()
        stored_for = lambda rel: f"{rel_root}/{rel}"  # noqa: E731
        origin = "mirror"
    job.log(f"Indexing {label}")
    stats, _scan = ingest.index_repo(lib, root, job, opts, uri_for=uri_for, origin=origin,
                                     stored_for=stored_for, uri_prefix=uri_prefix)
    src["last_run"] = time.time()
    _finish(job, stats)


def repo_task(ctx, lib: Library, params: dict, job: Job) -> None:
    src = {"type": "repo", "kind": params["kind"], "url": params.get("url", ""), "path": params.get("path", ""),
           "ref": params.get("ref", ""), "include": params.get("include") or [], "exclude": params.get("exclude") or [],
           "use_gitignore": bool(params.get("use_gitignore", True)), "include_docs": bool(params.get("include_docs", True))}
    if params.get("token"):
        src["token_enc"] = osutil.protect(params["token"].strip())
    elif params.get("keep_token"):
        old = next((s for s in lib.meta["sources"] if s.get("type") == "repo" and s.get("url") == src["url"]), {})
        if old.get("token_enc"):
            src["token_enc"] = old["token_enc"]
    _run_repo(ctx, lib, src, job)
    lib.add_source(src)
    ingest.embed_pending(lib, ctx.settings, job)


# --------------------------------------------------------------------------- folders of documents, uploads
def folder_task(ctx, lib: Library, params: dict, job: Job) -> None:
    path = Path(params["path"])
    lib.add_source({"type": "folder", "path": str(path), "last_run": time.time()})
    stats = ingest.index_folder(lib, path, job, skip_patterns=params.get("skip") or [])
    _finish(job, stats)
    ingest.embed_pending(lib, ctx.settings, job)


def upload_task(ctx, lib: Library, params: dict, job: Job) -> None:
    lib.add_source({"type": "upload", "path": "uploads", "last_run": time.time()})
    stats = ingest.index_uploads(lib, job)
    _finish(job, stats)
    ingest.embed_pending(lib, ctx.settings, job)


# --------------------------------------------------------------------------- maintenance
def refresh_task(ctx, lib: Library, params: dict, job: Job) -> None:
    """Re-run every recorded source (re-check websites, re-fetch repositories, re-scan folders, new uploads)."""
    sources = list(lib.meta.get("sources") or [])
    if not sources:
        raise RuntimeError("This library has no recorded sources to refresh.")
    for src in sources:
        job.check()
        kind = src.get("type")
        if kind == "crawl":
            opts = CrawlOptions(**{k: v for k, v in (src.get("options") or {}).items()
                                   if k in CrawlOptions.__dataclass_fields__})
            opts.refresh = True
            Crawler(lib, opts, job).run()
            src["last_run"] = time.time()
            ingest.index_mirror(lib, job, src.get("index_skip") or [])
        elif kind == "repo":
            _run_repo(ctx, lib, src, job)
        elif kind == "folder" and Path(src["path"]).is_dir():
            ingest.index_folder(lib, Path(src["path"]), job)
            src["last_run"] = time.time()
        elif kind == "upload":
            ingest.index_uploads(lib, job)
    lib.save_meta()
    ingest.embed_pending(lib, ctx.settings, job)


def resume_task(ctx, lib: Library, params: dict, job: Job) -> None:
    n = ingest.embed_pending(lib, ctx.settings, job)
    job.update(message=f"Embedded {n} passages.")


def reembed_task(ctx, lib: Library, params: dict, job: Job) -> None:
    n = ingest.reembed(lib, ctx.settings, params["embedder"], job)
    job.update(message=f"Re-embedded {n} passages with {params['embedder'].get('model')}.")
