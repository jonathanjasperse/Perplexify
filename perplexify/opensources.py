"""Web search that needs no key and no setup: open sources that exist to be queried by programs.

Research mode used to depend on a search service key (Tavily, Brave) or on DuckDuckGo, which refuses automated searches from
time to time. Here a search asks several open sources at once and keeps what is on topic:

    Wikipedia       encyclopedia articles
    PubMed          biomedical and life-science papers, with their abstracts
    arXiv           preprints in computer science, statistics, physics and engineering
    OpenAlex        scholarly papers from every field, with abstracts
    Stack Overflow  programming questions with their best answers
    GitHub          open-source projects
    DuckDuckGo      ordinary web pages (best effort)

How it stays a good guest, and reliable:

* every service is asked at the pace it publishes (arXiv: one request every three seconds; NCBI: three a second ...), under the
  app's own name with the project's address, never disguised as a browser;
* a service that refuses (rate limit, "too many requests", an automated-traffic page) is reported and then left alone for a while
  instead of being asked again at once. A refusal is never worked around;
* the same question is not asked twice within fifteen minutes;
* one source failing, or being switched off, never stops the others.

What comes back is a list of `WebResult`. A result that arrives with its text (an abstract, an answer) carries it in `text`, so the
page behind it is not downloaded; the others (Wikipedia, GitHub, DuckDuckGo) are read like any web page by websearch.py.
"""
from __future__ import annotations

import html as _html
import logging
import re
import threading
import time
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import httpx

from . import __version__
from .textutil import STOPWORDS

log = logging.getLogger("perplexify.opensources")

# Services are asked under the app's own name, never disguised as a browser, with the project's address as the contact that
# Wikipedia, NCBI and others ask for.
PROJECT_URL = "https://github.com/jonathanjasperse/Perplexify"
USER_AGENT = f"Perplexify/{__version__} (+{PROJECT_URL}; desktop research assistant) python-httpx/{httpx.__version__}"

REQUEST_TIMEOUT = httpx.Timeout(12.0, connect=5.0)
DEADLINE = 28.0                      # seconds after which a search stops waiting for slow sources
CACHE_SECONDS = 900
CACHE_ENTRIES = 256


class WebSearchError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


class SourceError(RuntimeError):
    """One source cannot answer right now (refused, rate limited, unreachable). The message is safe to show.
    `block` is how long to leave the source alone; `network` is true when it could not be reached at all."""

    def __init__(self, message: str, block: float = 0.0, network: bool = False):
        super().__init__(message)
        self.block = block
        self.network = network


@dataclass
class WebResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""        # the name shown for it ("PubMed"); empty for an ordinary web page (its site is shown instead)
    text: str = ""          # text that came with the result (an abstract, an answer): used as it is, the page is not downloaded
    ident: str = ""         # a DOI, PubMed id or arXiv id, to recognise the same paper from two sources


# --------------------------------------------------------------------------- the query
_WORD = re.compile(r"\d+(?:\.\d+)+|[^\W_][\w'\-]*", re.UNICODE)       # words (hyphens kept) and version numbers such as 1.80
_FILLER = frozenset("""work works working explain explained tutorial guide example examples way ways thing things overview
introduction basics basic really actually between difference differences versus vs compare comparison latest recent new best
good need needs want like just also one two""".split())


def _specific(word: str) -> bool:
    """An acronym (ECG), a number (1.80, MIT-BIH-2), or a CamelCase name (PowerShell): a word that says what the search is about."""
    return (len(word) >= 2 and word.isupper()) or any(c.isdigit() for c in word) or any(c.isupper() for c in word[1:])


