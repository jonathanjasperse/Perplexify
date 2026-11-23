"""`python -m perplexify selftest`: proves an installation works end to end, with no network and no API key.

The installer builds run this against the bundled runtime before the installer is compiled.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HTML = """<html><head><title>Engine Reference</title></head><body><div id="doc-content"><div class="header">
<div class="headertitle"><div class="title">demo::Engine Class Reference</div></div></div><div class="contents">
<h2 class="groupheader">Detailed Description</h2><div class="textblock"><p>The Engine renders audio in blocks. Call
prepare() once before playback starts, then renderBlock() for every buffer of samples.</p></div>
<a id="a1" name="a1"></a><h2 class="memtitle"><span class="permalink"><a href="#a1">&#9670;&nbsp;</a></span>prepare()</h2>
<div class="memitem"><div class="memproto"><table class="memname"><tr><td class="memname">void demo::Engine::prepare </td><td>(</td>
<td class="paramtype">double&#160;</td><td class="paramname">sampleRate</td><td>)</td></tr></table></div>
<div class="memdoc"><p>Prepares the engine for playback at the given sample rate. Call this before renderBlock.</p></div></div>
</div></div></body></html>"""

CPP = """#include <vector>

namespace demo
{
/** Mixes the voices together and writes the result into the output buffer. */
class Mixer
{
public:
    /** Adds a voice with the given gain. */
    void addVoice (int id, float gain)
    {
        voices.push_back ({ id, gain });
    }

    /** Renders one block of audio by summing every voice. */
    void renderBlock (float* out, int numSamples)
    {
        for (int i = 0; i < numSamples; ++i)
        {
            float sum = 0.0f;
            for (auto& v : voices)
                sum += v.gain;
            out[i] = sum;
        }
    }

private:
    struct Voice { int id; float gain; };
    std::vector<Voice> voices;
};
}
"""

PY = '''"""Converts audio files."""

def convert_to_wav(path, sample_rate=44100):
    """Convert an input file to WAV at the given sample rate."""
    return path + ".wav"
'''


def window_support() -> str:
    """What the app's own window needs is installed and loads (no window is opened). A PC without the Microsoft Edge
    WebView2 Runtime is not a failure: the app then shows its page in the default browser instead."""
    from . import desktop

    if not (desktop.IS_WINDOWS or desktop.IS_MAC):
        return "not used on this platform: the app opens in the browser"
    from importlib.metadata import version

    import webview  # noqa: F401  (a missing package is a failure)

    detail = f"pywebview {version('pywebview')}"
    if desktop.IS_MAC:
        return detail + " with WKWebView"
    import clr  # noqa: F401  (pythonnet: how pywebview talks to WebView2)

    runtime = desktop.webview2_runtime_version()
    if runtime is None:
        return detail + ", pythonnet; no WebView2 Runtime on this PC, so the app opens in the browser"
    from webview.guilib import initialize      # loads the .NET window classes and the WebView2 control, without a window

    gui = initialize("edgechromium")
    if getattr(gui, "renderer", "") != "edgechromium":
        raise AssertionError(f"the window would use the {getattr(gui, 'renderer', '?')} engine instead of Edge WebView2")
    return detail + f", pythonnet, WebView2 Runtime {runtime}"


def run(skip_embed: bool = False) -> int:
    home = Path(tempfile.mkdtemp(prefix="perplexify_selftest_"))
    os.environ["PERPLEXIFY_HOME"] = str(home)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> object:
        t = time.time()
        try:
            detail = fn()
            results.append((name, True, f"{detail}  ({time.time() - t:.1f}s)" if detail else f"({time.time() - t:.1f}s)"))
            return detail
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"))
            return None

    from . import __version__

    state: dict = {}

    def python_info():
        return f"Perplexify {__version__}, Python {sys.version.split()[0]} on {sys.platform}"

    def sqlite_fts5():
        import sqlite3

        con = sqlite3.connect(":memory:")
        con.execute("create virtual table t using fts5(a)")
        return f"SQLite {sqlite3.sqlite_version} with FTS5"

    def grammars():
        import importlib

        import tree_sitter

        from .loaders.code import GRAMMARS

        for lang, (module, fn) in GRAMMARS.items():
            tree_sitter.Parser(tree_sitter.Language(getattr(importlib.import_module(module), fn)()))
        return f"{len(GRAMMARS)} language grammars"

    def build_library():
        from . import ingest
        from .context import AppContext
        from .jobs import Job
        from .repo import RepoOptions

        ctx = AppContext()
        state["ctx"] = ctx
        docs = home / "src_docs"
        repo = home / "src_repo"
        (docs).mkdir()
        (repo / "src").mkdir(parents=True)
        (docs / "engine.html").write_text(HTML, "utf-8")
        (docs / "guide.md").write_text("# Guide\n\n## Getting started\nInstall the plugin, then press the play button.\n", "utf-8")
        (repo / "src" / "mixer.cpp").write_text(CPP, "utf-8")
        (repo / "tools.py").write_text(PY, "utf-8")
        (repo / ".env").write_text("API_KEY=sk-ant-abcdefghijklmnopqrstuvwxyz\n", "utf-8")
        lib = ctx.libs.create("Selftest", "", ctx.settings.embedder())
        state["lib"] = lib
        job = Job("selftest", "selftest", lib.id)
        s1 = ingest.index_folder(lib, docs, job)
        s2, scan = ingest.index_repo(lib, repo, job, RepoOptions(), uri_for=lambda r: f"repo://t/{r}", origin="external",
                                     stored_for=lambda r: str(repo / r), uri_prefix="repo://t/")
        if scan.secret_files != [".env"]:
            raise AssertionError(f"the .env file should have been skipped, got {scan.secret_files}")
        st = lib.store.stats()
        if st["kinds"].get("code") != 2 or st["kinds"].get("html") != 1:
            raise AssertionError(f"unexpected documents: {st['kinds']}")
        return f"{st['documents']} files, {st['chunks']} passages ({s1.summary()}; {s2.summary()})"

    def keyword_search():
        from .textutil import build_exact_query, build_fts_query

        store = state["lib"].store
        ids = store.bm25(build_exact_query("renderBlock") or "", 5) + store.bm25(build_fts_query("renderBlock") or "", 5)
        rows = store.get_chunks([i for i, _ in ids])
        hit = next((r for r in rows.values() if "renderBlock" in r["text"] and "mixer.cpp" in r["heading"]), None)
        if hit is None:
            raise AssertionError(f"keyword search missed the function: {[r['heading'] for r in rows.values()]}")
        if "renderBlock" not in hit["heading"]:
            raise AssertionError(f"member names should appear in the heading: {hit['heading']}")
        return f"found renderBlock at {hit['anchor']} under '{hit['heading']}'"

    def secrets():
        from .secretscan import redact

        text, n = redact('token = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\npassword = "hunter2hunter2hunter2"')
        if n != 2 or "ghp_" in text or "hunter2" in text:
            raise AssertionError("secrets were not redacted")
        return "credentials are redacted"

    def embed_and_search():
        from . import ingest
        from .jobs import Job

        ctx, lib = state["ctx"], state["lib"]
        n = ingest.embed_pending(lib, ctx.settings, Job("selftest", "embed", lib.id))
        hits, dbg = ctx.retriever.search(["how do I prepare the engine before playback"], [lib.id], top_k=3)
        heads = [h.heading for h in hits]
        if not any("prepare" in h for h in heads):
            raise AssertionError(f"hybrid search did not find prepare(): {heads}")
        hits, _ = ctx.retriever.search(["sum all voices into the output buffer"], [lib.id], top_k=3)
        if not any("renderBlock" in h.text for h in hits):
            raise AssertionError("semantic search did not find the mixer")
        return f"embedded {n} passages; hybrid search top hit: {heads[0].split(' > ')[-1]}"

    def web_app():
        from fastapi.testclient import TestClient

        from .server import create_app

        client = TestClient(create_app(state["ctx"]), base_url="http://127.0.0.1")
        h = client.get("/api/health").json()
        b = client.get("/api/bootstrap").json()
        page = client.get("/")
        if h.get("app") != "perplexify" or not b["providers"] or page.status_code != 200:
            raise AssertionError("web app did not respond correctly")
        evil = client.get("/api/health", headers={"host": "evil.example.com"})
        if evil.status_code != 403:
            raise AssertionError("foreign Host headers must be refused")
        return f"{len(b['libraries'])} library, UI page {len(page.content) // 1024} KB, host guard works"

    def local_engine():
        """The llama.cpp engine behind local models starts and reports its version. Running from the source without it is not a
        failure (it is fetched with `download-llama`); an installation without it is, because the installer ships it."""
        from . import localllm

        eng = localllm.locate_engine()
        if eng is None:
            if localllm.installed_layout():
                raise AssertionError(f"the llama.cpp engine is missing from the installation (expected {localllm.engine_search_dirs()[-2][1]})")
            return "not installed (running from the source: local models need `python -m perplexify download-llama`)"
        version = localllm.engine_version(eng)
        if not version:
            raise AssertionError(f"{eng.exe} does not run (llama-server --version failed)")
        gpu = ", ".join(localllm.gpu_backends(eng.dir)) or "CPU only"
        return f"llama.cpp {version} from {eng.dir} ({eng.source}; {gpu})"

    check("runtime", python_info)
    check("sqlite full-text search", sqlite_fts5)
    check("code grammars", grammars)
    check("secret redaction", secrets)
    check("import documents + code repository", build_library)
    check("keyword search", keyword_search)
    if not skip_embed:
        check("built-in embeddings + hybrid search", embed_and_search)
    check("web app", web_app)
    check("local engine", local_engine)
    check("app window", window_support)

    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    for _n, _ok, _d in results:
        pass
    ctx = state.get("ctx")
    if ctx is not None and state.get("lib") is not None:
        state["lib"].close()
    shutil.rmtree(home, ignore_errors=True)
    failed = [n for n, ok, _ in results if not ok]
    print("\nSelf-test passed." if not failed else f"\nSelf-test FAILED: {', '.join(failed)}")
    return 0 if not failed else 1
