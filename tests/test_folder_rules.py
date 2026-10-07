"""Site rules that pick the folder a download goes to."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.core.profiles import render_folder

WHEN = datetime(2026, 10, 7, 9, 30)
BASE = Path("/downloads")


@pytest.mark.parametrize("template, expected", [
    ("{host}", BASE / "github.com"),
    ("{host}/{year}-{month}", BASE / "github.com" / "2026-10"),
    ("Work\\{host}\\{date}", BASE / "Work" / "github.com" / "2026-10-07"),
    ("{category}/{day}", BASE / "Nén" / "07"),
    ("../../etc/{host}", BASE / "etc" / "github.com"),
    ("a:b<c>/{unknown}", BASE / "a_b_c_" / "{unknown}"),
    ("  ", BASE),
])
def test_relative_templates_sit_under_the_download_folder(template, expected):
    got = render_folder(template, "https://www.github.com/x/release.zip",
                        base=BASE, category="Nén", when=WHEN)
    assert got == expected


def test_an_absolute_template_is_used_as_it_is(tmp_path):
    root = tmp_path / "srv"  # absolute on any OS: C:\\... on Windows, /tmp/... elsewhere
    got = render_folder(f"{root}/{{host}}", "https://cdn.example/a.iso", base=BASE, when=WHEN)
    assert got == root / "cdn.example"


PySide6 = pytest.importorskip("PySide6")


def test_a_rule_sends_a_sites_downloads_to_its_folder(tmp_path):
    from app.storage.db import Database
    from app.storage.settings import Settings
    from app.ui.controller import Controller

    db = Database(tmp_path / "f.db")
    settings = Settings(db)
    settings.set("download_dir", str(tmp_path / "dl"))
    db.save_profile("*.github.com", folder="Dev/{host}")
    controller = Controller(db, settings)

    item = controller.add("https://objects.github.com/r/tool.zip", start_now=False)
    assert Path(item.save_path) == tmp_path / "dl" / "Dev" / "objects.github.com"
    other = controller.add("https://example.com/a.zip", start_now=False)
    assert Path(other.save_path) == tmp_path / "dl"
    chosen = controller.add("https://objects.github.com/r/b.zip", save_dir=tmp_path / "mine",
                            start_now=False)
    assert Path(chosen.save_path) == tmp_path / "mine", "a folder picked by hand wins"
    db.close()
