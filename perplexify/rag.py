"""Question answering: retrieve passages, fit them into the model's context window, stream a grounded answer."""
from __future__ import annotations

import logging
import re
import time
from typing import Iterator

from . import providers, websearch
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

# Claude and ChatGPT are strong enough to reason beyond the excerpts, so they get prompts written for deep research by data
# scientists and biomedical engineers: the excerpts are the evidence, their own knowledge is allowed to build on it, and the
# answer keeps the two apart. The other models (Gemini, DeepSeek, local and custom ones) keep the strict prompts above.
EXPERT_PROVIDERS = ("anthropic", "openai")

_EXPERT_COMMON = """

How to answer:
1. The excerpts are your evidence. Cite them inline with bracketed numbers like [1] or [2][4], right after the statement they support. Anything specific to this material (names, parameters, values, units, thresholds, versions, claims, quotes) must come from an excerpt and be cited. Never invent a citation or attribute your own knowledge to the source.
2. You are expected to go beyond the excerpts. Combine what they say with your general scientific and engineering knowledge: explain the underlying concepts, connect the material to established methods and standards, infer what follows from it, and fill gaps with sound reasoning. Mark that content plainly so the reader can tell the difference, for example "From general knowledge:", "Inference:" or "Not in the source, but typically...". Do not refuse or stop at "the excerpts do not say"; say what they do and do not cover, then reason from there.
3. Give engineering guidance, not only a summary. When it helps, cover how to build or apply the idea: the approach and its steps, design choices and trade-offs, assumptions and where they break, edge cases and failure modes, how to validate or test it (metrics, controls, data splits, verification and validation), and what to measure first.
4. Be rigorous. Check units, scales, sample sizes, statistical validity, data leakage, bias and reproducibility. When the source is ambiguous, weak, internally inconsistent or at odds with established practice, say so and explain the consequences. State your confidence when it matters.
5. For clinical, regulatory or safety-related topics (medical devices, patient data, diagnostics, dosing), give the technical substance and name the relevant norms or regulations from your knowledge (for example IEC 62304, ISO 14971, ISO 13485, FDA and EU MDR pathways, HIPAA, GDPR) as general knowledge, flag what needs expert or regulatory review, and do not present your own reasoning as medical advice.
6. Write for experts: precise terminology, no padding. Lead with the answer, then the reasoning and the engineering detail. Use headings, short lists, equations or tables only when they make the answer clearer. Put code and formulas in fenced blocks and copy anything quoted from the source exactly."""

SYSTEM_PROMPT_EXPERT = """You are a senior research partner for data scientists and biomedical engineers doing deep research into these document(s): {manuals}.

You are given numbered excerpts retrieved from them.""" + _EXPERT_COMMON

SYSTEM_PROMPT_EXPERT_CODE = """You are a senior research and engineering partner for data scientists and biomedical engineers working with these repositories and documents: {manuals}.

You are given numbered excerpts of source code and documentation retrieved from them. When you point at code, give its file path and line range as shown in the excerpt header, for example `src/Foo.cpp:120-148`, and quote code exactly as written. Do not invent functions, parameters or behaviour that the code is claimed to have; suggestions for how it could be improved or extended are welcome, but present them as suggestions.""" + _EXPERT_COMMON

SYSTEM_PROMPT_RESEARCH = """You are a careful research assistant. Today is {today}.

You are given numbered excerpts: pages found on the web for this question{and_libraries}. Follow these rules:
1. Answer from the excerpts. You may add widely known background, but every specific fact, figure, date or quote must come from an excerpt.
2. Cite inline with bracketed numbers like [1] or [2][4], right after the statement they support. Cite the most direct source.
3. When sources disagree or are dated, say so and prefer the most recent, authoritative one.
4. If the excerpts do not answer the question, say what is missing instead of guessing.
5. Be direct and well organised: a short answer first, then the details. Use headings or bullet points only when they help."""

WEB_PLAN_SYSTEM = ("You plan web searches for a research assistant. Given a question, write 1 to 3 web search queries that "
                   "together find the facts needed to answer it: specific, with the names, versions and dates that matter, "
                   "the way you would type them into a search engine. One query per line, no numbering, no commentary.")

