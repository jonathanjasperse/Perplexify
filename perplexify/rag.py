"""Question answering: retrieve passages, fit them into the model's context window, stream a grounded answer."""
from __future__ import annotations

import logging
import re
import time
from typing import Iterator

from .embeddings import EmbeddingError
from .llm import LLMError, make_provider
from .retrieval import Hit
from .textutil import estimate_tokens

log = logging.getLogger("perplexify.rag")

SYSTEM_PROMPT = """You are a precise documentation assistant for these manual(s): {manuals}.

You are given numbered excerpts retrieved from the manual(s). Follow these rules:
1. Base your answer only on the excerpts. Do not fill gaps with outside knowledge; if something is not in the excerpts, say so.
2. Cite excerpts inline with bracketed numbers like [1] or [2][4], right after the statement they support.
3. If the excerpts do not answer the question, say that clearly and mention what related information they do contain.
4. Copy class names, function signatures and code exactly as written in the excerpts, including namespaces. Put code in fenced code blocks.
5. Be direct and concise. For how-to questions prefer short numbered steps."""

SYSTEM_PROMPT_CODE = """You are a precise code assistant for these repositories and manuals: {manuals}.

You are given numbered excerpts of source code and documentation retrieved from them. Follow these rules:
1. Base your answer only on the excerpts. Do not invent functions, parameters or behaviour. If something is not in the excerpts, say so, and name the file, class or function that most likely contains it.
2. Cite excerpts inline with bracketed numbers like [1] or [2][4]. When you point at code, also give its file path and line range as shown in the excerpt header, for example `src/Foo.cpp:120-148`.
3. Quote code exactly as written, including namespaces and signatures. Put code in fenced code blocks with the language.
4. Explain how the pieces fit together when the question needs it, but stay direct and concise."""

REWRITE_SYSTEM = ("You rewrite a follow-up question into one standalone search query for a documentation and source code "
                  "search engine. Keep class, function, file and parameter names exactly as written. "
                  "Output only the query, nothing else.")
EXPAND_SYSTEM = ("You help search technical documentation and source code. Given a question, write up to 3 alternative "
                 "search queries that use different wording and the identifiers (class, function, file or setting names) "
                 "most likely to appear in the material. One query per line, no numbering, no commentary.")

NO_MODEL = "No model is connected yet. Open Settings > Models and paste an API key for ChatGPT, Claude, Gemini or DeepSeek."


def _history_text(history: list[dict], limit_chars: int = 1800) -> str:
    lines = []
    for m in history[-6:]:
        who = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{who}: {str(m.get('content', ''))[:600]}")
    return "\n".join(lines)[-limit_chars:]


def trim_history(history: list[dict], budget_tokens: int) -> list[dict]:
    """Recent turns only, assistant answers shortened, newest kept first when over budget."""
    kept: list[dict] = []
    used = 0
    for m in reversed(history[-8:]):
        role = "user" if m.get("role") == "user" else "assistant"
        content = str(m.get("content", "")).strip()
        if role == "assistant" and len(content) > 1500:
            content = content[:1500] + " ..."
        t = estimate_tokens(content)
        if used + t > budget_tokens and kept:
            break
        kept.append({"role": role, "content": content})
        used += t
    kept.reverse()
    while kept and kept[0]["role"] != "user":  # the API wants the conversation to start with the user
        kept.pop(0)
    return kept


def line_range(anchor: str | None) -> str:
    """'L157-L177' -> '157-177' (empty when the anchor is not a line range)."""
    m = re.fullmatch(r"L(\d+)(?:-L(\d+))?", anchor or "")
    if not m:
        return ""
    return f"{m.group(1)}-{m.group(2)}" if m.group(2) else m.group(1)


def format_excerpt(n: int, h: Hit, body: str) -> str:
    """How one passage is shown to the model. Code gets a path:lines header and a fenced block."""
    if h.kind == "code":
        lines = line_range(h.anchor)
        symbol = h.heading[len(h.title) + 3:] if h.heading.startswith(h.title + " > ") else ""
        head = f"[{n}] {h.title}" + (f":{lines}" if lines else "") + (f"  ({symbol})" if symbol else "")
        return f"{head}\n```{h.lang}\n{body}\n```"
    return f"[{n}] {h.heading}\n{body}"


def build_excerpts(hits: list[Hit], budget_tokens: int) -> tuple[str, list[Hit]]:
    """Number the best passages and stop when the token budget is used up."""
    parts: list[str] = []
    used_hits: list[Hit] = []
    used = 0
    for h in hits:
        per_token = 3.0 if h.kind == "code" else 3.6  # code splits into more tokens per character
        n = len(used_hits) + 1
        cost = estimate_tokens(format_excerpt(n, h, h.text)) * (3.6 / per_token)
        body = h.text
        if used + cost > budget_tokens:
            room = budget_tokens - used - estimate_tokens(h.heading)
            if room < 250 or (used_hits and room < 400):
                break
            body = body[: int(room * per_token)].rstrip() + ("\n..." if h.kind != "code" else "\n// ...")
            cost = budget_tokens - used
        parts.append(format_excerpt(n, h, body))
        used_hits.append(h)
        used += cost
    return "\n\n".join(parts), used_hits


