"""Frozen entry point for the windowed application.

PyInstaller runs its script as `__main__`, where `app`'s relative imports would
break - hence these three one-line shims instead of pointing the spec straight
at `app/gui.py`.
"""

from __future__ import annotations

import sys

from app.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
