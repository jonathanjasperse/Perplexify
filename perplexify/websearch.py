"""Web search for Research mode, and the `web_search` tool of the MCP server.

The connected model plans the searches (see rag.py); this module runs them and reads the pages:

* search: Tavily or Brave Search when a key is saved (Settings > Search > Web research), else DuckDuckGo's
  HTML page, which needs no key but refuses automated searches from time to time. A refusal is reported,
  never worked around.
* read: the best results are downloaded in parallel and their main text is extracted with the same HTML
  loader the libraries use. Only the passages that match the query best are kept, so many pages fit the window.
* safety: only public http(s) addresses are opened. Every redirect is checked again, so a result can never
  point the app at this computer or the local network.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from . import __version__
from .retrieval import Hit
from .textutil import search_terms

log = logging.getLogger("perplexify.websearch")

# Pages are read under the app's own name, never disguised as a browser, with the project's address as the contact
# that sites such as Wikipedia require (they refuse requests without one).
PROJECT_URL = "https://github.com/jonathanjasperse/Perplexify"
USER_AGENT = f"Perplexify/{__version__} (+{PROJECT_URL}; desktop research assistant) python-httpx/{httpx.__version__}"
MAX_PAGE_BYTES = 3 * 1024 * 1024
PAGE_TIMEOUT = 12.0
KEY_SERVICES = ("tavily", "brave")          # the keyed search services, best first
ENV_KEYS = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY"}


class WebSearchError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str = ""


# --------------------------------------------------------------------------- keys
def service_key(settings, service: str) -> str:
    """A saved key (Settings > Search), else the service's usual environment variable."""
    key = ""
    if settings is not None:
        try:
            key = settings.service_key(service)
        except Exception:  # noqa: BLE001 - a broken secret store must not stop the fallback
            key = ""
    return key or os.environ.get(ENV_KEYS.get(service, ""), "").strip()


def engine_name(settings) -> str:
    for s in KEY_SERVICES:
        if service_key(settings, s):
            return s
    return "duckduckgo"


# --------------------------------------------------------------------------- search back ends
def _tavily(query: str, key: str, n: int) -> list[WebResult]:
    r = httpx.post("https://api.tavily.com/search", timeout=20,
                   headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT},
                   json={"query": query, "max_results": n, "search_depth": "basic"})
    if r.status_code in (401, 403):
        raise WebSearchError("Tavily rejected the API key. Check it in Settings > Search.")
    if r.status_code == 429 or r.status_code == 432:
        raise WebSearchError("Tavily's search limit was reached. Wait a moment, or check your plan.")
    if r.status_code >= 400:
        raise WebSearchError(f"Tavily returned {r.status_code}.")
    return [WebResult(x.get("title") or x.get("url", ""), x.get("url", ""), x.get("content") or "")
            for x in r.json().get("results", []) if x.get("url")]


def _brave(query: str, key: str, n: int) -> list[WebResult]:
    r = httpx.get("https://api.search.brave.com/res/v1/web/search", timeout=20, params={"q": query, "count": n},
                  headers={"X-Subscription-Token": key, "Accept": "application/json", "User-Agent": USER_AGENT})
    if r.status_code in (401, 403, 422):
        raise WebSearchError("Brave Search rejected the API key. Check it in Settings > Search.")
    if r.status_code == 429:
        raise WebSearchError("Brave Search's rate limit was reached. Wait a moment and try again.")
    if r.status_code >= 400:
        raise WebSearchError(f"Brave Search returned {r.status_code}.")
    out = []
    for x in (r.json().get("web") or {}).get("results", []):
        snippet = " ".join([x.get("description") or ""] + list(x.get("extra_snippets") or []))
        out.append(WebResult(_strip_tags(x.get("title") or x.get("url", "")), x.get("url", ""), _strip_tags(snippet)))
    return [w for w in out if w.url]


