"""Writes THIRD-PARTY-NOTICES.txt for the packages in a site-packages folder, licence texts included.

    python make_notices.py <site-packages> <output.txt> [<embedding model name>]

The installer build runs this so the licences of everything bundled ship with the app.
"""
from __future__ import annotations

import sys
from email.parser import Parser
from pathlib import Path

LICENSE_PREFIXES = ("license", "licence", "copying", "notice", "unlicense")
FALLBACK_PREFIXES = LICENSE_PREFIXES + ("thirdpartynotices", "third_party_notices", "third-party-notices")
MAX_FILE = 300_000       # a licence text is never this big; skip anything that is not one
MAX_FALLBACK = 2_000_000  # but a package's own third-party notices file can be


MIT_TEXT = """Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE."""


def metadata(dist: Path) -> dict[str, str]:
    meta = dist / "METADATA"
    if not meta.is_file():
        return {}
    msg = Parser().parsestr(meta.read_text("utf-8", errors="replace"))
    lic = msg.get("License-Expression") or ""
    if not lic:
        lic = (msg.get("License") or "").strip().splitlines()[0][:80] if msg.get("License") else ""
    if not lic or len(lic) > 80:
        lic = next((c.split("::")[-1].strip() for c in msg.get_all("Classifier") or [] if c.startswith("License ::")), "")
    home = msg.get("Home-page") or next((u.split(",", 1)[-1].strip() for u in msg.get_all("Project-URL") or []), "")
    return {"name": msg.get("Name", dist.name), "version": msg.get("Version", ""), "license": lic or "see the text below",
            "home": home}


def unique_texts(files: list[Path]) -> list[Path]:
    """Some packages ship the same licence under licenses/ and at the top; keep one copy of identical text."""
    seen: set[str] = set()
    out = []
    for p in files:
        text = p.read_text("utf-8", errors="replace").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(p)
    return out


def license_files(dist: Path, site: Path) -> list[Path]:
    files = [p for p in sorted(dist.rglob("*")) if p.is_file() and p.name.lower().startswith(LICENSE_PREFIXES)
             and p.stat().st_size < MAX_FILE]
    if not files and (dist / "RECORD").is_file():
        # nothing in the metadata folder: look for the package's own licence or notices file among the files it installed
        for line in (dist / "RECORD").read_text("utf-8", errors="replace").splitlines():
            p = site / line.split(",")[0]
            if p.is_file() and p.name.lower().startswith(FALLBACK_PREFIXES) and p.stat().st_size < MAX_FALLBACK:
                files.append(p)
    return unique_texts(files)


def main() -> int:
    site, out = Path(sys.argv[1]), Path(sys.argv[2])
    model = sys.argv[3] if len(sys.argv) > 3 else ""
    reranker = sys.argv[4] if len(sys.argv) > 4 else ""
    llama = Path(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None     # the staged llama.cpp folder (local models)
    parts = [
        "Perplexify bundles the open-source software listed below. Each component is used under its own licence.\n"
        "The texts follow, one section per component.\n\n"
        "The Python packages are installed as ordinary files in runtime\\Lib\\site-packages, where their source can be\n"
        "read and, if you wish, replaced. This matters for pystray (GNU LGPL v3): its source is the .py files in\n"
        "runtime\\Lib\\site-packages\\pystray, and the app only imports it, so you can swap in a modified version.\n\n"
        "Components that are not Python packages:\n"
        "  - Python (runtime): Python Software Foundation License, see runtime\\LICENSE.txt\n"
        + (f"  - {model}: embedding model, MIT licence (ONNX conversion by Qdrant, run through fastembed)\n" if model else "")
        + "  - Whisper base (Systran/faster-whisper-base, in the folder models): the speech-to-text model behind dictation, MIT licence (OpenAI Whisper; CTranslate2 conversion run through faster-whisper)
"
        + (f"  - {reranker}: reranking model, Apache License 2.0 (ONNX conversion run through fastembed)\n" if reranker else "")
        + ("  - llama.cpp (llama-server and its libraries, in the folder llama): the engine that runs local models, MIT licence;\n"
           "    its OpenMP runtime (libomp.dll) is under the LLVM licence, see llama\\LICENSE-LLVM-OpenMP\n" if llama else "")
        + "  - Microsoft Visual C++ Redistributable: installed by the installer when needed, under Microsoft's licence terms\n"
    ]
    dists = sorted(site.glob("*.dist-info"), key=lambda p: p.name.lower())
    entries = []
    for dist in dists:
        meta = metadata(dist)
        if meta:
            entries.append((meta, "\n\n".join(p.read_text("utf-8", errors="replace").strip() for p in license_files(dist, site))))
    # a few packages ship no licence text at all; for those under a standard licence, give the standard text
    apache = next((b for _m, b in entries if b.lstrip().startswith("Apache License") and "Version 2.0" in b[:200]), "")
    for meta, body in entries:
        if not body:
            kind = meta["license"].lower()
            home = f" Project page: {meta['home']}" if meta["home"] else ""
            if "apache" in kind and apache:
                body = ("(this package ships no licence text; it is licensed under the Apache License 2.0, reproduced from another "
                        "package below)." + home + "\n\n" + apache)
            elif kind.startswith("mit") or " mit" in kind:
                body = "(this package ships no licence text; it is licensed under the MIT License, standard text below)." + home + "\n\n" + MIT_TEXT
            else:
                body = "(this package ships no licence text; see its project page)." + home
        head = f"{meta['name']} {meta['version']}  ({meta['license']})"
        parts.append("=" * 78 + "\n" + head + "\n" + "-" * 78 + "\n" + body + "\n")
    if llama:
        # the licence texts of llama.cpp and of what is built into llama-server (installer\licenses\llama.cpp.txt, copied from the
        # source release the pinned build comes from); the bare MIT text is only the fallback
        static = Path(__file__).resolve().parents[1] / "licenses" / "llama.cpp.txt"
        if static.is_file():
            parts.append("=" * 78 + "\n" + static.read_text("utf-8").strip() + "\n")
        else:
            parts.append("=" * 78 + "\nllama.cpp  (MIT)\n" + "-" * 78 + "\nCopyright (c) 2023-2026 The ggml authors\n\n" + MIT_TEXT + "\n")
        for lic in sorted(llama.glob("LICENSE*")):
            parts.append("=" * 78 + f"\n{lic.name}  (shipped with llama.cpp)\n" + "-" * 78 + "\n"
                         + lic.read_text("utf-8", errors="replace").strip() + "\n")
    out.write_text("\n".join(parts), "utf-8")
    print(f"{len(entries)} packages, {out.stat().st_size // 1024} KB -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
