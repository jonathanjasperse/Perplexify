"""One-click sources and the filters that keep generated-documentation boilerplate out of the index."""
from __future__ import annotations

# Doxygen sites contain many pages that are pure indexes or raw source listings. They add noise and no
# knowledge, so they are neither downloaded nor indexed.
DOXYGEN_CRAWL_EXCLUDE = [
    r"_source\.html$",          # annotated source listings
    r"-members\.html$",         # "list of all members" pages
    r"/dir_[0-9a-f]+\.html$",   # directory reference pages
    r"\.(?:map|md5)$",
]

DOXYGEN_INDEX_SKIP = [
    r"(^|/)functions(_[a-z0-9_]+)?\.html$",
    r"(^|/)globals(_[a-z0-9_]+)?\.html$",
    r"(^|/)namespacemembers(_[a-z0-9_]+)?\.html$",
    r"(^|/)(annotated|classes|hierarchy|files|pages|topics|modules|namespaces|graph_legend)\.html$",
]

PRESETS = [
    {
        "id": "juce",
        "name": "JUCE documentation",
        "description": "The JUCE C++ framework API reference and tutorials (docs.juce.com, master branch).",
        "kind": "crawl",
        "url": "https://docs.juce.com/master/index.html",
        "library_name": "JUCE",
    },
]
