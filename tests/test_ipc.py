from __future__ import annotations

import io
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from app.ipc import endpoint, native_host, register

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="the registry is a Windows thing"
)
from app.ipc.protocol import (
    HOST_NAME,
    ProtocolError,
    decode_line,
    encode_line,
    encode_native,
    is_streaming_url,
    read_native,
    write_native,
)


# ------------------------------------------------------------------- protocol


def test_native_message_roundtrip():
    message = {"type": "download", "url": "https://example.com/a b.zip", "vi": "Tiếng Việt"}
    raw = encode_native(message)
    (length,) = struct.unpack("<I", raw[:4])
    assert length == len(raw) - 4
    assert read_native(io.BytesIO(raw)) == message


def test_read_native_returns_none_on_closed_pipe():
    assert read_native(io.BytesIO(b"")) is None
    assert read_native(io.BytesIO(b"\x02\x00")) is None  # truncated header


def test_read_native_rejects_oversized_declaration():
    header = struct.pack("<I", 50 * 1024 * 1024)
    with pytest.raises(ProtocolError):
        read_native(io.BytesIO(header + b"{}"))


def test_read_native_rejects_garbage_body():
    body = b"not json"
    with pytest.raises(ProtocolError):
        read_native(io.BytesIO(struct.pack("<I", len(body)) + body))


def test_write_native_prefixes_length():
    buffer = io.BytesIO()
    write_native(buffer, {"ok": True})
    assert buffer.getvalue() == encode_native({"ok": True})


def test_line_codec():
    assert decode_line(encode_line({"a": 1}).strip()) == {"a": 1}
    with pytest.raises(ProtocolError):
        decode_line(b"[1, 2]")  # must be an object
    with pytest.raises(ProtocolError):
        decode_line(b"{oops")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://h/v/master.m3u8", True),
        ("https://h/v/manifest.mpd?token=x", True),
        ("https://h/v/movie.mp4", False),
        ("https://h/m3u8.html", False),
    ],
)
def test_is_streaming_url(url, expected):
    assert is_streaming_url(url) is expected


# -------------------------------------------------------------------- endpoint


@pytest.fixture
def ipc_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOLTDOWN_HOME", str(tmp_path))
    return tmp_path


def test_server_roundtrip(ipc_home):
    seen = []

    def handler(message):
        seen.append(message)
        return {"ok": True, "echo": message.get("url")}

    with endpoint.IpcServer(handler) as server:
        assert server.port > 0
        published = json.loads((ipc_home / "ipc.json").read_text(encoding="utf-8"))
        assert published["port"] == server.port

        reply = endpoint.send({"type": "download", "url": "https://h/a.zip"})
        assert reply == {"ok": True, "echo": "https://h/a.zip"}
        assert seen == [{"type": "download", "url": "https://h/a.zip"}]
        assert "token" not in seen[0], "the token must not reach the handler"
        assert endpoint.is_running()

    assert not (ipc_home / "ipc.json").exists()
    assert endpoint.send({"type": "ping"}) is None


def test_server_rejects_a_wrong_token(ipc_home):
    called = []

    with endpoint.IpcServer(lambda m: called.append(m) or {"ok": True}) as server:
        import socket

        with socket.create_connection(("127.0.0.1", server.port), timeout=2) as sock:
            sock.sendall(encode_line({"type": "ping", "token": "wrong"}))
            with sock.makefile("rb") as stream:
                reply = decode_line(stream.readline().strip())
        assert reply["ok"] is False
        assert "token" in reply["error"]
        assert called == []


def test_handler_errors_become_a_reply(ipc_home):
    def boom(_message):
        raise RuntimeError("handler exploded")

    with endpoint.IpcServer(boom):
        reply = endpoint.send({"type": "ping"})
    assert reply["ok"] is False
    assert "exploded" in reply["error"]


def test_send_without_a_server_returns_none(ipc_home):
    assert endpoint.send({"type": "ping"}) is None
    assert endpoint.is_running() is False


def test_send_survives_a_stale_endpoint_file(ipc_home):
    (ipc_home / "ipc.json").write_text(
        json.dumps({"port": 1, "token": "x", "pid": 999999}), encoding="utf-8"
    )
    assert endpoint.send({"type": "ping"}) is None


