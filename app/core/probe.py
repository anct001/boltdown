"""Resource probing: size, range support, validators and file name.

The authoritative test for multi-segment support is a `Range: bytes=0-0`
request that comes back `206` with a parsable `Content-Range`. A `HEAD`
advertising `Accept-Ranges: bytes` is only a hint - plenty of CDNs advertise
it and then ignore the header.
"""

from __future__ import annotations

import re

import httpx

from ..util import filenames
from ..util.log import get_logger
from .errors import DownloadError, TransientError, classify_status
from .http_client import RequestSpec

log = get_logger(__name__)

_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


class ProbeResult:
    """What we learned about a URL before creating any segments."""

    __slots__ = (
        "url", "final_url", "size", "resumable", "etag", "last_modified",
        "content_type", "filename", "status",
    )

    def __init__(
        self,
        url: str,
        final_url: str,
        size: int | None,
        resumable: bool,
        etag: str | None,
        last_modified: str | None,
        content_type: str | None,
        filename: str,
        status: int,
    ) -> None:
        self.url = url
        self.final_url = final_url
        self.size = size
        self.resumable = resumable
        self.etag = etag
        self.last_modified = last_modified
        self.content_type = content_type
        self.filename = filename
        self.status = status

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ProbeResult(filename={self.filename!r}, size={self.size}, "
            f"resumable={self.resumable}, etag={self.etag!r})"
        )


def parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    """Parse `bytes start-end/total`; total is None when the server sent `*`."""
    if not value:
        return None
    m = _CONTENT_RANGE.search(value)
    if not m:
        return None
    start, end, total = m.group(1), m.group(2), m.group(3)
    return int(start), int(end), (None if total == "*" else int(total))


def _pick_filename(url: str, headers: httpx.Headers) -> str:
    name = filenames.from_content_disposition(headers.get("content-disposition"))
    if not name:
        name = filenames.from_url(url)
    if not name:
        name = "download"
    return filenames.sanitize(name)


async def probe(client: httpx.AsyncClient, spec: RequestSpec) -> ProbeResult:
    """Discover size / range support without downloading the body."""
    head_headers: httpx.Headers | None = None
    final_url = spec.url

    try:
        resp = await client.head(spec.url)
        if resp.status_code < 400:
            head_headers = resp.headers
            final_url = str(resp.url)
    except httpx.HTTPError as exc:
        log.debug("HEAD failed for %s: %s", spec.url, exc)

    # Ranged GET - authoritative for both size and range support.
    try:
        req = client.build_request("GET", spec.url, headers={"Range": "bytes=0-0"})
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise TransientError(f"probe failed: {exc}") from exc

    try:
        err = classify_status(resp.status_code)
        if err is not None:
            raise err
        headers = resp.headers
        final_url = str(resp.url)
        resumable = False
        size: int | None = None

        if resp.status_code == 206:
            parsed = parse_content_range(headers.get("content-range"))
            if parsed is not None:
                _start, _end, total = parsed
                resumable = True
                size = total
            else:
                log.warning("206 without a usable Content-Range on %s", final_url)
        else:
            # Server ignored Range and started sending the whole body.
            length = headers.get("content-length")
            size = int(length) if length and length.isdigit() else None

        if size is None and head_headers is not None:
            length = head_headers.get("content-length")
            if length and length.isdigit():
                size = int(length)

        source = head_headers if head_headers is not None else headers
        etag = source.get("etag") or headers.get("etag")
        last_modified = source.get("last-modified") or headers.get("last-modified")
        content_type = source.get("content-type") or headers.get("content-type")

        disp_source = headers if headers.get("content-disposition") else source
        filename = _pick_filename(final_url, disp_source)

        return ProbeResult(
            url=spec.url,
            final_url=final_url,
            size=size,
            resumable=bool(resumable and size),
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
            filename=filename,
            status=resp.status_code,
        )
    except DownloadError:
        raise
    finally:
        await resp.aclose()
