"""The local web server: JSON API + the browser UI. Listens on 127.0.0.1 only."""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, github, osutil, paths, providers, tasks
from .context import AppContext
from .embeddings import EmbeddingError, validate_embedder
from .bookmarks_api import make_router as bookmarks_router
from .history_api import make_router as history_router
from .llm import LLMError, make_provider
from .local_api import make_router as local_router, stop_engine_on_shutdown
from .presets import PRESETS
from .settings import clean_embedder, sanitize_profile
from .textutil import decode_bytes

log = logging.getLogger("perplexify.server")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_VIEW_BYTES = 2 * 1024 * 1024


class Host(Protocol):
    """What shows the interface: the app's own window, or the default browser (see app.py and desktop.py)."""

    mode: str   # "window" or "browser"

    def show(self) -> None: ...                                   # bring the interface to the front
    def set_theme(self, theme: str) -> None: ...                  # the page switched between light and dark
    def pick_folder(self, title: str = "") -> str | None: ...     # native folder dialog
    def pick_files(self, title: str = "") -> list[str]: ...       # native file dialog


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ThemeBody(Body):
    theme: str


class ConnectBody(Body):
    provider: str
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    name: str = ""


class ProfilePatch(Body):
    model: str | None = None
    reasoning: str | None = None
    effort: str | None = None
    show_thinking: bool | None = None
    context_tokens: int | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    activate: bool = False


class LibraryCreate(Body):
    name: str
    description: str = ""
    embedder: dict | None = None


class LibraryPatch(Body):
    name: str | None = None
    description: str | None = None


class CrawlBody(Body):
    url: str
    scope: str = "path"
    max_pages: int = Field(20000, ge=1, le=200000)
    concurrency: int = Field(4, ge=1, le=8)
    delay: float = Field(0.25, ge=0, le=10)
    include: list[str] = []
    exclude: list[str] = []
    download_assets: bool = True
    include_pdfs: bool = True
    respect_robots: bool = True


class RepoBody(Body):
    url: str = ""
    path: str = ""
    ref: str = ""
    token: str = ""
    include: list[str] = []
    exclude: list[str] = []
    use_gitignore: bool = True
    include_docs: bool = True


class FolderBody(Body):
    path: str
    skip: list[str] = []


class EmbedderBody(Body):
    embedder: dict


DOC_KINDS = ["html", "pdf", "markdown", "text", "rst", "epub", "docx"]


class SearchBody(Body):
    """Manual search: no model is involved, only the index."""
    query: str
    libraries: list[str] = []          # empty = every library
    mode: str = "hybrid"               # hybrid | semantic (meaning only) | keyword (words and identifiers only)
    match: str = "any"                 # any | all (keyword part: every typed word must appear)
    scope: str = "all"                 # all | code | docs
    langs: list[str] = []              # programming languages, for code
    path: str = ""                     # only files / pages whose path or title contains this
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0, le=2000)
    max_per_doc: int = Field(4, ge=0, le=20)


class ChatBody(Body):
    question: str
    history: list[dict] = []
    libraries: list[str] = []
    profile_id: str | None = None
    options: dict | None = None
    chat_id: str | None = None        # the saved conversation this question continues (see history.py); a new one when absent


class KeyBody(Body):
    provider: str
    key: str = ""


