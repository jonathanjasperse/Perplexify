"""Command line: `python -m perplexify` (no arguments) starts the desktop app. Everything the UI does is also here."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import __version__


def _ctx():
    from .context import AppContext

    return AppContext()


def _wait(job, quiet: bool = False) -> bool:
    """Print a job's progress until it finishes. Returns True on success."""
    last = ""
    while job.status in ("queued", "running"):
        d = job.to_dict()
        line = f"{d['phase'] or 'working'} {d['current']}/{d['total'] or '?'}  {d['message']}"[:110]
        if line != last and not quiet:
            print(line, flush=True)
            last = line
        time.sleep(1.0)
    d = job.to_dict()
    for ln in d["log"]:
        print("  " + ln)
    if d["status"] == "done":
        print(d["message"] or "Done.")
        return True
    print(f"{d['status'].capitalize()}: {d['error'] or d['message']}", file=sys.stderr)
    return False


def cmd_connect(a) -> int:
    from . import providers
    from .llm import LLMError, make_provider
    from .settings import sanitize_profile

    ctx = _ctx()
    prov = providers.get_chat_provider(a.provider)
    if prov is None:
        print("Providers: " + ", ".join(p["id"] if p["id"] == "custom" else p["name"].lower() for p in providers.CHAT_PROVIDERS),
              file=sys.stderr)
        return 2
    key = a.key or ""
    use_env = False
    if not key:
        env = providers.env_key(prov["id"])
        if not env:
            print(f"Pass --key, or set {prov['env'][0] if prov['env'] else 'the key'} in the environment.", file=sys.stderr)
            return 2
        key, use_env = env[1], True
    base = a.base_url or prov["base_url"]
    temp = sanitize_profile({"id": "tmp", "provider": prov["id"], "base_url": base, "model": a.model or ""})
    try:
        models = make_provider(temp, key).list_models()
    except LLMError as e:
        print(f"Could not connect: {e}", file=sys.stderr)
        return 1
    ids = [m["id"] for m in models]
    model = a.model if a.model in ids else providers.pick_default_model(prov["id"], ids)
    ctx.settings.connect(prov["id"], None if use_env else key, model, base, "", use_env)
    print(f"Connected to {prov['name']} using {model}. {len(ids)} models available.")
    return 0


def cmd_add_docs(a) -> int:
    from . import tasks

    ctx = _ctx()
    lib = ctx.libs.create(a.name, "", ctx.settings.embedder())
    if a.url:
        job = ctx.jobs.submit("crawl", a.name, lib.id, lambda j: tasks.crawl_task(ctx, lib, {"start_url": a.url}, j))
    else:
        job = ctx.jobs.submit("folder", a.name, lib.id, lambda j: tasks.folder_task(ctx, lib, {"path": a.path}, j))
    return 0 if _wait(job) else 1


def cmd_add_repo(a) -> int:
    from . import github, tasks

    ctx = _ctx()
    lib = ctx.libs.create(a.name, "", ctx.settings.embedder())
    source = a.source
    if Path(source).is_dir():
        params = {"kind": "folder", "path": source}
    else:
        params = {"kind": "github" if github.parse_github(source, a.ref) else "git", "url": source, "ref": a.ref or "",
                  "token": a.token or ""}
    params.update(include=a.include or [], exclude=a.exclude or [])
    job = ctx.jobs.submit("repo", a.name, lib.id, lambda j: tasks.repo_task(ctx, lib, params, j))
    return 0 if _wait(job) else 1


def cmd_list(a) -> int:
    ctx = _ctx()
    for lib in ctx.libs.list():
        s = lib.store.stats()
        kinds = ", ".join(f"{k} {v}" for k, v in s["kinds"].items())
        print(f"{lib.id:24s} {lib.name:28s} {s['documents']:6d} docs  {s['chunks']:7d} passages  "
              f"{s['embedded']}/{s['chunks']} embedded  [{kinds}]")
    if not ctx.libs.list():
        print("No libraries yet. Add one with `add-docs` or `add-repo`, or open the app.")
    return 0


def _resolve_libs(ctx, names: list[str] | None) -> list[str]:
    if not names:
        return [lib.id for lib in ctx.libs.list()]
    out = []
    for n in names:
        lib = ctx.libs.get(n) or next((x for x in ctx.libs.list() if x.name.lower() == n.lower()), None)
        if lib is None:
            raise SystemExit(f"No library called '{n}'.")
        out.append(lib.id)
    return out


def cmd_search(a) -> int:
    ctx = _ctx()
    r = ctx.settings.retrieval()
    hits, dbg = ctx.retriever.search([a.query], _resolve_libs(ctx, a.library), top_k=a.top_k, candidates=r["candidates"],
                                     max_per_doc=r["max_per_doc"], rerank=r["rerank"], reranker_model=r["reranker_model"])
    for i, h in enumerate(hits, 1):
        loc = f"{h.title}{':' + h.anchor if h.kind == 'code' and h.anchor else ''}"
        print(f"[{i}] {h.heading}\n    {loc}  (score {h.score:.3f})")
        if a.full:
            print("    " + h.text.replace("\n", "\n    ")[:1500])
    print(f"\n{len(hits)} passages in {dbg['timing_ms'].get('total', '?')} ms", file=sys.stderr)
    return 0


