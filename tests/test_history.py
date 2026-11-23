"""Chat and search history: what is saved, what comes back, the suggestions and the related queries."""
import json
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from helpers import TempHome

from perplexify import history
from perplexify.history import HistoryStore, content_words, fts_query, norm


def answer(question="how do I implement processBlock", text=("Call it ", "once [1]."), sources=1, usage=True):
    """What the answer stream of the app looks like (see rag.py), as a generator."""
    def stream():
        yield {"type": "status", "text": "searching"}
        yield {"type": "rewrite", "query": "standalone " + question}
        if sources:
            yield {"type": "sources", "sources": [{"n": i + 1, "title": f"Page {i + 1}", "library_id": "lib", "snippet": "text"}
                                                  for i in range(sources)], "debug": {"vector_hits": 3}}
        yield {"type": "status", "text": "generating"}
        for t in text:
            yield {"type": "token", "text": t}
        if usage:
            yield {"type": "usage", "prompt_tokens": 100, "completion_tokens": 5}
        yield {"type": "done", "elapsed_ms": 12}
    return stream()


META = {"profile": {"id": "p1", "name": "ChatGPT", "model": "gpt"}, "libraries": [{"id": "lib", "name": "JUCE docs"}]}


class Store(TempHome):
    def setUp(self):
        self.h = HistoryStore(self._home + "/history-" + str(time.time_ns()) + ".db")
        self.addCleanup(self.h.close)

    def ask(self, question="how do I implement processBlock", chat_id=None, **kw):
        return list(self.h.record(question, chat_id, answer(question, **kw), META))

    # ---- recording
    def test_a_turn_passes_through_unchanged_and_is_saved(self):
        events = self.ask()
        self.assertEqual(events[0]["type"], "chat")
        self.assertTrue(events[0]["new"])
        self.assertEqual([e["type"] for e in events[1:]], ["status", "rewrite", "sources", "status", "token", "token", "usage", "done"])
        chats = self.h.list_chats()["chats"]
        self.assertEqual(len(chats), 1)
        self.assertEqual((chats[0]["title"], chats[0]["turns"], chats[0]["sources"]), ("how do I implement processBlock", 1, 1))
        self.assertEqual(chats[0]["profile"]["name"], "ChatGPT")
        self.assertEqual(chats[0]["libraries"], [{"id": "lib", "name": "JUCE docs"}])
        self.assertEqual(chats[0]["preview"], "Call it once.")                         # plain text: the citation marker is dropped

    def test_a_saved_chat_has_the_answer_the_sources_and_the_trace_to_show_it_again(self):
        cid = self.ask()[0]["id"]
        chat = self.h.get_chat(cid)
        turn = chat["turns_list"][0]
        self.assertEqual((turn["question"], turn["answer"]), ("how do I implement processBlock", "Call it once [1]."))
        kinds = [e["type"] for e in turn["events"]]
        self.assertEqual(kinds, ["rewrite", "sources", "usage", "done"])             # status and token events are not kept
        self.assertEqual(turn["events"][1]["sources"][0]["title"], "Page 1")
        self.assertFalse(turn["stopped"])
        self.assertEqual(turn["model"], "gpt")                                          # which model wrote this turn's answer

    def test_a_follow_up_joins_the_same_chat(self):
        cid = self.ask()[0]["id"]
        second = self.ask("and its parameters?", chat_id=cid)
        self.assertEqual((second[0]["id"], second[0]["new"], second[0]["title"]), (cid, False, "how do I implement processBlock"))
        chat = self.h.get_chat(cid)
        self.assertEqual([t["question"] for t in chat["turns_list"]], ["how do I implement processBlock", "and its parameters?"])
        self.assertEqual((chat["turns"], chat["sources"]), (2, 2))
        self.assertEqual(self.h.list_chats()["total"], 1)

    def test_an_unknown_or_malformed_chat_id_starts_a_new_chat(self):
        weird = self.ask(chat_id="../../etc")[0]
        self.assertTrue(weird["new"] and weird["id"] != "../../etc" and len(weird["id"]) == 12)
        fresh = self.ask(chat_id="0123456789ab")[0]
        self.assertEqual((fresh["id"], fresh["new"]), ("0123456789ab", True))

    def test_a_stopped_answer_is_kept_as_far_as_it_got(self):
        stream = self.h.record("explain mixing", None, answer("explain mixing", text=("Part one, ", "part two", "...")), META)
        for ev in stream:
            if ev["type"] == "token":
                break                       # the page pressed Stop
        stream.close()
        chat = self.h.get_chat(self.h.list_chats()["chats"][0]["id"])
        turn = chat["turns_list"][0]
        self.assertEqual(turn["answer"], "Part one, ")
        self.assertTrue(turn["stopped"])

    def test_a_failed_question_without_answer_or_sources_is_not_saved(self):
        def failing():
            yield {"type": "error", "message": "no model"}
        list(self.h.record("hello", None, failing(), META))
        self.assertEqual(self.h.list_chats()["total"], 0)

    def test_an_exception_in_the_stream_still_passes_through_and_saves_what_exists(self):
        def broken():
            yield {"type": "sources", "sources": [{"n": 1, "title": "T", "library_id": "x", "snippet": ""}], "debug": {}}
            raise RuntimeError("provider down")
        with self.assertRaises(RuntimeError):
            list(self.h.record("hello there", None, broken(), META))
        turn = self.h.get_chat(self.h.list_chats()["chats"][0]["id"])["turns_list"][0]
        self.assertEqual(turn["events"][-1], {"type": "error", "message": "provider down"})

    def test_nothing_is_recorded_while_history_is_off(self):
        self.h.set_enabled(False)
        events = self.ask()
        self.assertNotIn("chat", [e["type"] for e in events])
        self.assertEqual(self.h.list_chats()["total"], 0)
        self.assertIsNone(self.h.note_search("audio buffer"))
        self.h.set_enabled(True)
        self.assertEqual(self.ask()[0]["type"], "chat")

    def test_history_failing_never_breaks_answering(self):
        with mock.patch.object(HistoryStore, "_save_turn", side_effect=RuntimeError("disk full")), \
                self.assertLogs("perplexify.history", level="WARNING"):
            events = self.ask()
        self.assertIn("done", [e["type"] for e in events])

    # ---- reading and searching
    def fill(self):
        self.ask("how do I implement processBlock", text=("Use the AudioBuffer argument.",))
        self.ask("what is a ValueTree", text=("A ValueTree stores a tree of properties.",))
        self.ask("how to play a MIDI note", text=("Send a note on message to the synthesiser.",))

    def test_chats_are_listed_newest_first_with_paging(self):
        self.fill()
        listing = self.h.list_chats()
        self.assertEqual([c["title"] for c in listing["chats"]], ["how to play a MIDI note", "what is a ValueTree", "how do I implement processBlock"])
        page = self.h.list_chats(limit=1, offset=1)
        self.assertEqual(([c["title"] for c in page["chats"]], page["total"]), (["what is a ValueTree"], 3))

    def test_searching_the_history_finds_questions_and_answers_with_a_snippet(self):
        self.fill()
        by_answer = self.h.list_chats("tree of properties")
        self.assertEqual([c["title"] for c in by_answer["chats"]], ["what is a ValueTree"])
        self.assertIn("\x01", by_answer["chats"][0]["match"])                         # the matched words are marked for the page
        self.assertEqual([c["title"] for c in self.h.list_chats("midi")["chats"]], ["how to play a MIDI note"])
        self.assertEqual([c["title"] for c in self.h.list_chats("valuetr")["chats"]], ["what is a ValueTree"])   # a half-typed word matches
        self.assertEqual(self.h.list_chats("zebra")["chats"], [])

    def test_a_renamed_chat_is_found_by_its_new_title(self):
        cid = self.ask()[0]["id"]
        self.assertTrue(self.h.rename_chat(cid, "  Audio callback notes  "))
        self.assertEqual(self.h.get_chat(cid)["title"], "Audio callback notes")
        self.assertEqual(len(self.h.list_chats("callback notes")["chats"]), 1)
        self.assertFalse(self.h.rename_chat(cid, "   "))
        self.assertFalse(self.h.rename_chat("nope", "x"))

    def test_deleting_a_chat_removes_it_from_search_too(self):
        self.fill()
        cid = self.h.list_chats("valuetree")["chats"][0]["id"]
        self.assertTrue(self.h.delete_chat(cid))
        self.assertIsNone(self.h.get_chat(cid))
        self.assertEqual(self.h.list_chats("valuetree")["chats"], [])
        self.assertEqual(self.h.list_chats()["total"], 2)
        self.assertFalse(self.h.delete_chat(cid))

    def test_clearing_removes_everything(self):
        self.fill()
        self.h.note_search("audio buffer")
        self.assertEqual(self.h.clear_all(), {"chats": 3, "searches": 1})
        self.assertEqual(self.h.stats()["chats"], 0)
        self.assertEqual(self.h.list_chats("midi")["chats"], [])
        self.assertEqual(self.h.suggest("how"), [])

    def test_quotes_and_percent_signs_in_a_search_do_not_break_it(self):
        self.fill()
        for q in ('"', "100%", "a_b", "'; DROP TABLE chats; --", "(((", "*"):
            self.h.list_chats(q)
            self.h.list_searches(q)
        self.assertEqual(self.h.list_chats()["total"], 3)

    # ---- searches
    def test_the_same_search_is_one_entry_that_counts_its_runs(self):
        first = self.h.note_search("  Audio   Buffer ", {"mode": "hybrid", "scope": "all"}, [{"id": "l", "name": "L"}], 12)
        again = self.h.note_search("audio buffer", {"scope": "all", "mode": "hybrid"}, [], 15)
        self.assertEqual(first, again)
        rows = self.h.list_searches()["searches"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["runs"], rows[0]["results"], rows[0]["query"]), (2, 15, "audio buffer"))
        self.assertEqual(rows[0]["filters"], {"mode": "hybrid", "scope": "all"})

    def test_different_filters_are_different_entries(self):
        self.h.note_search("audio buffer", {"scope": "code"})
        self.h.note_search("audio buffer", {"scope": "docs"})
        self.h.note_search("audio buffer", {})
        self.assertEqual(self.h.list_searches()["total"], 3)

    def test_too_short_searches_are_ignored_and_the_list_can_be_filtered(self):
        self.assertIsNone(self.h.note_search("a"))
        self.assertIsNone(self.h.note_search("   "))
        self.h.note_search("audio buffer")
        self.h.note_search("midi note")
        self.assertEqual([s["query"] for s in self.h.list_searches("MID")["searches"]], ["midi note"])
        sid = self.h.list_searches()["searches"][0]["id"]
        self.assertTrue(self.h.delete_search(sid))
        self.assertEqual(self.h.list_searches()["total"], 1)
        self.assertEqual(self.h.clear_searches(), 1)

    # ---- suggestions
    def test_suggestions_start_with_what_you_typed_then_match_words_then_anything_inside(self):
        self.h.note_search("audio buffer size")
        self.h.note_search("reduce audio latency")
        self.h.note_search("buffer overflow in audio callback")
        self.h.note_search("midi note")
        got = [s["text"] for s in self.h.suggest("audio")]
        self.assertEqual(got[0], "audio buffer size")                 # starts with it
        self.assertEqual(set(got), {"audio buffer size", "reduce audio latency", "buffer overflow in audio callback"})
        self.assertEqual([s["text"] for s in self.h.suggest("buf aud")][:1], ["buffer overflow in audio callback"])   # words in any order
        self.assertIn("reduce audio latency", [s["text"] for s in self.h.suggest("ce audio lat")])                 # inside the text
        self.assertEqual([s["text"] for s in self.h.suggest("zebra")], [])                                         # nothing alike

    def test_a_small_typo_still_finds_the_suggestion(self):
        self.h.note_search("synthesiser voices")
        self.assertEqual([s["text"] for s in self.h.suggest("sythesi")], ["synthesiser voices"])

    def test_questions_and_searches_both_suggest_and_the_preferred_kind_comes_first(self):
        self.ask("how do I read a file")
        self.h.note_search("how to read a file quickly")
        both = self.h.suggest("how", prefer="search")
        self.assertEqual([s["kind"] for s in both], ["search", "chat"])
        self.assertEqual([s["kind"] for s in self.h.suggest("how", prefer="chat")], ["chat", "search"])

    def test_with_nothing_typed_the_most_recent_things_come_back_and_the_limit_applies(self):
        for q in ("alpha one", "beta two", "gamma three", "delta four"):
            self.h.note_search(q)
            time.sleep(0.01)
        self.assertEqual([s["text"] for s in self.h.suggest("", limit=3)], ["delta four", "gamma three", "beta two"])

    def test_the_same_text_asked_and_searched_is_suggested_once(self):
        self.ask("audio buffer")
        self.h.note_search("Audio Buffer")
        self.assertEqual(len(self.h.suggest("audio")), 1)

    # ---- related
    def test_related_queries_share_meaningful_words(self):
        self.h.note_search("audio buffer size")
        self.h.note_search("buffer sizes in the audio callback")
        self.h.note_search("midi note on message")
        got = [r["text"] for r in self.h.related("audio buffers")]
        self.assertEqual(set(got), {"audio buffer size", "buffer sizes in the audio callback"})
        self.assertEqual(self.h.related("completely unrelated zebra"), [])

    def test_related_leaves_out_the_same_query_and_understands_identifiers(self):
        self.h.note_search("getNextAudioBlock")
        self.h.note_search("next audio block")
        self.assertEqual([r["text"] for r in self.h.related("next audio block")], ["getNextAudioBlock"])
        self.assertEqual(self.h.related("the and of"), [])                                            # only stop words

    def test_the_helpers(self):
        self.assertEqual(norm("  A   B\nC "), "a b c")
        self.assertEqual(fts_query("how do pro"), '"how" "do" "pro"*')
        self.assertIsNone(fts_query("   "))
        self.assertEqual(content_words("Searching the AudioBuffer"), {"search", "audio", "buffer", "audiobuffer"})
        self.assertEqual(history.one_line("a\n\n b" + "x" * 300, 10), "a bxxxxxx…")
        md = "## Short answer\n\nCall `prepareToPlay()` **once** [1][2].\n\n```cpp\nint x = 1;\n```\nThen go on."
        self.assertEqual(history.plain_preview(md), "Short answer Call prepareToPlay() once. Then go on.")
        self.assertEqual(history.plain_preview("```cpp\nunfinished"), "")


