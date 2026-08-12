"""Expanding batch download patterns, IDM style.

    https://site.com/img[001-120].jpg   -> 120 URLs, zero padding kept
    https://site.com/vol[a-e]/x.zip     -> 5 URLs
    https://site.com/[1-3]/[a-b].txt    -> 6 URLs (every combination)

Pure string work so the dialog can preview the result before anything is
downloaded, and so the awkward cases - padding, descending ranges, a pattern
that would expand to a million URLs - are testable without Qt.
"""

from __future__ import annotations

import re
from itertools import product

#: `[001-120]` or `[a-e]`, the two forms IDM's batch dialog understands
RANGE = re.compile(r"\[(?:(\d+)-(\d+)|([a-zA-Z])-([a-zA-Z]))\]")
#: a wrong pattern must not lock the UI up building a list nobody wants
MAX_URLS = 10_000


class PatternError(ValueError):
    """The pattern is malformed, or would expand to an absurd number of URLs."""


def _numbers(start: str, end: str) -> list[str]:
    low, high = int(start), int(end)
    step = 1 if high >= low else -1
    width = len(start) if start.startswith("0") or len(start) == len(end) else 0
    values = [str(n) for n in range(low, high + step, step)]
    return [v.rjust(width, "0") for v in values] if width else values


def _letters(start: str, end: str) -> list[str]:
    low, high = ord(start), ord(end)
    step = 1 if high >= low else -1
    return [chr(c) for c in range(low, high + step, step)]


def count(pattern: str) -> int:
    """How many URLs `pattern` would produce."""
    total = 1
    for match in RANGE.finditer(pattern):
        digits_start, digits_end, alpha_start, alpha_end = match.groups()
        if digits_start is not None:
            total *= len(_numbers(digits_start, digits_end))
        else:
            total *= len(_letters(alpha_start, alpha_end))
    return total


def expand(pattern: str) -> list[str]:
    """Every URL `pattern` stands for; a pattern with no range is itself."""
    pattern = pattern.strip()
    if not pattern:
        return []
    matches = list(RANGE.finditer(pattern))
    if not matches:
        return [pattern]

    total = count(pattern)
    if total > MAX_URLS:
        raise PatternError(f"that pattern expands to {total} URLs (limit {MAX_URLS})")

    options = []
    for match in matches:
        digits_start, digits_end, alpha_start, alpha_end = match.groups()
        options.append(
            _numbers(digits_start, digits_end)
            if digits_start is not None
            else _letters(alpha_start, alpha_end)
        )

    results = []
    for combination in product(*options):
        out, last = [], 0
        for match, value in zip(matches, combination):
            out.append(pattern[last : match.start()])
            out.append(value)
            last = match.end()
        out.append(pattern[last:])
        results.append("".join(out))
    return results


def parse(text: str) -> list[str]:
    """Turn a pasted block into a de-duplicated URL list, expanding patterns."""
    seen: set[str] = set()
    urls: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.lower().startswith(("http://", "https://")):
            continue
        for url in expand(line):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls
