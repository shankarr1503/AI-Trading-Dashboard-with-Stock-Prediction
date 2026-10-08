#!/usr/bin/env python3
"""
Build the desktop sidecar (tradebot-backend) with PyInstaller.

Run from the repository root, in a Python 3.11 environment with
packaging/requirements-desktop.txt installed, after the static frontend export:

    cd frontend && NEXT_OUTPUT=export NEXT_PUBLIC_SAME_ORIGIN=true npm run build && cd ..
    python packaging/build_backend.py [--smoke-test]

Output: dist-backend/tradebot-backend/ (executable tradebot-backend, or
tradebot-backend.exe on Windows), which electron-builder ships as
<resources>/backend/. Intermediate files go to build-backend/.

Options:
    --frontend-out DIR          static export to bundle (default: frontend/out)
    --allow-missing-frontend    build without a frontend (the app then serves the API only)
    --version X.Y.Z             version reported in TRADEBOT_READY (default: desktop/package.json)
    --clean                     discard PyInstaller's cache first
    --smoke-test                start the built executable on a temporary data dir, wait for
                                TRADEBOT_READY, check /health and shut it down via the control API

Works the same on Windows, macOS and Linux; it builds for the machine it runs on.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "tradebot-backend.spec"
NAME = "tradebot-backend"
EXE_NAME = NAME + (".exe" if os.name == "nt" else "")


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def default_version() -> str:
    try:
        version = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8")).get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    except (OSError, ValueError):
        pass
    match = re.search(r'APP_VERSION:\s*str\s*=\s*"([^"]+)"', (ROOT / "backend" / "config.py").read_text(encoding="utf-8"))
    return match.group(1) if match else "0.0.0"


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def smoke_test(executable: Path, timeout: float = 180.0) -> None:
    """Start the built sidecar like the Electron shell does and stop it again."""
    print(f"smoke test: {executable}")
    token = secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix="tradebot-smoke-") as tmp:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("TRADEBOT_") and k not in ("DATABASE_URL", "JWT_SECRET_KEY", "APP_ENV")}
        env.update({"TRADEBOT_DATA_DIR": str(Path(tmp) / "data"), "TRADEBOT_CONTROL_TOKEN": token,
                    "TRADEBOT_PORT": "0", "TRADEBOT_NO_BOT": "1"})
        proc = subprocess.Popen([str(executable)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                cwd=tmp)
        stderr_lines: list = []
        threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True).start()
        result: dict = {}

        def read_stdout():
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith(("TRADEBOT_READY ", "TRADEBOT_ERROR ")):
                    result["line"] = line
                    return

        reader = threading.Thread(target=read_stdout, daemon=True)
        reader.start()
        reader.join(timeout)
        try:
            line = result.get("line")
            if not line or not line.startswith("TRADEBOT_READY "):
                tail = b"".join(stderr_lines[-30:]).decode("utf-8", "replace")
                fail(f"no TRADEBOT_READY line (got {line!r}); stderr tail:\n{tail}")
            ready = json.loads(line.split(" ", 1)[1])
            print(f"  ready: {ready}")
            with urllib.request.urlopen(ready["url"] + "/health", timeout=30) as response:
                health = json.loads(response.read())
            if health.get("status") != "healthy":
                fail(f"unexpected /health response {health}")
            print("  /health ok")
            request = urllib.request.Request(ready["url"] + "/api/desktop/shutdown", method="POST",
                                             headers={"X-Desktop-Token": token})
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 202:
                    fail(f"shutdown returned HTTP {response.status}")
            code = proc.wait(timeout=60)
            if code != 0:
                fail(f"the sidecar exited with code {code}")
            print("  graceful shutdown ok (exit 0)")
        except (OSError, urllib.error.URLError, subprocess.TimeoutExpired, ValueError) as e:
            fail(f"smoke test failed: {type(e).__name__}: {e}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the desktop sidecar (dist-backend/tradebot-backend/).")
    parser.add_argument("--frontend-out", default=str(ROOT / "frontend" / "out"),
                        help="static frontend export to bundle (default: frontend/out)")
    parser.add_argument("--allow-missing-frontend", action="store_true",
                        help="build even if the frontend export is missing (the app serves only the API)")
    parser.add_argument("--version", default=None, help="app version (default: desktop/package.json)")
    parser.add_argument("--distpath", default=str(ROOT / "dist-backend"), help="output directory (default: dist-backend)")
    parser.add_argument("--workpath", default=str(ROOT / "build-backend"),
                        help="PyInstaller work directory (default: build-backend)")
    parser.add_argument("--clean", action="store_true", help="clear PyInstaller's cache before building")
    parser.add_argument("--smoke-test", action="store_true", help="run the built executable once and stop it")
    args = parser.parse_args()

    if sys.version_info[:2] != (3, 11):
        print(f"warning: built and tested with Python 3.11, this is {sys.version.split()[0]}", file=sys.stderr)
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        fail("PyInstaller is not installed: pip install -r packaging/requirements-desktop.txt")

    frontend = Path(args.frontend_out).resolve()
    if (frontend / "index.html").is_file():
        frontend_env = str(frontend)
        print(f"frontend: {frontend}")
    elif args.allow_missing_frontend:
        frontend_env = ""
        print(f"warning: no frontend export at {frontend}; building an API-only sidecar", file=sys.stderr)
    else:
        fail(f"the frontend export {frontend} is missing (no index.html). Build it first:\n"
             "    cd frontend && NEXT_OUTPUT=export NEXT_PUBLIC_SAME_ORIGIN=true npm run build\n"
             "or pass --frontend-out DIR, or --allow-missing-frontend for an API-only build.")

    version = (args.version or default_version()).strip()
    workpath = Path(args.workpath).resolve()
    distpath = Path(args.distpath).resolve()
    workpath.mkdir(parents=True, exist_ok=True)
    version_file = workpath / "tradebot_version.txt"
    version_file.write_text(version + "\n", encoding="utf-8")
    print(f"version: {version}")

    output = distpath / NAME
    if output.exists():
        shutil.rmtree(output)

    env = dict(os.environ, TRADEBOT_FRONTEND_OUT=frontend_env, TRADEBOT_VERSION_FILE=str(version_file),
               PYTHONDONTWRITEBYTECODE="1")
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm",
           "--distpath", str(distpath), "--workpath", str(workpath)]
    if args.clean:
        cmd.append("--clean")
    print("+ " + " ".join(cmd), flush=True)
    started = time.monotonic()
    code = subprocess.call(cmd, cwd=str(ROOT), env=env)
    if code != 0:
        fail(f"PyInstaller failed with exit code {code}")

    executable = output / EXE_NAME
    if not executable.is_file():
        fail(f"PyInstaller finished but {executable} is missing")
    size_mb = dir_size(output) / 1024 / 1024
    print(f"built {output} ({size_mb:.0f} MB) in {time.monotonic() - started:.0f}s")
    if args.smoke_test:
        smoke_test(executable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
