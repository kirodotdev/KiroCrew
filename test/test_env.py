"""Tests for kiro_crew.env."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import kiro_crew.env as env_mod
from kiro_crew.env import (
    activate_mise,
    augmented_path,
    ensure_node,
    node_all_bin_dirs,
    resolve_krb5_ccname,
)


def _fake_run(stdout="", returncode=0, stderr=""):
    """Return a subprocess.run replacement that yields a canned CompletedProcess."""

    def _run(argv, **kwargs):  # noqa: ANN001 - test shim
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return _run


def _fake_statfns(spec):
    """Build ``(lstat, stat)`` replacements for ccache-resolution tests.

    ``spec`` maps a path to a descriptor:
      * ``("reg", owner)``           regular file owned by uid ``owner``
      * ``("link", owner, target)``  symlink owned by uid ``owner``; on
                                     ``os.stat`` (follow) it resolves to a
                                     regular file owned by uid ``target``
                                     (``target=None`` = broken/dangling link).
    Any path absent from ``spec`` raises ``OSError`` from both functions.
    """

    def _result(mode, owner):
        return os.stat_result((mode | 0o600, 0, 0, 1, owner, 0, 0, 0, 0, 0))

    def _lstat(path):  # inspects the link itself, does NOT follow
        d = spec.get(path)
        if d is None:
            raise OSError("no such file")
        if d[0] == "reg":
            return _result(stat.S_IFREG, d[1])
        return _result(stat.S_IFLNK, d[1])  # "link"

    def _stat(path):  # follows symlinks
        d = spec.get(path)
        if d is None:
            raise OSError("no such file")
        if d[0] == "reg":
            return _result(stat.S_IFREG, d[1])
        if d[2] is None:  # "link" with dangling target
            raise OSError("dangling symlink")
        return _result(stat.S_IFREG, d[2])

    return _lstat, _stat


def _patch_statfns(monkeypatch, spec, *, uid=4242):
    """Patch os.getuid/os.lstat/os.stat in kiro_crew.env for a ccache test."""
    monkeypatch.setattr("kiro_crew.env.os.getuid", lambda: uid)
    lstat, stat_fn = _fake_statfns(spec)
    monkeypatch.setattr("kiro_crew.env.os.lstat", lstat)
    monkeypatch.setattr("kiro_crew.env.os.stat", stat_fn)


class TestAugmentedPath:
    def test_prepends_extra_dirs(self) -> None:
        result = augmented_path("/usr/bin")
        dirs = result.split(os.pathsep)
        # base_path sits after the well-known extras but BEFORE the
        # interpreter-dir fallback (the final entry).
        assert dirs[-2] == "/usr/bin"
        assert dirs[-1] == str(Path(sys.executable).parent)
        assert any(".local/bin" in d for d in dirs)

    def test_appends_running_interpreter_bin_dir_last(self, monkeypatch) -> None:
        """The venv's own console-scripts dir must be discoverable — but LAST.

        On Windows a non-shell gateway does not inherit the venv's ``Scripts\\``
        on ``$PATH``, so ``shutil.which("kirocrew")`` silently returns ``None``
        and every user-configured MCP that spawns the ``kirocrew`` wrapper
        (e.g. ``kirocrew-core``) is dropped. Appending ``sys.executable``'s
        parent restores parity with the POSIX ``bin/`` layout systemd already
        picks up. It must be the LAST entry: the dir also holds ``python`` /
        ``pip``, and placing it before base_path would rebind a user MCP's
        bare ``"command": "python"`` to the gateway's venv interpreter.
        """
        fake_exe = "/opt/venv/Scripts/python.exe"
        monkeypatch.setattr(sys, "executable", fake_exe)
        dirs = augmented_path("/usr/bin").split(os.pathsep)
        assert dirs[-1] == str(Path(fake_exe).parent)
        # base_path still outranks the interpreter dir.
        assert dirs.index("/usr/bin") < dirs.index(str(Path(fake_exe).parent))

    def test_local_bin_before_toolbox(self) -> None:
        result = augmented_path("")
        dirs = result.split(os.pathsep)
        local_idx = next(i for i, d in enumerate(dirs) if ".local/bin" in d)
        toolbox_idx = next(i for i, d in enumerate(dirs) if ".toolbox/bin" in d)
        assert local_idx < toolbox_idx

    def test_empty_base(self) -> None:
        result = augmented_path("")
        assert result  # not empty
        assert not result.endswith(os.pathsep)  # no trailing separator

    def test_no_arg_defaults_empty(self) -> None:
        result = augmented_path()
        assert ".local/bin" in result

    def test_includes_nvm_node_bins(self, tmp_path, monkeypatch) -> None:
        # Simulate a home with two nvm-installed node versions. The bin dirs are
        # deliberately EMPTY (no `node` inside): MCP-binary discovery must keep
        # including them — a global npm binary does not need node beside it.
        nvm = tmp_path / ".nvm" / "versions" / "node"
        (nvm / "v18.0.0" / "bin").mkdir(parents=True)
        (nvm / "v22.5.0" / "bin").mkdir(parents=True)
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
        env_mod._node_all_bin_dirs.cache_clear()
        try:
            dirs = augmented_path("/usr/bin").split(os.pathsep)
        finally:
            env_mod._node_all_bin_dirs.cache_clear()
        nvm_marker = os.path.join(".nvm", "versions", "node")
        nvm_bins = [d for d in dirs if nvm_marker in d]
        assert len(nvm_bins) == 2
        # Newest version first (numeric version ranking).
        assert "v22.5.0" in nvm_bins[0]
        assert "v18.0.0" in nvm_bins[1]

    def test_mise_shims_follow_mise_data_dir(self, tmp_path, monkeypatch) -> None:
        """The shims entry must track MISE_DATA_DIR, and must stay AHEAD of the
        per-version install bins — the shim honours the project's version pin,
        the raw install bin does not."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
        monkeypatch.setenv("MISE_DATA_DIR", str(tmp_path / "custom-mise"))
        shims = tmp_path / "custom-mise" / "shims"
        install_bin = tmp_path / "custom-mise" / "installs" / "node" / "22.0.0" / "bin"
        shims.mkdir(parents=True)
        install_bin.mkdir(parents=True)
        env_mod._node_all_bin_dirs.cache_clear()
        try:
            dirs = augmented_path("/usr/bin").split(os.pathsep)
        finally:
            env_mod._node_all_bin_dirs.cache_clear()
        assert str(shims) in dirs
        assert str(install_bin) in dirs
        assert dirs.index(str(shims)) < dirs.index(str(install_bin))

    def test_relative_mise_data_dir_yields_no_relative_path_entry(
        self, tmp_path, monkeypatch
    ) -> None:
        """A relative MISE_DATA_DIR must not put a relative entry (e.g.
        'relative-mise/shims') on a spawned subprocess's PATH — the child would
        re-resolve it against ITS cwd, shadowing the configured command."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
        monkeypatch.setenv("MISE_DATA_DIR", "relative-mise")
        env_mod._node_all_bin_dirs.cache_clear()
        try:
            dirs = augmented_path("/usr/bin").split(os.pathsep)
        finally:
            env_mod._node_all_bin_dirs.cache_clear()
        for d in dirs:
            assert os.path.isabs(d), f"relative PATH entry leaked: {d!r}"


class TestNodeAllBinDirs:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        env_mod._node_all_bin_dirs.cache_clear()
        yield
        env_mod._node_all_bin_dirs.cache_clear()

    @pytest.fixture
    def fake_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p
        )
        monkeypatch.delenv("MISE_DATA_DIR", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        return tmp_path

    def test_empty_when_no_managers(self, fake_home) -> None:
        assert node_all_bin_dirs() == ()

    def test_skips_version_dir_without_bin(self, fake_home) -> None:
        # A node version dir that has no bin/ subdir is ignored.
        (fake_home / ".nvm" / "versions" / "node" / "v20.0.0").mkdir(parents=True)
        assert node_all_bin_dirs() == ()

    def test_returns_existing_bin_even_without_node(self, fake_home) -> None:
        # No-narrowing pin vs the retired _node_version_manager_bins: a bare
        # bin dir (no executable `node`) is still a search location, because a
        # globally-installed MCP binary can live there on its own.
        bin_dir = fake_home / ".nvm" / "versions" / "node" / "v20.0.0" / "bin"
        bin_dir.mkdir(parents=True)
        assert node_all_bin_dirs() == (str(bin_dir),)

    def test_returns_all_versions_not_just_the_best(self, fake_home) -> None:
        # THE regression this consolidation must not ship: narrowing to the
        # best version per root would silently stop finding MCP binaries
        # installed under a non-best Node version.
        nvm = fake_home / ".nvm" / "versions" / "node"
        old = nvm / "v18.0.0" / "bin"
        new = nvm / "v22.5.0" / "bin"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        dirs = node_all_bin_dirs()
        assert str(old) in dirs
        assert str(new) in dirs
        assert dirs.index(str(new)) < dirs.index(str(old))

    def test_covers_mise_asdf_and_fnm_layouts(self, fake_home) -> None:
        # The retired implementation knew only nvm + a wrong fnm layout; the
        # consolidated one searches every manager root the build tier knows.
        layouts = [
            fake_home / ".local/share/mise/installs/node/22.0.0/bin",
            fake_home / ".asdf/installs/nodejs/20.1.0/bin",
            fake_home / ".local/share/fnm/node-versions/v20.1.0/installation/bin",
            fake_home / ".fnm/node-versions/v18.2.0/installation/bin",
        ]
        for d in layouts:
            d.mkdir(parents=True)
        dirs = node_all_bin_dirs()
        for d in layouts:
            assert str(d) in dirs

    def test_numeric_versions_outrank_alias_names(self, fake_home) -> None:
        # Ordering change vs the retired reverse-lexicographic sort, which put
        # 'lts-krypton' above '24.16.0'.
        root = fake_home / ".local/share/mise/installs/node"
        alias = root / "lts-krypton" / "bin"
        numeric = root / "24.16.0" / "bin"
        alias.mkdir(parents=True)
        numeric.mkdir(parents=True)
        dirs = node_all_bin_dirs()
        assert dirs.index(str(numeric)) < dirs.index(str(alias))

    def test_is_cached(self, fake_home) -> None:
        """Second call returns the cached result without re-globbing."""
        nvm = fake_home / ".nvm" / "versions" / "node" / "v20.0.0" / "bin"
        nvm.mkdir(parents=True)
        result1 = node_all_bin_dirs()
        # Remove the dir -- a non-cached implementation would return () now.
        nvm.rmdir()
        result2 = node_all_bin_dirs()
        assert result1 == result2 == (str(nvm),)

    def test_cache_is_keyed_on_home(self, fake_home, tmp_path_factory, monkeypatch) -> None:
        """A different HOME is a different cache key — a caller under a patched
        HOME must get a fresh scan, not the previous key's dirs."""
        first = fake_home / ".nvm" / "versions" / "node" / "v20.0.0" / "bin"
        first.mkdir(parents=True)
        assert node_all_bin_dirs() == (str(first),)
        other = tmp_path_factory.mktemp("otherhome")
        monkeypatch.setattr(
            os.path, "expanduser", lambda p: str(other) if p == "~" else p
        )
        assert node_all_bin_dirs() == ()

    def test_relative_mise_data_dir_is_excluded(self, fake_home, monkeypatch) -> None:
        """A relative MISE_DATA_DIR must not put a relative entry on a spawned
        subprocess's PATH — the child would re-resolve it against ITS cwd."""
        monkeypatch.setenv("MISE_DATA_DIR", "relative-mise")
        d = fake_home / "relative-mise" / "installs" / "node" / "22.0.0" / "bin"
        d.mkdir(parents=True)
        monkeypatch.chdir(fake_home)
        for entry in node_all_bin_dirs():
            assert os.path.isabs(entry), entry

    def test_legacy_fnm_flat_bin_layout_still_found(self, fake_home) -> None:
        """Strict-superset pin vs the retired scan, which globbed
        ``~/.fnm/node-versions/<ver>/bin`` (no ``installation`` segment)."""
        d = fake_home / ".fnm" / "node-versions" / "v20.0.0" / "bin"
        d.mkdir(parents=True)
        assert str(d) in node_all_bin_dirs()

    def test_cache_info_exists(self) -> None:
        """lru_cache exposes cache_info -- confirms decorator is applied."""
        assert hasattr(env_mod._node_all_bin_dirs, "cache_info")
        assert hasattr(env_mod._node_all_bin_dirs, "cache_clear")