@dataclass(frozen=True)
class Query:
    """A search as the sources need it: the plain text, its significant words, and the most specific words first."""
    text: str
    words: tuple[str, ...]          # the words as written, in order, without filler
    terms: tuple[str, ...]          # the same, lower case
    must: tuple[str, ...] = ()      # the terms a result has to contain to be about this: the specific ones, and the first (the subject)

    @staticmethod
    def parse(text: str) -> "Query":
        text = " ".join((text or "").split())[:400]
        words, seen = [], set()
        for m in _WORD.finditer(text):
            w = m.group(0).strip("-'")
            low = w.lower()
            if not w or low in STOPWORDS or low in _FILLER or (len(low) < 2 and not low.isdigit()) or low in seen:
                continue
            seen.add(low)
            words.append(w)
        terms = tuple(w.lower() for w in words)
        must = tuple(dict.fromkeys(([terms[0]] if terms else []) + [w.lower() for w in words if _specific(w)]))
        return Query(text, tuple(words), terms, must)

    def lead(self, k: int) -> list[str]:
        """The `k` most specific terms (acronyms, numbers and longer words first), in the order they were written. Services
        that need every word to match (GitHub, Stack Overflow, arXiv) get few and well chosen ones."""
        if not self.terms:
            return [self.text] if self.text else []

        def weight(i: int) -> tuple[int, int]:
            w = self.words[i]
            return (-(2 * _specific(w) + (len(w) >= 6)), -len(w), i)               # longer words say more than short ones
        keep = sorted(sorted(range(len(self.terms)), key=weight)[:k])
        return [self.terms[i] for i in keep]


def _stem(term: str) -> str:
    return re.sub(r"(ing|ed|es|s)$", "", term) if len(term) > 5 else term


def coverage(q: Query, w: WebResult) -> tuple[float, int, float]:
    """How on topic a result is: (the share of the query's terms found in it, how many, the share of the must-have terms found)."""
    if not q.terms:
        return 1.0, 0, 1.0
    hay = " ".join([w.title, w.snippet, w.text[:2500]]).lower()
    words = set(_WORD.findall(hay))

    def has(t: str) -> bool:
        # a word of up to three letters ("ai", "ecg") must be there as a word, not inside another one ("main", "second")
        return t in words if len(t) < 4 else _stem(t) in hay
    hits = sum(1 for t in q.terms if has(t))
    must = sum(1 for t in q.must if has(t)) / len(q.must) if q.must else 1.0
    return hits / len(q.terms), hits, must


# --------------------------------------------------------------------------- manners: pace, pause, remember
_clock = time.monotonic
_sleep = time.sleep
_lock = threading.Lock()
_next_ok: dict[str, float] = {}
_blocked: dict[str, tuple[float, str]] = {}
_cache: "OrderedDict[tuple, tuple[float, list[WebResult]]]" = OrderedDict()


def _pace(key: str, interval: float) -> None:
    """Wait so that requests to `key` are at least `interval` seconds apart (the services ask for this)."""
    if interval <= 0:
        return
    with _lock:
        now = _clock()
        at = max(now, _next_ok.get(key, 0.0))
        _next_ok[key] = at + interval
    if at > now:
        _sleep(at - now)


def _block(source: str, seconds: float, why: str) -> None:
    if seconds > 0:
        with _lock:
            _blocked[source] = (_clock() + seconds, why)


def _why_blocked(source: str) -> str | None:
    with _lock:
        item = _blocked.get(source)
        if item and item[0] > _clock():
            return item[1]
        _blocked.pop(source, None)
    return None


def _cached(key: tuple) -> list[WebResult] | None:
    with _lock:
        item = _cache.get(key)
        if item and _clock() - item[0] < CACHE_SECONDS:
            _cache.move_to_end(key)
            return [replace(w) for w in item[1]]
        _cache.pop(key, None)
    return None


def _remember(key: tuple, results: list[WebResult]) -> None:
    with _lock:
        _cache[key] = (_clock(), [replace(w) for w in results])
        while len(_cache) > CACHE_ENTRIES:
            _cache.popitem(last=False)


def reset() -> None:
    """Forget what was asked, paused and remembered (used by the tests)."""
    with _lock:
        _next_ok.clear()
        _blocked.clear()
        _cache.clear()


# --------------------------------------------------------------------------- small helpers
_TAGS = re.compile(r"<[^>]+>")


