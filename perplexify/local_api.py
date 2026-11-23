"""HTTP API for local models (llama.cpp). Everything stays on this computer; see localllm.py for what is behind it.

    GET  /api/local/status               engine found?, running model, hardware, the "Local model" profile
    GET  /api/local/models               downloaded and chosen model files + the curated list with state
    POST /api/local/models/download      {id}            start downloading a curated model (a job: progress in /api/jobs)
    POST /api/local/models/delete        {id}            delete a downloaded model, or forget a chosen file (it is never deleted)
    POST /api/local/models/link          {path}          use a .gguf file where it is; without a path the file dialog opens
    POST /api/local/connect              {model, context_tokens?, gpu?}   create / update the profile and make it the active one
    POST /api/local/start                                load the model in the background (poll /status)
    POST /api/local/stop                                 unload it (frees the memory)
    POST /api/local/test                                 load if needed, ask one tiny question, report the speed
    POST /api/local/engine/download                      fetch the pinned llama.cpp build (when running from the source)
"""
from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from . import localllm, paths, providers
from .llm import LLMError, make_provider
from .settings import GPU_MODES

log = logging.getLogger("perplexify.local")


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class IdBody(_Body):
    id: str


class LinkBody(_Body):
    path: str = ""


class ConnectBody(_Body):
    model: str
    context_tokens: int | None = None
    gpu: str = "auto"