def cmd_ask(a) -> int:
    ctx = _ctx()
    ids = _resolve_libs(ctx, a.library)
    for ev in ctx.engine.answer_stream(a.question, [], ids):
        t = ev["type"]
        if t == "token":
            print(ev["text"], end="", flush=True)
        elif t == "sources":
            print("Sources:", ", ".join(f"[{s['n']}] {s['title']}" for s in ev["sources"][:6]), file=sys.stderr)
        elif t == "error":
            print(f"\nError: {ev['message']}", file=sys.stderr)
            return 1
        elif t == "done":
            print()
    return 0


def cmd_serve(a) -> int:
    import threading
    import webbrowser

    import uvicorn

    from .app import setup_logging
    from .server import create_app

    setup_logging()
    app = create_app(_ctx())
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{a.port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="info")
    return 0


def cmd_download_model(a) -> int:
    from .models import download_model

    download_model(a.model, Path(a.dest), a.reranker or None)
    print(f"Model files are in {a.dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = [x for x in argv if not x.startswith("-psn_")]  # old macOS Finder launch argument
    p = argparse.ArgumentParser(prog="perplexify", description="Search and chat with manuals and code repositories.")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("app", help="start the desktop app in its own window (default)")
    s.add_argument("--browser", action="store_true", help="use your default browser (with a tray icon) instead of the app's own window")
    s.add_argument("--no-browser", action="store_true", help="with --browser: do not open the page, only run the app")
    s.add_argument("--no-tray", action="store_true", help="with --browser: no tray icon (stop with Ctrl+C)")
    s.add_argument("--port", type=int)
    s.add_argument("--debug", action="store_true", help="enable the developer tools (F12) in the window")

    s = sub.add_parser("serve", help="run only the web server in this terminal")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")

    s = sub.add_parser("connect", help="connect a cloud model: chatgpt | claude | gemini | deepseek | custom")
    s.add_argument("provider")
    s.add_argument("--key")
    s.add_argument("--model")
    s.add_argument("--base-url")

    s = sub.add_parser("add-docs", help="add a documentation website or a folder of documents")
    s.add_argument("name")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--url")
    g.add_argument("--path")

    s = sub.add_parser("add-repo", help="add a code repository (GitHub address, git URL or folder)")
    s.add_argument("name")
    s.add_argument("source")
    s.add_argument("--ref")
    s.add_argument("--token")
    s.add_argument("--include", action="append")
    s.add_argument("--exclude", action="append")

    sub.add_parser("list", help="list libraries")

    s = sub.add_parser("search", help="search without asking a model")
    s.add_argument("query")
    s.add_argument("-l", "--library", action="append")
    s.add_argument("-k", "--top-k", type=int, default=8)
    s.add_argument("--full", action="store_true")

    s = sub.add_parser("ask", help="ask a question with the connected model")
    s.add_argument("question")
    s.add_argument("-l", "--library", action="append")

    s = sub.add_parser("download-model", help="download the built-in embedding model (used when building installers)")
    s.add_argument("--dest", required=True)
    s.add_argument("--model", default="BAAI/bge-small-en-v1.5")
    s.add_argument("--reranker")

    s = sub.add_parser("selftest", help="check that this installation works")
    s.add_argument("--skip-embed", action="store_true")

    sub.add_parser("mcp", help="run as an MCP server over stdio (for Claude Desktop, Claude Code, Cursor...)")
    sub.add_parser("stop", help="ask a running copy of the app to quit (the installer uses this)")
    sub.add_parser("version")

    a = p.parse_args(argv)
    cmd = a.cmd or "app"
    if cmd == "app":
        from .app import run_app

        return run_app(window=not getattr(a, "browser", False), open_browser=not getattr(a, "no_browser", False),
                       use_tray=not getattr(a, "no_tray", False), port=getattr(a, "port", None),
                       debug=getattr(a, "debug", False))
    if cmd == "version":
        print(__version__)
        return 0
    if cmd == "mcp":
        from .mcp_server import serve

        return serve()
    if cmd == "stop":
        from .app import stop_running

        return stop_running()
    if cmd == "selftest":
        from .selftest import run

        return run(skip_embed=a.skip_embed)
    handler = {"serve": cmd_serve, "connect": cmd_connect, "add-docs": cmd_add_docs, "add-repo": cmd_add_repo,
               "list": cmd_list, "search": cmd_search, "ask": cmd_ask, "download-model": cmd_download_model}[cmd]
    return handler(a)
