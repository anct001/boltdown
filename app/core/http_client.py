"""Shared HTTP client construction.

Everything that talks to the network goes through `build_client` so that
proxy / cookie / User-Agent / TLS settings are applied uniformly. Browser
integration (P3) will populate `RequestSpec` from the extension payload, so
cookies and referer are first-class here rather than bolted on later.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Boltdown/0.1"
)


@dataclass(slots=True)
class RequestSpec:
    """Everything needed to reproduce a request the browser would have made."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    cookie: str | None = None
    referer: str | None = None
    user_agent: str | None = None
    proxy: str | None = None
    auth: tuple[str, str] | None = None
    verify_tls: bool = True

    def effective_headers(self) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent or DEFAULT_USER_AGENT}
        # Never let the server hand us a gzip stream: byte offsets in a
        # compressed body do not map to byte offsets in the saved file.
        headers["Accept-Encoding"] = "identity"
        headers["Accept"] = "*/*"
        if self.referer:
            headers["Referer"] = self.referer
        if self.cookie:
            headers["Cookie"] = self.cookie
        headers.update(self.headers)
        return headers


def build_client(
    spec: RequestSpec,
    *,
    connect_timeout: float = 15.0,
    read_timeout: float = 30.0,
    max_connections: int = 64,
) -> httpx.AsyncClient:
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=read_timeout,
        write=read_timeout,
        pool=connect_timeout,
    )
    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_connections,
    )
    return httpx.AsyncClient(
        headers=spec.effective_headers(),
        follow_redirects=True,
        timeout=timeout,
        limits=limits,
        proxy=spec.proxy,
        auth=httpx.BasicAuth(*spec.auth) if spec.auth else None,
        verify=spec.verify_tls,
        trust_env=True,
    )