class _StopWhenTheBrowserLeaves:
    """Pure ASGI middleware for POST /api/chat. When the browser closes the answer stream (the Stop button, a closed window) the
    web framework (Starlette with uvicorn) cancels the response, but it leaves the generator behind the stream suspended until
    the garbage collector closes it, which can take a long while. The connection to llama.cpp would stay open meanwhile and the
    engine would generate the whole answer for nobody. So the disconnect is watched here, and it cancels what the local engine
    is doing for this request (localllm.RequestScope)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/api/chat":
            await self.app(scope, receive, send)
            return
        request_scope = localllm.RequestScope()
        token = localllm.current_scope.set(request_scope)

        async def watched_receive():
            message = await receive()
            if message["type"] == "http.disconnect":
                request_scope.cancel()
            return message

        try:
            await self.app(scope, watched_receive, send)
        finally:
            localllm.current_scope.reset(token)
            request_scope.cancel()                            # whatever is still open now belongs to a finished request


def stop_engine_on_shutdown(app) -> None:
    """Hooks into the app for local models: stop llama-server when the app's web server shuts down (window closed, /api/shutdown,
    Ctrl+C), and stop the engine's generation when the browser closes an answer stream. The engine is also stopped when the
    process exits and, whatever happens to the process, by the job object that ties it to the app (see localllm.py)."""
    app.add_middleware(_StopWhenTheBrowserLeaves)
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        try:
            async with inner(a) as state:
                yield state
        finally:
            localllm.shutdown()

    app.router.lifespan_context = lifespan


def make_router(ctx) -> APIRouter:
    router = APIRouter(prefix="/api/local")
    settings = ctx.settings
    engine_job: dict = {}

    def local_profile() -> dict | None:
        return next((p for p in settings.get()["profiles"] if p["id"] == "local"), None)

    def entries() -> list[dict]:
        return localllm.installed_models(settings.linked_models())

    def find_entry(model_id: str) -> dict | None:
        return next((e for e in entries() if e["id"] == model_id or e["file"] == model_id), None)

    def downloading(item_id: str):
        job = localllm.active_download(item_id)
        return job.to_dict() if job is not None else None

    def public_profile(p: dict | None) -> dict | None:
        if p is None:
            return None
        q = {k: v for k, v in p.items() if k != "api_key_enc"}
        q["has_key"] = True
        return q

    def provider_for(p: dict):
        return make_provider(p, "")

    # ------------------------------------------------------------------ status
    @router.get("/status")
    def status():
        p = local_profile()
        prof = None
        if p is not None:
            path = localllm.resolve_model_path(p)
            prof = {"model": p.get("model"), "model_path": p.get("model_path"), "context_tokens": p.get("context_tokens"),
                    "gpu": p.get("gpu") or "auto", "active": settings.get()["active_profile"] == "local", "model_found": path is not None}
            if path is not None:
                n = localllm.resolve_context(p, path)
                size = path.stat().st_size
                prof.update(resolved_context=n, context_length=localllm.read_gguf_info(path).get("context_length"),
                            fit=localllm.fit_hint(size, n))
        job = engine_job.get("job")
        return {"engine": localllm.engine_info(), "running": localllm.manager().status(), "hardware": localllm.hardware(),
                "profile": prof, "context_choices": list(localllm.CONTEXT_CHOICES),
                "engine_job": job.to_dict() if job is not None and job.status in ("queued", "running") else None}

    # ------------------------------------------------------------------ models
    @router.get("/models")
    def models():
        active = (local_profile() or {}).get("model")
        hw = localllm.hardware()
        installed = []
        for e in entries():
            installed.append({**e, "active": e["file"] == active,
                              "fit": localllm.fit_hint(e["size"], 8192) if e["exists"] else None,
                              "auto_context": localllm.auto_context(e["size"], e["context_length"], hw["ram_total"])})
        catalog = []
        for c in localllm.CATALOG:
            st = localllm.catalog_state(c)
            job = downloading(c["id"])
            catalog.append({"id": c["id"], "name": c["name"], "params": c["params"], "quant": c["quant"], "license": c["license"],
                            "size": c["size"], "ram_gb": c["ram_gb"], "summary": c["summary"], "tags": c["tags"], "file": c["file"],
                            "repo": c["repo"], "state": "downloading" if job else st["state"], "bytes": st["bytes"], "job": job,
                            "fit": localllm.fit_hint(c["size"], 8192)})
        return {"dir": str(localllm.models_dir()), "installed": installed, "catalog": catalog, "hardware": hw}

    @router.post("/models/download")
    def download(body: IdBody):
        try:
            job = localllm.start_model_download(ctx.jobs, body.id)
        except ValueError as e:
            raise HTTPException(409 if "already" in str(e) else 404, str(e)) from e
        return {"job": job.to_dict()}

    @router.post("/models/delete")
    def delete(body: IdBody):
        e = find_entry(body.id)
        if e is None:
            raise HTTPException(404, "That model is not in the list.")
        if e["catalog_id"] and localllm.active_download(e["catalog_id"]):
            raise HTTPException(409, "That model is still being downloaded. Stop the download first.")
        running = localllm.manager().status()
        if running.get("state") in ("ready", "loading") and Path(running.get("model_path", "")) == Path(e["path"]):
            localllm.stop_engine()                          # a model in use cannot be deleted: unload it first
        deleted = False
        if e["linked"]:
            settings.unlink_model(e["path"])                # a file the user chose is only forgotten, never deleted
        else:
            try:
                deleted = localllm.delete_model_file(Path(e["path"]))
            except ValueError as err:
                raise HTTPException(400, str(err)) from err
        p = local_profile()
        if p is not None and p.get("model") == e["file"] and (not p.get("model_path") or Path(p["model_path"]) == Path(e["path"])):
            settings.update({"profiles": [{"id": "local", "model": "", "model_path": ""}]})     # the profile no longer points at it
        return {"ok": True, "deleted": deleted, "unlinked": bool(e["linked"])}

    @router.post("/models/link")
    def link(body: LinkBody):
        raw = body.path.strip().strip('"')
        if not raw:
            raw = localllm.pick_model_file() or ""
            if not raw:
                return {"cancelled": True}
        p = Path(raw).expanduser()
        if not p.is_file():
            raise HTTPException(400, f"That file does not exist: {p}")
        g = localllm.read_gguf_info(p)
        if not g.get("ok"):
            raise HTTPException(400, f"{p.name} cannot be used: {g.get('error')}.")
        clash = next((e for e in entries() if e["file"].lower() == p.name.lower() and Path(e["path"]) != p), None)
        if clash is not None:
            raise HTTPException(400, f"A model called {p.name} is already in the list ({clash['path']}). Rename one of the files first.")
        settings.link_model(str(p))
        return {"ok": True, "model": find_entry(p.name)}

    # ------------------------------------------------------------------ the profile
    @router.post("/connect")
    def connect(body: ConnectBody):
        e = find_entry(body.model)
        if e is None or not e["exists"]:
            raise HTTPException(404, "That model file was not found. Choose another one, or download it first.")
        if not e["valid"]:
            raise HTTPException(400, f"{e['file']} cannot be used: {e['error']}.")
        gpu = body.gpu if body.gpu in GPU_MODES else "auto"
        n = body.context_tokens if body.context_tokens and body.context_tokens >= 1024 else None
        profile = settings.connect_local(e["file"], e["path"] if e["linked"] else "", n, gpu)
        try:                                                 # an engine started for other settings would hold memory for nothing
            running = localllm.manager().current()
            if running is not None and running.spec != localllm.spec_for_profile(profile):
                localllm.stop_engine()
        except LLMError:
            pass
        info = provider_for(profile).info()
        return {"ok": True, "profile": public_profile(profile), "info": info, "fit": localllm.fit_hint(e["size"], info["context_window"]),
                "embeddings_note": (providers.get_chat_provider("local") or {}).get("embeddings_note")}

    @router.post("/start")
    def start():
        p = local_profile()
        if p is None:
            raise HTTPException(400, "No local model is connected yet.")
        try:
            provider_for(p).begin()
        except LLMError as e:
            raise HTTPException(400, str(e)) from e
        return localllm.manager().status()

    @router.post("/stop")
    def stop():
        localllm.stop_engine()
        return localllm.manager().status()

    @router.post("/test")
    def test():
        p = local_profile()
        if p is None:
            raise HTTPException(400, "No local model is connected yet.")
        provider = provider_for(p)
        t0 = time.perf_counter()
        try:
            handle = provider.engine()
            t1 = time.perf_counter()
            reply = provider.complete([{"role": "user", "content": "Reply with the single word OK."}], quick=True)
        except LLMError as e:
            raise HTTPException(400, str(e)) from e
        t2 = time.perf_counter()
        return {"ok": True, "reply": reply[:200], "load_seconds": round(t1 - t0, 1), "answer_seconds": round(t2 - t1, 1),
                "n_ctx": handle.n_ctx, "gpu": handle.gpu}

    # ------------------------------------------------------------------ the engine itself
    @router.post("/engine/download")
    def engine_download():
        job = engine_job.get("job")
        if job is not None and job.status in ("queued", "running"):
            return {"job": job.to_dict()}

        def work(j) -> None:
            try:
                localllm.download_engine(paths.llama_dir(), paths.home() / "downloads", progress=localllm.job_progress(j), check=j.check,
                                         keep_archive=False)       # the 38 MB zip is not needed again once it is unpacked
            except localllm.DownloadError as e:
                raise RuntimeError(str(e)) from e
            j.update(phase="done", message="The engine is ready.")

        job = localllm.start_job(ctx.jobs, "llm-engine", "Download the llama.cpp engine", work)
        engine_job["job"] = job
        return {"job": job.to_dict()}

    return router
