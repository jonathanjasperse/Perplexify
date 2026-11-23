# Perplexify

Ask questions about manuals and code repositories that are far larger than any model's context window.

Perplexify downloads or reads your documentation (websites, PDFs, Markdown, folders) and source code (GitHub
repositories or folders), cuts it into passages, and builds a search index on your computer. When you ask a question
it finds the few passages that matter, hands only those to the model you chose (a cloud model: ChatGPT, Claude, Gemini,
DeepSeek or any OpenAI-compatible service; or a **local model that runs on your own computer**), and shows the answer
with citations you can open.
You can also **search without any model**: the Search tab combines meaning search and keyword search over the same index.

- Everything is indexed locally. The built-in embedding model needs no key and works offline.
- Only your question and the few retrieved excerpts leave the computer, and only to the provider you connected. With a
  local model (see below) nothing leaves the computer at all.
- Libraries are separate: tick one, several or all of them in the sidebar for each question or search. Each library shows
  its size in passages and in approximate tokens.
- Chats and searches are kept on this computer (the **History** tab): open an old chat with the sources it cited and
  carry on, and get suggestions while you type, like a search engine's autocomplete but from your own history, plus
  related searches under the results. History can be switched off and cleared from the History tab.
- **Bookmarks** (next to Search): press *bookmark* under a source in an answer, a search result or the source viewer to
  save it, then keep your bookmarks in folders of your own. Folders nest, open and close with an arrow like a file
  system, and bookmarks (and folders) move by drag and drop or from a menu. Bookmarks are never cleared with the history.
- A fresh installation answers with 30 passages per question (at most 10 from one page or file, 20 candidates per search
  method, a 30,000-token excerpt budget), rewrites follow-up questions, searches with extra phrasings and reranks the
  candidates (the small reranking model ships with the installer). All of it is adjustable in Settings > Search.

## Install and use

Run `Perplexify-Setup-<version>-x64.exe`. It installs for the current user (no administrator needed), adds a Start menu
entry and opens Perplexify in its own window. Paste an API key to connect a model, add a documentation website, a GitHub
repository or a folder, and ask. Closing the window quits the app (it asks first while a library is still being indexed).

The window is drawn by Microsoft Edge WebView2, which Windows 11 and current Windows 10 already include; the installer
adds it when it is missing. Without it (or when you start the app with `--browser`) Perplexify opens in your default
browser instead and keeps an icon in the tray (Open, Quit): everything else works the same.

Libraries, downloaded manuals and settings live in `%LOCALAPPDATA%\Perplexify`, outside the install folder, so
upgrading or reinstalling never loses them. (Versions that were called Manual RAG kept their data in
`%LOCALAPPDATA%\ManualRAG`; the first start moves it.) Uninstalling asks whether to delete them too.

## Local models (no cloud, no key)

