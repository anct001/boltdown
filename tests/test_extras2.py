"""Post-processing, bandwidth windows, statistics and update checks."""

from __future__ import annotations

import zipfile
from datetime import date, datetime, timedelta

import pytest

from app.core.schedule import Schedule
from app.util import postprocess, updates

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.storage.db import Database  # noqa: E402
from app.ui.stats_dialog import StatsDialog, daily_totals, summarise  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ------------------------------------------------------------ post-processing


def make_zip(path, names=("a.txt", "sub/b.txt")):
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, f"contents of {name}")
    return path


def test_unpacking_puts_files_next_to_the_archive(tmp_path):
    archive = make_zip(tmp_path / "pack.zip")
    result = postprocess.extract(archive)
    assert result.ok
    target = tmp_path / "pack"
    assert (target / "a.txt").read_text().startswith("contents")
    assert (target / "sub" / "b.txt").exists()


def test_a_second_unpack_does_not_overwrite_the_first(tmp_path):
    archive = make_zip(tmp_path / "pack.zip")
    postprocess.extract(archive)
    postprocess.extract(archive)
    assert (tmp_path / "pack").is_dir() and (tmp_path / "pack (1)").is_dir()


def test_a_zip_that_escapes_its_folder_is_refused(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escaped.txt", "nope")
    result = postprocess.extract(archive)
    assert not result.ok and "unsafe" in result.detail
    assert not (tmp_path.parent / "escaped.txt").exists()


def test_only_real_archives_are_attempted(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("hello")
    assert not postprocess.is_archive(plain)
    assert not postprocess.extract(plain).ok
    assert not postprocess.extract(tmp_path / "missing.zip").ok


class _Completed:
    def __init__(self, code: int) -> None:
        self.returncode = code


@pytest.mark.parametrize(
    "code, ok, detail",
    [(0, True, "clean"), (2, False, "threat found"), (5, False, "scanner exited 5")],
)
def test_defender_exit_codes(tmp_path, monkeypatch, code, ok, detail):
    target = tmp_path / "file.bin"
    target.write_bytes(b"x")
    monkeypatch.setattr(postprocess, "defender_available", lambda: True)
    result = postprocess.scan(target, runner=lambda argv: _Completed(code))
    assert result.ok is ok and detail in result.detail


def test_scanning_without_defender_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(postprocess, "defender_available", lambda: False)
    assert not postprocess.scan(tmp_path / "x.bin").ok


# -------------------------------------------------------------- bandwidth


def at(hour, minute=0, day=12):
    return datetime(2026, 8, day, hour, minute)


def test_a_daytime_window_covers_only_its_hours():
    window = Schedule(start_at="08:00", stop_at="18:00")
    assert not window.covers(at(7, 59))
    assert window.covers(at(8, 0))
    assert window.covers(at(17, 59))
    assert not window.covers(at(18, 0))


def test_an_overnight_window_covers_past_midnight():
    window = Schedule(start_at="22:00", stop_at="06:00")
    assert window.covers(at(23, 0))
    assert window.covers(at(3, 0, day=13))
    assert not window.covers(at(7, 0, day=13))


def test_a_disabled_window_covers_nothing():
    assert not Schedule(start_at="08:00", stop_at="18:00", enabled=False).covers(at(9))
    assert not Schedule().covers(at(9))


# ------------------------------------------------------------------- stats


def test_daily_totals_bucket_by_day(tmp_path):
    today = date(2026, 8, 13)
    rows = [
        {"finished_at": datetime(2026, 8, 13, 9).timestamp(), "size": 100},
        {"finished_at": datetime(2026, 8, 13, 20).timestamp(), "size": 50},
        {"finished_at": datetime(2026, 8, 11, 8).timestamp(), "size": 7},
        {"finished_at": datetime(2020, 1, 1).timestamp(), "size": 999},  # too old
    ]
    totals = dict(daily_totals(rows, days=5, today=today))
    assert totals[today] == 150
    assert totals[date(2026, 8, 11)] == 7
    assert totals[date(2026, 8, 12)] == 0
    assert len(totals) == 5
    assert 999 not in totals.values()


def test_stats_dialog_reads_the_history(qapp, tmp_path):
    with Database(tmp_path / "stats.db") as db:
        for index, size in enumerate((1000, 2000, 3000)):
            db_id = db.add_download(
                url=f"https://x/{index}.bin", filename=f"{index}.bin",
                save_path=".", size=size,
            )
            db.execute("UPDATE downloads SET size = ? WHERE id = ?", (size, db_id))
            db.update_progress(db_id, size, "completed")
            db.archive(db_id)

        summary = summarise(db.list_history())
        assert summary.files == 3
        assert summary.total == 6000
        assert summary.average == 2000
        assert summary.largest_name == "2.bin" and summary.largest_size == 3000

        dialog = StatsDialog(db)
        try:
            assert dialog.summary.total == 6000
            assert dialog.chart._data  # the chart got its buckets
        finally:
            dialog.deleteLater()


def test_stats_survive_an_empty_history(qapp, tmp_path):
    with Database(tmp_path / "empty.db") as db:
        assert summarise(db.list_history()).files == 0
        dialog = StatsDialog(db)
        try:
            assert dialog.summary.total == 0
        finally:
            dialog.deleteLater()


# ------------------------------------------------------------------ updates


@pytest.mark.parametrize(
    "candidate, current, newer",
    [
        ("v0.3.0", "0.2.0", True),
        ("0.2.1", "0.2.0", True),
        ("v0.10.0", "0.9.0", True),      # string comparison would say no
        ("v0.2.0", "0.2.0", False),
        ("v0.1.0", "0.2.0", False),
        ("v1.0.0-beta", "0.9.9", True),
        (None, "0.2.0", False),
    ],
)
def test_version_comparison(candidate, current, newer):
    assert updates.is_newer(candidate, current) is newer


RELEASE = {
    "tag_name": "v0.3.0",
    "name": "Boltdown 0.3.0",
    "html_url": "https://github.com/x/y/releases/tag/v0.3.0",
    "body": "notes",
    "assets": [
        {"name": "source.zip", "browser_download_url": "https://x/source.zip", "size": 1},
        {"name": "BoltdownSetup-0.3.0.exe",
         "browser_download_url": "https://x/setup.exe", "size": 52_000_000},
    ],
}


def test_the_installer_asset_is_the_one_picked():
    release = updates.release_from_dict(RELEASE)
    assert release is not None and release.has_installer
    assert release.asset_name == "BoltdownSetup-0.3.0.exe"
    assert release.asset_url == "https://x/setup.exe"


def test_check_reports_only_when_it_is_newer():
    assert updates.check("0.2.0", fetch_json=lambda url: RELEASE).tag == "v0.3.0"
    assert updates.check("0.3.0", fetch_json=lambda url: RELEASE) is None
    assert updates.check("9.9.9", fetch_json=lambda url: RELEASE) is None


def test_an_unreachable_or_private_repo_is_not_an_error():
    def refuse(url):
        raise OSError("404 Not Found")

    assert updates.fetch_latest(fetch_json=refuse) is None
    assert updates.check("0.2.0", fetch_json=refuse) is None
    assert updates.release_from_dict({}) is None


# ------------------------------------------------- the browser integration dialog


def test_the_dialog_lists_every_browser_and_its_registration(qapp, tmp_path, monkeypatch):
    from app.storage.db import Database
    from app.storage.settings import Settings
    from app.ui import browser_dialog
    from app.ipc import register

    monkeypatch.setattr(
        register, "status",
        lambda *a, **k: {"chrome": r"C:\x\com.boltdown.host.json", "firefox": None},
    )
    db = Database(":memory:")
    dialog = browser_dialog.BrowserDialog(Settings(db))
    rows = [
        (dialog.tree.topLevelItem(i).text(0), dialog.tree.topLevelItem(i).text(1))
        for i in range(dialog.tree.topLevelItemCount())
    ]
    db.close()
    assert rows[0] == ("Google Chrome", r"C:\x\com.boltdown.host.json")
    assert rows[1][0] == "Firefox" and "đăng ký" in rows[1][1].lower()


def test_registering_from_the_dialog_always_covers_firefox(qapp, monkeypatch):
    """Firefox's id is fixed, so there is no reason to leave it out."""
    from app.storage.db import Database
    from app.storage.settings import Settings
    from app.ui import browser_dialog
    from app.ipc import register

    calls: list[list[str]] = []
    monkeypatch.setattr(register, "install", lambda ids, *a, **k: calls.append(list(ids)) or {})
    monkeypatch.setattr(register, "status", lambda *a, **k: {})

    db = Database(":memory:")
    settings = Settings(db)
    dialog = browser_dialog.BrowserDialog(settings)
    dialog.extension_id.setText("a" * 32)
    dialog.register_host()
    assert calls == [["a" * 32, register.DEFAULT_GECKO_ID]]
    assert settings.get("extension_id") == "a" * 32

    # With nothing typed at all, Firefox alone is still worth registering.
    calls.clear()
    dialog.extension_id.setText("")
    dialog.register_host()
    db.close()
    assert calls == [[register.DEFAULT_GECKO_ID]]


def test_a_typo_in_the_id_is_refused_not_registered(qapp, monkeypatch):
    from app.storage.db import Database
    from app.storage.settings import Settings
    from app.ui import browser_dialog
    from app.ipc import register

    monkeypatch.setattr(register, "status", lambda *a, **k: {})
    monkeypatch.setattr(
        register, "install",
        lambda *a, **k: pytest.fail("a bad id must never reach the registrar"),
    )
    warned = []
    monkeypatch.setattr(
        browser_dialog.QMessageBox, "warning",
        lambda *args, **kwargs: warned.append(args[2]),
    )
    db = Database(":memory:")
    dialog = browser_dialog.BrowserDialog(Settings(db))
    dialog.extension_id.setText("not-an-id")
    dialog.register_host()
    db.close()
    assert warned, "the user was told nothing"


def test_the_dialog_never_installs_the_extension_itself(qapp):
    """Browsers do not let a program add extensions behind the user's back,
    and this dialog must not pretend otherwise - it only opens folders."""
    import inspect

    from app.ui import browser_dialog

    source = inspect.getsource(browser_dialog)
    for forbidden in ("--load-extension", "ExtensionInstallForcelist", "webstore"):
        assert forbidden not in source
