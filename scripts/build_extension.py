"""Package the browser extension, once per browser family.

    python scripts/build_extension.py [--out dist/extension]

One source tree, two outputs, because the two families disagree about three
things and nothing else:

  * the background script - Chromium wants a service worker, Firefox runs a
    plain script (its MV3 service worker only arrived in 121, and a `scripts`
    entry works everywhere);
  * the add-on identity - Firefox needs `browser_specific_settings.gecko.id`,
    which is also the id the native-messaging manifest allows, so it has to be
    fixed rather than assigned at install time the way Chromium does it;
  * the packaging - a `.zip` to load unpacked or upload to the Web Store, and
    an `.xpi` (which is a zip with another name) for Firefox.

The extension's version is taken from the application, so a build never claims
to be a version that does not exist.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import __version__  # noqa: E402
from app.ipc.register import DEFAULT_GECKO_ID  # noqa: E402

SOURCE = PROJECT_ROOT / "extension"
#: the oldest Firefox with MV3, storage.session and native messaging as used here
MIN_FIREFOX = "115.0"
SKIP = {"__pycache__", ".DS_Store", "Thumbs.db"}


def chrome_manifest(manifest: dict) -> dict:
    manifest = json.loads(json.dumps(manifest))
    manifest["version"] = __version__
    return manifest


def firefox_manifest(manifest: dict) -> dict:
    manifest = json.loads(json.dumps(manifest))
    manifest["version"] = __version__
    manifest.pop("minimum_chrome_version", None)
    # A service worker would simply never start on Firefox 115.
    manifest["background"] = {"scripts": ["background.js"]}
    manifest["browser_specific_settings"] = {
        "gecko": {"id": DEFAULT_GECKO_ID, "strict_min_version": MIN_FIREFOX}
    }
    return manifest


def copy_tree(target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        SOURCE, target, ignore=shutil.ignore_patterns(*SKIP)
    )


def write_variant(out_dir: Path, name: str, manifest: dict, archive: Path) -> Path:
    folder = out_dir / name
    copy_tree(folder)
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder).as_posix())
    return folder


def build(out_dir: Path) -> list[tuple[str, Path, Path]]:
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    built = []
    for name, shaped, archive_name in (
        ("chrome", chrome_manifest(manifest), f"boltdown-chrome-{__version__}.zip"),
        ("firefox", firefox_manifest(manifest), f"boltdown-firefox-{__version__}.xpi"),
    ):
        archive = out_dir / archive_name
        folder = write_variant(out_dir, name, shaped, archive)
        built.append((name, folder, archive))
    return built


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(PROJECT_ROOT / "dist" / "extension"))
    args = parser.parse_args(argv)

    for name, folder, archive in build(Path(args.out).resolve()):
        size = archive.stat().st_size / 1024
        print(f"{name:8} {folder}  ->  {archive.name} ({size:.0f} KB)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
