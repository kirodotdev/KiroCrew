"""Snapshot and restore name the account that ran them, on Windows as on POSIX.

The SEL row ``_audit`` writes and the bundle's ``MANIFEST.json`` both record who ran the
command. Windows sets ``USERNAME`` and leaves ``USER`` unset, so a read of ``USER`` alone
names nobody there.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import snapshot_helpers
from snapshot_helpers import unpinnable_argv
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import sel as sel_mod
from kiro_crew import snapshot as snap
from kiro_crew import snapshot_build

_no_gateway = snapshot_helpers._no_gateway

#: Every variable ``getpass.getuser`` reads, cleared so the host's own login never leaks in.
_ACCOUNT_VARS = ("LOGNAME", "USER", "LNAME", "USERNAME")


def _account_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for var in _ACCOUNT_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, value in values.items():
        monkeypatch.setenv(var, value)


class _Recorder:
    def __init__(self) -> None:
        self.events: list = []

    def log(self, event) -> None:
        self.events.append(event)


def test_a_windows_shell_names_its_account_in_the_audit_row_and_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _setup_fake_kirocrew(home)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    _account_env(monkeypatch, USERNAME="alice")
    recorder = _Recorder()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)

    out = tmp_path / "out"
    assert snap.snapshot_main([str(out), *unpinnable_argv()]) == 0

    (archive,) = out.glob("kirocrew-snapshot-*.tar.gz")
    with tarfile.open(archive) as tf:
        (member,) = [m for m in tf.getmembers() if m.name.endswith("/MANIFEST.json")]
        manifest = json.loads(tf.extractfile(member).read())
    assert manifest["user"] == "alice"
    created = [e for e in recorder.events if e.event_type == "snapshot_created"]
    assert [e.caller_identity for e in created] == ["alice"]


@pytest.mark.parametrize(
    ("env", "os_login", "expected"),
    [
        ({"USER": "bob", "USERNAME": "alice"}, None, "bob"),
        ({}, "carol", "carol"),
        ({}, OSError("no login name"), "unknown"),
    ],
    ids=["user-wins", "os-login", "nothing-resolves"],
)
def test_the_caller_name_reads_user_then_username_then_the_os_login(
    monkeypatch: pytest.MonkeyPatch, env: dict, os_login, expected: str
) -> None:
    _account_env(monkeypatch, **env)

    def _getuser() -> str:
        if os_login is None:
            raise AssertionError("the OS login was read although a variable named the account")
        if isinstance(os_login, Exception):
            raise os_login
        return os_login

    # The owner's own binding, not the stdlib module every caller in the worker shares.
    monkeypatch.setattr(snapshot_build, "getpass", SimpleNamespace(getuser=_getuser))
    assert snap._caller_name() == expected
