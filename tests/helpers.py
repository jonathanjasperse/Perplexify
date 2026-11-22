import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TempHome(unittest.TestCase):
    """Each test class gets its own empty data folder."""

    @classmethod
    def setUpClass(cls):
        cls._home = tempfile.mkdtemp(prefix="perplexify_test_")
        cls._old = os.environ.get("PERPLEXIFY_HOME")
        os.environ["PERPLEXIFY_HOME"] = cls._home
        os.environ["PERPLEXIFY_NO_WARMUP"] = "1"  # tests must not load models in the background
        for var in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY",
                    "VOYAGE_API_KEY"):
            os.environ.pop(var, None)

    @classmethod
    def tearDownClass(cls):
        if cls._old is None:
            os.environ.pop("PERPLEXIFY_HOME", None)
        else:
            os.environ["PERPLEXIFY_HOME"] = cls._old
        shutil.rmtree(cls._home, ignore_errors=True)


def sse(*events: dict, done: bool = True) -> bytes:
    """OpenAI-style server-sent events."""
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()
