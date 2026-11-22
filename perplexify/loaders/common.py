"""Shared helper: collects blocks of text under a moving heading breadcrumb."""
from __future__ import annotations

from ..chunking import Section


class Builder:
    def __init__(self, title: str = ""):
        self.title = title.strip()
        self.sections: list[Section] = []
        self._stack: list[tuple[int, str]] = []
        self._blocks: list[str] = []
        self._path: list[str] = []
        self._anchor: str | None = None
        self._title_consumed = False

    def heading(self, level: int, text: str, anchor: str | None = None) -> None:
        text = " ".join(text.split())
        if not text:
            return
        if level == 1 and not self._title_consumed and self.title and text.lower() == self.title.lower():
            self._title_consumed = True  # the page's own title, not a section
            return
        self._flush()
        while self._stack and self._stack[-1][0] >= level:
            self._stack.pop()
        self._stack.append((level, text))
        self._path = [t for _, t in self._stack]
        self._anchor = anchor

    def add(self, text: str) -> None:
        text = text.strip()
        if text:
            self._blocks.append(text)

    def _flush(self) -> None:
        if self._blocks:
            self.sections.append(Section(list(self._path), self._blocks, self._anchor))
            self._blocks = []

    def finish(self) -> list[Section]:
        self._flush()
        return self.sections
