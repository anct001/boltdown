"""Build the Windows package.

    python scripts/build.py [--no-installer] [--clean]

Two steps, the second optional:

1. PyInstaller turns `packaging/boltdown.spec` into `dist/Boltdown/` - three
   executables sharing one copy of Qt.
2. Inno Setup (`ISCC.exe`) wraps that folder into `dist/BoltdownSetup-<ver>.exe`.
   Skipped with a note when Inno Setup is not installed, so the build still
   produces something usable on a machine without it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling scripts

from app import __version__  # noqa: E402

SPEC = PROJECT_ROOT / "packaging" / "boltdown.spec"
ISS = PROJECT_ROOT / "packaging" / "installer.iss"
DIST = PROJECT_ROOT / "dist"
BUILD = PROJECT_ROOT / "build"
APP_DIR = DIST / "Boltdown"

def iscc_candidates() -> list[Path]:
    """Where Inno Setup ends up, machine-wide or per-user.

    `winget install JRSoftware.InnoSetup` without elevation lands in
    %LOCALAPPDATA%\\Programs, which is not on PATH - so look there too.
    """
    roots = [
        Path(r"C:\Program Files (x86)"),
        Path(r"C:\Program Files"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
    ]
    return [
        root / f"Inno Setup {version}" / "ISCC.exe"
        for root in roots
        if str(root)
        for version in (6, 5)
    ]


def find_iscc() -> Path | None:
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return Path(found)
    for candidate in iscc_candidates():
        if candidate.is_file():
            return candidate
    return None


def run(argv: list[str], **kwargs) -> int:
    print("+", " ".join(argv), flush=True)
    return subprocess.call(argv, **kwargs)


def build_icon() -> None:
    icon = PROJECT_ROOT / "packaging" / "boltdown.ico"
    if icon.exists():
        return
    run([sys.executable, str(PROJECT_ROOT / "scripts" / "make_app_icon.py")])


def build_app(clean: bool) -> int:
    if clean:
        for path in (BUILD, APP_DIR):
            shutil.rmtree(path, ignore_errors=True)
    argv = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm"]
    if clean:
        argv.append("--clean")
    return run(argv, cwd=str(PROJECT_ROOT))


def build_installer() -> int:
    iscc = find_iscc()
    if iscc is None:
        print(
            "\nInno Setup not found - skipping the installer.\n"
            "Install it from https://jrsoftware.org/isdl.php and rerun, or "
            f"ship {APP_DIR} as a folder.",
            file=sys.stderr,
        )
        return 0
    return run(
        [str(iscc), f"/DMyAppVersion={__version__}", str(ISS)],
        cwd=str(PROJECT_ROOT),
    )


def sign_files(paths: list[Path], thumbprint: str | None) -> int:
    """Hand `paths` to scripts/sign.py.

    Order matters: the executables have to be signed *before* Inno Setup packs
    them, or the installed copies are the unsigned ones. The installer itself
    is signed afterwards.
    """
    existing = [p for p in paths if p.exists()]
    if not existing:
        return 0
    argv = [sys.executable, str(PROJECT_ROOT / "scripts" / "sign.py")]
    if thumbprint:
        argv += ["--thumbprint", thumbprint]
    return run(argv + [str(p) for p in existing], cwd=str(PROJECT_ROOT))


def describe(path: Path) -> str:
    if not path.exists():
        return f"{path} (missing)"
    if path.is_file():
        total = path.stat().st_size
    else:
        total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return f"{path}  ({total / 1048576:.1f} MB)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-installer", action="store_true")
    parser.add_argument("--clean", action="store_true", help="rebuild from scratch")
    parser.add_argument(
        "--sign", nargs="?", const="", default=None, metavar="THUMBPRINT",
        help="Authenticode-sign the executables and the installer "
             "(see scripts/sign.py)",
    )
    args = parser.parse_args(argv)

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    build_icon()

    # The extension is packaged first: it is what the user has to load into
    # their browser, and it costs a second.
    import build_extension

    for name, _folder, archive in build_extension.build(DIST / "extension"):
        print(f"extension:   {name:8} {archive.name}")

    code = build_app(args.clean)
    if code != 0:
        return code
    print("\napplication:", describe(APP_DIR))

    if args.sign is not None:
        code = sign_files(sorted(APP_DIR.glob("*.exe")), args.sign or None)
        if code != 0:
            return code

    if not args.no_installer:
        code = build_installer()
        if code != 0:
            return code
        setup = DIST / f"BoltdownSetup-{__version__}.exe"
        if setup.exists():
            if args.sign is not None:
                code = sign_files([setup], args.sign or None)
                if code != 0:
                    return code
            print("installer:  ", describe(setup))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