class TestEnsureNode:
    def test_returns_resolved_node_without_bootstrap(self, monkeypatch) -> None:
        # When node already resolves, ensure_node returns it and never shells the
        # bootstrap script.
        monkeypatch.setattr(env_mod, "find_node_tool", lambda name, base=None: "/usr/bin/node")
        called = {"ran": False}

        def _boom(*a, **k):
            called["ran"] = True
            raise AssertionError("bootstrap must not run when node is present")

        monkeypatch.setattr(env_mod.subprocess, "run", _boom)
        assert ensure_node() == "/usr/bin/node"
        assert called["ran"] is False

    def test_no_script_returns_none(self, monkeypatch) -> None:
        # No node and no bundled ensure-node.sh (wheel install): graceful None.
        monkeypatch.setattr(env_mod, "find_node_tool", lambda name, base=None: None)
        monkeypatch.setattr(env_mod, "_ensure_node_script", lambda: None)
        assert ensure_node() is None

    def test_runs_bootstrap_then_reresolves(self, monkeypatch, tmp_path) -> None:
        # No node initially; a resolvable ensure-node.sh runs, then node resolves.
        script = tmp_path / "ensure-node.sh"
        script.write_text("#!/bin/bash\n")
        monkeypatch.setattr(env_mod, "_ensure_node_script", lambda: script)
        monkeypatch.setattr(env_mod.platform_compat, "IS_WINDOWS", False)
        calls = iter([None, "/opt/node/bin/node"])
        monkeypatch.setattr(env_mod, "find_node_tool", lambda name, base=None: next(calls))
        monkeypatch.setattr(env_mod.subprocess, "run", lambda *a, **k: None)
        assert ensure_node() == "/opt/node/bin/node"


