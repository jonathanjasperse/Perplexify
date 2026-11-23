"""HTTP API for the chat and search history (see history.py). Everything stays on this computer."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RenameBody(_Body):
    title: str


class ConfigBody(_Body):
    enabled: bool


class SearchNote(_Body):
    query: str
    filters: dict = {}
    libraries: list[str] = []          # library ids; the names are looked up here
    results: int = 0


def make_router(ctx) -> APIRouter:
    router = APIRouter(prefix="/api/history")
    store = ctx.history

    def libraries(ids: list[str]) -> list[dict]:
        out = []
        for i in ids[:50]:
            lib = ctx.libs.get(i)
            if lib is not None:
                out.append({"id": lib.id, "name": lib.name})
        return out

    @router.get("")
    def overview():
        return store.stats()

    @router.put("/config")
    def config(body: ConfigBody):
        store.set_enabled(body.enabled)
        return store.stats()

    # ------------------------------------------------------------------ chats
    @router.get("/chats")
    def chats(q: str = "", limit: int = Query(60, ge=1, le=200), offset: int = Query(0, ge=0)):
        return store.list_chats(q, limit, offset)

    @router.get("/chats/{chat_id}")
    def chat(chat_id: str):
        found = store.get_chat(chat_id)
        if found is None:
            raise HTTPException(404, "That chat is not in the history any more.")
        return found

    @router.patch("/chats/{chat_id}")
    def rename(chat_id: str, body: RenameBody):
        if not store.rename_chat(chat_id, body.title):
            raise HTTPException(404, "That chat is not in the history any more.")
        return {"ok": True}

    @router.delete("/chats/{chat_id}")
    def delete_chat(chat_id: str):
        return {"ok": store.delete_chat(chat_id)}

    @router.delete("/chats")
    def clear_chats():
        return {"deleted": store.clear_chats()}

    # ------------------------------------------------------------------ searches
    @router.get("/searches")
    def searches(q: str = "", limit: int = Query(80, ge=1, le=300), offset: int = Query(0, ge=0)):
        return store.list_searches(q, limit, offset)

    @router.post("/searches")
    def note_search(body: SearchNote):
        sid = store.note_search(body.query, body.filters, libraries(body.libraries), body.results)
        return {"id": sid}

    @router.delete("/searches/{search_id}")
    def delete_search(search_id: int):
        return {"ok": store.delete_search(search_id)}

    @router.delete("/searches")
    def clear_searches():
        return {"deleted": store.clear_searches()}

    # ------------------------------------------------------------------ typing help
    @router.get("/suggest")
    def suggest(q: str = "", limit: int = Query(8, ge=1, le=20), prefer: str = ""):
        """What you typed before that starts like `q`; the most recent things when `q` is empty."""
        if not store.enabled:
            return {"suggestions": []}
        return {"suggestions": store.suggest(q, limit, prefer)}

    @router.get("/related")
    def related(q: str = "", limit: int = Query(6, ge=1, le=20)):
        """Earlier questions and searches about the same things as `q`."""
        if not store.enabled:
            return {"related": []}
        return {"related": store.related(q, limit)}

    return router
