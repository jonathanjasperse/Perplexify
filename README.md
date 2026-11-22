# Perplexify

Ask questions about manuals and code repositories that are far larger than any model's context window.

Perplexify downloads or reads your documentation (websites, PDFs, Markdown, folders) and source code (GitHub
repositories or folders), cuts it into passages, and builds a search index on your computer. When you ask a question
it finds the few passages that matter, hands only those to the cloud model you chose (ChatGPT, Claude, Gemini,
DeepSeek or any OpenAI-compatible service), and shows the answer with citations you can open.
You can also **search without any model**: the Search tab combines meaning search and keyword search over the same index.

- Everything is indexed locally. The built-in embedding model needs no key and works offline.
- Only your question and the few retrieved excerpts leave the computer, and only to the provider you connected.
- Libraries are separate: tick one, several or all of them in the sidebar for each question or search.

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
| Defaults for passages per question, candidates, reranking | `perplexify/settings.py` (`RETRIEVAL_DEFAULTS`) |
| Providers, their default models and context windows | `perplexify/providers.py` |
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
  llm/                Claude, OpenAI-compatible (ChatGPT, DeepSeek...) and Gemini clients
  embeddings/         built-in (fastembed) and cloud embedding back ends
  retrieval.py        meaning + keyword search, fusion, optional reranking
  rag.py              prompts, excerpt packing, citations
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

`add-docs`, `add-repo`, `list`, `search`, `ask`, `connect`, `app`, `serve`, `stop` and `selftest` are available.
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
- **Using it offline.** Everything except the cloud model works offline, including search; the built-in embedding
  model ships inside the installer.

The macOS installer (a DMG built by a GitHub Actions workflow) is not part of this repository yet.
