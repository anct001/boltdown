"""Choosing a proxy: explicit, SOCKS, or whatever Windows is already using.

`httpx` handles `http://`, `https://` and - with the `socksio` package -
`socks5://` on its own, so the work here is deciding *which* URL to hand it.

About PAC files: evaluating one properly means running JavaScript, and
`FindProxyForURL` can branch on the host, the time of day, DNS results.
Shipping a JS engine for that is out of proportion, so `pac_proxies` does the
honest, limited thing - it reads the `PROXY host:port` literals out of the
file and returns them in order. If the script is more clever than that, the
result may be wrong, which is why the caller is told (`is_guess`) and can say
so in the UI.
"""

from __future__ import annotations

import re
import sys
import threading
import time
from dataclasses import dataclass

from .log import get_logger

log = get_logger(__name__)

SCHEMES = ("http://", "https://", "socks5://", "socks5h://", "socks4://")
#: `PROXY 10.0.0.1:8080` / `SOCKS5 10.0.0.1:1080` inside a PAC script
_PAC_ENTRY = re.compile(
    r"\b(PROXY|HTTPS?|SOCKS5?|SOCKS4)\s+([A-Za-z0-9_.\-]+:\d+)", re.IGNORECASE
)
_REGISTRY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


@dataclass(slots=True)
class SystemProxy:
    """What Windows' own settings say."""

    server: str | None = None       # "host:port" or "http=...;https=..."
    pac_url: str | None = None
    enabled: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.server) or bool(self.pac_url)


def normalise(value: str | None) -> str | None:
    """Add a scheme when the user typed a bare `host:port`."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if value.lower().startswith(SCHEMES):
        return value
    return f"http://{value}"


def is_socks(value: str | None) -> bool:
    return bool(value) and value.strip().lower().startswith(("socks4://", "socks5"))


def socks_available() -> bool:
    """SOCKS needs an extra package; without it httpx raises at request time."""
    try:
        import socksio  # noqa: F401
    except ImportError:
        return False
    return True


def system_proxy() -> SystemProxy:
    """Read the WinINET settings the rest of Windows uses."""
    if sys.platform != "win32":  # pragma: no cover - Windows only feature
        return SystemProxy()
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY) as key:
            def read(name: str):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except OSError:
                    return None

            return SystemProxy(
                server=read("ProxyServer"),
                pac_url=read("AutoConfigURL"),
                enabled=bool(read("ProxyEnable")),
            )
    except OSError as exc:
        log.debug("cannot read the system proxy: %s", exc)
        return SystemProxy()


def for_scheme(server: str | None, scheme: str = "https") -> str | None:
    """Pick one entry out of `http=a:1;https=b:2`, which WinINET allows."""
    if not server:
        return None
    if "=" not in server:
        return normalise(server)
    parts = dict(
        piece.split("=", 1) for piece in server.split(";") if "=" in piece
    )
    chosen = parts.get(scheme) or parts.get("http") or next(iter(parts.values()), None)
    return normalise(chosen)


def pac_proxies(script: str) -> list[str]:
    """Every `PROXY host:port` literal in a PAC file, in order.

    Best effort by design - see the module docstring.
    """
    found: list[str] = []
    for keyword, endpoint in _PAC_ENTRY.findall(script or ""):
        keyword = keyword.upper()
        prefix = "socks5://" if keyword.startswith("SOCKS") else "http://"
        candidate = prefix + endpoint
        if candidate not in found:
            found.append(candidate)
    return found


@dataclass(slots=True)
class Resolved:
    url: str | None = None
    source: str = "none"     # explicit | system | pac | none
    is_guess: bool = False   # True when it came out of a PAC script

    @property
    def needs_socks_package(self) -> bool:
        return is_socks(self.url) and not socks_available()


def resolve(explicit: str | None, *, use_system: bool = False,
            fetch_pac=None, scheme: str = "https") -> Resolved:
    """Work out the proxy URL for a download.

    `fetch_pac` is injected so the network call stays out of this module (and
    out of the tests).
    """
    direct = normalise(explicit)
    if direct:
        return Resolved(direct, "explicit")
    if not use_system:
        return Resolved()

    settings = system_proxy()
    if settings.pac_url and fetch_pac is not None:
        try:
            script = fetch_pac(settings.pac_url)
        except Exception as exc:  # noqa: BLE001 - a bad PAC must not stop a download
            log.info("could not read the PAC file: %s", exc)
            script = None
        candidates = pac_proxies(script or "")
        if candidates:
            return Resolved(candidates[0], "pac", is_guess=True)
    if settings.enabled and settings.server:
        return Resolved(for_scheme(settings.server, scheme), "system")
    return Resolved()


class PacCache:
    """The PAC script, fetched on a worker thread and remembered.

    `resolve` wants the script at the moment a download starts - which is the
    moment the user clicks OK, on the GUI thread. Fetching it there froze the
    window for as long as the proxy server took to answer, up to the timeout.

    So `read` never blocks. The first call starts a background fetch and
    returns what it has (nothing); that download falls back to the system
    proxy, which is what a PAC file almost always names anyway. Every later
    download gets the cached script until it goes stale.
    """

    TTL = 600.0

    def __init__(self, fetch=None, *, ttl: float | None = None) -> None:
        self._fetch = fetch or _http_get
        self.ttl = self.TTL if ttl is None else ttl
        self._lock = threading.Lock()
        self._url: str | None = None
        self._text = ""
        self._at = 0.0
        self._thread: threading.Thread | None = None

    def read(self, url: str) -> str:
        """The script for `url`, or "" while it is still being fetched."""
        with self._lock:
            fresh = url == self._url and (time.monotonic() - self._at) < self.ttl
            if fresh:
                return self._text
            busy = self._thread is not None and self._thread.is_alive()
            known = self._text if url == self._url else ""
            if busy:
                return known
            self._thread = threading.Thread(
                target=self._load, args=(url,), name="boltdown-pac", daemon=True
            )
            self._thread.start()
        return known

    def prefetch(self, url: str | None) -> None:
        """Warm the cache so the first download does not miss."""
        if url:
            self.read(url)

    def wait(self, timeout: float = 5.0) -> None:
        """Block until an in-flight fetch finishes. For tests and shutdown."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _load(self, url: str) -> None:
        text = ""
        try:
            text = self._fetch(url)
        except Exception as exc:  # noqa: BLE001 - a bad PAC never stops a download
            log.info("could not read the PAC file: %s", exc)
        # An empty result is cached too: a proxy that is down should be asked
        # again in ten minutes, not on every single download.
        with self._lock:
            self._url, self._text, self._at = url, text, time.monotonic()


def _http_get(url: str) -> str:
    import httpx

    return httpx.get(url, timeout=8.0).text