class RagEngine:
    def __init__(self, ctx):
        self.ctx = ctx

    # ------------------------------------------------------------------ helper model calls
    def _rewrite(self, provider, question: str, history: list[dict]) -> str:
        prompt = (f"Conversation so far:\n{_history_text(history)}\n\nFollow-up question: {question}\n\n"
                  "Standalone search query:")
        out = provider.complete([{"role": "user", "content": prompt}], REWRITE_SYSTEM)
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip().strip('"').splitlines()[0:1]
        q = out[0].strip() if out else ""
        return q if 0 < len(q) <= 400 else ""

    def _expand(self, provider, question: str) -> list[str]:
        out = provider.complete([{"role": "user", "content": question}], EXPAND_SYSTEM)
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S)
        lines = [re.sub(r"^[\-\*\d\.\)\s]+", "", ln).strip() for ln in out.splitlines()]
        return [ln for ln in lines if 3 <= len(ln) <= 300][:3]

    # ------------------------------------------------------------------ main entry
    def answer_stream(self, question: str, history: list[dict], library_ids: list[str],
                      profile_id: str | None = None, overrides: dict | None = None) -> Iterator[dict]:
        t0 = time.perf_counter()
        settings = self.ctx.settings
        question = question.strip()
        if not question:
            yield {"type": "error", "message": "Type a question first."}
            return
        profile = settings.profile(profile_id)
        if profile is None or not settings.api_key(profile):
            yield {"type": "error", "message": NO_MODEL, "code": "no_model"}
            return
        if not library_ids:
            yield {"type": "error", "message": "Select at least one library in the sidebar to search."}
            return
        retr = {**settings.retrieval(), **(overrides or {})}
        provider = make_provider(profile, settings.api_key(profile))
        info: dict = {}
        try:
            info = self.ctx.llm_info(profile)
        except Exception:  # noqa: BLE001 - the gauge is informative, never critical
            pass
        window = int(info.get("context_window") or profile.get("context_tokens") or 128_000)

        try:
            # 1. a standalone search query (follow-ups like "and what about its parameters?")
            queries = [question]
            yield {"type": "status", "text": "searching"}
            if history and retr["query_rewrite"]:
                try:
                    q = self._rewrite(provider, question, history)
                    if q and q.lower() != question.lower():
                        queries = [q]
                        yield {"type": "rewrite", "query": q}
                except LLMError:
                    pass
            if retr["multi_query"]:
                try:
                    extra = self._expand(provider, queries[0])
                    if extra:
                        queries += extra
                        yield {"type": "expand", "queries": extra}
                except LLMError:
                    pass

            # 2. hybrid search
            hits, debug = self.ctx.retriever.search(
                queries, library_ids, top_k=retr["top_k"], candidates=retr["candidates"],
                max_per_doc=retr["max_per_doc"], rerank=retr["rerank"], reranker_model=retr["reranker_model"])

            # 3. fit passages into the context window
            excerpt_budget = min(int(retr["max_excerpt_tokens"]), int(window * 0.55))
            excerpts, used = build_excerpts(hits, excerpt_budget)
            debug["excerpt_budget"] = excerpt_budget
            yield {"type": "sources", "sources": [h.to_public(i + 1) for i, h in enumerate(used)],
                   "retrieved": len(hits), "debug": debug}
            if not used:
                msg = "I couldn't find anything relevant in the selected libraries."
                if debug.get("pending"):
                    msg += f" {debug['pending']} passages are still waiting to be embedded, so search quality is reduced until indexing finishes."
                yield {"type": "token", "text": msg}
                yield {"type": "done", "elapsed_ms": round((time.perf_counter() - t0) * 1000)}
                return

            names = ", ".join(dict.fromkeys(h.library_name for h in used))
            code_share = sum(h.kind == "code" for h in used) / len(used)
            system = (SYSTEM_PROMPT_CODE if code_share >= 0.4 else SYSTEM_PROMPT).format(manuals=names)
            hist = trim_history(history, int(window * 0.15))
            user_msg = f"Excerpts:\n\n{excerpts}\n\nQuestion: {question}"
            messages = hist + [{"role": "user", "content": user_msg}]
            breakdown = {"system": estimate_tokens(system), "history": sum(estimate_tokens(m["content"]) for m in hist),
                         "excerpts": estimate_tokens(excerpts), "question": estimate_tokens(question)}
            yield {"type": "context", "window": window, "max_context": info.get("max_context"),
                   "estimated_prompt": sum(breakdown.values()), "breakdown": breakdown,
                   "model": profile.get("model"), "provider": profile.get("provider")}

            # 4. stream the answer
            yield {"type": "status", "text": "generating"}
            t_gen = time.perf_counter()
            usage: dict = {}
            n_chars = 0
            for d in provider.stream(messages, system):
                if d.kind == "text":
                    n_chars += len(d.text)
                    yield {"type": "token", "text": d.text}
                elif d.kind == "thinking":
                    yield {"type": "thinking", "text": d.text}
                elif d.kind == "usage":
                    usage = {k: v for k, v in (d.data or {}).items() if v is not None}
                elif d.kind in ("refusal", "info"):
                    yield {"type": "notice", "kind": d.kind, "text": d.text}
            if usage:
                yield {"type": "usage", **usage}
            gen_s = max(time.perf_counter() - t_gen, 1e-6)
            out_tokens = usage.get("completion_tokens") or estimate_tokens("x" * n_chars)
            yield {"type": "done", "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                   "tokens_per_s": round(out_tokens / gen_s, 1)}
        except (LLMError, EmbeddingError, RuntimeError) as e:
            yield {"type": "error", "message": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("chat failed")
            yield {"type": "error", "message": f"Unexpected error: {type(e).__name__}: {e}"}
