"""File loaders: turn a file on disk into a ParsedDoc (title + sections of text)."""
from __future__ import annotations

from pathlib import Path

from ..chunking import ParsedDoc

HTML_EXT = {".html", ".htm", ".xhtml"}
TEXT_EXT = {".md", ".markdown", ".rst", ".txt", ".adoc"}
PDF_EXT = {".pdf"}


def supported_extensions() -> set[str]:
    """Extensions picked up when a folder is indexed as documents (code goes through the repository importer)."""
    return HTML_EXT | TEXT_EXT | PDF_EXT


def load_file(path: Path, rel: str = "") -> ParsedDoc | None:
    """Parse a file. `rel` is the path inside a repository (it becomes the title of code and text files).
    Returns None when there is nothing useful to index."""
    ext = path.suffix.lower()
    from .code import NAME_LANG, detect_language

    lang = detect_language(path)
    if lang and (ext not in TEXT_EXT or path.name.lower() in NAME_LANG):
        from .code import load_code

        return load_code(path, rel)
    if ext in HTML_EXT:
        from .html import load_html

        return load_html(path)
    if ext in PDF_EXT:
        from .pdf import load_pdf

        return load_pdf(path)
    if ext in TEXT_EXT:
        from .text import load_text

        return load_text(path, rel)
    return None