# ----------------------------------------------------------------- native host


def test_deliver_forwards_to_a_running_app(monkeypatch):
    monkeypatch.setattr(endpoint, "send", lambda message, **kw: {"ok": True, "seen": message})
    assert native_host.deliver({"type": "ping"})["ok"] is True


def test_deliver_starts_the_app_when_nobody_answers(monkeypatch):
    attempts = {"n": 0}

    def fake_send(message, **kw):
        attempts["n"] += 1
        return {"ok": True} if attempts["n"] > 1 else None

    monkeypatch.setattr(endpoint, "send", fake_send)
    monkeypatch.setattr(native_host, "launch_app", lambda: True)
    monkeypatch.setattr(native_host, "LAUNCH_POLL", 0.01)
    assert native_host.deliver({"type": "ping"}) == {"ok": True}
    assert attempts["n"] == 2


def test_deliver_reports_a_failed_launch(monkeypatch):
    monkeypatch.setattr(endpoint, "send", lambda message, **kw: None)
    monkeypatch.setattr(native_host, "launch_app", lambda: False)
    reply = native_host.deliver({"type": "ping"})
    assert reply["ok"] is False


def test_host_loop_answers_every_message(monkeypatch):
    monkeypatch.setattr(native_host, "deliver", lambda message: {"ok": True, "n": message["n"]})
    stdin = io.BytesIO(encode_native({"n": 1}) + encode_native({"n": 2}))
    stdout = io.BytesIO()
    monkeypatch.setattr(native_host.sys, "stdin", type("S", (), {"buffer": stdin})())
    monkeypatch.setattr(native_host.sys, "stdout", type("S", (), {"buffer": stdout})())

    assert native_host.main([]) == 0

    stdout.seek(0)
    assert read_native(stdout) == {"ok": True, "n": 1}
    assert read_native(stdout) == {"ok": True, "n": 2}
    assert read_native(stdout) is None


# -------------------------------------------------------------------- register


def test_launcher_points_at_a_real_python(tmp_path):
    path = register.write_launcher(tmp_path)
    body = path.read_text(encoding="utf-8")
    assert sys.executable in body
    assert "app.ipc.native_host" in body
    # pythonw has no usable stdio, which native messaging depends on.
    assert "pythonw" not in body.lower()


