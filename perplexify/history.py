"""Chat and search history, kept on this computer in `history.db` inside the data folder.

A chat is a list of turns (question, answer, the sources the answer cites and the trace of how it was found), so an old
conversation can be opened again exactly as it was and continued. A search is a query typed in the Search view with its
filters. Both feed the suggestions shown while typing (like a search engine's autocomplete, but only from your own
history) and the "related" queries under a search.

The store never talks to the network and never leaves the data folder. It can be switched off (nothing is recorded then)
and cleared from the History view.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Iterable, Iterator

from . import paths
from .textutil import STOPWORDS, split_identifier

log = logging.getLogger("perplexify.history")

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    n_turns INTEGER NOT NULL DEFAULT 0,
    n_sources INTEGER NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT '',
    profile TEXT NOT NULL DEFAULT '{}',        -- {"id", "name", "model"} of the model that answered last
    libraries TEXT NOT NULL DEFAULT '[]'       -- [{"id", "name"}] searched by the last question
);
CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated DESC);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL DEFAULT '',
    record TEXT NOT NULL DEFAULT '{}',          -- JSON: thinking, events (rewrite, expand, sources, context, usage, notices, done), stopped
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_chat ON turns(chat_id, ordinal);

CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    question, answer,
    content='turns', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, question, answer) VALUES (new.id, new.question, new.answer);
END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, question, answer) VALUES ('delete', old.id, old.question, old.answer);
END;
CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE OF question, answer ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, question, answer) VALUES ('delete', old.id, old.question, old.answer);
    INSERT INTO turns_fts(rowid, question, answer) VALUES (new.id, new.question, new.answer);
END;

CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    norm TEXT NOT NULL,                          -- the query, lower-cased with single spaces: what "the same search" means
    query TEXT NOT NULL,                         -- as last typed
    filters TEXT NOT NULL DEFAULT '{}',          -- JSON: mode, scope, match, langs, path
    filters_key TEXT NOT NULL DEFAULT '',
    libraries TEXT NOT NULL DEFAULT '[]',        -- [{"id", "name"}]
    results INTEGER NOT NULL DEFAULT 0,
    first_run REAL NOT NULL,
    last_run REAL NOT NULL,
    runs INTEGER NOT NULL DEFAULT 1,
    UNIQUE (norm, filters_key)
);
CREATE INDEX IF NOT EXISTS idx_searches_last ON searches(last_run DESC);
"""

_ID = re.compile(r"^[0-9a-f]{12}$")
_WORDS = re.compile(r"[a-z0-9]+")
_SCAN = 4000          # how many recent entries the suggestions look through


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def one_line(text: str, limit: int) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


_FENCE = re.compile(r"```.*?(```|$)", re.S)


def plain_preview(markdown: str, limit: int = 160) -> str:
    """The start of an answer as plain text for a list: no code blocks, headings, emphasis marks or backticks."""
    text = _FENCE.sub(" ", markdown or "")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)               # heading marks
    text = re.sub(r"[`*_>|]+", "", text)                                      # emphasis, quotes, inline code, table bars
    text = re.sub(r"\s*\[\d+\]", "", text)                                    # citation markers like [1]
    return one_line(text, limit)


