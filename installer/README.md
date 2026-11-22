# The Windows installer

`build.ps1` turns the repository into `dist\Perplexify-Setup-<version>-x64.exe`. Run it with Ctrl+Shift+B in VS Code or
`powershell -File installer\build.ps1` in a terminal.

## What ends up on the user's PC

```
<install folder>                       %LOCALAPPDATA%\Programs\Perplexify  (or Program Files for "all users")
  runtime\                             the python.org embeddable Python 3.12 + Lib\site-packages (all dependencies)
  app\perplexify\                       the app's own files (what you edit in this repository)
  models\                              the built-in embedding model; copied to the user's data folder on first use
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
| Notices | `scripts\make_notices.py` collects the licence text of every bundled package |
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
- **Name and publisher**: `app` in `pins.json`.
- **Installer wizard, shortcuts, tasks, uninstall behaviour**: `Perplexify.iss`.
- **AppId** in `Perplexify.iss` identifies the product for upgrades and uninstalling. Never change it once you have
  shipped a version, and never reuse it for another product.

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

## Limits worth knowing

- 64-bit Windows 10 or 11 only (also ARM64 PCs through Windows' x64 emulation; that combination has not been tried).
- The window needs the WebView2 Runtime and .NET Framework 4.8 (both part of current Windows; pythonnet drives the
  window through the built-in .NET Framework, so no separate .NET install is needed). Without them the app runs in
  the browser.
- Right-click menus (copy, paste) are switched off in the window outside `--debug` mode; the keyboard shortcuts
  (Ctrl+C, Ctrl+V, Ctrl+A) work. The page's stored settings (theme, ticked libraries) belong to its address, so the app
  keeps using the port it used last time.
- Size and time: about 11,000 files and 306 MB once installed, a 128 MB installer. Installing takes a minute or so
  on a PC with real-time virus scanning, a build with warm caches about four minutes.
- No automatic updates: users run a newer installer.
- Reranking (an optional setting) downloads a small model the first time it is switched on; everything else needed
  works offline.
- Long paths: the deepest file is about 175 characters below the install folder, so the install folder itself should
  stay under roughly 80 characters.
