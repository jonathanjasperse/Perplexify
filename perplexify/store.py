"""SQLite store for one library: documents, passages, the full-text index and the vectors.

Keyword search uses SQLite FTS5 (BM25). Vectors are float32 blobs kept next to each passage and
searched with a brute-force dot product in numpy, which is plenty fast for the hundreds of
thousands of passages a manual produces and avoids any vector-database dependency.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import numpy as np

from .textutil import CHARS_PER_TOKEN

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    uri TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    path TEXT,
    origin TEXT NOT NULL DEFAULT 'external',   -- 'mirror' (relative to the library's source folder) or 'external'
    kind TEXT NOT NULL DEFAULT 'text',
    lang TEXT NOT NULL DEFAULT '',
    content_hash TEXT,
    text_hash TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    n_chunks INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_documents_text_hash ON documents(text_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    anchor TEXT,
    text TEXT NOT NULL,
    terms TEXT NOT NULL DEFAULT '',
    n_chars INTEGER NOT NULL DEFAULT 0,
    vector BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_chunks_pending ON chunks(id) WHERE vector IS NULL;
CREATE INDEX IF NOT EXISTS idx_chunks_vec ON chunks(id) WHERE vector IS NOT NULL;

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    heading, text, terms,
    content='chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, heading, text, terms) VALUES (new.id, new.heading, new.text, new.terms);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, heading, text, terms) VALUES ('delete', old.id, old.heading, old.text, old.terms);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE OF heading, text, terms ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, heading, text, terms) VALUES ('delete', old.id, old.heading, old.text, old.terms);
    INSERT INTO chunks_fts(rowid, heading, text, terms) VALUES (new.id, new.heading, new.text, new.terms);
END;

CREATE TABLE IF NOT EXISTS crawl_pages (
    url TEXT PRIMARY KEY,
    relpath TEXT,
    status INTEGER,
    content_type TEXT,
    etag TEXT,
    last_modified TEXT,
    fetched_at REAL,
    size INTEGER
);
"""


@dataclass
class ChunkIn:
    heading: str
    anchor: str | None
    text: str
    terms: str


