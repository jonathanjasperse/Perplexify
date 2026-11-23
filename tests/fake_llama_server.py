"""A tiny stand-in for llama.cpp's `llama-server`, used by the tests (tests/test_local.py) through PERPLEXIFY_LLAMA_SERVER.

It takes the arguments the app passes, listens on 127.0.0.1:<port> and speaks just enough of the real server's HTTP API:

    GET  /health                 200 {"status":"ok"} when loaded, 503 while "loading the model" (FAKE_LLAMA_LOAD_DELAY seconds)
    GET  /props                  {"default_generation_settings": {"n_ctx": ...}}   (needs the API key)
    POST /tokenize               {"tokens": [...]}                                  (needs the API key)
    POST /v1/chat/completions    streaming server-sent events like the real thing   (needs the API key)

Behaviour is steered by environment variables (all optional):

    FAKE_LLAMA_LOG=<file>        append one JSON line per event (start, chat, aborted, tokenize) for the test to read
    FAKE_LLAMA_LOAD_DELAY=<s>    seconds the model "loads" before /health says ok
    FAKE_LLAMA_FAIL=<kind>       exit while loading with a message the app must understand: oom | arch | corrupt | other,
                                 template (fails only with --jinja), gpu (fails unless --device none), bind:<n> (fails the first n starts)
    FAKE_LLAMA_TOKENS=<n>        words in an answer (default 6);  FAKE_LLAMA_TOKEN_DELAY=<s> pause between them (default 0)
    FAKE_LLAMA_PROMPT_DELAY=<s>  seconds of silence before the first byte of an answer (reading a long prompt on a slow CPU)
    FAKE_LLAMA_REASONING=1       send reasoning_content before the answer
    FAKE_LLAMA_CHARS_PER_TOKEN=<n>   how /tokenize and the context check count tokens (default 4)
    FAKE_LLAMA_CRASH_AFTER=<s>   die suddenly this many seconds after becoming ready
    FAKE_LLAMA_GPU=1             print the lines a real engine prints when it uses a graphics card (at -lv 4 only, like the real one)
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def arg(name, default=None, flag_only=False):
    a = sys.argv
    if name in a:
        i = a.index(name)
        return True if flag_only else (a[i + 1] if i + 1 < len(a) else default)
    return False if flag_only else default


def log_event(**kw):
    path = os.environ.get("FAKE_LLAMA_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"pid": os.getpid(), "t": time.time(), **kw}) + "\n")


if arg("--version", flag_only=True):
    print("version: 1.0.0-dev (build 12359, commit b1b770d90)\nbuilt with a fake for tests")        # the format of the real b12359
    sys.exit(0)

PORT = int(arg("--port", "0"))
KEY = arg("--api-key", "")
N_CTX = int(arg("-c", "4096"))
MODEL = arg("-m", "")
CPT = float(os.environ.get("FAKE_LLAMA_CHARS_PER_TOKEN", "4"))
READY_AT = time.time() + float(os.environ.get("FAKE_LLAMA_LOAD_DELAY", "0"))
state = {"ready_logged": False}

args_seen = sys.argv[1:]
log_event(event="start", args=args_seen)
print(f"main: loading model '{MODEL}'", flush=True)

fail = os.environ.get("FAKE_LLAMA_FAIL", "")
if fail:
    kind, _, count = fail.partition(":")
    should_fail = True
    if kind == "template":
        should_fail = "--jinja" in args_seen
    elif kind == "gpu":
        should_fail = not ("--device" in args_seen and args_seen[args_seen.index("--device") + 1] == "none")
    elif kind == "bind":
        counter = os.environ.get("FAKE_LLAMA_LOG", "") + ".bind"
        n = 0
        if os.path.exists(counter):
            n = int(open(counter).read() or 0)
        open(counter, "w").write(str(n + 1))
        should_fail = n < int(count or 1)
    if should_fail:
        time.sleep(0.2)
        message = {
            "oom": "ggml_backend_cpu_buffer_type_alloc_buffer: failed to allocate buffer of size 17179869184\nllama_model_load: error loading model: unable to allocate CPU buffer",
            "arch": "llama_model_load: error loading model: error loading model architecture: unknown model architecture: 'zorblax'",
            "corrupt": "gguf_init_from_file_impl: invalid magic characters: 'abcd'\nllama_model_load: error loading model: failed to read magic",
            "template": "common_chat_templates_init: failed to parse the jinja chat template (minja): unexpected token",
            "gpu": "ggml_vulkan: Failed to create instance: vk::createInstance: ErrorInitializationFailed",
            "bind": "srv  start: couldn't bind HTTP server socket, hostname: 127.0.0.1, port: " + str(PORT),
        }.get(kind, "something went wrong in the engine")
        print(message, flush=True)
        sys.exit(1)

def log_level():
    a = sys.argv
    return int(a[a.index("-lv") + 1]) if "-lv" in a else 3


if os.environ.get("FAKE_LLAMA_GPU") and log_level() >= 4:       # the real engine says this at -lv 4 and not before
    print("0.00.161.360 I cmn  common_param: device_info:\n"
          "0.00.162.109 I cmn  common_param:   - Vulkan0 : Fake GPU 9000 (16GB) (28016 MiB, 27248 MiB free)", flush=True)
    print("0.00.410.836 I llama_prepare_model_devices: using device Vulkan0 (Fake GPU 9000 (16GB)) (unknown id) - 27247 MiB free", flush=True)
    print("0.00.664.261 I load_tensors: offloaded 25/25 layers to GPU", flush=True)


def tokens_of(text):
    return max(1, int(len(text) / CPT))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"      # the body ends when the connection closes: no chunk framing to get wrong

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self):
        if self.headers.get("Authorization") == f"Bearer {KEY}":
            return True
        self._json(401, {"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}})
        return False

    def do_GET(self):
        if self.path.startswith("/health"):
            if time.time() < READY_AT:
                return self._json(503, {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}})
            if not state["ready_logged"]:
                state["ready_logged"] = True
                print(f"main: server is listening on http://127.0.0.1:{PORT}", flush=True)
                crash = os.environ.get("FAKE_LLAMA_CRASH_AFTER")
                if crash:
                    threading.Timer(float(crash), lambda: os._exit(3)).start()
            return self._json(200, {"status": "ok"})
        if not self._authorised():
            return
        if self.path.startswith("/props"):
            return self._json(200, {"default_generation_settings": {"n_ctx": N_CTX}, "total_slots": 1, "model_path": MODEL})
        self._json(404, {"error": {"code": 404, "message": "not found", "type": "not_found_error"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not self._authorised():
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": {"code": 400, "message": "bad json", "type": "invalid_request_error"}})
        if self.path.startswith("/tokenize"):
            n = tokens_of(body.get("content", ""))
            log_event(event="tokenize", n=n)
            return self._json(200, {"tokens": list(range(n))})
        if self.path.startswith("/v1/chat/completions"):
            return self.chat(body)
        self._json(404, {"error": {"code": 404, "message": "not found", "type": "not_found_error"}})

    def chat(self, body):
        messages = body.get("messages", [])
        prompt_tokens = sum(tokens_of(str(m.get("content", ""))) + 4 for m in messages)
        log_event(event="chat", body=body, auth=self.headers.get("Authorization"), prompt_tokens=prompt_tokens)
        if prompt_tokens > N_CTX:
            return self._json(400, {"error": {"code": 400, "type": "exceed_context_size_error", "n_prompt_tokens": prompt_tokens, "n_ctx": N_CTX,
                                              "message": f"request ({prompt_tokens} tokens) exceeds the available context size ({N_CTX} tokens), try increasing it"}})
        time.sleep(float(os.environ.get("FAKE_LLAMA_PROMPT_DELAY", "0")))          # "reading the prompt": nothing is sent meanwhile
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        n_words = int(os.environ.get("FAKE_LLAMA_TOKENS", "6"))
        delay = float(os.environ.get("FAKE_LLAMA_TOKEN_DELAY", "0"))

        def send(obj):
            self.wfile.write(b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n")
            self.wfile.flush()

        sent = 0
        try:
            if os.environ.get("FAKE_LLAMA_REASONING"):
                send({"choices": [{"index": 0, "delta": {"reasoning_content": "Let me think about the excerpts."}}]})
            for i in range(n_words):
                send({"choices": [{"index": 0, "delta": {"content": f"w{i} "}, "finish_reason": None}]})
                sent += 1
                if delay:
                    time.sleep(delay)
            send({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            send({"choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": n_words, "total_tokens": prompt_tokens + n_words}})
            send(b"[DONE]")
            log_event(event="finished", sent=sent)
        except (BrokenPipeError, ConnectionResetError, OSError):
            log_event(event="aborted", sent=sent)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


Server(("127.0.0.1", PORT), Handler).serve_forever()
