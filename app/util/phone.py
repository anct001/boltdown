"""Tell the user's phone: a download finished, failed, or a queue is done.

Two services, both free and both a single HTTPS request:

* ntfy (ntfy.sh, or a server of one's own): no account; the phone app
  subscribes to a topic, and whoever knows the topic name can post to it -
  so the name should be long and unguessable.
* Telegram: a bot made with @BotFather, and the chat it should write to.

Sending happens on a thread of its own: a phone being slow to answer never
holds up the window or a download.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

import httpx

from .credentials import unprotect
from .log import get_logger

log = get_logger(__name__)

OFF, NTFY, TELEGRAM = "off", "ntfy", "telegram"
DEFAULT_NTFY = "https://ntfy.sh"
TIMEOUT = 10.0
#: what may be sent; each switched on or off in the settings
EVENTS = ("finished", "failed", "queue_done")


@dataclass(frozen=True, slots=True)
class Target:
    service: str = OFF
    ntfy_server: str = DEFAULT_NTFY
    ntfy_topic: str = ""
    telegram_token: str = ""
    telegram_chat: str = ""
    proxy: str | None = None

    @property
    def ready(self) -> bool:
        if self.service == NTFY:
            return bool(self.ntfy_topic.strip())
        if self.service == TELEGRAM:
            return bool(self.telegram_token.strip() and self.telegram_chat.strip())
        return False

    @classmethod
    def from_settings(cls, settings) -> "Target":
        return cls(
            service=str(settings.get("phone_service") or OFF),
            ntfy_server=str(settings.get("ntfy_server") or DEFAULT_NTFY),
            ntfy_topic=str(settings.get("ntfy_topic") or ""),
            telegram_token=unprotect(settings.get("telegram_token")),
            telegram_chat=str(settings.get("telegram_chat") or ""),
            proxy=settings.get("proxy") or None,
        )


def build_request(target: Target, title: str, body: str) -> httpx.Request:
    """The one request that carries the message - separate, so it is testable."""
    if target.service == NTFY:
        server = target.ntfy_server.rstrip("/") or DEFAULT_NTFY
        return httpx.Request(
            "POST", f"{server}/{target.ntfy_topic.strip()}",
            content=body.encode("utf-8"),
            # Header values must be latin-1; ntfy reads RFC 2047 for the rest.
            headers={"Title": _rfc2047(title), "Tags": "arrow_down"},
        )
    if target.service == TELEGRAM:
        return httpx.Request(
            "POST", f"https://api.telegram.org/bot{target.telegram_token.strip()}/sendMessage",
            json={"chat_id": target.telegram_chat.strip(), "text": f"{title}\n{body}",
                  "disable_web_page_preview": True},
        )
    raise ValueError(f"no phone service: {target.service!r}")


def _rfc2047(text: str) -> str:
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        import base64

        return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def send(target: Target, title: str, body: str, *, transport=None) -> str | None:
    """Send now; None when it went, otherwise what went wrong."""
    if not target.ready:
        return "not set up"
    try:
        request = build_request(target, title, body)
        with httpx.Client(timeout=TIMEOUT, proxy=target.proxy, transport=transport) as client:
            response = client.send(request)
        if response.status_code >= 400:
            return f"HTTP {response.status_code}"
    except (httpx.HTTPError, ValueError, OSError) as exc:
        return str(exc) or type(exc).__name__
    return None


def send_later(
    target: Target, title: str, body: str, done: Callable[[str | None], None] | None = None
) -> None:
    """Send on a thread; `done` hears the outcome (from that thread)."""
    def work() -> None:
        problem = send(target, title, body)
        if problem:
            log.warning("could not notify the phone: %s", problem)
        if done is not None:
            done(problem)

    threading.Thread(target=work, name="phone-notify", daemon=True).start()
