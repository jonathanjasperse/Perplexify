"""Which system prompt a question gets: Claude and ChatGPT may reason beyond the excerpts, the other models stay strict."""
import unittest

from perplexify import rag
from perplexify.retrieval import Hit


def hit(kind="doc"):
    return Hit("lib", "Demo", 1, 1, "Title", "u", "Heading", "", "Some text about the mixer.", "external", "p", kind)


def system(hits, **kw):
    return rag.RagEngine._prompt("q", [], hits, 100000, 20000, False, **kw)["system"]


class Prompts(unittest.TestCase):
    def test_claude_and_chatgpt_are_told_to_combine_the_source_with_their_own_knowledge(self):
        self.assertEqual(rag.EXPERT_PROVIDERS, ("anthropic", "openai"))
        doc = system([hit()], expert=True)
        self.assertIn("data scientists and biomedical engineers", doc)
        self.assertIn("general scientific and engineering knowledge", doc)
        self.assertIn("Mark that content plainly", doc)                       # their own reasoning is kept apart from the source
        self.assertIn("Never invent a citation", doc)
        self.assertNotIn("Base your answer only on the excerpts", doc)

    def test_the_other_models_keep_the_strict_prompt(self):
        strict = system([hit()])
        self.assertIn("Base your answer only on the excerpts", strict)
        self.assertNotIn("biomedical", strict)

    def test_code_gets_its_own_expert_prompt_with_file_and_line_references(self):
        code = system([hit("code"), hit("code")], expert=True)
        self.assertIn("engineering partner", code)
        self.assertIn("src/Foo.cpp:120-148", code)
        self.assertIn("go beyond the excerpts", code)

    def test_research_answers_get_the_expert_researcher_prompt_for_claude_chatgpt_and_gemini(self):
        self.assertEqual(rag.EXPERT_RESEARCH_PROVIDERS, ("anthropic", "openai", "gemini"))
        web = system([hit("web")], research=True, expert_research=True)
        self.assertIn("senior research partner for data scientists and biomedical engineers", web)
        self.assertIn("A paper abstract is a summary of the paper, not its full text", web)
        self.assertIn("Synthesise, do not just list", web)
        self.assertIn("Judge the evidence like a reviewer", web)
        self.assertIn("patient-wise versus record-wise", web)
        self.assertIn("From general knowledge:", web)                       # their own reasoning is marked and kept apart from the sources
        self.assertIn("Never invent a citation", web)
        self.assertNotIn("careful research assistant", web)
        self.assertNotIn("{", web)                                          # every placeholder was filled in
        self.assertIn("Today is ", web)

    def test_the_other_models_keep_the_strict_research_prompt(self):
        web = system([hit("web")], research=True, expert=True)             # the flag for library questions does not change Research
        self.assertIn("careful research assistant", web)
        self.assertNotIn("senior research partner", web)
        self.assertNotIn("{", web)

    def test_the_users_own_libraries_are_named_in_the_expert_research_prompt(self):
        mixed = system([hit("web"), hit("doc")], research=True, expert_research=True)
        self.assertIn("and passages from the user's own libraries (Demo)", mixed)

    def test_research_with_nothing_from_the_web_uses_the_library_prompts(self):
        self.assertIn("senior research partner for data scientists and biomedical engineers doing deep research", system([hit("doc")], research=True, expert=True, expert_research=True))


if __name__ == "__main__":
    unittest.main()
