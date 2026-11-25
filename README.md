# Perplexify

Perplexify lets you ask questions about documentation and code that's much bigger than a model's context window. You point it at docs (websites, PDFs, Markdown, folders) or code (GitHub repos or folders) and it builds a search index on your computer. When you ask something, it finds the few passages that matter, sends only those to the model you picked, and answers with citations you can open. The model can be ChatGPT, Claude, Gemini, DeepSeek, anything OpenAI-compatible, or a local model running on your own machine. There's also a Search tab that works with no model at all.

Everything is indexed locally, and the built-in embedding model works offline with no key. Libraries are kept separate, so you tick whichever ones you want for each question. Chats and searches are saved in the History tab (you can turn that off or clear it), and bookmarks go in folders you make yourself. The retrieval defaults, like 30 passages per question, query rewriting and reranking, can be changed in Settings > Search.

## Install

Run `Perplexify-Setup-<version>-x64.exe`. It needs 64-bit Windows 10 or 11 and installs for the current user, so no admin rights. Paste an API key (or pick a local model), add a docs site, repo or folder, and ask.

The window runs on Microsoft Edge WebView2, and the installer adds it if it's missing. If it isn't there, or you start the app with `--browser`, Perplexify opens in your default browser and keeps an icon in the tray instead.

Your data lives in `%LOCALAPPDATA%\Perplexify`, outside the install folder, so upgrading never touches it. API keys are encrypted with Windows DPAPI (the Keychain on a Mac). If that fails, the key isn't saved.

## Local models

