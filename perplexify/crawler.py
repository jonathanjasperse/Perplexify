"""Polite site downloader: mirrors a documentation website into a library's source folder.

* stays on the start host and under the start URL's folder (e.g. /master/)
* honours robots.txt and Crawl-delay, limits concurrency, backs off on 429/503
* resumable: pages already downloaded are re-read from disk instead of fetched again
* `refresh` re-checks pages with conditional requests (ETag / Last-Modified)
* optionally mirrors CSS / JS / images so pages can be browsed offline
"""
from __future__ import annotations

import hashlib
import html
import logging
import posixpath
import re
import time
import urllib.robotparser
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse

import httpx

from . import __version__
from .jobs import Cancelled, Job
from .library import Library
from .presets import DOXYGEN_CRAWL_EXCLUDE

log = logging.getLogger("perplexify.crawler")

USER_AGENT = f"Perplexify/{__version__} (+personal documentation indexer)"
MAX_PAGE_BYTES = 30 * 1024 * 1024
MAX_FILE_BYTES = 200 * 1024 * 1024
SKIP_EXT = {".zip", ".gz", ".tgz", ".tar", ".7z", ".rar", ".exe", ".msi", ".dmg", ".pkg", ".iso", ".mp4", ".mov",
            ".avi", ".mp3", ".wav", ".woff", ".woff2", ".ttf", ".otf", ".eot"}
ASSET_EXT = {".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp"}

