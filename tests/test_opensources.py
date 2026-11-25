"""The open sources behind Research: what each one's answer is turned into, what is kept, and how they are treated.

The answers below have the shapes the real services returned when they were tried (Wikipedia, PubMed, arXiv, OpenAlex, Stack
Overflow, GitHub, DuckDuckGo). No test touches the network: a fake transport answers by address."""
import json
import unittest
from unittest import mock

import httpx

from helpers import TempHome  # noqa: F401  (puts the repo on sys.path like the other tests)

from perplexify import opensources as o

WIKI = {"pages": [
    {"id": 1, "key": "PowerShell", "title": "PowerShell",
     "excerpt": 'a <span class="searchmatch">pipeline</span>, <span class="searchmatch">PowerShell</span> a', "description": "Cross-platform shell based on .NET technology"},
    {"id": 2, "key": "Pipeline_(software)", "title": "Pipeline (software)", "excerpt": "instead of text.", "description": "Chain of software processing elements"}]}

PUBMED_IDS = {"esearchresult": {"count": "2", "idlist": ["111", "222"]}}
# a real PubMed answer starts with a DOCTYPE; this one also tries to read a file through an entity
PUBMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2024//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_240101.dtd" [<!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">]>
<PubmedArticleSet>
<PubmedArticle><MedlineCitation><PMID Version="1">111</PMID><Article>
<Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue><Title>Computers in biology and medicine</Title></Journal>
<ArticleTitle>ECG signal classification with <i>binarized</i> convolutional neural network.</ArticleTitle>
<Abstract><AbstractText Label="BACKGROUND">Arrhythmia detection matters. &xxe;</AbstractText><AbstractText Label="METHODS">We trained a CNN on MIT-BIH.</AbstractText></Abstract>
<AuthorList><Author><LastName>Smith</LastName><Initials>J</Initials></Author><Author><LastName>Jones</LastName><Initials>K</Initials></Author>
<Author><LastName>Lee</LastName><Initials>M</Initials></Author><Author><LastName>Kim</LastName><Initials>S</Initials></Author></AuthorList>
</Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="pubmed">111</ArticleId><ArticleId IdType="doi">10.1016/j.compbiomed.2020.103800</ArticleId></ArticleIdList></PubmedData></PubmedArticle>
<PubmedArticle><MedlineCitation><PMID Version="1">222</PMID><Article><Journal><Title>No abstract journal</Title></Journal><ArticleTitle>A record without an abstract.</ArticleTitle></Article></MedlineCitation></PubmedArticle>
</PubmedArticleSet>"""

ARXIV = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/2304.06427v2</id><published>2023-04-13T10:00:00Z</published>
<title>In-Distribution ECG
  Representation Learning</title><summary>  Self-supervised ECG arrhythmia models are studied.  </summary>
<author><name>A. Author</name></author><arxiv:primary_category term="eess.SP"/></entry></feed>"""
ARXIV_EMPTY = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'

OPENALEX = {"results": [
    {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.48550/arxiv.2304.06427", "title": "In-Distribution ECG Representation Learning",
     "publication_year": 2023, "abstract_inverted_index": {"models": [3], "Self-supervised": [0], "ECG": [1], "arrhythmia": [2]},
     "primary_location": {"source": {"display_name": "arXiv"}}, "authorships": [{"author": {"display_name": "A. Author"}}], "cited_by_count": 3},
    {"id": "https://openalex.org/W2", "doi": None, "title": "No abstract", "abstract_inverted_index": None}]}

SO_SEARCH = {"quota_remaining": 290, "items": [
    {"question_id": 10, "title": "PowerShell: Pipeline objects into pipeline objects", "link": "https://stackoverflow.com/questions/10/x", "answer_count": 2,
     "body": "<p>How do I pass objects <code>down</code> the pipeline?</p>"},
    {"question_id": 11, "title": "No answer here", "link": "https://stackoverflow.com/questions/11/y", "answer_count": 0, "body": "<p>unanswered</p>"}]}
SO_ANSWERS = {"quota_remaining": 289, "items": [
    {"question_id": 10, "score": 12, "is_accepted": False, "body": "<p>Use a function.</p>"},
    {"question_id": 10, "score": 3, "is_accepted": True, "body": "<p>Pipe them:</p><pre><code>Get-Process | Sort-Object CPU\n</code></pre>"}]}

