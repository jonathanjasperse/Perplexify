"""Turn parsed documents into passages ("chunks") sized for retrieval.

A parsed document is a list of Sections (heading breadcrumb + blocks of text). Chunks follow the
document's own structure: one chunk per section when it fits, big sections are split at block
boundaries (code blocks stay whole), tiny neighbouring sections are merged. Each chunk carries the
full heading breadcrumb, which is also prepended to the text that gets embedded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SEP = " > "


@dataclass
class Section:
    path: list[str]
    blocks: list[str]
    anchor: str | None = None


@dataclass
class ParsedDoc:
    title: str
    sections: list[Section]
    kind: str = "text"        # html | pdf | markdown | rst | text | code
    lang: str = ""            # programming language for code files
    redactions: int = 0       # credential-looking strings replaced before indexing


@dataclass
class Chunk:
    heading: str
    anchor: str | None
    text: str


def _split_big_block(block: str, target: int) -> list[str]:
    fenced = block.startswith("```") and block.rstrip().endswith("```")
    lines = block.split("\n")
    if fenced and len(lines) >= 2:
        lines = lines[1:-1]
    out: list[str] = []
    cur: list[str] = []
    n = 0
    for ln in lines:
        while len(ln) > target:
            cut = ln.rfind(" ", 0, target)
            cut = cut if cut > target * 0.5 else target
            if cur:
                out.append("\n".join(cur))
                cur, n = [], 0
            out.append(ln[:cut])
            ln = ln[cut:].lstrip()
        if n + len(ln) + 1 > target and cur:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    if fenced:
        out = ["```\n" + o + "\n```" for o in out]
    return out


def _split_blocks(blocks: list[str], target: int, max_chars: int) -> list[str]:
    expanded: list[str] = []
    for b in blocks:
        expanded.extend(_split_big_block(b, target) if len(b) > max_chars else [b])
    parts: list[str] = []
    cur: list[str] = []
    n = 0
    fresh = 0  # blocks in `cur` that were not carried over as overlap
    for b in expanded:
        if cur and n + len(b) + 2 > target and fresh:
            parts.append("\n\n".join(cur))
            tail = cur[-1]
            if len(tail) <= 400 and not tail.startswith("```"):
                cur, n = [tail], len(tail)
            else:
                cur, n = [], 0
            fresh = 0
        cur.append(b)
        n += len(b) + 2
        fresh += 1
    if cur and fresh:
        parts.append("\n\n".join(cur))
    return parts


def _useful(text: str) -> bool:
    return sum(c.isalnum() for c in text) >= 12


def chunk_document(doc: ParsedDoc, target: int = 1200, max_chars: int = 2000, min_chars: int = 220) -> list[Chunk]:
    title = (doc.title or "").strip()
    if doc.kind == "code":
        # the code loader already cut along function / class boundaries and knows the exact line ranges
        out: list[Chunk] = []
        for sec in doc.sections:
            text = "\n\n".join(b for b in sec.blocks if b).rstrip()
            if _useful(text):
                out.append(Chunk(SEP.join([title] + [p for p in sec.path if p]), sec.anchor, text))
        return out
    pieces: list[tuple[list[str], str | None, str]] = []
    for sec in doc.sections:
        blocks = [b for b in sec.blocks if b and b.strip()]
        if not blocks:
            continue
        body = "\n\n".join(blocks)
        if len(body) <= max_chars:
            pieces.append((sec.path, sec.anchor, body))
        else:
            for part in _split_blocks(blocks, target, max_chars):
                pieces.append((sec.path, sec.anchor, part))

    merged: list[tuple[list[str], str | None, str]] = []
    for path, anchor, text in pieces:
        if merged:
            ppath, panchor, ptext = merged[-1]
            if len(ptext) < min_chars and ppath[:-1] == path[:-1] and len(ptext) + len(text) + 2 <= target:
                sub = path[-1] if (path and path != ppath) else ""
                merged[-1] = (ppath, panchor, ptext + "\n\n" + (sub + "\n" if sub else "") + text)
                continue
        merged.append((path, anchor, text))

    chunks: list[Chunk] = []
    for path, anchor, text in merged:
        if not _useful(text):
            continue
        crumbs = [title] if title else []
        crumbs += [p for p in path if p and p != title]
        chunks.append(Chunk(heading=SEP.join(crumbs), anchor=anchor, text=text.strip()))
    return chunks


def embedding_text(heading: str, text: str, limit: int = 2400) -> str:
    """What actually gets embedded: breadcrumb + passage (gives the model the page context)."""
    s = f"{heading}\n\n{text}" if heading else text
    return s[:limit]