REWRITE_SYSTEM = ("You rewrite a follow-up question into one standalone search query for a documentation and source code "
                  "search engine. Keep class, function, file and parameter names exactly as written. "
                  "Output only the query, nothing else.")
EXPAND_SYSTEM = ("You help search technical documentation and source code. Given a question, write up to 3 alternative "
                 "search queries that use different wording and the identifiers (class, function, file or setting names) "
                 "most likely to appear in the material. One query per line, no numbering, no commentary.")

NO_MODEL = ("No model is connected yet. Open Settings > Models and paste an API key for ChatGPT, Claude, Gemini or DeepSeek, "
            "or choose a local model that runs on this computer.")


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
    if h.kind == "web":
        return f"[{n}] {h.title} ({h.uri})\n{body}"
    if h.kind == "code":
        lines = line_range(h.anchor)
        symbol = h.heading[len(h.title) + 3:] if h.heading.startswith(h.title + " > ") else ""
        head = f"[{n}] {h.title}" + (f":{lines}" if lines else "") + (f"  ({symbol})" if symbol else "")
        return f"{head}\n```{h.lang}\n{body}\n```"
    return f"[{n}] {h.heading}\n{body}"


def interleave(first: list[Hit], second: list[Hit]) -> list[Hit]:
    """Alternate two best-first lists (web pages and library passages), so a tight budget keeps the best of both."""
    out: list[Hit] = []
    for i in range(max(len(first), len(second))):
        out += [x[i] for x in (first, second) if i < len(x)]
    return out


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

    def _plan_web(self, provider, question: str) -> list[str]:
        """The model decides what to look up: 1-3 web search queries for the (standalone) question."""
        out = provider.complete([{"role": "user", "content": f"Today is {time.strftime('%Y-%m-%d')}.\n\nQuestion: {question}"}],
                                WEB_PLAN_SYSTEM)
        out = re.sub(r"<think>.*?</think>", "", out, flags=re.S)
        lines = [re.sub(r"^[\-\*\d\.\)\s]+", "", ln).strip().strip('"') for ln in out.splitlines()]
        return list(dict.fromkeys(ln for ln in lines if 3 <= len(ln) <= 300))[:3]

    def _web_research(self, provider, question: str) -> Iterator[dict]:
        """Plan the searches with the model, run them and read the pages. Yields trace events; the last one is
        {"type": "_web", "hits": [...], "info": {...}}, which the caller keeps to itself."""
        try:
            queries = self._plan_web(provider, question) or [question]
        except LLMError:
            queries = [question]
        yield {"type": "web_search", "queries": queries}
        try:
            hits, info = websearch.research(queries, self.ctx.settings, question=question)
        except websearch.WebSearchError as e:
            yield {"type": "notice", "kind": "info", "text": str(e)}
            hits, info = [], {"engine": "", "results": 0, "pages": 0, "error": str(e)}
        yield {"type": "_web", "hits": hits, "info": info}

    # ------------------------------------------------------------------ main entry
    def answer_stream(self, question: str, history: list[dict], library_ids: list[str],
                      profile_id: str | None = None, overrides: dict | None = None, *, research: bool = False) -> Iterator[dict]:
        t0 = time.perf_counter()
        settings = self.ctx.settings
        question = question.strip()
        if not question:
            yield {"type": "error", "message": "Type a question first."}
            return
        profile = settings.profile(profile_id)
        if profile is None or not settings.can_chat(profile):
            yield {"type": "error", "message": NO_MODEL, "code": "no_model"}
            return
        if not library_ids and not research:
            yield {"type": "error", "message": "Select at least one library in the sidebar to search, or turn on Research to search the web."}
            return
        retr = {**settings.retrieval(), **(overrides or {})}
        provider = make_provider(profile, settings.api_key(profile))
        local = providers.is_local(profile)      # a model on this computer: it may have to be loaded first, and its window is small
        info: dict = {}
        if not local:
            try:
                info = self.ctx.llm_info(profile)
            except Exception:  # noqa: BLE001 - the gauge is informative, never critical
                pass
        window = int(info.get("context_window") or profile.get("context_tokens") or 128_000)

        try:
            # A local model is started in the background right away (loading a large one takes up to a minute). The search below
            # carries on meanwhile; wherever the model is needed first the answer waits for it and says so ("status" events).
            engine = provider.begin() if local else None
            model_name = str(profile.get("model") or "local model")

            # 1. a standalone search query (follow-ups like "and what about its parameters?")
            queries = [question]
            yield {"type": "status", "text": "searching"}
            needs_model = (history and retr["query_rewrite"]) or (library_ids and retr["multi_query"]) or research
            if engine is not None and needs_model:
                yield from self._wait_for_engine(engine, model_name)      # the helper calls below already need it
            if history and retr["query_rewrite"]:
                try:
                    q = self._rewrite(provider, question, history)
                    if q and q.lower() != question.lower():
                        queries = [q]
                        yield {"type": "rewrite", "query": q}
                except LLMError:
                    pass
            if library_ids and retr["multi_query"]:
                try:
                    extra = self._expand(provider, queries[0])
                    if extra:
                        queries += extra
                        yield {"type": "expand", "queries": extra}
                except LLMError:
                    pass

            # 2. hybrid search of the libraries, and in Research mode the web (the model plans those searches)
            hits: list[Hit] = []
            debug: dict = {}
            if library_ids:
                hits, debug = self.ctx.retriever.search(
                    queries, library_ids, top_k=retr["top_k"], candidates=retr["candidates"],
                    max_per_doc=retr["max_per_doc"], rerank=retr["rerank"], reranker_model=retr["reranker_model"])
            if research:
                web_hits: list[Hit] = []
                for ev in self._web_research(provider, queries[0]):
                    if ev["type"] == "_web":
                        web_hits, debug["web"] = ev["hits"], ev["info"]
                    else:
                        yield ev
                hits = interleave(web_hits, hits)

            # 3. fit passages into the context window
            if engine is not None:
                yield from self._wait_for_engine(engine, model_name)
                info = provider.info()                     # the window the engine really has (it was asked for the one in Settings)
                window = int(info.get("context_window") or window)
            excerpt_budget = min(int(retr["max_excerpt_tokens"]), int(window * (0.5 if local else 0.55)))
            expert = getattr(provider, "provider_id", "") in EXPERT_PROVIDERS and not local
            prompt = self._prompt(question, history, hits, window, excerpt_budget, local, research, expert)
            if local:
                prompt = self._fit_local(provider, question, history, hits, window, excerpt_budget, prompt, research)
            used = prompt["used"]
            debug["excerpt_budget"] = prompt["budget"]
            yield {"type": "sources", "sources": [h.to_public(i + 1) for i, h in enumerate(used)],
                   "retrieved": len(hits), "debug": debug}
            if not used:
                msg = ("I couldn't find anything relevant on the web" + (" or in the selected libraries." if library_ids else ".")
                       if research else "I couldn't find anything relevant in the selected libraries.")
                if debug.get("pending"):
                    msg += f" {debug['pending']} passages are still waiting to be embedded, so search quality is reduced until indexing finishes."
                yield {"type": "token", "text": msg}
                yield {"type": "done", "elapsed_ms": round((time.perf_counter() - t0) * 1000)}
                return

            system, messages, breakdown = prompt["system"], prompt["messages"], prompt["breakdown"]
            yield {"type": "context", "window": window, "max_context": info.get("max_context"),
                   "estimated_prompt": prompt.get("real_tokens") or sum(breakdown.values()), "breakdown": breakdown,
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
            code = getattr(e, "code", None)           # a local model problem says what to offer: engine_missing, model_missing, ...
            yield {"type": "error", "message": str(e), **({"code": code} if code else {})}
        except Exception as e:  # noqa: BLE001
            log.exception("chat failed")
            yield {"type": "error", "message": f"Unexpected error: {type(e).__name__}: {e}"}

    # ------------------------------------------------------------------ the prompt, and the local model that has to hold it
    @staticmethod
    def _prompt(question: str, history: list[dict], hits: list[Hit], window: int, budget: int, local: bool,
                research: bool = False, expert: bool = False) -> dict:
        """System text, messages and the passages that fit into `budget` tokens of excerpts. A local model gets a little less
        of the window for excerpts and conversation: its tokenizer is unknown here, and the answer needs room too."""
        excerpts, used = build_excerpts(hits, budget)
        if not used:
            return {"used": [], "budget": budget}
        if research and any(h.kind == "web" for h in used):
            libs = ", ".join(dict.fromkeys(h.library_name for h in used if h.kind != "web"))
            system = SYSTEM_PROMPT_RESEARCH.format(today=time.strftime("%A %d %B %Y"),
                                                   and_libraries=f", and passages from the user's own libraries ({libs})" if libs else "")
        else:
            names = ", ".join(dict.fromkeys(h.library_name for h in used))
            code_share = sum(h.kind == "code" for h in used) / len(used)
            code = code_share >= 0.4
            if expert:
                system = (SYSTEM_PROMPT_EXPERT_CODE if code else SYSTEM_PROMPT_EXPERT).format(manuals=names)
            else:
                system = (SYSTEM_PROMPT_CODE if code else SYSTEM_PROMPT).format(manuals=names)
        hist = trim_history(history, int(window * (0.12 if local else 0.15)))
        user_msg = f"Excerpts:\n\n{excerpts}\n\nQuestion: {question}"
        messages = hist + [{"role": "user", "content": user_msg}]
        breakdown = {"system": estimate_tokens(system), "history": sum(estimate_tokens(m["content"]) for m in hist),
                     "excerpts": estimate_tokens(excerpts), "question": estimate_tokens(question)}
        return {"used": used, "budget": budget, "system": system, "messages": messages, "breakdown": breakdown}

    def _fit_local(self, provider, question: str, history: list[dict], hits: list[Hit], window: int, budget: int, prompt: dict,
                   research: bool = False) -> dict:
        """Count the prompt with the engine's own tokenizer and take passages out until it fits the window with room left for the
        answer (about 15% of the window, at least 384 tokens). The estimate (3.6 characters per token) is only a guess for a
        model whose tokenizer we do not know; a prompt that is too long is refused by the engine."""
        room = window - max(384, int(window * 0.15))
        for _ in range(3):
            if not prompt["used"]:
                return prompt
            text = "\n".join([prompt["system"]] + [m["content"] for m in prompt["messages"]])
            counted = provider.count_tokens(text)
            if counted is None:
                return prompt                                  # the engine cannot say: keep the estimate
            real = counted + 16 * (len(prompt["messages"]) + 1)  # + what the chat template adds around every message
            prompt["real_tokens"] = real
            if real <= room:
                return prompt
            est = sum(prompt["breakdown"].values())
            ratio = real / max(est, 1)                         # how far the estimate was off
            fixed = est - prompt["breakdown"]["excerpts"]
            target = (room - fixed * ratio) / ratio            # what is left for excerpts, in estimated tokens
            if target < 250:
                return prompt
            budget = int(min(budget * 0.9, target))
            prompt = self._prompt(question, history, hits, window, budget, True, research)
        return prompt

    @staticmethod
    def _wait_for_engine(attempt, model_name: str) -> Iterator[dict]:
        """Wait for a local model that is still loading. A "status" event every second keeps the interface informed (and lets a
        closed connection be noticed); raises when the model could not be started."""
        if attempt.done.is_set():
            attempt.result()
            return
        yield {"type": "status", "text": "loading_model", "model": model_name, "seconds": 0}
        while not attempt.done.wait(1.0):
            yield {"type": "status", "text": "loading_model", "model": model_name, "seconds": attempt.seconds}
        handle = attempt.result()
        yield {"type": "status", "text": "model_ready", "model": model_name, "seconds": attempt.seconds, "n_ctx": handle.n_ctx}
