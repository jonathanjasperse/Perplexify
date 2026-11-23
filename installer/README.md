# The Windows installer

`build.ps1` turns the repository into `dist\Perplexify-Setup-<version>-x64.exe`. Run it with Ctrl+Shift+B in VS Code or
`powershell -File installer\build.ps1` in a terminal.

## What ends up on the user's PC

```
<install folder>                       %LOCALAPPDATA%\Programs\Perplexify  (or Program Files for "all users")
  runtime\                             the python.org embeddable Python 3.12 + Lib\site-packages (all dependencies)
  app\perplexify\                       the app's own files (what you edit in this repository)
  models\                              the built-in embedding model and the reranking model (reranking is on by default);
                                       copied to the user's data folder on first use
  llama\                               llama.cpp, the engine for local models: llama-server.exe and its DLLs (see "Local models")
  app.ico                              the icon of the shortcuts
  THIRD-PARTY-NOTICES.txt              licences of everything bundled
%LOCALAPPDATA%\Perplexify\              user data: libraries, settings (API keys encrypted with Windows DPAPI), logs
```

The Start menu shortcut runs `runtime\pythonw.exe -X utf8 -P -m perplexify app` (python.org's windowless interpreter,
with the Perplexify icon on the shortcut). There is deliberately no custom launcher `.exe`: the only programs in the
install are python.org's signed interpreters and Inno Setup's uninstaller, so antivirus heuristics have nothing
unfamiliar to object to. The app opens in **its own window** (pywebview hosting Microsoft Edge WebView2); closing the
window quits the app. The shortcut carries the app id `Perplexify.Desktop`, the same one the process sets
(`APP_USER_MODEL_ID` in `perplexify\desktop.py`), so the taskbar button, pinning and the shortcut belong together.
Task Manager lists the running app as "Python" with a few "Microsoft Edge WebView2" helper processes. When no window is
possible the app opens the browser and shows a tray icon whose menu has Quit.

Nothing is written into the install folder while the app runs, so "all users" installs under Program Files work.
Two Microsoft runtimes are installed only when the PC lacks them, each with Windows' own administrator prompt:

- the **Visual C++ runtime** (needed by numpy and onnxruntime) when the PC has none or an older one than
  `vc_redist.min_version` in `pins.json`;
- the **WebView2 Runtime** (draws the window), found by the `pv` value under
  `Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}`, the same place `desktop.py` looks.
  Windows 11 and current Windows 10 have it already. The 2 MB Evergreen *bootstrapper* in the installer downloads the
  runtime (about 150 MB, so it needs the internet at that moment). If the prompt is declined it tries a per-user
  install; if that fails too, the last page of the wizard says so and the app simply opens in the browser.

## How the build works

