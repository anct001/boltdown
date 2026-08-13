"""Wire formats used by the browser integration.

Two different framings meet in this package:

* **Native messaging** (Chrome <-> `native_host.py`): a 4-byte little-endian
  length followed by UTF-8 JSON, on stdin/stdout. Chrome rejects messages
  larger than 1 MB in the browser->host direction.
* **Local IPC** (`native_host.py` / a second instance <-> the running app):
  newline-delimited JSON over a loopback TCP socket, guarded by a token.

Keeping them separate means the app never has to speak Chrome's protocol and
the host process stays tiny.
"""

from __future__ import annotations

import json
import struct
from typing import IO, Any

HOST_NAME = "com.idmclone.host"
PROTOCOL_VERSION = 1
MAX_MESSAGE = 1024 * 1024

#: message types accepted by the running application
TYPE_PING = "ping"
TYPE_DOWNLOAD = "download"
TYPE_MEDIA = "media"
TYPE_SHOW = "show"
TYPE_LIST = "list"
TYPE_PAUSE = "pause"
TYPE_RESUME = "resume"

STREAMING_EXTENSIONS = (".m3u8", ".mpd")


class ProtocolError(Exception):
    pass


# ------------------------------------------------------------ native messaging


def encode_native(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE:
        raise ProtocolError(f"message too large: {len(payload)} bytes")
    return struct.pack("<I", len(payload)) + payload


def read_native(stream: IO[bytes]) -> dict[str, Any] | None:
    """Read one native message; None means the browser closed the pipe."""
    header = stream.read(4)
    if not header or len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE:
        raise ProtocolError(f"declared message too large: {length}")
    body = stream.read(length)
    if body is None or len(body) < length:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"malformed native message: {exc}") from exc


def write_native(stream: IO[bytes], message: dict[str, Any]) -> None:
    stream.write(encode_native(message))
    stream.flush()


# -------------------------------------------------------------------- local IPC


def encode_line(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


def decode_line(line: bytes) -> dict[str, Any]:
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"malformed IPC line: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("IPC message must be a JSON object")
    return data


def is_streaming_url(url: str) -> bool:
    """True for playlist URLs that need the media pipeline, not a plain GET.

    Kept here (rather than imported from `app.media`) so the native host stays
    a few kilobytes of stdlib; `app.media.detect` is the richer version the
    application itself uses.
    """
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return path.endswith(STREAMING_EXTENSIONS)
