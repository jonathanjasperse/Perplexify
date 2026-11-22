"""A file that is about to be indexed."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Candidate:
    path: Path
    uri: str            # what the document is called in the index (a web address, file:// URL, ...)
    origin: str         # "mirror": stored_path is relative to the library's source folder; "external": absolute
    stored_path: str
    rel: str = ""       # path inside a repository; becomes the title of code files
