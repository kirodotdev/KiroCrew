"""Node build-toolchain resolution.

Kiro Crew's supported installer (``install.sh --mise`` / ``ensure-node.sh``)
installs node under ``$HOME``. Callers that build the SPA pin ``PATH`` to system
bin dirs for credential safety, so before this resolution existed they could not
see the very node Kiro Crew installed for them: ``kirocrew pod provision`` died
with an unhandled ``FileNotFoundError: 'npm'`` and Dev Fleet's Pull+Build
reported "no trusted executable for 'npm'".
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import env as env_mod
from kiro_crew import platform_compat


def _fake_node_bin(d: Path) -> Path:
    """Create *d* containing an executable ``node`` (and ``npm``).

    Platform-faithful naming matters: on Windows a bare extensionless ``node``
    is not an executable file, and real Windows toolchains ship ``node.exe`` /
    ``npm.cmd`` — which is exactly what ``_has_node`` and ``shutil.which``
    (via PATHEXT) look for. A POSIX-only fixture made every layout test here
    report an empty result on Windows CI.
    """
    d.mkdir(parents=True, exist_ok=True)
    names = ("node.exe", "npm.cmd") if platform_compat.IS_WINDOWS else ("node", "npm")
    for name in names:
        f = d / name
        f.write_text("@echo off\n" if name.endswith(".cmd") else "#!/bin/sh\nexit 0\n")
        f.chmod(0o755)
    return d


@pytest.fixture(autouse=True)
def _clear_caches():
    """``node_bin_dirs`` is lru_cached for the process lifetime — reset per test."""
    env_mod.node_bin_dirs.cache_clear()
    yield
    env_mod.node_bin_dirs.cache_clear()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """A HOME with no version manager and no ensure-node.sh marker.

    Sets USERPROFILE as well as HOME, and clears HOMEDRIVE/HOMEPATH: the
    resolver calls ``os.path.expanduser("~")``, and on Windows that reads
    USERPROFILE (then HOMEDRIVE+HOMEPATH) and **never HOME**. Patching only HOME
    left the resolver scanning the runner's real profile — which has no version
    managers — so every layout assertion below saw an empty result on Windows CI
    while passing on POSIX.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.delenv("KIROCREW_NODE_BIN_DIR", raising=False)
    monkeypatch.delenv("MISE_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    # No marker file: point data_home at an empty dir.
    monkeypatch.setattr(
        env_mod, "_marker_node_bin_dir", lambda: None, raising=True
    )
    return home


def test_isolated_home_actually_redirects_expanduser(isolated_home):
    """Non-vacuity guard for the fixture itself: if expanduser stops following
    it, every layout test silently degrades to scanning the real home."""
    assert os.path.expanduser("~") == str(isolated_home)


# --- the marker file ensure-node.sh writes ---
def test_marker_dir_is_preferred_over_a_filesystem_scan(isolated_home, tmp_path, monkeypatch):
    """ensure-node.sh records the version it decided on; that answer wins."""
    mise_node = isolated_home / ".local/share/mise/installs/node"
    _fake_node_bin(mise_node / "24.0.0" / "bin")
    marker_dir = _fake_node_bin(tmp_path / "chosen" / "bin")
    monkeypatch.setattr(
        env_mod, "_marker_node_bin_dir", lambda: str(marker_dir), raising=True
    )

    dirs = env_mod.node_bin_dirs()
    assert dirs[0] == str(marker_dir)
    # The scan still contributes, so a stale marker is not the only chance.
    assert str(mise_node / "24.0.0" / "bin") in dirs


def test_marker_carrying_a_path_separator_is_rejected(tmp_path, monkeypatch):
    """A multi-entry value would smuggle extra dirs into every PATH built from it."""
    home = tmp_path / "dh"
    home.mkdir()
    (home / "node-bin-dir").write_text(f"/opt/a/bin{os.pathsep}/tmp/evil/bin\n")
    monkeypatch.setattr(env_mod, "data_home", lambda: home, raising=True)
    assert env_mod._marker_node_bin_dir() is None


def test_relative_marker_is_rejected(tmp_path, monkeypatch):
    home = tmp_path / "dh"
    home.mkdir()
    (home / "node-bin-dir").write_text("relative/bin\n")
    monkeypatch.setattr(env_mod, "data_home", lambda: home, raising=True)
    assert env_mod._marker_node_bin_dir() is None


def test_absolute_marker_first_line_is_taken(tmp_path, monkeypatch):
    home = tmp_path / "dh"
    home.mkdir()
    (home / "node-bin-dir").write_text("/opt/node/bin\ntrailing junk\n")
    monkeypatch.setattr(env_mod, "data_home", lambda: home, raising=True)
    assert env_mod._marker_node_bin_dir() == "/opt/node/bin"


def test_missing_marker_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "data_home", lambda: tmp_path / "nope", raising=True)
    assert env_mod._marker_node_bin_dir() is None


