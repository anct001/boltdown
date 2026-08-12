"""Site Grabber: walk a site and collect the files worth downloading."""

from __future__ import annotations

from .crawler import CrawlOptions, CrawlResult, FoundFile, crawl

__all__ = ["CrawlOptions", "CrawlResult", "FoundFile", "crawl"]