| Step | Detail |
| --- | --- |
| Tools | finds Inno Setup 6 and a Python 3.12 |
| Tests | `python -m unittest discover -s tests` in `.venv` |
| Runtime | downloads the embeddable Python named in `pins.json`, checks its SHA-256 **and** its Authenticode signature, writes `python312._pth`, builds wheels for the source-only packages listed (with hashes) in `source-only-requirements.txt` into `cache\wheels`, then installs `requirements.txt` into `runtime\Lib\site-packages` with `pip --only-binary=:all: --platform win_amd64` (so no compilers are needed), using `windows-constraints.txt` for exact versions |
| App | mirrors `perplexify\` into `stage\app\perplexify` |
| Icon | `python -m perplexify.icon --ico` draws the icon (no image files are stored in the repository) |
| Model | `python -m perplexify download-model` fetches the embedding model once into `cache\`, then copies it into the stage |
| Engine | `python -m perplexify download-llama --pins installer\pins.json` fetches the llama.cpp build pinned in `pins.json` (`llama`: tag, URL, size, SHA-256) into `cache\downloads`, checks the SHA-256 (a mismatch deletes the file and stops the build), unpacks it, removes what the server does not need, starts `llama-server.exe --version` once, and keeps the result in `cache\llama-<tag>`; that is mirrored into `stage\llama` |
| Notices | `scripts\make_notices.py` collects the licence text of every bundled package, and of llama.cpp (`licenses\llama.cpp.txt` plus the licence files in `stage\llama`) |
| Compile | `compileall` byte-compiles everything so the first start is fast and the install folder can be read-only |
| Self-test | `python -m perplexify selftest` against the staged copy with `HF_HUB_OFFLINE=1`: import, keyword and meaning search, the web app, and that the window's libraries (pywebview, pythonnet, WebView2 control) load |
| VC++ runtime | downloads `vc_redist.x64.exe` and checks its Microsoft signature |
| WebView2 bootstrapper | downloads `MicrosoftEdgeWebview2Setup.exe` (`webview2` in `pins.json`) and checks its Microsoft signature |
| Installer | Inno Setup compiles `Perplexify.iss` |

Downloads are cached in `cache\`; `build\` holds the staged app. Both are ignored by git. The staged Python and
packages are reused until `requirements.txt`, `windows-constraints.txt` or the pinned Python changes.

Switches: `-Fast` (quick compression), `-Clean` (start from nothing), `-SkipTests`, `-SkipSelfTest`,
`-Relock` (rewrite `windows-constraints.txt` from the packages installed in `.venv`), `-SignCommand`.

## Changing things

- **Python version**: put the new version, download URL and SHA-256 into `pins.json`, rebuild with `-Clean`. Python
  does not publish checksums on its download pages for recent releases; compute it with
  `Get-FileHash <zip> -Algorithm SHA256` after downloading over HTTPS. The build also verifies the Authenticode
  signature of `python.exe` and the Python DLL, so a tampered download cannot pass.
- **Dependencies**: edit `requirements.txt`, then run `build.ps1 -Relock` in an environment where the tests pass. Every
  dependency needs a prebuilt Windows wheel for Python 3.12 (`cp312`, `win_amd64`). The exception is a tiny pure-Python
  package that PyPI publishes only as source (today `proxy_tools`, which pywebview needs): add it, pinned with the
  sha256 of its source archive, to `source-only-requirements.txt` and the build makes its wheel.
- **Embedding model**: `embedding_model` in `pins.json` (it must also be the model the app uses by default,
  `DEFAULT_EMBEDDER` in `perplexify/settings.py`).
- **llama.cpp version**: the `llama` block of `pins.json` (tag, asset name, URL, size, SHA-256 from the release's asset
  list on GitHub), plus the equal copy `EMBEDDED_PIN` in `perplexify/localllm.py` (the installed app has no `installer`
  folder; `tests/test_local.py` fails when the two differ). Then run the unit tests and check the new engine by hand as
  described under "Local models" below. Rebuild: the cache folder is named after the tag, so the new build is fetched.
- **Name and publisher**: `app` in `pins.json`.
- **Installer wizard, shortcuts, tasks, uninstall behaviour**: `Perplexify.iss`.
- **AppId** in `Perplexify.iss` identifies the product for upgrades and uninstalling. Never change it once you have
  shipped a version, and never reuse it for another product.

## Local models (llama.cpp)

Local models run through `llama-server.exe` from the official llama.cpp release. The installer ships it in `<install
folder>\llama`; the app never downloads it by itself (a developer running from the source can press *Download engine* or run
`python -m perplexify download-llama`, which fetch the same pinned build into `%LOCALAPPDATA%\Perplexify\llama`). The app looks
for it, in this order, in `PERPLEXIFY_LLAMA_DIR`, in `llama` next to the app, and in `<data folder>\llama`.

- **Which build.** `v1.0.0`, the release llama.cpp itself calls stable (it has no files of its own; its `nightly-tag.txt` names
  build `b12359`), asset `llama-b12359-bin-win-vulkan-x64.zip`, 38,090,643 bytes, SHA-256 `e8d18c6e...a0d5e` (the digest GitHub
  shows for the asset; pinned in `pins.json` and checked after every download). The binaries report `version: 1.0.0-dev (build
  12359, commit b1b770d90)`, the commit the tag points at.
- **Why the Vulkan build.** One program and no graphics-card detection of our own: this zip contains the CPU back end in
  fourteen variants (the engine picks the one that fits the processor when it starts: a plain x64 build up to ones for AVX-512 and newer processors) and
  `ggml-vulkan.dll`, which uses any graphics card with a Vulkan driver (NVIDIA, AMD, Intel) and quietly does nothing on a PC
  without one. Measured: the CPU-only zip is 25.1 MB, the Vulkan zip 38.1 MB (13.0 MB more); after the build drops the
  command line tools the engine needs 86 MB on disk (the Vulkan DLL is 42 MB of it); a solid LZMA run over those files, the way
  Inno Setup compresses, gives about 14 MB in the installer, of which the Vulkan part is only about 4 MB (an estimate: the
  real figure is the difference between two installer builds). Prompts built from 20 or more passages are thousands of
  tokens, and reading them is where a graphics card helps most, so it is worth it. (CUDA builds are 180 to 430 MB.)
- **What the build keeps.** Only `llama-server.exe`, the DLLs it imports (found by reading the import tables, so a future build
  cannot lose one by accident), the `ggml*.dll` back ends and the licence files. The command line tools in the zip are removed.
  `llama-server.exe --version` is run once at build time and again by `test-install.ps1` and the self-test.
- **Licences.** llama.cpp is MIT (Copyright The ggml authors); the zip carries no licence file for it, so
  `installer\licenses\llama.cpp.txt` has the texts (llama.cpp, cpp-httplib, nlohmann/json, copied from the source at tag
  b12359, with a note on the public-domain headers); the LLVM OpenMP licence in the zip (`LICENSE-LLVM-OpenMP`) is kept next to
  `libomp.dll`. `make_notices.py` puts all of it into `THIRD-PARTY-NOTICES.txt`.
- **Process.** The app starts one `llama-server.exe` on demand (first question), bound to 127.0.0.1 on a free port behind a
  random per-run key, without a console window, at below-normal priority, with its output in
  `%LOCALAPPDATA%\Perplexify\logs\llama-server.log`. It lives in a Windows job object with `KILL_ON_JOB_CLOSE` held by the app,
  so it ends with the app however the app ends (`/api/shutdown`, Ctrl+Break in a terminal and an app killed from the task manager
  were each tried with the real engine; closing the window ends in the same shutdown). A small file per engine in `%LOCALAPPDATA%\Perplexify\engines` lets the next start end one that
  somehow survived.
- **Updating by hand.** After changing the pin, run `python -m perplexify download-llama --dest <folder>` and start the
  server with a small model at `-lv 4`: the app asks for log level 4 because it is the only level at which this build names the
  graphics card and the number of offloaded layers, so confirm that a newer build still does that and that it does not write
  prompts or answers to its log at that level.
- **Unsigned.** The llama.cpp release binaries are not code-signed. They are integrity-checked by the pinned SHA-256 only.

## Installing silently

```
Perplexify-Setup-0.1.0-x64.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

