"""A depth-limited crawler that lists downloadable files.

IDM's "Site Grabber" is a breadth-first walk with two filters: how far to
follow links, and which files to keep. That is all this is.

Deliberately small: `html.parser` from the standard library instead of a
parsing dependency, no JavaScript execution, no robots.txt fetching for a tool
the user points at one page by hand. Pages are fetched at most once, only
`text/html` bodies are parsed, and both the number of pages and the number of
results are capped so a wrong link cannot spider the internet.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Iterable
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

import httpx

from ..core.errors import CancelledByUser
from ..util.log import get_logger

log = get_logger(__name__)

#: extensions that mean "another page", not "a file to download"
PAGE_EXTENSIONS = frozenset(
    ("", "html", "htm", "xhtml", "shtml", "php", "asp", "aspx", "jsp", "cgi", "do")
)
#: attributes worth looking at, per tag
ASSET_ATTRS = {
    "img": ("src", "data-src"),
    "source": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "embed": ("src",),
    "object": ("data",),
    "iframe": ("src",),
}
MAX_PAGE_BYTES = 4 << 20


@dataclass(slots=True)
class CrawlOptions:
    depth: int = 1                       # how many link hops to follow
    max_pages: int = 50
    max_links: int = 1000
    same_host: bool = True
    extensions: tuple[str, ...] = ()     # ("jpg", "png"); empty = anything
    pattern: str | None = None           # regex the file URL must match
    exclude: str | None = None           # regex that rejects a file URL

    def normalised_extensions(self) -> frozenset[str]:
        return frozenset(e.lower().lstrip(".") for e in self.extensions if e.strip())


@dataclass(slots=True)
class FoundFile:
    url: str
    name: str
    referer: str
    extension: str
    depth: int


@dataclass(slots=True)
class CrawlResult:
    files: list[FoundFile] = field(default_factory=list)
    pages_visited: int = 0
    stopped_early: bool = False

    def urls(self) -> list[str]:
        return [f.url for f in self.files]


class LinkExtractor(HTMLParser):
    """Collects `<a>` targets and media/asset URLs, keeping them apart.

    `<a>` decides where the crawl can go next; assets are only ever results.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.assets: list[str] = []
        self.title: str | None = None
        self.base: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): (value or "") for name, value in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            href = values.get("href", "").strip()
            if href:
                self.links.append(href)
        elif tag == "base":
            # A <base href> rewrites every relative URL on the page.
            href = values.get("href", "").strip()
            if href:
                self.base = href
        elif tag in ASSET_ATTRS:
            for attr in ASSET_ATTRS[tag]:
                value = values.get(attr, "").strip()
                if value:
                    self.assets.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is None:
            self.title = data.strip()


def extension_of(url: str) -> str:
    path = urlsplit(url).path
    name = path.rpartition("/")[2]
    return name.rpartition(".")[2].lower() if "." in name else ""


def name_of(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return unquote(path.rpartition("/")[2]) or urlsplit(url).hostname or url


def same_site(a: str, b: str) -> bool:
    return (urlsplit(a).hostname or "").lower() == (urlsplit(b).hostname or "").lower()


def _clean(base: str, href: str) -> str | None:
    """Absolute, fragment-free URL, or None when it is not fetchable."""
    href = href.strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    absolute, _fragment = urldefrag(urljoin(base, href))
    if urlsplit(absolute).scheme not in ("http", "https"):
        return None
    return absolute


class _Matcher:
    def __init__(self, options: CrawlOptions) -> None:
        self.extensions = options.normalised_extensions()
        self.pattern = re.compile(options.pattern, re.IGNORECASE) if options.pattern else None
        self.exclude = re.compile(options.exclude, re.IGNORECASE) if options.exclude else None

    def wanted(self, url: str) -> bool:
        extension = extension_of(url)
        if self.extensions:
            if extension not in self.extensions:
                return False
        elif extension in PAGE_EXTENSIONS:
            return False  # without a filter, pages are not "files"
        if self.pattern is not None and not self.pattern.search(url):
            return False
        if self.exclude is not None and self.exclude.search(url):
            return False
        return True


async def crawl(
    client: httpx.AsyncClient,
    start_url: str,
    options: CrawlOptions | None = None,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
    stop_event: asyncio.Event | None = None,
) -> CrawlResult:
    """Walk from `start_url` and return the files that match `options`."""
    options = options or CrawlOptions()
    matcher = _Matcher(options)
    result = CrawlResult()

    queue: deque[tuple[str, int]] = deque([(start_url, 0)])
    seen_pages: set[str] = {start_url}
    seen_files: set[str] = set()

    while queue:
        if stop_event is not None and stop_event.is_set():
            result.stopped_early = True
            raise CancelledByUser("crawl stopped")
        if result.pages_visited >= options.max_pages:
            result.stopped_early = True
            break

        url, depth = queue.popleft()
        html = await _fetch_page(client, url)
        result.pages_visited += 1
        if on_progress is not None:
            on_progress(result.pages_visited, len(result.files), url)
        if html is None:
            continue

        parser = LinkExtractor()
        try:
            parser.feed(html)
        except Exception as exc:  # noqa: BLE001 - malformed markup is normal
            log.debug("could not parse %s: %s", url, exc)
        base = _clean(url, parser.base) if parser.base else url

        for href in _candidates(parser, base or url):
            if href in seen_files:
                continue
            if matcher.wanted(href):
                seen_files.add(href)
                result.files.append(
                    FoundFile(
                        url=href,
                        name=name_of(href),
                        referer=url,
                        extension=extension_of(href),
                        depth=depth,
                    )
                )
                if len(result.files) >= options.max_links:
                    result.stopped_early = True
                    return result

        if depth >= options.depth:
            continue
        for href in (_clean(base or url, h) for h in parser.links):
            if href is None or href in seen_pages:
                continue
            if options.same_host and not same_site(href, start_url):
                continue
            if extension_of(href) not in PAGE_EXTENSIONS:
                continue  # a file link is a result, never somewhere to go
            seen_pages.add(href)
            queue.append((href, depth + 1))

    return result


def _candidates(parser: LinkExtractor, base: str) -> Iterable[str]:
    for href in (*parser.links, *parser.assets):
        cleaned = _clean(base, href)
        if cleaned is not None:
            yield cleaned


async def _fetch_page(client: httpx.AsyncClient, url: str) -> str | None:
    """Return the HTML of `url`, or None if it is not a page we can read."""
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        log.info("crawl: %s failed (%s)", url, exc)
        return None
    if response.status_code >= 400:
        log.info("crawl: %s returned HTTP %d", url, response.status_code)
        return None
    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and "html" not in content_type and "xml" not in content_type:
        return None
    body = response.content[:MAX_PAGE_BYTES]
    return body.decode(response.encoding or "utf-8", "replace")
