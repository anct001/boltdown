"""Frozen entry point for the console tool (`boltdown.exe`)."""

from __future__ import annotations

import sys

from app.cli import main

if __name__ == "__main__":
    sys.exit(main())
