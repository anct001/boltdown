"""Shared HTTP client construction.

Everything that talks to the network goes through `build_client` so that
proxy / cookie / User-Agent / TLS settings are applied uniformly. Browser
integration (P3) will populate `RequestSpec` from the extension payload, so
cookies and referer are first-class here rather than bolted on later.
"""

from __future__ import annotations

import os
import ssl
import threading
from dataclasses import dataclass, field
from ipaddress import ip_address
from urllib.parse import urlsplit

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

    def cookie_header(self) -> str | None:
        """The Cookie value, whether it came from the browser or a header."""
        for name, value in self.headers.items():
            if name.lower() == "cookie":
                return value
        return self.cookie


# ------------------------------------------------------------- cookie scoping

#: second-level labels under which a country code is the real suffix:
#: `example.co.uk`, `example.com.vn`, `example.co.jp`
_GENERIC_SECOND_LEVEL = frozenset(
    {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "nic"}
)
#: hosting suffixes where every subdomain belongs to somebody else
_SHARED_SUFFIXES = (
    "github.io", "gitlab.io", "blogspot.com", "herokuapp.com", "appspot.com",
    "netlify.app", "vercel.app", "pages.dev", "workers.dev", "web.app",
    "firebaseapp.com", "azurewebsites.net", "cloudfront.net", "amazonaws.com",
    "r2.dev", "onrender.com", "fly.dev", "glitch.me", "repl.co", "ngrok.io",
    "ngrok-free.app", "trycloudflare.com", "duckdns.org", "no-ip.org",
)


def _site(host: str) -> str:
    """An approximation of the registrable domain, without a suffix list.

    Good enough to decide where a browser cookie may follow a redirect: it
    errs towards *not* sending, which costs at worst a login page instead of
    the file, never a session handed to a stranger.
    """
    host = host.rstrip(".").lower()
    try:
        ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass
    if any(host == s or host.endswith("." + s) for s in _SHARED_SUFFIXES):
        # Tenants nest at different depths (`bucket.s3.amazonaws.com`), so
        # nothing below a shared suffix is assumed to be the same owner.
        return host
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _GENERIC_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def cookie_allowed(origin_url: str, target_url: str) -> bool:
    """May the browser's cookies for `origin_url` be sent to `target_url`?

    The extension reads the cookies for the link the user clicked, and those
    are a logged-in session. They belong to that site: not to the CDN yt-dlp
    resolved a video to, not to wherever an open redirect points, and never
    in clear text once the page was https.
    """
    origin, target = urlsplit(origin_url), urlsplit(target_url)
    if not origin.hostname or not target.hostname:
        return False
    if origin.scheme == "https" and target.scheme != "https":
        return False
    return _site(origin.hostname) == _site(target.hostname)


def _cookie_hook(origin_url: str, cookie: str):
    async def scope_cookie(request: httpx.Request) -> None:
        if cookie_allowed(origin_url, str(request.url)):
            request.headers["Cookie"] = cookie
        else:
            request.headers.pop("Cookie", None)

    return scope_cookie


_SSL_LOCK = threading.Lock()
_SSL_CONTEXTS: dict[tuple[str | None, str | None], ssl.SSLContext] = {}


def shared_ssl_context() -> ssl.SSLContext:
    """One verifying TLS context for every client, built once.

    Building one reads and parses the whole CA bundle - about 50 ms, paid by
    every download before its first request, http:// ones included. The
    context is safe to share between connections and threads. It is keyed on
    the variables httpx itself honours, so pointing SSL_CERT_FILE elsewhere
    still takes effect.
    """
    key = (os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR"))
    with _SSL_LOCK:
        context = _SSL_CONTEXTS.get(key)
        if context is None:
            context = httpx.create_ssl_context(verify=True, trust_env=True)
            _SSL_CONTEXTS[key] = context
        return context


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
    headers = spec.effective_headers()
    hooks: dict[str, list] = {}
    cookie = spec.cookie_header()
    if cookie:
        # Applied per request rather than as a default header. httpx drops a
        # default Cookie on every redirect - same site included, which turned
        # "download behind a login" into "save the login page" - and the hook
        # runs again for each hop, so it can put it back where it belongs.
        for name in [n for n in headers if n.lower() == "cookie"]:
            del headers[name]
        hooks["request"] = [_cookie_hook(spec.url, cookie)]
    return httpx.AsyncClient(
        headers=headers,
        event_hooks=hooks,
        follow_redirects=True,
        timeout=timeout,
        limits=limits,
        proxy=spec.proxy,
        auth=httpx.BasicAuth(*spec.auth) if spec.auth else None,
        verify=shared_ssl_context() if spec.verify_tls else False,
        trust_env=True,
    )