_TAG = re.compile(rb"(?is)<(a|area|link|script|img)\b([^>]*)>")
_ATTR = re.compile(rb"""(?is)(?<![\w-])(href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_REL = re.compile(rb"""(?is)(?<![\w-])rel\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_BASE = re.compile(rb"""(?is)<base\b[^>]*?(?<![\w-])href\s*=\s*(?:"([^"]*)"|'([^']*)')""")


@dataclass
class CrawlOptions:
    start_url: str
    scope: str = "path"             # "path": stay under the start URL's folder; "host": whole site
    max_pages: int = 20000
    concurrency: int = 4
    delay: float = 0.25             # seconds each worker waits after a request
    exclude: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    download_assets: bool = True
    include_pdfs: bool = True
    respect_robots: bool = True
    refresh: bool = False

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class CrawlResult:
    pages: int = 0
    assets: int = 0
    cached: int = 0
    failed: int = 0
    bytes: int = 0
    doxygen: bool = False
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- URL helpers
def normalize_url(url: str) -> str | None:
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname.lower()
    netloc = host if (u.port in (None, 80, 443)) else f"{host}:{u.port}"
    path = u.path or "/"
    trailing = path.endswith("/")
    path = posixpath.normpath(path)
    if not path.startswith("/"):
        path = "/" + path
    if trailing and not path.endswith("/"):
        path += "/"
    path = quote(unquote(path), safe="/%:@!$&'()*+,;=-._~")
    return urlunparse((u.scheme, netloc, path, "", u.query, ""))


def _safe_segment(seg: str) -> str:
    seg = re.sub(r'[<>:"|?*\\\x00-\x1f]', "_", seg).strip(" .") or "_"
    if len(seg) > 90:
        stem, dot, ext = seg.rpartition(".")
        h = hashlib.sha1(seg.encode()).hexdigest()[:8]
        seg = (stem[:60] + "_" + h + dot + ext) if dot and len(ext) <= 8 else seg[:70] + "_" + h
    return seg


def url_to_relpath(url: str, content_type: str = "") -> str:
    u = urlparse(url)
    segs = unquote(u.path).split("/")
    if segs and segs[0] == "":
        segs = segs[1:]
    if not segs or segs[-1] == "":
        segs = (segs[:-1] if segs else []) + ["index.html"]
    last = segs[-1]
    if "." not in last and ("html" in content_type or not content_type):
        last += ".html"
    if u.query:
        stem, dot, ext = last.rpartition(".")
        q = hashlib.sha1(u.query.encode()).hexdigest()[:8]
        last = f"{stem}_q{q}.{ext}" if dot else f"{last}_q{q}"
    segs[-1] = last
    host = u.netloc.replace(":", "_")
    return "/".join([_safe_segment(host)] + [_safe_segment(s) for s in segs])


def extract_links(body: bytes, base_url: str) -> tuple[list[str], list[str]]:
    """(page links, asset links) found in an HTML document. Regex based on purpose: fast and forgiving."""
    base = base_url
    m = _BASE.search(body[:60000])
    if m:
        base = urljoin(base_url, html.unescape((m.group(1) or m.group(2)).decode("utf-8", "replace")))
    pages: list[str] = []
    assets: list[str] = []
    for tm in _TAG.finditer(body):
        tag = tm.group(1).lower()
        attrs = tm.group(2)
        am = _ATTR.search(attrs)
        if not am:
            continue
        raw = (am.group(2) or am.group(3) or am.group(4) or b"").decode("utf-8", "replace").strip()
        if not raw or raw.startswith(("#", "javascript:", "mailto:", "data:", "tel:")):
            continue
        url = urljoin(base, html.unescape(raw))
        if tag in (b"a", b"area"):
            pages.append(url)
        elif tag == b"link":
            rm = _REL.search(attrs)
            rel = (rm.group(1) or rm.group(2) or rm.group(3) or b"").lower() if rm else b""
            if b"stylesheet" in rel or b"icon" in rel:
                assets.append(url)
        else:  # script / img
            assets.append(url)
    return pages, assets


# --------------------------------------------------------------------------- crawler
@dataclass
class _Fetched:
    url: str
    ok: bool
    relpath: str | None = None
    content_type: str = ""
    body: bytes | None = None
    cached: bool = False
    size: int = 0
    error: str = ""
    final_url: str = ""  # after redirects; relative links must resolve against this


class Crawler:
    def __init__(self, lib: Library, opts: CrawlOptions, job: Job):
        self.lib = lib
        self.opts = opts
        self.job = job
        self.store = lib.store
        self.result = CrawlResult()
        start = normalize_url(opts.start_url)
        if start is None:
            raise ValueError("That doesn't look like a web address (it must start with http:// or https://).")
        self.start = start
        u = urlparse(start)
        self.host = u.netloc
        self.prefix = "/" if opts.scope == "host" else (u.path if u.path.endswith("/") else posixpath.dirname(u.path) + "/")
        self.exclude = [re.compile(p, re.I) for p in opts.exclude if p.strip()]
        self.include = [re.compile(p, re.I) for p in opts.include if p.strip()]
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5"},
            follow_redirects=True, timeout=httpx.Timeout(30, connect=10),
            limits=httpx.Limits(max_connections=opts.concurrency * 2))
        self.robots: urllib.robotparser.RobotFileParser | None = None
        self.crawl_delay = opts.delay

    # ------------------------------------------------------------------ rules
    def _load_robots(self) -> None:
        if not self.opts.respect_robots:
            return
        u = urlparse(self.start)
        try:
            r = self.client.get(f"{u.scheme}://{u.netloc}/robots.txt")
        except httpx.HTTPError:
            return
        if r.status_code >= 500:
            raise RuntimeError("The site's robots.txt is unavailable (server error). Try again later.")
        if r.status_code != 200:
            return  # no robots.txt: everything is allowed
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        self.robots = rp
        delay = rp.crawl_delay(USER_AGENT)
        if delay:
            self.crawl_delay = max(self.crawl_delay, float(delay))

    def _allowed(self, url: str, is_page: bool) -> bool:
        u = urlparse(url)
        if u.netloc != self.host:
            return False
        path = u.path or "/"
        if is_page and not path.startswith(self.prefix):
            return False
        ext = posixpath.splitext(path)[1].lower()
        if ext in SKIP_EXT or (ext == ".pdf" and not self.opts.include_pdfs):
            return False
        if is_page and ext in ASSET_EXT:
            return False
        if any(p.search(url) for p in self.exclude):
            return False
        if is_page and self.include and not any(p.search(url) for p in self.include):
            return False
        if self.robots is not None and not self.robots.can_fetch(USER_AGENT, url):
            return False
        return True

    # ------------------------------------------------------------------ fetching
    def _write(self, rel: str, data: bytes) -> None:
        path = self.lib.source_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(path)

    def _fetch(self, url: str, want_body: bool) -> _Fetched:
        rec = self.store.crawl_get(url)
        headers: dict[str, str] = {}
        if rec is not None and rec["status"] and 200 <= rec["status"] < 300 and rec["relpath"]:
            local = self.lib.source_dir / rec["relpath"]
            if local.is_file():
                if not self.opts.refresh:
                    body = local.read_bytes() if (want_body and "html" in (rec["content_type"] or "")) else None
                    return _Fetched(url, True, rec["relpath"], rec["content_type"] or "", body, True, rec["size"] or 0)
                if rec["etag"]:
                    headers["If-None-Match"] = rec["etag"]
                if rec["last_modified"]:
                    headers["If-Modified-Since"] = rec["last_modified"]
        err = ""
        for attempt in range(4):
            self.job.check()
            try:
                with self.client.stream("GET", url, headers=headers) as r:
                    status = r.status_code
                    if status == 304 and rec is not None and rec["relpath"]:
                        body = (self.lib.source_dir / rec["relpath"]).read_bytes() if want_body else None
                        self.store.crawl_put(url, rec["relpath"], 200, rec["content_type"], rec["etag"],
                                             rec["last_modified"], rec["size"] or 0)
                        return _Fetched(url, True, rec["relpath"], rec["content_type"] or "", body, True, rec["size"] or 0)
                    if status in (429, 502, 503, 504):
                        retry_after = str(r.headers.get("retry-after", ""))
                        wait_s = min(60.0, float(retry_after) if retry_after.isdigit() else float(2 ** (attempt + 1)))
                        err = f"HTTP {status}"
                        time.sleep(wait_s)
                        continue
                    if status >= 400:
                        self.store.crawl_put(url, None, status, None, None, None, 0)
                        return _Fetched(url, False, error=f"HTTP {status}")
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    is_html = "html" in ctype
                    if not (is_html or ctype == "application/pdf" or posixpath.splitext(urlparse(url).path)[1].lower() in ASSET_EXT
                            or ctype.startswith(("text/css", "text/javascript", "application/javascript", "image/"))):
                        self.store.crawl_put(url, None, 415, ctype, None, None, 0)
                        return _Fetched(url, False, error=f"skipped type {ctype}")
                    limit = MAX_PAGE_BYTES if is_html else MAX_FILE_BYTES
                    chunks: list[bytes] = []
                    size = 0
                    for part in r.iter_bytes():
                        size += len(part)
                        if size > limit:
                            self.store.crawl_put(url, None, 413, ctype, None, None, 0)
                            return _Fetched(url, False, error="file too large")
                        chunks.append(part)
                    data = b"".join(chunks)
                    rel = url_to_relpath(url, ctype)
                    self._write(rel, data)
                    self.store.crawl_put(url, rel, 200, ctype, r.headers.get("etag"), r.headers.get("last-modified"), size)
                    time.sleep(self.crawl_delay)
                    return _Fetched(url, True, rel, ctype, data if (want_body and is_html) else None, False, size,
                                    final_url=str(r.url))
            except httpx.HTTPError as e:
                err = f"{type(e).__name__}"
                time.sleep(1.5 * (attempt + 1))
        return _Fetched(url, False, error=err or "failed")

    # ------------------------------------------------------------------ main loop
    def run(self) -> CrawlResult:
        job, opts, res = self.job, self.opts, self.result
        try:
            job.update(phase="Downloading", message=f"Connecting to {self.host}")
            self._load_robots()
            first = self._fetch(self.start, True)
            if not first.ok:
                raise RuntimeError(f"Could not download {self.start}: {first.error}")
            if first.body is not None and b'name="generator" content="Doxygen' in first.body[:6000]:
                res.doxygen = True
                self.exclude += [re.compile(p, re.I) for p in DOXYGEN_CRAWL_EXCLUDE]
                job.log("Detected Doxygen documentation: skipping source listings and member-list pages.")

            seen: set[str] = {self.start}
            page_q: deque[str] = deque()    # breadth first, so a page limit keeps the shallow (important) pages
            asset_q: deque[str] = deque()   # styles / images are fetched after the pages
            n_pages = 0

            def enqueue(links: list[str], is_page: bool) -> None:
                for raw in links:
                    u = normalize_url(raw)
                    if u and u not in seen and self._allowed(u, is_page):
                        seen.add(u)
                        (page_q if is_page else asset_q).append(u)

            def absorb(f: _Fetched, is_page: bool) -> None:
                nonlocal n_pages
                if not f.ok:
                    res.failed += 1
                    if len(res.errors) < 50:
                        res.errors.append(f"{f.url}: {f.error}")
                    return
                res.bytes += f.size
                if f.cached:
                    res.cached += 1
                if is_page:
                    n_pages += 1
                    res.pages += 1
                    if f.body:
                        pages, assets = extract_links(f.body, f.final_url or f.url)
                        enqueue(pages, True)
                        if opts.download_assets:
                            enqueue(assets, False)
                else:
                    res.assets += 1

            absorb(first, True)
            submitted_pages = 1
            with ThreadPoolExecutor(max_workers=max(1, opts.concurrency), thread_name_prefix="crawl") as pool:
                pending: dict = {}  # future -> (url, is_page)
                while page_q or asset_q or pending:
                    job.check()
                    while len(pending) < opts.concurrency * 2:
                        if page_q and submitted_pages < opts.max_pages:
                            url, is_page = page_q.popleft(), True
                            submitted_pages += 1
                        elif asset_q and not page_q:
                            url, is_page = asset_q.popleft(), False
                        else:
                            break
                        pending[pool.submit(self._fetch, url, is_page)] = (url, is_page)
                    if not pending:
                        if page_q:
                            job.log(f"Stopped at the page limit ({opts.max_pages}).")
                        break
                    done, _ = wait(list(pending), timeout=1.0, return_when=FIRST_COMPLETED)
                    for fut in done:
                        url, is_page = pending.pop(fut)
                        try:
                            fetched = fut.result()
                        except Cancelled:
                            raise
                        except Exception as e:  # noqa: BLE001 - one bad page must not end the crawl
                            fetched = _Fetched(url, False, error=f"{type(e).__name__}: {e}")
                        absorb(fetched, is_page)
                    job.update(phase="Downloading", current=res.pages + res.assets,
                               total=res.pages + res.assets + len(page_q) + len(asset_q) + len(pending),
                               message=f"{res.pages} pages, {res.assets} assets, {res.failed} failed")
            job.update(message=f"Downloaded {res.pages} pages ({res.bytes / 1e6:.0f} MB)")
            return res
        except Cancelled:
            raise
        finally:
            self.client.close()
