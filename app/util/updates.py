"""Is there a newer release than the one running?

Asks the GitHub Releases API and compares tags. Deliberately small: it never
installs anything by itself - it reports, and the caller offers to download
the installer *with the application's own engine*, which is a pleasing way for
a download manager to update itself.

Version comparison is its own function because "0.10.0 is newer than 0.9.0" is
exactly the kind of thing string comparison gets wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .log import get_logger

log = get_logger(__name__)

REPOSITORY = "anct001/idmclone"
API = "https://api.github.com/repos/{repo}/releases/latest"
#: only ever pick an installer, never a source archive
ASSET = re.compile(r"IDMCloneSetup-.*\.exe$", re.IGNORECASE)
TIMEOUT = 10.0


def parse_version(text: str | None) -> tuple[int, ...]:
    """`v1.2.3-beta` -> `(1, 2, 3)`; anything unparsable sorts lowest."""
    if not text:
        return (0,)
    numbers = re.findall(r"\d+", text.split("-")[0].split("+")[0])
    return tuple(int(n) for n in numbers) or (0,)


def is_newer(candidate: str | None, current: str | None) -> bool:
    left, right = parse_version(candidate), parse_version(current)
    length = max(len(left), len(right))
    left += (0,) * (length - len(left))
    right += (0,) * (length - len(right))
    return left > right


@dataclass(slots=True)
class Release:
    tag: str
    name: str = ""
    url: str = ""
    notes: str = ""
    asset_url: str = ""
    asset_name: str = ""
    asset_size: int = 0

    @property
    def has_installer(self) -> bool:
        return bool(self.asset_url)


def release_from_dict(data: dict) -> Release | None:
    if not isinstance(data, dict) or not data.get("tag_name"):
        return None
    release = Release(
        tag=str(data["tag_name"]),
        name=str(data.get("name") or data["tag_name"]),
        url=str(data.get("html_url") or ""),
        notes=str(data.get("body") or ""),
    )
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        if ASSET.search(name):
            release.asset_url = str(asset.get("browser_download_url") or "")
            release.asset_name = name
            release.asset_size = int(asset.get("size") or 0)
            break
    return release


def fetch_latest(repository: str = REPOSITORY, *, fetch_json=None) -> Release | None:
    """Ask GitHub for the newest release. `None` when it cannot be reached.

    A private repository answers 404 to an anonymous request, which is exactly
    what happens today - so a missing answer is normal, not an error worth
    putting in the user's face.
    """
    getter = fetch_json or _fetch_json
    try:
        data = getter(API.format(repo=repository))
    except Exception as exc:  # noqa: BLE001 - update checks never break the app
        log.info("update check failed: %s", exc)
        return None
    return release_from_dict(data or {})


def check(current: str, repository: str = REPOSITORY, *, fetch_json=None) -> Release | None:
    """The newer release, or None when this build is current."""
    latest = fetch_latest(repository, fetch_json=fetch_json)
    if latest is None:
        return None
    return latest if is_newer(latest.tag, current) else None


def _fetch_json(url: str):
    import httpx

    response = httpx.get(
        url,
        timeout=TIMEOUT,
        headers={"Accept": "application/vnd.github+json"},
        follow_redirects=True,
    )
    response.raise_for_status()
    return response.json()
