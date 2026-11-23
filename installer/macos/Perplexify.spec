# PyInstaller recipe for the macOS app:   pyinstaller --noconfirm installer/macos/Perplexify.spec
# Result: dist/Perplexify.app (the embedding models and the llama.cpp engine are copied into
# Contents/Resources afterwards by the workflow, see .github/workflows/macos-installer.yml).
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).resolve().parents[1]
version = re.search(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"', (root / "perplexify" / "__init__.py").read_text("utf-8")).group(1)

# Packages whose compiled parts or data files PyInstaller cannot see from the import statements alone.
packages = ["fastembed", "onnxruntime", "tokenizers", "webview", "tree_sitter", "py_rust_stemmers", "certifi"]
# one compiled grammar per language: read from requirements.txt so a new language needs no change here
packages += [m.group(1).replace("-", "_") for m in
             (re.match(r"(tree-sitter-[a-z0-9-]+)", ln) for ln in (root / "requirements.txt").read_text("utf-8").splitlines()) if m]

datas, binaries, hidden = [], [], []
for name in packages:
    d, b, h = collect_all(name)
    datas += d
    binaries += b
    hidden += h
# imported by name at run time (providers, loaders, the server's own protocol modules)
for name in ["perplexify", "uvicorn", "anthropic", "pystray", "lxml", "huggingface_hub"]:
    hidden += collect_submodules(name)
datas += [(str(root / "perplexify" / "web"), "perplexify/web")]

a = Analysis(
    [str(root / "installer" / "macos" / "entry.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "IPython"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Perplexify", console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="Perplexify", upx=False)
app = BUNDLE(
    coll,
    name="Perplexify.app",
    icon=str(root / "build" / "Perplexify.icns") if (root / "build" / "Perplexify.icns").exists() else None,
    bundle_identifier="com.perplexify.desktop",
    version=version,
    info_plist={
        "CFBundleName": "Perplexify",
        "CFBundleDisplayName": "Perplexify",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},   # the window shows http://127.0.0.1:<port>, served by the app itself
        "NSHumanReadableCopyright": "Perplexify",
    },
)
