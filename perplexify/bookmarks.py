"""Bookmarked sources, kept in folders like a file system, in `bookmarks.db` inside the data folder.

A bookmark points at a source the app showed: a passage of a document in a library (where it is, plus the title, the
heading and a snippet as they were shown). Folders nest (up to MAX_DEPTH levels), have names the user chooses, and
hold folders and bookmarks; a bookmark is in one folder or loose at the top. Only what is needed to list and reopen a
bookmark is stored: the text itself stays in the library, so deleting a bookmark never touches a library.

Unlike the history, bookmarks are something the user made on purpose: "clear history" never removes them.
"""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import threading
import time

from . import paths

log = logging.getLogger("perplexify.bookmarks")

MAX_DEPTH = 8              # folders inside folders
NAME_LIMIT = 80
TITLE_LIMIT = 200
NOTE_LIMIT = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES folders(id) ON DELETE CASCADE,       -- NULL: at the top
    name TEXT NOT NULL,
    open INTEGER NOT NULL DEFAULT 1,                               -- shown expanded (the arrow points down)
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    folder_id TEXT REFERENCES folders(id) ON DELETE CASCADE,       -- NULL: at the top
    key TEXT NOT NULL UNIQUE,                                      -- library | document | anchor: what "the same source" means
    library_id TEXT NOT NULL,
    library TEXT NOT NULL DEFAULT '',                              -- the library's name when it was saved
    doc_id INTEGER NOT NULL,
    uri TEXT NOT NULL DEFAULT '',                                  -- the document's stable address, to find it again after a re-index
    title TEXT NOT NULL,                                           -- as shown (the user may rename it)
    heading TEXT NOT NULL DEFAULT '',
    anchor TEXT,
    snippet TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_folder ON items(folder_id);
"""


class BookmarkError(Exception):
    """Something the user did cannot be done; `status` is the HTTP status that fits and the message says what to do."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def clean_name(name: str, limit: int = NAME_LIMIT) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", name or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise BookmarkError("Give the folder a name.")
    return text[:limit].strip()


def source_key(library_id: str, doc_id: int, anchor: str | None) -> str:
    return f"{library_id}|{int(doc_id)}|{anchor or ''}"


