"""Reading published checksums and checking a download against them."""

from __future__ import annotations

import hashlib

import pytest

from app.util.checksums import (
    Expected,
    candidate_urls,
    discover,
    find_in_listing,
    parse_expected,
    verify,
    worth_checking,
)

SHA = hashlib.sha256(b"hello").hexdigest()
MD5 = hashlib.md5(b"hello").hexdigest()


@pytest.mark.parametrize("text, algorithm", [
    (SHA, "sha256"),
    (SHA.upper(), "sha256"),
    (f"sha256:{SHA}", "sha256"),
    (f"SHA-256: {SHA}", "sha256"),
    (f"{SHA}  ubuntu.iso", "sha256"),
    (f"{SHA} *ubuntu.iso", "sha256"),
    (f"SHA256 (ubuntu.iso) = {SHA}", "sha256"),
    (MD5, "md5"),
    (hashlib.sha1(b"x").hexdigest(), "sha1"),
    (hashlib.sha512(b"x").hexdigest(), "sha512"),
])
def test_what_people_paste_is_understood(text, algorithm):
    found = parse_expected(text)
    assert found is not None and found.algorithm == algorithm
    assert found.digest == found.digest.lower() and len(found.digest) in (32, 40, 64, 128)


@pytest.mark.parametrize("text", ["", "   ", "not a hash", "abc123", f"md5:{SHA}", "zz" * 32])
def test_nonsense_is_not_a_checksum(text):
    assert parse_expected(text) is None


def test_a_listing_gives_the_line_for_this_file():
    listing = (
        "# release hashes\n"
        f"{'1' * 64}  other.iso\n"
        f"{SHA} *dir/wanted.iso\n"
        f"SHA256 (third.iso) = {'2' * 64}\n"
    )
    found = find_in_listing(listing, "wanted.iso", "https://x/SHA256SUMS")
    assert found == Expected("sha256", SHA, "https://x/SHA256SUMS")
    bsd = find_in_listing(listing, "third.iso", "s")
    assert bsd is not None and bsd.digest == "2" * 64
    assert find_in_listing(listing, "missing.iso", "s") is None


def test_a_bare_hash_file_belongs_to_its_neighbour():
    found = find_in_listing(f"{SHA}\n", "anything.iso", "https://x/a.iso.sha256", "sha256")
    assert found is not None and found.digest == SHA


def test_signed_links_are_looked_up_without_their_signature():
    urls = [u for u, _ in candidate_urls("https://cdn.example/rel/v2/app.exe?sig=abc&exp=1")]
    assert urls[0] == "https://cdn.example/rel/v2/app.exe.sha256"
    assert "https://cdn.example/rel/v2/SHA256SUMS" in urls
    assert all("sig=" not in u for u in urls)
    assert candidate_urls("https://cdn.example/folder/") == []
    assert candidate_urls("ftp://x/a.iso") == []


def test_discovery_stops_at_the_first_listing_that_names_the_file():
    asked = []
    pages = {"https://x/rel/SHA256SUMS": f"{SHA}  app.iso\n"}

    def fetch(url):
        asked.append(url)
        return pages.get(url)

    found = discover(fetch, "https://x/rel/app.iso", "app.iso")
    assert found is not None and found.source == "https://x/rel/SHA256SUMS"
    assert asked[-1] == "https://x/rel/SHA256SUMS"
    assert discover(lambda url: None, "https://x/rel/app.iso", "app.iso") is None


def test_only_files_people_publish_hashes_for_are_looked_up():
    assert worth_checking("ubuntu.iso", 4 << 30)
    assert worth_checking("Setup.EXE", 5 << 20)
    assert not worth_checking("photo.jpg", 5 << 20)
    assert not worth_checking("tiny.zip", 1000)
    assert not worth_checking("noextension", 5 << 20)


def test_verify_compares_the_file(tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"hello")
    assert verify(path, Expected("sha256", SHA)).ok
    bad = verify(path, Expected("md5", "0" * 32))
    assert not bad.ok and bad.actual == MD5


# ------------------------------------------------------------- in the app

PySide6 = pytest.importorskip("PySide6")

from app.core.task import TaskState  # noqa: E402

from .conftest import make_payload  # noqa: E402
from .test_gui import pump, qapp, stack  # noqa: E402,F401 - fixtures


@pytest.fixture
def window(qapp, stack):
    from app.ui.main_window import MainWindow

    controller, settings, _db = stack
    settings.set("ask_before_download", False)
    win = MainWindow(controller, settings)
    win.notices = []
    win._notify = lambda title, body: win.notices.append((title, body))
    yield win
    win._ticker.stop()
    win.deleteLater()


