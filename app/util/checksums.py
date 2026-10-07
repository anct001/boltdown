"""Checksums: read what a site publishes, find it next to a download, compare.

A release page usually publishes its hashes in one of a few shapes, and the
code here accepts all of them:

    e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    sha256:e3b0c442...
    e3b0c442...  ubuntu-24.04-desktop-amd64.iso      (GNU, `sha256sum`)
    e3b0c442... *ubuntu-24.04-desktop-amd64.iso      (GNU, binary mode)
    SHA256 (ubuntu-24.04-desktop-amd64.iso) = e3b0c442...   (BSD, `shasum`)

and the algorithm follows from the length when it is not named.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BLOCK = 1 << 20
#: hex length -> algorithm
BY_LENGTH = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}
ALGORITHMS = frozenset(BY_LENGTH.values())
_ALIASES = {"sha-1": "sha1", "sha-256": "sha256", "sha-512": "sha512", "md-5": "md5"}

#: files worth looking up a published checksum for: installers, archives and
#: disk images are what sites publish hashes for, and what hurts most corrupted
VERIFIABLE = frozenset({
    "iso", "img", "zip", "7z", "rar", "gz", "tgz", "xz", "bz2", "zst", "tar",
    "exe", "msi", "msix", "dmg", "pkg", "apk", "deb", "rpm", "appimage", "bin",
    "jar", "whl", "vhd", "vhdx", "qcow2", "ova",
})
MIN_VERIFY_SIZE = 1 << 20
#: a checksum listing larger than this is not one
MAX_LISTING = 512 * 1024

_HEX = r"[0-9a-fA-F]"
_PLAIN = re.compile(rf"^\s*(?:([a-zA-Z0-9-]+)\s*[:=]\s*)?({_HEX}{{32,128}})\s*$")
_GNU = re.compile(rf"^\s*({_HEX}{{32,128}})\s+\*?(.+?)\s*$")
_BSD = re.compile(rf"^\s*([A-Za-z0-9-]+)\s*\((.+)\)\s*=\s*({_HEX}{{32,128}})\s*$")


@dataclass(frozen=True, slots=True)
class Expected:
    algorithm: str
    digest: str
    #: where it came from: "entered", or the URL of the listing it was read from
    source: str = "entered"


def _algorithm(name: str | None, digest: str) -> str | None:
    by_length = BY_LENGTH.get(len(digest))
    if name:
        name = _ALIASES.get(name.lower(), name.lower())
        if name in ALGORITHMS and by_length == name:
            return name
        return None
    return by_length


def parse_expected(text: str, source: str = "entered") -> Expected | None:
    """What the user pasted, in any of the shapes above; None if it is none."""
    text = (text or "").strip()
    if not text:
        return None
    line = text.splitlines()[0]
    for pattern, name_at, digest_at in ((_PLAIN, 1, 2), (_BSD, 1, 3)):
        match = pattern.match(line)
        if match:
            digest = match.group(digest_at).lower()
            algorithm = _algorithm(match.group(name_at), digest)
            return Expected(algorithm, digest, source) if algorithm else None
    match = _GNU.match(line)
    if match:
        digest = match.group(1).lower()
        algorithm = _algorithm(None, digest)
        return Expected(algorithm, digest, source) if algorithm else None
    return None


def find_in_listing(text: str, filename: str, source: str, hint: str | None = None) -> Expected | None:
    """The line for `filename` in a SHA256SUMS-style listing.

    A listing that is a single bare hash (`file.iso.sha256` often is) belongs
    to the file it sits next to.
    """
    lines = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    wanted = filename.strip()
    for line in lines:
        bsd = _BSD.match(line)
        if bsd and Path(bsd.group(2).strip()).name == wanted:
            found = parse_expected(line, source)
            if found:
                return found
        gnu = _GNU.match(line)
        if gnu and Path(gnu.group(2).strip().lstrip("*")).name == wanted:
            digest = gnu.group(1).lower()
            algorithm = _algorithm(hint, digest) or _algorithm(None, digest)
            if algorithm:
                return Expected(algorithm, digest, source)
    if len(lines) == 1:
        plain = _PLAIN.match(lines[0])
        if plain:
            digest = plain.group(2).lower()
            algorithm = _algorithm(plain.group(1) or hint, digest) or _algorithm(None, digest)
            if algorithm:
                return Expected(algorithm, digest, source)
    return None


def worth_checking(filename: str, size: int | None) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in VERIFIABLE and (size or 0) >= MIN_VERIFY_SIZE


def candidate_urls(url: str) -> list[tuple[str, str | None]]:
    """Where a published checksum for `url` might live, most specific first.

    The query string is dropped: on a signed link `?sig=...` is the
    signature, and `file.iso?sig=...sha256` is nothing at all.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.path or parts.path.endswith("/"):
        return []
    bare = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    folder = urlunsplit((parts.scheme, parts.netloc, parts.path.rsplit("/", 1)[0] + "/", "", ""))
    found = [
        (f"{bare}.sha256", "sha256"),
        (f"{bare}.sha256sum", "sha256"),
        (f"{bare}.sha512", "sha512"),
        (f"{bare}.sha1", "sha1"),
        (f"{bare}.md5", "md5"),
    ]
    found += [
        (folder + name, hint)
        for name, hint in (
            ("SHA256SUMS", "sha256"), ("sha256sums.txt", "sha256"),
            ("SHA512SUMS", "sha512"), ("checksums.txt", None), ("MD5SUMS", "md5"),
        )
    ]
    return found


