"""`python -m app` - GUI with no arguments, CLI when given URLs."""

from __future__ import annotations

import sys


GUI_FLAGS = ("--gui", "-g", "--tray", "--minimized")


def main() -> int:
    argv = sys.argv[1:]
    # URLs alone still open the GUI handoff path; a bare flag decides the rest.
    if not argv or argv[0] in GUI_FLAGS:
        from .gui import main as gui_main

        return gui_main([a for a in argv if a not in ("--gui", "-g")])

    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