@dataclass
class DocFilter:
    """Restricts a search to some documents. Empty means no restriction."""
    kinds: tuple[str, ...] = ()
    langs: tuple[str, ...] = ()
    path_contains: str = ""

    @property
    def empty(self) -> bool:
        return not (self.kinds or self.langs or self.path_contains.strip())

    def sql(self) -> tuple[str, list]:
        """WHERE fragments over a `documents` table aliased d."""
        parts: list[str] = []
        params: list = []
        if self.kinds:
            parts.append(f"d.kind IN ({','.join('?' * len(self.kinds))})")
            params += list(self.kinds)
        if self.langs:
            parts.append(f"d.lang IN ({','.join('?' * len(self.langs))})")
            params += list(self.langs)
        if self.path_contains.strip():
            like = "%" + self.path_contains.strip().replace("%", r"\%").replace("_", r"\_") + "%"
            parts.append("(d.title LIKE ? ESCAPE '\\' OR d.uri LIKE ? ESCAPE '\\')")
            params += [like, like]
        return " AND ".join(parts), params


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        self._vec_lock = threading.Lock()
        self._vec_cache: tuple[tuple[int, int], np.ndarray, np.ndarray, np.ndarray] | None = None
        self._init_schema()

    # ------------------------------------------------------------------ connections
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=60, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA temp_store=MEMORY")
            c.execute("PRAGMA cache_size=-65536")
            self._local.conn = c
            with self._all_lock:
                self._all.append(c)
        return c

    def close(self) -> None:
        with self._all_lock:
            conns, self._all = self._all, []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()
        self._vec_cache = None

    def _init_schema(self) -> None:
        c = self._conn()
        try:
            c.executescript(SCHEMA)
        except sqlite3.OperationalError as e:
            if "fts5" in str(e).lower():
                raise RuntimeError("This Python's SQLite was built without FTS5 full-text search.") from e
            raise
        cols = {r[1] for r in c.execute("PRAGMA table_info(documents)")}
        if "lang" not in cols:  # indexes created before code repositories were supported
            c.execute("ALTER TABLE documents ADD COLUMN lang TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def tx(self):
        """One write transaction. Not re-entrant."""
        c = self._conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn().execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))

    # ------------------------------------------------------------------ documents
    def doc_by_uri(self, uri: str) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM documents WHERE uri=?", (uri,)).fetchone()

    def doc_by_id(self, doc_id: int) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

    def doc_by_text_hash(self, text_hash: str) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM documents WHERE text_hash=? LIMIT 1", (text_hash,)).fetchone()

    def replace_document(self, uri: str, title: str, path: str | None, origin: str, kind: str,
                         content_hash: str, text_hash: str, size: int, chunks: Sequence[ChunkIn],
                         lang: str = "") -> int:
        c = self._conn()
        now = time.time()
        row = c.execute("SELECT id FROM documents WHERE uri=?", (uri,)).fetchone()
        if row:
            doc_id = row["id"]
            c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            c.execute(
                "UPDATE documents SET title=?, path=?, origin=?, kind=?, lang=?, content_hash=?, text_hash=?, size=?, "
                "n_chunks=?, indexed_at=? WHERE id=?",
                (title, path, origin, kind, lang, content_hash, text_hash, size, len(chunks), now, doc_id))
        else:
            cur = c.execute(
                "INSERT INTO documents(uri, title, path, origin, kind, lang, content_hash, text_hash, size, n_chunks, indexed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uri, title, path, origin, kind, lang, content_hash, text_hash, size, len(chunks), now))
            doc_id = int(cur.lastrowid)
        c.executemany(
            "INSERT INTO chunks(doc_id, ordinal, heading, anchor, text, terms, n_chars) VALUES (?,?,?,?,?,?,?)",
            [(doc_id, i, ch.heading, ch.anchor, ch.text, ch.terms, len(ch.text)) for i, ch in enumerate(chunks)])
        return doc_id

    def delete_document(self, doc_id: int) -> None:
        c = self._conn()
        c.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    def list_documents(self, q: str | None = None, offset: int = 0, limit: int = 50) -> tuple[list[sqlite3.Row], int]:
        c = self._conn()
        if q:
            like = f"%{q}%"
            total = c.execute("SELECT COUNT(*) FROM documents WHERE title LIKE ? OR uri LIKE ?", (like, like)).fetchone()[0]
            rows = c.execute("SELECT * FROM documents WHERE title LIKE ? OR uri LIKE ? ORDER BY title COLLATE NOCASE "
                             "LIMIT ? OFFSET ?", (like, like, limit, offset)).fetchall()
        else:
            total = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            rows = c.execute("SELECT * FROM documents ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
                             (limit, offset)).fetchall()
        return rows, total

    def iter_documents(self) -> Iterator[sqlite3.Row]:
        yield from self._conn().execute("SELECT * FROM documents ORDER BY id")

    def doc_chunks(self, doc_id: int) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT id, ordinal, heading, anchor, text FROM chunks WHERE doc_id=? ORDER BY ordinal", (doc_id,)).fetchall()

    # ------------------------------------------------------------------ vectors
    def pending_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM chunks WHERE vector IS NULL").fetchone()[0]

    def pending_chunks(self, limit: int) -> list[sqlite3.Row]:
        return self._conn().execute(
            "SELECT id, heading, text FROM chunks WHERE vector IS NULL ORDER BY id LIMIT ?", (limit,)).fetchall()

    def set_vectors(self, ids: Sequence[int], mat: np.ndarray) -> None:
        mat = np.ascontiguousarray(mat, dtype=np.float32)
        with self.tx() as c:
            c.executemany("UPDATE chunks SET vector=? WHERE id=?", [(mat[i].tobytes(), int(cid)) for i, cid in enumerate(ids)])

    def clear_vectors(self) -> None:
        self._conn().execute("UPDATE chunks SET vector=NULL")
        self._vec_cache = None

    def _vectors(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(chunk ids, their document ids, vector matrix), cached until the set of vectors changes."""
        c = self._conn()
        # answered from the partial index idx_chunks_vec, so this is instant even for 100k+ passages
        r = c.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM chunks WHERE vector IS NOT NULL").fetchone()
        stamp = (int(r[0]), int(r[1]))
        with self._vec_lock:
            if self._vec_cache and self._vec_cache[0] == stamp:
                return self._vec_cache[1], self._vec_cache[2], self._vec_cache[3]
            ids: list[int] = []
            docs: list[int] = []
            rows: list[np.ndarray] = []
            for row in c.execute("SELECT id, doc_id, vector FROM chunks WHERE vector IS NOT NULL ORDER BY id"):
                ids.append(row[0])
                docs.append(row[1])
                rows.append(np.frombuffer(row[2], dtype=np.float32))
            mat = np.vstack(rows) if rows else np.zeros((0, 0), dtype=np.float32)
            id_arr = np.asarray(ids, dtype=np.int64)
            doc_arr = np.asarray(docs, dtype=np.int64)
            self._vec_cache = (stamp, id_arr, doc_arr, mat)
            return id_arr, doc_arr, mat

    def allowed_doc_ids(self, flt: DocFilter | None) -> np.ndarray | None:
        """Ids of the documents a filter admits (None when there is no filter)."""
        if flt is None or flt.empty:
            return None
        where, params = flt.sql()
        rows = self._conn().execute(f"SELECT d.id FROM documents d WHERE {where}", params).fetchall()
        return np.asarray([r[0] for r in rows], dtype=np.int64)

    def vector_search(self, q: np.ndarray, n: int, flt: DocFilter | None = None) -> tuple[list[int], list[float]]:
        ids, doc_ids, mat = self._vectors()
        if mat.size == 0:
            return [], []
        q = np.asarray(q, dtype=np.float32).reshape(-1)
        if mat.shape[1] != q.shape[0]:
            raise RuntimeError("This manual was indexed with a different embedding model. Re-embed it in the library settings.")
        allowed = self.allowed_doc_ids(flt)
        if allowed is not None:
            mask = np.isin(doc_ids, allowed)
            if not mask.any():
                return [], []
            ids, mat = ids[mask], mat[mask]
        sims = mat @ q
        n = min(n, len(sims))
        idx = np.argpartition(-sims, n - 1)[:n] if n < len(sims) else np.arange(len(sims))
        idx = idx[np.argsort(-sims[idx])]
        return ids[idx].tolist(), sims[idx].astype(float).tolist()

    def get_vectors(self, ids: Sequence[int]) -> dict[int, np.ndarray]:
        out: dict[int, np.ndarray] = {}
        c = self._conn()
        ids = list(ids)
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            for row in c.execute(f"SELECT id, vector FROM chunks WHERE vector IS NOT NULL AND id IN ({marks})", part):
                out[row[0]] = np.frombuffer(row[1], dtype=np.float32)
        return out

    # ------------------------------------------------------------------ keyword search
    def bm25(self, fts_query: str, n: int, flt: DocFilter | None = None) -> list[tuple[int, float]]:
        sql = "SELECT chunks_fts.rowid, bm25(chunks_fts, 6.0, 1.0, 0.6) AS s FROM chunks_fts"
        params: list = []
        where = "chunks_fts MATCH ?"
        if flt is not None and not flt.empty:
            extra, fparams = flt.sql()
            sql += " JOIN chunks c ON c.id = chunks_fts.rowid JOIN documents d ON d.id = c.doc_id"
            where += " AND " + extra
            params = [fts_query] + fparams
        else:
            params = [fts_query]
        try:
            rows = self._conn().execute(f"{sql} WHERE {where} ORDER BY s LIMIT ?", params + [n]).fetchall()
        except sqlite3.OperationalError:
            return []  # a malformed query (stray quote or operator) simply matches nothing
        return [(int(r[0]), float(r[1])) for r in rows]

    # ------------------------------------------------------------------ chunk lookup
    def get_chunks(self, ids: Sequence[int]) -> dict[int, sqlite3.Row]:
        out: dict[int, sqlite3.Row] = {}
        c = self._conn()
        ids = list(ids)
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            rows = c.execute(
                "SELECT c.id, c.doc_id, c.ordinal, c.heading, c.anchor, c.text, d.uri, d.title, d.path, d.origin, d.kind, d.lang "
                f"FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id IN ({marks})", part)
            for r in rows:
                out[r["id"]] = r
        return out

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict:
        c = self._conn()
        docs = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks, chars = c.execute("SELECT COUNT(*), COALESCE(SUM(n_chars), 0) FROM chunks").fetchone()
        pending = c.execute("SELECT COUNT(*) FROM chunks WHERE vector IS NULL").fetchone()[0]
        last = c.execute("SELECT COALESCE(MAX(indexed_at), 0) FROM documents").fetchone()[0]
        kinds = {r[0]: r[1] for r in c.execute("SELECT kind, COUNT(*) FROM documents WHERE n_chunks > 0 GROUP BY kind")}
        langs = {r[0]: r[1] for r in c.execute(
            "SELECT lang, COUNT(*) FROM documents WHERE n_chunks > 0 AND lang != '' GROUP BY lang ORDER BY COUNT(*) DESC")}
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {"documents": docs, "chunks": chunks, "pending": pending, "embedded": chunks - pending,
                "tokens": int(chars / CHARS_PER_TOKEN),            # approximate size of everything indexed, in model tokens
                "last_indexed": last, "db_bytes": size, "kinds": kinds, "langs": langs}

    # ------------------------------------------------------------------ crawl state
    def crawl_get(self, url: str) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM crawl_pages WHERE url=?", (url,)).fetchone()

    def crawl_put(self, url: str, relpath: str | None, status: int, content_type: str | None,
                  etag: str | None, last_modified: str | None, size: int) -> None:
        self._conn().execute(
            "INSERT INTO crawl_pages(url, relpath, status, content_type, etag, last_modified, fetched_at, size) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET relpath=excluded.relpath, status=excluded.status, "
            "content_type=excluded.content_type, etag=excluded.etag, last_modified=excluded.last_modified, "
            "fetched_at=excluded.fetched_at, size=excluded.size",
            (url, relpath, status, content_type, etag, last_modified, time.time(), size))

    def crawl_rows(self) -> Iterator[sqlite3.Row]:
        yield from self._conn().execute("SELECT * FROM crawl_pages WHERE status BETWEEN 200 AND 299")

    def crawl_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM crawl_pages WHERE status BETWEEN 200 AND 299").fetchone()[0]
