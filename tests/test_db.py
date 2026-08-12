from __future__ import annotations

from app.storage.db import SCHEMA_VERSION, Database


def test_schema_is_created(tmp_path):
    with Database(tmp_path / "test.db") as db:
        tables = {
            row["name"]
            for row in db.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"downloads", "download_headers", "queues", "schedules",
                "settings", "history", "meta"} <= tables
        version = db.query("SELECT value FROM meta WHERE key='schema_version'")
        assert version[0]["value"] == str(SCHEMA_VERSION)


def test_download_lifecycle(tmp_path):
    with Database(tmp_path / "test.db") as db:
        download_id = db.add_download(
            url="http://h/f.zip",
            filename="f.zip",
            save_path=str(tmp_path),
            size=1000,
            category="Compressed",
            cookie="a=b",
            referer="http://h/",
        )
        row = db.get_download(download_id)
        assert row["filename"] == "f.zip"
        assert row["state"] == "queued"
        assert row["downloaded"] == 0

        headers = db.query(
            "SELECT * FROM download_headers WHERE download_id = ?", (download_id,)
        )
        assert headers[0]["cookie"] == "a=b"

        db.update_progress(download_id, 1000, "completed")
        row = db.get_download(download_id)
        assert row["state"] == "completed"
        assert row["finished_at"] is not None

        db.archive(download_id)
        assert len(db.query("SELECT * FROM history")) == 1


def test_headers_cascade_on_delete(tmp_path):
    with Database(tmp_path / "test.db") as db:
        download_id = db.add_download(
            url="http://h/f", filename="f", save_path="d", cookie="x=1"
        )
        db.delete_download(download_id)
        assert db.query(
            "SELECT * FROM download_headers WHERE download_id = ?", (download_id,)
        ) == []


def test_list_downloads_filters_by_state(tmp_path):
    with Database(tmp_path / "test.db") as db:
        a = db.add_download(url="u1", filename="a", save_path="d")
        db.add_download(url="u2", filename="b", save_path="d")
        db.update_progress(a, 10, "error", error="boom")
        assert [r["filename"] for r in db.list_downloads("error")] == ["a"]
        assert len(db.list_downloads()) == 2
        assert db.get_download(a)["error"] == "boom"


def test_settings_roundtrip(tmp_path):
    with Database(tmp_path / "test.db") as db:
        db.set_setting("connections", 16)
        db.set_setting("categories", {"Video": "D:/Video"})
        assert db.get_setting("connections") == 16
        assert db.get_setting("categories")["Video"] == "D:/Video"
        assert db.get_setting("missing", "fallback") == "fallback"
        db.set_setting("connections", 32)
        assert db.get_setting("connections") == 32
