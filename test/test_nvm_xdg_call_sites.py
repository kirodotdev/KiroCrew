"""nvm under the XDG layout, at the two call sites outside ``env``'s Node scan.

nvm's installer puts its root at ``$XDG_CONFIG_HOME/nvm`` (default
``~/.config/nvm``) when that variable is set. ``kiro_crew.env`` finds that root
for its own Node scan. Two more places look for nvm, and both read nvm's roots
from the same ``env._NVM_ROOTS`` list:

- ``kiro_crew.apps.backend._resolve_nvm_path`` sources ``<root>/nvm.sh`` to find
  the node an app backend runs on, through :func:`kiro_crew.env.nvm_dirs`;
- the Design Tweak backend's ``_node_bin_dirs`` puts nvm's version dirs ahead of
  its own list, through :func:`kiro_crew.env.nvm_version_bin_dirs`.

Each test builds its layout under ``tmp_path``. The backend tests run the real
``bash`` against a fake ``nvm.sh`` whose ``nvm`` function prints a node beside
that script, so the resolved path names the root that was sourced. No stdlib
attribute is patched.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import kiro_crew.apps.backend as bmod
from kiro_crew import env as env_mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.design_tweak.backend import server as dt_server

needs_bash = pytest.mark.skipif(
    platform_compat.IS_WINDOWS or shutil.which("bash") is None,
    reason="the nvm resolver is POSIX-only and sources nvm.sh through bash",
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake HOME (``USERPROFILE`` too, for Windows) with no nvm env set."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.delenv("NVM_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert os.path.expanduser("~") == str(home)
    return home


def _version_bin(root: Path, version: str) -> Path:
    d = root / "versions" / "node" / version / "bin"
    d.mkdir(parents=True)
    return d


def _fake_nvm(root: Path) -> Path:
    """An nvm root whose ``nvm which current`` prints ``<root>/bin/node``."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("node", "npm"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (root / "nvm.sh").write_text(
        f"nvm() {{ printf '%s\\n' '{bin_dir / 'node'}'; }}\n", encoding="utf-8"
    )
    return bin_dir


# --- the shared root list ---


def test_nvm_dirs_default_puts_legacy_first_then_xdg_default(home: Path) -> None:
    assert env_mod.nvm_dirs() == (str(home / ".nvm"), str(home / ".config" / "nvm"))


def test_nvm_dirs_follow_an_absolute_xdg_config_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert env_mod.nvm_dirs() == (str(home / ".nvm"), str(tmp_path / "xdg" / "nvm"))


