"""Frozen entry point for the native messaging host (`idmclone-host.exe`).

This one must stay a *console* executable: native messaging is stdio, and a
windowed build has none. It also stays out of the GUI exe so the browser
starts a process that does not have to load Qt.
"""

from __future__ import annotations

import sys

from app.ipc.native_host import main

if __name__ == "__main__":
    sys.exit(main())
