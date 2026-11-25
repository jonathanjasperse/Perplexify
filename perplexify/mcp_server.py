"""A minimal MCP (Model Context Protocol) server over stdio, so Claude Desktop, Claude Code, Cursor and other
agents can search the same libraries. No extra dependency: it speaks JSON-RPC lines on stdin/stdout.

Add to a client's config:  {"command": "<install>\\runtime\\python.exe", "args": ["-m", "perplexify", "mcp"]}
"""
from __future__ import annotations

import json
import sys
import traceback

from . import __version__
from .context import AppContext

SUPPORTED = ["2025-06-18", "2025-03-26", "2024-11-05"]
MAX_TEXT = 14000

TOOLS = [
    {"name": "list_libraries", "description": "List the searchable libraries (manuals and code repositories) with their ids.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search_library",
     "description": "Search documentation and source code. Returns the most relevant passages with their source and, for "
                    "code, the file path and line range.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "What to look for: a question, or identifiers such as processBlock"},
         "libraries": {"type": "array", "items": {"type": "string"}, "description": "Library ids or names (default: all)"},
         "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}}}},
    {"name": "read_source",
     "description": "Read lines of an indexed file (from search results) with line numbers.",
     "inputSchema": {"type": "object", "required": ["library", "path"], "properties": {
         "library": {"type": "string"}, "path": {"type": "string", "description": "File path as shown in search results"},
         "start_line": {"type": "integer", "default": 1}, "end_line": {"type": "integer"}}}},
    {"name": "web_search",
     "description": "Search the web and read the best pages. Returns numbered excerpts with each page's title and URL, "
                    "for citing. Needs no key: it searches Wikipedia, PubMed, arXiv, OpenAlex, Stack Overflow, GitHub and DuckDuckGo "
                    "(Tavily or Brave Search instead when a key is saved in Perplexify).",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "What to search for, as you would type it into a search engine"},
         "max_pages": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}}}},
]


class Handler:
    def __init__(self) -> None:
        self.ctx = AppContext()

    def _libs(self, names: list[str] | None) -> list[str]:
        if not names:
            return [lib.id for lib in self.ctx.libs.list()]
        out = []
        for n in names:
            lib = self.ctx.libs.get(n) or next((x for x in self.ctx.libs.list() if x.name.lower() == n.lower()), None)
            if lib is None:
                raise ValueError(f"No library called '{n}'")
            out.append(lib.id)
        return out

    def list_libraries(self, _a: dict) -> str:
        rows = []
        for lib in self.ctx.libs.list():
            s = lib.store.stats()
            kinds = ", ".join(f"{k}: {v}" for k, v in s["kinds"].items())
            rows.append(f"- {lib.id}: {lib.name} ({s['documents']} files/pages, {s['chunks']} passages; {kinds})")
        return "\n".join(rows) or "No libraries yet. Add one in the Perplexify app."

    def search_library(self, a: dict) -> str:
        r = self.ctx.settings.retrieval()
        hits, dbg = self.ctx.retriever.search(
            [a["query"]], self._libs(a.get("libraries")), top_k=int(a.get("top_k") or 8), candidates=r["candidates"],
            max_per_doc=r["max_per_doc"], rerank=r["rerank"], reranker_model=r["reranker_model"])
        if not hits:
            return "No matching passages."
        out, size = [], 0
        for i, h in enumerate(hits, 1):
            loc = h.title + (f":{h.anchor}" if h.kind == "code" and h.anchor else "")
            fence = f"```{h.lang}\n{h.text}\n```" if h.kind == "code" else h.text
            block = f"[{i}] {h.heading}\nlibrary: {h.library_name} | source: {loc}\n{fence}"
            size += len(block)
            if size > MAX_TEXT and out:
                break
            out.append(block)
        return "\n\n".join(out)

    def web_search(self, a: dict) -> str:
        from . import websearch

        hits, info = websearch.research([a["query"]], self.ctx.settings, per_query=int(a.get("max_pages") or 5),
                                        max_pages=int(a.get("max_pages") or 5), chars_per_page=2500, question=a["query"])
        if not hits:
            return "No web results."
        out, size = [], 0
        for i, h in enumerate(hits, 1):
            block = f"[{i}] {h.title}\nurl: {h.uri}\n{h.text}"
            size += len(block)
            if size > MAX_TEXT and out:
                break
            out.append(block)
        return "\n\n".join(out) + f"\n\n(searched with {info.get('engine') or 'the web'})"

    def read_source(self, a: dict) -> str:
        lib = self.ctx.libs.get(a["library"]) or next((x for x in self.ctx.libs.list() if x.name.lower() == a["library"].lower()), None)
        if lib is None:
            raise ValueError("Unknown library")
        row = next((d for d in lib.store.iter_documents() if d["title"] == a["path"]), None) or \
            next((d for d in lib.store.iter_documents() if d["title"].endswith(a["path"])), None)
        if row is None:
            raise ValueError("That file is not in the library")
        from pathlib import Path

        p = (lib.source_dir / row["path"]) if row["origin"] == "mirror" else Path(row["path"])
        lines = p.read_text("utf-8", errors="replace").splitlines()
        start = max(1, int(a.get("start_line") or 1))
        end = min(len(lines), int(a.get("end_line") or start + 199), start + 399)
        return "\n".join(f"{n:5d}  {lines[n - 1]}" for n in range(start, end + 1))

    # ------------------------------------------------------------------ JSON-RPC
    def handle(self, msg: dict) -> dict | None:
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                want = (msg.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": want if want in SUPPORTED else SUPPORTED[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "manual-rag", "version": __version__}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                fn = getattr(self, params.get("name", ""), None)
                if params.get("name") not in {t["name"] for t in TOOLS} or fn is None:
                    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "Unknown tool"}}
                try:
                    text, is_error = fn(params.get("arguments") or {}), False
                except Exception as e:  # noqa: BLE001 - tool errors are reported to the model, not raised
                    text, is_error = f"Error: {e}", True
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def serve() -> int:
    handler = Handler()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handler.handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0
