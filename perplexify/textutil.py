"""Text helpers shared by indexing and search."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_PARTS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_HAS_SPLIT = re.compile(r"[a-z][A-Z]|[A-Z]{2,}[a-z]|_|[A-Za-z][0-9]|[0-9][A-Za-z]")

STOPWORDS = frozenset("""
a an the is are was were be been being do does did how what which who whom whose when where why can could
should would will shall may might must i me my we our you your it its this that these those to of in on for
with and or but if so as at by from into about than then there here use using used get gets make makes not no
yes please tell show give me explain describe
""".split())


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.6))


def split_identifier(token: str) -> list[str]:
    """'getNextAudioBlock' -> ['get', 'next', 'audio', 'block'];  'JUCE_MODULE_X' -> ['juce', 'module', 'x']."""
    return [p.lower() for p in _CAMEL_PARTS.findall(token.replace("_", " "))]


def terms_for_index(text: str, limit: int = 400) -> str:
    """Words hidden inside identifiers (camelCase / snake_case) so that a search for
    'audio processor' also finds 'AudioProcessor'. Stored in a separate, low-weight FTS column."""
    seen: dict[str, None] = {}
    for m in _WORD.finditer(text):
        tok = m.group(0)
        if len(tok) < 4 or not _HAS_SPLIT.search(tok):
            continue
        for part in split_identifier(tok):
            if len(part) > 1:
                seen[part] = None
        if len(seen) >= limit:
            break
    return " ".join(seen)


def search_terms(query: str, max_terms: int = 30) -> list[str]:
    """Keyword terms for a natural-language or identifier-style query."""
    out: dict[str, None] = {}
    for m in _WORD.finditer(query):
        tok = m.group(0)
        low = tok.lower()
        if low in STOPWORDS or (len(low) < 2 and not low.isdigit()):
            continue
        out[low] = None
        if _HAS_SPLIT.search(tok):
            for part in split_identifier(tok):
                if len(part) > 1 and part not in STOPWORDS:
                    out[part] = None
        if len(out) >= max_terms:
            break
    return list(out)


_PHRASE = re.compile(r'"([^"]{2,200})"')


def quoted_phrases(query: str) -> list[str]:
    """Text the user put in "double quotes": matched as an exact phrase."""
    return [p.strip() for p in _PHRASE.findall(query) if p.strip()]


def _unquoted(query: str) -> str:
    return _PHRASE.sub(lambda m: m.group(1), query)


def _fts_quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def build_fts_query(query: str, match: str = "any") -> str | None:
    """Keyword query for FTS5. match="any": any word counts and passages with more of them rank higher (BM25).
    match="all": every word the user typed must appear (identifier parts are not required)."""
    q = _unquoted(query)
    if match == "all":
        typed = list(dict.fromkeys(m.group(0).lower() for m in _WORD.finditer(q)
                                   if m.group(0).lower() not in STOPWORDS and len(m.group(0)) >= 2))
        return " AND ".join(_fts_quote(t) for t in typed) if typed else None
    terms = search_terms(q)
    if not terms:
        return None
    return " OR ".join(_fts_quote(t) for t in terms)


def identifier_terms(query: str) -> list[str]:
    """Tokens that look like code identifiers (camelCase, snake_case, with digits): matched exactly,
    because splitting them into common words ('process', 'block') loses what the user asked for."""
    out: dict[str, None] = {}
    for m in _WORD.finditer(_unquoted(query)):
        tok = m.group(0)
        if len(tok) >= 4 and tok.lower() not in STOPWORDS and _HAS_SPLIT.search(tok):
            out[tok.lower()] = None
    return list(out)


def build_exact_query(query: str) -> str | None:
    """Identifiers and "quoted phrases", which deserve an exact match rather than a loose one."""
    parts = [_fts_quote(t) for t in identifier_terms(query)] + [_fts_quote(p) for p in quoted_phrases(query)]
    return " OR ".join(parts) if parts else None


def highlight_terms(query: str, limit: int = 14) -> list[str]:
    """What to emphasise in a result: quoted phrases, the words typed, and the parts of identifiers."""
    out: dict[str, None] = {}
    for p in quoted_phrases(query):
        out[p.lower()] = None
    for m in _WORD.finditer(_unquoted(query)):
        tok = m.group(0)
        low = tok.lower()
        if low in STOPWORDS or len(low) < 3:
            continue
        out[low] = None
        if _HAS_SPLIT.search(tok):
            for part in split_identifier(tok):
                if len(part) >= 3 and part not in STOPWORDS:
                    out[part] = None
    return sorted(out, key=len, reverse=True)[:limit]


def best_excerpt(text: str, terms: list[str], max_chars: int = 560, context_lines: int = 2) -> str:
    """The most relevant few lines of a passage: the line with the most matches, plus a little context."""
    lines = text.split("\n")
    low = [t.lower() for t in terms if len(t) >= 2]
    scores = [sum(ln.lower().count(t) for t in low) for ln in lines] if low else []
    if not scores or max(scores) == 0:
        head = text[:max_chars]
        return head + (" ..." if len(text) > max_chars else "")
    best = scores.index(max(scores))
    start, end = max(0, best - context_lines), min(len(lines), best + context_lines + 1)
    excerpt = "\n".join(lines[start:end])
    if len(excerpt) > max_chars:  # long prose lines: cut around the first match instead
        lowered = excerpt.lower()
        pos = min((i for i in (lowered.find(t) for t in low) if i >= 0), default=0)
        a = max(0, pos - max_chars // 3)
        excerpt = ("... " if a > 0 else "") + excerpt[a:a + max_chars] + " ..."
    else:
        excerpt = ("... " if start > 0 else "") + excerpt + (" ..." if end < len(lines) else "")
    return excerpt


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_file(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def slugify(name: str, fallback: str = "manual") -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", name.strip().lower()).strip("-")
    return s[:48] or fallback
