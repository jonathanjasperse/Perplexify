"""Runs in worker processes: parse one file into passages. Kept import-light on purpose
(no numpy, no embedding libraries) so worker processes start quickly."""
from __future__ import annotations

from pathlib import Path


def parse_file(path_str: str, rel: str = "") -> dict:
    """Returns {'ok': bool, 'title', 'kind', 'lang', 'redactions', 'chunks': [(heading, anchor, text, terms)],
    'text_hash', 'error'}."""
    try:
        from .chunking import chunk_document
        from .loaders import load_file
        from .textutil import sha1_bytes, terms_for_index

        doc = load_file(Path(path_str), rel)
        if doc is None:
            return {"ok": True, "title": "", "kind": "empty", "lang": "", "redactions": 0, "chunks": [], "text_hash": ""}
        chunks = chunk_document(doc)
        rows = [(c.heading, c.anchor, c.text, terms_for_index(c.heading + " " + c.text)) for c in chunks]
        text_hash = sha1_bytes("\n".join(c.text for c in chunks).encode("utf-8", "replace")) if chunks else ""
        return {"ok": True, "title": doc.title, "kind": doc.kind, "lang": doc.lang, "redactions": doc.redactions,
                "chunks": rows, "text_hash": text_hash}
    except Exception as e:  # noqa: BLE001 - one bad file must not stop a 5,000 file import
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
