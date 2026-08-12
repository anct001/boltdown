"""Logging setup: rotating file log plus optional console output."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .paths import log_path

_configured = False

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"


def setup_logging(level: int = logging.INFO, console: bool = True) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path(), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(level)
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(stream)

    # httpx logs every request at INFO; too chatty for a download manager.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