def _plain(s: str) -> str:
    return " ".join(_html.unescape(_TAGS.sub("", s or "")).split())


def _check(r: httpx.Response) -> None:
    """Turn an error answer into a SourceError. The messages continue the source's name: "PubMed is busy right now"."""
    if r.status_code < 400:
        return
    if r.status_code in (429, 503):
        raise SourceError("is busy right now", block=120)
    if r.status_code in (401, 403):
        raise SourceError("refused the request", block=300)
    raise SourceError(f"answered {r.status_code}", block=30)


def _json(r: httpx.Response):
    _check(r)
    try:
        return r.json()
    except ValueError as e:
        raise SourceError("answered in a way Perplexify cannot read", block=60) from e


def _first(fetch: Callable[[list[str]], list[WebResult]], q: Query, sizes: tuple[int, ...]) -> list[WebResult]:
    """Ask with the `sizes[0]` most specific terms; when that finds nothing, with fewer (every word has to match)."""
    tried: list[list[str]] = []
    for k in sizes:
        terms = q.lead(k)
        if terms in tried:
            continue
        tried.append(terms)
        found = fetch(terms)
        if found:
            return found
    return []


def _xml(data: bytes):
    from lxml import etree

    # entities are not resolved, nothing is fetched, no DTD is loaded: a service (or something in between) cannot make the
    # parser read files or expand into gigabytes
    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False))


def _norm_ident(doi: str) -> str:
    doi = (doi or "").lower().replace("https://doi.org/", "").replace("http://dx.doi.org/", "").strip()
    return "arxiv:" + doi[len("10.48550/arxiv."):] if doi.startswith("10.48550/arxiv.") else doi


def _names(authors: list[str], limit: int = 3) -> str:
    authors = [a for a in authors if a]
    return ", ".join(authors[:limit]) + (" et al." if len(authors) > limit else "")


# --------------------------------------------------------------------------- the sources
def _wikipedia(client: httpx.Client, q: Query, n: int) -> list[WebResult]:
    r = client.get("https://en.wikipedia.org/w/rest.php/v1/search/page", params={"q": q.text, "limit": min(n, 10)})
    out = []
    for p in _json(r).get("pages", []):
        key = p.get("key") or ""
        if not key:
            continue
        desc = _plain(p.get("description") or "")
        out.append(WebResult(_plain(p.get("title") or key.replace("_", " ")), "https://en.wikipedia.org/wiki/" + quote(key, safe="()_,:'!%"),
                             (desc + ". " if desc else "") + _plain(p.get("excerpt") or ""), "Wikipedia"))
    return out


def _pubmed(client: httpx.Client, q: Query, n: int) -> list[WebResult]:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

    def ids_for(terms: list[str]) -> list[str]:
        _pace("ncbi", 0.4)                                                # NCBI allows three requests a second without a key
        r = client.get(base + "esearch.fcgi", params={"db": "pubmed", "term": " ".join(terms), "retmax": n, "retmode": "json",
                                                      "sort": "relevance", "tool": "Perplexify"})
        return [str(i) for i in (_json(r).get("esearchresult") or {}).get("idlist", [])]

    ids = _first(lambda terms: [WebResult("", "", ident=i) for i in ids_for(terms)], q, (5, 3, 2))
    if not ids:
        return []
    _pace("ncbi", 0.4)
    r = client.get(base + "efetch.fcgi", params={"db": "pubmed", "id": ",".join(w.ident for w in ids), "retmode": "xml", "tool": "Perplexify"})
    _check(r)
    out = []
    for art in _xml(r.content).iter("PubmedArticle"):
        pmid = art.findtext(".//MedlineCitation/PMID") or ""
        title = _plain("".join(art.find(".//ArticleTitle").itertext())) if art.find(".//ArticleTitle") is not None else ""
        parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label") or ""
            parts.append((label.capitalize() + ": " if label else "") + _plain("".join(ab.itertext())))
        abstract = " ".join(p for p in parts if p.strip())
        if not (pmid and title and abstract):
            continue                                                      # nothing to read: not worth a place among the sources
        authors = []
        for a in art.findall(".//AuthorList/Author"):
            who = a.findtext("CollectiveName") or " ".join(x for x in (a.findtext("LastName"), a.findtext("Initials")) if x)
            authors.append(who)
        journal = art.findtext(".//Journal/Title") or ""
        year = (art.findtext(".//Journal/JournalIssue/PubDate/Year") or (art.findtext(".//Journal/JournalIssue/PubDate/MedlineDate") or "")[:4]
                or art.findtext(".//ArticleDate/Year") or "")
        doi = next((x.text or "" for x in art.findall(".//ArticleIdList/ArticleId") if x.get("IdType") == "doi"), "")
        head = f"{title}\n{_names(authors)} - {journal}" + (f" ({year})" if year else "") + f" - PMID {pmid}"
        out.append(WebResult(title, f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", abstract[:300], "PubMed",
                             f"{head}\n\n{abstract}", _norm_ident(doi) or "pmid:" + pmid))
    return out