class Api(TempHome):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from perplexify.context import AppContext
        from perplexify.server import create_app

        cls.ctx = AppContext()
        cls.client = TestClient(create_app(cls.ctx), base_url="http://127.0.0.1")

    def setUp(self):
        self.ctx.history.set_enabled(True)
        self.ctx.history.clear_all()

    def chat(self, question, chat_id=None, **kw):
        """POST /api/chat with a fake model behind it; returns the decoded events."""
        body = {"question": question, "libraries": [], **({"chat_id": chat_id} if chat_id else {})}
        with mock.patch.object(self.ctx.engine, "answer_stream", lambda *a, **k: answer(question, **kw)):
            r = self.client.post("/api/chat", json=body)
        self.assertEqual(r.status_code, 200)
        return [json.loads(line[6:]) for line in r.text.split("\n\n") if line.startswith("data: ")]

    def test_a_question_is_saved_and_can_be_opened_again(self):
        events = self.chat("how do I implement processBlock")
        self.assertEqual(events[0]["type"], "chat")
        listing = self.client.get("/api/history/chats").json()
        self.assertEqual([c["title"] for c in listing["chats"]], ["how do I implement processBlock"])
        chat = self.client.get("/api/history/chats/" + events[0]["id"]).json()
        self.assertEqual(chat["turns_list"][0]["answer"], "Call it once [1].")
        self.assertEqual(self.client.get("/api/history/chats/000000000000").status_code, 404)

    def test_a_follow_up_with_the_chat_id_extends_the_chat(self):
        first = self.chat("how do I implement processBlock")[0]["id"]
        self.chat("and its parameters?", chat_id=first)
        self.assertEqual(self.client.get("/api/history/chats/" + first).json()["turns"], 2)

    def test_rename_delete_and_clear(self):
        cid = self.chat("how do I implement processBlock")[0]["id"]
        self.assertEqual(self.client.patch("/api/history/chats/" + cid, json={"title": "Mixing"}).status_code, 200)
        self.assertEqual(self.client.get("/api/history/chats", params={"q": "mixing"}).json()["chats"][0]["title"], "Mixing")
        self.assertEqual(self.client.patch("/api/history/chats/nope", json={"title": "x"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/history/chats/" + cid).json(), {"ok": True})
        self.chat("one more question")
        self.assertEqual(self.client.delete("/api/history/chats").json(), {"deleted": 1})

    def test_searches_are_noted_listed_and_used_for_suggestions_and_related(self):
        r = self.client.post("/api/history/searches", json={"query": "audio buffer size", "filters": {"mode": "hybrid"}, "results": 7})
        self.assertIsNotNone(r.json()["id"])
        self.client.post("/api/history/searches", json={"query": "buffer sizes for the callback"})
        listing = self.client.get("/api/history/searches").json()
        self.assertEqual(listing["total"], 2)
        self.assertEqual([s["text"] for s in self.client.get("/api/history/suggest", params={"q": "aud"}).json()["suggestions"]], ["audio buffer size"])
        related = self.client.get("/api/history/related", params={"q": "audio buffers"}).json()["related"]
        self.assertEqual(len(related), 2)
        self.assertEqual(self.client.delete(f"/api/history/searches/{listing['searches'][0]['id']}").json(), {"ok": True})
        self.assertEqual(self.client.delete("/api/history/searches").json(), {"deleted": 1})

    def test_switching_history_off_stops_recording_and_suggesting_but_keeps_what_exists(self):
        self.chat("how do I implement processBlock")
        self.assertEqual(self.client.put("/api/history/config", json={"enabled": False}).json()["enabled"], False)
        self.assertNotIn("chat", [e["type"] for e in self.chat("a second question")])
        self.assertEqual(self.client.get("/api/history/chats").json()["total"], 1)
        self.assertEqual(self.client.get("/api/history/suggest", params={"q": "how"}).json(), {"suggestions": []})
        self.assertEqual(self.client.get("/api/history/related", params={"q": "processBlock"}).json(), {"related": []})
        self.assertEqual(self.client.get("/api/history").json()["chats"], 1)

    def test_a_stopped_request_keeps_its_partial_answer(self):
        body = {"question": "explain mixing", "libraries": []}
        with mock.patch.object(self.ctx.engine, "answer_stream", lambda *a, **k: answer("x", text=("Part one, ", "part two"))):
            with self.client.stream("POST", "/api/chat", json=body) as r:
                for line in r.iter_lines():
                    if '"token"' in line:
                        break                 # leave early, as the page does when Stop is pressed
        time.sleep(0.3)
        chats = self.client.get("/api/history/chats").json()["chats"]
        self.assertEqual(len(chats), 1)


if __name__ == "__main__":
    unittest.main()
