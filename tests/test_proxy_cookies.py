"""Proxy selection and reading cookies out of a Chromium profile."""

from __future__ import annotations

import base64
import json

import pytest

from app.util import browser_cookies as bc
from app.util import proxy


# --------------------------------------------------------------------- proxy


@pytest.mark.parametrize(
    "value, expected",
    [
        ("127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://a:1", "http://a:1"),
        ("socks5://a:1", "socks5://a:1"),
        ("  ", None),
        (None, None),
    ],
)
def test_normalise(value, expected):
    assert proxy.normalise(value) == expected


def test_is_socks():
    assert proxy.is_socks("socks5://a:1") and proxy.is_socks("SOCKS5H://a:1")
    assert not proxy.is_socks("http://a:1") and not proxy.is_socks(None)


def test_picking_one_entry_out_of_the_wininet_string():
    server = "http=10.0.0.1:80;https=10.0.0.2:443;ftp=10.0.0.3:21"
    assert proxy.for_scheme(server, "https") == "http://10.0.0.2:443"
    assert proxy.for_scheme(server, "http") == "http://10.0.0.1:80"
    assert proxy.for_scheme("10.0.0.9:3128") == "http://10.0.0.9:3128"
    assert proxy.for_scheme(None) is None


PAC = """
function FindProxyForURL(url, host) {
  if (isPlainHostName(host)) return "DIRECT";
  if (dnsDomainIs(host, ".internal")) return "PROXY 10.0.0.5:8080";
  return "SOCKS5 10.0.0.6:1080; PROXY 10.0.0.7:3128; DIRECT";
}
"""


def test_pac_literals_are_read_in_order():
    assert proxy.pac_proxies(PAC) == [
        "http://10.0.0.5:8080", "socks5://10.0.0.6:1080", "http://10.0.0.7:3128"
    ]
    assert proxy.pac_proxies("function f() { return 'DIRECT'; }") == []
    assert proxy.pac_proxies("") == []


def test_resolve_prefers_what_the_user_typed():
    result = proxy.resolve("socks5://a:1", use_system=True)
    assert result.url == "socks5://a:1" and result.source == "explicit"
    assert not result.is_guess


def test_resolve_ignores_the_system_unless_asked():
    assert proxy.resolve(None).url is None
    assert proxy.resolve(None).source == "none"


def test_resolve_reads_a_pac_and_marks_it_a_guess(monkeypatch):
    monkeypatch.setattr(
        proxy, "system_proxy",
        lambda: proxy.SystemProxy(pac_url="http://wpad/proxy.pac", enabled=True),
    )
    result = proxy.resolve(None, use_system=True, fetch_pac=lambda url: PAC)
    assert result.url == "http://10.0.0.5:8080"
    assert result.source == "pac" and result.is_guess


def test_a_broken_pac_falls_back_to_the_plain_setting(monkeypatch):
    def boom(url):
        raise OSError("no route")

    monkeypatch.setattr(
        proxy, "system_proxy",
        lambda: proxy.SystemProxy(server="10.0.0.1:80", pac_url="http://wpad/x.pac",
                                  enabled=True),
    )
    result = proxy.resolve(None, use_system=True, fetch_pac=boom)
    assert result.url == "http://10.0.0.1:80" and result.source == "system"


def test_socks_without_the_package_is_flagged(monkeypatch):
    monkeypatch.setattr(proxy, "socks_available", lambda: False)
    assert proxy.resolve("socks5://a:1").needs_socks_package
    monkeypatch.setattr(proxy, "socks_available", lambda: True)
    assert not proxy.resolve("socks5://a:1").needs_socks_package


# ------------------------------------------------------------------- cookies


def test_the_aes_key_is_read_out_of_local_state():
    blob = b"DPAPI" + b"encrypted-key-bytes"
    state = json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(blob).decode()}})
    assert bc.encrypted_key(state) == b"encrypted-key-bytes"
    assert bc.encrypted_key("{}") is None
    assert bc.encrypted_key("not json") is None


def test_aes_gcm_values_round_trip():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = AESGCM.generate_key(bit_length=256)
    nonce = b"0123456789ab"
    blob = b"v10" + nonce + AESGCM(key).encrypt(nonce, b"session=abc123", None)
    assert bc.decrypt_value(blob, key) == "session=abc123"
    assert bc.decrypt_value(blob, AESGCM.generate_key(bit_length=256)) == ""
    assert bc.decrypt_value(b"", key) == ""


@pytest.mark.parametrize(
    "host_key, host, expected",
    [
        (".example.com", "example.com", True),
        (".example.com", "cdn.example.com", True),
        ("example.com", "example.com", True),
        (".example.com", "notexample.com", False),
        ("", "example.com", False),
    ],
)
def test_domain_matching_follows_the_leading_dot(host_key, host, expected):
    assert bc.domain_matches(host_key, host) is expected


def test_cookie_header_skips_empty_values():
    assert bc.cookie_header([("a", "1"), ("b", ""), ("", "2"), ("c", "3")]) == "a=1; c=3"


def test_reading_a_profile_that_is_not_there_is_quiet(tmp_path):
    browser = bc.Browser("Fake", tmp_path / "nope")
    assert browser.cookie_files() == []
    assert bc.read_cookies(browser, "example.com") == ""
