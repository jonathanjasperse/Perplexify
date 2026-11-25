"""Which macOS the finished Perplexify.app really needs, and a promise to say so in its Info.plist.

Every compiled file inside the app (Python itself, numpy, ONNX Runtime, CTranslate2, the llama.cpp engine...) records the
oldest macOS it can run on. The app can run only where all of them can, so the newest of those numbers is its real minimum.
If Info.plist promises less (LSMinimumSystemVersion), a Mac that is too old opens the app and crashes without a word; if it says
the truth, Finder explains "requires macOS 13.0 or later" before anything is started.

    python installer/macos/min_macos.py dist/Perplexify.app          # prints the result and fixes Info.plist when needed
    python installer/macos/min_macos.py dist/Perplexify.app --check  # only reports; exit 1 when Info.plist promises too little
"""
from __future__ import annotations

import plistlib
import re
import subprocess
import sys
from pathlib import Path

MAGIC = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",      # thin Mach-O, either byte order
         b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}                                                   # universal (fat) binary


def parse_version(text: str) -> tuple[int, int]:
    nums = [int(x) for x in re.findall(r"\d+", text)[:2]]
    return (nums[0], nums[1] if len(nums) > 1 else 0)


def versions_in(otool_output: str) -> list[tuple[int, int]]:
    """The minimum macOS versions named in the output of `otool -l` (one per architecture of the file)."""
    found = []
    lines = otool_output.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("cmd LC_BUILD_VERSION"):
            platform, minos = None, None
            for nxt in lines[i + 1:i + 10]:
                n = nxt.strip().split()
                if len(n) == 2 and n[0] == "platform":
                    platform = n[1]
                elif len(n) == 2 and n[0] == "minos":
                    minos = n[1]
                if n[:1] == ["cmd"]:
                    break
            if minos and platform in (None, "1", "MACOS"):                  # platform 1 is macOS (not iOS, Mac Catalyst...)
                found.append(parse_version(minos))
        elif s.startswith("cmd LC_VERSION_MIN_MACOSX"):
            for nxt in lines[i + 1:i + 6]:
                n = nxt.strip().split()
                if len(n) == 2 and n[0] == "version":
                    found.append(parse_version(n[1]))
                    break
    return found


def newest(versions) -> tuple[int, int] | None:
    return max(versions) if versions else None


def is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) in MAGIC
    except OSError:
        return False


def scan(app: Path) -> tuple[tuple[int, int] | None, Path | None]:
    """(the newest minimum found, the file that needs it)."""
    best, who = None, None
    for p in app.rglob("*"):
        if p.is_symlink() or not p.is_file() or not is_macho(p):
            continue
        out = subprocess.run(["otool", "-l", str(p)], capture_output=True, text=True, errors="replace").stdout
        v = newest(versions_in(out))
        if v and (best is None or v > best):
            best, who = v, p
    return best, who


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    app, check = Path(argv[0]), "--check" in argv
    plist_path = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist_path.read_bytes())
    declared = parse_version(str(info.get("LSMinimumSystemVersion", "10.0")))
    best, who = scan(app)
    if best is None:
        print("no compiled files found: nothing to compare")
        return 1
    print(f"the app needs macOS {best[0]}.{best[1]} or later (set by {who.relative_to(app) if who else '?'}); Info.plist says {declared[0]}.{declared[1]}")
    if best <= declared:
        return 0
    if check:
        return 1
    info["LSMinimumSystemVersion"] = f"{best[0]}.{best[1]}"
    plist_path.write_bytes(plistlib.dumps(info))
    print(f"Info.plist now says LSMinimumSystemVersion {best[0]}.{best[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
