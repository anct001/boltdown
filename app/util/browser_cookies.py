"""Reading cookies out of Chrome / Edge, so login-walled files download.

The alternative is what the app has done so far: the user opens devtools,
copies a Cookie header by hand, and pastes it in. That works and stays
available - this module is the convenience path.

How Chromium stores cookies since v80:

* `Local State` holds a base64 blob prefixed `DPAPI`, which is the AES key
  encrypted for the current Windows account;
* each cookie value is `v10`/`v11` + 12-byte nonce + ciphertext + 16-byte tag,
  AES-256-GCM under that key.

Only the current user's own account can decrypt it, which is the point: this
reads what the user could read anyway, never anyone else's profile. The parts
that do not touch the disk are split out so they can be tested.
"""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .log import get_logger

log = get_logger(__name__)

KEY_PREFIX = b"DPAPI"
GCM_PREFIXES = (b"v10", b"v11")
NONCE = 12
TAG = 16


@dataclass(slots=True)
class Browser:
    name: str
    user_data: Path

    @property
    def local_state(self) -> Path:
        return self.user_data / "Local State"

    def cookie_files(self) -> list[Path]:
        """Every profile's cookie database (Default, Profile 1, ...)."""
        found = []
        for profile in sorted(self.user_data.glob("*")):
            candidate = profile / "Network" / "Cookies"
            if candidate.is_file():
                found.append(candidate)
        return found


def installed_browsers() -> list[Browser]:
    if sys.platform != "win32":  # pragma: no cover - Windows only feature
        return []
    import os

    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = {
        "Chrome": local / "Google" / "Chrome" / "User Data",
        "Edge": local / "Microsoft" / "Edge" / "User Data",
        "Brave": local / "BraveSoftware" / "Brave-Browser" / "User Data",
        "Chromium": local / "Chromium" / "User Data",
    }
    return [
        Browser(name, path) for name, path in candidates.items()
        if (path / "Local State").is_file()
    ]


def encrypted_key(local_state_text: str) -> bytes | None:
    """The AES key blob out of `Local State`, still DPAPI-encrypted."""
    try:
        data = json.loads(local_state_text)
    except (TypeError, json.JSONDecodeError):
        return None
    encoded = (data.get("os_crypt") or {}).get("encrypted_key")
    if not encoded:
        return None
    blob = base64.b64decode(encoded)
    return blob[len(KEY_PREFIX):] if blob.startswith(KEY_PREFIX) else blob


def unprotect(blob: bytes) -> bytes | None:
    """DPAPI: only this Windows account can undo it."""
    if sys.platform != "win32":  # pragma: no cover - Windows only feature
        return None
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    source = Blob(len(blob), ctypes.cast(ctypes.create_string_buffer(blob),
                                         ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)
    )
    if not ok:
        return None
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(result.pbData)


def decrypt_value(blob: bytes, key: bytes | None) -> str:
    """One cookie value; empty string when it cannot be read."""
    if not blob:
        return ""
    if blob[:3] in GCM_PREFIXES:
        if not key:
            return ""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            nonce = blob[3:3 + NONCE]
            payload = blob[3 + NONCE:]
            plain = AESGCM(key).decrypt(nonce, payload, None)
        except Exception as exc:  # noqa: BLE001 - a stale cookie is not fatal
            log.debug("cookie decrypt failed: %s", exc)
            return ""
        return plain.decode("utf-8", "replace")
    # Pre-v80 cookies are DPAPI blobs on their own.
    plain = unprotect(blob)
    return plain.decode("utf-8", "replace") if plain else ""


def domain_matches(host_key: str, host: str) -> bool:
    """Chromium stores `.example.com` for "and every subdomain"."""
    host_key = (host_key or "").lower().lstrip(".")
    host = (host or "").lower()
    if not host_key or not host:
        return False
    return host == host_key or host.endswith("." + host_key)


def cookie_header(pairs: list[tuple[str, str]]) -> str:
    """`[('a', '1'), ('b', '2')]` -> `a=1; b=2`, skipping empties."""
    return "; ".join(f"{name}={value}" for name, value in pairs if name and value)


def read_cookies(browser: Browser, host: str) -> str:
    """Every cookie `host` would send, as a ready-to-use header value."""
    key = None
    try:
        key = unprotect(encrypted_key(browser.local_state.read_text(encoding="utf-8")) or b"")
    except OSError as exc:
        log.info("cannot read %s Local State: %s", browser.name, exc)

    pairs: list[tuple[str, str]] = []
    for database in browser.cookie_files():
        # The browser keeps the file locked, so work on a copy.
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "Cookies"
            try:
                shutil.copy2(database, copy)
            except OSError as exc:
                log.info("cannot copy %s: %s", database, exc)
                continue
            try:
                connection = sqlite3.connect(str(copy))
                rows = connection.execute(
                    "SELECT host_key, name, encrypted_value, value FROM cookies"
                ).fetchall()
                connection.close()
            except sqlite3.Error as exc:
                log.info("cannot read %s: %s", database, exc)
                continue
        for host_key, name, encrypted, plain in rows:
            if not domain_matches(host_key, host):
                continue
            value = plain or decrypt_value(encrypted, key)
            if value:
                pairs.append((name, value))
    return cookie_header(pairs)