def _duckduckgo(query: str, n: int) -> list[WebResult]:
    from bs4 import BeautifulSoup

    r = httpx.post("https://html.duckduckgo.com/html/", data={"q": query}, timeout=15, follow_redirects=True,
                   headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for d in soup.select("div.result"):
        if "result--ad" in (d.get("class") or []):
            continue
        a = d.select_one("a.result__a")
        if a is None:
            continue
        href = a.get("href") or ""
        if "uddg=" in href:                                  # DuckDuckGo's own redirect: the real address is in uddg
            href = unquote(parse_qs(urlparse(urljoin("https://duckduckgo.com", href)).query).get("uddg", [""])[0])
        sn = d.select_one(".result__snippet")
        if href.startswith(("http://", "https://")):
            out.append(WebResult(a.get_text(" ", strip=True), href, sn.get_text(" ", strip=True) if sn else ""))
        if len(out) >= n:
            break
    if not out and (r.status_code == 202 or "anomaly" in r.text.lower()):
        raise WebSearchError("DuckDuckGo is refusing automated searches from this connection right now. Add a free "
                             "Tavily or Brave Search key in Settings > Search > Web research.")
    return out


def search(query: str, settings=None, n: int = 6) -> tuple[list[WebResult], str]:
    """(results, the engine that found them). Raises WebSearchError with a readable message."""
    query = query.strip()[:400]
    if not query:
        return [], ""
    try:
        for s in KEY_SERVICES:
            key = service_key(settings, s)
            if key:
                return (_tavily if s == "tavily" else _brave)(query, key, n), s
        return _duckduckgo(query, n), "duckduckgo"
    except httpx.HTTPError as e:
        raise WebSearchError(f"Web search failed: could not reach the search service ({type(e).__name__}).") from e


# --------------------------------------------------------------------------- reading pages
def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").replace("&quot;", '"').replace("&#x27;", "'").replace("&amp;", "&").strip()


def is_public_url(url: str) -> bool:
    """http(s) on the public internet: not this computer, the local network, or a reserved range."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.strip("[]").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return False
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def fetch_page(url: str, client: httpx.Client) -> tuple[str, bytes] | None:
    """(final url, html bytes) of a public web page, following up to 4 redirects that are each checked again."""
    for _ in range(5):
        if not is_public_url(url):
            return None
        with client.stream("GET", url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.3",
                                                "Accept-Language": "en"}) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            ctype = r.headers.get("content-type", "").lower()
            if r.status_code >= 400 or not ("html" in ctype or "text/plain" in ctype):
                return None
            chunks, size = [], 0
            for part in r.iter_bytes():
                size += len(part)
                if size > MAX_PAGE_BYTES:
                    break
                chunks.append(part)
            return str(r.url), b"".join(chunks)
    return None


def page_passages(data: bytes, url: str) -> tuple[str, list[tuple[str, str]]]:
    """(title, [(heading, text)]) of a downloaded page, using the library HTML loader."""
    from .chunking import chunk_document
    from .loaders.html import _parse

    stem = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1] or urlparse(url).hostname or "page"
    doc = _parse(data, stem)
    if doc is None:
        return "", []
    return doc.title, [(c.heading, c.text) for c in chunk_document(doc)]


def _best_text(passages: list[tuple[str, str]], terms: list[str], budget_chars: int) -> str:
    """The passages that share the most words with the query, kept in page order, up to `budget_chars`."""
    def score(i_p):
        i, (head, text) = i_p
        low = (head + " " + text).lower()
        return sum(low.count(t) for t in terms) + (0.5 if i == 0 else 0)   # the opening passage usually sets the scene
    ranked = sorted(enumerate(passages), key=score, reverse=True)
    keep, used = [], 0
    for i, (_head, text) in ranked:
        if used + len(text) > budget_chars and keep:
            continue
        keep.append(i)
        used += len(text)
        if used >= budget_chars:
            break
    return "\n\n".join(passages[i][1] for i in sorted(keep))[:budget_chars]


def _domain(url: str) -> str:
    host = urlparse(url).hostname or url
    return host[4:] if host.startswith("www.") else host


def research(queries: list[str], settings=None, *, per_query: int = 5, max_pages: int = 8,
             chars_per_page: int = 3500, question: str = "") -> tuple[list[Hit], dict]:
    """Run the searches, read the best pages, return them as passages (`Hit`, kind "web") plus a summary for the trace."""
    found: list[WebResult] = []
    seen: set[str] = set()
    engine, errors = "", []
    per: list[list[WebResult]] = []
    for q in queries[:4]:
        try:
            res, engine = search(q, settings, per_query)
        except WebSearchError as e:
            errors.append(str(e))
            continue
        per.append(res)
    for rank in range(per_query):                             # take the results round robin, so every search contributes
        for res in per:
            if rank < len(res):
                key = res[rank].url.split("#")[0].rstrip("/")
                if key not in seen:
                    seen.add(key)
                    found.append(res[rank])
    found = found[:max_pages]
    if not found:
        if errors:
            raise WebSearchError(errors[0])
        return [], {"engine": engine, "results": 0, "pages": 0}

    terms = [t.lower() for t in search_terms(" ".join([question] + queries))][:30]

    def read(w: WebResult) -> tuple[WebResult, str, str, bool]:
        try:
            with httpx.Client(timeout=httpx.Timeout(PAGE_TIMEOUT, connect=6), follow_redirects=False) as c:
                got = fetch_page(w.url, c)
            if got:
                title, passages = page_passages(got[1], got[0])
                text = _best_text(passages, terms, chars_per_page)
                if len(text) >= 200:
                    return w, title or w.title, text, True
        except Exception as e:  # noqa: BLE001 - one unreadable page must not end the research
            log.debug("could not read %s: %s", w.url, e)
        return w, w.title, w.snippet, False                    # fall back to what the search engine said about it

    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="web") as pool:
        pages = list(pool.map(read, found))
    hits: list[Hit] = []
    read_n = 0
    for i, (w, title, text, was_read) in enumerate(pages):
        if not text.strip():
            continue
        read_n += was_read
        hits.append(Hit(library_id="web", library_name=_domain(w.url), chunk_id=-(i + 1), doc_id=-(i + 1),
                        title=_strip_tags(title or w.title) or _domain(w.url), uri=w.url, heading=_strip_tags(title or w.title),
                        anchor=None, text=text.strip(), origin="web", path=None, kind="web", score=round(1.0 - i * 0.05, 3)))
    return hits, {"engine": engine, "results": len(found), "pages": read_n, "errors": errors[:2]}