Useful switches: `/CURRENTUSER` or `/ALLUSERS` (the latter needs administrator rights), `/DIR="C:\Apps\Perplexify"`,
`/TASKS="desktopicon"` (`/TASKS=""` for none), `/LOG="setup.log"`. On a PC without the WebView2 Runtime even a
silent install shows Windows' administrator prompt for it.
Uninstall with `unins000.exe /VERYSILENT`; user data is kept unless you add `/PURGE=1`.
Installing a newer version over an older one upgrades in place and asks a running copy to quit first.

## Testing it

`test-install.ps1` (VS Code task *Test the installer*) installs the newest build into a temporary folder, runs the
installed program (offline self-test, a start in browser mode without a window, importing a code folder, searching it),
then starts it the way the shortcut does, in its own window: it finds the window, hands it a second launch, closes it
with its close button and checks that the app quit (these window checks are skipped, with a note, on a PC without the
WebView2 Runtime). It upgrades the installation while the app runs (with its window open), uninstalls it, then installs
again with the desktop icon and uninstalls with `/PURGE=1`, checking along the way that user data is kept by default and
removed only on request, that an old "start when I sign in" entry of the earlier Manual RAG is removed, and that a
purge also removes the old `ManualRAG` data folder. It removes every trace. The app itself uses a temporary data
folder; the keep/purge checks plant canaries in `%LOCALAPPDATA%\Perplexify` and `%LOCALAPPDATA%\ManualRAG` only when
those folders do not exist (otherwise they are skipped, to protect real data). It refuses to run when Perplexify is
already installed on the PC. `-KeepInstalled` leaves the test installation in place so you can look around.

