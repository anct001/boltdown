"""Resume metadata sidecar (`<file>.part.boltdown`).

Per-segment progress changes several times a second, which is far too often
for SQLite. It lives in a small JSON file next to the `.part` file instead,
written atomically so a crash mid-save can never corrupt it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ..util.log import get_logger
from .segment import Segment

log = get_logger(__name__)

FORMAT_VERSION = 1
SUFFIX = ".boltdown"
#: what the sidecar was called before the rename
LEGACY_SUFFIX = ".idmdown"


@dataclass(slots=True)
class ResumeMeta:
    url: str
    final_url: str
    filename: str
    size: int | None
    resumable: bool
    segments: list[Segment] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    version: int = FORMAT_VERSION

    @property
    def downloaded(self) -> int:
        return sum(s.done for s in self.segments)

    def to_dict(self) -> dict:
        return {
            "v": self.version,
            "url": self.url,
            "final_url": self.final_url,
            "filename": self.filename,
            "size": self.size,
            "resumable": self.resumable,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ResumeMeta":
        return cls(
            url=data["url"],
            final_url=data.get("final_url", data["url"]),
            filename=data.get("filename", ""),
            size=data.get("size"),
            resumable=bool(data.get("resumable", False)),
            segments=[Segment.from_dict(s) for s in data.get("segments", [])],
            etag=data.get("etag"),
            last_modified=data.get("last_modified"),
            version=int(data.get("v", FORMAT_VERSION)),
        )

    def save(self, path: Path) -> None:
        """Write atomically: temp file in the same directory, then replace."""
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps(self.to_dict(), ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "ResumeMeta | None":
        path = Path(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            # A download interrupted by the pre-rename version left its
            # metadata under the old name; reading it means those bytes are
            # not thrown away.
            legacy = Path(str(path).replace(SUFFIX, LEGACY_SUFFIX))
            if legacy != path and legacy.exists():
                log.info("continuing a download started before the rename")
                return cls.load(legacy)
            return None
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            log.warning("ignoring unreadable resume metadata %s: %s", path, exc)
            return None
        if int(data.get("v", 0)) != FORMAT_VERSION:
            log.warning("resume metadata %s has an unsupported version", path)
            return None
        try:
            return cls.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("ignoring malformed resume metadata %s: %s", path, exc)
            return None

    def mismatch_reason(self, probe) -> str | None:
        """Return why this metadata cannot be reused, or None if it can."""
        if self.size != probe.size:
            return f"size changed ({self.size} -> {probe.size})"
        if self.etag and probe.etag and self.etag != probe.etag:
            return "ETag changed"
        if (
            not (self.etag and probe.etag)
            and self.last_modified
            and probe.last_modified
            and self.last_modified != probe.last_modified
        ):
            return "Last-Modified changed"
        if not self.segments:
            return "no segments recorded"
        if self.size is not None:
            covered = sum((s.total or 0) for s in self.segments)
            if covered != self.size:
                return "segment ranges do not cover the file"
        for s in self.segments:
            if s.done < 0 or (s.total is not None and s.done > s.total):
                return f"segment {s.index} has an impossible offset"
        return None


def legacy_meta_path_for(part_path: Path) -> Path:
    """Where a download interrupted by the previous version left its metadata."""
    return Path(str(part_path) + LEGACY_SUFFIX)


def meta_path_for(part_path: Path) -> Path:
    return Path(str(part_path) + SUFFIX)


def cleanup(part_path: Path) -> None:
    """Remove the part file and its metadata (used on cancel / restart)."""
    for candidate in (
        meta_path_for(part_path),
        Path(str(meta_path_for(part_path)) + ".tmp"),
        legacy_meta_path_for(part_path),   # left by the pre-rename version
        part_path,
    ):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - filesystem dependent
            log.warning("could not remove %s: %s", candidate, exc)
