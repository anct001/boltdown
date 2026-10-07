"""Per-site rules: how to talk to one host in particular.

The problem this solves is concrete. One CDN happily serves sixteen parallel
ranges; the next returns 403 above four, and a third needs a Referer or it
hands back an HTML error page. Without somewhere to record that, the user
re-types the same options every time they download from the same place.

Matching is pure and lives here, so the interesting part - which of several
overlapping patterns wins - is testable without a database or a GUI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass(slots=True)
class SiteProfile:
    """Overrides applied to every download from a matching host."""

    id: int | None = None
    pattern: str = ""            # "example.com", "*.example.com", "*"
    enabled: bool = True
    connections: int | None = None
    speed_limit: int | None = None
    user_agent: str | None = None
    referer: str | None = None
    cookie: str | None = None
    proxy: str | None = None
    note: str = ""
    username: str | None = None
    #: in the clear here; the database only ever holds it protected
    password: str | None = None
    #: where this site's downloads go: "{host}/{year}-{month}", or absolute
    folder: str | None = None

    # ---------------------------------------------------------------- matching

    @property
    def normalised(self) -> str:
        pattern = (self.pattern or "").strip().lower()
        if pattern.startswith("www."):
            pattern = pattern[4:]
        return pattern

    def matches(self, host: str) -> bool:
        pattern = self.normalised
        if not pattern or not host:
            return False
        if pattern == "*":
            return True
        if pattern.startswith("*."):
            suffix = pattern[2:]
            return host == suffix or host.endswith("." + suffix)
        return host == pattern

    @property
    def specificity(self) -> int:
        """How narrow the pattern is; the narrowest match wins.

        `cdn.example.com` beats `*.example.com` beats `*`, so a general rule
        can be written once and a single awkward host corrected on top of it.
        """
        pattern = self.normalised
        if pattern == "*":
            return 0
        if pattern.startswith("*."):
            return 1 + pattern.count(".")
        return 100 + pattern.count(".")

    def overrides(self) -> dict[str, object]:
        """Only the fields that are actually set."""
        values = {
            "connections": self.connections,
            "speed_limit": self.speed_limit,
            "user_agent": self.user_agent,
            "referer": self.referer,
            "cookie": self.cookie,
            "proxy": self.proxy,
            "auth": (self.username, self.password or "") if self.username else None,
        }
        return {k: v for k, v in values.items() if v not in (None, "")}


def match(url: str, profiles: list[SiteProfile]) -> SiteProfile | None:
    """The most specific enabled profile for `url`, or None."""
    host = host_of(url)
    candidates = [p for p in profiles if p.enabled and p.matches(host)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.specificity)


#: what a folder template may say
FOLDER_FIELDS = ("host", "date", "year", "month", "day", "category")
_FIELD = re.compile(r"\{(\w+)\}")
_UNSAFE = re.compile(r'[<>:"|?*\x00-\x1f]')


def render_folder(
    template: str,
    url: str,
    *,
    base: Path,
    category: str = "",
    when: datetime | None = None,
) -> Path:
    """Where a download from `url` goes under this template.

    Relative templates sit under `base` (the download folder); an absolute
    one ("D:\\Work\\{host}") is used as it is. Every piece is made safe for
    a Windows path, and ".." is never let through - a rule decides which
    folder, not how far up the tree.
    """
    when = when or datetime.now()
    values = {
        "host": host_of(url) or "unknown",
        "date": when.strftime("%Y-%m-%d"),
        "year": when.strftime("%Y"),
        "month": when.strftime("%m"),
        "day": when.strftime("%d"),
        "category": category or "",
    }
    rendered = _FIELD.sub(lambda m: values.get(m.group(1).lower(), m.group(0)), template.strip())
    windows = PureWindowsPath(rendered)
    absolute = windows.is_absolute() or Path(rendered).is_absolute()
    anchor = windows.anchor if windows.is_absolute() else ("/" if absolute else "")
    pieces = []
    for part in re.split(r"[\\/]+", rendered[len(anchor):] if anchor else rendered):
        part = _UNSAFE.sub("_", part).strip().rstrip(". ")
        if part and part not in (".", ".."):
            pieces.append(part)
    root = Path(anchor) if absolute else Path(base)
    return root.joinpath(*pieces)


def apply_to(url: str, profiles: list[SiteProfile], values: dict) -> dict:
    """Fill the blanks in `values` from the matching profile.

    Anything the user typed for this particular download wins; the profile
    only supplies what was left empty.
    """
    profile = match(url, profiles)
    if profile is None:
        return values
    merged = dict(values)
    for key, value in profile.overrides().items():
        if merged.get(key) in (None, "", 0):
            merged[key] = value
    return merged
