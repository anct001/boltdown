"""Error taxonomy: the retry loop keys off these classes."""

from __future__ import annotations


class DownloadError(Exception):
    """Base class for every engine level failure."""


class TransientError(DownloadError):
    """Temporary condition - worth retrying with backoff."""


class FatalError(DownloadError):
    """Permanent condition - retrying cannot help."""


class NotFoundError(FatalError):
    """404 / 410."""


class AuthRequiredError(FatalError):
    """401 / 403 - caller must supply credentials or cookies."""


class RangeNotSatisfiableError(DownloadError):
    """416 - our saved offsets no longer match the resource."""


class ResourceChangedError(FatalError):
    """Size/ETag/Last-Modified differ from the saved resume metadata."""


class DiskFullError(FatalError):
    """No space left on the target volume."""


class CancelledByUser(DownloadError):
    """Task was paused or cancelled deliberately."""


def classify_status(status: int) -> DownloadError | None:
    """Map an HTTP status to an error, or None when the status is usable."""
    if status in (200, 206):
        return None
    if status in (401, 403):
        return AuthRequiredError(f"HTTP {status}: authentication required")
    if status in (404, 410):
        return NotFoundError(f"HTTP {status}: resource not found")
    if status == 416:
        return RangeNotSatisfiableError("HTTP 416: range not satisfiable")
    if status in (408, 425, 429, 500, 502, 503, 504):
        return TransientError(f"HTTP {status}")
    if 400 <= status < 500:
        return FatalError(f"HTTP {status}")
    return TransientError(f"HTTP {status}")
