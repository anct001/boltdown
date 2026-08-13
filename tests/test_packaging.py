"""Packaging: autostart, frozen paths, and the build scripts staying in sync."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app.ipc import native_host, register
from app.util import autostart

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGING = PROJECT_ROOT / "packaging"
SPEC = PACKAGING / "boltdown.spec"
ISS = PACKAGING / "installer.iss"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def freeze(monkeypatch, exe: Path) -> None:
    """Pretend we are running from a packaged build at `exe`."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))


# ------------------------------------------------------------------ autostart


def test_launch_command_quotes_the_frozen_executable(monkeypatch, tmp_path):
    exe = tmp_path / "Boltdown.exe"
    exe.write_bytes(b"")
    freeze(monkeypatch, exe)
    command = autostart.launch_command()
    assert command == f'"{exe}" --tray'
    assert autostart.launcher() == exe


def test_launch_command_prefers_the_installed_console_script(monkeypatch, tmp_path):
    """From a checkout, the venv's launcher already knows where the code is."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    launcher = scripts / ("boltdown-gui.exe" if sys.platform == "win32" else "boltdown-gui")
    launcher.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    assert autostart.launcher() == launcher


def test_an_explicit_executable_wins(tmp_path):
    assert autostart.launch_command(tmp_path / "other.exe").endswith('other.exe" --tray')


@windows_only
def test_autostart_round_trip_in_the_registry():
    key = r"Software\Boltdown\test-autostart"
    try:
        assert not autostart.is_enabled(key)
        command = autostart.enable(Path(r"C:\fake\Boltdown.exe"), key)
        assert autostart.current(key) == command
        assert autostart.is_enabled(key)
        assert autostart.disable(key)
        assert not autostart.is_enabled(key)
        # Removing something that is not there is not an error.
        assert autostart.disable(key) is False
    finally:
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


@windows_only
def test_apply_mirrors_a_boolean():
    key = r"Software\Boltdown\test-apply"
    try:
        assert autostart.apply(True, Path(r"C:\fake\Boltdown.exe"), key)
        assert autostart.is_enabled(key)
        assert autostart.apply(False, key_path=key)
        assert not autostart.is_enabled(key)
    finally:
        import winreg

        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


# ------------------------------------------------------------- frozen layout


def test_frozen_host_is_only_used_when_it_exists(monkeypatch, tmp_path):
    assert register.frozen_host() is None  # we are not frozen right now

    freeze(monkeypatch, tmp_path / "Boltdown.exe")
    assert register.frozen_host() is None, "no host executable next to the app"

    host = tmp_path / register.FROZEN_HOST_NAME
    host.write_bytes(b"")
    assert register.frozen_host() == host


def test_a_packaged_manifest_points_at_the_host_executable(monkeypatch, tmp_path):
    host = tmp_path / register.FROZEN_HOST_NAME
    host.write_bytes(b"")
    freeze(monkeypatch, tmp_path / "Boltdown.exe")

    launcher = register.write_launcher(tmp_path / "data")
    assert launcher == host, "an installed build has no interpreter to shim"
    manifest = register.build_manifest(["a" * 32], launcher)
    assert manifest["path"] == str(host)
    assert manifest["allowed_origins"] == ["chrome-extension://" + "a" * 32 + "/"]


def test_a_source_checkout_still_gets_a_shim(tmp_path):
    launcher = register.write_launcher(tmp_path)
    assert launcher.name == register.LAUNCHER_NAME
    assert "native_host" in launcher.read_text(encoding="utf-8")


def test_the_host_starts_the_gui_not_itself(monkeypatch, tmp_path):
    """The bug this guards: `sys.executable` is the host, not the application."""
    gui = tmp_path / native_host.GUI_EXE_NAME
    gui.write_bytes(b"")
    freeze(monkeypatch, tmp_path / "boltdown-host.exe")

    command, cwd = native_host.app_command()
    assert command == [str(gui)]
    assert cwd == str(tmp_path)


def test_the_host_falls_back_to_a_module_run(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    command, cwd = native_host.app_command()
    assert command[1:] == ["-m", "app"]
    assert Path(cwd) == PROJECT_ROOT


# ------------------------------------------------------------- build scripts


def test_the_spec_and_entry_points_exist():
    assert SPEC.is_file() and ISS.is_file()
    # The icon is generated (scripts/make_app_icon.py), so it may not be here
    # on a fresh clone - but the generator must be.
    assert (PROJECT_ROOT / "scripts" / "make_app_icon.py").is_file()
    spec = SPEC.read_text(encoding="utf-8")
    for script in ("entry_gui.py", "entry_cli.py", "entry_host.py"):
        assert script in spec
        assert (PACKAGING / script).is_file()


def exe_names() -> list[str]:
    """The `name=` of every EXE() block (the COLLECT name is not an exe)."""
    spec = SPEC.read_text(encoding="utf-8")
    blocks = spec.split("= EXE(")[1:]
    return [re.search(r'name="([^"]+)"', block).group(1) for block in blocks]


def test_the_three_executables_have_case_distinct_names():
    """Windows file names are case-insensitive: `boltdown` would clobber the GUI."""
    names = exe_names()
    assert len(names) == 3
    lowered = [n.lower() for n in names]
    assert len(set(lowered)) == len(lowered), f"names collide on Windows: {names}"
    assert {"boltdown-host", "boltdown-cli"} <= set(names)


def test_the_installer_refers_to_the_names_the_spec_builds():
    spec = SPEC.read_text(encoding="utf-8")
    iss = ISS.read_text(encoding="utf-8")
    for name in ("Boltdown.exe", "boltdown-cli.exe", "boltdown-host.exe"):
        assert name in iss, f"{name} is missing from the installer script"
        assert name.rsplit(".", 1)[0] in spec
    assert register.FROZEN_HOST_NAME in iss
    # The uninstaller has to clean the registration the app may have written.
    assert "--unregister-host" in iss
    assert "--tray" in iss


def test_the_host_name_matches_what_the_spec_builds():
    spec = SPEC.read_text(encoding="utf-8")
    assert f'name="{register.FROZEN_HOST_NAME.removesuffix(".exe")}"' in spec


# ----------------------------------------------------------------- cli glue


def test_register_host_flags_reach_the_registrar(monkeypatch, capsys):
    from app.cli import main

    calls: list[list[str]] = []
    monkeypatch.setattr(
        register, "install", lambda ids, *a, **k: calls.append(list(ids)) or {"chrome": "ok"}
    )
    assert main(["--register-host", "b" * 32]) == 0
    assert calls == [["b" * 32]]
    assert "chrome" in capsys.readouterr().out


def test_a_bad_extension_id_is_reported(monkeypatch, capsys):
    from app.cli import main

    assert main(["--register-host", "nope"]) == 2
    assert "registration failed" in capsys.readouterr().err


def test_host_status_and_unregister(monkeypatch, capsys):
    from app.cli import main

    monkeypatch.setattr(register, "status", lambda *a, **k: {"chrome": None, "edge": "path"})
    monkeypatch.setattr(register, "uninstall", lambda *a, **k: ["edge"])
    assert main(["--host-status"]) == 0
    assert main(["--unregister-host"]) == 0
    out = capsys.readouterr().out
    assert "edge" in out and "removed" in out


def test_the_cli_still_needs_a_url():
    from app.cli import main

    with pytest.raises(SystemExit):
        main([])


# ------------------------------------------------------- the rename to Boltdown


def test_an_existing_profile_is_adopted_after_the_rename(tmp_path, monkeypatch):
    """A user upgrading from IDMClone keeps their database and settings."""
    from app.util import paths

    old = tmp_path / paths.LEGACY_NAME
    old.mkdir()
    (old / "boltdown.db").write_bytes(b"pretend database")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("BOLTDOWN_HOME", raising=False)
    monkeypatch.delenv(paths.LEGACY_ENV, raising=False)
    monkeypatch.setattr(paths, "is_portable", lambda: False)

    resolved = paths.data_dir()
    assert resolved == tmp_path / paths.APP_NAME
    assert (resolved / "boltdown.db").read_bytes() == b"pretend database"
    assert not old.exists(), "the old folder was moved, not copied"


def test_the_old_environment_variable_still_works(tmp_path, monkeypatch):
    from app.util import paths

    monkeypatch.delenv("BOLTDOWN_HOME", raising=False)
    monkeypatch.setenv(paths.LEGACY_ENV, str(tmp_path / "old-home"))
    assert paths.data_dir() == tmp_path / "old-home"


def test_a_download_interrupted_before_the_rename_still_resumes(tmp_path):
    """The sidecar changed name; the bytes on disk did not."""
    from app.core.resume import (
        LEGACY_SUFFIX,
        ResumeMeta,
        cleanup,
        legacy_meta_path_for,
        meta_path_for,
    )
    from app.core.segment import Segment

    part = tmp_path / "film.mkv.part"
    part.write_bytes(b"x" * 1024)
    meta = ResumeMeta(
        url="https://x/film.mkv", final_url="https://x/film.mkv",
        filename="film.mkv", size=4096, resumable=True,
        segments=[Segment(index=0, start=0, end=4095, done=1024)],
    )
    legacy = legacy_meta_path_for(part)
    assert legacy.name.endswith(LEGACY_SUFFIX)
    meta.save(legacy)

    loaded = ResumeMeta.load(meta_path_for(part))
    assert loaded is not None and loaded.downloaded == 1024

    # And cancelling removes both names, old and new.
    cleanup(part)
    assert not legacy.exists() and not part.exists()


def test_nothing_still_answers_to_the_old_name():
    """The rename has to be complete, or half the app looks for the wrong file."""
    import re

    skip = {".git", ".venv", "dist", "build", "__pycache__", ".pytest_cache"}
    pattern = re.compile(r"idmclone|IDMClone|IDMCLONE|idmdown|idmedia", re.IGNORECASE)
    offenders = []
    here = Path(__file__).resolve()
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or any(part in skip for part in path.parts):
            continue
        if path.suffix not in {".py", ".js", ".json", ".toml", ".iss", ".spec", ".md"}:
            continue
        if path.resolve() == here:
            continue  # this file has to spell the old name to search for it
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            # The compatibility shims are supposed to mention the old name.
            if any(word in line for word in
                   ("LEGACY", "legacy", "before the rename", "pre-rename",
                    "the previous version", "IDMClone (", "renamed")):
                continue
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {line.strip()}")
    assert not offenders, "old name still in:\n" + "\n".join(offenders[:15])