## Code signing

Without a signature, Windows SmartScreen warns about an unknown publisher until the file has built up download
reputation. To sign (you need a code-signing certificate and the Windows SDK's `signtool.exe`):

```
powershell -File installer\build.ps1 -SignCommand 'signtool sign /fd sha256 /td sha256 /tr http://timestamp.digicert.com /a $f'
```

`$f` stands for the file being signed; Inno Setup signs the installer and the uninstaller. This path has not been
exercised in this repository because no certificate was available.

## The macOS app (GitHub Actions)

`.github/workflows/macos-installer.yml` builds `dist/Perplexify-<version>-macos-<arm64|x64>.dmg` on GitHub's Mac runners
(run it from the Actions tab, or push a `v*` tag). It does not need your PC. Steps: install the packages, run the unit
tests (informational until they have been seen passing on a Mac), download the models (`pins.json`), fetch llama.cpp
(`installer/macos/fetch_llama.py`, SHA-256 from `pins.json`), build `Perplexify.app` with PyInstaller
(`installer/macos/Perplexify.spec`, entry point `installer/macos/entry.py`), copy `models` and `llama` into
`Contents/Resources`, sign, run the app's own `selftest` plus a web-server and engine check **on the finished bundle**,
make the DMG, optionally notarize it, upload it.

Signing is optional. Add these repository secrets (Settings > Secrets and variables > Actions) and the next run signs
with your Apple Developer ID and notarizes, so the app opens without a warning:

| Secret | What it is |
| --- | --- |
| `MACOS_CERTIFICATE_P12_BASE64` | your "Developer ID Application" certificate exported as .p12, then base64-encoded |
| `MACOS_CERTIFICATE_PASSWORD` | the password you gave the .p12 |
| `MACOS_SIGN_IDENTITY` | e.g. `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | for notarization (an app-specific password from appleid.apple.com) |

Without them the app is signed ad hoc, which Apple Silicon requires, and the user approves it once under *System
Settings > Privacy & Security > Open Anyway*. To update llama.cpp, change the tag, asset names and hashes in `pins.json`
(`llama` for Windows, `llama_macos` for the Mac; a test checks that both use the same tag). Nothing in this workflow has
run yet, because it needs a Mac: the first run may need small fixes (PyInstaller sometimes needs another `collect_all`
line for a package that loads files by name; the spec lists them in one place). The Intel build is marked experimental.

## Limits worth knowing

- 64-bit Windows 10 or 11 only (also ARM64 PCs through Windows' x64 emulation; that combination has not been tried).
- The window needs the WebView2 Runtime and .NET Framework 4.8 (both part of current Windows; pythonnet drives the
  window through the built-in .NET Framework, so no separate .NET install is needed). Without them the app runs in
  the browser.
- Right-click menus (copy, paste) are switched off in the window outside `--debug` mode; the keyboard shortcuts
  (Ctrl+C, Ctrl+V, Ctrl+A) work. The page's stored settings (theme, ticked libraries) belong to its address, so the app
  keeps using the port it used last time.
- Size and time: about 11,200 files and 485 MB once installed (llama.cpp with its graphics-card back end is 86 MB of that,
  the two built-in models about 90 MB). The `-Fast` test build is 260 MB; the normal build compresses harder and is smaller
  (the last lines of the build say how big). A build with warm caches takes about four minutes.
- No automatic updates: users run a newer installer.
- Everything needed works offline, including reranking and local models once a model file (.gguf) is on the PC: the models
  and the engine ship in the installer; only the language-model files themselves are chosen or downloaded by the user.
- Long paths: the deepest file is about 175 characters below the install folder, so the install folder itself should
  stay under roughly 80 characters.
