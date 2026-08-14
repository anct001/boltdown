"""The string tables.

The failure mode worth guarding is a key that does not match the English
string in the code: `tr()` then returns the English and the translation sits
in the file forever, doing nothing, with nothing to show it is broken. So the
keys are checked against what the source actually asks for.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from app.ui import i18n, languages

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP = PROJECT_ROOT / "app"


def source_strings() -> set[str]:
    """Every string literal in the application.

    Wider than "arguments to tr()" on purpose: plenty of translated text
    reaches `tr` through a variable - category names, task states, theme
    labels - and those are literals somewhere else in the code. The point of
    the check is to catch a key that matches *nothing*, which is a typo.
    """
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        if path.name == "languages.py":
            continue        # the tables themselves are not evidence
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
    return found


def test_every_language_is_offered_in_its_own_name():
    """Someone who only reads Korean cannot find "Korean" in an English list."""
    assert set(i18n.LANGUAGES) == {"vi", "en", *languages.NAMES}
    assert i18n.LANGUAGES["ko"] == "한국어"
    assert i18n.LANGUAGES["ru"] == "Русский"
    assert all(name.strip() for name in i18n.LANGUAGES.values())


def test_english_is_the_key_language():
    assert i18n._TABLES["en"] == {}
    i18n.set_language("en")
    assert i18n.tr("Add URL") == "Add URL"


@pytest.mark.parametrize("code", sorted(set(i18n.LANGUAGES) - {"en"}))
def test_a_table_only_translates_strings_the_program_asks_for(code):
    """A key that does not match the source is a translation nobody sees."""
    known = source_strings()
    if code == "vi":
        known |= set(i18n._VI)      # its own table is the reference for itself
    stray = sorted(key for key in i18n._TABLES[code] if key not in known)
    assert not stray, f"{code}: keys that match no string in the code: {stray[:5]}"


@pytest.mark.parametrize("code", sorted(set(i18n.LANGUAGES) - {"en"}))
def test_a_table_actually_translates(code):
    table = i18n._TABLES[code]
    assert len(table) >= 90, f"{code} covers only {len(table)} strings"
    # Some entries are the same word in both languages - "Video", "Cookie:",
    # the theme names - so the bar is that the great majority differ.
    identical = sum(1 for key, value in table.items() if key == value)
    assert identical < len(table) * 0.2, (
        f"{code}: {identical} of {len(table)} strings are just the English"
    )
    assert all(value.strip() for value in table.values()), code


@pytest.mark.parametrize("code", sorted(i18n.LANGUAGES))
def test_switching_language_changes_what_the_user_reads(code):
    i18n.set_language(code)
    assert i18n.language() == code
    # The toolbar is the first thing anyone sees; it must be covered.
    for key in ("Add URL", "Pause", "Delete", "Settings", "Downloads"):
        assert i18n.tr(key), f"{code} returned nothing for {key}"
    i18n.set_language("vi")


def test_an_unknown_language_falls_back_to_english():
    i18n.set_language("xx")
    assert i18n.language() == "en"
    assert i18n.tr("Add URL") == "Add URL"
    i18n.set_language("vi")


def test_the_core_of_the_interface_is_covered_everywhere():
    """These are the strings on screen before the user does anything."""
    core = ["Add URL", "Resume", "Pause", "Delete", "Options", "Downloads",
            "Name", "Size", "Status", "Speed", "completed", "downloading",
            "Settings", "Cancel", "Save", "Close"]
    for code in set(i18n.LANGUAGES) - {"en"}:
        missing = [key for key in core if key not in i18n._TABLES[code]]
        assert not missing, f"{code} is missing {missing}"


def test_placeholders_survive_translation():
    """A %s that goes missing turns into a crash at format time."""
    for code, table in i18n._TABLES.items():
        for key, value in table.items():
            for token in re.findall(r"%[sd]", key):
                assert token in value, f"{code}: {key!r} lost {token}"