def _finish(qapp, window, url, **kw):
    item = window.controller.add(url, **kw)
    assert pump(qapp, lambda: item.state is TaskState.COMPLETED)
    return item


def test_an_entered_checksum_is_confirmed(qapp, server, window):
    data = make_payload(300_000, seed=81)
    url = server.add_file("entered.bin", data)
    item = _finish(qapp, window, url, checksum=f"sha256:{hashlib.sha256(data).hexdigest()}")

    assert pump(qapp, lambda: any(t == "Checksum khớp" for t, _ in window.notices))
    row = window.controller.db.get_checksum(item.db_id)
    assert row["ok"] == 1 and row["source"] == "entered"
    assert item.error is None


def test_a_wrong_file_is_flagged_and_not_unpacked(qapp, server, window, monkeypatch):
    from app.util import postprocess

    data = make_payload(300_000, seed=82)
    url = server.add_file("tampered.zip", data)
    window.settings.set("auto_extract", True)
    monkeypatch.setattr(postprocess, "is_archive", lambda path: True)
    monkeypatch.setattr(postprocess, "extract",
                        lambda path: pytest.fail("unpacked a file that failed its checksum"))
    item = _finish(qapp, window, url, checksum="0" * 64)

    assert pump(qapp, lambda: any(t == "Checksum KHÔNG khớp" for t, _ in window.notices))
    assert item.error and "SHA256 mismatch" in item.error
    assert window.controller.db.get_checksum(item.db_id)["ok"] == 0


def test_a_published_listing_is_found_and_used(qapp, server, window):
    data = make_payload(1_200_000, seed=83)
    url = server.add_file("release.iso", data)
    server.add_file("SHA256SUMS", (
        f"{'1' * 64}  other.iso\n{hashlib.sha256(data).hexdigest()}  release.iso\n"
    ).encode())
    item = _finish(qapp, window, url)

    assert pump(qapp, lambda: any(t == "Checksum khớp" for t, _ in window.notices))
    row = window.controller.db.get_checksum(item.db_id)
    assert row["ok"] == 1 and row["source"].endswith("/SHA256SUMS")


def test_nothing_is_looked_up_when_switched_off(qapp, server, window):
    data = make_payload(1_200_000, seed=84)
    url = server.add_file("quiet.iso", data)
    window.settings.set("auto_checksum", False)
    before = server.state.requests["SHA256SUMS"]
    item = _finish(qapp, window, url)
    pump(qapp, lambda: False, timeout=0.5)
    assert server.state.requests["SHA256SUMS"] == before
    assert window.controller.db.get_checksum(item.db_id) is None


def test_a_checksum_that_is_not_one_is_refused(stack):
    controller, _settings, _db = stack
    with pytest.raises(ValueError):
        controller.add("https://example.invalid/a.iso", checksum="banana", start_now=False)


def test_the_add_dialog_passes_the_checksum_on(qapp, stack, monkeypatch):
    from app.ui import add_url_dialog
    from app.ui.add_url_dialog import AddUrlDialog

    _controller, settings, _db = stack
    warned = []
    monkeypatch.setattr(add_url_dialog.QMessageBox, "warning",
                        lambda *a, **k: warned.append(a[-1]))
    dialog = AddUrlDialog(settings, url="https://example.invalid/a.iso")
    dialog.checksum.setText("not a hash")
    assert dialog._validate() is False and warned
    dialog.checksum.setText(f"{SHA}  a.iso")
    assert dialog._validate() is True
    assert dialog.options()["checksum"] == f"{SHA}  a.iso"
    dialog.deleteLater()


def test_the_add_dialog_passes_mirrors_on(qapp, stack, monkeypatch):
    from app.ui import add_url_dialog
    from app.ui.add_url_dialog import AddUrlDialog

    _controller, settings, _db = stack
    monkeypatch.setattr(add_url_dialog.QMessageBox, "warning", lambda *a, **k: None)
    dialog = AddUrlDialog(settings, url="https://a.example/f.iso")
    dialog.mirrors.setPlainText("https://b.example/f.iso\n\n  https://c.example/f.iso  ")
    assert dialog._validate() is True
    assert dialog.options()["mirrors"] == ["https://b.example/f.iso", "https://c.example/f.iso"]
    dialog.mirrors.setPlainText("not a link")
    assert dialog._validate() is False
    dialog.deleteLater()
