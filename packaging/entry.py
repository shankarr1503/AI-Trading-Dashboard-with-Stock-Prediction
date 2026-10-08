"""PyInstaller entry point of the desktop sidecar (tradebot-backend). See backend/desktop.py."""
import multiprocessing
import sys

if __name__ == "__main__":
    # A library that starts a multiprocessing child must not re-run the server in it.
    multiprocessing.freeze_support()
    from backend.desktop import cli

    sys.exit(cli())
