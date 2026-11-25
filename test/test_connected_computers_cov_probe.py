"""Connected computers: the confinement witness probe's answers, called in this process.

The probe is a standalone script; here its functions run unconfined against
witnesses under ``tmp_path``, so every answer it gives is the control answer.
Sockets are loopback or abstract only, listeners bind port 0, and the keyring
syscall goes to a stand-in ``libc``.
"""

from __future__ import annotations

import ctypes
import json
import os
import runpy
import secrets
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.connected_computers import confinement_probe as probe

pytestmark = [
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="the confinement probe runs on Linux only"
    ),
    # The keyring witness replaces ctypes.CDLL; the shared libc handle caches are reset.
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
]


@pytest.fixture
def base(tmp_path) -> Path:
    # Every component is opened with O_NOFOLLOW, so the witnesses need a real path.
    return Path(os.path.realpath(tmp_path))


@pytest.fixture
def tcp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        yield sock.getsockname()[1]
    finally:
        sock.close()


@pytest.fixture
def abstract_listener():
    name = "kirocrew-probe-test-" + secrets.token_hex(8)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind("\0" + name)
        sock.listen(4)
        yield name
    finally:
        sock.close()


class TestRead:
    def test_a_witness_file_is_read(self, base):
        witness = base / "w.txt"
        witness.write_text("witness-text", encoding="ascii")
        assert probe.read(str(witness)) == "witness-text"

    def test_a_missing_file_is_denied(self, base):
        assert probe.read(str(base / "missing.txt")) == "denied"

    def test_a_link_is_never_followed(self, base):
        target = base / "target.txt"
        target.write_text("behind-the-link", encoding="ascii")
        (base / "link").symlink_to(target)
        assert probe.read(str(base / "link")) == "denied"

    def test_a_linked_directory_component_is_never_followed(self, base):
        real = base / "real"
        real.mkdir()
        (real / "f.txt").write_text("inside", encoding="ascii")
        (base / "alias").symlink_to(real, target_is_directory=True)
        assert probe.read(str(base / "alias" / "f.txt")) == "denied"


class TestWrite:
    def test_a_new_file_beside_the_witness_is_allowed(self, base):
        assert probe.write(str(base / "new")) == "allowed"
        [made] = [p for p in base.iterdir() if p.name.startswith("new-")]
        assert made.read_text(encoding="ascii") == "x"
        assert len(made.name) == len("new-") + 32

    def test_a_write_into_a_missing_folder_is_denied(self, base):
        assert probe.write(str(base / "absent" / "new")) == "denied"
        assert not (base / "absent").exists()


class TestListing:
    def test_a_listed_name_is_allowed(self, base):
        (base / "seen.txt").write_text("", encoding="ascii")
        assert probe.listing(str(base), "seen.txt") == "allowed"

    def test_a_name_not_in_the_folder_is_denied(self, base):
        assert probe.listing(str(base), "not-here.txt") == "denied"

    def test_a_missing_folder_is_denied(self, base):
        assert probe.listing(str(base / "absent"), "x") == "denied"


class TestConnect:
    def test_a_loopback_listener_is_reachable(self, tcp_listener):
        assert probe.connect(socket.AF_INET, ("127.0.0.1", tcp_listener)) == "allowed"

    def test_an_abstract_listener_is_reachable(self, abstract_listener):
        assert probe.connect(socket.AF_UNIX, "\0" + abstract_listener) == "allowed"

    def test_an_abstract_name_nobody_holds_is_denied(self):
        name = "\0kirocrew-probe-test-absent-" + secrets.token_hex(8)
        assert probe.connect(socket.AF_UNIX, name) == "denied"

    def test_a_socket_that_cannot_be_made_is_denied(self, monkeypatch):
        def refuse(*_args):
            raise PermissionError("socket creation refused")

        monkeypatch.setattr(
            probe, "socket", SimpleNamespace(socket=refuse, SOCK_STREAM=socket.SOCK_STREAM)
        )
        assert probe.connect(socket.AF_INET, ("127.0.0.1", 1)) == "denied"