def discover(fetch, url: str, filename: str) -> Expected | None:
    """Look next to `url` for a published checksum of `filename`.

    `fetch(url) -> str | None` returns a small text body, or None for
    anything that is not one (missing, too large, not text). The first match
    wins; this costs a handful of tiny requests, after a download that took
    far longer.
    """
    for candidate, hint in candidate_urls(url):
        text = fetch(candidate)
        if not text:
            continue
        found = find_in_listing(text, filename, candidate, hint)
        if found:
            return found
    return None


def http_fetcher(
    *, proxy: str | None = None, user_agent: str | None = None, verify_tls: bool = True
):
    """`fetch(url) -> str | None` for `discover`, over plain HTTP(S).

    No cookies: a checksum listing is public, and the download's session has
    no business going to every URL guessed next to it. Bodies are read only
    up to MAX_LISTING, and anything not answered 200 is nothing.
    """
    import httpx

    from ..core.http_client import DEFAULT_USER_AGENT, shared_ssl_context

    def fetch(url: str) -> str | None:
        try:
            with httpx.Client(
                proxy=proxy,
                verify=shared_ssl_context() if verify_tls else False,
                follow_redirects=True,
                timeout=httpx.Timeout(10.0),
                headers={"User-Agent": user_agent or DEFAULT_USER_AGENT},
            ) as client, client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_LISTING:
                        return None
        except (httpx.HTTPError, OSError, ValueError):
            return None
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return None

    return fetch


def hash_file(
    path: Path,
    algorithm: str = "sha256",
    *,
    on_progress=None,
    stop: threading.Event | None = None,
) -> str | None:
    """Digest `path`; None if it was cancelled."""
    digest = hashlib.new(algorithm)
    total = Path(path).stat().st_size or 1
    done = 0
    with open(path, "rb") as handle:
        while True:
            if stop is not None and stop.is_set():
                return None
            block = handle.read(BLOCK)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if on_progress is not None:
                on_progress(done * 100 // total)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class Verdict:
    expected: Expected
    actual: str

    @property
    def ok(self) -> bool:
        return self.actual == self.expected.digest


def verify(path: Path, expected: Expected, stop: threading.Event | None = None) -> Verdict | None:
    actual = hash_file(Path(path), expected.algorithm, stop=stop)
    return None if actual is None else Verdict(expected, actual)
