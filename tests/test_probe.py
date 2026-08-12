from __future__ import annotations

import pytest

from app.core.errors import NotFoundError
from app.core.http_client import RequestSpec, build_client
from app.core.probe import parse_content_range, probe


async def _probe(url: str):
    spec = RequestSpec(url=url)
    async with build_client(spec) as client:
        return await probe(client, spec)


def test_parse_content_range():
    assert parse_content_range("bytes 0-0/12345") == (0, 0, 12345)
    assert parse_content_range("bytes 10-19/*") == (10, 19, None)
    assert parse_content_range("garbage") is None
    assert parse_content_range(None) is None


async def test_probe_detects_size_and_range_support(server, payload):
    url = server.add_file("probe-ok.bin", payload)
    result = await _probe(url)
    assert result.size == len(payload)
    assert result.resumable is True
    assert result.filename == "probe-ok.bin"
    assert result.etag == '"v1"'


async def test_probe_falls_back_when_range_is_ignored(server, payload):
    server.add_file("probe-norange.bin", payload)
    result = await _probe(server.url_for("probe-norange.bin", norange=1))
    assert result.resumable is False
    assert result.size == len(payload)


async def test_probe_survives_head_rejection(server, payload):
    server.add_file("probe-nohead.bin", payload)
    result = await _probe(server.url_for("probe-nohead.bin", nohead=1))
    assert result.resumable is True
    assert result.size == len(payload)


async def test_probe_without_content_length(server, payload):
    server.add_file("probe-nolength.bin", payload)
    result = await _probe(server.url_for("probe-nolength.bin", nolength=1))
    assert result.resumable is False
    assert result.size is None


async def test_probe_prefers_content_disposition_name(server, payload):
    server.add_file("probe-disp.bin", payload, disposition="Báo cáo quý 4.pdf")
    result = await _probe(server.url_for("probe-disp.bin"))
    assert result.filename == "Báo cáo quý 4.pdf"


async def test_probe_raises_on_missing_resource(server):
    with pytest.raises(NotFoundError):
        await _probe(f"{server.base_url}/does-not-exist.bin")