def _arxiv(client: httpx.Client, q: Query, n: int) -> list[WebResult]:
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}

    def fetch(terms: list[str]) -> list[WebResult]:
        r = client.get("https://export.arxiv.org/api/query", params={
            "search_query": " AND ".join("all:" + t for t in terms), "start": 0, "max_results": n, "sortBy": "relevance"})
        _check(r)
        out = []
        for e in _xml(r.content).findall("a:entry", ns):
            title = _plain(e.findtext("a:title", namespaces=ns) or "")
            summary = _plain(e.findtext("a:summary", namespaces=ns) or "")
            raw = e.findtext("a:id", namespaces=ns) or ""
            code = re.sub(r"v\d+$", "", raw.split("/abs/")[-1])
            if not (title and summary and "/abs/" in raw):
                continue
            authors = [_plain(a.findtext("a:name", namespaces=ns) or "") for a in e.findall("a:author", ns)]
            cat = (e.find("x:primary_category", ns).get("term") if e.find("x:primary_category", ns) is not None else "")
            date = (e.findtext("a:published", namespaces=ns) or "")[:10]
            head = f"{title}\n{_names(authors)} - arXiv {code}" + (f" [{cat}]" if cat else "") + (f" - {date}" if date else "")
            out.append(WebResult(title, "https://arxiv.org/abs/" + code, summary[:300], "arXiv", f"{head}\n\n{summary}", "arxiv:" + code.lower()))
        return out
    return _first(fetch, q, (4, 2))


def _abstract_from_index(index: dict | None) -> str:
    """OpenAlex ships abstracts as {word: [positions]}; put the words back in order."""
    if not index:
        return ""
    slots: dict[int, str] = {}
    for word, positions in index.items():
        for p in positions or []:
            if isinstance(p, int):
                slots[p] = word
    return " ".join(slots[i] for i in sorted(slots))


def _openalex(client: httpx.Client, q: Query, n: int) -> list[WebResult]:
    r = client.get("https://api.openalex.org/works", params={
        "search": " ".join(q.terms[:10]) or q.text, "per-page": n, "filter": "has_abstract:true",
        "select": "id,doi,title,publication_year,abstract_inverted_index,primary_location,authorships,cited_by_count"})
    out = []
    for w in _json(r).get("results", []):
        title = _plain(w.get("title") or "")
        abstract = _plain(_abstract_from_index(w.get("abstract_inverted_index")))
        url = w.get("doi") or w.get("id") or ""
        if not (title and abstract and url):
            continue
        authors = [(a.get("author") or {}).get("display_name") or "" for a in w.get("authorships") or []]
        venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        year, cites = w.get("publication_year"), w.get("cited_by_count")
        head = f"{title}\n{_names(authors)}" + (f" - {venue}" if venue else "") + (f" ({year})" if year else "") + (f" - cited by {cites}" if cites else "")
        out.append(WebResult(title, url, abstract[:300], "OpenAlex", f"{head}\n\n{abstract}", _norm_ident(w.get("doi") or "")))
    return out


