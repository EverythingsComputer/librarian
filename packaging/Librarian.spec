# PyInstaller spec for the Librarian bundle (Linux onedir).
# Built by packaging/build_appimage.sh — do not invoke by hand unless you know why.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

REPO = Path(SPECPATH).parent

datas, binaries, hiddenimports = [], [], []
for pkg in ("onnxruntime", "sqlite_vec", "fastembed", "extract_msg",
            "reportlab", "pypdf"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# uvicorn imports its protocol/loop backends lazily by name
hiddenimports += collect_submodules("uvicorn.protocols")
hiddenimports += collect_submodules("uvicorn.loops")
hiddenimports += ["uvicorn.logging", "uvicorn.lifespan.on", "websockets", "wsproto"]

a = Analysis(
    [str(REPO / "packaging" / "entry.py")],
    pathex=[str(REPO)],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=["tkinter", "matplotlib", "PIL.ImageQt", "pytest"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="librarian",
    console=True,
    strip=(sys.platform == "linux"),
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, name="librarian")
