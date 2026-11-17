"""The service PATH under ``sudo kirocrew service install``.

sudo replaces ``PATH`` with its ``secure_path``, so the installer's PATH names
no nvm dir and carries root's own dirs. The unit runs as ``SUDO_USER``, so its
PATH must name that account's nvm Node, read from the account's home, and none
of root's dirs.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.service import common
from kiro_crew.service.linux_unit import _home_for_user

pwd = pytest.importorskip("pwd")  # Unix-only module; Windows collects nothing here.

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the service PATH is colon-joined POSIX paths"
)

SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _node_versions(root: Path, *versions: str) -> None:
    for version in versions:
        bin_dir = root / "versions" / "node" / version / "bin"
        bin_dir.mkdir(parents=True)
        node = bin_dir / "node"
        node.write_text("#!/bin/sh\n", encoding="utf-8")
        node.chmod(0o755)


def _alias(root: Path, name: str, target: str) -> None:
    path = root / "alias" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(target + "\n", encoding="utf-8")


@pytest.fixture
def alice_home(tmp_path, monkeypatch):
    """A sudo install for ``alice``: her passwd home is under tmp_path."""
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    root_home = tmp_path / "root"
    accounts = {"alice": str(home), "root": str(root_home)}
    monkeypatch.setattr(
        pwd, "getpwnam", lambda name: SimpleNamespace(pw_dir=accounts[name], pw_uid=1000)
    )
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(pw_dir=str(root_home)))
    monkeypatch.setattr(common.sys, "platform", "linux")
    monkeypatch.setenv("SUDO_USER", "alice")
    monkeypatch.setenv("PATH", SECURE_PATH)
    for name in ("NVM_DIR", "MISE_DATA_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    return home


def _unit_path(account: str = "alice") -> list[str]:
    return common.service_environment(_home_for_user(account))["PATH"].split(":")


@pytest.mark.parametrize(
    "aliases",
    [
        {"default": "22"},
        {"default": "v22.22.2"},
        {"default": "lts/*", "lts/*": "lts/jod", "lts/jod": "v22.22.2"},
        {"default": "a", "a": "b", "b": "c", "c": "v22.22.2"},
    ],
)
def test_default_alias_picks_that_version(alice_home, aliases):
    nvm = alice_home / ".nvm"
    _node_versions(nvm, "v18.20.4", "v22.22.2", "v24.1.0")
    for name, target in aliases.items():
        _alias(nvm, name, target)

    dirs = _unit_path()

    want = str(nvm / "versions" / "node" / "v22.22.2" / "bin")
    assert want in dirs
    assert dirs.index(want) < dirs.index("/usr/bin")
    assert not any("v24.1.0" in d or "v18.20.4" in d for d in dirs)


def test_without_alias_the_newest_version_is_picked(alice_home):
    nvm = alice_home / ".config" / "nvm"
    _node_versions(nvm, "v9.11.2", "v18.20.4", "v22.22.2")

    dirs = _unit_path()

    want = str(nvm / "versions" / "node" / "v22.22.2" / "bin")
    assert dirs.index(want) < dirs.index("/usr/bin")
    assert not any("v9.11.2" in d or "v18.20.4" in d for d in dirs)


def test_alias_to_a_missing_version_falls_back_to_the_newest(alice_home):
    nvm = alice_home / ".nvm"
    _node_versions(nvm, "v18.20.4", "v22.22.2")
    _alias(nvm, "default", "20")

    dirs = _unit_path()

    assert str(nvm / "versions" / "node" / "v22.22.2" / "bin") in dirs


def test_default_alias_system_keeps_the_system_node(alice_home):
    nvm = alice_home / ".nvm"
    _node_versions(nvm, "v22.22.2")
    _alias(nvm, "default", "system")

    dirs = _unit_path()

    assert not any("versions/node" in d for d in dirs)


def test_root_home_entries_are_dropped(alice_home, tmp_path, monkeypatch):
    root_home = tmp_path / "root"
    monkeypatch.setenv("PATH", f"{root_home}/.local/bin:{root_home}/.toolbox/bin:{SECURE_PATH}")

    dirs = _unit_path()

    assert not any(d == str(root_home) or d.startswith(f"{root_home}/") for d in dirs)
    assert dirs[0] == f"{alice_home}/.local/bin"
    assert "/usr/bin" in dirs


def test_installer_nvm_dir_still_wins_over_the_home_scan(alice_home, monkeypatch):
    nvm = alice_home / ".nvm"
    _node_versions(nvm, "v18.20.4", "v22.22.2")
    on_path = str(nvm / "versions" / "node" / "v18.20.4" / "bin")
    monkeypatch.setenv("PATH", f"{on_path}:{SECURE_PATH}")

    dirs = _unit_path()

    assert dirs.index(on_path) < dirs.index("/usr/bin")
    assert str(nvm / "versions" / "node" / "v22.22.2" / "bin") not in dirs


def test_without_sudo_the_home_is_not_scanned(alice_home, monkeypatch):
    _node_versions(alice_home / ".nvm", "v22.22.2")
    monkeypatch.delenv("SUDO_USER")

    dirs = common.service_path(str(alice_home)).split(":")

    assert not any("/versions/node/" in d for d in dirs)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_fifo_alias_does_not_block_and_falls_back_to_the_newest(alice_home):
    nvm = alice_home / ".nvm"
    _node_versions(nvm, "v18.20.4", "v22.22.2")
    (nvm / "alias").mkdir()
    os.mkfifo(nvm / "alias" / "default")

    dirs = _unit_path()

    assert str(nvm / "versions" / "node" / "v22.22.2" / "bin") in dirs