Choose **Local model** next to ChatGPT, Claude and the others in *Connect a model* (or in Settings > Models) and the
answers are written by a model that runs on this computer, through [llama.cpp](https://github.com/ggml-org/llama.cpp).
Nothing is sent anywhere, there is no key and no cost. Small models are quick but make more mistakes than the cloud
ones: the 7B and 8B models in the list are the most capable and need about 8 GB of free memory, the tiny ones run on
almost anything.

- **Models.** Pick a `.gguf` file you already have (it is used where it is and never copied), or download one of six
  checked models from Hugging Face (0.5B to 8B, 0.5 to 5.3 GB; the first, Qwen2.5 0.5B, is for a quick test). Downloads
  stop and resume, are verified against their published checksum, and are kept in `%LOCALAPPDATA%\Perplexify\llm`. The
  panel lists memory needs and warns when a model is probably too big for the computer.
- **Engine.** The installer ships llama.cpp (the program `llama-server.exe` and its libraries) in the folder `llama`
  next to the app, so nothing about it is downloaded when the app runs. It adds about 86 MB to the installed app and
  roughly 14 MB to the installer. It chooses the fastest CPU code for your processor when it starts and uses the graphics
  card through Vulkan when there is one with room (Settings > Models: GPU use, Automatic or Off).
- **How it runs.** The model loads when you ask the first question (a few seconds to a minute, the answer says so),
  stays loaded while you work, and is unloaded when you quit. It listens on `127.0.0.1` only, on a free port, behind a key
  made for that run, and it cannot outlive the app: Windows ends it with the app even if the app is killed.
  Its log is `%LOCALAPPDATA%\Perplexify\logs\llama-server.log`.
- **Context size.** *Automatic* picks 4,096 to 16,384 tokens from the memory of the computer; choose a larger or smaller
  one in the same panel. A small window holds fewer passages (the excerpts get half of it), whatever *Passages per
  question* says.
- **From the source.** Running with `python -m perplexify app` there is no `llama` folder yet: press *Download engine* in
  the Local model panel, or run `python -m perplexify download-llama` once. It fetches the build pinned in
  `installer/pins.json`, checks its SHA-256 and unpacks it into `%LOCALAPPDATA%\Perplexify\llama`. (`PERPLEXIFY_LLAMA_DIR`
  names another folder.)

## Work on it in VS Code

You need: VS Code, Python 3.12 (64-bit, from python.org), and for the installer
[Inno Setup 6](https://jrsoftware.org/isdl.php) (`winget install JRSoftware.InnoSetup`).

1. Open this folder in VS Code and accept the recommended extensions.
2. **Terminal > Run Task > Set up dev environment.** It creates `.venv`, installs the dependencies and runs the tests.
3. **F5** starts the app from the source, in its window ("Perplexify: app (scratch data)" keeps its data in `.dev-data`,
   so it never touches an installed copy).

The web interface is plain HTML, CSS and JavaScript in `perplexify/web`: no build step. Started with `--debug` (which
the F5 configuration does), the window accepts **Ctrl+R** to reload after you change something there and **F12** for the
inspector. Python changes need a restart (close the window and F5 again). `python -m perplexify app --browser` runs the
same page in your browser instead, which is handy next to the browser's own developer tools.

| To do this | Use |
| --- | --- |
| Run the app from source | F5, or task *Run the app (scratch data in .dev-data)* |
| Run the unit tests | task *Run unit tests* (the default test task) |
| Run one test file under the debugger | open it, then F5 on "Tests: current file" |
| Check an installation end to end | task *Run self-test* (`python -m perplexify selftest`, no network, no key) |
| Use the command line | `.venv\Scripts\python.exe -m perplexify --help` |

Common tweaks and where they live:

| To change | Edit |
| --- | --- |
| How the interface looks (colours, fonts, spacing) | `perplexify/web/style.css` (the colour tokens are at the top, one set per theme; `--r` and `--r-lg` are the corner radii) |
| The window (size, title bar colour, file dialogs) | `perplexify/desktop.py` (its `THEMES` must equal `--bg`, `--text` and `--line` in the stylesheet: a test checks it) |
| Interface behaviour | `perplexify/web/js/*.js` (`search.js` is the Search tab, `chat.js` the Ask tab, `dialogs.js` the dialogs) |
| What the model is told, how answers cite | `perplexify/rag.py` (`SYSTEM_PROMPT`, `SYSTEM_PROMPT_CODE`) |
| Defaults for passages per question, candidates, reranking | `perplexify/settings.py` (`RETRIEVAL_DEFAULTS`; a settings file that still holds an earlier default set follows the new one) |
| Chat and search history, suggestions, related searches | `perplexify/history.py` (storage and ranking), `history_api.py`, `perplexify/web/js/history.js`, `web/history.css` |
| Bookmarks: folders, saved sources, the tree | `perplexify/bookmarks.py` (storage and rules: names, depth, moves), `bookmarks_api.py`, `perplexify/web/js/bookmarks.js`, `web/bookmarks.css` |
| Providers, their default models and context windows | `perplexify/providers.py` |
| Local models: the curated download list, how llama-server is started, memory hints | `perplexify/localllm.py` (`CATALOG`, `build_args`), `perplexify/llm/local.py` (the chat client), `local_api.py` (`/api/local/...`), `web/js/local.js` and `web/local.css` (the panel) |
| Which llama.cpp build is shipped | `llama` in `installer/pins.json` (tag, zip, SHA-256; `localllm.py` keeps an equal copy, a test compares them) |
| The quick-add documentation buttons | `perplexify/presets.py` |
| The built-in embedding model | `DEFAULT_EMBEDDER` in `perplexify/settings.py` and `embedding_model` in `installer/pins.json` |
| How code is split into passages | `perplexify/chunking.py` and `perplexify/loaders/code.py` |

## Build the Windows installer

Press **Ctrl+Shift+B** (task *Build Windows installer*). The result is `dist\Perplexify-Setup-<version>-x64.exe`.

The first build downloads a few hundred MB (Python, the packages, the embedding model, the two Microsoft runtime
installers) and takes several minutes; the downloads are cached (`installer\cache`, plus pip's own cache), so later
builds mostly just repackage your changes. The staged copy needs about 300 MB of disk space and the installer is 100 to
150 MB. The build:

1. runs the unit tests,
2. stages a self-contained copy of the app in `installer\build\stage`: the pinned embeddable Python, every package in
   `requirements.txt` (exact versions from `installer\windows-constraints.txt`; the one package PyPI only ships as source
   is built from `installer\source-only-requirements.txt`, hash-checked), your `perplexify` folder, the built-in
   embedding model, the icon, and the licence notices of everything bundled,
3. runs the self-test against that staged copy with the network off (it also loads the window's libraries),
4. compiles it all with Inno Setup into one installer. The Visual C++ runtime and the small WebView2 bootstrapper ride
   along and are run only on a PC that lacks them (their Microsoft signatures are checked at build time).

Other build tasks: *fast, for testing* (quick compression, bigger file) and *from scratch* (`-Clean`).
From a terminal: `powershell -File installer\build.ps1 [-Fast] [-Clean] [-SkipTests] [-SkipSelfTest] [-Relock]`.

**Try the result** with task *Test the installer*: it installs the newest installer silently into a temporary folder,
runs the installed app (first in browser mode, then in its own window, which it finds, hands a second launch to and
closes), imports a small code folder and searches it, upgrades the installation while the app is running, uninstalls
it, and leaves nothing behind. It never touches your real data folder. (A window appears on screen for a few seconds.)

Things you will want to change, and where:

| Change | Edit |
| --- | --- |
| Version number | `perplexify/__init__.py` (`__version__`) |
| Product name, publisher | `installer/pins.json` (`app`) |
| Icon | `perplexify/icon.py` (drawn in code; no image files) |
| Add or update a dependency | `requirements.txt`, then `installer\build.ps1 -Relock` |
| Python version | `installer/pins.json` (`python`: version, url, sha256), then rebuild with `-Clean` |
| Installer pages, shortcuts, options | `installer/Perplexify.iss` |

More detail, including silent-install switches and code signing, is in [installer/README.md](installer/README.md).

## Where things are

```
perplexify/            the app (Python)
  web/                the user interface: index.html, style.css, js/*.js
  loaders/            HTML, PDF, text and code readers (tree-sitter for code)
  llm/                Claude, OpenAI-compatible (ChatGPT, DeepSeek...), Gemini and local (llama.cpp) clients
  embeddings/         built-in (fastembed) and cloud embedding back ends
  retrieval.py        meaning + keyword search, fusion, optional reranking
  rag.py              prompts, excerpt packing, citations
  history.py          saved chats (with their sources) and searches, suggestions, related queries (history.db)
  bookmarks.py        bookmarked sources in nested folders (bookmarks.db)
  localllm.py         local models: finding and downloading the llama.cpp engine, running it as a child process, model files and downloads
  local_api.py        the web API for local models (/api/local/...)
  server.py           the local web API (127.0.0.1 only)
  desktop.py          the app's own window (pywebview + WebView2): size, title bar, native dialogs
  app.py              starting and stopping: the window, or the browser + tray.py when no window is possible
  cli.py              the command line
tests/                unit tests (python -m unittest discover -s tests)
installer/            Windows installer: build.ps1, Perplexify.iss, pins.json, test-install.ps1
scripts/              setup-dev.ps1
.vscode/              tasks (build, test, run), debug configurations
```

## Command line and MCP

Installed, the bundled Python runs the same command line the source uses:

```
"%LOCALAPPDATA%\Programs\Perplexify\runtime\python.exe" -m perplexify --help
```

`add-docs`, `add-repo`, `list`, `search`, `ask`, `connect`, `app`, `serve`, `stop`, `selftest` and `download-llama` are
available. (`connect local --model <file.gguf>` uses a model file as the local model; `download-llama` fetches the
engine for local models when you run from the source.)
(`app` is what the Start menu shortcut runs: add `--browser` to use the browser instead of the window, `--debug` for the
inspector and reload shortcuts, `--port N` for another port.)
`python -m perplexify mcp` runs a stdio MCP server, so tools that speak MCP can search your libraries. Point such a tool
at the bundled `runtime\python.exe` with the arguments `-P -m perplexify mcp`. (It has not been tried against specific
MCP clients yet.)

## Troubleshooting

- **Windows says "unknown publisher" when you run the installer.** The installer is not code-signed. Sign it (see
  `installer/README.md`) or choose *More info > Run anyway*.
- **The build says Python 3.12 or Inno Setup is missing.** Install them as described above; the message names the
  exact command.
- **The app does not start.** Look in `%LOCALAPPDATA%\Perplexify\logs`: `perplexify.log` for normal logging and
  `crash.log` for a start-up error (a message box shows the same error when it happens).
- **Perplexify opens in the browser instead of its own window.** The Microsoft Edge WebView2 Runtime is missing or
  could not load; `perplexify.log` says which. Install it from <https://developer.microsoft.com/microsoft-edge/webview2>
  (Evergreen Bootstrapper) and start the app again. `python -m perplexify selftest` reports the window check too.
- **Using it offline.** Everything except a cloud model works offline, including search and local models; the
  built-in embedding model and the llama.cpp engine ship inside the installer (a local model's own file must be on the
  computer first).
- **A local model does not load or is slow.** The panel says why (not enough memory, a model type the bundled llama.cpp
  does not know, a damaged download). `%LOCALAPPDATA%\Perplexify\logs\llama-server.log` has the engine's own messages. Try
  a smaller model or a smaller context size, or switch *GPU use* to Off if the graphics driver misbehaves. Answers
  start after the whole prompt has been read, which on a CPU takes long for a big context size.

## macOS installer (built on GitHub)

A Mac cannot be built from a Windows PC, so the macOS app is built by a GitHub Actions workflow,
`.github/workflows/macos-installer.yml`:

1. Push this project to GitHub.
2. Open the repository's **Actions** tab, choose **macOS installer**, then **Run workflow**. (Pushing a tag such as
   `v0.1.0` starts it too and attaches the DMGs to that release.)
3. When it finishes, download the artifact `Perplexify-macos-arm64` (Apple Silicon) and, if it built,
   `Perplexify-macos-x64` (Intel). Each holds a `.dmg` and its checksum.

The workflow builds `Perplexify.app` with PyInstaller (`installer/macos/Perplexify.spec`), puts the embedding models and
llama.cpp (the local-model engine, checked against `installer/pins.json`) inside it, runs the app's self-test and a web
server check on the finished bundle, and only then makes the disk image. Without signing secrets the app is signed ad hoc:
on first start macOS says it cannot verify the developer; open *System Settings > Privacy & Security* and choose
*Open Anyway*. Signing with a Developer ID and notarization are built in; see `installer/README.md`.

This workflow has not been run yet (there is no Mac here): expect to fix a thing or two on its first run, and read the
log of the first failing step.
