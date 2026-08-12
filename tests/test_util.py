from __future__ import annotations

import pytest

from app.util import filenames
from app.util.fmt import human_duration, human_size, human_speed, parse_size


def test_human_size():
    assert human_size(512) == "512 B"
    assert human_size(1024) == "1.00 KB"
    assert human_size(1536 * 1024) == "1.50 MB"
    assert human_size(None) == "?"


def test_human_speed_and_duration():
    assert human_speed(1024).endswith("/s")
    assert human_duration(65) == "01:05"
    assert human_duration(3725) == "1:02:05"
    assert human_duration(None) == "--:--"


@pytest.mark.parametrize(
    "text,expected",
    [("500", 500), ("2k", 2048), ("1.5M", int(1.5 * 1024**2)), ("1G", 1024**3),
     ("300kb", 300 * 1024)],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_sanitize_strips_invalid_characters():
    assert filenames.sanitize('a<b>c:d"e/f\\g|h?i*j.txt') == "a_b_c_d_e_f_g_h_i_j.txt"


def test_sanitize_handles_reserved_and_trailing_dots():
    assert filenames.sanitize("CON.txt") == "_CON.txt"
    assert filenames.sanitize("report.  ") == "report"
    assert filenames.sanitize("   ") == "download"


def test_filename_from_content_disposition_plain():
    header = 'attachment; filename="my report.zip"'
    assert filenames.from_content_disposition(header) == "my report.zip"


def test_filename_from_content_disposition_rfc5987():
    header = "attachment; filename=\"fallback.bin\"; filename*=UTF-8''b%C3%A1o%20c%C3%A1o.pdf"
    assert filenames.from_content_disposition(header) == "báo cáo.pdf"


def test_filename_from_content_disposition_strips_paths():
    header = 'attachment; filename="../../etc/passwd"'
    assert filenames.from_content_disposition(header) == "passwd"


def test_filename_from_url():
    assert filenames.from_url("https://host/a/b/setup%20x.exe?v=1") == "setup x.exe"
    assert filenames.from_url("https://host/") is None


def test_unique_path(tmp_path):
    target = tmp_path / "a.txt"
    assert filenames.unique_path(target) == target
    target.write_text("x")
    assert filenames.unique_path(target).name == "a (1).txt"
