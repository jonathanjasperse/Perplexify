"""PDF manuals -> ParsedDoc, one section per page, labelled with the PDF's bookmarks when it has them."""
from __future__ import annotations

import re
from pathlib import Path

from ..chunking import ParsedDoc, Section


def _outline_paths(reader) -> list[tuple[int, list[str]]]:
    starts: list[tuple[int, list[str]]] = []

    def rec(items, trail):
        last_title: str | None = None
        for it in items:
            if isinstance(it, list):
                rec(it, trail + ([last_title] if last_title else []))
                continue
            try:
                page = reader.get_destination_page_number(it)
                title = str(getattr(it, "title", "") or "").strip()
            except Exception:
                continue
            if page is None or page < 0 or not title:
                continue
            last_title = title
            starts.append((page, (trail + [title])[-3:]))

    try:
        rec(reader.outline, [])
    except Exception:
        return []
    starts.sort(key=lambda s: s[0])
    return starts


def _reflow(text: str) -> list[str]:
    text = text.replace("\r", "")
    text = re.sub(r"-\n(?=[a-z])", "", text)  # undo hyphenation at line ends
    blocks: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        if not lines:
            continue
        avg = sum(len(x) for x in lines) / len(lines)
        blocks.append("\n".join(lines) if (len(lines) > 3 and avg < 45) else " ".join(lines))
    return blocks


def load_pdf(path: Path) -> ParsedDoc | None:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return None
    meta_title = ""
    try:
        meta_title = (reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:
        pass
    title = meta_title or path.stem
    starts = _outline_paths(reader)
    sections: list[Section] = []
    ptr = 0
    current: list[str] = []
    for i, page in enumerate(reader.pages):
        while ptr < len(starts) and starts[ptr][0] <= i:
            current = starts[ptr][1]
            ptr += 1
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        blocks = _reflow(text)
        if blocks:
            sections.append(Section(list(current) + [f"Page {i + 1}"], blocks, f"page={i + 1}"))
    if not sections:
        return None
    return ParsedDoc(title=title, sections=sections, kind="pdf")
