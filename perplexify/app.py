"""The desktop app: starts the local server and shows the interface in the app's own window.

The interface is a web page served by the app itself on 127.0.0.1. On Windows and macOS it is shown in a native
window (see desktop.py), so Perplexify behaves like any other program: its own window, icon and taskbar entry, native
file dialogs, closing the window quits the app. Where a window is not available (Linux, a PC without the WebView2
Runtime, `--browser`) the page opens in the default browser instead and a tray icon keeps the app running.

Only one copy runs at a time: starting the app again brings the running window to the front.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser

import httpx

from . import __version__, desktop, osutil, paths

log = logging.getLogger("perplexify")

# After a window has been used the .NET / Cocoa threads behind it must never keep a finished app alive: when the app
# is done and the process is still there after this many seconds, it is ended. 0 switches that off (tests do).
EXIT_WATCHDOG = 10.0


def setup_logging() -> None:
    log_file = paths.logs_dir() / "perplexify.log"
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3,
                                                                             encoding="utf-8")]
    # a windowless pythonw.exe has no stdout/stderr; libraries that print progress would crash on None
    if sys.stderr is None or sys.stdout is None:
        stream = open(log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    try:
        import truststore

        truststore.inject_into_ssl()  # use the operating system's certificate store (corporate proxies)
    except Exception:  # noqa: BLE001
        pass


def _probe(port: int) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1.5)
        return r.status_code == 200 and r.json().get("app") == "perplexify"
    except (httpx.HTTPError, ValueError):
        return False


DEFAULT_PORT = 8765


def _choose_port(explicit: int | None, configured: int | None, remembered: int | None) -> int:
    """The port to try first. The page's address is what its stored settings (theme, ticked libraries) belong to, so the app
    keeps the port it used last time while that is free, instead of going back to the default and losing them."""
    if explicit:
        return int(explicit)                       # --port
    if configured and int(configured) != DEFAULT_PORT:
        return int(configured)                     # chosen in Settings
    return int(remembered or configured or DEFAULT_PORT)


def _free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def running_instance() -> int | None:
    """Port of a Perplexify server that is already running for this user, if any."""
    try:
        port = json.loads(paths.run_info_path(create=False).read_text("utf-8")).get("port")
    except (OSError, ValueError):
        port = None
    for candidate in [port] if port else []:
        if _probe(int(candidate)):
            return int(candidate)
    return None


def stop_running(timeout: float = 20.0) -> int:
    """`perplexify stop`: ask a running copy of the app to quit and wait until its process has gone.

    The Windows installer calls this before it replaces or removes files. Returns 0 when nothing was running or
    the app stopped, 1 when it did not stop in time."""
    try:
        info = json.loads(paths.run_info_path(create=False).read_text("utf-8"))
        port, pid = int(info["port"]), int(info.get("pid") or 0)
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    if not _probe(port):
        return 0  # a leftover file from a crash, or nothing is running
    try:
        httpx.post(f"http://127.0.0.1:{port}/api/shutdown", timeout=5)
    except httpx.HTTPError:
        pass  # the connection can drop while the server shuts down
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _probe(port) and (not pid or not osutil.pid_alive(pid)):
            return 0
        time.sleep(0.2)
    print("Perplexify did not stop in time.", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------- what the server talks to
class BrowserHost:
    """The page is open in the default browser: there is no window of ours, so these fall back to the browser and the OS."""

    mode = "browser"

    def __init__(self, url: str) -> None:
        self.url = url

    def show(self) -> None:
        webbrowser.open(self.url)

    def set_theme(self, theme: str) -> None:
        pass

    def pick_folder(self, title: str = "Choose a folder") -> str | None:
        return osutil.pick_folder(title)

    def pick_files(self, title: str = "Choose files") -> list[str]:
        return osutil.pick_files(title)


class HostSwitch:
    """The server holds one of these; the app points it at the browser if the native window cannot be opened."""

    def __init__(self, target) -> None:
        self.target = target

    @property
    def mode(self) -> str:
        return self.target.mode

    def __getattr__(self, name: str):
        return getattr(self.target, name)


def _show_running(port: int) -> None:
    """A second launch: bring the running window to the front (or open its page when it lives in a browser)."""
    try:
        if httpx.post(f"http://127.0.0.1:{port}/api/window/show", timeout=5).status_code == 200:
            return
    except httpx.HTTPError:
        pass
    webbrowser.open(f"http://127.0.0.1:{port}")


# --------------------------------------------------------------------------- the app
def run_app(*, window: bool = True, open_browser: bool = True, use_tray: bool = True, port: int | None = None,
            debug: bool = False) -> int:
    import uvicorn

    setup_logging()
    existing = running_instance()
    if existing:
        _show_running(existing)
        return 0
    if not osutil.acquire_single_instance():
        return 0  # another copy is starting up right now

    from .context import AppContext
    from .server import create_app

    ctx = AppContext()
    port = _free_port(_choose_port(port, ctx.settings.get()["server"].get("port"), desktop.load_state().get("port")))
    url = f"http://127.0.0.1:{port}"
    stop = threading.Event()
    server_holder: dict = {}

    native = None
    if window:
        possible, why = desktop.support()
        if possible:
            native = desktop.NativeWindow(url, busy=lambda: bool(ctx.jobs.active_libraries()), debug=debug)
        else:
            log.warning("showing the interface in the browser instead of a window: %s", why)
    host = HostSwitch(native or BrowserHost(url))
    used_window = native is not None

    def quit_app() -> None:
        stop.set()
        srv = server_holder.get("server")
        if srv is not None:
            srv.should_exit = True
        if native is not None:
            native.close()

    app = create_app(ctx, shutdown=quit_app, host=host)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    server_holder["server"] = server
    thread = threading.Thread(target=lambda: (server.run(), stop.set()), name="server", daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        log.error("server failed to start")
        return 1
    paths.run_info_path().write_text(json.dumps({"pid": os.getpid(), "port": port, "version": __version__,
                                                 "mode": host.mode}), "utf-8")
    desktop.save_state(port=port)
    log.info("Perplexify %s is serving %s (%s)", __version__, url, host.mode)

    try:
        if native is not None:
            if native.run():
                return 0                       # the window was used and closed: the app is done (cleanup below)
            log.warning("the window could not be opened (%s); using the browser instead", native.problem)
            host.target = BrowserHost(url)
            native = None
        if open_browser and ctx.settings.get()["server"].get("open_browser", True):
            webbrowser.open(url)
        from .tray import run_tray

        if not (use_tray and run_tray(url, stop, quit_app)):
            print(f"Perplexify is running at {url}  (press Ctrl+C to stop)") if sys.stdout else None
            while thread.is_alive() and not stop.is_set():
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        quit_app()
        thread.join(timeout=5)
        try:
            paths.run_info_path(create=False).unlink()
        except OSError:
            pass
        if used_window and EXIT_WATCHDOG:
            watchdog = threading.Timer(EXIT_WATCHDOG, lambda: os._exit(0))
            watchdog.daemon = True
            watchdog.start()
    return 0
