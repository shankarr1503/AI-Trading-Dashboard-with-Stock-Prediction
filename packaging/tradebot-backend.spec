# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec of the desktop sidecar: a one-folder app

    dist-backend/tradebot-backend/tradebot-backend[.exe]
    dist-backend/tradebot-backend/_internal/...         (Python, libraries, data)

Build it with packaging/build_backend.py (run from the repository root), which
checks the inputs and passes them in through these environment variables:

    TRADEBOT_FRONTEND_OUT   the static frontend export (frontend/out), bundled as
                            "frontend_out"; empty = no frontend (API only)
    TRADEBOT_VERSION_FILE   a text file holding the app version, bundled as
                            tradebot_version.txt

Bundled data (relative to sys._MEIPASS, see backend/desktop.py):
    backend/database/migrations/   Alembic env.py + versions/*.py, run from source at startup
    frontend_out/                  the static export served at "/"
    tradebot_version.txt
"""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent          # SPECPATH: the directory holding this spec
sys.path.insert(0, str(ROOT))                   # collect_submodules("backend") needs to import it

FRONTEND_OUT = os.environ.get("TRADEBOT_FRONTEND_OUT", "").strip()
VERSION_FILE = os.environ.get("TRADEBOT_VERSION_FILE", "").strip()


def source_tree(src: Path, dest: str):
    """(file, dest dir) pairs for every file under src, without bytecode caches."""
    pairs = []
    for path in sorted(src.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo"):
            rel_parent = path.parent.relative_to(src).as_posix()
            pairs.append((str(path), dest if rel_parent == "." else f"{dest}/{rel_parent}"))
    return pairs


datas = []
binaries = []
hiddenimports = []

# Alembic loads env.py and the revision files from disk, so they ship as source files.
migrations = ROOT / "backend" / "database" / "migrations"
if not (migrations / "env.py").is_file():
    raise SystemExit(f"Alembic migrations not found at {migrations}")
datas += source_tree(migrations, "backend/database/migrations")

if FRONTEND_OUT:
    frontend = Path(FRONTEND_OUT).resolve()
    if not (frontend / "index.html").is_file():
        raise SystemExit(f"TRADEBOT_FRONTEND_OUT={frontend} has no index.html")
    datas += source_tree(frontend, "frontend_out")

if VERSION_FILE:
    datas.append((str(Path(VERSION_FILE).resolve()), "."))

# Our own packages: everything, including modules only imported lazily.
hiddenimports += collect_submodules("backend") + collect_submodules("shared")

# Third-party packages that load parts of themselves dynamically (by name, from
# data files, native libraries or package metadata).
for package in (
    "yfinance",         # data files, protobuf live-price messages
    "curl_cffi",        # yfinance's HTTP client: native libcurl-impersonate + CA handling
    "uvicorn",          # protocols / loops / lifespan picked by name
    "websockets",       # uvicorn's ws="websockets" (legacy server implementation)
    "limits",           # slowapi's backend: storage plugins and data files
    "tzdata",           # zoneinfo database (Windows has none)
    "certifi",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Large packages whose optional extras are not installed: everything but those.
hiddenimports += collect_submodules("anthropic", filter=lambda name: "httpx_aiohttp" not in name)
hiddenimports += collect_submodules("alembic", filter=lambda name: not name.startswith("alembic.testing"))

hiddenimports += collect_submodules("sqlalchemy.dialects.sqlite")
hiddenimports += collect_submodules("anyio")
hiddenimports += [
    "aiosqlite",
    "sqlalchemy.ext.asyncio",
    "email_validator",
    "bcrypt",
    "jwt",
    "h11",
    "slowapi",
    "pydantic_settings",
    "zoneinfo",
]
# pydantic's EmailStr checks email-validator's installed version (package metadata).
for dist in ("email-validator", "pydantic", "pydantic-settings", "fastapi", "starlette", "httpx", "anthropic",
             "yfinance", "slowapi", "limits", "SQLAlchemy", "alembic", "uvicorn", "websockets"):
    try:
        datas += copy_metadata(dist)
    except Exception as e:      # noqa: BLE001 - optional metadata
        print(f"note: no metadata for {dist}: {e}")

excludes = [
    "tkinter", "_tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest", "PyQt5", "PyQt6",
    "PySide2", "PySide6", "wx",
    # uvicorn[standard] extras the sidecar never uses (loop="asyncio", http="h11")
    "uvloop", "httptools", "watchfiles",
    # server-only database / cache drivers
    "asyncpg", "psycopg2", "redis",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tradebot-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX breaks native libraries and upsets virus scanners
    console=True,               # stdout carries the TRADEBOT_READY protocol; the shell hides the window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,           # the build machine's architecture (one macOS runner per arch)
    codesign_identity=None,     # electron-builder signs the app, including extraResources
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="tradebot-backend",
)
