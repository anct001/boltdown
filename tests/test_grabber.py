"""Site Grabber: link extraction and the depth-limited crawl."""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import CancelledByUser
from app.core.http_client import RequestSpec, build_client
from app.grabber.crawler import (
    CrawlOptions,
    LinkExtractor,
    crawl,
    extension_of,
    name_of,
    same_site,
)

INDEX = """<!doctype html>
<html><head><title>Thư viện ảnh</title></head>
<body>
  <a href="page2.html">Trang 2</a>
  <a href="/gallery/one.jpg">Ảnh 1</a>
  <a href="https://other.example.com/away.html">Ngoài site</a>
  <a href="mailto:a@b.c">Mail</a>
  <a href="#top">Neo</a>
  <img src="thumbs/one.png">
  <video src="clip.mp4" poster="thumbs/poster.jpg"></video>
  <a href="docs/manual.pdf">Tài liệu</a>
</body></html>
"""

PAGE2 = """<!doctype html>
<html><body>
  <a href="/gallery/two.jpg">Ảnh 2</a>
  <a href="page3.html">Trang 3</a>
  <img src="/gallery/two-thumb.png">
</body></html>
"""

PAGE3 = """<!doctype html>
<html><body><a href="/gallery/three.jpg">Ảnh 3</a></body></html>
"""


@pytest.fixture
def site(server):
    """Publish a tiny three-page site and return the entry URL."""
    server.add_file("site/index.html", INDEX.encode("utf-8"))
    server.add_file("site/page2.html", PAGE2.encode("utf-8"))
    server.add_file("site/page3.html", PAGE3.encode("utf-8"))
    for name in ("gallery/one.jpg", "gallery/two.jpg", "gallery/three.jpg"):
        server.add_file(name, b"jpeg-bytes")
    for name in ("site/thumbs/one.png", "site/thumbs/poster.jpg",
                 "gallery/two-thumb.png"):
        server.add_file(name, b"png-bytes")
    server.add_file("site/clip.mp4", b"mp4-bytes")
    server.add_file("site/docs/manual.pdf", b"pdf-bytes")
    return server.url_for("site/index.html")


async def run_crawl(url: str, options: CrawlOptions | None = None, **kwargs):
    async with build_client(RequestSpec(url=url)) as client:
        return await crawl(client, url, options, **kwargs)


# --------------------------------------------------------------- unit pieces


def test_link_extractor_separates_navigation_from_assets():
    parser = LinkExtractor()
    parser.feed(INDEX)
    assert parser.title == "Thư viện ảnh"
    assert "page2.html" in parser.links
    assert "thumbs/one.png" in parser.assets
    assert "clip.mp4" in parser.assets and "thumbs/poster.jpg" in parser.assets


def test_base_href_overrides_the_page_url():
    parser = LinkExtractor()
    parser.feed('<html><head><base href="https://cdn.example.com/x/"></head>'
                '<body><a href="a.zip">z</a></body></html>')
    assert parser.base == "https://cdn.example.com/x/"


@pytest.mark.parametrize(
    "url, extension, name",
    [
        ("https://x/a/b/file.TAR.GZ", "gz", "file.TAR.GZ"),
        ("https://x/a/b/", "", "b"),
        ("https://x/gallery/%C4%91%E1%BA%B9p.jpg", "jpg", "đẹp.jpg"),
    ],
)
def test_url_helpers(url, extension, name):
    assert extension_of(url) == extension
    assert name_of(url) == name


def test_same_site_compares_hosts():
    assert same_site("https://a.com/x", "https://a.com/y")
    assert not same_site("https://a.com/x", "https://b.com/x")


# ------------------------------------------------------------------- crawling


async def test_depth_zero_only_reads_the_first_page(site):
    result = await run_crawl(site, CrawlOptions(depth=0))
    names = sorted(f.name for f in result.files)
    assert names == ["clip.mp4", "manual.pdf", "one.jpg", "one.png", "poster.jpg"]
    assert result.pages_visited == 1
    # Every result remembers the page it came from - the referer a CDN wants.
    assert all(f.referer == site for f in result.files)


async def test_depth_one_follows_links_but_stays_on_the_host(site):
    result = await run_crawl(site, CrawlOptions(depth=1))
    names = {f.name for f in result.files}
    assert {"one.jpg", "two.jpg", "two-thumb.png"} <= names
    assert "three.jpg" not in names, "page3 is two hops away"
    assert result.pages_visited == 2  # index + page2, never other.example.com


async def test_depth_two_reaches_the_last_page(site):
    result = await run_crawl(site, CrawlOptions(depth=2))
    assert "three.jpg" in {f.name for f in result.files}
    assert result.pages_visited == 3


async def test_extension_filter(site):
    result = await run_crawl(site, CrawlOptions(depth=1, extensions=("jpg",)))
    assert {f.name for f in result.files} == {"one.jpg", "two.jpg", "poster.jpg"}


async def test_pattern_and_exclude_filters(site):
    only_gallery = await run_crawl(site, CrawlOptions(depth=1, pattern=r"/gallery/"))
    assert all("/gallery/" in f.url for f in only_gallery.files)

    no_thumbs = await run_crawl(
        site, CrawlOptions(depth=1, extensions=("png", "jpg"), exclude=r"thumb")
    )
    assert all("thumb" not in f.url for f in no_thumbs.files)


async def test_page_limit_stops_the_walk(site):
    result = await run_crawl(site, CrawlOptions(depth=3, max_pages=1))
    assert result.pages_visited == 1 and result.stopped_early


async def test_link_limit_stops_the_walk(site):
    result = await run_crawl(site, CrawlOptions(depth=2, max_links=2))
    assert len(result.files) == 2 and result.stopped_early


async def test_progress_is_reported_per_page(site):
    seen: list[tuple[int, int, str]] = []
    await run_crawl(site, CrawlOptions(depth=1), on_progress=lambda *args: seen.append(args))
    assert [row[0] for row in seen] == [1, 2]
    assert seen[-1][2].endswith("page2.html")


async def test_stopping_raises_and_leaves_no_work_behind(site):
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(CancelledByUser):
        await run_crawl(site, CrawlOptions(depth=2), stop_event=stop)


async def test_a_missing_start_page_yields_nothing(server):
    result = await run_crawl(server.url_for("site/absent.html"))
    assert result.files == [] and result.pages_visited == 1


async def test_non_html_start_pages_are_not_parsed(server):
    url = server.add_file("site/not-a-page.bin", b"\x00\x01binary")
    result = await run_crawl(url)
    assert result.files == []


async def test_the_engine_loop_can_run_a_crawl(site):
    """The grabber dialog borrows the engine loop instead of making its own."""
    from app.core.engine import Engine

    async def job():
        async with build_client(RequestSpec(url=site)) as client:
            return await crawl(client, site, CrawlOptions(depth=1))

    with Engine() as engine:
        result = engine.run_coroutine(job()).result(timeout=30)
    assert len(result.files) > 3
