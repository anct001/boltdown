"""Register the native messaging host with the browsers on this machine.

A browser finds a host by reading `HKCU\\Software\\<Browser>\\
NativeMessagingHosts\\<host name>`, whose default value is the path of a JSON
manifest. The manifest names the executable to run and - critically - which
extensions may talk to it, so a hostile page cannot drive the download manager.

Firefox uses the same registry mechanism but its own dialect: extensions are
listed under `allowed_extensions` as add-on ids (`boltdown@anct001`), where
Chromium wants `allowed_origins` full of `chrome-extension://<32 letters>/`.
Two manifests are therefore written side by side, and which one a browser is
pointed at depends on which family it belongs to.

    python -m app.ipc.register --install <extension-id> [<extension-id> ...]
    python -m app.ipc.register --status
    python -m app.ipc.register --uninstall
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ..util.paths import data_dir
from .protocol import HOST_NAME

BROWSER_KEYS = {
    "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
    "chromium": r"Software\Chromium\NativeMessagingHosts",
    "brave": r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
    # Mozilla's key is shared by Firefox and its forks (LibreWolf, Waterfox).
    "firefox": r"Software\Mozilla\NativeMessagingHosts",
}
#: browsers that speak Firefox's dialect of the manifest
GECKO_BROWSERS = frozenset({"firefox"})

#: what Chrome shows under an unpacked extension: 32 letters, a-p only
EXTENSION_ID = re.compile(r"^[a-p]{32}$")
#: what a Firefox add-on calls itself: an email address or a {GUID}
GECKO_ID = re.compile(r"^([^@\s]+@[^@\s.]+(\.[^@\s.]+)*|\{[0-9a-fA-F-]{36}\})$")
#: the add-on id this project ships in the Firefox manifest
DEFAULT_GECKO_ID = "boltdown@anct001"
MANIFEST_NAME = f"{HOST_NAME}.json"
GECKO_MANIFEST_NAME = f"{HOST_NAME}.firefox.json"
LAUNCHER_NAME = "native_host.bat" if sys.platform == "win32" else "native_host.sh"
#: the console executable an installed build ships for native messaging
FROZEN_HOST_NAME = "boltdown-host.exe" if sys.platform == "win32" else "boltdown-host"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def valid_extension_id(value: str) -> bool:
    """True for either dialect of identifier."""
    value = value.strip()
    return bool(EXTENSION_ID.match(value) or GECKO_ID.match(value))


def is_gecko_id(value: str) -> bool:
    return bool(GECKO_ID.match(value.strip()))


def frozen_host() -> Path | None:
    """The packaged host executable, when running from an installed build.

    An installed copy has no interpreter to point a `.bat` at, and the GUI exe
    is windowless - native messaging needs real stdio, so the build ships a
    separate console executable next to it.
    """
    if not getattr(sys, "frozen", False):
        return None
    candidate = Path(sys.executable).parent / FROZEN_HOST_NAME
    return candidate if candidate.exists() else None


def write_launcher(target_dir: Path | None = None) -> Path:
    """Write the wrapper the browser executes.

    Chrome needs a real executable, so a source checkout gets a `.bat` shim. It
    must be `python.exe`, not `pythonw.exe`: the windowless build has no usable
    stdio, which is exactly what native messaging runs on.
    """
    packaged = frozen_host()
    if packaged is not None:
        return packaged
    target_dir = Path(target_dir) if target_dir else data_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / LAUNCHER_NAME
    root = _project_root()
    if sys.platform == "win32":
        body = (
            "@echo off\r\n"
            f'set "PYTHONPATH={root}"\r\n'
            f'"{sys.executable}" -m app.ipc.native_host %*\r\n'
        )
    else:
        body = (
            "#!/bin/sh\n"
            f'PYTHONPATH="{root}" exec "{sys.executable}" -m app.ipc.native_host "$@"\n'
        )
    path.write_text(body, encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(0o755)
    return path


def extension_dir(flavour: str = "chrome") -> Path | None:
    """The unpacked extension to load into the browser, or None.

    A packaged build carries both flavours next to the program; a checkout has
    the Chromium one in `extension/` (that is the source of truth) and gets the
    Firefox one from `scripts/build_extension.py`.
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).parent)
        candidate = base / "extension" / flavour
        if candidate.is_dir():
            return candidate
        legacy = base / "extension"  # a build made before the split
        return legacy if flavour == "chrome" and legacy.is_dir() else None
    root = _project_root()
    if flavour == "chrome":
        candidate = root / "extension"
        return candidate if candidate.is_dir() else None
    built = root / "dist" / "extension" / flavour
    return built if built.is_dir() else None


