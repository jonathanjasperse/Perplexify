"""The native window: Perplexify's interface shown in the app's own window instead of a browser tab.

The interface is a web page served by the app itself on 127.0.0.1. Here it is hosted by pywebview, which uses the
operating system's own web view (Microsoft Edge WebView2 on Windows, WKWebView on macOS), so the app gets its own
window, icon, taskbar entry and native file dialogs, and no browser is involved.

Nothing in this module is needed when the window is not available (Linux, a PC without the WebView2 Runtime,
`--browser`): `app.py` then opens the page in the default browser instead.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Callable

from . import APP_NAME, paths

log = logging.getLogger("perplexify.window")

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

APP_USER_MODEL_ID = "Perplexify.Desktop"   # groups the taskbar entry under Perplexify instead of python.org's pythonw.exe
WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"   # the Evergreen WebView2 Runtime in the EdgeUpdate registry
DEFAULT_SIZE = (1280, 860)
MIN_SIZE = (900, 620)

# The window background and title bar follow the interface theme. Keep these in step with --bg, --text and --line in
# web/style.css (dark and light).
THEMES = {
    "dark": {"bg": "#16171b", "fg": "#e3e4e8", "border": "#25262a"},
    "light": {"bg": "#e4e5e8", "fg": "#202226", "border": "#cdcfd3"},
}
DOCUMENT_TYPES = ("Documents (*.pdf;*.html;*.htm;*.md;*.markdown;*.txt;*.rst;*.zip)", "All files (*.*)")


# --------------------------------------------------------------------------- is a native window possible here?
def webview2_runtime_version() -> str | None:
    """Version of the Microsoft Edge WebView2 Runtime (Evergreen) installed on this PC, or None. Windows only."""
    if not IS_WINDOWS:
        return None
    import winreg

    base = "\\Microsoft\\EdgeUpdate\\Clients\\" + WEBVIEW2_CLIENT
    places = [(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node" + base),   # installed for all users (the usual place)
              (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE" + base),
              (winreg.HKEY_CURRENT_USER, "Software" + base)]                  # installed for this user only
    for hive, key in places:
        try:
            with winreg.OpenKey(hive, key) as k:
                version = str(winreg.QueryValueEx(k, "pv")[0]).strip()
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return version
    return None


def support() -> tuple[bool, str]:
    """(True, details) when this PC can show the native window, otherwise (False, why not)."""
    if paths.env("NO_WINDOW"):
        return False, "the window is switched off (PERPLEXIFY_NO_WINDOW)"
    if not (IS_WINDOWS or IS_MAC):
        return False, "native windows are available on Windows and macOS"
    try:
        import webview  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"pywebview could not be loaded ({type(e).__name__}: {e})"
    if IS_WINDOWS:
        try:
            import clr  # noqa: F401  (pythonnet: how pywebview talks to WebView2)
        except Exception as e:  # noqa: BLE001
            return False, f"pythonnet could not be loaded ({type(e).__name__}: {e})"
        version = webview2_runtime_version()
        if version is None:
            return False, "the Microsoft Edge WebView2 Runtime is not installed"
        return True, f"WebView2 Runtime {version}"
    return True, "WKWebView"


# --------------------------------------------------------------------------- remembered window state
def _state_file() -> Path:
    return paths.home() / "window.json"


def load_state() -> dict:
    """{width, height, x, y, maximized, theme, port} from the last session; anything missing or silly is left out."""
    try:
        raw = json.loads(_state_file().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict = {}
    if isinstance(raw, dict):
        for key, lo, hi in (("width", MIN_SIZE[0], 10000), ("height", MIN_SIZE[1], 10000), ("x", -10000, 20000), ("y", -10000, 20000),
                            ("port", 1024, 65535)):
            v = raw.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
                out[key] = int(v)
        if isinstance(raw.get("maximized"), bool):
            out["maximized"] = raw["maximized"]
        if raw.get("theme") in THEMES:
            out["theme"] = raw["theme"]
    return out


def save_state(**changes) -> None:
    """Merge `changes` into the remembered state. Never raises: losing the window size is not worth a crash."""
    try:
        state = load_state()
        state.update({k: v for k, v in changes.items() if v is not None})
        _state_file().write_text(json.dumps(state), "utf-8")
    except Exception:  # noqa: BLE001
        log.debug("could not save the window state", exc_info=True)


def fits_on_screen(state: dict) -> bool:
    """A remembered position is only used while it is still on a monitor (screens come and go)."""
    if "x" not in state or "y" not in state:
        return False
    if not IS_WINDOWS:
        return True
    try:
        user32 = _user32()
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)           # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN
        width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)        # SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN
        w = state.get("width", DEFAULT_SIZE[0])
        return state["x"] + w > left + 80 and state["x"] < left + width - 80 and top - 20 <= state["y"] < top + height - 80
    except Exception:  # noqa: BLE001
        return False


def work_area() -> tuple[int, int, int, int] | None:
    """(left, top, right, bottom) of the main screen without the taskbar, in the units the window is sized in.

    Only right before pywebview starts: the process is not yet DPI aware then, so Windows reports the same scaled
    ("logical") pixels that pywebview takes for a window size and saves for it. None when it cannot be asked.
    """
    if not IS_WINDOWS:
        return None
    try:
        from ctypes import wintypes

        r = wintypes.RECT()
        if _user32().SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):       # SPI_GETWORKAREA
            return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001
        log.debug("could not read the work area", exc_info=True)
    return None


def window_geometry(state: dict) -> tuple[int, int, tuple[int, int] | None]:
    """(width, height, (x, y) or None) to open the window with.

    Where it was last time, never bigger than the main screen when that is where it was; otherwise a window of the
    usual size, cut down to fit the screen and centred in it (the window system's own placement ignores the taskbar).
    """
    width, height = state.get("width", DEFAULT_SIZE[0]), state.get("height", DEFAULT_SIZE[1])
    area = work_area()
    if fits_on_screen(state):
        x, y = state["x"], state["y"]
        if area and area[0] <= x < area[2] and area[1] <= y < area[3]:
            width, height = min(width, area[2] - area[0]), min(height, area[3] - area[1])
            x, y = max(area[0], min(x, area[2] - width)), max(area[1], min(y, area[3] - height))
        return width, height, (x, y)
    if not area:
        return width, height, None
    left, top, right, bottom = area
    width = max(MIN_SIZE[0], min(width, right - left - 32))
    height = max(MIN_SIZE[1], min(height, bottom - top - 32))
    return width, height, (left + (right - left - width) // 2, top + (bottom - top - height) // 2)


# --------------------------------------------------------------------------- the icon
def icon_file() -> str | None:
    """A .ico of the app icon for the window and taskbar. Drawn once by icon.py (no image file is stored in the repository)."""
    try:
        import hashlib

        from . import __version__, icon
        from .icon import save_ico

        # named after the drawing code too, so a redesigned icon is not hidden behind an old cached one
        design = hashlib.sha1(Path(icon.__file__).read_bytes()).hexdigest()[:8]
        target = paths.home() / "cache" / f"icon-{__version__}-{design}.ico"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".tmp")
            save_ico(tmp)
            tmp.replace(target)
        return str(target)
    except Exception:  # noqa: BLE001
        log.debug("could not prepare the window icon", exc_info=True)
        return None


# --------------------------------------------------------------------------- Windows touches (all of them fail quietly)
_user32_cache = None


def _user32():
    """user32.dll with 64-bit safe prototypes (a bare ctypes call would squeeze handles into 32-bit ints)."""
    global _user32_cache
    if _user32_cache is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        void, c_int, c_uint = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint
        u.GetForegroundWindow.restype = void
        u.GetForegroundWindow.argtypes = []
        u.IsIconic.argtypes, u.IsIconic.restype = [void], c_int
        u.ShowWindow.argtypes, u.ShowWindow.restype = [void, c_int], c_int
        u.SetForegroundWindow.argtypes, u.SetForegroundWindow.restype = [void], c_int
        u.BringWindowToTop.argtypes, u.BringWindowToTop.restype = [void], c_int
        u.GetWindowThreadProcessId.argtypes, u.GetWindowThreadProcessId.restype = [void, void], c_uint
        u.AttachThreadInput.argtypes, u.AttachThreadInput.restype = [c_uint, c_uint, c_int], c_int
        u.GetSystemMetrics.argtypes, u.GetSystemMetrics.restype = [c_int], c_int
        u.SystemParametersInfoW.argtypes, u.SystemParametersInfoW.restype = [c_uint, c_uint, void, c_uint], c_int
        _user32_cache = u
    return _user32_cache


def _colorref(hex_color: str) -> int:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r      # COLORREF is 0x00BBGGRR


def set_app_user_model_id() -> None:
    """Must run before the first window: taskbar grouping and pinning then belong to Perplexify, not to pythonw.exe."""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_USER_MODEL_ID))
    except Exception:  # noqa: BLE001
        log.debug("SetCurrentProcessExplicitAppUserModelID failed", exc_info=True)


def style_titlebar(hwnd: int, theme: str) -> None:
    """Colour the Windows title bar like the page (dark mode on Windows 10 2004+, exact colours on Windows 11)."""
    if not IS_WINDOWS or not hwnd:
        return
    colors = THEMES.get(theme, THEMES["dark"])
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]

        def attribute(number: int, value: int) -> None:
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, number, ctypes.byref(v), ctypes.sizeof(v))

        attribute(20, 1 if theme == "dark" else 0)   # DWMWA_USE_IMMERSIVE_DARK_MODE
        attribute(35, _colorref(colors["bg"]))        # DWMWA_CAPTION_COLOR (Windows 11)
        attribute(36, _colorref(colors["fg"]))        # DWMWA_TEXT_COLOR
        attribute(34, _colorref(colors["border"]))    # DWMWA_BORDER_COLOR
    except Exception:  # noqa: BLE001
        log.debug("could not style the title bar", exc_info=True)


def bring_to_front(hwnd: int) -> None:
    """Restore a minimised window and take the foreground even when another program has focus."""
    if not IS_WINDOWS or not hwnd:
        return
    try:
        user32, kernel32 = _user32(), ctypes.WinDLL("kernel32")
        user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)    # SW_RESTORE, or SW_SHOW
        foreground = user32.GetForegroundWindow()
        mine = kernel32.GetCurrentThreadId()
        theirs = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = bool(theirs and theirs != mine and user32.AttachThreadInput(theirs, mine, 1))
        try:
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(theirs, mine, 0)
    except Exception:  # noqa: BLE001
        log.debug("could not bring the window to the front", exc_info=True)


# --------------------------------------------------------------------------- the window
class NativeWindow:
    """The window host the server talks to (see `Host` in server.py). `run()` blocks until the window is closed."""

    mode = "window"

    def __init__(self, url: str, busy: Callable[[], bool] = lambda: False, debug: bool = False) -> None:
        self.url = url
        self.busy = busy
        self.debug = debug
        self.problem = ""             # why the window could not be opened, when it could not
        self.window = None            # the pywebview window once it exists
        self._webview = None
        self._quitting = False
        self._shown = threading.Event()
        self.theme = load_state().get("theme", "dark")

    # ---- what the server calls
    def show(self) -> None:
        """Bring the window to the front, un-minimised (a second launch of the app lands here)."""
        win = self.window
        if win is None:
            return
        try:
            win.restore()          # a minimised window comes back; for any other window this does nothing
            win.show()             # shows and activates it: what works while another program has the focus
        except Exception:  # noqa: BLE001
            log.debug("show failed", exc_info=True)
        if IS_WINDOWS:
            bring_to_front(self._handle())     # belt and braces: Windows sometimes keeps the foreground for itself

    def set_theme(self, theme: str) -> None:
        if theme not in THEMES:
            return
        self.theme = theme
        save_state(theme=theme)
        if self.window is not None:
            style_titlebar(self._handle(), theme)

    def pick_folder(self, title: str = "Choose a folder") -> str | None:
        found = self._dialog("FOLDER", False, ())
        return str(found[0]) if found else None

    def pick_files(self, title: str = "Choose files") -> list[str]:
        return [str(p) for p in (self._dialog("OPEN", True, DOCUMENT_TYPES) or [])]

    def close(self) -> None:
        """Close the window without asking (the app is quitting)."""
        win = self.window
        if win is not None and not self._quitting:
            self._remember()          # also when the app quits from its own Quit button, not only from the window's X
        self._quitting = True
        if win is not None:
            try:
                win.destroy()
            except Exception:  # noqa: BLE001
                log.debug("destroy failed", exc_info=True)

    # ---- running
    def run(self) -> bool:
        """Open the window and block until it is closed. False when it could not be opened (the caller then uses the browser)."""
        try:
            import webview
        except Exception as e:  # noqa: BLE001
            self.problem = f"pywebview could not be loaded: {e}"
            return False
        self._webview = webview
        set_app_user_model_id()
        state = load_state()
        width, height, where = window_geometry(state)
        options: dict = {
            "width": width, "height": height,
            "min_size": MIN_SIZE, "background_color": THEMES[self.theme]["bg"], "text_select": True,
            "maximized": bool(state.get("maximized")),
        }
        if where:
            options.update(x=where[0], y=where[1])
        try:
            self.window = webview.create_window(APP_NAME, self.url, **options)
            self.window.events.closing += self._on_closing
            self.window.events.shown += self._on_shown
            start: dict = {"debug": self.debug, "private_mode": False,       # private mode would forget localStorage: the theme, the ticks
                           "storage_path": str(self._storage())}
            icon = icon_file()
            if icon:
                start["icon"] = icon
            if IS_WINDOWS:
                start["gui"] = "edgechromium"      # never fall back to the old Internet Explorer engine
            if self.debug:
                try:
                    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False    # F12 or right-click > Inspect opens them, no pop-up on every start
                except Exception:  # noqa: BLE001
                    log.debug("could not keep the developer tools closed", exc_info=True)
            webview.start(**start)
        except Exception as e:  # noqa: BLE001
            log.exception("the window failed")
            self.problem = f"{type(e).__name__}: {e}"
        return self._shown.is_set()               # shown at least once: it ran, so do not also open a browser

    # ---- events (the closing one runs on the window's own thread: nothing in it may wait for that thread)
    def _on_shown(self) -> None:
        self._shown.set()
        style_titlebar(self._handle(), self.theme)

    def _on_closing(self):
        """Return False to keep the window open."""
        if self._quitting:
            return True
        try:
            working = bool(self.busy())
        except Exception:  # noqa: BLE001
            working = False
        if working:
            ok = self.window.create_confirmation_dialog(
                APP_NAME, "Perplexify is still indexing. If you quit now it carries on the next time you open it "
                          "(use finish beside the library).\n\nQuit anyway?")
            if not ok:
                return False
        self._remember()
        return True

    def _remember(self) -> None:
        try:
            if self._is_maximized():
                save_state(maximized=True)
            else:
                win = self.window
                save_state(width=int(win.width), height=int(win.height), x=int(win.x), y=int(win.y), maximized=False)
        except Exception:  # noqa: BLE001
            log.debug("could not read the window size", exc_info=True)

    # ---- helpers
    def _storage(self) -> Path:
        p = paths.home() / "webview"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _handle(self) -> int:
        """The Win32 window handle, 0 when there is none."""
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:  # noqa: BLE001
            return 0

    def _is_maximized(self) -> bool:
        try:
            return str(self.window.native.WindowState) == "Maximized"
        except Exception:  # noqa: BLE001
            return False

    def _dialog(self, kind: str, many: bool, types) -> tuple | None:
        win, webview = self.window, self._webview
        if win is None or webview is None:
            return None
        dialogs = getattr(webview, "FileDialog", None)
        which = getattr(dialogs, kind) if dialogs is not None else getattr(webview, kind + "_DIALOG")
        try:
            return win.create_file_dialog(which, directory=str(Path.home()), allow_multiple=many, file_types=types)
        except Exception:  # noqa: BLE001
            log.exception("the file dialog failed")
            return None
