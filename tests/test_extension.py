"""Static checks on the browser extension - it has no Python to unit test."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parent.parent / "extension"


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))


def test_manifest_is_v3_and_declares_what_the_code_uses(manifest):
    assert manifest["manifest_version"] == 3
    required = {
        "downloads",    # cancel the browser's transfer
        "cookies",      # authenticated downloads need the session cookie
        "storage",      # per-tab media lists survive worker eviction
        "tabs",
        "webRequest",   # observational media sniffing
        "nativeMessaging",
    }
    assert required <= set(manifest["permissions"])
    assert manifest["background"]["service_worker"] == "background.js"


def test_every_referenced_file_exists(manifest):
    referenced = [
        manifest["background"]["service_worker"],
        manifest["action"]["default_popup"],
        *manifest["icons"].values(),
        *manifest["action"]["default_icon"].values(),
    ]
    for entry in manifest["content_scripts"]:
        referenced.extend(entry["js"])
    for relative in referenced:
        assert (EXTENSION / relative).exists(), f"missing {relative}"


def test_popup_assets_exist():
    html = (EXTENSION / "popup" / "popup.html").read_text(encoding="utf-8")
    assert 'src="popup.js"' in html
    assert 'href="popup.css"' in html
    assert (EXTENSION / "popup" / "popup.js").exists()
    assert (EXTENSION / "popup" / "popup.css").exists()


def test_host_name_matches_the_python_side(manifest):
    from app.ipc.protocol import HOST_NAME

    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    assert f'"{HOST_NAME}"' in background


def test_icons_are_real_pngs(manifest):
    for relative in manifest["icons"].values():
        data = (EXTENSION / relative).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(data) > 100


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    "script", ["background.js", "content.js", "popup/popup.js"]
)
def test_javascript_parses(script):
    result = subprocess.run(
        ["node", "--check", str(EXTENSION / script)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
