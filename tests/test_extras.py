"""Clipboard capture, batch patterns, history, checksums, drop box, theme."""

from __future__ import annotations

import hashlib

import pytest

from app.util.patterns import MAX_URLS, PatternError, count, expand, parse

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.task import TaskState  # noqa: E402
from app.storage.db import Database  # noqa: E402
from app.storage.settings import Settings  # noqa: E402
from app.ui import i18n, theme  # noqa: E402
from app.ui.checksum_dialog import ChecksumDialog, hash_file, normalise  # noqa: E402
from app.ui.clipboard_watch import (  # noqa: E402
    ClipboardWatcher,
    extension_of,
    link_in,
    parse_extensions,
)
from app.ui.controller import Controller  # noqa: E402
from app.ui.dropbox import DropBox, urls_in  # noqa: E402
from app.ui.history_dialog import ROW_ROLE, HistoryDialog  # noqa: E402
from app.ui.batch_dialog import BatchDialog  # noqa: E402
from app.ui.task_model import state_color  # noqa: E402

from .conftest import make_payload  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def stack(qapp, tmp_path):
    db = Database(tmp_path / "extras.db")
    settings = Settings(db)
    settings.set("download_dir", str(tmp_path / "dl"))
    controller = Controller(db, settings)
    yield controller, settings, db
    db.close()


# ------------------------------------------------------------------ patterns


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("https://x/[1-3].jpg", ["https://x/1.jpg", "https://x/2.jpg", "https://x/3.jpg"]),
        ("https://x/[01-03].jpg",
         ["https://x/01.jpg", "https://x/02.jpg", "https://x/03.jpg"]),
        ("https://x/[a-c]/f.zip",
         ["https://x/a/f.zip", "https://x/b/f.zip", "https://x/c/f.zip"]),
        ("https://x/plain.zip", ["https://x/plain.zip"]),
    ],
)
def test_expand(pattern, expected):
    assert expand(pattern) == expected


def test_zero_padding_is_kept_at_the_right_width():
    urls = expand("https://x/img[008-011].jpg")
    assert urls[0].endswith("008.jpg") and urls[-1].endswith("011.jpg")


def test_a_descending_range_counts_down():
    assert expand("https://x/[3-1].t") == [
        "https://x/3.t", "https://x/2.t", "https://x/1.t"
    ]


def test_two_ranges_produce_every_combination():
    urls = expand("https://x/[1-2]/[a-b].zip")
    assert urls == [
        "https://x/1/a.zip", "https://x/1/b.zip",
        "https://x/2/a.zip", "https://x/2/b.zip",
    ]
    assert count("https://x/[1-2]/[a-b].zip") == 4


def test_an_absurd_pattern_is_refused_before_it_is_built():
    with pytest.raises(PatternError, match="limit"):
        expand("https://x/[1-999999].bin")
    assert count("https://x/[1-999999].bin") > MAX_URLS


def test_parse_skips_comments_and_duplicates():
    urls = parse(
        "https://x/a.zip\n"
        "# a note\n"
        "not a url\n"
        "https://x/a.zip\n"
        "  https://x/[1-2].bin  \n"
    )
    assert urls == ["https://x/a.zip", "https://x/1.bin", "https://x/2.bin"]


# ----------------------------------------------------------------- clipboard


@pytest.mark.parametrize(
    "text, extensions, expected",
    [
        ("https://x/a.zip", ("zip",), "https://x/a.zip"),
        ("  https://x/a.zip\t", ("zip",), "https://x/a.zip"),
        ("https://x/a.zip", (), "https://x/a.zip"),
        ("https://x/a.pdf", ("zip",), None),
        ("look at https://x/a.zip please", ("zip",), None),
        ("https://x/page", ("zip",), None),
        ("ftp://x/a.zip", ("zip",), None),
        ("", ("zip",), None),
        (None, ("zip",), None),
    ],
)
def test_link_in(text, extensions, expected):
    assert link_in(text, extensions) == expected


def test_extension_helpers():
    assert extension_of("https://x/a/b.TAR.GZ?token=1") == "gz"
    assert extension_of("https://x/a/b/") == ""
    assert parse_extensions(" zip, .RAR;7z ,, ") == ("zip", "rar", "7z")


def test_the_watcher_only_fires_for_new_links(stack):
    _controller, settings, _db = stack
    settings.set("clipboard_monitor", True)
    settings.set("clipboard_extensions", "zip")
    watcher = ClipboardWatcher(settings)
    caught: list[str] = []
    watcher.linkCaptured.connect(caught.append)

    assert watcher.handle("https://x/a.zip") == "https://x/a.zip"
    assert watcher.handle("https://x/a.zip") is None, "same text twice"
    assert watcher.handle("https://x/b.pdf") is None, "extension not watched"
    assert watcher.handle("https://x/b.zip") == "https://x/b.zip"
    assert caught == ["https://x/a.zip", "https://x/b.zip"]


