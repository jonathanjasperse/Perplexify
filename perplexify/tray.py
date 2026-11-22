"""The tray / menu-bar icon: used only when the interface lives in the default browser (no window of our own),
so that closing the browser tab does not stop the app and there is still a way to quit it."""
from __future__ import annotations

import logging
import threading
import webbrowser

from . import __version__, osutil, paths

log = logging.getLogger("perplexify")


def run_tray(url: str, stop: threading.Event, quit_app) -> bool:
    """Show the tray / menu-bar icon until Quit. Returns False when no tray is available."""
    try:
        import pystray

        from .icon import make_icon
    except Exception:  # noqa: BLE001
        return False
    if osutil.IS_MAC:
        try:  # menu-bar only: no Dock icon
            import AppKit

            AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:  # noqa: BLE001
            pass
    icon = pystray.Icon("Perplexify", make_icon(64), f"Perplexify {__version__}", menu=pystray.Menu(
        pystray.MenuItem("Open Perplexify", lambda: webbrowser.open(url), default=True),
        pystray.MenuItem("Open data folder", lambda: osutil.open_folder(str(paths.home()))),
        pystray.MenuItem("Open log folder", lambda: osutil.open_folder(str(paths.logs_dir()))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda: quit_app())))

    def watcher() -> None:  # the UI's own Quit button ends the server; close the tray with it
        stop.wait()
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=watcher, daemon=True).start()
    try:
        icon.run()
    except Exception:  # noqa: BLE001
        log.exception("tray icon failed; continuing without it")
        return False
    return True