def create_app(ctx: AppContext | None = None, shutdown: Callable[[], None] | None = None,
               host: Host | None = None) -> FastAPI:
    ctx = ctx or AppContext()
    app = FastAPI(title="Perplexify", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    settings = ctx.settings
    ctx.warm_up()  # load the embedding model and vector index in the background so the first search is quick

    # ------------------------------------------------------------------ safety: only this computer, only this app
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").lower()
        hostname = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        if hostname not in LOCAL_HOSTS:
            return JSONResponse({"error": "This server only answers on localhost."}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1].lower() != host:
                return JSONResponse({"error": "Cross-site request refused."}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"error": "Cross-site request refused."}, status_code=403)
        return await call_next(request)

    def lib_or_404(lib_id: str):
        lib = ctx.libs.get(lib_id)
        if lib is None:
            raise HTTPException(404, "Library not found")
        return lib

    def idle_or_409(lib_id: str) -> None:
        if lib_id in ctx.jobs.active_libraries():
            raise HTTPException(409, "This library is busy with another job. Wait for it to finish or stop it first.")

    def start(kind: str, title: str, lib, fn, params: dict) -> dict:
        idle_or_409(lib.id)
        job = ctx.jobs.submit(kind, title, lib.id, lambda j: fn(ctx, lib, params, j))
        return job.to_dict()

    def public_profile(p: dict) -> dict:
        q = {k: v for k, v in p.items() if k != "api_key_enc"}
        env = providers.env_key(p["provider"])
        q["has_key"] = bool(p.get("api_key_enc")) or bool(p.get("use_env_key") and env) or providers.is_local(p)  # a local model needs no key
        return q

    def embedder_options() -> list[dict]:
        out = []
        for o in providers.EMBEDDING_OPTIONS:
            key_ok = o["key"] is None or bool(settings.provider_key(o["key"]))
            out.append({**o, "available": key_ok})
        return out

    app.include_router(history_router(ctx))      # chats and searches of this computer: /api/history/...
    app.include_router(bookmarks_router(ctx))    # bookmarked sources in folders: /api/bookmarks/...
    app.include_router(local_router(ctx))        # local models (llama.cpp): /api/local/...
    stop_engine_on_shutdown(app)                 # the llama.cpp engine goes down with the app (window closed, /api/shutdown, Ctrl+C)

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health():
        return {"app": "perplexify", "version": __version__, "ready": settings.is_ready()}

    @app.get("/api/bootstrap")
    def bootstrap():
        s = settings.public()
        return {"app": "perplexify", "version": __version__, "ready": settings.is_ready(),
                "settings": s, "providers": providers.public_catalog(), "embedders": embedder_options(),
                "libraries": [lib.summary() for lib in ctx.libs.list()], "presets": PRESETS,
                "busy": sorted(ctx.jobs.active_libraries()), "platform": "windows" if osutil.IS_WINDOWS else "mac" if osutil.IS_MAC else "other",
                # where the interface is shown: its own "window", the default "browser", or only "server" (perplexify serve)
                "shell": host.mode if host is not None else "server"}

    @app.post("/api/shutdown")
    def stop():
        if shutdown is None:
            raise HTTPException(400, "Not running as an app")
        shutdown()
        return {"ok": True}

    @app.post("/api/window/show")
    def window_show():
        """A second launch of the app lands here: bring the window to the front (or open the page in the browser)."""
        if host is None:
            raise HTTPException(400, "Not running as an app")
        host.show()
        return {"ok": True}

    @app.post("/api/window/theme")
    def window_theme(body: ThemeBody):
        """The page switched between light and dark: the window's title bar follows. Harmless without a window."""
        if host is not None and body.theme in ("light", "dark"):
            host.set_theme(body.theme)
        return {"ok": True}

    # ------------------------------------------------------------------ connecting a model
    @app.post("/api/detect-provider")
    def detect(body: KeyBody):
        return {"provider": providers.detect_provider(body.key)}

    @app.post("/api/connect")
    def connect(body: ConnectBody):
        prov = providers.get_chat_provider(body.provider)
        if prov is None:
            raise HTTPException(400, "Unknown provider")
        key = body.api_key.strip()
        use_env = False
        if not key:
            env = providers.env_key(prov["id"])
            if env is None:
                raise HTTPException(400, "Paste your API key first.")
            key, use_env = env[1], True
        base_url = body.base_url.strip() or prov["base_url"]
        if prov["id"] == "custom" and not base_url:
            raise HTTPException(400, "Enter the service's base URL (for example https://api.example.com/v1).")
        temp = sanitize_profile({"id": "tmp", "provider": prov["id"], "base_url": base_url, "model": body.model,
                                 "name": body.name or prov["name"]})
        provider = make_provider(temp, key)
        models: list[dict] = []
        try:
            models = provider.list_models()
        except LLMError as e:
            if prov["id"] == "custom" and body.model:  # some services have no model list; try a tiny request instead
                try:
                    provider.complete([{"role": "user", "content": "Reply with the single word OK."}])
                except LLMError as e2:
                    raise HTTPException(400, str(e2)) from e2
            else:
                raise HTTPException(400, str(e)) from e
        ids = [m["id"] for m in models]
        model = body.model if (body.model and (body.model in ids or not ids)) else providers.pick_default_model(prov["id"], ids)
        profile = settings.connect(prov["id"], None if use_env else key, model, base_url, body.name, use_env)
        info: dict = {}
        try:
            info = ctx.llm_info(profile, force=True)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "profile": public_profile(profile), "models": models, "model": model, "info": info,
                "embeddings_note": prov.get("embeddings_note")}

    @app.get("/api/profiles/{profile_id}/models")
    def profile_models(profile_id: str):
        profile = settings.profile(profile_id)
        if profile is None or profile["id"] != profile_id:
            raise HTTPException(404, "Not connected")
        try:
            return {"models": make_provider(profile, settings.api_key(profile)).list_models()}
        except LLMError as e:
            raise HTTPException(400, str(e)) from e

    @app.patch("/api/profiles/{profile_id}")
    def patch_profile(profile_id: str, body: ProfilePatch):
        if settings.profile(profile_id) is None:
            raise HTTPException(404, "Not connected")
        fields = {k: v for k, v in body.model_dump(exclude={"activate"}).items() if k in body.model_fields_set}
        settings.update({"profiles": [{"id": profile_id, **fields}],
                         **({"active_profile": profile_id} if body.activate or "model" in fields else {})})
        profile = settings.profile(profile_id)
        info: dict = {}
        try:
            info = ctx.llm_info(profile, force=True)
        except Exception:  # noqa: BLE001
            pass
        return {"profile": public_profile(profile), "info": info}

    @app.delete("/api/profiles/{profile_id}")
    def delete_profile(profile_id: str):
        settings.remove_profile(profile_id)
        return {"ok": True}

    @app.post("/api/profiles/{profile_id}/activate")
    def activate(profile_id: str):
        settings.update({"active_profile": profile_id})
        return {"ok": True}

    @app.get("/api/llm/info")
    def llm_info(profile_id: str | None = None, refresh: bool = False):
        profile = settings.profile(profile_id)
        if profile is None:
            return {"connected": False}
        try:
            return {"connected": True, **ctx.llm_info(profile, force=refresh)}
        except Exception as e:  # noqa: BLE001
            return {"connected": True, "error": str(e), "model": profile.get("model"), "provider": profile["provider"]}

    # ------------------------------------------------------------------ settings and embeddings
    @app.get("/api/settings")
    def get_settings():
        return settings.public()

    @app.put("/api/settings")
    def put_settings(body: dict):
        settings.update(body)
        return settings.public()

    @app.get("/api/embedders")
    def embedders():
        return {"options": embedder_options(), "default": settings.embedder()}

    @app.post("/api/embedding-keys")
    def embedding_key(body: KeyBody):
        if body.provider != "voyage":
            raise HTTPException(400, "Only Voyage AI needs its own key; the others reuse the connected provider's key.")
        settings.set_embedding_key("voyage", body.key)
        return {"ok": True}

    @app.post("/api/embedders/test")
    def embedder_test(body: EmbedderBody):
        spec = clean_embedder(body.embedder)
        try:
            return {"ok": True, **validate_embedder(spec, settings)}
        except EmbeddingError as e:
            raise HTTPException(400, str(e)) from e

    # ------------------------------------------------------------------ libraries
    @app.get("/api/libraries")
    def libraries():
        busy = ctx.jobs.active_libraries()
        return {"libraries": [{**lib.summary(), "busy": lib.id in busy} for lib in ctx.libs.list()]}

    @app.post("/api/libraries")
    def create_library(body: LibraryCreate):
        spec = clean_embedder(body.embedder) if body.embedder else settings.embedder()
        lib = ctx.libs.create(body.name, body.description, spec)
        return lib.summary()

    @app.get("/api/libraries/{lib_id}")
    def library(lib_id: str):
        return lib_or_404(lib_id).summary(deep=True)

    @app.patch("/api/libraries/{lib_id}")
    def patch_library(lib_id: str, body: LibraryPatch):
        lib = lib_or_404(lib_id)
        if body.name is not None and body.name.strip():
            lib.meta["name"] = body.name.strip()
        if body.description is not None:
            lib.meta["description"] = body.description.strip()
        lib.save_meta()
        return lib.summary()

    @app.delete("/api/libraries/{lib_id}")
    def delete_library(lib_id: str):
        lib_or_404(lib_id)
        idle_or_409(lib_id)
        ctx.libs.delete(lib_id)
        return {"ok": True}

    @app.get("/api/libraries/{lib_id}/documents")
    def documents(lib_id: str, q: str = "", offset: int = 0, limit: int = 50):
        rows, total = lib_or_404(lib_id).store.list_documents(q or None, max(0, offset), min(200, max(1, limit)))
        return {"total": total, "documents": [
            {"id": r["id"], "title": r["title"], "uri": r["uri"], "kind": r["kind"], "lang": r["lang"],
             "chunks": r["n_chunks"], "indexed_at": r["indexed_at"]} for r in rows]}

    @app.delete("/api/libraries/{lib_id}/documents/{doc_id}")
    def delete_document(lib_id: str, doc_id: int):
        lib = lib_or_404(lib_id)
        idle_or_409(lib_id)
        with lib.store.tx():
            lib.store.delete_document(doc_id)
        return {"ok": True}

    def doc_path(lib, doc_id: int) -> tuple[Any, Path]:
        row = lib.store.doc_by_id(doc_id)
        if row is None or not row["path"]:
            raise HTTPException(404, "Document not found")
        if row["origin"] == "mirror":
            base = lib.source_dir.resolve()
            p = (base / row["path"]).resolve()
            if base not in p.parents:
                raise HTTPException(404, "Document not found")
        else:
            p = Path(row["path"])
        if not p.is_file():
            raise HTTPException(404, "The file is no longer on disk.")
        return row, p

    @app.get("/api/libraries/{lib_id}/source")
    def source(lib_id: str, doc: int):
        """The text of a document for the in-app viewer (code and text; other kinds are opened as files)."""
        lib = lib_or_404(lib_id)
        row, p = doc_path(lib, doc)
        if row["kind"] not in ("code", "markdown", "text", "rst"):
            return {"kind": row["kind"], "title": row["title"], "text": None, "online_url": row["uri"] if row["uri"].startswith("http") else None,
                    "file_url": f"/api/libraries/{lib_id}/file?doc={doc}"}
        if p.stat().st_size > MAX_VIEW_BYTES:
            raise HTTPException(413, "That file is too large to show here.")
        text = decode_bytes(p.read_bytes())
        return {"kind": row["kind"], "title": row["title"], "lang": row["lang"], "text": text, "lines": text.count("\n") + 1,
                "online_url": row["uri"] if row["uri"].startswith("http") else None}

    @app.get("/api/libraries/{lib_id}/file")
    def file(lib_id: str, doc: int):
        lib = lib_or_404(lib_id)
        _row, p = doc_path(lib, doc)
        media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        headers = {"X-Content-Type-Options": "nosniff"}
        if media in ("text/html", "application/xhtml+xml"):
            headers["Content-Security-Policy"] = "sandbox"
        return FileResponse(p, media_type=media if media != "text/markdown" else "text/plain", headers=headers)

    @app.get("/mirror/{lib_id}/{path:path}")
    def mirror(lib_id: str, path: str):
        """Offline copy of a downloaded site. Served sandboxed: downloaded pages may not run scripts or touch this app."""
        lib = lib_or_404(lib_id)
        base = lib.source_dir.resolve()
        target = (base / path).resolve()
        if base not in target.parents or not target.is_file():
            raise HTTPException(404, "Not found")
        media = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        headers = {"X-Content-Type-Options": "nosniff"}
        if media != "application/pdf":
            headers["Content-Security-Policy"] = "sandbox"
        return FileResponse(target, media_type=media, headers=headers)

    # ------------------------------------------------------------------ adding content
    @app.get("/api/presets")
    def presets():
        return {"presets": PRESETS}

    @app.post("/api/presets/{preset_id}/add")
    def add_preset(preset_id: str):
        preset = next((p for p in PRESETS if p["id"] == preset_id), None)
        if preset is None:
            raise HTTPException(404, "Unknown preset")
        lib = next((x for x in ctx.libs.list() if x.name == preset["library_name"]), None) or \
            ctx.libs.create(preset["library_name"], preset["description"], settings.embedder())
        return {"library": lib.summary(), "job": start("crawl", f"Download {preset['name']}", lib, tasks.crawl_task,
                                                       {"start_url": preset["url"]})}

    @app.post("/api/libraries/{lib_id}/ingest/crawl")
    def ingest_crawl(lib_id: str, body: CrawlBody):
        lib = lib_or_404(lib_id)
        return start("crawl", f"Download {body.url}", lib, tasks.crawl_task,
                     {**body.model_dump(exclude={"url"}), "start_url": body.url})

    @app.post("/api/libraries/{lib_id}/ingest/repo")
    def ingest_repo(lib_id: str, body: RepoBody):
        lib = lib_or_404(lib_id)
        params = body.model_dump()
        if body.path.strip():
            kind, label = "folder", body.path.strip()
            if not Path(body.path.strip()).is_dir():
                raise HTTPException(400, "That folder does not exist.")
        elif body.url.strip():
            label = body.url.strip()
            kind = "github" if github.parse_github(label, body.ref) else "git"
        else:
            raise HTTPException(400, "Enter a GitHub address or choose a folder.")
        params.update(kind=kind, path=body.path.strip(), url=body.url.strip())
        return start("repo", f"Import {label}", lib, tasks.repo_task, params)

    @app.post("/api/libraries/{lib_id}/ingest/folder")
    def ingest_folder(lib_id: str, body: FolderBody):
        lib = lib_or_404(lib_id)
        if not Path(body.path).is_dir():
            raise HTTPException(400, "That folder does not exist.")
        return start("folder", f"Index {body.path}", lib, tasks.folder_task, body.model_dump())

    @app.put("/api/libraries/{lib_id}/upload")
    async def upload(lib_id: str, request: Request, name: str):
        lib = lib_or_404(lib_id)
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(name).name).strip(" .") or "upload"
        dest = lib.source_dir / "uploads"
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / safe
        size = 0
        with open(target, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > 2 << 30:
                    f.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(413, "That file is too large.")
                f.write(chunk)
        return {"ok": True, "name": safe, "bytes": size}

    @app.post("/api/libraries/{lib_id}/ingest/uploads")
    def ingest_uploads(lib_id: str):
        lib = lib_or_404(lib_id)
        return start("upload", "Index uploaded files", lib, tasks.upload_task, {})

    @app.post("/api/libraries/{lib_id}/refresh")
    def refresh(lib_id: str):
        lib = lib_or_404(lib_id)
        return start("refresh", f"Refresh {lib.name}", lib, tasks.refresh_task, {})

    @app.post("/api/libraries/{lib_id}/resume")
    def resume(lib_id: str):
        lib = lib_or_404(lib_id)
        return start("embed", f"Finish embedding {lib.name}", lib, tasks.resume_task, {})

    @app.post("/api/libraries/{lib_id}/reembed")
    def reembed(lib_id: str, body: EmbedderBody):
        lib = lib_or_404(lib_id)
        spec = clean_embedder(body.embedder)
        try:
            validate_embedder(spec, settings)
        except EmbeddingError as e:
            raise HTTPException(400, str(e)) from e
        return start("embed", f"Re-embed {lib.name}", lib, tasks.reembed_task, {"embedder": spec})

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def jobs():
        return {"jobs": ctx.jobs.list()}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        j = ctx.jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "Unknown job")
        return j.to_dict()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return {"ok": ctx.jobs.cancel(job_id)}

    # ------------------------------------------------------------------ search and chat
    @app.post("/api/search")
    def search(body: SearchBody):
        """Search the libraries directly, with the same meaning + keyword engine the chat uses. No model needed."""
        q = body.query.strip()
        if not q:
            raise HTTPException(400, "Type something to search for.")
        ids = [i for i in (body.libraries or [lib.id for lib in ctx.libs.list()]) if ctx.libs.get(i) is not None]
        r = settings.retrieval()
        kinds = ["code"] if body.scope == "code" else DOC_KINDS if body.scope == "docs" else []
        want = body.offset + body.limit + 1  # one extra tells us whether there is another page
        hits, debug = ctx.retriever.search(
            [q], ids, top_k=want, candidates=max(r["candidates"], min(300, want * 3)), max_per_doc=body.max_per_doc,
            rerank=bool(r["rerank"]) and body.mode != "keyword", reranker_model=r["reranker_model"],
            mode=body.mode, match=body.match, kinds=kinds, langs=body.langs, path_contains=body.path)
        terms = debug.get("terms", [])
        page = hits[body.offset: body.offset + body.limit]
        per_lib: dict[str, dict] = {}
        for h in hits:
            e = per_lib.setdefault(h.library_id, {"id": h.library_id, "name": h.library_name, "count": 0})
            e["count"] += 1
        return {"hits": [h.to_public(body.offset + i + 1, full_text=True, terms=terms) for i, h in enumerate(page)],
                "offset": body.offset, "limit": body.limit, "has_more": len(hits) > body.offset + body.limit,
                "shown": body.offset + len(page), "terms": terms, "mode": debug["mode"],
                "facets": {"libraries": list(per_lib.values())}, "debug": debug}

    @app.post("/api/chat")
    def chat(body: ChatBody):
        profile = settings.profile(body.profile_id)
        meta = {"profile": {"id": profile["id"], "name": profile.get("name"), "model": profile.get("model")} if profile else {},
                "libraries": [{"id": lib.id, "name": lib.name} for lib in (ctx.libs.get(i) for i in body.libraries) if lib is not None]}

        def events():
            # the history wrapper passes every event through and saves the turn (also a stopped one) when the stream ends
            stream = ctx.history.record(body.question, body.chat_id,
                                        ctx.engine.answer_stream(body.question, body.history, body.libraries, body.profile_id, body.options), meta)
            try:
                for ev in stream:
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            except GeneratorExit:  # the browser went away (Stop pressed): keep what was written so far
                stream.close()
                return
            except Exception as e:  # noqa: BLE001
                log.exception("chat stream failed")
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ native pickers
    @app.post("/api/pick-folder")
    def pick_folder():
        return {"path": host.pick_folder("Choose a folder") if host is not None else osutil.pick_folder("Choose a folder")}

    @app.post("/api/pick-files")
    def pick_files():
        return {"paths": host.pick_files("Choose files") if host is not None else osutil.pick_files("Choose files")}

    @app.post("/api/open-data-folder")
    def open_data():
        osutil.open_folder(str(paths.home()))
        return {"ok": True}

    # ------------------------------------------------------------------ the UI itself
    class FreshStaticFiles(StaticFiles):
        """The UI files change whenever the app is updated, so browsers must revalidate them (cheap 304s)
        instead of reusing a stale copy."""

        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache"
            return response

    web = paths.web_dir()
    if (web / "index.html").exists():
        app.mount("/", FreshStaticFiles(directory=str(web), html=True), name="web")
    else:  # pragma: no cover
        @app.get("/")
        def missing_ui():
            return JSONResponse({"error": "The web interface files are missing."}, status_code=500)

    return app