def test_the_watcher_ignores_what_the_app_copied(stack):
    _controller, settings, _db = stack
    settings.set("clipboard_monitor", True)
    watcher = ClipboardWatcher(settings)
    watcher.ignore("https://x/mine.zip")
    assert watcher.handle("https://x/mine.zip") is None
    # Only once: copying it again by hand is a real request.
    assert watcher.handle("https://x/other.zip") == "https://x/other.zip"
    assert watcher.handle("https://x/mine.zip") == "https://x/mine.zip"


def test_the_watcher_does_nothing_while_disabled(stack):
    _controller, settings, _db = stack
    settings.set("clipboard_monitor", False)
    watcher = ClipboardWatcher(settings)
    assert watcher.handle("https://x/a.zip") is None
    watcher.set_enabled(True)
    assert watcher.handle("https://x/a.zip") == "https://x/a.zip"
    assert settings.get("clipboard_monitor") is True


# ------------------------------------------------------------------- history


def test_history_records_a_finished_download_once(tmp_path):
    with Database(tmp_path / "h.db") as db:
        db_id = db.add_download(url="https://x/a.zip", filename="a.zip", save_path=".")
        db.update_progress(db_id, 100, "completed")
        db.archive(db_id)
        db.archive(db_id)  # the row is removed later - still one history entry
        rows = db.list_history()
        assert len(rows) == 1 and rows[0]["filename"] == "a.zip"


def test_history_search_and_clearing(tmp_path):
    with Database(tmp_path / "h2.db") as db:
        for name in ("phim.mkv", "nhac.mp3", "tai-lieu.pdf"):
            db_id = db.add_download(
                url=f"https://x/{name}", filename=name, save_path="."
            )
            db.update_progress(db_id, 1, "completed")
            db.archive(db_id)
        assert len(db.list_history()) == 3
        assert [r["filename"] for r in db.list_history("nhac")] == ["nhac.mp3"]
        assert [r["filename"] for r in db.list_history("https://x/phim")] == ["phim.mkv"]

        first = db.list_history()[0]
        db.delete_history(first["id"])
        assert len(db.list_history()) == 2
        db.clear_history()
        assert db.list_history() == []


def test_the_controller_archives_on_completion(qapp, server, stack, tmp_path):
    controller, _settings, db = stack
    controller.start()
    try:
        data = make_payload(40_000, seed=950)
        url = server.add_file("extras/done.bin", data)
        item = controller.add(url, save_dir=tmp_path)
        import time

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and item.state is not TaskState.COMPLETED:
            qapp.processEvents()
            time.sleep(0.02)
        assert item.state is TaskState.COMPLETED
        assert [r["filename"] for r in db.list_history()] == ["done.bin"]
    finally:
        controller.shutdown()


def test_history_dialog_lists_and_forgets(qapp, stack, tmp_path):
    controller, _settings, db = stack
    for name in ("a.zip", "b.zip"):
        db_id = db.add_download(url=f"https://x/{name}", filename=name, save_path=".")
        db.update_progress(db_id, 1, "completed")
        db.archive(db_id)

    dialog = HistoryDialog(controller, db)
    try:
        assert dialog.table.topLevelItemCount() == 2
        dialog.search.setText("b.zip")
        assert dialog.table.topLevelItemCount() == 1
        dialog.table.setCurrentItem(dialog.table.topLevelItem(0))
        assert dialog.selected()[0]["filename"] == "b.zip"
        dialog.forget_selected()
        dialog.search.clear()
        assert [row["filename"] for row in _rows(dialog)] == ["a.zip"]
    finally:
        dialog.deleteLater()


def _rows(dialog: HistoryDialog) -> list[dict]:
    return [
        dialog.table.topLevelItem(i).data(0, ROW_ROLE)
        for i in range(dialog.table.topLevelItemCount())
    ]


# ------------------------------------------------------------------ checksum


def test_hash_file_matches_hashlib(tmp_path):
    path = tmp_path / "blob.bin"
    data = make_payload(300_000, seed=951)
    path.write_bytes(data)
    assert hash_file(path, "sha256") == hashlib.sha256(data).hexdigest()
    assert hash_file(path, "md5") == hashlib.md5(data).hexdigest()


def test_hashing_reports_progress_and_can_be_cancelled(tmp_path):
    import threading

    path = tmp_path / "blob.bin"
    path.write_bytes(make_payload(4 << 20, seed=952))
    seen: list[int] = []
    assert hash_file(path, "sha256", on_progress=seen.append) is not None
    assert seen and seen[-1] == 100

    stop = threading.Event()
    stop.set()
    assert hash_file(path, "sha256", stop=stop) is None


