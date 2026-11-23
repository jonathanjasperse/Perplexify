"""HTML manuals -> ParsedDoc.

Tuned for Doxygen output (member summary tables, function signatures, code fragments, anchors that
let answers deep-link to a single member), with a generic path for ordinary documentation sites and
WordPress-style pages: the main content area is located, site chrome (menus, headers, footers) is
dropped, and the rest is flattened into headings + paragraphs + code blocks.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..chunking import ParsedDoc
from .common import Builder

try:
    import lxml  # noqa: F401
    _PARSER = "lxml"
except ImportError:  # pragma: no cover
    _PARSER = "html.parser"

sys.setrecursionlimit(max(sys.getrecursionlimit(), 4000))

DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "template", "form", "button", "select", "input",
             "textarea", "canvas", "audio", "video", "picture", "object", "embed", "map", "link", "meta"]
_DROP_SET = set(DROP_TAGS)
CHROME_TAGS = {"nav", "footer", "aside"}
SKIP_CLASSES = {"permalink", "headerlink", "header-anchor", "hash-link", "anchorjs-link", "heading-anchor",  # the ¶ / # beside headings (Sphinx, VitePress, Docusaurus, ...)
                "dynheader", "dyncontent", "navpath", "memSeparator", "ttc", "lineno",
                "juce-site-chrome", "site-header", "site-footer", "yoast-breadcrumbs", "wp-block-navigation",
                "skip-link", "screen-reader-text", "sr-only", "toc", "breadcrumbs", "breadcrumb", "navigation"}
SKIP_IDS = {"nav-path", "side-nav", "MSearchBox", "MSearchSelectWindow", "MSearchResultsWindow", "titlearea", "top", "toc"}
SKIP_ROLES = {"navigation", "banner", "contentinfo", "search"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCKS = {"p", "div", "section", "article", "main", "blockquote", "figure", "figcaption", "details", "summary",
          "address", "center", "tbody", "thead", "tfoot", "dd", "dt", "li", "hr", "header", "fieldset", "legend",
          "body", "html", "ul", "ol", "dl", "table", "pre", "tr", "td", "th", *HEADINGS}
ROOT_SELECTORS = ["#doc-content", "main", "article", "[role=main]", "#content", "#main-content", "#main",
                  ".markdown-body", ".document", "div.body", ".content", "body"]
PARAM_TABLES = {"params", "fieldtable", "exception", "retval", "tparams"}

_WS = re.compile(r"\s+")
_HEADING_TAG = re.compile(r"^h[1-6]$")
_NOISE = re.compile(r"^(The documentation for this (class|struct|union|file|namespace) was generated from the following"
                    r"|Inheritance diagram for|Collaboration diagram for)", re.I)


# --------------------------------------------------------------------------- text helpers
def _clean(s: str) -> str:
    s = s.replace(" ", " ").replace("​", "").replace("◆", "").replace("♦", "")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _skip(tag: Tag) -> bool:
    name = tag.name
    if name in CHROME_TAGS:
        return True
    if name == "header" and tag.find(_HEADING_TAG) is None:
        return True
    cls = tag.get("class") or []
    if cls:
        if SKIP_CLASSES.intersection(cls):
            return True
        if name == "span" and "mlabels" in cls:
            return True  # "abstract" / "virtual" badges are read from the signature table instead
        if "summary" in cls and tag.parent is not None and "header" in (tag.parent.get("class") or []):
            return True  # Doxygen's "Classes | Public Types | ..." anchor menu
    ident = tag.get("id")
    if ident and ident in SKIP_IDS:
        return True
    if tag.get("role") in SKIP_ROLES or tag.has_attr("hidden"):
        return True
    style = tag.get("style")
    if style and "display:none" in style.replace(" ", "").lower():
        return True
    return False


def _itext(node: Tag) -> str:
    """Inline text of a node: whitespace collapsed, <br> kept as newline, <code> wrapped in backticks."""
    out: list[str] = []
    for c in node.children:
        if isinstance(c, Comment):
            continue
        if isinstance(c, NavigableString):
            out.append(_WS.sub(" ", str(c)))
        elif isinstance(c, Tag):
            if c.name in _DROP_SET or _skip(c):
                continue
            n = c.name
            if n == "br":
                out.append("\n")
            elif n in ("code", "tt", "kbd", "samp"):
                t = _itext(c).strip()
                if t:
                    out.append("`" + t.replace("`", "'") + "`")
            elif n in BLOCKS:
                out.append("\n" + _itext(c) + "\n")
            else:
                out.append(_itext(c))
    return "".join(out)


def _line(tag: Tag | None) -> str:
    return _clean(_itext(tag)).replace("\n", " ") if tag is not None else ""


def _heading_anchor(h: Tag) -> str | None:
    prev = h.previous_sibling
    while isinstance(prev, NavigableString) and not prev.strip():
        prev = prev.previous_sibling
    if isinstance(prev, Tag) and prev.name == "a":
        v = prev.get("id") or prev.get("name")
        if v:
            return str(v)
    a = h.find("a", attrs={"id": True}) or h.find("a", attrs={"name": True})
    if a is not None:
        v = a.get("id") or a.get("name")
        if v:
            return str(v)
    v = h.get("id")
    return str(v) if v else None


# --------------------------------------------------------------------------- block handlers
def _heading(h: Tag, b: Builder) -> None:
    cls = h.get("class") or []
    level = HEADINGS[h.name]
    if "memtitle" in cls:
        level = max(level, 3)  # a member documented under a "... Documentation" group heading
    b.heading(level, _line(h), _heading_anchor(h))


def _code(el: Tag, b: Builder) -> None:
    lines = el.find_all("div", class_="line")
    if lines:
        parts = []
        for ln in lines:
            for n in ln.select(".lineno"):
                n.decompose()
            parts.append(ln.get_text(""))
        code = "\n".join(parts)
    else:
        code = el.get_text("")
    code = code.replace(" ", " ").strip("\n").rstrip()
    if code.strip():
        b.add("```\n" + code + "\n```")


def _rows(table: Tag) -> list[Tag]:
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _memberdecls(t: Tag, b: Builder) -> None:
    lines: list[str] = []
    tmpl = ""

    def flush() -> None:
        nonlocal lines
        if lines:
            b.add("\n".join(lines))
            lines = []

    for tr in _rows(t):
        classes = tr.get("class") or []
        if any(c.startswith("inherit") for c in classes):
            continue  # members inherited from base classes are documented on their own pages
        if "heading" in classes:
            flush()
            h = tr.find(["h2", "h3"])
            if h is not None:
                b.heading(2, _line(h), _heading_anchor(h))
            continue
        if any(c.startswith("memtemplate") for c in classes):
            tmpl = _line(tr)
            continue
        if any(c.startswith("memitem") for c in classes):
            left = _line(tr.find("td", class_="memItemLeft"))
            right = _line(tr.find("td", class_="memItemRight"))
            line = " ".join(x for x in (tmpl, left, right) if x).strip()
            tmpl = ""
            if line:
                lines.append(line)
        elif any(c.startswith("memdesc") for c in classes):
            desc = re.sub(r"\s*More\.\.\.\s*$", "", _line(tr.find("td", class_="mdescRight"))).strip()
            if desc and lines:
                lines[-1] += " - " + desc
    flush()


def _memname(t: Tag, b: Builder, labels: list[str] | None = None) -> None:
    cells = [_line(td) for td in t.find_all("td")]
    sig = " ".join(c for c in cells if c)
    sig = re.sub(r"\s+([,)])", r"\1", sig)
    sig = re.sub(r"\(\s+", "(", sig)
    if sig:
        extra = f" [{', '.join(labels)}]" if labels else ""
        b.add("`" + sig.replace("`", "'") + "`" + extra)


def _table(t: Tag, b: Builder) -> None:
    classes = set(t.get("class") or [])
    if "memberdecls" in classes:
        return _memberdecls(t, b)
    if "memname" in classes:
        return _memname(t, b)
    if "mlabels" in classes:
        # Doxygen wraps the signature table together with badges such as "protected" / "virtual"
        inner = t.find("table", class_="memname")
        labels = [x.get_text(strip=True) for x in t.select("span.mlabel")]
        if inner is not None:
            _memname(inner, b, labels)
        return
    sep = ": " if classes & PARAM_TABLES else " | "
    out: list[str] = []
    for tr in _rows(t):
        texts = [x for x in (_line(c) for c in tr.find_all(["td", "th"], recursive=False)) if x]
        if texts:
            out.append(sep.join(texts) if len(texts) > 1 else texts[0])
    if out:
        b.add("\n".join(out))


def _list(el: Tag, b: Builder) -> None:
    lines: list[str] = []

    def rec(lst: Tag, depth: int) -> None:
        n = 0
        for li in lst.find_all("li", recursive=False):
            n += 1
            parts: list[str] = []
            nested: list[Tag] = []
            for ch in li.children:
                if isinstance(ch, Comment):
                    continue
                if isinstance(ch, NavigableString):
                    parts.append(_WS.sub(" ", str(ch)))
                elif isinstance(ch, Tag):
                    if ch.name in ("ul", "ol"):
                        nested.append(ch)
                    elif ch.name in _DROP_SET or _skip(ch):
                        continue
                    elif ch.name == "br":
                        parts.append("\n")
                    elif ch.name in BLOCKS:
                        parts.append("\n" + _itext(ch))
                    elif ch.name in ("code", "tt", "kbd", "samp"):
                        parts.append("`" + _itext(ch).strip() + "`")
                    else:
                        parts.append(_itext(ch))
            text = _clean("".join(parts)).replace("\n", " ")
            marker = f"{n}." if lst.name == "ol" else "-"
            if text:
                lines.append("  " * depth + marker + " " + text)
            for nl in nested:
                rec(nl, depth + 1)

    rec(el, 0)
    if lines:
        b.add("\n".join(lines))


def _dl(dl: Tag, b: Builder) -> None:
    for ch in dl.children:
        if not isinstance(ch, Tag) or _skip(ch):
            continue
        if ch.name == "dt":
            label = _line(ch)
            if label:
                b.add(label + ":")
        elif ch.name == "dd":
            _walk(ch, b)


def _walk(el: Tag, b: Builder) -> None:
    inline: list[str] = []

    def flush() -> None:
        if inline:
            txt = _clean("".join(inline))
            inline.clear()
            if txt and not _NOISE.match(txt):
                b.add(txt)

    for ch in el.children:
        if isinstance(ch, Comment):
            continue
        if isinstance(ch, NavigableString):
            inline.append(_WS.sub(" ", str(ch)))
            continue
        if not isinstance(ch, Tag) or ch.name in _DROP_SET or _skip(ch):
            continue
        name = ch.name
        if name == "br":
            inline.append("\n")
        elif name in HEADINGS:
            flush()
            _heading(ch, b)
        elif name == "pre" or "fragment" in (ch.get("class") or []):
            flush()
            _code(ch, b)
        elif name == "table":
            flush()
            _table(ch, b)
        elif name in ("ul", "ol"):
            flush()
            _list(ch, b)
        elif name == "dl":
            flush()
            _dl(ch, b)
        elif name in BLOCKS:
            flush()
            if name == "blockquote":
                sub = Builder()
                _walk(ch, sub)
                for s in sub.sections:
                    for blk in s.blocks:
                        b.add("\n".join("> " + ln for ln in blk.split("\n")))
            else:
                _walk(ch, b)
        elif name in ("code", "tt", "kbd", "samp"):
            t = _itext(ch).strip()
            if t:
                inline.append("`" + t.replace("`", "'") + "`")
        else:
            inline.append(_itext(ch))
    flush()


# --------------------------------------------------------------------------- page level
def _find_root(soup: BeautifulSoup) -> Tag:
    for sel in ROOT_SELECTORS:
        el = soup.select_one(sel)
        if el is not None and len(el.get_text(strip=True)) >= 200:
            return el
    return soup.body or soup


def _page_title(soup: BeautifulSoup, root: Tag) -> str:
    el = soup.select_one(".headertitle .title") or soup.select_one("div.title")
    if el is not None:
        t = _line(el)
        if t:
            return t
    h1 = root.find("h1") or soup.find("h1")
    if h1 is not None:
        t = _line(h1)
        if t:
            return t
    if soup.title is not None:
        t = _clean(soup.title.get_text())
        t = re.split(r"\s+[|–—-]\s+", t)[0] if t else t
        return t
    return ""


def _parse(data: bytes, stem: str) -> ParsedDoc | None:
    soup = BeautifulSoup(data, _PARSER)
    for t in soup.find_all(DROP_TAGS):
        t.decompose()
    root = _find_root(soup)
    title = _page_title(soup, root) or stem
    b = Builder(title)
    _walk(root, b)
    sections = b.finish()
    if sum(len(x) for s in sections for x in s.blocks) < 40:
        return None
    return ParsedDoc(title=title, sections=sections, kind="html")


def load_html(path: Path) -> ParsedDoc | None:
    data = path.read_bytes()
    try:
        return _parse(data, path.stem)
    except RecursionError:  # absurdly nested markup: fall back to plain text
        soup = BeautifulSoup(data, "html.parser")
        for t in soup.find_all(DROP_TAGS):
            t.decompose()
        text = _clean(soup.get_text("\n"))
        if len(text) < 40:
            return None
        b = Builder(path.stem)
        for para in re.split(r"\n\s*\n", text):
            b.add(para)
        return ParsedDoc(title=path.stem, sections=b.finish(), kind="html")