def test_manifest_lists_only_the_given_extensions(tmp_path):
    extension_id = "a" * 32
    path = register.write_manifest([extension_id], tmp_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["name"] == HOST_NAME
    assert manifest["type"] == "stdio"
    assert manifest["allowed_origins"] == [f"chrome-extension://{extension_id}/"]
    assert (tmp_path / register.LAUNCHER_NAME).exists()


def test_install_rejects_bad_extension_ids(tmp_path):
    with pytest.raises(ValueError):
        register.install(["not-an-id"], target_dir=tmp_path)
    with pytest.raises(ValueError):
        register.install([], target_dir=tmp_path)
    assert register.valid_extension_id("z" * 32) is False  # only a-p are valid
    assert register.valid_extension_id("p" * 32) is True


@pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows only")
def test_install_and_uninstall_registry(tmp_path):
    import winreg

    prefix = "Software\\BoltdownTest\\"
    extension_id = "b" * 32
    try:
        results = register.install(
            [extension_id], browsers=["chrome", "edge"],
            target_dir=tmp_path, key_prefix=prefix,
        )
        assert set(results) == {"chrome", "edge"}
        assert all(register.MANIFEST_NAME in value for value in results.values())

        current = register.status(key_prefix=prefix)
        assert current["chrome"] == str(tmp_path / register.MANIFEST_NAME)
        assert current["brave"] is None

        removed = register.uninstall(["chrome", "edge"], key_prefix=prefix)
        assert set(removed) == {"chrome", "edge"}
        assert register.status(key_prefix=prefix)["chrome"] is None
    finally:
        _delete_tree(winreg, winreg.HKEY_CURRENT_USER, "Software\\BoltdownTest")


# ------------------------------------------------------------ end to end (host)


def test_launcher_relays_a_real_native_message(ipc_home, tmp_path):
    """Drive the exact artifact Chrome executes: the generated launcher script.

    Covers the whole chain - native framing over real pipes, discovery of the
    running app through ipc.json, and the token handshake.
    """
    import os
    import subprocess

    launcher = register.write_launcher(tmp_path)
    received = []

    def handler(message):
        received.append(message)
        return {"ok": True, "accepted": message.get("url")}

    with endpoint.IpcServer(handler):
        env = {
            **os.environ,
            "BOLTDOWN_HOME": str(ipc_home),
            "PYTHONIOENCODING": "utf-8",
        }
        proc = subprocess.Popen(
            [str(launcher)], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            proc.stdin.write(
                encode_native({"type": "download", "url": "https://h/setup.exe"})
            )
            proc.stdin.flush()
            reply = read_native(proc.stdout)
            proc.stdin.close()
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()

    assert reply == {"ok": True, "accepted": "https://h/setup.exe"}
    assert received == [{"type": "download", "url": "https://h/setup.exe"}]


def _delete_tree(winreg, root, path: str) -> None:
    try:
        key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        return
    with key:
        while True:
            try:
                child = winreg.EnumKey(key, 0)
            except OSError:
                break
            _delete_tree(winreg, root, f"{path}\\{child}")
    try:
        winreg.DeleteKey(root, path)
    except OSError:
        pass


# ------------------------------------------------- Firefox speaks its own dialect


def test_firefox_gets_a_manifest_it_can_actually_read(tmp_path):
    """`allowed_origins` and `allowed_extensions` are not interchangeable."""
    launcher = tmp_path / "native_host.bat"
    chromium = register.build_manifest(["a" * 32], launcher)
    gecko = register.build_manifest(["boltdown@anct001"], launcher, gecko=True)

    assert chromium["allowed_origins"] == ["chrome-extension://" + "a" * 32 + "/"]
    assert "allowed_extensions" not in chromium
    assert gecko["allowed_extensions"] == ["boltdown@anct001"]
    assert "allowed_origins" not in gecko
    assert gecko["path"] == str(launcher) and gecko["type"] == "stdio"


def test_a_chromium_id_never_leaks_into_the_firefox_manifest(tmp_path):
    gecko = register.build_manifest(
        ["a" * 32, "boltdown@anct001"], tmp_path / "h.bat", gecko=True
    )
    assert gecko["allowed_extensions"] == ["boltdown@anct001"]
    chromium = register.build_manifest(
        ["a" * 32, "boltdown@anct001"], tmp_path / "h.bat"
    )
    assert chromium["allowed_origins"] == ["chrome-extension://" + "a" * 32 + "/"]


def test_firefox_falls_back_to_the_id_this_project_ships(tmp_path):
    """Registering with only a Chrome id still leaves Firefox usable."""
    gecko = register.build_manifest(["a" * 32], tmp_path / "h.bat", gecko=True)
    assert gecko["allowed_extensions"] == [register.DEFAULT_GECKO_ID]


@pytest.mark.parametrize("value, ok", [
    ("a" * 32, True),
    ("boltdown@anct001", True),
    ("{d4d4d4d4-1111-2222-3333-444444444444}", True),
    ("z" * 32, False),          # q-z are not in Chrome's alphabet
    ("nope", False),
    ("", False),
    ("a" * 31, False),
])
def test_both_kinds_of_identifier_are_recognised(value, ok):
    assert register.valid_extension_id(value) is ok


@windows_only
def test_each_browser_family_is_pointed_at_its_own_manifest(tmp_path):
    prefix = "Software\\Boltdown\\test-families\\"
    try:
        results = register.install(
            ["a" * 32, "boltdown@anct001"], target_dir=tmp_path, key_prefix=prefix
        )
        state = register.status(key_prefix=prefix)
        assert state["firefox"].endswith(register.GECKO_MANIFEST_NAME)
        assert state["chrome"].endswith(register.MANIFEST_NAME)
        assert state["chrome"] != state["firefox"]
        assert set(results) == set(register.BROWSER_KEYS)

        # Both files are on disk and are valid JSON of the right shape.
        firefox = json.loads(Path(state["firefox"]).read_text(encoding="utf-8"))
        chrome = json.loads(Path(state["chrome"]).read_text(encoding="utf-8"))
        assert firefox["allowed_extensions"] == ["boltdown@anct001"]
        assert chrome["allowed_origins"] == ["chrome-extension://" + "a" * 32 + "/"]
    finally:
        register.uninstall(key_prefix=prefix)
    assert set(register.status(key_prefix=prefix).values()) == {None}


@windows_only
def test_a_chromium_browser_is_not_registered_without_a_chromium_id(tmp_path):
    """A manifest allowing nobody is worse than no manifest: say so."""
    prefix = "Software\\Boltdown\\test-gecko-only\\"
    try:
        results = register.install(
            ["boltdown@anct001"], target_dir=tmp_path, key_prefix=prefix
        )
        assert "skipped" in results["chrome"]
        assert results["firefox"].endswith(register.GECKO_MANIFEST_NAME)
        state = register.status(key_prefix=prefix)
        assert state["chrome"] is None and state["firefox"] is not None
    finally:
        register.uninstall(key_prefix=prefix)


def test_the_extension_folder_can_be_found_from_a_checkout():
    chrome = register.extension_dir("chrome")
    assert chrome is not None and (chrome / "manifest.json").is_file()
    assert (chrome / "background.js").is_file()


# ------------------------------------- the download must survive a missing host


HARNESS = Path(__file__).resolve().parent / "extension_harness.js"


def run_extension(mode: str, script: Path | None = None) -> dict:
    """Run background.js against a fake browser and report what it did."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is needed to run the extension")
    command = [node, str(HARNESS), mode]
    if script is not None:
        command.append(str(script))
    out = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_reachable_app_takes_the_download_over():
    trace = run_extension("ok")
    assert len(trace["native"]) == 1
    payload = trace["native"][0]
    assert payload["url"].endswith("big.iso")
    assert payload["cookie"] == "sid=abc", "cookies have to travel with the URL"
    assert payload["referer"] and payload["user_agent"]
    assert trace["cancelled"] == [7] and trace["erased"] == [7]


def test_an_unreachable_app_leaves_the_browser_download_alone():
    """The bug behind "downloads stopped working in the browser".

    The extension used to cancel first and ask afterwards, so when the native
    host was not registered - which is what a reinstall leaves behind - every
    download was cancelled and nothing replaced it.
    """
    trace = run_extension("fail")
    assert trace["native"], "it should still have tried"
    assert trace["cancelled"] == [], "the browser's own download was killed"
    assert trace["erased"] == []
    assert any("browser will fetch it" in message for message in trace["notified"])


def test_the_script_survives_a_browser_without_onDeterminingFilename():
    """The harness sets that event to undefined, as Firefox does; if the
    script blew up on it, nothing at all would be traced."""
    assert run_extension("ok")["native"], "the background script died on load"


# ----------------------------------------- repairing a lost registration


@windows_only
def test_the_registration_is_rewritten_when_it_goes_missing(tmp_path):
    """A reinstall runs --unregister-host and the installer cannot put it
    back, so the application does it from the id it remembers."""
    prefix = "Software\\Boltdown\\test-repair\\"
    try:
        assert register.repair(["a" * 32], key_prefix=prefix)
        state = register.status(key_prefix=prefix)
        assert state["edge"] and state["firefox"]

        # Already registered: repair must not touch a working setup.
        assert register.repair(["b" * 32], key_prefix=prefix) == {}
        assert register.status(key_prefix=prefix)["edge"] == state["edge"]

        # Only some of them missing - the case that actually happens, because
        # a browser the user does not have keeps its key while the ones they
        # do use lose theirs. An all-or-nothing check reads that leftover as
        # "everything is fine" and repairs nothing.
        register.uninstall(browsers=["chrome", "edge"], key_prefix=prefix)
        assert register.status(key_prefix=prefix)["chrome"] is None
        assert register.status(key_prefix=prefix)["brave"], "left as the decoy"

        repaired = register.repair(["a" * 32], key_prefix=prefix)
        assert set(repaired) == {"chrome", "edge"}, repaired
        after = register.status(key_prefix=prefix)
        assert all(after.values()), after
    finally:
        register.uninstall(key_prefix=prefix)


def test_repair_does_nothing_without_an_id():
    assert register.repair([]) == {}
    assert register.repair([None, "", "nonsense"]) == {}
