"""`python -m perplexify`: with no arguments it starts the desktop app, otherwise it runs a command (see `--help`)."""
import sys


def _report_crash() -> None:
    """The installed app runs under pythonw.exe, which has no console, so an unexpected error would vanish.
    Write it to the log folder and, when there is no console to print to, show it in a message box."""
    import traceback

    text = traceback.format_exc()
    saved = ""
    try:
        from . import paths

        log = paths.logs_dir() / "crash.log"
        with open(log, "a", encoding="utf-8") as f:
            f.write(text + "\n")
        saved = f"\n\nDetails were saved to:\n{log}"
    except Exception:  # noqa: BLE001
        pass
    if sys.stderr is not None:
        sys.stderr.write(text)
    elif sys.platform == "win32":
        try:
            import ctypes

            last = text.strip().splitlines()[-1] if text.strip() else "unknown error"
            ctypes.windll.user32.MessageBoxW(0, "Perplexify could not start.\n\n" + last + saved, "Perplexify", 0x10)
        except Exception:  # noqa: BLE001
            pass


def _main() -> int:
    try:
        from .cli import main

        return main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return 130
    except BaseException:  # noqa: BLE001
        _report_crash()
        return 1


if __name__ == "__main__":  # the guard matters: worker processes re-import this module
    raise SystemExit(_main())
