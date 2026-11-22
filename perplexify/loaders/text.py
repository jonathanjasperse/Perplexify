"""Markdown / reStructuredText / AsciiDoc / plain text / source files -> ParsedDoc."""
from __future__ import annotations

import re
from pathlib import Path

from ..chunking import ParsedDoc
from ..secretscan import redact
from ..textutil import decode_bytes
from .common import Builder

_MD_HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ADOC_HEAD = re.compile(r"^(={1,6})\s+(.*?)\s*$")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+._:])\1{2,}\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _strip_front_matter(lines: list[str]) -> list[str]:
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 60)):
            if lines[i].strip() in ("---", "..."):
                return lines[i + 1:]
    return lines


def _blocks_from_lines(lines: list[str], b: Builder, head_re: re.Pattern | None, level_of=None) -> None:
    para: list[str] = []
    fence: list[str] | None = None

    def flush_para() -> None:
        if para:
            b.add("\n".join(para))
            para.clear()

    for ln in lines:
        if fence is not None:
            fence.append(ln)
            if _FENCE.match(ln):
                b.add("\n".join(fence))
                fence = None
            continue
        if _FENCE.match(ln):
            flush_para()
            fence = [ln]
            continue
        m = head_re.match(ln) if head_re else None
        if m:
            flush_para()
            level = level_of(m) if level_of else len(m.group(1))
            text = m.group(2)
            b.heading(level, text, _slug(text))
            continue
        if not ln.strip():
            flush_para()
        else:
            para.append(ln.rstrip())
    if fence:
        b.add("\n".join(fence))
    flush_para()


def _first_heading(lines: list[str], regex: re.Pattern) -> str:
    in_fence = False
    for ln in lines[:400]:
        if _FENCE.match(ln):
            in_fence = not in_fence
        if in_fence:
            continue
        m = regex.match(ln)
        if m and len(m.group(1)) == 1:
            return m.group(2).strip()
    return ""


def _markdown(text: str, stem: str, adoc: bool = False) -> ParsedDoc:
    lines = _strip_front_matter(text.splitlines())
    regex = _ADOC_HEAD if adoc else _MD_HEAD
    title = _first_heading(lines, regex) or stem
    b = Builder(title)
    _blocks_from_lines(lines, b, regex)
    return ParsedDoc(title, b.finish(), "markdown")


def _rst(text: str, stem: str) -> ParsedDoc:
    lines = text.splitlines()
    levels: dict[str, int] = {}
    heads: list[tuple[int, int, str]] = []  # (line index of title, level, text)
    skip: set[int] = set()
    for i in range(len(lines) - 1):
        t, u = lines[i], lines[i + 1]
        if t.strip() and not _RST_UNDERLINE.match(t) and _RST_UNDERLINE.match(u) and len(u.strip()) >= len(t.strip()):
            ch = u.strip()[0]
            levels.setdefault(ch, len(levels) + 1)
            heads.append((i, levels[ch], t.strip()))
            skip.update((i, i + 1))
    title = next((h[2] for h in heads if h[1] == 1), stem)
    b = Builder(title)
    head_at = {i: (lvl, txt) for i, lvl, txt in heads}
    para: list[str] = []

    def flush() -> None:
        if para:
            b.add("\n".join(para))
            para.clear()

    for i, ln in enumerate(lines):
        if i in head_at:
            flush()
            lvl, txt = head_at[i]
            b.heading(lvl, txt, _slug(txt))
        elif i in skip:
            continue
        elif not ln.strip():
            flush()
        else:
            para.append(ln.rstrip())
    flush()
    return ParsedDoc(title, b.finish(), "rst")


def _plain(text: str, stem: str, kind: str) -> ParsedDoc:
    b = Builder(stem)
    _blocks_from_lines(text.splitlines(), b, None)
    return ParsedDoc(stem, b.finish(), kind)


def load_text(path: Path, rel: str = "") -> ParsedDoc | None:
    text = decode_bytes(path.read_bytes())
    if "\x00" in text[:2000]:
        return None  # binary
    redactions = 0
    if rel:  # inside a code repository: keep credentials out of the index
        text, redactions = redact(text)
    ext = path.suffix.lower()
    if ext in (".md", ".markdown"):
        doc = _markdown(text, path.stem)
    elif ext == ".adoc":
        doc = _markdown(text, path.stem, adoc=True)
    elif ext == ".rst":
        doc = _rst(text, path.stem)
    else:
        doc = _plain(text, path.name, "text")
    if not doc.sections:
        return None
    if rel:
        doc.title = rel  # the file's path is the most useful title inside a repository
    doc.redactions = redactions
    return doc
