"""Libraries: one searchable manual (or a collection of manuals) with its own index and source files."""
from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path

from . import paths
from .settings import DEFAULT_EMBEDDER
from .store import Store
from .textutil import slugify


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._store: Store | None = None
        self._lock = threading.Lock()
        self._size_cache: tuple[float, int] = (0.0, 0)
        self.meta: dict = {}
        self.load_meta()

    # ------------------------------------------------------------------ metadata
    def load_meta(self) -> None:
        try:
            self.meta = json.loads((self.root / "library.json").read_text("utf-8"))
        except (OSError, ValueError):
            self.meta = {}
        self.meta.setdefault("name", self.root.name)
        self.meta.setdefault("description", "")
        self.meta.setdefault("created", time.time())
        self.meta.setdefault("embedder", dict(DEFAULT_EMBEDDER))
        self.meta.setdefault("sources", [])

    def save_meta(self) -> None:
        tmp = self.root / "library.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2), "utf-8")
        os.replace(tmp, self.root / "library.json")

    @property
    def id(self) -> str:
        return self.root.name

    @property
    def name(self) -> str:
        return self.meta["name"]

    @property
    def embedder_spec(self) -> dict:
        return dict(self.meta["embedder"])

    def set_embedder(self, spec: dict) -> None:
        self.meta["embedder"] = spec
        self.save_meta()

    def add_source(self, src: dict) -> None:
        key = (src.get("type"), src.get("url") or src.get("path"))
        for i, old in enumerate(self.meta["sources"]):
            if (old.get("type"), old.get("url") or old.get("path")) == key:
                self.meta["sources"][i] = {**old, **src}
                break
        else:
            self.meta["sources"].append(src)
        self.save_meta()

    # ------------------------------------------------------------------ storage
    @property
    def store(self) -> Store:
        with self._lock:
            if self._store is None:
                self._store = Store(self.root / "index.db")
            return self._store

    @property
    def source_dir(self) -> Path:
        p = self.root / "source"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def close(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None

    def source_bytes(self, max_age: float = 30.0) -> int:
        ts, size = self._size_cache
        if time.time() - ts < max_age:
            return size
        total = 0
        for dirpath, _dirs, files in os.walk(self.source_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        self._size_cache = (time.time(), total)
        return total

    def summary(self, deep: bool = False) -> dict:
        s = self.store.stats()
        out = {"id": self.id, "name": self.name, "description": self.meta["description"],
               "created": self.meta["created"], "embedder": self.meta["embedder"],
               "sources": self.meta["sources"], **s}
        if deep:
            out["source_bytes"] = self.source_bytes()
        return out


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):  # read-only files (common in mirrored sites) on Windows
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    for attempt in range(4):
        shutil.rmtree(path, onerror=onerror)
        if not path.exists():
            return
        time.sleep(0.4 * (attempt + 1))


class LibraryManager:
    def __init__(self) -> None:
        self._libs: dict[str, Library] = {}
        self._lock = threading.RLock()
        self.reload()

    def reload(self) -> None:
        with self._lock:
            found: dict[str, Library] = {}
            for d in sorted(paths.libraries_dir().iterdir()):
                if d.is_dir() and (d / "library.json").exists():
                    found[d.name] = self._libs.get(d.name) or Library(d)
            self._libs = found

    def list(self) -> list[Library]:
        with self._lock:
            return sorted(self._libs.values(), key=lambda lib: lib.name.lower())

    def get(self, lib_id: str) -> Library | None:
        with self._lock:
            return self._libs.get(lib_id)

    def create(self, name: str, description: str = "", embedder: dict | None = None) -> Library:
        with self._lock:
            base = slugify(name)
            lib_id, n = base, 2
            while (paths.libraries_dir() / lib_id).exists():
                lib_id = f"{base}-{n}"
                n += 1
            root = paths.libraries_dir() / lib_id
            root.mkdir(parents=True)
            (root / "library.json").write_text(json.dumps({
                "name": name.strip() or lib_id, "description": description.strip(), "created": time.time(),
                "embedder": embedder or dict(DEFAULT_EMBEDDER), "sources": []}, indent=2), "utf-8")
            lib = Library(root)
            self._libs[lib_id] = lib
            return lib

    def delete(self, lib_id: str) -> None:
        with self._lock:
            lib = self._libs.pop(lib_id, None)
            if lib is None:
                return
            lib.close()
            _rmtree(lib.root)