def test_nvm_dirs_ignore_a_relative_xdg_config_home(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    assert env_mod.nvm_dirs() == (str(home / ".nvm"), str(home / ".config" / "nvm"))


def test_nvm_version_bin_dirs_is_nvms_share_of_the_all_versions_scan(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One mechanism: the helper the app uses returns exactly the nvm entries
    ``node_all_bin_dirs`` holds, in the same order, so the two cannot drift."""
    monkeypatch.setattr(env_mod, "_HOMEBREW_NODE_KEG_ROOT", str(tmp_path / "no-homebrew"))
    monkeypatch.delenv("MISE_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    legacy_old = _version_bin(home / ".nvm", "v9.11.2")
    legacy_new = _version_bin(home / ".nvm", "v20.1.0")
    xdg = _version_bin(home / ".config" / "nvm", "v26.3.1")
    asdf = home / ".asdf" / "installs" / "nodejs" / "22.0.0" / "bin"
    asdf.mkdir(parents=True)  # another manager: in the full scan, not nvm's share

    expected = (str(legacy_new), str(legacy_old), str(xdg))
    assert env_mod.nvm_version_bin_dirs() == expected
    env_mod._node_all_bin_dirs.cache_clear()
    try:
        all_dirs = env_mod.node_all_bin_dirs()
    finally:
        env_mod._node_all_bin_dirs.cache_clear()
    assert str(asdf) in all_dirs
    nvm_roots = env_mod.nvm_dirs()
    assert tuple(d for d in all_dirs if d.startswith(nvm_roots)) == expected


# --- apps/backend.py: _resolve_nvm_path ---


@needs_bash
@pytest.mark.parametrize("binary", ["node", "npm"])
def test_backend_finds_an_xdg_default_nvm_root(home: Path, binary: str) -> None:
    bin_dir = _fake_nvm(home / ".config" / "nvm")
    assert bmod._resolve_nvm_path(binary) == str(bin_dir / binary)


@needs_bash
def test_backend_follows_xdg_config_home_over_the_default(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = _fake_nvm(tmp_path / "xdg" / "nvm")
    _fake_nvm(home / ".config" / "nvm")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert bmod._resolve_nvm_path("node") == str(chosen / "node")


@needs_bash
def test_backend_ignores_a_relative_xdg_config_home(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_nvm(tmp_path / "relative-config" / "nvm")  # under the cwd
    default = _fake_nvm(home / ".config" / "nvm")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    assert bmod._resolve_nvm_path("node") == str(default / "node")


@needs_bash
def test_backend_prefers_the_legacy_nvm_root(home: Path) -> None:
    legacy = _fake_nvm(home / ".nvm")
    _fake_nvm(home / ".config" / "nvm")
    assert bmod._resolve_nvm_path("node") == str(legacy / "node")


@needs_bash
def test_backend_nvm_dir_wins_over_both_roots(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = _fake_nvm(tmp_path / "explicit")
    _fake_nvm(home / ".nvm")
    _fake_nvm(home / ".config" / "nvm")
    monkeypatch.setenv("NVM_DIR", str(tmp_path / "explicit"))
    assert bmod._resolve_nvm_path("node") == str(explicit / "node")


@needs_bash
def test_backend_nvm_dir_without_nvm_sh_does_not_fall_back(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``NVM_DIR`` is the only root, as before: no silent fallback."""
    _fake_nvm(home / ".config" / "nvm")
    monkeypatch.setenv("NVM_DIR", str(tmp_path / "absent"))
    assert bmod._resolve_nvm_path("node") is None


@needs_bash
def test_backend_treats_an_empty_nvm_dir_as_unset(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = _fake_nvm(home / ".config" / "nvm")
    monkeypatch.setenv("NVM_DIR", "")
    assert bmod._resolve_nvm_path("node") == str(default / "node")


@needs_bash
def test_backend_treats_an_empty_xdg_config_home_as_unset(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = _fake_nvm(home / ".config" / "nvm")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert bmod._resolve_nvm_path("node") == str(default / "node")


# The nvm root reaches bash as "$1", never inside the script text, so no root
# (XDG_CONFIG_HOME, HOME or NVM_DIR) is shell-expanded. Building the path into
# the `bash -c` string turns the first and third tests red. `source --` keeps a
# relative NVM_DIR that starts with `-` from being read as an option; dropping
# the `--` turns the leading-dash test red.


@needs_bash
def test_shell_metachar_in_xdg_root_is_not_expanded(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "PWNED"
    weird = tmp_path / f"$(touch {marker})"
    bin_dir = _fake_nvm(weird / "nvm")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(weird))
    assert bmod._resolve_nvm_path("node") == str(bin_dir / "node")
    assert not marker.exists(), "command substitution ran from XDG_CONFIG_HOME"


@needs_bash
def test_backend_sources_a_home_root_containing_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    spaced = tmp_path / "a home with spaces"
    spaced.mkdir()
    monkeypatch.setenv("HOME", str(spaced))
    monkeypatch.setenv("USERPROFILE", str(spaced))
    bin_dir = _fake_nvm(spaced / ".nvm")
    assert bmod._resolve_nvm_path("node") == str(bin_dir / "node")


@needs_bash
def test_backend_keeps_a_literal_dollar_home_in_nvm_dir(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    literal = tmp_path / "$HOME" / "nvm"
    bin_dir = _fake_nvm(literal)
    _fake_nvm(home / "nvm")  # where an expanded $HOME would point
    monkeypatch.setenv("NVM_DIR", str(literal))
    assert bmod._resolve_nvm_path("node") == str(bin_dir / "node")


@needs_bash
@pytest.mark.parametrize("name", ["-x", "-", "--no-use", "-nvm"])
def test_backend_sources_a_relative_nvm_dir_that_starts_with_a_dash(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # The fixture chdirs to tmp_path, so NVM_DIR=<name> is a relative root
    # whose nvm.sh path (`-x/nvm.sh`) begins with `-`.
    bin_dir = _fake_nvm(tmp_path / name)
    monkeypatch.setenv("NVM_DIR", name)
    assert bmod._resolve_nvm_path("node") == str(bin_dir / "node")


def test_backend_ends_option_parsing_before_the_nvm_path(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mutation pin that needs no bash: the script text must carry `source --`.
    _fake_nvm(home / ".nvm")
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: object) -> object:
        seen.append(argv)
        raise OSError("stop")

    monkeypatch.setattr(bmod.subprocess, "run", fake_run)
    monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", False)
    assert bmod._resolve_nvm_path("node") is None
    assert seen
    assert seen[0][2].startswith('source -- "$1"')
    assert seen[0][-1] == str(home / ".nvm" / "nvm.sh")


def test_backend_windows_still_returns_before_any_nvm_probe(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / ".config" / "nvm").mkdir(parents=True)
    (home / ".config" / "nvm" / "nvm.sh").write_text("# nvm\n", encoding="utf-8")
    monkeypatch.setattr(bmod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(bmod, "_nvm_dirs", lambda: pytest.fail("nvm roots read on Windows"))
    assert bmod._resolve_nvm_path("node") is None


# --- Design Tweak backend: _node_bin_dirs ---


@pytest.fixture
def dt_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``home`` with the app's own non-nvm list emptied, so only nvm shows."""
    monkeypatch.setattr(dt_server, "_NODE_BIN_DIRS", ())
    return home


def test_design_tweak_finds_an_xdg_default_nvm_root(dt_home: Path) -> None:
    d = _version_bin(dt_home / ".config" / "nvm", "v26.3.1")
    assert dt_server._node_bin_dirs() == [d]


def test_design_tweak_follows_xdg_config_home_over_the_default(
    dt_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chosen = _version_bin(tmp_path / "xdg" / "nvm", "v22.9.0")
    _version_bin(dt_home / ".config" / "nvm", "v24.0.0")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert dt_server._node_bin_dirs() == [chosen]


def test_design_tweak_ignores_a_relative_xdg_config_home(
    dt_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _version_bin(tmp_path / "relative-config" / "nvm", "v22.0.0")  # under the cwd
    default = _version_bin(dt_home / ".config" / "nvm", "v20.0.0")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    assert dt_server._node_bin_dirs() == [default]


def test_design_tweak_treats_an_empty_xdg_config_home_as_unset(
    dt_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default = _version_bin(dt_home / ".config" / "nvm", "v20.0.0")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert dt_server._node_bin_dirs() == [default]


def test_design_tweak_keeps_legacy_nvm_first_and_newest_first(dt_home: Path) -> None:
    """``~/.nvm`` before the XDG root, newest numeric version first in each:
    ``v20`` outranks ``v9``, which a plain string sort gets backwards."""
    legacy_old = _version_bin(dt_home / ".nvm", "v9.11.2")
    legacy_new = _version_bin(dt_home / ".nvm", "v20.1.0")
    xdg = _version_bin(dt_home / ".config" / "nvm", "v26.3.1")
    assert dt_server._node_bin_dirs() == [legacy_new, legacy_old, xdg]


def test_design_tweak_puts_nvm_ahead_of_its_own_list(
    dt_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = tmp_path / "own-bin"
    own.mkdir()
    monkeypatch.setattr(dt_server, "_NODE_BIN_DIRS", (str(own),))
    nvm = _version_bin(dt_home / ".config" / "nvm", "v26.3.1")
    assert dt_server._node_bin_dirs() == [nvm, own]


def test_design_tweak_reads_nvm_through_the_shared_core_helper() -> None:
    """No local nvm glob to drift from ``env``: the app's seam is the core scan."""
    assert dt_server._nvm_version_bin_dirs is env_mod.nvm_version_bin_dirs
    assert not hasattr(dt_server, "_NVM_GLOB")
