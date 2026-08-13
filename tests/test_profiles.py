"""Per-host rules: which pattern wins, and what it fills in."""

from __future__ import annotations

import pytest

from app.core.profiles import SiteProfile, apply_to, host_of, match
from app.storage.db import Database


def profile(pattern: str, **kw) -> SiteProfile:
    return SiteProfile(pattern=pattern, **kw)


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.Example.com/a.zip", "example.com"),
        ("https://cdn.example.com:8443/a", "cdn.example.com"),
        ("not a url", ""),
    ],
)
def test_host_of(url, expected):
    assert host_of(url) == expected


@pytest.mark.parametrize(
    "pattern, host, expected",
    [
        ("example.com", "example.com", True),
        ("example.com", "cdn.example.com", False),
        ("*.example.com", "cdn.example.com", True),
        ("*.example.com", "example.com", True),
        ("*.example.com", "notexample.com", False),
        ("*", "anything.test", True),
        ("WWW.Example.com", "example.com", True),
        ("", "example.com", False),
    ],
)
def test_matching(pattern, host, expected):
    assert profile(pattern).matches(host) is expected


def test_the_narrowest_pattern_wins():
    profiles = [
        profile("*", connections=2),
        profile("*.example.com", connections=4),
        profile("cdn.example.com", connections=16),
    ]
    assert match("https://cdn.example.com/a.zip", profiles).connections == 16
    assert match("https://other.example.com/a.zip", profiles).connections == 4
    assert match("https://elsewhere.test/a.zip", profiles).connections == 2


def test_disabled_profiles_are_skipped():
    profiles = [profile("example.com", connections=16, enabled=False),
                profile("*", connections=2)]
    assert match("https://example.com/a", profiles).connections == 2
    assert match("https://example.com/a", [profiles[0]]) is None


def test_a_profile_only_fills_what_is_empty():
    profiles = [profile("example.com", connections=4, cookie="a=b",
                        referer="https://example.com/", speed_limit=500_000)]
    values = {"connections": 16, "cookie": None, "referer": "", "speed_limit": None,
              "user_agent": None, "proxy": None}
    merged = apply_to("https://example.com/f.zip", profiles, values)
    assert merged["connections"] == 16, "what the user chose survives"
    assert merged["cookie"] == "a=b"
    assert merged["referer"] == "https://example.com/"
    assert merged["speed_limit"] == 500_000


def test_no_match_changes_nothing():
    values = {"connections": 8, "cookie": None}
    assert apply_to("https://other.test/a", [profile("example.com")], values) == values


def test_profiles_round_trip_through_the_database(tmp_path):
    with Database(tmp_path / "p.db") as db:
        db.save_profile("example.com", connections=4, speed_limit=1024,
                        cookie="a=b", note="hay chặn")
        db.save_profile("*.cdn.test", connections=16, enabled=True)
        rows = db.list_profiles()
        assert [r["pattern"] for r in rows] == ["*.cdn.test", "example.com"]
        assert rows[1]["connections"] == 4 and rows[1]["note"] == "hay chặn"

        # Saving the same pattern updates rather than duplicating.
        db.save_profile("example.com", connections=2)
        rows = db.list_profiles()
        assert len(rows) == 2
        assert [r for r in rows if r["pattern"] == "example.com"][0]["connections"] == 2

        db.delete_profile(rows[0]["id"])
        assert len(db.list_profiles()) == 1
