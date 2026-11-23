"""HTTP API for the bookmarked sources and their folders (see bookmarks.py)."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .bookmarks import BookmarkError


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FolderCreate(_Body):
    name: str
    parent_id: str | None = None


class FolderPatch(_Body):
    name: str | None = None
    open: bool | None = None


class MoveBody(_Body):
    parent_id: str | None = None        # folders: the folder to go into; null is the top
    folder_id: str | None = None        # bookmarks: the same, null is the top


class OpenAll(_Body):
    open: bool


class ItemCreate(_Body):
    library_id: str
    doc_id: int
    anchor: str | None = None
    title: str = ""
    heading: str = ""
    snippet: str = ""
    folder_id: str | None = None


class ItemPatch(_Body):
    title: str | None = None
    note: str | None = None


def make_router(ctx) -> APIRouter:
    router = APIRouter(prefix="/api/bookmarks")
    store = ctx.bookmarks

    def fail(e: BookmarkError):
        raise HTTPException(e.status, str(e))

    def links(lib_id: str, row, anchor: str | None) -> tuple[str | None, str | None]:
        """(original online address, address served by this app) of the document, like a search hit's links."""
        frag = ""
        if anchor:
            frag = "#" + (anchor if anchor.startswith("page=") else quote(anchor))
        online = (row["uri"] + frag) if row["uri"].startswith(("http://", "https://")) else None
        local = None
        if row["origin"] == "mirror" and row["path"]:
            local = f"/mirror/{lib_id}/{quote(row['path'])}{frag}"
        elif row["origin"] == "external":
            local = f"/api/libraries/{lib_id}/file?doc={row['id']}{frag}"
        return online, local

    def describe(item: dict) -> dict:
        """The bookmark as the page needs it: what is stored, plus whether the document is still in its library."""
        out = dict(item)
        out.update(available=False, library_exists=False, kind="", lang="", online_url=None, local_url=None)
        lib = ctx.libs.get(item["library_id"])
        if lib is None:
            return out
        out["library_exists"] = True
        out["library"] = lib.name
        row = lib.store.doc_by_uri(item["uri"]) if item["uri"] else lib.store.doc_by_id(item["doc_id"])
        if row is None:
            return out
        if row["id"] != item["doc_id"]:                      # re-indexed under a new number: follow it
            moved = store.relink(item["id"], row["id"])
            if moved is not None:
                out.update(doc_id=moved["doc_id"], key=moved["key"])
        online, local = links(item["library_id"], row, item["anchor"])
        out.update(available=True, kind=row["kind"], lang=row["lang"], online_url=online, local_url=local)
        return out

    @router.get("")
    def tree():
        data = store.tree()
        data["items"] = [describe(i) for i in data["items"]]
        return data

    # ------------------------------------------------------------------ folders
    @router.post("/folders")
    def create_folder(body: FolderCreate):
        try:
            return store.create_folder(body.name, body.parent_id)
        except BookmarkError as e:
            fail(e)

    @router.patch("/folders/{folder_id}")
    def patch_folder(folder_id: str, body: FolderPatch):
        try:
            if body.name is not None:
                store.rename_folder(folder_id, body.name)
            if body.open is not None:
                store.set_open(folder_id, body.open)
            f = store.get_folder(folder_id)
        except BookmarkError as e:
            fail(e)
        if f is None:
            raise HTTPException(404, "That folder does not exist any more.")
        return f

    @router.post("/folders/{folder_id}/move")
    def move_folder(folder_id: str, body: MoveBody):
        try:
            return store.move_folder(folder_id, body.parent_id)
        except BookmarkError as e:
            fail(e)

    @router.post("/open-all")
    def open_all(body: OpenAll):
        store.set_all_open(body.open)
        return {"ok": True}

    @router.delete("/folders/{folder_id}")
    def delete_folder(folder_id: str):
        try:
            return store.delete_folder(folder_id)
        except BookmarkError as e:
            fail(e)

    # ------------------------------------------------------------------ bookmarks
    @router.post("/items")
    def create_item(body: ItemCreate):
        lib = ctx.libs.get(body.library_id)
        if lib is None:
            raise HTTPException(404, "That library is not here any more.")
        row = lib.store.doc_by_id(body.doc_id)
        if row is None:
            raise HTTPException(404, "That page or file is not in the library any more.")
        fields = {"library_id": lib.id, "library": lib.name, "doc_id": row["id"], "uri": row["uri"], "anchor": body.anchor,
                  "title": body.title or row["title"], "heading": body.heading, "snippet": body.snippet}
        try:
            item, created = store.add_item(fields, body.folder_id)
        except BookmarkError as e:
            fail(e)
        return {"item": describe(item), "created": created}

    @router.patch("/items/{item_id}")
    def patch_item(item_id: str, body: ItemPatch):
        try:
            if body.title is not None:
                store.rename_item(item_id, body.title)
            if body.note is not None:
                store.set_note(item_id, body.note)
        except BookmarkError as e:
            fail(e)
        item = store.get_item(item_id)
        if item is None:
            raise HTTPException(404, "That bookmark does not exist any more.")
        return describe(item)

    @router.post("/items/{item_id}/move")
    def move_item(item_id: str, body: MoveBody):
        try:
            return describe(store.move_item(item_id, body.folder_id))
        except BookmarkError as e:
            fail(e)

    @router.delete("/items/{item_id}")
    def delete_item(item_id: str):
        return {"ok": store.delete_item(item_id)}

    return router