# --- operator override ---
def test_env_override_wins(isolated_home, tmp_path, monkeypatch):
    override = _fake_node_bin(tmp_path / "operator" / "bin")
    monkeypatch.setenv("KIROCREW_NODE_BIN_DIR", str(override))
    assert env_mod.node_bin_dirs()[0] == str(override)


def test_relative_env_override_is_ignored(isolated_home, monkeypatch):
    monkeypatch.setenv("KIROCREW_NODE_BIN_DIR", "some/relative/bin")
    assert "some/relative/bin" not in env_mod.node_bin_dirs()


def test_env_override_carrying_a_path_separator_is_rejected(isolated_home, monkeypatch):
    """The override and the marker must validate IDENTICALLY.

    `os.path.isabs("/a:/b")` is True on POSIX, so an absolute-only check would
    let the override contribute two PATH entries where the marker refuses one.
    """
    monkeypatch.setenv("KIROCREW_NODE_BIN_DIR", f"/opt/a/bin{os.pathsep}/tmp/evil/bin")
    assert env_mod.node_bin_dirs() == ()


@pytest.mark.parametrize("bad", [
    "",
    "   ",
    "relative/bin",
    "/opt/a/bin:/tmp/evil/bin" if os.pathsep == ":" else "/opt/a/bin;/tmp/evil",
    "/opt/\0/bin",
])
def test_validated_bin_dir_rejects(bad):
    assert env_mod._validated_bin_dir(bad) is None


def test_validated_bin_dir_accepts_and_strips():
    assert env_mod._validated_bin_dir("  /opt/node/bin  ") == "/opt/node/bin"


def test_data_home_is_imported_at_module_scope():
    """The marker read uses a top-level import (AUTOSDE `top-level-imports`).

    Proven by attribute presence rather than by reading the source: a reverted
    lazy import would leave `env.data_home` undefined.
    """
    assert callable(env_mod.data_home)


# --- version selection ---
def test_only_the_best_version_per_root_is_returned(isolated_home):
    """~18 mise install+alias dirs on a real box would bury the right toolchain."""
    root = isolated_home / ".local/share/mise/installs/node"
    for name in ("16.20.2", "18", "22.22.2", "24.16.0", "lts", "lts-krypton", "latest"):
        _fake_node_bin(root / name / "bin")

    dirs = env_mod.node_bin_dirs()
    mise_entries = [d for d in dirs if str(root) in d]
    assert mise_entries == [str(root / "24.16.0" / "bin")]


def test_numeric_versions_outrank_alias_names(isolated_home):
    """Reverse-lexicographic order would pick 'lts-krypton' over '24.16.0'."""
    root = isolated_home / ".local/share/mise/installs/node"
    _fake_node_bin(root / "lts-krypton" / "bin")
    _fake_node_bin(root / "24.16.0" / "bin")
    assert env_mod.node_bin_dirs()[0] == str(root / "24.16.0" / "bin")


def test_version_key_orders_numerically_not_lexicographically():
    assert env_mod._node_version_key("22.0.0") > env_mod._node_version_key("9.9.9")
    assert env_mod._node_version_key("20.1.0") > env_mod._node_version_key("lts")
    assert env_mod._node_version_key("v18.1.0") > env_mod._node_version_key("v9.1.0")


# --- validation: a dir is a node bin dir only if it provides node ---
def test_dir_without_node_is_dropped(isolated_home):
    root = isolated_home / ".local/share/mise/installs/node"
    (root / "22.0.0" / "bin").mkdir(parents=True)  # empty: no node
    assert env_mod.node_bin_dirs() == ()


