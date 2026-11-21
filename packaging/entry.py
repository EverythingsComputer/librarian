"""PyInstaller entry point for the Librarian bundle (CLI + web UI + MCP servers)."""
import sys

from librarian.cli import main

if __name__ == "__main__":
    sys.exit(main())
