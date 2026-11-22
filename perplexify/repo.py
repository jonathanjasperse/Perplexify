"""Finding the files of a code repository worth indexing.

Respects .gitignore (including nested ones), skips vendored / generated / build directories, lock files,
minified bundles, oversized files, and files that are meant to hold secrets.
"""
from __future__ import annotations

import os
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pathspec

from .candidate import Candidate
from .loaders import TEXT_EXT
from .loaders.code import detect_language
from .secretscan import is_secret_file

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "third_party", "thirdparty", "third-party", "external",
    "extern", "deps", "bower_components", "Pods", "Carthage", "dist", "build", "out", "target", "obj",
    "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache", ".pytest_cache", ".gradle", ".idea", ".vscode",
    ".vs", "DerivedData", ".next", ".nuxt", ".cache", "coverage", "site-packages", ".terraform",
}
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "cargo.lock", "poetry.lock", "pipfile.lock",
              "composer.lock", "gemfile.lock", "go.sum", "bun.lockb", "packages.lock.json", "uv.lock", "flake.lock"}
# data-like formats get a stricter size cap: big ones are almost always generated or fixtures
DATA_LIMITS = {".json": 200_000, ".xml": 300_000, ".yaml": 300_000, ".yml": 300_000, ".sql": 400_000,
               ".toml": 200_000, ".ini": 100_000, ".proto": 400_000, ".graphql": 400_000, ".css": 200_000,
               ".scss": 200_000, ".txt": 300_000}


@dataclass
class RepoOptions:
    include: list[str] = field(default_factory=list)   # gitignore-style patterns: only matching files are indexed
    exclude: list[str] = field(default_factory=list)   # gitignore-style patterns: matching files are skipped
    use_gitignore: bool = True
    include_docs: bool = True                          # README / docs written in Markdown, reStructuredText, text
    max_file_bytes: int = 600_000
    max_files: int = 60_000

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class RepoScan:
    candidates: list[Candidate] = field(default_factory=list)
    secret_files: list[str] = field(default_factory=list)
    too_big: int = 0
    ignored: int = 0
    truncated: bool = False
    languages: Counter = field(default_factory=Counter)


class _Stop(Exception):
    pass


def _spec(lines: list[str]) -> pathspec.PathSpec:
    try:  # pathspec 1.x renamed the pattern factory; 0.12 only knows the old name
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except (LookupError, ValueError):
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def _ignored(specs: list[tuple[pathspec.PathSpec, str]], rel: str) -> bool:
    for spec, base in specs:
        if rel.startswith(base) and spec.match_file(rel[len(base):]):
            return True
    return False


def scan_repo(root: Path, opts: RepoOptions, *, uri_for: Callable[[str], str], origin: str,
              stored_for: Callable[[str], str]) -> RepoScan:
    root = root.resolve()
    include = _spec(opts.include) if opts.include else None
    exclude = _spec(opts.exclude) if opts.exclude else None
    scan = RepoScan()

    def walk(directory: Path, rel_dir: str, specs: list[tuple[pathspec.PathSpec, str]]) -> None:
        local = specs
        if opts.use_gitignore:
            gi = directory / ".gitignore"
            if gi.is_file():
                try:
                    local = specs + [(_spec(gi.read_text("utf-8", errors="replace").splitlines()), rel_dir)]
                except OSError:
                    pass
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
        except OSError:
            return
        for e in entries:
            if e.is_symlink():
                continue
            rel = rel_dir + e.name
            if e.is_dir(follow_symlinks=False):
                if e.name in SKIP_DIRS or e.name.startswith("cmake-build-") or e.name.endswith(".egg-info"):
                    continue
                if _ignored(local, rel + "/") or (exclude and exclude.match_file(rel + "/")):
                    scan.ignored += 1
                    continue
                walk(Path(e.path), rel + "/", local)
                continue
            low = e.name.lower()
            ext = Path(e.name).suffix.lower()
            if low in LOCK_FILES or low.endswith((".min.js", ".min.css", ".map", ".lock")):
                continue
            if is_secret_file(e.name):
                scan.secret_files.append(rel)
                continue
            lang = detect_language(Path(e.name))
            is_doc = ext in TEXT_EXT and not (lang and low in ("cmakelists.txt",))
            if lang is None and not is_doc:
                continue
            if is_doc and not opts.include_docs:
                continue
            try:
                size = e.stat().st_size
            except OSError:
                continue
            if size == 0:
                continue
            if size > DATA_LIMITS.get(ext, opts.max_file_bytes) or size > opts.max_file_bytes:
                scan.too_big += 1
                continue
            if include and not include.match_file(rel):
                continue
            if (exclude and exclude.match_file(rel)) or _ignored(local, rel):
                scan.ignored += 1
                continue
            scan.candidates.append(Candidate(Path(e.path), uri_for(rel), origin, stored_for(rel), rel))
            scan.languages[lang or "docs"] += 1
            if len(scan.candidates) >= opts.max_files:
                scan.truncated = True
                raise _Stop()

    try:
        walk(root, "", [])
    except _Stop:
        pass
    return scan


def safe_extract_zip(zip_path: Path, dest: Path, max_bytes: int = 4 << 30, max_files: int = 300_000) -> int:
    """Extract a zip without trusting it: no path escapes, bounded size."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    total = count = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            target = (dest / info.filename).resolve()
            if dest != target and dest not in target.parents:
                continue  # zip-slip attempt
            total += info.file_size
            count += 1
            if total > max_bytes or count > max_files:
                raise RuntimeError("The archive is too large to import.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
    return count