@pytest.mark.skipif(
    platform_compat.IS_WINDOWS,
    reason="asserts POSIX execute-bit semantics; Windows has no x bit, so an "
           "existing .exe is executable regardless of mode",
)
def test_non_executable_node_is_dropped(isolated_home):
    d = isolated_home / ".local/share/mise/installs/node/22.0.0/bin"
    d.mkdir(parents=True)
    (d / "node").write_text("not executable")
    (d / "node").chmod(0o644)
    assert env_mod.node_bin_dirs() == ()


def test_no_toolchain_anywhere_returns_empty(isolated_home):
    assert env_mod.node_bin_dirs() == ()


# --- other managers ---
@pytest.mark.parametrize("layout", [
    ".asdf/installs/nodejs/{v}/bin",
    ".nvm/versions/node/{v}/bin",
    ".local/share/fnm/node-versions/{v}/installation/bin",
    ".fnm/node-versions/{v}/installation/bin",
])
def test_each_version_manager_layout_is_found(isolated_home, layout):
    d = _fake_node_bin(isolated_home / layout.format(v="v20.1.0"))
    assert str(d) in env_mod.node_bin_dirs()


@pytest.mark.parametrize("layout", [
    ".local/share/mise/shims",
    ".volta/bin",
    "n/bin",
])
def test_shim_dirs_are_found(isolated_home, layout):
    d = _fake_node_bin(isolated_home / layout)
    assert str(d) in env_mod.node_bin_dirs()


def test_returned_paths_are_os_normalized(isolated_home):
    """Every entry must use the platform's own separator.

    The static-dir templates are written with forward slashes, so without
    normalization Windows emitted "C:\\home/.volta/bin" while the glob branch
    emitted "C:\\home\\.volta\\bin" — two spellings of one directory from one
    function. They land on PATH and are compared, so this is not cosmetic.
    """
    _fake_node_bin(isolated_home / ".volta" / "bin")
    _fake_node_bin(isolated_home / ".local/share/mise/installs/node/22.0.0/bin")
    dirs = env_mod.node_bin_dirs()
    assert dirs
    for d in dirs:
        assert d == os.path.normpath(d), f"not normalized: {d!r}"
    if os.sep == "\\":
        assert not any("/" in d for d in dirs)


def test_duplicate_spellings_collapse(isolated_home, monkeypatch):
    """Normalizing before the dedup check is what makes `seen` effective."""
    d = _fake_node_bin(isolated_home / ".volta" / "bin")
    monkeypatch.setenv("KIROCREW_NODE_BIN_DIR", str(d) + os.sep + ".")
    dirs = env_mod.node_bin_dirs()
    assert dirs.count(os.path.normpath(str(d))) == 1


def test_mise_data_dir_env_is_honoured(isolated_home, tmp_path, monkeypatch):
    monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "custom-mise"))
    d = _fake_node_bin(tmp_path / "custom-mise" / "installs/node/22.0.0/bin")
    assert str(d) in env_mod.node_bin_dirs()


def test_xdg_data_home_is_honoured(isolated_home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    d = _fake_node_bin(tmp_path / "xdg" / "mise" / "installs/node/22.0.0/bin")
    assert str(d) in env_mod.node_bin_dirs()


# --- PATH composition ---
def test_node_augmented_path_prepends(isolated_home):
    """Prepended, not appended: a distro's node 18 must not shadow a managed 22."""
    d = _fake_node_bin(isolated_home / ".volta" / "bin")
    out = env_mod.node_augmented_path("/usr/bin:/bin")
    assert out.split(os.pathsep)[0] == str(d)
    assert out.endswith("/usr/bin:/bin")


def test_node_augmented_path_with_empty_base_has_no_empty_entry(isolated_home):
    _fake_node_bin(isolated_home / ".volta" / "bin")
    assert "" not in env_mod.node_augmented_path("").split(os.pathsep)


def test_find_node_tool_returns_absolute_path(isolated_home):
    d = _fake_node_bin(isolated_home / ".volta" / "bin")
    found = env_mod.find_node_tool("npm", "")
    assert found is not None
    assert os.path.isabs(found)
    assert Path(found).parent == d


def test_find_node_tool_returns_none_when_absent(isolated_home):
    assert env_mod.find_node_tool("npm", "") is None