GITHUB = {"items": [{"full_name": "physhik/ecg-mit-bih", "html_url": "https://github.com/physhik/ecg-mit-bih", "description": "ECG classification using MIT-BIH data",
                     "stargazers_count": 270, "language": "Python", "topics": ["cardiology", "cnn"]}]}

DDG = """<html><body><div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa&amp;rut=x">Example A</a>
<a class="result__snippet">Snippet A</a></div><div class="result result--ad"><a class="result__a" href="https://ads.example/">An ad</a></div></body></html>"""


def respond(table):
    """A client whose answers come from `table`: {part of the address: body, or a function of the request, or an httpx.Response}."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        for part, body in table.items():
            if part in str(request.url):
                if callable(body):
                    body = body(request)
                if isinstance(body, httpx.Response):
                    return body
                if isinstance(body, (dict, list)):
                    return httpx.Response(200, json=body)
                return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="nothing here")
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5)
    client.calls = calls
    return client


class Base(unittest.TestCase):
    def setUp(self):
        o.reset()
        sleeps = []
        patches = [mock.patch.object(o, "_sleep", sleeps.append)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.sleeps = sleeps


class QueryTerms(Base):
    def test_filler_is_dropped_and_specific_words_are_kept_as_must_haves(self):
        q = o.Query.parse("how does PowerShell work objects pipeline")
        self.assertEqual(q.terms, ("powershell", "objects", "pipeline"))
        self.assertEqual(q.must, ("powershell",))                         # the first word, which is also a CamelCase name
        q = o.Query.parse("rust 1.80 release date")
        self.assertIn("1.80", q.terms)                                    # a version number is one word, not "1" and "80"
        self.assertEqual(q.must, ("rust", "1.80"))
        q = o.Query.parse("convolutional neural network ECG arrhythmia classification")
        self.assertEqual(q.must, ("convolutional", "ecg"))

    def test_services_that_need_every_word_get_few_and_specific_ones(self):
        q = o.Query.parse("convolutional neural network ECG arrhythmia classification")
        self.assertEqual(q.lead(4), ["convolutional", "ecg", "arrhythmia", "classification"])   # "ECG" survives, "network" does not
        self.assertEqual(o.Query.parse("").lead(3), [])

    def test_a_short_word_must_be_a_word_and_not_part_of_another(self):
        q = o.Query.parse("AI safety")
        self.assertEqual(o.coverage(q, o.WebResult("The main idea", "u", "a second look at safety"))[1], 1)       # "ai" is not in "main"
        self.assertEqual(o.coverage(q, o.WebResult("AI and safety", "u"))[1], 2)


class Sources(Base):
    q = staticmethod(o.Query.parse)

    def test_wikipedia(self):
        c = respond({"wikipedia.org/w/rest.php": WIKI})
        r = o._wikipedia(c, self.q("PowerShell pipeline"), 5)
        self.assertEqual([w.title for w in r], ["PowerShell", "Pipeline (software)"])
        self.assertEqual(r[1].url, "https://en.wikipedia.org/wiki/Pipeline_(software)")
        self.assertIn("Cross-platform shell", r[0].snippet)
        self.assertNotIn("<span", r[0].snippet)                          # the search engine's highlighting is removed
        self.assertTrue(all(w.source == "Wikipedia" and not w.text for w in r))                # its pages are read like web pages

    def test_pubmed_gives_abstracts_and_never_reads_files_named_in_the_answer(self):
        c = respond({"esearch.fcgi": PUBMED_IDS, "efetch.fcgi": PUBMED_XML})
        r = o._pubmed(c, self.q("ECG arrhythmia convolutional neural network"), 5)
        self.assertEqual(len(r), 1, "the record without an abstract is left out")
        w = r[0]
        self.assertEqual((w.source, w.url), ("PubMed", "https://pubmed.ncbi.nlm.nih.gov/111/"))
        self.assertIn("ECG signal classification with binarized convolutional neural network.", w.title)
        self.assertIn("Background: Arrhythmia detection matters.", w.text)
        self.assertIn("Methods: We trained a CNN on MIT-BIH.", w.text)
        self.assertIn("Smith J, Jones K, Lee M et al. - Computers in biology and medicine (2020) - PMID 111", w.text)
        self.assertEqual(w.ident, "10.1016/j.compbiomed.2020.103800")
        self.assertNotIn("[fonts]", w.text.lower())                         # the entity pointing at a local file was not followed
        self.assertTrue(any("sort=relevance" in u for u in c.calls) and any("tool=Perplexify" in u for u in c.calls))

    def test_arxiv_reads_the_feed_and_widens_a_search_that_finds_nothing(self):
        answers = iter([ARXIV_EMPTY, ARXIV])
        c = respond({"export.arxiv.org": lambda req: next(answers)})
        r = o._arxiv(c, self.q("ECG arrhythmia detection deep learning MIT-BIH"), 5)
        self.assertEqual(len(c.calls), 2, "the second try uses fewer words")
        self.assertLess(len(c.calls[1].split("search_query=")[1].split("&")[0]), len(c.calls[0].split("search_query=")[1].split("&")[0]))
        w = r[0]
        self.assertEqual((w.url, w.ident, w.source), ("https://arxiv.org/abs/2304.06427", "arxiv:2304.06427", "arXiv"))   # no version, https
        self.assertEqual(w.title, "In-Distribution ECG Representation Learning")             # line breaks inside the title are gone
        self.assertIn("A. Author - arXiv 2304.06427 [eess.SP] - 2023-04-13", w.text)
        self.assertIn("Self-supervised ECG arrhythmia models are studied.", w.text)

    def test_openalex_puts_the_abstract_back_in_order_and_skips_records_without_one(self):
        c = respond({"api.openalex.org": OPENALEX})
        r = o._openalex(c, self.q("ECG arrhythmia self-supervised"), 5)
        self.assertEqual(len(r), 1)
        self.assertIn("Self-supervised ECG arrhythmia models", r[0].text)
        self.assertEqual((r[0].url, r[0].ident), ("https://doi.org/10.48550/arxiv.2304.06427", "arxiv:2304.06427"))
        self.assertIn("cited by 3", r[0].text)
        self.assertIn("has_abstract%3Atrue", c.calls[0])

    def test_stack_overflow_pairs_a_question_with_its_accepted_answer(self):
        c = respond({"/search/advanced": SO_SEARCH, "/answers": SO_ANSWERS})
        r = o._stackoverflow(c, self.q("PowerShell pipeline objects"), 5)
        self.assertEqual(len(r), 1, "a question nobody answered is not worth a place")
        w = r[0]
        self.assertEqual((w.source, w.url), ("Stack Overflow", "https://stackoverflow.com/questions/10/x"))
        self.assertIn("Answer (score 3, accepted):", w.text)                          # the accepted answer beats the more voted one
        self.assertIn("```\nGet-Process | Sort-Object CPU\n```", w.text)               # code stays code
        self.assertIn("/questions/10/answers", c.calls[1])                              # only the questions that have answers are asked about

    def test_stack_overflow_asks_to_wait_and_is_left_alone(self):
        c = respond({"/search/advanced": {"items": [], "backoff": 30, "quota_remaining": 100}})
        o._stackoverflow(c, self.q("PowerShell pipeline objects"), 5)
        self.assertIn("wait", o._why_blocked("stackoverflow") or "")

    def test_github(self):
        c = respond({"api.github.com": GITHUB})
        r = o._github(c, self.q("ECG MIT-BIH classification"), 5)
        self.assertEqual((r[0].title, r[0].url, r[0].source), ("physhik/ecg-mit-bih", "https://github.com/physhik/ecg-mit-bih", "GitHub"))
        self.assertIn("270 stars", r[0].snippet)

    def test_github_running_out_of_searches_is_reported_and_waited_out(self):
        c = respond({"api.github.com": httpx.Response(403, json={"message": "rate limit"}, headers={"x-ratelimit-remaining": "0"})})
        with self.assertRaises(o.SourceError) as e:
            o._github(c, self.q("ECG MIT-BIH classification"), 5)
        self.assertIn("limit for searches", str(e.exception))
        self.assertGreaterEqual(e.exception.block, 30)

    def test_duckduckgo_results_and_its_refusal(self):
        r = o._duckduckgo(respond({"duckduckgo.com/html": DDG}), self.q("example"), 5)
        self.assertEqual([(w.title, w.url, w.snippet) for w in r], [("Example A", "https://example.org/a", "Snippet A")])   # the ad is skipped
        with self.assertRaises(o.SourceError) as e:
            o._duckduckgo(respond({"duckduckgo.com/html": httpx.Response(202, text="<html>anomaly</html>")}), self.q("example"), 5)
        self.assertIn("refusing automated searches", str(e.exception))
        self.assertEqual(e.exception.block, 900)


class Merging(Base):
    def test_off_topic_results_of_a_filling_source_are_dropped_but_a_search_engine_is_trusted(self):
        q = o.Query.parse("rust 1.80 release date")
        wheat = o.WebResult("Registration of Five Spring Wheat Lines", "https://doi.org/x", text="Wheat release date " * 10, source="OpenAlex")
        rustpage = o.WebResult("Rust (programming language)", "https://en.wikipedia.org/wiki/Rust", "Rust is a language. Release history.", "Wikipedia")
        kept = o.merge(q, {"openalex": [wheat], "wikipedia": [rustpage]}, 6)
        self.assertEqual([w.source for w in kept], ["Wikipedia"])

    def test_the_same_paper_from_two_sources_appears_once_and_no_source_takes_over(self):
        q = o.Query.parse("ECG arrhythmia self-supervised models")
        a = o.WebResult("In-Distribution ECG", "https://arxiv.org/abs/2304.06427", text="Self-supervised ECG arrhythmia models.", source="arXiv", ident="arxiv:2304.06427")
        b = o.WebResult("In-Distribution ECG", "https://doi.org/10.48550/arxiv.2304.06427", text="Self-supervised ECG arrhythmia models.", source="OpenAlex", ident="arxiv:2304.06427")
        many = [o.WebResult(f"ECG arrhythmia self-supervised models {i}", f"https://doi.org/{i}", text="ECG arrhythmia self-supervised models", source="PubMed", ident=f"d{i}") for i in range(8)]
        kept = o.merge(q, {"arxiv": [a], "openalex": [b], "pubmed": many}, 10)
        self.assertEqual(sum(1 for w in kept if w.ident == "arxiv:2304.06427"), 1)
        self.assertEqual(sum(1 for w in kept if w.source == "PubMed"), 3, "PubMed's share per search is capped")


def everything():
    return {
        "en.wikipedia.org": WIKI, "esearch.fcgi": PUBMED_IDS, "efetch.fcgi": PUBMED_XML, "export.arxiv.org": ARXIV, "api.openalex.org": OPENALEX,
        "/search/advanced": SO_SEARCH, "/answers": SO_ANSWERS, "api.github.com": GITHUB, "duckduckgo.com/html": DDG}


class Searching(Base):
    def test_all_sources_are_asked_at_once_and_each_search_keeps_what_is_about_it(self):
        c = respond(everything())
        r = o.search_many(["ECG arrhythmia self-supervised models", "PowerShell pipeline objects"], client=c)
        ecg, shell = r.per_query
        self.assertEqual(len({u.split("/")[2] for u in c.calls}), 7, "every service was asked")
        self.assertEqual(r.errors, {})
        # the paper found by both arXiv and OpenAlex is there once; the PowerShell answers are not offered for an ECG search
        self.assertEqual(len([w for w in ecg if w.ident == "arxiv:2304.06427"]), 1)
        self.assertNotIn("Stack Overflow", [w.source for w in ecg])
        self.assertIn("PubMed", [w.source for w in ecg])
        self.assertEqual({w.source for w in shell} & {"Wikipedia", "Stack Overflow"}, {"Wikipedia", "Stack Overflow"})
        self.assertNotIn("PubMed", [w.source for w in shell])
        self.assertEqual(r.label, ", ".join(r.used))
        self.assertEqual(sum(r.counts.values()), len(ecg) + len(shell))

    def test_one_source_failing_never_stops_the_others(self):
        table = {**everything(), "en.wikipedia.org": httpx.Response(500, text="oops"), "esearch.fcgi": httpx.Response(429, text="slow down")}
        r = o.search_many(["PowerShell pipeline objects"], client=respond(table))
        self.assertIn("Wikipedia answered 500", {f"{k} {v}" for k, v in r.errors.items()})
        self.assertIn("PubMed is busy right now", {f"{k} {v}" for k, v in r.errors.items()})
        self.assertTrue(r.per_query[0], "the other sources still answered")
        self.assertFalse(r.offline)

    def test_nothing_reachable_is_said_plainly(self):
        def down(request):
            raise httpx.ConnectError("no network", request=request)
        c = httpx.Client(transport=httpx.MockTransport(down))
        r = o.search_many(["PowerShell pipeline"], client=c)
        self.assertEqual(r.per_query, [[]])
        self.assertTrue(r.offline)
        self.assertIn("could not reach the internet", r.explain())

    def test_a_search_with_every_source_switched_off_says_so(self):
        r = o.search_many(["PowerShell"], enabled=set(), client=respond(everything()))
        self.assertIn("switched off", r.explain())

    def test_only_the_chosen_sources_are_asked(self):
        c = respond(everything())
        r = o.search_many(["PowerShell pipeline objects"], enabled={"wikipedia", "github"}, client=c)
        self.assertEqual({u.split("/")[2] for u in c.calls}, {"en.wikipedia.org", "api.github.com"})
        self.assertEqual(r.asked, ["Wikipedia", "GitHub"])

    def test_arxiv_is_asked_once_however_many_searches_there_are(self):
        c = respond(everything())
        o.search_many(["ECG arrhythmia models", "ECG arrhythmia deep learning", "ECG beat classification"], enabled={"arxiv", "wikipedia"}, client=c)
        self.assertEqual(sum("arxiv.org" in u for u in c.calls), 1, "it asks for no more than one request every three seconds")
        self.assertEqual(sum("wikipedia.org" in u for u in c.calls), 3)

    def test_the_same_question_is_not_asked_twice(self):
        c = respond(everything())
        o.search_many(["PowerShell pipeline objects"], client=c)
        first = len(c.calls)
        o.search_many(["PowerShell pipeline objects"], client=c)
        self.assertEqual(len(c.calls), first)

    def test_a_refusal_is_not_repeated_at_once(self):
        table = {**everything(), "duckduckgo.com/html": httpx.Response(202, text="anomaly")}
        c = respond(table)
        first = o.search_many(["PowerShell pipeline objects"], client=c)
        self.assertIn("DuckDuckGo", first.errors)
        asked = sum("duckduckgo.com" in u for u in c.calls)
        again = o.search_many(["PowerShell pipeline internals"], client=c)       # a different question: the cache does not answer it
        self.assertEqual(sum("duckduckgo.com" in u for u in c.calls), asked, "DuckDuckGo is left alone for a while after refusing")
        self.assertIn("refusing automated searches", again.errors["DuckDuckGo"])

    def test_requests_to_one_service_keep_the_pace_it_asks_for(self):
        waits = []
        clock = [100.0]
        with mock.patch.object(o, "_clock", lambda: clock[0]), mock.patch.object(o, "_sleep", lambda s: waits.append(round(s, 2))):
            o._pace("arxiv", 3.1)
            o._pace("arxiv", 3.1)                                              # right after: has to wait out the interval
            clock[0] += 10
            o._pace("arxiv", 3.1)                                              # long after: no wait
            o._pace("github", 1.0)                                             # another service has its own pace
        self.assertEqual(waits, [3.1])

    def test_a_service_that_is_too_slow_does_not_hold_the_search_up(self):
        import threading

        gate = threading.Event()

        def slow(request):
            gate.wait(5)
            return httpx.Response(200, json=WIKI)
        table = {**everything(), "en.wikipedia.org": slow}
        with mock.patch.object(o, "DEADLINE", 0.5):
            r = o.search_many(["PowerShell pipeline objects"], client=respond(table))
        gate.set()
        self.assertIn("took too long", r.errors.get("Wikipedia", ""))
        self.assertTrue(r.per_query[0])

    def test_the_cache_hands_out_copies(self):
        c = respond(everything())
        a = o.search_many(["PowerShell pipeline objects"], enabled={"wikipedia"}, client=c).per_query[0][0]
        a.title = "changed by a caller"
        b = o.search_many(["PowerShell pipeline objects"], enabled={"wikipedia"}, client=c).per_query[0][0]
        self.assertNotEqual(b.title, "changed by a caller")


if __name__ == "__main__":
    unittest.main()