Pick Local model when you connect and the answers come from a model running on your computer through [llama.cpp](https://github.com/ggml-org/llama.cpp). There's no key, and nothing leaves your machine. Small models make more mistakes than the cloud ones, and the 7B and 8B options need about 8 GB of free memory.

You can use a `.gguf` file you already have (it stays where it is) or download one of the six in the list. Downloads can be stopped and resumed, and they're checked against their published checksum. The installer includes llama.cpp, and it uses your GPU through Vulkan when there's one with enough room (Settings > Models > GPU use). The model loads when you ask your first question and unloads when you quit. It only listens on 127.0.0.1, behind a key that's made fresh every run, and its log is `%LOCALAPPDATA%\Perplexify\logs\llama-server.log`.

Running from source there's no engine yet. Press Download engine in the Local model panel, or run `python -m perplexify download-llama`.

## Research mode

The Research switch next to the question box lets the model search the web as well. It works with no key and no setup. Perplexify asks Wikipedia, PubMed, arXiv, OpenAlex, Stack Overflow, GitHub and DuckDuckGo, keeps what's actually on topic, and the model cites it. Papers come with their abstracts, and Stack Overflow results come with their best answer.

Each of those services asks to be used at a certain pace, and Perplexify sticks to it. If one refuses (DuckDuckGo sometimes does), it's left alone for a while and the others still work. You can switch any source off in Settings > Search, and the Test web search button there shows which ones answered. A free Tavily or Brave Search key is optional. If you add one it's used first and gives you regular web results, and the open sources step in if that service fails.

## How the models are prompted

Claude and ChatGPT (and Gemini, in Research mode) get prompts written for data science and biomedical engineering research. They're allowed to add their own knowledge to what the sources say and to give build and engineering advice, as long as they mark which is which. Every other model gets the stricter prompts that stick to the excerpts. The prompts are in `rag.py`.

## Dictation

The microphone button in the search boxes types what you say. The recognition runs on your computer (Whisper, through faster-whisper), so it needs no internet and no key, and the audio never leaves. The installer includes the model. Running from source, the first press downloads it (about 145 MB). It stops listening after a short pause or when you press the button again, and it uses your system language.

## What leaves your computer

Your libraries, history, bookmarks and search index stay on your computer. When you ask a question, the model you connected gets the question, the few excerpts that matter and the recent conversation, and nothing else. With a local model nothing is sent anywhere. If you pick a cloud embedding model for a library, its passages go to that provider while it's indexed, and the built-in model sends nothing. If you turn on Research, the searches the model writes (not your documents) go to the sources you left on, or to Tavily or Brave if you added a key.

## Development

You need Python 3.12 (64-bit), plus Inno Setup 6 if you're building the installer. Open the folder in VS Code, run the task Set up dev environment (it creates `.venv`, installs the dependencies and runs the tests), then press F5. The "scratch data" launch configuration keeps its data in `.dev-data`, so it never touches an installed copy.

The interface is plain HTML, CSS and JS in `perplexify/web`, with no build step. When the app is started with `--debug` (F5 does that), Ctrl+R reloads the window after you change something there and F12 opens the inspector. Python changes need a restart. `python -m perplexify app --browser` shows the same page in your browser, which is handy for the browser's own dev tools.

Run the tests with the Run unit tests task or `python -m unittest discover -s tests`. `python -m perplexify selftest` checks an install end to end, offline and with no key.

```
perplexify/            the app
  web/                 the interface (index.html, style.css, js/)
  loaders/             HTML, PDF, text and code readers
  llm/                 model clients (Claude, OpenAI-compatible, Gemini, local)
  embeddings/          built-in and cloud embeddings
  retrieval.py         hybrid search, fusion, reranking
  rag.py               prompts, excerpt packing, citations
  websearch.py         Research mode: planning, reading pages
  opensources.py       the open sources Research searches
  dictation.py         speech to text
  localllm.py          local models: catalog, llama.cpp, downloads
  history.py           chats and searches
  bookmarks.py         bookmarks
  server.py            the local web API (127.0.0.1 only)
  desktop.py           the native window (pywebview + WebView2)
  app.py, cli.py       startup and the command line
tests/                 unit tests
installer/             the Windows and macOS installer builds
.vscode/               tasks and debug configs
```

Colors and spacing are in `web/style.css`, and the window is in `desktop.py` (its `THEMES` have to match the stylesheet, and a test checks that). Retrieval defaults are in `settings.py`, providers in `providers.py`, and the llama.cpp version in `installer/pins.json`.

## Building the installer

Press Ctrl+Shift+B (the Build Windows installer task). The result is `dist\Perplexify-Setup-<version>-x64.exe`, which is roughly 400 MB because the models and llama.cpp are inside.

The first build downloads a few hundred MB (Python, the packages, the models and the Microsoft runtimes) and takes several minutes. Downloads are cached in `installer\cache`, so later builds mostly just repackage your changes. The build runs the tests, stages a self-contained copy in `installer\build\stage`, self-tests that copy offline, and then compiles it with Inno Setup. From a terminal it's `powershell -File installer\build.ps1 [-Fast] [-Clean] [-SkipTests] [-SkipSelfTest] [-Relock]`.

The Test the installer task installs the newest build into a temp folder, runs it, upgrades it, uninstalls it and cleans up after itself. A window flashes on screen while it runs.

The version is in `perplexify/__init__.py`, and the name and publisher are in `installer/pins.json`. Dependencies go in `requirements.txt` (then run `installer\build.ps1 -Relock`). The Python version is in `pins.json` too, and changing it needs a rebuild with `-Clean`. Installer pages and shortcuts are in `installer/Perplexify.iss`. Silent installs and code signing are covered in [installer/README.md](installer/README.md).

## macOS

I can't build a Mac version from Windows, so GitHub Actions does it (`.github/workflows/macos-installer.yml`). Run the macOS installer workflow from the Actions tab, or push a tag like `v0.1.0`, and download the `.dmg`. The app is signed ad hoc unless you add signing secrets, so macOS will ask you to approve it once under System Settings > Privacy & Security > Open Anyway. The workflow also writes the oldest macOS the app really supports into the app, and the app asks for microphone access the first time you use dictation. I haven't been able to run any of this on a Mac yet, so expect to fix a thing or two the first time.

## Command line and MCP

The installed app has the same command line as the source:

```
"%LOCALAPPDATA%\Programs\Perplexify\runtime\python.exe" -m perplexify --help
```

The commands are `app`, `serve`, `connect`, `add-docs`, `add-repo`, `list`, `search`, `ask`, `download-model`, `download-llama`, `selftest`, `mcp`, `stop` and `version`. `app` takes `--browser`, `--debug` and `--port N`.

`python -m perplexify mcp` runs a stdio MCP server, so other tools can search your libraries and use web search. Point the tool at `runtime\python.exe` with the arguments `-P -m perplexify mcp`. I haven't tried it with specific MCP clients yet.

## Troubleshooting

- Windows says "unknown publisher" when you run the installer. The installer isn't code-signed. Click More info, then Run anyway.
- The app won't start. Check `%LOCALAPPDATA%\Perplexify\logs`. `perplexify.log` has the normal log and `crash.log` has startup errors.
- It opens in a browser instead of its own window. WebView2 is missing or failed to load. Install the [Evergreen runtime](https://developer.microsoft.com/microsoft-edge/webview2), and `selftest` will tell you what it found.
- A local model won't load or is slow. The panel says why. Try a smaller model or context size, or set GPU use to Off.
- Dictation says the microphone was refused. Allow desktop apps under Windows Settings > Privacy > Microphone, then press the microphone again.
- Research finds very little. DuckDuckGo refuses automated searches now and then. Test web search in Settings > Search shows which sources answered, and a free Tavily or Brave key adds regular web results.

## License

MIT, see `LICENSE`.