@pytest.mark.parametrize(
    "pasted, expected",
    [
        ("ABCD", "abcd"),
        ("  abcd  ", "abcd"),
        ("abcd  file.zip", "abcd"),
        ("", ""),
    ],
)
def test_normalise_accepts_how_people_paste_checksums(pasted, expected):
    assert normalise(pasted) == expected


def test_checksum_dialog_compares(qapp, tmp_path):
    path = tmp_path / "file.bin"
    data = make_payload(10_000, seed=953)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    dialog = ChecksumDialog(path)
    try:
        dialog._on_finished(digest)
        assert dialog.result.text() == digest
        # Pasted in upper case, with the file name after it - still a match.
        dialog.expected.setText(f"{digest.upper()}  file.bin")
        assert dialog.verdict.text() == i18n.tr("Match")
        dialog.expected.setText("0" * 64)
        assert dialog.verdict.text() == i18n.tr("Does NOT match")
    finally:
        dialog.deleteLater()


# ------------------------------------------------------------------ drop box


def test_urls_in_reads_both_url_and_text_drops():
    mime = QMimeData()
    mime.setUrls([QUrl("https://x/a.zip"), QUrl("file:///c:/local.txt")])
    assert urls_in(mime) == ["https://x/a.zip"]

    text = QMimeData()
    text.setText("https://x/b.zip\nnot a url\nhttps://x/c.zip")
    assert urls_in(text) == ["https://x/b.zip", "https://x/c.zip"]


def test_the_drop_box_remembers_where_it_was_parked(qapp, stack):
    _controller, settings, _db = stack
    box = DropBox(settings)
    try:
        box.move(321, 234)
        box.save_position()
        assert settings.get("dropbox_position") == [321, 234]

        again = DropBox(settings)
        assert (again.x(), again.y()) == (321, 234)
        again.deleteLater()

        box.hide_box()
        assert settings.get("dropbox_visible") is False
        box.show_box()
        assert settings.get("dropbox_visible") is True
    finally:
        box.deleteLater()


# --------------------------------------------------------------- batch dialog


def test_batch_dialog_previews_and_adds(qapp, stack, tmp_path):
    controller, settings, _db = stack
    added: list[dict] = []
    controller.add = lambda url, **kw: added.append({"url": url, **kw})

    dialog = BatchDialog(controller, settings)
    try:
        dialog.text.setPlainText("https://x/f[1-3].bin\nhttps://x/one.zip")
        assert dialog.preview.count() == 4
        assert "4" in dialog.summary.text()
        assert dialog.add_button.isEnabled()

        dialog.dir_edit.setText(str(tmp_path))
        dialog.connections.setValue(6)
        dialog.add_all()
        assert [a["url"] for a in added] == [
            "https://x/f1.bin", "https://x/f2.bin", "https://x/f3.bin", "https://x/one.zip"
        ]
        assert added[0]["connections"] == 6 and added[0]["start_now"] is True
    finally:
        dialog.deleteLater()


def test_batch_dialog_reports_a_bad_pattern(qapp, stack):
    controller, settings, _db = stack
    dialog = BatchDialog(controller, settings)
    try:
        dialog.text.setPlainText("https://x/[1-999999].bin")
        assert not dialog.add_button.isEnabled()
        assert "limit" in dialog.summary.text()
    finally:
        dialog.deleteLater()


# ---------------------------------------------------------------------- theme


def test_theme_resolves_the_preference(qapp):
    assert theme.resolve("light", qapp) is theme.LIGHT
    assert theme.resolve("dark", qapp) is theme.DARK
    assert theme.resolve("auto", qapp) in (theme.LIGHT, theme.DARK)


def test_applying_a_theme_changes_the_painted_colours(qapp):
    try:
        theme.apply(qapp, "dark")
        assert theme.current().is_dark
        dark_done = state_color(TaskState.COMPLETED)
        assert qapp.palette().window().color().lightness() < 100

        theme.apply(qapp, "light")
        assert not theme.current().is_dark
        assert state_color(TaskState.COMPLETED) != dark_done
        assert state_color(TaskState.ERROR) == theme.LIGHT.color("danger")
    finally:
        theme.apply(qapp, "auto")


def test_the_stylesheet_carries_every_token(qapp):
    sheet = theme.stylesheet(theme.DARK)
    for token in ("accent", "surface", "border", "muted"):
        assert getattr(theme.DARK, token) in sheet
    # A universal QWidget rule is what made Qt re-polish everything.
    assert "QWidget {" not in sheet