class BookmarkStore:
    def __init__(self, path=None) -> None:
        self._path = path
        self._conn_: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._last_time = 0.0

    # ------------------------------------------------------------------ plumbing
    def _conn(self) -> sqlite3.Connection:
        if self._conn_ is None:
            path = self._path or (paths.home() / "bookmarks.db")
            c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=15)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.executescript(SCHEMA)
            self._conn_ = c
        return self._conn_

    def close(self) -> None:
        with self._lock:
            if self._conn_ is not None:
                try:
                    self._conn_.close()
                finally:
                    self._conn_ = None

    def _now(self) -> float:
        """The time, but never the same twice, so things made in the same instant keep their order."""
        with self._lock:
            self._last_time = max(time.time(), self._last_time + 1e-6)
            return self._last_time

    @staticmethod
    def _folder(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "parent_id": r["parent_id"], "name": r["name"], "open": bool(r["open"]), "created": r["created"]}

    @staticmethod
    def _item(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "folder_id": r["folder_id"], "key": r["key"], "library_id": r["library_id"], "library": r["library"],
                "doc_id": r["doc_id"], "uri": r["uri"], "title": r["title"], "heading": r["heading"], "anchor": r["anchor"],
                "snippet": r["snippet"], "note": r["note"], "created": r["created"]}

    # ------------------------------------------------------------------ reading
    def tree(self) -> dict:
        """Every folder and every bookmark (the page builds the tree from the parent links)."""
        with self._lock:
            c = self._conn()
            return {"folders": [self._folder(r) for r in c.execute("SELECT * FROM folders ORDER BY created")],
                    "items": [self._item(r) for r in c.execute("SELECT * FROM items ORDER BY created DESC")]}

    def get_folder(self, folder_id: str) -> dict | None:
        with self._lock:
            r = self._conn().execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
        return self._folder(r) if r else None

    def get_item(self, item_id: str) -> dict | None:
        with self._lock:
            r = self._conn().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return self._item(r) if r else None

    def find_item(self, key: str) -> dict | None:
        with self._lock:
            r = self._conn().execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
        return self._item(r) if r else None

    def stats(self) -> dict:
        with self._lock:
            c = self._conn()
            return {"folders": c.execute("SELECT COUNT(*) FROM folders").fetchone()[0],
                    "items": c.execute("SELECT COUNT(*) FROM items").fetchone()[0]}

    # ------------------------------------------------------------------ folder structure helpers
    def _children_map(self) -> dict[str | None, list[str]]:
        kids: dict[str | None, list[str]] = {}
        for r in self._conn().execute("SELECT id, parent_id FROM folders"):
            kids.setdefault(r["parent_id"], []).append(r["id"])
        return kids

    def _depth(self, folder_id: str | None) -> int:
        """How many folders deep `folder_id` is (a folder at the top is 1; the top itself is 0)."""
        depth, seen = 0, set()
        while folder_id is not None:
            if folder_id in seen:
                raise BookmarkError("The folders are tangled up. Delete the folder and make it again.", 500)
            seen.add(folder_id)
            r = self._conn().execute("SELECT parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
            if r is None:
                raise BookmarkError("That folder does not exist any more.", 404)
            depth += 1
            folder_id = r["parent_id"]
        return depth

    def _height(self, folder_id: str, kids: dict[str | None, list[str]]) -> int:
        """Levels in the subtree under and including `folder_id` (a folder with no folders inside is 1)."""
        below = [self._height(k, kids) for k in kids.get(folder_id, [])]
        return 1 + (max(below) if below else 0)

    def _subtree(self, folder_id: str) -> list[str]:
        kids, out, todo = self._children_map(), [], [folder_id]
        while todo:
            f = todo.pop()
            out.append(f)
            todo.extend(kids.get(f, []))
        return out

    def _same_name(self, parent_id: str | None, name: str, ignore: str | None = None) -> bool:
        r = self._conn().execute("SELECT 1 FROM folders WHERE parent_id IS ? AND lower(name)=lower(?) AND id IS NOT ?",
                                 (parent_id, name, ignore)).fetchone()
        return r is not None

    # ------------------------------------------------------------------ folders
    def create_folder(self, name: str, parent_id: str | None = None) -> dict:
        name = clean_name(name)
        with self._lock:
            if parent_id is not None and self._depth(parent_id) >= MAX_DEPTH:
                raise BookmarkError(f"Folders can go {MAX_DEPTH} levels deep.")
            if self._same_name(parent_id, name):
                raise BookmarkError(f'There is already a folder named "{name}" here.', 409)
            fid = secrets.token_hex(6)
            self._conn().execute("INSERT INTO folders(id, parent_id, name, open, created) VALUES (?,?,?,?,?)",
                                 (fid, parent_id, name, 1, self._now()))
            if parent_id is not None:
                self._conn().execute("UPDATE folders SET open=1 WHERE id=?", (parent_id,))   # the new folder should be visible
            return self.get_folder(fid)

    def rename_folder(self, folder_id: str, name: str) -> dict:
        name = clean_name(name)
        with self._lock:
            f = self.get_folder(folder_id)
            if f is None:
                raise BookmarkError("That folder does not exist any more.", 404)
            if self._same_name(f["parent_id"], name, ignore=folder_id):
                raise BookmarkError(f'There is already a folder named "{name}" here.', 409)
            self._conn().execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
            return self.get_folder(folder_id)

    def set_open(self, folder_id: str, is_open: bool) -> bool:
        with self._lock:
            return self._conn().execute("UPDATE folders SET open=? WHERE id=?", (1 if is_open else 0, folder_id)).rowcount > 0

    def set_all_open(self, is_open: bool) -> None:
        with self._lock:
            self._conn().execute("UPDATE folders SET open=?", (1 if is_open else 0,))

    def move_folder(self, folder_id: str, parent_id: str | None) -> dict:
        with self._lock:
            f = self.get_folder(folder_id)
            if f is None:
                raise BookmarkError("That folder does not exist any more.", 404)
            if parent_id is not None and self.get_folder(parent_id) is None:
                raise BookmarkError("The folder to move it into does not exist any more.", 404)
            if parent_id == f["parent_id"]:
                return f
            if parent_id is not None and parent_id in self._subtree(folder_id):
                raise BookmarkError("A folder cannot go inside itself.")
            if self._same_name(parent_id, f["name"], ignore=folder_id):
                raise BookmarkError(f'There is already a folder named "{f["name"]}" there.', 409)
            if self._depth(parent_id) + self._height(folder_id, self._children_map()) > MAX_DEPTH:
                raise BookmarkError(f"Folders can go {MAX_DEPTH} levels deep.")
            self._conn().execute("UPDATE folders SET parent_id=? WHERE id=?", (parent_id, folder_id))
            if parent_id is not None:
                self._conn().execute("UPDATE folders SET open=1 WHERE id=?", (parent_id,))
            return self.get_folder(folder_id)

    def delete_folder(self, folder_id: str) -> dict:
        """Delete a folder with the folders and bookmarks inside it. Returns how many of each went."""
        with self._lock:
            if self.get_folder(folder_id) is None:
                raise BookmarkError("That folder does not exist any more.", 404)
            ids = self._subtree(folder_id)
            marks = ",".join("?" * len(ids))
            n_items = self._conn().execute(f"SELECT COUNT(*) FROM items WHERE folder_id IN ({marks})", ids).fetchone()[0]
            self._conn().execute("DELETE FROM folders WHERE id=?", (folder_id,))      # the rest follow by cascade
            return {"folders": len(ids), "items": n_items}

    # ------------------------------------------------------------------ bookmarks
    def add_item(self, fields: dict, folder_id: str | None = None) -> tuple[dict, bool]:
        """Save a source. Returns (bookmark, created); a source that is already saved is returned as it is (and moved into
        `folder_id` when one is given), because a bookmark is one file in one place."""
        library_id = str(fields["library_id"])
        doc_id = int(fields["doc_id"])
        anchor = fields.get("anchor") or None
        key = source_key(library_id, doc_id, anchor)
        with self._lock:
            if folder_id is not None and self.get_folder(folder_id) is None:
                raise BookmarkError("That folder does not exist any more.", 404)
            existing = self.find_item(key)
            if existing is not None:
                if folder_id is not None and existing["folder_id"] != folder_id:
                    self._conn().execute("UPDATE items SET folder_id=? WHERE id=?", (folder_id, existing["id"]))
                    existing = self.find_item(key)
                return existing, False
            title = re.sub(r"\s+", " ", str(fields.get("title") or "")).strip()[:TITLE_LIMIT] or "Untitled"
            iid = secrets.token_hex(6)
            self._conn().execute(
                "INSERT INTO items(id, folder_id, key, library_id, library, doc_id, uri, title, heading, anchor, snippet, note, created) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid, folder_id, key, library_id, str(fields.get("library") or "")[:120], doc_id, str(fields.get("uri") or ""),
                 title, str(fields.get("heading") or "")[:300], anchor, str(fields.get("snippet") or "")[:600], "", self._now()))
            if folder_id is not None:
                self._conn().execute("UPDATE folders SET open=1 WHERE id=?", (folder_id,))
            return self.get_item(iid), True

    def rename_item(self, item_id: str, title: str) -> dict:
        title = re.sub(r"\s+", " ", title or "").strip()[:TITLE_LIMIT]
        if not title:
            raise BookmarkError("Give the bookmark a name.")
        with self._lock:
            if self._conn().execute("UPDATE items SET title=? WHERE id=?", (title, item_id)).rowcount == 0:
                raise BookmarkError("That bookmark does not exist any more.", 404)
            return self.get_item(item_id)

    def set_note(self, item_id: str, note: str) -> dict:
        with self._lock:
            if self._conn().execute("UPDATE items SET note=? WHERE id=?", ((note or "").strip()[:NOTE_LIMIT], item_id)).rowcount == 0:
                raise BookmarkError("That bookmark does not exist any more.", 404)
            return self.get_item(item_id)

    def move_item(self, item_id: str, folder_id: str | None) -> dict:
        with self._lock:
            if folder_id is not None and self.get_folder(folder_id) is None:
                raise BookmarkError("The folder to move it into does not exist any more.", 404)
            if self._conn().execute("UPDATE items SET folder_id=? WHERE id=?", (folder_id, item_id)).rowcount == 0:
                raise BookmarkError("That bookmark does not exist any more.", 404)
            if folder_id is not None:
                self._conn().execute("UPDATE folders SET open=1 WHERE id=?", (folder_id,))
            return self.get_item(item_id)

    def delete_item(self, item_id: str) -> bool:
        with self._lock:
            return self._conn().execute("DELETE FROM items WHERE id=?", (item_id,)).rowcount > 0

    def relink(self, item_id: str, doc_id: int) -> dict | None:
        """The library was re-indexed and the document has a new number: point the bookmark at it (the address is the same)."""
        with self._lock:
            it = self.get_item(item_id)
            if it is None or it["doc_id"] == doc_id:
                return it
            key = source_key(it["library_id"], doc_id, it["anchor"])
            if self.find_item(key) is not None:           # the same source is already saved under the new number
                return it
            self._conn().execute("UPDATE items SET doc_id=?, key=? WHERE id=?", (doc_id, key, item_id))
            return self.get_item(item_id)
