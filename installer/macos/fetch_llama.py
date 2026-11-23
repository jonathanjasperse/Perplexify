"""Download the pinned llama.cpp build for macOS, check its SHA-256 and unpack the parts the app needs.

    python installer/macos/fetch_llama.py arm64|x64 <destination folder> [cache folder]

Only the standard library is used (it runs in CI before anything is installed). The destination ends up with
llama-server, the libraries it loads (*.dylib, symlinks kept) and the licence, nothing else.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

PINS = Path(__file__).resolve().parents[1] / "pins.json"
KEEP_EXE = {"llama-server"}


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("arm64", "x64"):
        print(__doc__)
        return 2
    arch, dest = argv[1], Path(argv[2])
    cache = Path(argv[3]) if len(argv) > 3 else Path("llama-cache")
    pin = json.loads(PINS.read_text("utf-8"))["llama_macos"][arch]
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / pin["asset"]
    if not archive.is_file():
        print(f"downloading {pin['url']}")
        with urllib.request.urlopen(pin["url"], timeout=120) as r, open(archive, "wb") as f:
            shutil.copyfileobj(r, f)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != pin["sha256"]:
        archive.unlink()
        print(f"error: {pin['asset']} has SHA-256 {digest}, installer/pins.json pins {pin['sha256']}. The download was deleted.", file=sys.stderr)
        return 1
    print("SHA-256 ok")

    work = cache / "unpacked"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        for m in tar.getmembers():                           # refuse paths that leave the folder (tar slip)
            target = (work / m.name).resolve()
            if work.resolve() not in target.parents and target != work.resolve():
                print(f"error: unsafe path in the archive: {m.name}", file=sys.stderr)
                return 1
        tar.extractall(work, filter="data")                  # "data": no absolute paths, no links that point outside the folder
    roots = [p for p in work.iterdir() if p.is_dir()]
    src = roots[0] if len(roots) == 1 else work
    if not (src / "llama-server").is_file():
        print("error: the archive has no llama-server", file=sys.stderr)
        return 1

    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    kept = 0
    for p in sorted(src.iterdir()):
        keep = p.name in KEEP_EXE or p.suffix == ".dylib" or p.name.startswith("LICENSE")
        if not keep:
            continue
        if p.is_symlink():
            (dest / p.name).symlink_to(p.readlink())          # libllama.dylib -> libllama.1.dylib
        else:
            shutil.copy2(p, dest / p.name)
        kept += 1
    (dest / "llama-server").chmod(0o755)
    print(f"{kept} files for {arch} in {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
