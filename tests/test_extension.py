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


def test_extension_pages_reference_files_that_exist(manifest):
    import re

    assert (EXTENSION / manifest["options_ui"]["page"]).is_file()
    for page in PAGES:
        html = (EXTENSION / page).read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
            assert (EXTENSION / page).parent.joinpath(ref).resolve().is_file(), f"{page}: {ref}"


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
    "script", ["background.js", "content.js", "popup/popup.js", "picker/picker.js",
               "options/options.js"]
)
def test_javascript_parses(script):
    result = subprocess.run(
        ["node", "--check", str(EXTENSION / script)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


LOCALES = EXTENSION / "_locales"
SCRIPTS = ("background.js", "content.js", "popup/popup.js", "picker/picker.js",
           "options/options.js")
PAGES = ("popup/popup.html", "picker/picker.html", "options/options.html")


def _catalogue(lang: str) -> dict:
    return json.loads((LOCALES / lang / "messages.json").read_text(encoding="utf-8"))


def test_every_locale_has_the_same_keys_and_placeholders(manifest):
    assert (LOCALES / manifest["default_locale"] / "messages.json").is_file()
    english = _catalogue("en")
    for folder in LOCALES.iterdir():
        other = _catalogue(folder.name)
        assert set(other) == set(english), folder.name
        for key, entry in english.items():
            assert set(entry.get("placeholders", {})) == set(
                other[key].get("placeholders", {})
            ), f"{folder.name}:{key}"
            for name in entry.get("placeholders", {}):
                assert f"${name.upper()}$" in other[key]["message"], f"{folder.name}:{key}"


def test_every_message_the_code_asks_for_exists(manifest):
    import re

    english = _catalogue("en")
    wanted = set()
    for script in SCRIPTS:
        source = (EXTENSION / script).read_text(encoding="utf-8")
        wanted |= set(re.findall(r'\bt2?\("([A-Za-z0-9_]+)"', source))
    for page in PAGES:
        html = (EXTENSION / page).read_text(encoding="utf-8")
        wanted |= set(re.findall(r'data-i18n="([A-Za-z0-9_]+)"', html))
    texts = [manifest["name"], manifest["description"]]
    texts += [c["description"] for c in manifest.get("commands", {}).values()]
    for value in texts:
        wanted |= set(re.findall(r"__MSG_([A-Za-z0-9_]+)__", value))
    # the picker builds these names: kind_video, kind_audio, ...
    picker = (EXTENSION / "picker" / "picker.js").read_text(encoding="utf-8")
    kinds = re.findall(r'\["([a-z]+)", /', picker) + ["other"]
    wanted |= {f"kind_{kind}" for kind in kinds}
    assert wanted, "the extension is not localised any more?"
    assert wanted <= set(english), sorted(wanted - set(english))


def test_no_page_script_writes_html_from_strings():
    """Everything the extension shows comes from pages it does not control;
    building DOM with textContent is what keeps a hostile file name inert."""
    for script in SCRIPTS:
        source = (EXTENSION / script).read_text(encoding="utf-8")
        assert "innerHTML" not in source and "insertAdjacentHTML" not in source, script

