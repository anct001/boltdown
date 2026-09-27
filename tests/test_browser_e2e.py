"""The browser integration, end to end, in a real Chromium.

Runs scripts/bench_browser.py and holds the results to what an IDM-class
integration has to manage. Skipped unless the machine has what it needs:
Linux, node, the `playwright` npm package on NODE_PATH, and a Chromium.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "scripts" / "bench_browser.py"


def _bench_module():
    spec = importlib.util.spec_from_file_location("bench_browser", BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ready() -> str | None:
    if sys.platform != "linux":
        return "Linux only"
    if shutil.which("node") is None:
        return "node is not installed"
    if _bench_module().find_node_modules() is None:
        return "the playwright npm package is not on NODE_PATH"
    chromium = os.environ.get("BOLTDOWN_CHROMIUM", "/opt/pw-browsers/chromium")
    if not Path(chromium).exists():
        return "no Chromium to drive"
    return None


@pytest.fixture(scope="module")
def bench(tmp_path_factory) -> dict:
    reason = _ready()
    if reason:
        pytest.skip(reason)
    out = tmp_path_factory.mktemp("bench") / "result.json"
    proc = subprocess.run(
        [sys.executable, str(BENCH), "--runs", "4", "--json", str(out)],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return json.loads(out.read_text(encoding="utf-8"))


def test_a_clicked_download_reaches_the_app_at_once(bench):
    times = [run["ms"] for run in bench["takeover"]]
    assert None not in times, bench["takeover"]
    # One host process for the session: no interpreter start per link.
    assert sorted(times)[len(times) // 2] < 100, bench["takeover"]


def test_the_browser_lets_go_of_the_file_quickly(bench):
    # paused while the app is asked, instead of downloading on regardless
    assert bench["wasted_kb_median"] < 1024, bench["takeover"]


def test_many_links_go_in_one_message(bench):
    assert bench["batch_links_handed"] >= 50
    assert bench["batch_messages"] == 1
    assert bench["batch_seconds"] < 1


def test_the_media_list_is_what_a_person_would_want(bench):
    assert bench["hls_playlist"] is True
    assert bench["hls_entries"] <= 3, "stream segments leaked into the list"
    assert bench["typed_media"] is True


def test_the_video_carries_its_own_download_button(bench):
    assert bench["video_button"] is True


def test_the_extension_logged_no_errors(bench):
    assert bench["worker_errors"] == []
