"""OS-specific helpers (Windows and macOS; everything degrades gracefully elsewhere).

* Secret storage for API keys: Windows DPAPI, macOS Keychain (through the `security` tool),
  otherwise a plain fallback. Keys are never sent back to the browser.
* A named mutex so the Windows installer can detect a running copy (Inno Setup `AppMutex`).
* Native file/folder pickers (the browser UI cannot hand us real paths).
"""
from __future__ import annotations

import base64
import ctypes
import os
import subprocess
import sys
import uuid

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

MUTEX_NAME = "Perplexify.SingleInstance"
KEYCHAIN_SERVICE = "Perplexify"
_CREATE_NO_WINDOW = 0x08000000


def _run(cmd: list[str], timeout: float = 20, **kw) -> subprocess.CompletedProcess:
    if IS_WINDOWS:
        kw.setdefault("creationflags", _CREATE_NO_WINDOW)
    # stdin must be set explicitly: a windowless pythonw.exe has no valid stdin handle
    return subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, **kw)


# --------------------------------------------------------------------------- Windows DPAPI
if IS_WINDOWS:
    from ctypes import wintypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _dpapi(data: bytes, protect: bool) -> bytes:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _BLOB()
        if protect:
            ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "Perplexify", None, None, None, 0,
                                          ctypes.byref(blob_out))
        else:
            ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0,
                                            ctypes.byref(blob_out))
        if not ok:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------------------- macOS Keychain
def _keychain_set(account: str, secret: str) -> bool:
    try:
        r = _run(["security", "add-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE, "-w", secret, "-U"])
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _keychain_get(account: str) -> str:
    try:
        r = _run(["security", "find-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE, "-w"])
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.decode("utf-8", "replace").rstrip("\r\n") if r.returncode == 0 else ""


def _keychain_delete(account: str) -> None:
    try:
        _run(["security", "delete-generic-password", "-a", account, "-s", KEYCHAIN_SERVICE])
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------------------- secrets API
def protect(text: str) -> str:
    """Store a secret; returns an opaque token that is safe to keep in settings.json."""
    if not text:
        return ""
    raw = text.encode("utf-8")
    if IS_WINDOWS:
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
        except Exception:
            pass
    if IS_MAC:
        account = uuid.uuid4().hex
        if _keychain_set(account, text):
            return "keychain:" + account
    return "plain:" + base64.b64encode(raw).decode("ascii")


def unprotect(token: str) -> str:
    if not token:
        return ""
    kind, _, payload = token.partition(":")
    try:
        if kind == "dpapi" and IS_WINDOWS:
            return _dpapi(base64.b64decode(payload), False).decode("utf-8")
        if kind == "keychain" and IS_MAC:
            return _keychain_get(payload)
        if kind == "plain":
            return base64.b64decode(payload).decode("utf-8")
    except Exception:
        return ""
    return ""


def discard(token: str) -> None:
    """Forget a stored secret (removes the Keychain item on macOS)."""
    kind, _, payload = (token or "").partition(":")
    if kind == "keychain" and IS_MAC and payload:
        _keychain_delete(payload)


# --------------------------------------------------------------------------- single instance
_mutex_handle = None


def mutex_name() -> str:
    """One running copy per data folder: a copy started with its own PERPLEXIFY_HOME (development, tests) does not
    collide with the installed app that uses the default folder."""
    from . import paths

    home = paths.env("HOME")
    if not home:
        return MUTEX_NAME
    import hashlib

    return MUTEX_NAME + "." + hashlib.sha1(os.path.abspath(home).lower().encode("utf-8")).hexdigest()[:8]


def acquire_single_instance(name: str | None = None) -> bool:
    """True if we are the first instance (always True off Windows; callers also probe the port)."""
    global _mutex_handle
    if not IS_WINDOWS:
        return True
    name = name or mutex_name()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    if not handle:
        return True
    if err == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def pid_alive(pid: int) -> bool:
    """True while a process with this id exists. It never signals the process: on Windows os.kill(pid, 0) would end it."""
    if IS_WINDOWS:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = k32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: it has not exited yet
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------- native dialogs
def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _run_ps(script: str, timeout: float = 900) -> str:
    prefix = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
           "-Command", prefix + script]
    return _run(cmd, timeout=timeout).stdout.decode("utf-8", errors="replace").strip()


def _as_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(lines: list[str], timeout: float = 900) -> str | None:
    args = ["osascript"]
    for ln in lines:
        args += ["-e", ln]
    r = _run(args, timeout=timeout)
    if r.returncode != 0:  # the user pressed Cancel (error -128) or the call failed
        return None
    return r.stdout.decode("utf-8", errors="replace").strip()


def pick_folder(title: str = "Choose a folder") -> str | None:
    if IS_WINDOWS:
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$o = New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$d.Description = {_ps_quote(title)}; $d.ShowNewFolderButton = $false; "
            "if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) { $d.SelectedPath }"
        )
        return _run_ps(script) or None
    if IS_MAC:
        out = _osascript(["tell current application to activate",
                          f"POSIX path of (choose folder with prompt {_as_quote(title)})"])
        return (out.rstrip("/") or "/") if out else None
    return None


def pick_files(title: str = "Choose files") -> list[str]:
    if IS_WINDOWS:
        flt = ("Manuals (*.pdf;*.html;*.htm;*.md;*.txt;*.rst;*.zip)|*.pdf;*.html;*.htm;*.md;*.txt;*.rst;*.zip"
               "|All files (*.*)|*.*")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$o = New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; "
            "$d = New-Object System.Windows.Forms.OpenFileDialog; $d.Multiselect = $true; "
            f"$d.Title = {_ps_quote(title)}; $d.Filter = {_ps_quote(flt)}; "
            "if ($d.ShowDialog($o) -eq [System.Windows.Forms.DialogResult]::OK) "
            "{ [string]::Join([string][char]10, $d.FileNames) }"
        )
        return [p for p in _run_ps(script).splitlines() if p.strip()]
    if IS_MAC:
        out = _osascript([
            "tell current application to activate",
            f"set fs to choose file with prompt {_as_quote(title)} with multiple selections allowed",
            'set out to ""',
            "repeat with f in fs",
            "set out to out & POSIX path of f & linefeed",
            "end repeat",
            "return out",
        ])
        return [p for p in (out or "").splitlines() if p.strip()]
    return []


def open_folder(path: str) -> None:
    if IS_WINDOWS:
        os.startfile(path)  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])
