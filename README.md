# Perplexify

Ask questions about documentation and code repositories that are too big for a model's context window.

Perplexify reads your docs (websites, PDFs, Markdown, folders) and source code (GitHub repos or folders), splits them
into passages and builds a search index on your computer. When you ask something, it finds the few relevant passages,
sends only those to the model you picked, and shows the answer with citations you can open. The model can be a cloud one
(ChatGPT, Claude, Gemini, DeepSeek, any OpenAI-compatible service) or a local one that runs on your machine. The Search
tab works with no model at all.

- Indexing is local. The built-in embedding model needs no key and works offline.
- Only your question and the retrieved excerpts leave the computer, and only to the provider you connected. With a
  local model nothing leaves.
- Libraries are separate. Tick any combination in the sidebar for each question.
- Chats and searches are saved in the History tab (can be turned off or cleared). Bookmarks live in nested folders and
  are not cleared with history.
- Retrieval defaults (30 passages, rewriting, reranking, and so on) can be changed in Settings > Search.

## Install

Run `Perplexify-Setup-<version>-x64.exe`. It installs for the current user, no admin needed. Paste an API key, add a
docs site, repo or folder, and ask.

The window uses Microsoft Edge WebView2, which the installer adds if missing. Without it, or with `--browser`, the app
opens in your default browser and keeps a tray icon.

Data lives in `%LOCALAPPDATA%\Perplexify`, so upgrading never loses it. API keys are encrypted with Windows DPAPI
(macOS Keychain on a Mac); if that fails the key is not saved.

## Local models

Pick **Local model** when connecting. Answers are written by a model running on your computer through
[llama.cpp](https://github.com/ggml-org/llama.cpp), with no key and nothing sent anywhere. Small models make more
mistakes than cloud ones. The 7B and 8B options need about 8 GB of free memory.

- **Models:** use a `.gguf` file you already have (it stays where it is), or download one of six checked models from
  Hugging Face. Downloads resume and are verified by checksum.
- **Engine:** the installer ships llama.cpp. It uses the GPU through Vulkan when there is one with room (Settings >
  Models > GPU use).
- **Running:** the model loads on the first question and unloads when you quit. It listens on `127.0.0.1` only, behind a
  per-run key. Log: `%LOCALAPPDATA%\Perplexify\logs\llama-server.log`.
- **From source:** there is no engine yet. Press *Download engine* in the Local model panel or run
  `python -m perplexify download-llama`.

## Development

You need Python 3.12 (64-bit) and, for the installer, [Inno Setup 6](https://jrsoftware.org/isdl.php).

1. Open the folder in VS Code.
2. Run the task **Set up dev environment** (creates `.venv`, installs dependencies, runs tests).
3. Press **F5** to run the app. The "scratch data" configuration uses `.dev-data` and won't touch an installed copy.

The interface is plain HTML/CSS/JS in `perplexify/web`, with no build step. With `--debug`, Ctrl+R reloads the window
and F12 opens the inspector. Python changes need a restart. `python -m perplexify app --browser` runs the same page in
a browser.

| Task | How |
| --- | --- |
| Run tests | task *Run unit tests*, or `python -m unittest discover -s tests` |
| Check an install end to end | task *Run self-test* (`python -m perplexify selftest`) |
| Command line | `.venv\Scripts\python.exe -m perplexify --help` |

Where things live:

```
perplexify/            the app
  web/                 interface (index.html, style.css, js/)
  loaders/             HTML, PDF, text and code readers
  llm/                 model clients (Claude, OpenAI-compatible, Gemini, local)
  embeddings/          built-in and cloud embedding back ends
  retrieval.py         hybrid search, fusion, reranking
  rag.py               prompts, excerpt packing, citations
  history.py           chats and searches (history.db)
  bookmarks.py         bookmarks (bookmarks.db)
  localllm.py          local model catalog, llama.cpp process, downloads
  server.py            local web API (127.0.0.1 only)
  desktop.py           native window (pywebview + WebView2)
  app.py, cli.py       startup, command line
tests/                 unit tests
installer/             Windows installer build
.vscode/               tasks and debug configs
```

Common edits: colours and spacing in `web/style.css`; window behaviour in `desktop.py` (its `THEMES` must match the
stylesheet, a test checks); model prompts in `rag.py`; retrieval defaults in `settings.py`; providers in `providers.py`;
the llama.cpp build in `installer/pins.json`.

## Build the Windows installer

Press **Ctrl+Shift+B** (*Build Windows installer*). Output: `dist\Perplexify-Setup-<version>-x64.exe`.

The first build downloads a few hundred MB and takes several minutes. Downloads are cached in `installer\cache`. The
build runs the tests, stages a self-contained copy in `installer\build\stage` (pinned Python, packages from
`installer\windows-constraints.txt`, the embedding model, licences), self-tests that copy offline, then compiles it with
Inno Setup.

From a terminal: `powershell -File installer\build.ps1 [-Fast] [-Clean] [-SkipTests] [-SkipSelfTest] [-Relock]`.

The task *Test the installer* installs the newest build into a temp folder, runs it, upgrades it, uninstalls it and
leaves nothing behind. A window flashes on screen.

| Change | Edit |
| --- | --- |
| Version | `perplexify/__init__.py` |
| Name, publisher | `installer/pins.json` |
| Dependencies | `requirements.txt`, then `installer\build.ps1 -Relock` |
| Python version | `installer/pins.json`, then rebuild with `-Clean` |
| Installer pages and shortcuts | `installer/Perplexify.iss` |

See [installer/README.md](installer/README.md) for silent install and code signing.

## Command line and MCP

The installed app includes the same CLI:

```
"%LOCALAPPDATA%\Programs\Perplexify\runtime\python.exe" -m perplexify --help
```

Commands: `app`, `serve`, `add-docs`, `add-repo`, `list`, `search`, `ask`, `connect`, `stop`, `selftest`,
`download-llama`. `app` takes `--browser`, `--debug` and `--port N`.

`python -m perplexify mcp` runs a stdio MCP server so MCP-capable tools can search your libraries. Point the tool at
`runtime\python.exe` with the arguments `-P -m perplexify mcp`. Not tested against specific MCP clients yet.

## Troubleshooting

- **"Unknown publisher" on install:** the installer isn't code-signed. Choose *More info > Run anyway*.
- **App won't start:** check `%LOCALAPPDATA%\Perplexify\logs` (`perplexify.log`, and `crash.log` for startup errors).
- **Opens in the browser instead of a window:** WebView2 is missing or failed to load. Install the
  [Evergreen runtime](https://developer.microsoft.com/microsoft-edge/webview2); `selftest` reports it too.
- **Local model won't load or is slow:** the panel says why. Try a smaller model or context size, or set GPU use to Off.

## macOS

A Mac build can't be made on Windows, so GitHub Actions builds it (`.github/workflows/macos-installer.yml`). Run the
**macOS installer** workflow from the Actions tab, or push a tag like `v0.1.0`, then download the `.dmg` artifact. The
app is ad-hoc signed unless you add signing secrets, so macOS will ask you to choose *Open Anyway* in System Settings >
Privacy & Security. This workflow hasn't been run yet, so expect a fix or two on the first run.
