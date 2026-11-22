"""Hybrid retrieval, as in the pipeline diagram:

    question -> vector search (meaning) + keyword search (exact terms / identifiers)
             -> combine (reciprocal rank fusion) and optionally rerank with a cross-encoder
             -> a few passages per page -> handed to the model (or shown as manual search results)

The same engine serves both: `mode` picks hybrid, meaning only ("semantic") or keywords only, so a person can
search without any model at all.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

import numpy as np

from . import paths
from .embeddings import EmbeddingError, get_embedder, spec_key
from .library import LibraryManager
from .store import DocFilter
from .textutil import best_excerpt, build_exact_query, build_fts_query, highlight_terms

log = logging.getLogger("perplexify.retrieval")
RRF_K = 60
MODES = ("hybrid", "semantic", "keyword")


@dataclass
class Hit:
    library_id: str
    library_name: str
    chunk_id: int
    doc_id: int
    title: str
    uri: str
    heading: str
    anchor: str | None
    text: str
    origin: str
    path: str | None
    kind: str
    lang: str = ""
    score: float = 0.0
    vec_rank: int | None = None
    kw_rank: int | None = None
    cosine: float | None = None
    rerank: float | None = None

    def links(self) -> tuple[str | None, str | None]:
        """(original online URL, URL served by this app) for opening the source page."""
        frag = ""
        if self.anchor:
            frag = "#" + (self.anchor if self.anchor.startswith("page=") else quote(self.anchor))
        online = (self.uri + frag) if self.uri.startswith(("http://", "https://")) else None
        local = None
        if self.origin == "mirror" and self.path:
            local = f"/mirror/{self.library_id}/{quote(self.path)}{frag}"
        elif self.origin == "external":
            local = f"/api/libraries/{self.library_id}/file?doc={self.doc_id}{frag}"
        return online, local

    def to_public(self, n: int, full_text: bool = False, terms: list[str] | None = None) -> dict:
        online, local = self.links()
        out = {"n": n, "library_id": self.library_id, "library": self.library_name, "title": self.title,
               "doc_id": self.doc_id, "heading": self.heading, "anchor": self.anchor, "online_url": online,
               "local_url": local, "kind": self.kind, "lang": self.lang,
               "snippet": self.text[:420], "text": self.text if full_text else None,
               "score": round(self.score, 4), "cosine": None if self.cosine is None else round(self.cosine, 3),
               "vec_rank": self.vec_rank, "kw_rank": self.kw_rank,
               "rerank": None if self.rerank is None else round(self.rerank, 3)}
        if terms is not None:
            out["excerpt"] = best_excerpt(self.text, terms)
        return out


class Retriever:
    def __init__(self, libs: LibraryManager, settings):
        self.libs = libs
        self.settings = settings
        self._rr = None
        self._rr_name: str | None = None
        self._rr_lock = threading.Lock()

    # ------------------------------------------------------------------ reranker
    def _reranker(self, name: str):
        with self._rr_lock:
            if self._rr is None or self._rr_name != name:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                from .models import seed_bundled_models

                seed_bundled_models()
                self._rr = TextCrossEncoder(model_name=name, cache_dir=str(paths.models_dir()))
                self._rr_name = name
            return self._rr

    # ------------------------------------------------------------------ search
    def search(self, queries: list[str], library_ids: list[str], *, top_k: int = 8, candidates: int = 40,
               max_per_doc: int = 3, rerank: bool = False, reranker_model: str | None = None,
               mode: str = "hybrid", match: str = "any", kinds: list[str] | None = None,
               langs: list[str] | None = None, path_contains: str = "") -> tuple[list[Hit], dict]:
        """Best passages for the queries across the chosen libraries.

        mode: "hybrid" (meaning + keywords), "semantic" (meaning only) or "keyword" (words and identifiers only)
        match: "any" word counts (ranked by BM25) or "all" typed words must appear (keyword part only)
        kinds / langs / path_contains: restrict which documents are searched"""
        t_all = time.perf_counter()
        mode = mode if mode in MODES else "hybrid"
        libs = [lib for lib in (self.libs.get(i) for i in library_ids) if lib is not None]
        flt = DocFilter(tuple(kinds or ()), tuple(langs or ()), (path_contains or "").strip())
        debug: dict = {"queries": queries, "libraries": [lib.name for lib in libs], "timing_ms": {}, "pending": 0,
                       "mode": mode, "warnings": [], "terms": highlight_terms(queries[0]) if queries else []}
        if not libs or not queries:
            return [], debug
        use_vec = mode in ("hybrid", "semantic")
        use_kw = mode in ("hybrid", "keyword")

        def warn(msg: str) -> None:
            if msg not in debug["warnings"]:
                debug["warnings"].append(msg)

        fused: dict[tuple[str, int], float] = {}
        vec_rank: dict[tuple[str, int], int] = {}
        kw_rank: dict[tuple[str, int], int] = {}
        primary_vec: dict[str, np.ndarray] = {}
        qcache: dict[tuple[str, str], np.ndarray] = {}
        failed: set[tuple[str, str]] = set()
        t_vec = t_kw = 0.0
        n_vec = n_kw = 0

        for qi, query in enumerate(queries):
            weight = 1.0 if qi == 0 else 0.7
            fts = build_fts_query(query, match) if use_kw else None
            exact = build_exact_query(query) if use_kw else None
            for lib in libs:
                store = lib.store
                spec = lib.embedder_spec
                if use_vec:
                    t0 = time.perf_counter()
                    key = (spec_key(spec), query)
                    qv = qcache.get(key)
                    if qv is None and key not in failed:
                        try:
                            qv = get_embedder(spec, self.settings).embed_query(query)
                            qcache[key] = qv
                        except EmbeddingError as e:  # e.g. a cloud embedder whose key is gone: keywords still work
                            failed.add(key)
                            warn(f"{lib.name}: search by meaning is unavailable ({e})")
                    if qv is not None:
                        try:
                            ids, _sims = store.vector_search(qv, candidates, flt)
                        except RuntimeError as e:
                            ids = []
                            warn(f"{lib.name}: {e}")
                        n_vec += len(ids)
                        if qi == 0:
                            primary_vec[lib.id] = qv
                        for rank, cid in enumerate(ids, 1):
                            k = (lib.id, cid)
                            fused[k] = fused.get(k, 0.0) + weight / (RRF_K + rank)
                            vec_rank[k] = min(vec_rank.get(k, rank), rank)
                    t_vec += time.perf_counter() - t0
                if use_kw:
                    t0 = time.perf_counter()
                    kws = store.bm25(fts, candidates, flt) if fts else []
                    t_kw += time.perf_counter() - t0
                    n_kw += len(kws)
                    for rank, (cid, _s) in enumerate(kws, 1):
                        k = (lib.id, cid)
                        fused[k] = fused.get(k, 0.0) + weight / (RRF_K + rank)
                        kw_rank[k] = min(kw_rank.get(k, rank), rank)
                    if exact:  # an identifier or "quoted phrase": reward passages that contain it verbatim
                        for rank, (cid, _s) in enumerate(store.bm25(exact, candidates, flt), 1):
                            k = (lib.id, cid)
                            fused[k] = fused.get(k, 0.0) + 1.5 * weight / (RRF_K + rank)
                            kw_rank[k] = min(kw_rank.get(k, rank), rank)
        debug["timing_ms"].update(vector=round(t_vec * 1000), keyword=round(t_kw * 1000))
        debug["vector_hits"], debug["keyword_hits"] = n_vec, n_kw
        debug["pending"] = sum(lib.store.pending_count() for lib in libs)
        if use_vec and not use_kw and debug["pending"] and not fused:
            warn("Passages are still being embedded, so search by meaning has little to find yet.")
        if not fused:
            debug["timing_ms"]["total"] = round((time.perf_counter() - t_all) * 1000)
            return [], debug

        # ---- combine: take the best fused candidates, look up their text and true cosine similarity
        pool_n = max(top_k * 4, 30)
        pool = sorted(fused, key=fused.get, reverse=True)[:pool_n]
        by_lib: dict[str, list[int]] = {}
        for lib_id, cid in pool:
            by_lib.setdefault(lib_id, []).append(cid)
        hits: list[Hit] = []
        for lib_id, cids in by_lib.items():
            lib = self.libs.get(lib_id)
            rows = lib.store.get_chunks(cids)
            qv = primary_vec.get(lib_id)
            vecs = lib.store.get_vectors(cids) if qv is not None else {}
            for cid in cids:
                r = rows.get(cid)
                if r is None:
                    continue
                cos = float(vecs[cid] @ qv) if (qv is not None and cid in vecs and vecs[cid].shape == qv.shape) else None
                k = (lib_id, cid)
                hits.append(Hit(lib.id, lib.name, cid, r["doc_id"], r["title"], r["uri"], r["heading"], r["anchor"],
                                r["text"], r["origin"], r["path"], r["kind"], lang=r["lang"],
                                score=fused[k] * (1.0 + max(cos or 0.0, 0.0)),
                                vec_rank=vec_rank.get(k), kw_rank=kw_rank.get(k), cosine=cos))
        hits.sort(key=lambda h: h.score, reverse=True)

        # ---- optional cross-encoder rerank of the best candidates
        if rerank:
            t0 = time.perf_counter()
            try:
                head = hits[:30]
                ce = self._reranker(reranker_model or "Xenova/ms-marco-MiniLM-L-6-v2")
                scores = list(ce.rerank(queries[0], [(h.heading + "\n" + h.text)[:1800] for h in head]))
                for h, s in zip(head, scores):
                    h.rerank = float(s)
                head.sort(key=lambda h: h.rerank or 0.0, reverse=True)
                hits = head + hits[30:]
                debug["reranked"] = True
            except Exception as e:  # noqa: BLE001 - reranking is an optional improvement
                log.warning("rerank skipped: %s", e)
                debug["rerank_error"] = str(e)[:200]
            debug["timing_ms"]["rerank"] = round((time.perf_counter() - t0) * 1000)

        # ---- a few passages per page, best first
        out: list[Hit] = []
        per_doc: dict[tuple[str, int], int] = {}
        for h in hits:
            k = (h.library_id, h.doc_id)
            if max_per_doc and per_doc.get(k, 0) >= max_per_doc:
                continue
            per_doc[k] = per_doc.get(k, 0) + 1
            out.append(h)
            if len(out) >= top_k:
                break
        debug["timing_ms"]["total"] = round((time.perf_counter() - t_all) * 1000)
        debug["candidates"] = len(fused)
        return out, debug