class TestResolveKrb5Ccname:
    def test_prefers_uid_ccache(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/tmp/krb5cc_4242"

    def test_falls_back_to_username_ccache(self, monkeypatch) -> None:
        import getpass

        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        monkeypatch.setattr(getpass, "getuser", lambda: "tuser")
        # uid path missing, username path present
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_tuser": ("reg", 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/tmp/krb5cc_tuser"

    def test_respects_existing_file_value(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env = {"KRB5CCNAME": "FILE:/custom/cc"}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/custom/cc"  # operator override wins

    def test_overrides_keyring_value(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env = {"KRB5CCNAME": "KEYRING:persistent:1000"}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/tmp/krb5cc_4242"

    def test_noop_when_no_cache_file(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_follows_uid_owned_symlink(self, monkeypatch) -> None:
        # sssd/systemd ship /tmp/krb5cc_<uid> as a uid-owned symlink into
        # /run/user/<uid>/krb5cc/... — follow it and trust the resolved target.
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("link", 4242, 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "FILE:/tmp/krb5cc_4242"

    def test_rejects_foreign_owned_symlink(self, monkeypatch) -> None:
        # A symlink owned by another uid is the attack vector — reject without
        # following (a co-tenant could point it anywhere).
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("link", 9999, 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_rejects_uid_symlink_to_foreign_target(self, monkeypatch) -> None:
        # uid-owned symlink whose resolved target is owned by someone else.
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("link", 4242, 9999)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_rejects_dangling_uid_symlink(self, monkeypatch) -> None:
        # uid-owned symlink whose target does not exist.
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("link", 4242, None)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_rejects_foreign_owned_ccache(self, monkeypatch) -> None:
        # A regular file owned by a different uid (planted by a co-tenant on a
        # shared /tmp) must NOT be trusted.
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 9999)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_preserves_kcm_scheme(self, monkeypatch) -> None:
        # macOS default is KCM: — a stale /tmp file must NOT hijack it.
        monkeypatch.setattr("kiro_crew.env.sys.platform", "darwin")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", False)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env = {"KRB5CCNAME": "KCM:"}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "KCM:"

    def test_preserves_dir_scheme(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env = {"KRB5CCNAME": "DIR:/run/user/4242/krb5cc"}
        resolve_krb5_ccname(env)
        assert env["KRB5CCNAME"] == "DIR:/run/user/4242/krb5cc"

    def test_noop_on_non_linux(self, monkeypatch) -> None:
        # On macOS with empty KRB5CCNAME, a stale /tmp file must not be adopted.
        monkeypatch.setattr("kiro_crew.env.sys.platform", "darwin")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", False)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env

    def test_logs_resolved_path_on_success(self, monkeypatch, caplog) -> None:
        import logging

        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 4242)})
        env: dict[str, str] = {}
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.env"):
            resolve_krb5_ccname(env)
        assert "FILE:/tmp/krb5cc_4242" in caplog.text

    def test_logs_rejection_reason(self, monkeypatch, caplog) -> None:
        # A present-but-rejected candidate must be logged with its reason so it
        # is distinguishable from the plain "no ccache" no-op.
        import logging

        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {"/tmp/krb5cc_4242": ("reg", 9999)})
        env: dict[str, str] = {}
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.env"):
            resolve_krb5_ccname(env)
        assert "KRB5CCNAME" not in env
        assert "foreign-owned" in caplog.text

    def test_no_log_when_no_candidate(self, monkeypatch, caplog) -> None:
        # The ordinary "no ccache present" case must NOT emit a rejection log.
        import logging

        monkeypatch.setattr("kiro_crew.env.sys.platform", "linux")
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", True)
        _patch_statfns(monkeypatch, {})
        env: dict[str, str] = {}
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.env"):
            resolve_krb5_ccname(env)
        assert "rejected ccache candidate" not in caplog.text

    def test_never_calls_getuid_when_not_linux(self, monkeypatch) -> None:
        """On Windows/macOS the resolver MUST short-circuit before touching
        ``os.getuid`` — the shim exists precisely because ``os.getuid`` is
        undefined on Windows and would crash the gateway boot. Regression
        guard for the exact Windows-crash the ``IS_LINUX`` gate was introduced
        to prevent: if a future refactor moves the ``getuid`` call above the
        platform check, this counter fires.
        """
        monkeypatch.setattr("kiro_crew.env.platform_compat.IS_LINUX", False)
        calls: list[None] = []

        def _getuid_boom() -> int:
            calls.append(None)
            raise AssertionError("os.getuid must not be called on non-Linux")

        # ``raising=False`` lets this run on Windows too, where ``os.getuid``
        # doesn't exist — that's the entire crash the shim prevents, and we
        # still want to prove the resolver returns without touching it.
        monkeypatch.setattr("kiro_crew.env.os.getuid", _getuid_boom, raising=False)
        env: dict[str, str] = {}
        resolve_krb5_ccname(env)
        assert calls == []
        assert "KRB5CCNAME" not in env


class TestActivateMise:
    def test_noop_when_mise_absent(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env._mise_bin", lambda: None)
        env: dict[str, str] = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []
        assert env == {"PATH": "/usr/bin"}

    def test_noop_when_disabled_via_env(self, monkeypatch) -> None:
        # KIROCREW_NO_MISE escape hatch short-circuits before mise is invoked.
        called = {"n": 0}
        monkeypatch.setattr("kiro_crew.env._mise_bin", lambda: called.__setitem__("n", 1))
        env = {"PATH": "/usr/bin", "KIROCREW_NO_MISE": "1"}
        assert activate_mise(env) == []
        assert called["n"] == 0  # _mise_bin never consulted

    def test_merges_path_and_added_vars(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env._mise_bin", lambda: "/home/u/.local/bin/mise")
        payload = json.dumps(
            {
                "PATH": "/home/u/.local/share/mise/installs/node/24/bin:/usr/bin",
                "NODE_ENV": "production",
            }
        )
        monkeypatch.setattr("kiro_crew.env.subprocess.run", _fake_run(stdout=payload))
        env = {"PATH": "/usr/bin"}
        changed = activate_mise(env)
        assert changed == ["NODE_ENV", "PATH"]  # sorted
        assert env["PATH"].startswith("/home/u/.local/share/mise/installs/node/24/bin")
        assert env["NODE_ENV"] == "production"

    def test_skips_unchanged_vars(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env._mise_bin", lambda: "/m")
        payload = json.dumps({"PATH": "/usr/bin"})  # identical to current
        monkeypatch.setattr("kiro_crew.env.subprocess.run", _fake_run(stdout=payload))
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []

    def test_nonzero_exit_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env._mise_bin", lambda: "/m")
        monkeypatch.setattr(
            "kiro_crew.env.subprocess.run", _fake_run(returncode=1, stderr="boom")
        )
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []
        assert env == {"PATH": "/usr/bin"}

    def test_unparsable_json_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env._mise_bin", lambda: "/m")
        monkeypatch.setattr("kiro_crew.env.subprocess.run", _fake_run(stdout="not json{"))
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []

    def test_non_dict_json_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env._mise_bin", lambda: "/m")
        monkeypatch.setattr("kiro_crew.env.subprocess.run", _fake_run(stdout="[1, 2, 3]"))
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []

    def test_skips_non_string_values(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env._mise_bin", lambda: "/m")
        payload = json.dumps({"PATH": "/new", "BOGUS": 42, "ALSO": None})
        monkeypatch.setattr("kiro_crew.env.subprocess.run", _fake_run(stdout=payload))
        env: dict[str, str] = {}
        assert activate_mise(env) == ["PATH"]
        assert env == {"PATH": "/new"}

    def test_subprocess_failure_is_swallowed(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.env._mise_bin", lambda: "/m")

        def _boom(*a, **k):  # noqa: ANN002, ANN003
            raise OSError("exec failed")

        monkeypatch.setattr("kiro_crew.env.subprocess.run", _boom)
        env = {"PATH": "/usr/bin"}
        assert activate_mise(env) == []
        assert env == {"PATH": "/usr/bin"}


class TestGitBuildInfo:
    """kiro_crew.env.git_build_info reports the running checkout's branch+sha."""

    def test_empty_when_no_project_dir(self, monkeypatch) -> None:
        from kiro_crew.env import git_build_info

        git_build_info.cache_clear()
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        assert git_build_info() == ("", "")
        git_build_info.cache_clear()

    def test_empty_when_not_a_git_tree(self, tmp_path, monkeypatch) -> None:
        # Project dir exists but has no .git (toolbox/pip-wheel layout).
        from kiro_crew.env import git_build_info

        git_build_info.cache_clear()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        assert git_build_info() == ("", "")
        git_build_info.cache_clear()

    def test_reads_branch_and_commit(self, tmp_path, monkeypatch) -> None:
        from kiro_crew import env

        env.git_build_info.cache_clear()
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        def _run(argv, **kwargs):  # noqa: ANN001 - test shim
            out = "beta-braveheart\n" if "--abbrev-ref" in argv else "abc1234\n"
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        monkeypatch.setattr("kiro_crew.env.subprocess.run", _run)
        assert env.git_build_info() == ("beta-braveheart", "abc1234")
        env.git_build_info.cache_clear()

    def test_reads_in_git_worktree(self, tmp_path, monkeypatch) -> None:
        # In a git worktree, .git is a FILE ("gitdir: ...") not a directory;
        # the .exists() gate must still let git run there.
        from kiro_crew import env

        env.git_build_info.cache_clear()
        (tmp_path / ".git").write_text("gitdir: /repo/.git/worktrees/wt\n")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        def _run(argv, **kwargs):  # noqa: ANN001 - test shim
            out = "wt-branch\n" if "--abbrev-ref" in argv else "def5678\n"
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        monkeypatch.setattr("kiro_crew.env.subprocess.run", _run)
        assert env.git_build_info() == ("wt-branch", "def5678")
        env.git_build_info.cache_clear()

    def test_fails_open_on_nonzero_exit(self, tmp_path, monkeypatch) -> None:
        from kiro_crew import env

        env.git_build_info.cache_clear()
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(
            "kiro_crew.env.subprocess.run", _fake_run(returncode=128, stderr="fatal")
        )
        assert env.git_build_info() == ("", "")
        env.git_build_info.cache_clear()

    def test_fails_open_on_oserror(self, tmp_path, monkeypatch) -> None:
        from kiro_crew import env

        env.git_build_info.cache_clear()
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))

        def _boom(*a, **k):  # noqa: ANN002, ANN003 - test shim
            raise OSError("git not on PATH")

        monkeypatch.setattr("kiro_crew.env.subprocess.run", _boom)
        assert env.git_build_info() == ("", "")
        env.git_build_info.cache_clear()
