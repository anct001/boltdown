"""Keeping a password in the database without keeping it in the clear.

On Windows the value goes through DPAPI (`CryptProtectData`): it is
encrypted with a key tied to the user's Windows account, so the database
file copied to another machine - or read by another account - gives nothing
away, and there is no key of ours to lose. Elsewhere (the application ships
for Windows; this is for running from a source checkout) the value is only
encoded, and marked so, rather than pretending to a protection it lacks.
"""

from __future__ import annotations

import base64
import sys

DPAPI = "dpapi:"
PLAIN = "plain:"


def protect(secret: str) -> str:
    if not secret:
        return ""
    data = secret.encode("utf-8")
    if sys.platform == "win32":
        return DPAPI + base64.b64encode(_dpapi(data, encrypt=True)).decode("ascii")
    return PLAIN + base64.b64encode(data).decode("ascii")


def unprotect(stored: str | None) -> str:
    """The secret back; "" for nothing, or for a value this account cannot read."""
    if not stored:
        return ""
    try:
        if stored.startswith(DPAPI):
            if sys.platform != "win32":
                return ""
            return _dpapi(base64.b64decode(stored[len(DPAPI):]), encrypt=False).decode("utf-8")
        if stored.startswith(PLAIN):
            return base64.b64decode(stored[len(PLAIN):]).decode("utf-8")
    except (ValueError, OSError, UnicodeDecodeError):
        return ""
    return ""


def _dpapi(data: bytes, *, encrypt: bool) -> bytes:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    call = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = call(
        ctypes.byref(source), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result),
    )
    if not ok:
        raise OSError(ctypes.get_last_error() or "DPAPI refused")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)