def build_manifest(extension_ids: list[str], launcher: Path, *, gecko: bool = False) -> dict:
    """The manifest for one browser family.

    Chromium identifies the caller by origin, Firefox by add-on id, and each
    rejects a manifest carrying the other's key - hence the two shapes.
    """
    manifest = {
        "name": HOST_NAME,
        "description": "Boltdown download manager integration",
        "path": str(launcher),
        "type": "stdio",
    }
    if gecko:
        ids = [eid for eid in extension_ids if is_gecko_id(eid)] or [DEFAULT_GECKO_ID]
        manifest["allowed_extensions"] = ids
    else:
        manifest["allowed_origins"] = [
            f"chrome-extension://{eid}/"
            for eid in extension_ids
            if EXTENSION_ID.match(eid.strip())
        ]
    return manifest


def write_manifest(
    extension_ids: list[str], target_dir: Path | None = None, *, gecko: bool = False
) -> Path:
    target_dir = Path(target_dir) if target_dir else data_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    launcher = write_launcher(target_dir)
    manifest = build_manifest(extension_ids, launcher, gecko=gecko)
    path = target_dir / (GECKO_MANIFEST_NAME if gecko else MANIFEST_NAME)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def install(
    extension_ids: list[str],
    browsers: list[str] | None = None,
    target_dir: Path | None = None,
    key_prefix: str = "",
) -> dict[str, str]:
    """Write the manifest and point every requested browser at it."""
    bad = [eid for eid in extension_ids if not valid_extension_id(eid)]
    if bad:
        raise ValueError(f"not valid extension ids: {', '.join(bad)}")
    if not extension_ids:
        raise ValueError("at least one extension id is required")

    wanted = [b for b in (browsers or list(BROWSER_KEYS)) if b in BROWSER_KEYS]
    chromium_ids = [e for e in extension_ids if EXTENSION_ID.match(e.strip())]
    manifests: dict[bool, Path] = {}
    results: dict[str, str] = {}
    for browser in wanted:
        gecko = browser in GECKO_BROWSERS
        # A Chromium browser with no Chromium id to allow would get a manifest
        # that permits nobody; say so instead of writing a useless key.
        if not gecko and not chromium_ids:
            results[browser] = "skipped: no chrome extension id given"
            continue
        if gecko not in manifests:
            manifests[gecko] = write_manifest(extension_ids, target_dir, gecko=gecko)
        try:
            _write_registry(f"{key_prefix}{BROWSER_KEYS[browser]}", str(manifests[gecko]))
            results[browser] = str(manifests[gecko])
        except OSError as exc:
            results[browser] = f"failed: {exc}"
    return results


def uninstall(browsers: list[str] | None = None, key_prefix: str = "") -> list[str]:
    removed = []
    for browser in browsers or list(BROWSER_KEYS):
        subkey = BROWSER_KEYS.get(browser)
        if subkey is None:
            continue
        if _delete_registry(f"{key_prefix}{subkey}"):
            removed.append(browser)
    return removed


def status(key_prefix: str = "") -> dict[str, str | None]:
    return {
        browser: _read_registry(f"{key_prefix}{subkey}")
        for browser, subkey in BROWSER_KEYS.items()
    }


# --------------------------------------------------------------------- registry


def _winreg():
    if sys.platform != "win32":  # pragma: no cover - Windows only feature
        raise OSError("native messaging registration requires Windows")
    import winreg

    return winreg


def _write_registry(subkey: str, value: str) -> None:
    winreg = _winreg()
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{subkey}\\{HOST_NAME}") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, value)


def _read_registry(subkey: str) -> str | None:
    winreg = _winreg()
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, f"{subkey}\\{HOST_NAME}"
        ) as key:
            return winreg.QueryValueEx(key, "")[0]
    except OSError:
        return None


def _delete_registry(subkey: str) -> bool:
    winreg = _winreg()
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{subkey}\\{HOST_NAME}")
        return True
    except OSError:
        return False


# -------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ipc.register")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--install", nargs="+", metavar="EXT_ID",
        help="extension id(s): 32 letters from chrome://extensions, and/or a "
             f"Firefox add-on id such as {DEFAULT_GECKO_ID}",
    )
    group.add_argument("--uninstall", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument(
        "--browser", action="append", choices=sorted(BROWSER_KEYS),
        help="limit to one browser (repeatable, default: all)",
    )
    args = parser.parse_args(argv)

    if args.status:
        for browser, value in status().items():
            print(f"{browser:9} {value or '-'}")
        return 0

    if args.uninstall:
        removed = uninstall(args.browser)
        print("removed:", ", ".join(removed) if removed else "nothing")
        return 0

    try:
        results = install(args.install, args.browser)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    for browser, value in results.items():
        print(f"{browser:9} {value}")
    print("\nNow reload the extension in the browser.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
