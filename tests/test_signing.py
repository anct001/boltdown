"""The signing helper: what it targets, and how it reports."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load(name: str):
    """Load a file from scripts/ - it is not an importable package."""
    path = PROJECT_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sign = load("sign")
build = load("build")


def test_targets_are_the_executables_then_the_installer(monkeypatch, tmp_path):
    app = tmp_path / "Boltdown"
    app.mkdir()
    for name in ("Boltdown.exe", "boltdown-cli.exe", "boltdown-host.exe"):
        (app / name).write_bytes(b"")
    setup = tmp_path / "BoltdownSetup-0.1.0.exe"
    setup.write_bytes(b"")
    (tmp_path / "notes.txt").write_text("ignored")

    monkeypatch.setattr(sign, "DIST", tmp_path)
    monkeypatch.setattr(sign, "APP_DIR", app)
    found = sign.targets()
    assert {p.name for p in found[:-1]} == {
        "Boltdown.exe", "boltdown-cli.exe", "boltdown-host.exe"
    }
    # The installer is signed last: it contains the others.
    assert found[-1] == setup


def test_targets_survive_a_missing_build(monkeypatch, tmp_path):
    monkeypatch.setattr(sign, "DIST", tmp_path)
    monkeypatch.setattr(sign, "APP_DIR", tmp_path / "nope")
    assert sign.targets() == []


def test_a_self_signed_chain_is_not_treated_as_a_failure(monkeypatch):
    """`UnknownError` only means the root is not trusted - the file is signed."""
    monkeypatch.setattr(
        sign, "powershell",
        lambda script, timeout=300: (0, "Boltdown.exe             UnknownError", ""),
    )
    assert sign.sign([Path("Boltdown.exe")], "AB", None) == 0


@pytest.mark.parametrize("status", ["HashMismatch", "NotSigned"])
def test_a_broken_signature_fails_the_build(monkeypatch, status):
    monkeypatch.setattr(
        sign, "powershell",
        lambda script, timeout=300: (0, f"Boltdown.exe             {status}", ""),
    )
    assert sign.sign([Path("Boltdown.exe")], "AB", None) == 1


def test_the_timestamp_server_can_be_switched_off(monkeypatch):
    seen: list[str] = []

    def fake(script, timeout=300):
        seen.append(script)
        return 0, "Boltdown.exe  Valid", ""

    monkeypatch.setattr(sign, "powershell", fake)
    sign.sign([Path("a.exe")], "AB", None)
    assert "TimestampServer" not in seen[-1]
    sign.sign([Path("a.exe")], "AB", sign.TIMESTAMP_URL)
    assert sign.TIMESTAMP_URL in seen[-1]


def test_signing_nothing_is_an_error(capsys):
    assert sign.sign([], "AB") == 1
    assert sign.verify([]) == 1


def test_missing_files_are_reported_before_any_signing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        sign, "powershell",
        lambda *a, **k: pytest.fail("PowerShell must not run for missing files"),
    )
    assert sign.main([str(tmp_path / "absent.exe")]) == 2
    assert "not found" in capsys.readouterr().err


def test_a_missing_certificate_is_explained(monkeypatch, capsys, tmp_path):
    target = tmp_path / "a.exe"
    target.write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sign, "find_cert", lambda thumbprint=None: None)
    assert sign.main([str(target)]) == 2
    assert "--make-cert" in capsys.readouterr().err


def test_build_signs_nothing_when_there_is_nothing_to_sign():
    assert build.sign_files([], None) == 0
    assert build.sign_files([Path("does-not-exist.exe")], None) == 0


def test_build_signs_the_executables_before_packing_them():
    """The installer must contain signed files, so the order is load-bearing."""
    import inspect

    source = inspect.getsource(build.main)
    assert "--sign" in source
    assert source.index("sign_files") < source.index("build_installer()"), (
        "signing must happen before Inno Setup packs the executables"
    )


def test_inno_setup_is_looked_for_where_winget_puts_it(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    candidates = [str(p) for p in build.iscc_candidates()]
    assert any(str(tmp_path) in c and "Inno Setup 6" in c for c in candidates)
    assert any("Program Files" in c for c in candidates)