def _html_text(fragment: str, limit: int) -> str:
    """Text of a Stack Exchange post (HTML), with code kept in fenced blocks."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(fragment or "", "lxml")
    for pre in soup.find_all("pre"):
        pre.replace_with("\n```\n" + pre.get_text().strip("\n") + "\n```\n")
    text = re.sub(r"[ \t]+", " ", soup.get_text(" "))
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:limit]


def _se(client: httpx.Client, path: str, params: dict) -> dict:
    r = client.get("https://api.stackexchange.com/2.3" + path, params={"site": "stackoverflow", **params})
    data = _json(r)
    if data.get("error_id"):
        raise SourceError("is busy right now", block=300)
    if data.get("backoff"):
        _block("stackoverflow", float(data["backoff"]) + 1, "asked to wait a little")
    if isinstance(data.get("quota_remaining"), int) and data["quota_remaining"] < 3:
        _block("stackoverflow", 3600, "has used up its daily allowance for searches without signing in")
    return data


def _stackoverflow(client: httpx.Client, q: Query, n: int) -> list[WebResult]:
    def fetch(terms: list[str]) -> list[WebResult]:
        data = _se(client, "/search/advanced", {"order": "desc", "sort": "relevance", "q": " ".join(terms), "pagesize": min(n, 6),
                                                "filter": "withbody"})
        return [WebResult(_plain(it.get("title") or ""), it.get("link") or "", ident=str(it.get("question_id")), text=it.get("body") or "")
                for it in data.get("items", []) if it.get("answer_count", 0) > 0 and it.get("link")]
    found = _first(fetch, q, (4, 2))[:3]
    if not found:
        return []
    ids = ";".join(w.ident for w in found)
    answers = _se(client, f"/questions/{ids}/answers", {"order": "desc", "sort": "votes", "pagesize": 30, "filter": "withbody"}).get("items", [])
    best: dict[str, dict] = {}
    for a in answers:                                                     # most voted first; an accepted answer wins
        qid = str(a.get("question_id"))
        if qid not in best or (a.get("is_accepted") and not best[qid].get("is_accepted")):
            best[qid] = a
    out = []
    for w in found:
        a = best.get(w.ident)
        if a is None:
            continue
        question = _html_text(w.text, 1200)
        answer = _html_text(a.get("body") or "", 2400)
        tag = f"score {a.get('score', 0)}" + (", accepted" if a.get("is_accepted") else "")
        out.append(WebResult(w.title, w.url, question[:300], "Stack Overflow",
                             f"{w.title}\n\nQuestion:\n{question}\n\nAnswer ({tag}):\n{answer}", "so:" + w.ident))
    return out


def _github(client: httpx.Client, q: Query, n: int) -> list[WebResult]:
    def fetch(terms: list[str]) -> list[WebResult]:
        r = client.get("https://api.github.com/search/repositories", params={"q": " ".join(terms), "per_page": min(n, 6)},
                       headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        if r.status_code in (403, 429) or r.headers.get("x-ratelimit-remaining") == "0":
            try:
                wait_s = max(30.0, min(900.0, float(r.headers.get("x-ratelimit-reset", "0")) - time.time()))
            except ValueError:
                wait_s = 300.0
            if r.status_code in (403, 429):
                raise SourceError("reached its limit for searches without signing in (a few a minute)", block=wait_s)
            _block("github", wait_s, "reached its limit for searches without signing in (a few a minute)")
        out = []
        for it in _json(r).get("items", []):
            desc = _plain(it.get("description") or "")
            extra = ", ".join(x for x in [it.get("language") or "", f"{it.get('stargazers_count', 0):,} stars"] + list(it.get("topics") or [])[:6] if x)
            if it.get("html_url"):
                out.append(WebResult(it.get("full_name") or it["html_url"], it["html_url"], (desc + " " if desc else "") + f"({extra})", "GitHub"))
        return out
    return _first(fetch, q, (3, 2))


def _duckduckgo(client: httpx.Client, q: Query, n: int) -> list[WebResult]:
    from bs4 import BeautifulSoup

    r = client.post("https://html.duckduckgo.com/html/", data={"q": q.text}, headers={"Accept-Language": "en"})
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
    if not out:
        if r.status_code in (202, 403, 429) or "anomaly" in r.text.lower():
            # reported and then left alone for a while: asking again at once only makes the refusal last longer
            raise SourceError("is refusing automated searches from this connection right now", block=900)
        _check(r)
    return out


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    about: str                      # one line for the settings page
    fn: Callable
    interval: float                 # seconds between two requests to it
    queries: int                    # how many of a question's searches it is asked (the first ones)
    cap: int                        # at most this many of its results are kept per search
    prior: float                    # how much it is trusted when results tie
    min_hits: int                   # a result must contain at least this many of the query's terms ...
    min_cov: float                  # ... and at least this share of them
    strict: bool = False            # ... and, for sources that fill up with near misses, the query's must-have terms (see Query.must)


SOURCES: dict[str, Source] = {s.id: s for s in (
    Source("wikipedia", "Wikipedia", "encyclopedia articles", _wikipedia, 0.15, 4, 2, 0.80, 1, 0.0),
    Source("pubmed", "PubMed", "biomedical and life-science papers (abstracts)", _pubmed, 0.0, 3, 3, 0.90, 2, 0.34, True),
    Source("arxiv", "arXiv", "preprints in computer science, statistics, physics and engineering", _arxiv, 3.1, 1, 3, 0.80, 2, 0.34, True),
    Source("openalex", "OpenAlex", "scholarly papers from every field (abstracts)", _openalex, 0.2, 3, 3, 0.70, 2, 0.34, True),
    Source("stackoverflow", "Stack Overflow", "programming questions with their best answers", _stackoverflow, 0.3, 2, 2, 0.70, 2, 0.34, True),
    Source("github", "GitHub", "open-source projects", _github, 1.0, 2, 2, 0.60, 1, 0.30, True),
    Source("duckduckgo", "DuckDuckGo", "ordinary web pages (best effort: it sometimes refuses automated searches)", _duckduckgo, 1.2, 4, 4, 0.80, 0, 0.0),
)}
SOURCE_IDS = tuple(SOURCES)


# --------------------------------------------------------------------------- asking, filtering, merging
@dataclass
class OpenSearch:
    per_query: list[list[WebResult]] = field(default_factory=list)   # the kept results of each search, best first
    used: list[str] = field(default_factory=list)                    # names of the sources that contributed, in a fixed order
    counts: dict[str, int] = field(default_factory=dict)             # name -> results kept
    errors: dict[str, str] = field(default_factory=dict)             # name -> why it could not answer
    asked: list[str] = field(default_factory=list)                   # names of the sources that were asked
    offline: bool = False                                            # every source that was asked could not be reached at all

    @property
    def label(self) -> str:
        return ", ".join(self.used)

    def explain(self) -> str:
        """What to tell the user when nothing was found."""
        if not self.asked:
            return "Every web source is switched off. Turn some on in Settings > Search > Web research."
        if self.offline:
            return "Web search could not reach the internet. Check the connection and try again."
        if self.errors:
            why = "; ".join(f"{name} {msg}" for name, msg in list(self.errors.items())[:3])
            return f"Web search found nothing ({why})."
        return "The web sources had nothing on this."


def _relevant(src: Source, q: Query, w: WebResult) -> bool:
    cov, hits, must = coverage(q, w)
    if hits < min(src.min_hits, len(q.terms)) or cov < src.min_cov:
        return False
    # with up to two must-have terms all of them have to be there; with more, two thirds of them
    return not src.strict or must >= (0.99 if len(q.must) <= 2 else 0.66)


def _dedupe_key(w: WebResult) -> str:
    return w.ident or re.sub(r"[^a-z0-9]+", "", w.title.lower())[:80] or w.url


def merge(q: Query, by_source: dict[str, list[WebResult]], limit: int) -> list[WebResult]:
    """The results of several sources as one list for one search: off-topic ones dropped, the best matches first, no source
    taking more than its share, the same paper from two sources once."""
    scored = []
    for sid, results in by_source.items():
        src = SOURCES[sid]
        for rank, w in enumerate(results):
            if not _relevant(src, q, w):
                continue
            cov = coverage(q, w)[0]
            scored.append((0.6 * cov + 0.25 / (1 + rank) + 0.15 * src.prior, rank, sid, w))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out, per_source, seen = [], Counter(), set()
    for _score, _rank, sid, w in scored:
        key = _dedupe_key(w)
        if per_source[sid] >= SOURCES[sid].cap or key in seen:
            continue
        seen.add(key)
        per_source[sid] += 1
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _run(client: httpx.Client, src: Source, q: Query, n: int) -> list[WebResult]:
    why = _why_blocked(src.id)
    if why:
        raise SourceError(why)
    key = (src.id, q.text.lower(), n)
    hit = _cached(key)
    if hit is not None:
        return hit
    _pace(src.id, src.interval)
    try:
        results = src.fn(client, q, n)
    except SourceError as e:
        _block(src.id, e.block, str(e))
        raise
    except httpx.HTTPError as e:                      # offline, a timeout, a certificate problem: do not ask again at once
        _block(src.id, 45, "could not be reached")
        raise SourceError("could not be reached", block=45, network=True) from e
    except Exception as e:  # noqa: BLE001 - a service that changed its answer must not break the search
        log.debug("%s: unexpected answer", src.name, exc_info=True)
        _block(src.id, 120, "answered in a way Perplexify cannot read")
        raise SourceError("answered in a way Perplexify cannot read", block=120) from e
    _remember(key, results)
    return results


def search_many(queries: list[str], enabled=None, per_query: int = 6, client: httpx.Client | None = None) -> OpenSearch:
    """Run every search against every enabled source at once. `enabled` is a set of source ids (None: all)."""
    parsed = [q for q in dict.fromkeys(Query.parse(x) for x in queries) if q.text][:4]
    sources = [s for s in SOURCES.values() if enabled is None or s.id in enabled]
    out = OpenSearch(per_query=[[] for _ in parsed])
    if not parsed or not sources:
        return out
    jobs = [(qi, s) for qi, q in enumerate(parsed) for s in sources if qi < s.queries]
    out.asked = [s.name for s in sources]
    own = client is None
    http = client or httpx.Client(timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    pool = ThreadPoolExecutor(max_workers=min(8, max(1, len(jobs))), thread_name_prefix="open")
    try:
        futures = {pool.submit(_run, http, s, parsed[qi], per_query + 2): (qi, s) for qi, s in jobs}
        done, late = wait(futures, timeout=DEADLINE)
        results: dict[tuple[int, str], list[WebResult]] = {}
        failures: dict[str, SourceError] = {}
        for fut in done:
            qi, s = futures[fut]
            try:
                results[(qi, s.id)] = fut.result()
            except SourceError as e:
                failures.setdefault(s.id, e)
        for fut in late:
            fut.cancel()
            failures.setdefault(futures[fut][1].id, SourceError("took too long", network=True))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        if own:
            http.close()
    for qi, q in enumerate(parsed):
        out.per_query[qi] = merge(q, {sid: res for (i, sid), res in results.items() if i == qi}, per_query)
    kept = Counter(w.source or "DuckDuckGo" for res in out.per_query for w in res)       # an ordinary web page comes from DuckDuckGo
    out.counts = {s.name: kept[s.name] for s in SOURCES.values() if kept[s.name]}
    out.used = list(out.counts)
    answered = {sid for (_i, sid) in results}
    out.errors = {SOURCES[sid].name: str(e) for sid, e in failures.items() if sid not in answered}
    out.offline = bool(failures) and not answered and all(e.network for e in failures.values())
    return out
