from __future__ import annotations

import io
import json
import struct
import sys

import pytest

from app.ipc import endpoint, native_host, register
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
    monkeypatch.setenv("IDMCLONE_HOME", str(tmp_path))
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

    prefix = "Software\\IDMCloneTest\\"
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
        _delete_tree(winreg, winreg.HKEY_CURRENT_USER, "Software\\IDMCloneTest")


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
            "IDMCLONE_HOME": str(ipc_home),
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