class _Libc:
    def __init__(self, answer: int) -> None:
        self.answer = answer
        self.calls: list[tuple] = []

    def syscall(self, *args):
        self.calls.append(tuple(a.value for a in args))
        return self.answer


class TestKeyring:
    @pytest.mark.parametrize("answer,expected", [(7, "allowed"), (0, "allowed"), (-1, "denied")])
    def test_the_session_keyring_read_maps_to_an_answer(self, monkeypatch, answer, expected):
        libc = _Libc(answer)
        opened = []

        def cdll(name, use_errno=False):
            opened.append((name, use_errno))
            return libc

        monkeypatch.setattr(ctypes, "CDLL", cdll)
        assert probe.keyring(250) == expected
        assert opened == [(None, True)]
        # KEYCTL_GET_KEYRING_ID (0) of the session keyring (-3), never creating one.
        assert libc.calls == [(250, 0, -3, 0)]

    def test_an_unavailable_libc_is_an_error_not_a_denial(self, monkeypatch):
        def cdll(*_args, **_kwargs):
            raise OSError("no libc here")

        monkeypatch.setattr(ctypes, "CDLL", cdll)
        assert probe.keyring(250) == "error"


def _spec(base: Path, tcp_port: int, ipc: str) -> dict:
    root = base / "root"
    out = base / "outside"
    scratch = base / "scratch"
    for folder in (root, out, scratch):
        folder.mkdir()
    (root / "f.txt").write_text("root-witness", encoding="ascii")
    (out / "f.txt").write_text("out-witness", encoding="ascii")
    (root / "link").symlink_to(out / "f.txt")
    return {
        "root_file": str(root / "f.txt"),
        "root_new": str(root / "new"),
        "out_file": str(out / "f.txt"),
        "out_new": str(out / "new"),
        "out_dir": str(out),
        "out_name": "f.txt",
        "link": str(root / "link"),
        "tcp_port": tcp_port,
        "scratch_name": "made",
        "ipc_abstract": ipc,
        "keyctl": 0,
    }


UNCONFINED = {
    "root_read": "root-witness",
    "root_write": "allowed",
    "out_read": "out-witness",
    "out_write": "allowed",
    "out_list": "allowed",
    "link_read": "denied",
    "net": "allowed",
    "scratch_write": "allowed",
    "ipc": "allowed",
    "keyring": "skip",
}


class TestRun:
    def test_an_unconfined_run_answers_the_control(
        self, base, tcp_listener, abstract_listener, monkeypatch
    ):
        spec = _spec(base, tcp_listener, abstract_listener)
        monkeypatch.setenv("TMPDIR", str(base / "scratch"))
        assert probe.run(spec) == UNCONFINED
        assert [p.name[:5] for p in (base / "scratch").iterdir()] == ["made-"]

    def test_a_keyring_number_runs_the_keyring_probe(
        self, base, tcp_listener, abstract_listener, monkeypatch
    ):
        spec = dict(_spec(base, tcp_listener, abstract_listener), keyctl=250)
        monkeypatch.setenv("TMPDIR", str(base / "scratch"))
        monkeypatch.setattr(ctypes, "CDLL", lambda *_a, **_k: _Libc(-1))
        assert probe.run(spec)["keyring"] == "denied"

    def test_the_script_prints_its_answers_as_one_json_line(
        self, base, tcp_listener, abstract_listener, monkeypatch, capsys
    ):
        spec = _spec(base, tcp_listener, abstract_listener)
        monkeypatch.setenv("TMPDIR", str(base / "scratch"))
        monkeypatch.setattr(sys, "argv", [probe.__file__, json.dumps(spec)])
        runpy.run_path(probe.__file__, run_name="__main__")
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == UNCONFINED
