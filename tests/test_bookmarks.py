"""Bookmarked sources in folders: the store, its rules (names, depth, moves), and the HTTP API the Bookmarks tab uses."""
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from helpers import TempHome

from perplexify import bookmarks
from perplexify.bookmarks import MAX_DEPTH, BookmarkError, BookmarkStore, clean_name, source_key


def src(n=1, **kw):
    """The fields of a source as the store keeps them."""
    base = {"library_id": "lib", "library": "JUCE docs", "doc_id": n, "uri": f"https://docs.example.com/page{n}.html",
            "title": f"Page {n}", "heading": f"Page {n} > Section", "anchor": None, "snippet": "Some text of the passage."}
    base.update(kw)
    return base


class Store(TempHome):
    def setUp(self):
        self.s = BookmarkStore(Path(self._home) / f"bookmarks-{time.time_ns()}.db")
        self.addCleanup(self.s.close)

    # ---- folders
    def test_folders_have_the_names_given_and_nest(self):
        a = self.s.create_folder("  Art   of 64 bit assembly ")
        self.assertEqual((a["name"], a["parent_id"], a["open"]), ("Art of 64 bit assembly", None, True))
        b = self.s.create_folder("Chapter 1", a["id"])
        self.assertEqual(b["parent_id"], a["id"])
        self.assertEqual([f["name"] for f in self.s.tree()["folders"]], ["Art of 64 bit assembly", "Chapter 1"])

    def test_names_must_be_real_and_unique_per_folder_ignoring_case(self):
        a = self.s.create_folder("Assembly")
        with self.assertRaises(BookmarkError):
            self.s.create_folder("   ")
        with self.assertRaises(BookmarkError) as why:
            self.s.create_folder("assembly")
        self.assertEqual(why.exception.status, 409)
        self.s.create_folder("assembly", a["id"])                       # another folder, another place: fine
        self.assertEqual(clean_name("a\tb\nc"), "a b c")
        self.assertEqual(len(clean_name("x" * 500)), 80)

    def test_rename_keeps_names_unique(self):
        a = self.s.create_folder("One")
        self.s.create_folder("Two")
        self.assertEqual(self.s.rename_folder(a["id"], "Uno")["name"], "Uno")
        self.assertEqual(self.s.rename_folder(a["id"], "UNO")["name"], "UNO")             # only its own old name: fine
        with self.assertRaises(BookmarkError) as why:
            self.s.rename_folder(a["id"], "two")
        self.assertEqual(why.exception.status, 409)
        with self.assertRaises(BookmarkError) as gone:
            self.s.rename_folder("nope", "x")
        self.assertEqual(gone.exception.status, 404)

    def test_folders_stop_at_the_depth_limit(self):
        parent = None
        for i in range(MAX_DEPTH):
            parent = self.s.create_folder(f"Level {i + 1}", parent["id"] if parent else None)
        with self.assertRaises(BookmarkError):
            self.s.create_folder("one too deep", parent["id"])

    def test_a_folder_moves_into_another_but_never_into_itself_or_what_it_holds(self):
        a, b = self.s.create_folder("A"), self.s.create_folder("B")
        inner = self.s.create_folder("Inner", a["id"])
        self.assertEqual(self.s.move_folder(b["id"], a["id"])["parent_id"], a["id"])
        with self.assertRaises(BookmarkError):
            self.s.move_folder(a["id"], a["id"])
        with self.assertRaises(BookmarkError):
            self.s.move_folder(a["id"], inner["id"])                      # into its own subfolder
        self.assertEqual(self.s.move_folder(inner["id"], None)["parent_id"], None)           # back to the top
        self.s.create_folder("B")                                          # a second B at the top...
        with self.assertRaises(BookmarkError) as why:
            self.s.move_folder(b["id"], None)                              # ...so the first cannot come up beside it
        self.assertEqual(why.exception.status, 409)

    def test_moving_a_deep_folder_cannot_break_the_depth_limit(self):
        chain, parent = [], None
        for i in range(MAX_DEPTH - 1):
            parent = self.s.create_folder(f"L{i}", parent["id"] if parent else None)
            chain.append(parent)
        loose = self.s.create_folder("Loose")
        self.s.create_folder("Child", loose["id"])                        # Loose is 2 levels tall now
        with self.assertRaises(BookmarkError):
            self.s.move_folder(loose["id"], chain[-1]["id"])

    def test_open_and_closed_is_remembered(self):
        a = self.s.create_folder("A")
        self.assertTrue(self.s.set_open(a["id"], False))
        self.assertFalse(self.s.get_folder(a["id"])["open"])
        self.s.set_all_open(True)
        self.assertTrue(self.s.get_folder(a["id"])["open"])
        self.assertFalse(self.s.set_open("nope", True))

    def test_deleting_a_folder_removes_what_is_inside_and_says_how_much(self):
        a = self.s.create_folder("A")
        inner = self.s.create_folder("Inner", a["id"])
        self.s.add_item(src(1), a["id"])
        self.s.add_item(src(2), inner["id"])
        keep, _ = self.s.add_item(src(3))
        self.assertEqual(self.s.delete_folder(a["id"]), {"folders": 2, "items": 2})
        self.assertEqual(self.s.stats(), {"folders": 0, "items": 1})
        self.assertEqual([i["id"] for i in self.s.tree()["items"]], [keep["id"]])
        with self.assertRaises(BookmarkError):
            self.s.delete_folder(a["id"])

    # ---- bookmarks
    def test_a_source_is_saved_once_and_the_second_save_returns_the_first(self):
        first, created = self.s.add_item(src(1, anchor="L10-L20"))
        self.assertTrue(created)
        self.assertEqual(first["key"], "lib|1|L10-L20")
        again, created2 = self.s.add_item(src(1, anchor="L10-L20", title="renamed by the page"))
        self.assertFalse(created2)
        self.assertEqual((again["id"], again["title"]), (first["id"], "Page 1"))
        other, created3 = self.s.add_item(src(1, anchor="L30-L40"))                       # another passage of the same document
        self.assertTrue(created3)
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(self.s.stats()["items"], 2)

    def test_saving_into_a_folder_and_saving_again_elsewhere_moves_it(self):
        a, b = self.s.create_folder("A"), self.s.create_folder("B")
        item, _ = self.s.add_item(src(1), a["id"])
        self.assertEqual(item["folder_id"], a["id"])
        moved, created = self.s.add_item(src(1), b["id"])
        self.assertEqual((created, moved["folder_id"], moved["id"]), (False, b["id"], item["id"]))
        with self.assertRaises(BookmarkError):
            self.s.add_item(src(2), "nope")

    def test_bookmarks_move_between_folders_and_to_the_top(self):
        a = self.s.create_folder("A")
        item, _ = self.s.add_item(src(1))
        self.assertEqual(self.s.move_item(item["id"], a["id"])["folder_id"], a["id"])
        self.assertTrue(self.s.get_folder(a["id"])["open"])
        self.assertIsNone(self.s.move_item(item["id"], None)["folder_id"])
        with self.assertRaises(BookmarkError):
            self.s.move_item(item["id"], "nope")
        with self.assertRaises(BookmarkError):
            self.s.move_item("nope", None)

    def test_rename_note_and_delete(self):
        item, _ = self.s.add_item(src(1))
        self.assertEqual(self.s.rename_item(item["id"], "  The mixer  ")["title"], "The mixer")
        self.assertEqual(self.s.set_note(item["id"], " remember this ")["note"], "remember this")
        with self.assertRaises(BookmarkError):
            self.s.rename_item(item["id"], "  ")
        self.assertTrue(self.s.delete_item(item["id"]))
        self.assertFalse(self.s.delete_item(item["id"]))
        self.assertIsNone(self.s.get_item(item["id"]))

    def test_a_reindexed_document_is_followed(self):
        item, _ = self.s.add_item(src(1, anchor="L5-L9"))
        moved = self.s.relink(item["id"], 7)
        self.assertEqual((moved["doc_id"], moved["key"]), (7, "lib|7|L5-L9"))
        self.assertIsNotNone(self.s.find_item("lib|7|L5-L9"))
        self.assertIsNone(self.s.find_item("lib|1|L5-L9"))
        self.s.add_item(src(9, anchor="L5-L9"))                                    # the number 9 is already taken by another save
        self.assertEqual(self.s.relink(item["id"], 9)["doc_id"], 7)                # so it stays where it is

    def test_the_tree_lists_everything_newest_first(self):
        self.s.create_folder("A")
        first, _ = self.s.add_item(src(1))
        second, _ = self.s.add_item(src(2))
        self.assertEqual([i["id"] for i in self.s.tree()["items"]], [second["id"], first["id"]])

    def test_the_helpers(self):
        self.assertEqual(source_key("lib", 3, None), "lib|3|")
        self.assertEqual(bookmarks.MAX_DEPTH, 8)

    def test_a_bookmark_survives_closing_and_reopening_the_file(self):
        path = Path(self._home) / "reopen.db"
        one = BookmarkStore(path)
        f = one.create_folder("Kept")
        one.add_item(src(1), f["id"])
        one.close()
        two = BookmarkStore(path)
        self.addCleanup(two.close)
        self.assertEqual(two.stats(), {"folders": 1, "items": 1})


