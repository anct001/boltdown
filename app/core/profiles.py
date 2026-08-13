"""Per-site rules: how to talk to one host in particular.

The problem this solves is concrete. One CDN happily serves sixteen parallel
ranges; the next returns 403 above four, and a third needs a Referer or it
hands back an HTML error page. Without somewhere to record that, the user
re-types the same options every time they download from the same place.

Matching is pure and lives here, so the interesting part - which of several
overlapping patterns wins - is testable without a database or a GUI.
"""

from __future__ import annotations

from dataclasses import dataclass
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
        }
        return {k: v for k, v in values.items() if v not in (None, "")}


def match(url: str, profiles: list[SiteProfile]) -> SiteProfile | None:
    """The most specific enabled profile for `url`, or None."""
    host = host_of(url)
    candidates = [p for p in profiles if p.enabled and p.matches(host)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.specificity)


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