def _stem(word: str) -> str:
    """A light stem so 'searching', 'searches' and 'search' count as the same word when finding related queries."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def content_words(text: str) -> set[str]:
    """The meaningful words of a query: no stop words, identifiers split (getNextAudioBlock -> get, next, audio, block)."""
    found: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9_]+", text or ""):
        parts = [p.lower() for p in split_identifier(raw)] or [raw.lower()]
        for p in parts + [raw.lower()]:
            for w in _WORDS.findall(p):
                if w not in STOPWORDS and (len(w) > 2 or w.isdigit()):
                    found.add(_stem(w))
    return found


def fts_query(text: str) -> str | None:
    """Every typed word, the last one as a prefix (so a half-typed word still matches)."""
    words = re.findall(r"[A-Za-z0-9_]+", text or "")[:12]
    if not words:
        return None
    quoted = [f'"{w}"' for w in words[:-1]] + [f'"{words[-1]}"*']
    return " ".join(quoted)


class HistoryStore:
    def __init__(self, path=None) -> None:
        self._path = path
        self._conn_: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._last_time = 0.0

    def _now(self) -> float:
        """The time, but never the same twice: entries made in the same instant (the clock ticks every few milliseconds on
        Windows) still sort in the order they were made."""
        with self._lock:
            self._last_time = max(time.time(), self._last_time + 1e-6)
            return self._last_time

    # ------------------------------------------------------------------ plumbing
    def _conn(self) -> sqlite3.Connection:
        if self._conn_ is None:
            path = self._path or (paths.home() / "history.db")
            c = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=15)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.executescript(SCHEMA)
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('version', ?)", (str(SCHEMA_VERSION),))
            self._conn_ = c
        return self._conn_

    def close(self) -> None:
        with self._lock:
            if self._conn_ is not None:
                try:
                    self._conn_.close()
                finally:
                    self._conn_ = None

    def _meta(self, key: str, default: str = "") -> str:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    # ------------------------------------------------------------------ on / off
    @property
    def enabled(self) -> bool:
        with self._lock:
            try:
                return self._meta("keep", "1") != "0"
            except sqlite3.Error:
                log.warning("history database unavailable", exc_info=True)
                return False

    def set_enabled(self, on: bool) -> None:
        with self._lock:
            self._conn().execute("INSERT INTO meta(key, value) VALUES ('keep', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                 ("1" if on else "0",))

    # ------------------------------------------------------------------ chats: writing
    def record(self, question: str, chat_id: str | None, stream: Iterable[dict], meta: dict | None = None) -> Iterator[dict]:
        """Pass the answer stream through unchanged, remember the turn, and save it when the stream ends or is stopped.

        A leading {"type": "chat", "id", "title", "new"} event tells the page which conversation this turn belongs to.
        Nothing is saved when history is off, or when the question produced neither an answer nor sources (a failed
        attempt that the user will simply retry)."""
        if not self.enabled or not (question or "").strip():
            yield from stream
            return
        new = not (chat_id and _ID.match(chat_id) and self.has_chat(chat_id))
        if new:
            chat_id = chat_id if (chat_id and _ID.match(chat_id)) else secrets.token_hex(6)
        title = one_line(question, 80) if new else self.title_of(chat_id)
        yield {"type": "chat", "id": chat_id, "title": title, "new": new}
        text: list[str] = []
        thinking: list[str] = []
        events: list[dict] = []
        finished = False
        try:
            for ev in stream:
                kind = ev.get("type")
                if kind == "token":
                    text.append(ev.get("text", ""))
                elif kind == "thinking":
                    thinking.append(ev.get("text", ""))
                elif kind != "status":                 # status events only say what the app is doing right now
                    events.append(ev)
                yield ev
            finished = True
        except Exception as e:  # noqa: BLE001
            events.append({"type": "error", "message": str(e)})
            raise
        finally:
            try:
                self._save_turn(chat_id, title, question, "".join(text), "".join(thinking), events, stopped=not finished, meta=meta or {})
            except Exception:  # noqa: BLE001 - history must never break answering
                log.warning("could not save the turn to the history", exc_info=True)

    def _save_turn(self, chat_id: str, title: str, question: str, answer: str, thinking: str, events: list[dict],
                   stopped: bool, meta: dict) -> None:
        sources = next((e.get("sources") for e in events if e.get("type") == "sources"), None) or []
        if not answer.strip() and not sources:
            return
        record = {"thinking": thinking, "events": events, "stopped": stopped, "profile": meta.get("profile") or {}}
        now = self._now()
        with self._lock:
            c = self._conn()
            c.execute("BEGIN")
            try:
                row = c.execute("SELECT n_turns, n_sources FROM chats WHERE id=?", (chat_id,)).fetchone()
                preview = plain_preview(answer)
                profile = json.dumps(meta.get("profile") or {}, ensure_ascii=False)
                libs = json.dumps(meta.get("libraries") or [], ensure_ascii=False)
                if row is None:
                    c.execute("INSERT INTO chats(id, title, created, updated, n_turns, n_sources, preview, profile, libraries) "
                              "VALUES (?,?,?,?,?,?,?,?,?)", (chat_id, title, now, now, 0, 0, preview, profile, libs))
                    n_turns, n_sources = 0, 0
                else:
                    n_turns, n_sources = row["n_turns"], row["n_sources"]
                c.execute("INSERT INTO turns(chat_id, ordinal, question, answer, record, created) VALUES (?,?,?,?,?,?)",
                          (chat_id, n_turns + 1, question, answer, json.dumps(record, ensure_ascii=False), now))
                c.execute("UPDATE chats SET updated=?, n_turns=?, n_sources=?, preview=?, profile=?, libraries=? WHERE id=?",
                          (now, n_turns + 1, n_sources + len(sources), preview, profile, libs, chat_id))
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ chats: reading
    def has_chat(self, chat_id: str) -> bool:
        with self._lock:
            return self._conn().execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone() is not None

    def title_of(self, chat_id: str) -> str:
        with self._lock:
            row = self._conn().execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
        return row[0] if row else ""

    @staticmethod
    def _chat_row(r: sqlite3.Row, snippet: str = "") -> dict:
        out = {"id": r["id"], "title": r["title"], "created": r["created"], "updated": r["updated"], "turns": r["n_turns"],
               "sources": r["n_sources"], "preview": r["preview"], "profile": json.loads(r["profile"] or "{}"),
               "libraries": json.loads(r["libraries"] or "[]")}
        if snippet:
            out["match"] = snippet
        return out

    def list_chats(self, q: str = "", limit: int = 60, offset: int = 0) -> dict:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        q = (q or "").strip()
        with self._lock:
            c = self._conn()
            if not q:
                total = c.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
                rows = c.execute("SELECT * FROM chats ORDER BY updated DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
                return {"chats": [self._chat_row(r) for r in rows], "total": total}
            order: list[str] = []
            snippets: dict[str, str] = {}
            match = fts_query(q)
            if match:
                try:
                    for r in c.execute(
                            "SELECT t.chat_id AS chat_id, snippet(turns_fts, -1, char(1), char(2), '…', 16) AS snip "
                            "FROM turns_fts JOIN turns t ON t.id = turns_fts.rowid WHERE turns_fts MATCH ? "
                            "ORDER BY rank LIMIT 300", (match,)):
                        if r["chat_id"] not in snippets:
                            order.append(r["chat_id"])
                            snippets[r["chat_id"]] = r["snip"]
                except sqlite3.OperationalError:
                    log.debug("history search query not understood: %r", q)
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for r in c.execute("SELECT id FROM chats WHERE title LIKE ? ESCAPE '\\' ORDER BY updated DESC LIMIT 100", (like,)):
                if r["id"] not in snippets:
                    order.append(r["id"])
                    snippets[r["id"]] = ""
            page = order[offset: offset + limit]
            rows = {}
            if page:
                marks = ",".join("?" * len(page))
                rows = {r["id"]: r for r in c.execute(f"SELECT * FROM chats WHERE id IN ({marks})", page)}
            return {"chats": [self._chat_row(rows[i], snippets[i]) for i in page if i in rows], "total": len(order)}

    def get_chat(self, chat_id: str) -> dict | None:
        with self._lock:
            c = self._conn()
            r = c.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            if r is None:
                return None
            turns = []
            for t in c.execute("SELECT * FROM turns WHERE chat_id=? ORDER BY ordinal", (chat_id,)):
                rec = {}
                try:
                    rec = json.loads(t["record"] or "{}")
                except ValueError:
                    pass
                turns.append({"id": t["id"], "ordinal": t["ordinal"], "question": t["question"], "answer": t["answer"],
                              "created": t["created"], "thinking": rec.get("thinking", ""), "events": rec.get("events", []),
                              "stopped": bool(rec.get("stopped")), "model": (rec.get("profile") or {}).get("model") or ""})
            out = self._chat_row(r)
            out["turns_list"] = turns
            return out

    def rename_chat(self, chat_id: str, title: str) -> bool:
        title = one_line(title, 120)
        if not title:
            return False
        with self._lock:
            cur = self._conn().execute("UPDATE chats SET title=? WHERE id=?", (title, chat_id))
            return cur.rowcount > 0

    def delete_chat(self, chat_id: str) -> bool:
        with self._lock:
            c = self._conn()
            c.execute("DELETE FROM turns WHERE chat_id=?", (chat_id,))     # the FTS trigger needs the rows deleted explicitly
            cur = c.execute("DELETE FROM chats WHERE id=?", (chat_id,))
            return cur.rowcount > 0

    def clear_chats(self) -> int:
        with self._lock:
            c = self._conn()
            n = c.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            c.execute("DELETE FROM turns")
            c.execute("DELETE FROM chats")
            c.execute("INSERT INTO turns_fts(turns_fts) VALUES ('rebuild')")
            return n

    # ------------------------------------------------------------------ searches
    def note_search(self, query: str, filters: dict | None = None, libraries: list[dict] | None = None, results: int = 0) -> int | None:
        query = (query or "").strip()
        n = norm(query)
        if len(n) < 2 or not self.enabled:
            return None
        filters = {k: v for k, v in (filters or {}).items() if v not in (None, "", [], False)}
        key = json.dumps(filters, sort_keys=True, ensure_ascii=False)
        now = self._now()
        with self._lock:
            c = self._conn()
            c.execute(
                "INSERT INTO searches(norm, query, filters, filters_key, libraries, results, first_run, last_run, runs) "
                "VALUES (?,?,?,?,?,?,?,?,1) ON CONFLICT(norm, filters_key) DO UPDATE SET query=excluded.query, "
                "libraries=excluded.libraries, results=excluded.results, last_run=excluded.last_run, runs=runs+1",
                (n, one_line(query, 300), json.dumps(filters, ensure_ascii=False), key,
                 json.dumps(libraries or [], ensure_ascii=False), int(results), now, now))
            row = c.execute("SELECT id FROM searches WHERE norm=? AND filters_key=?", (n, key)).fetchone()
            return row[0] if row else None

    @staticmethod
    def _search_row(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "query": r["query"], "filters": json.loads(r["filters"] or "{}"),
                "libraries": json.loads(r["libraries"] or "[]"), "results": r["results"], "first_run": r["first_run"],
                "last_run": r["last_run"], "runs": r["runs"]}

    def list_searches(self, q: str = "", limit: int = 80, offset: int = 0) -> dict:
        limit = max(1, min(int(limit), 300))
        offset = max(0, int(offset))
        q = norm(q)
        with self._lock:
            c = self._conn()
            if q:
                like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                total = c.execute("SELECT COUNT(*) FROM searches WHERE norm LIKE ? ESCAPE '\\'", (like,)).fetchone()[0]
                rows = c.execute("SELECT * FROM searches WHERE norm LIKE ? ESCAPE '\\' ORDER BY last_run DESC LIMIT ? OFFSET ?",
                                 (like, limit, offset)).fetchall()
            else:
                total = c.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
                rows = c.execute("SELECT * FROM searches ORDER BY last_run DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return {"searches": [self._search_row(r) for r in rows], "total": total}

    def delete_search(self, search_id: int) -> bool:
        with self._lock:
            return self._conn().execute("DELETE FROM searches WHERE id=?", (int(search_id),)).rowcount > 0

    def clear_searches(self) -> int:
        with self._lock:
            c = self._conn()
            n = c.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
            c.execute("DELETE FROM searches")
            return n

    # ------------------------------------------------------------------ suggestions
    def _candidates(self) -> list[dict]:
        """Distinct things typed before: questions asked and searches run, newest first."""
        with self._lock:
            c = self._conn()
            asked = c.execute("SELECT question AS text, MAX(created) AS last, COUNT(*) AS n, MIN(chat_id) AS ref FROM turns "
                              "GROUP BY lower(trim(question)) ORDER BY last DESC LIMIT ?", (_SCAN,)).fetchall()
            ran = c.execute("SELECT query AS text, MAX(last_run) AS last, SUM(runs) AS n, MIN(id) AS ref FROM searches "
                            "GROUP BY norm ORDER BY last DESC LIMIT ?", (_SCAN,)).fetchall()
        merged: dict[str, dict] = {}
        for kind, rows in (("chat", asked), ("search", ran)):
            for r in rows:
                key = norm(r["text"])
                if not key:
                    continue
                item = {"text": one_line(r["text"], 200), "norm": key, "kind": kind, "last": r["last"], "n": r["n"], "ref": r["ref"]}
                old = merged.get(key)
                if old is None or item["last"] > old["last"]:
                    item["n"] = item["n"] + (old["n"] if old else 0)
                    merged[key] = item
                else:
                    old["n"] += item["n"]
        return list(merged.values())

    def suggest(self, prefix: str, limit: int = 8, prefer: str = "") -> list[dict]:
        """What you typed before that starts like `prefix` (or contains its words), best first; the most recent when empty."""
        limit = max(1, min(int(limit), 20))
        p = norm(prefix)
        items = self._candidates()
        if not p:
            items.sort(key=lambda i: (prefer != i["kind"], -i["last"]))
            return [self._suggestion(i) for i in items[:limit]]
        p_words = p.split(" ")
        ranked: list[tuple[tuple, dict]] = []
        for item in items:
            text = item["norm"]
            tier = None
            if text.startswith(p):
                tier = 0
            else:
                words = text.split(" ")
                if all(any(w.startswith(pw) for w in words) for pw in p_words):
                    tier = 1
                elif p in text:
                    tier = 2
                elif len(p) >= 4 and difflib.SequenceMatcher(None, p, text[: len(p) + 1]).ratio() >= 0.84:
                    tier = 3           # a typo in what was typed so far
            if tier is None:
                continue
            ranked.append(((tier, prefer != item["kind"], -item["last"], -item["n"]), item))
        ranked.sort(key=lambda t: t[0])
        return [self._suggestion(item) for _key, item in ranked[:limit]]

    @staticmethod
    def _suggestion(item: dict) -> dict:
        return {"text": item["text"], "kind": item["kind"], "ref": item["ref"], "last": item["last"], "count": item["n"]}

    def related(self, query: str, limit: int = 6) -> list[dict]:
        """Earlier questions and searches that share meaningful words with `query` (not the same text), best first."""
        limit = max(1, min(int(limit), 20))
        words = content_words(query)
        if not words:
            return []
        own = norm(query)
        scored: list[tuple[float, float, dict]] = []
        for item in self._candidates():
            if item["norm"] == own:
                continue
            theirs = content_words(item["text"])
            shared = words & theirs
            if not shared:
                continue
            score = len(shared) / ((len(words) * len(theirs)) ** 0.5)
            if score < 0.25 and not (len(words) == 1 and shared == words):
                continue
            scored.append((score, item["last"], item))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return [self._suggestion(item) for _s, _l, item in scored[:limit]]

    # ------------------------------------------------------------------ housekeeping
    def stats(self) -> dict:
        with self._lock:
            c = self._conn()
            return {"enabled": self.enabled,
                    "chats": c.execute("SELECT COUNT(*) FROM chats").fetchone()[0],
                    "turns": c.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
                    "searches": c.execute("SELECT COUNT(*) FROM searches").fetchone()[0]}

    def clear_all(self) -> dict:
        return {"chats": self.clear_chats(), "searches": self.clear_searches()}