class Api(TempHome):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from perplexify.context import AppContext
        from perplexify.server import create_app

        cls.ctx = AppContext()
        cls.client = TestClient(create_app(cls.ctx), base_url="http://127.0.0.1")
        cls.lib = cls.ctx.libs.create("Assembly book")
        cls.doc = cls.lib.store.replace_document("https://example.com/regs.html", "Registers", None, "mirror", "html", "h", "t", 10, [], "")
        cls.code = cls.lib.store.replace_document("repo://x/mixer.cpp", "src/mixer.cpp", "C:/nowhere/mixer.cpp", "external", "code", "h2", "t2", 10, [], "cpp")

    def setUp(self):
        tree = self.client.get("/api/bookmarks").json()
        for f in tree["folders"]:
            if f["parent_id"] is None:
                self.client.delete("/api/bookmarks/folders/" + f["id"])
        for i in self.client.get("/api/bookmarks").json()["items"]:
            self.client.delete("/api/bookmarks/items/" + i["id"])

    def save(self, doc=None, **kw):
        body = {"library_id": self.lib.id, "doc_id": doc or self.doc, "title": "Registers", "heading": "Registers > rax", "snippet": "text", **kw}
        r = self.client.post("/api/bookmarks/items", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_source_is_saved_with_what_the_library_knows_about_it(self):
        out = self.save(anchor="rax")
        self.assertTrue(out["created"])
        item = out["item"]
        self.assertEqual((item["uri"], item["library"], item["kind"], item["available"]), ("https://example.com/regs.html", "Assembly book", "html", True))
        self.assertEqual(item["online_url"], "https://example.com/regs.html#rax")
        self.assertFalse(self.save(anchor="rax")["created"])
        tree = self.client.get("/api/bookmarks").json()
        self.assertEqual([i["title"] for i in tree["items"]], ["Registers"])

    def test_a_code_passage_keeps_its_lines_and_links_to_the_file(self):
        item = self.save(doc=self.code, anchor="L10-L20", title="src/mixer.cpp:10-20")["item"]
        self.assertEqual((item["lang"], item["kind"], item["anchor"]), ("cpp", "code", "L10-L20"))
        self.assertTrue(item["local_url"].startswith(f"/api/libraries/{self.lib.id}/file?doc={self.code}"))

    def test_saving_something_that_is_not_there_is_a_clear_error(self):
        r = self.client.post("/api/bookmarks/items", json={"library_id": "nope", "doc_id": 1})
        self.assertEqual((r.status_code, "library" in r.json()["detail"]), (404, True))
        r = self.client.post("/api/bookmarks/items", json={"library_id": self.lib.id, "doc_id": 99999})
        self.assertEqual(r.status_code, 404)

    def test_folders_through_the_api(self):
        a = self.client.post("/api/bookmarks/folders", json={"name": "Art of 64 bit assembly"}).json()
        b = self.client.post("/api/bookmarks/folders", json={"name": "Chapter 1", "parent_id": a["id"]}).json()
        dup = self.client.post("/api/bookmarks/folders", json={"name": "chapter 1", "parent_id": a["id"]})
        self.assertEqual((dup.status_code, "already a folder" in dup.json()["detail"]), (409, True))
        self.assertEqual(self.client.post("/api/bookmarks/folders", json={"name": " "}).status_code, 400)
        self.assertEqual(self.client.patch("/api/bookmarks/folders/" + b["id"], json={"name": "Registers", "open": False}).json()["name"], "Registers")
        moved = self.client.post(f"/api/bookmarks/folders/{b['id']}/move", json={"parent_id": None}).json()
        self.assertIsNone(moved["parent_id"])
        self.assertEqual(self.client.post(f"/api/bookmarks/folders/{a['id']}/move", json={"parent_id": a["id"]}).status_code, 400)
        self.assertEqual(self.client.patch("/api/bookmarks/folders/nope", json={"name": "x"}).status_code, 404)

    def test_bookmarks_move_rename_and_delete_through_the_api(self):
        folder = self.client.post("/api/bookmarks/folders", json={"name": "Reading"}).json()
        item = self.save(folder_id=folder["id"])["item"]
        self.assertEqual(item["folder_id"], folder["id"])
        self.assertEqual(self.client.patch("/api/bookmarks/items/" + item["id"], json={"title": "Register notes", "note": "start here"}).json()["title"], "Register notes")
        self.assertIsNone(self.client.post(f"/api/bookmarks/items/{item['id']}/move", json={"folder_id": None}).json()["folder_id"])
        self.assertEqual(self.client.delete("/api/bookmarks/items/" + item["id"]).json(), {"ok": True})
        self.assertEqual(self.client.patch("/api/bookmarks/items/" + item["id"], json={"title": "x"}).status_code, 404)

    def test_deleting_a_folder_reports_what_went_with_it(self):
        folder = self.client.post("/api/bookmarks/folders", json={"name": "Temp"}).json()
        self.save(folder_id=folder["id"])
        self.save(doc=self.code, anchor="L1-L2", folder_id=folder["id"])
        self.assertEqual(self.client.delete("/api/bookmarks/folders/" + folder["id"]).json(), {"folders": 1, "items": 2})

    def test_a_bookmark_whose_library_or_document_is_gone_is_shown_as_unavailable(self):
        lib = self.ctx.libs.create("Short lived")
        doc = lib.store.replace_document("https://example.com/x.html", "X", None, "mirror", "html", "h", "t", 1, [], "")
        item = self.client.post("/api/bookmarks/items", json={"library_id": lib.id, "doc_id": doc, "title": "X"}).json()["item"]
        lib.store.delete_document(doc)
        self.assertFalse(next(i for i in self.client.get("/api/bookmarks").json()["items"] if i["id"] == item["id"])["available"])
        self.client.delete("/api/libraries/" + lib.id)
        gone = next(i for i in self.client.get("/api/bookmarks").json()["items"] if i["id"] == item["id"])
        self.assertEqual((gone["library_exists"], gone["available"]), (False, False))

    def test_open_all_and_close_all(self):
        a = self.client.post("/api/bookmarks/folders", json={"name": "A"}).json()
        self.client.post("/api/bookmarks/open-all", json={"open": False})
        self.assertFalse(next(f for f in self.client.get("/api/bookmarks").json()["folders"] if f["id"] == a["id"])["open"])

    def test_clearing_the_history_leaves_bookmarks_alone(self):
        self.save()
        self.ctx.history.clear_all()
        self.assertEqual(len(self.client.get("/api/bookmarks").json()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
