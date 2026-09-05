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
    register_mcp_path_dirs,
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

    def test_includes_both_macos_install_prefixes(self) -> None:
        """``/usr/local/bin`` belongs beside ``/opt/homebrew/bin``, not instead of it.

        The two are different install locations, not alternatives: Homebrew uses
        ``/opt/homebrew`` on Apple Silicon and ``/usr/local`` on Intel, and macOS
        ``.pkg`` installers symlink into ``/usr/local/bin`` regardless of
        architecture. A GUI-launched app inherits launchd's minimal
        ``/usr/bin:/bin:/usr/sbin:/sbin``, so anything absent here is unresolvable
        for an in-process ``shutil.which()`` even though it works in a shell.

        Every other bin-dir list in the tree already pairs them -- see
        ``deploy/engine._AWS_BIN_DIRS``, ``kiro_cli._kiro_cli_dirs`` and
        ``dashboard/tailnet`` -- so omitting one here made this helper the
        outlier those call sites had to compensate for locally.
        """
        # The declaration is platform-independent, so assert it directly rather
        # than inferring it from a composed PATH.
        assert "/opt/homebrew/bin" in env_mod._EXTRA_PATH_DIRS
        assert "/usr/local/bin" in env_mod._EXTRA_PATH_DIRS
        # Apple Silicon's prefix stays ahead of the Intel/pkg one, matching the
        # ordering `_AWS_BIN_DIRS` uses.
        assert env_mod._EXTRA_PATH_DIRS.index("/opt/homebrew/bin") < env_mod._EXTRA_PATH_DIRS.index(
            "/usr/local/bin"
        )

        # Both reach the composed PATH ahead of the inherited base_path -- but only
        # where they survive `_validated_bin_dir`, whose sole test is absoluteness.
        # Gating on that same predicate keeps this assertion honest on a host whose
        # os.path flavour does not consider a POSIX root path absolute, instead of
        # encoding a Python version or platform name that would go stale.
        if os.path.isabs("/usr/local/bin"):
            dirs = augmented_path("/usr/bin").split(os.pathsep)
            assert dirs.index("/opt/homebrew/bin") < dirs.index("/usr/bin")
            assert dirs.index("/usr/local/bin") < dirs.index("/usr/bin")
            assert dirs.index("/opt/homebrew/bin") < dirs.index("/usr/local/bin")

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


class TestExtraMcpPathDirs:
    """The MCP binary search path must be extensible (issue #5083).

    A launcher installed into a directory ``_EXTRA_PATH_DIRS`` does not guess
    resolves nowhere on a systemd gateway's PATH, so the server never starts and
    the session merely comes up short of tools. Both seams -- the
    ``mcp.extra_path_dirs`` setting and :func:`register_mcp_path_dirs` -- are
    merged in ``mcp_search_path``, the path the MCP probe, the agent-config
    resolver and gatewayd's rewriter all RESOLVE against, so one contributed
    directory reaches all three. It is deliberately NOT ``spec_env_path``, whose
    result is persisted -- see ``TestContributedDirsAreNeverPersisted``.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self, monkeypatch):
        # Module-global snapshots + warn-once memo: isolate all three so ordering
        # between tests cannot leak a directory or swallow an expected warning.
        monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
        monkeypatch.setattr(env_mod, "_config_path_dirs", ())
        monkeypatch.setattr(env_mod, "_warned_bad_bin_dirs", set())
        yield

    @staticmethod
    def _set_configured(monkeypatch, value):
        """Publish *value* as the config's ``mcp.extra_path_dirs``.

        Goes through the real publish entry point rather than assigning the
        global, so a rename or a change of stored shape breaks these tests
        instead of silently passing against a stale field.
        """
        env_mod.publish_config_path_dirs(value)

    @staticmethod
    def _mcp_dirs(declared: str = "") -> list[str]:
        """The search path an MCP server's command is resolved against."""
        return env_mod.mcp_search_path(declared).split(os.pathsep)

    def test_configured_dir_outranks_builtin_guesses(self, monkeypatch) -> None:
        """An explicitly named directory must win over a built-in guess.

        Ordering is the whole point: when the same command name exists in both,
        the directory the operator named is the one they meant.
        """
        self._set_configured(monkeypatch, ["/opt/pixi/bin"])
        dirs = self._mcp_dirs()
        local_idx = next(i for i, d in enumerate(dirs) if ".local/bin" in d)
        assert dirs.index("/opt/pixi/bin") < local_idx

    def test_spec_declared_path_still_outranks_a_configured_dir(self, monkeypatch) -> None:
        """An operator pin on the spec must not be displaceable by this setting."""
        self._set_configured(monkeypatch, ["/opt/pixi/bin"])
        dirs = self._mcp_dirs("/opt/pinned/bin")
        assert dirs.index("/opt/pinned/bin") < dirs.index("/opt/pixi/bin")

    def test_configured_dir_expands_tilde(self, monkeypatch, tmp_path) -> None:
        """``~/x/bin`` is what a human writes in a config file."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path), 1))
        self._set_configured(monkeypatch, ["~/pixi/bin"])
        dirs = self._mcp_dirs()
        assert str(tmp_path / "pixi" / "bin") in dirs
        assert "~/pixi/bin" not in dirs

    @pytest.mark.parametrize(
        "bad",
        [
            "relative/bin",  # re-resolved against the CHILD's cwd
            f"/opt/a{os.pathsep}/opt/b",  # smuggles two entries into one
            "/opt/\0bin",  # cannot survive exec
            "",
            42,  # hand-edited config / a caller passing junk
            None,
        ],
    )
    def test_bad_entry_is_dropped_not_fatal(self, monkeypatch, bad, caplog) -> None:
        """One bad entry must cost only itself -- never the whole list.

        Failing the list would take out every other directory the operator
        declared; failing the call would take out every MCP probe and rewrite.
        """
        self._set_configured(monkeypatch, [bad, "/opt/good/bin"])
        with caplog.at_level("WARNING"):
            dirs = self._mcp_dirs()
        assert "/opt/good/bin" in dirs
        assert all(os.path.isabs(d) and os.pathsep not in d for d in dirs)
        assert any("mcp.extra_path_dirs" in r.message for r in caplog.records)

    def test_rejected_entry_is_warned_once(self, monkeypatch, caplog) -> None:
        """This runs once per candidate per rebuild; warning every time is spam."""
        self._set_configured(monkeypatch, ["relative/bin"])
        with caplog.at_level("WARNING"):
            self._mcp_dirs()
            self._mcp_dirs()
        warnings = [r for r in caplog.records if "mcp.extra_path_dirs" in r.message]
        assert len(warnings) == 1

    def test_non_list_setting_is_ignored(self, monkeypatch) -> None:
        self._set_configured(monkeypatch, "/opt/pixi/bin")  # a bare string, not a list
        dirs = self._mcp_dirs()
        assert "/opt/pixi/bin" not in dirs
        assert any(".local/bin" in d for d in dirs)  # the rest still built

    def test_reads_no_config(self, monkeypatch) -> None:
        """The whole reason the value is pushed: this composition is reached from
        the event loop by every MCP probe (probe_server), so it must not
        stat/read/validate config.json."""
        from kiro_crew.config.loader import KiroCrewConfig

        def _boom(cls):
            raise AssertionError("the search path must not load the config")

        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
        self._set_configured(monkeypatch, ["/opt/pixi/bin"])
        assert "/opt/pixi/bin" in self._mcp_dirs()

    def test_publish_clears_a_removed_setting(self, monkeypatch) -> None:
        """Every load republishes, so deleting the setting must take effect too --
        not leave the last value pinned for the process lifetime."""
        self._set_configured(monkeypatch, ["/opt/pixi/bin"])
        assert "/opt/pixi/bin" in self._mcp_dirs()
        self._set_configured(monkeypatch, [])
        assert "/opt/pixi/bin" not in self._mcp_dirs()

    def test_register_contributes_and_is_idempotent(self) -> None:
        """A packaged build's installer hook may run more than once."""
        assert register_mcp_path_dirs("/opt/dist/bin") == ("/opt/dist/bin",)
        register_mcp_path_dirs("/opt/dist/bin")
        assert self._mcp_dirs().count("/opt/dist/bin") == 1

    def test_register_returns_only_accepted_entries(self) -> None:
        """The caller needs to see that a value was rejected."""
        assert register_mcp_path_dirs("relative/bin", "/opt/dist/bin") == ("/opt/dist/bin",)
        assert "relative/bin" not in self._mcp_dirs()

    def test_config_outranks_registered(self, monkeypatch) -> None:
        """An operator's own host setting beats a distribution default."""
        self._set_configured(monkeypatch, ["/opt/operator/bin"])
        register_mcp_path_dirs("/opt/dist/bin")
        dirs = self._mcp_dirs()
        assert dirs.index("/opt/operator/bin") < dirs.index("/opt/dist/bin")

    def test_registration_order_is_precedence_order(self) -> None:
        register_mcp_path_dirs("/opt/first/bin")
        register_mcp_path_dirs("/opt/second/bin")
        dirs = self._mcp_dirs()
        assert dirs.index("/opt/first/bin") < dirs.index("/opt/second/bin")

    def test_contributed_duplicate_of_builtin_appears_once(self, monkeypatch, tmp_path) -> None:
        """Contributing a directory the built-in list already covers must not
        lengthen every child's PATH with a second copy of it."""
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
        local_bin = str(tmp_path / ".local" / "bin")
        self._set_configured(monkeypatch, [local_bin])
        env_mod._node_all_bin_dirs.cache_clear()
        try:
            dirs = self._mcp_dirs()
        finally:
            env_mod._node_all_bin_dirs.cache_clear()
        assert dirs.count(local_bin) == 1
        assert dirs.index(local_bin) == 0

    def test_default_config_changes_nothing(self, monkeypatch) -> None:
        """With the setting untouched the emitted PATH is byte-identical to before,
        so the seam cannot perturb an install that never uses it."""
        self._set_configured(monkeypatch, [])
        assert env_mod.mcp_search_path("/opt/pinned/bin") == env_mod.spec_env_path(
            "/opt/pinned/bin"
        )


class TestContributedDirsAreNeverPersisted:
    """A contributed directory must never be written into a consumed config file.

    ``emit_env`` writes ``spec_env_path``'s result into the agent config, the
    kiro-global ``mcp.json`` and the Claude Code sidecar, and those files are read
    back as a spec's AUTHORED ``env.PATH`` on the next rebuild -- which is why
    ``spec_env_path`` documents being idempotent under re-expansion. A contributed
    directory rendered there would become indistinguishable from an authored
    entry, so clearing ``mcp.extra_path_dirs`` could never remove it again.
    Resolution is recomputed every time and stored nowhere, which is why the
    contribution lives only on ``mcp_search_path``.
    """

    @pytest.fixture(autouse=True)
    def _contributed(self, monkeypatch):
        monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
        monkeypatch.setattr(env_mod, "_config_path_dirs", ())
        monkeypatch.setattr(env_mod, "_warned_bad_bin_dirs", set())
        env_mod.publish_config_path_dirs(["/opt/contributed/bin"])
        register_mcp_path_dirs("/opt/contributed-registered/bin")
        yield

    def test_spec_env_path_excludes_contributed_dirs(self) -> None:
        entries = env_mod.spec_env_path("/opt/pinned/bin").split(os.pathsep)
        assert "/opt/contributed/bin" not in entries
        assert "/opt/contributed-registered/bin" not in entries

    def test_emit_env_excludes_contributed_dirs(self) -> None:
        """The persisted surface. ``emit_env`` is the single writer for every
        consumed config file, so excluding it here covers all of them."""
        emitted = env_mod.emit_env({"PATH": "/opt/pinned/bin"})["PATH"]
        assert "/opt/contributed/bin" not in emitted.split(os.pathsep)
        assert "/opt/contributed-registered/bin" not in emitted.split(os.pathsep)

    def test_a_removed_setting_stops_being_searched(self) -> None:
        """The consequence the exclusion buys: clearing the setting takes effect
        even for a spec whose PATH was rendered while it was set."""
        rendered = env_mod.emit_env({"PATH": "/opt/pinned/bin"})["PATH"]
        env_mod.publish_config_path_dirs([])
        monkeyed = env_mod.mcp_search_path(rendered).split(os.pathsep)
        assert "/opt/contributed/bin" not in monkeyed

    def test_contributed_dir_still_reaches_the_mcp_search_path(self) -> None:
        """The negative assertions above must not pass by the feature being broken."""
        entries = env_mod.mcp_search_path("").split(os.pathsep)
        assert "/opt/contributed/bin" in entries
        assert "/opt/contributed-registered/bin" in entries


class TestContributedDirsNeverReachTheTrustedRuntime:
    """A contributed MCP directory must not be able to shadow the agent runtime.

    ``kiro_cli.known_kiro_cli_dirs`` appends ``augmented_path`` when looking for
    ``kiro-cli`` itself, and ``resolve_kiro_cli`` takes the FIRST executable
    candidate. On an install whose CLI is reachable only that way -- a toolbox
    install, found via ``~/.toolbox/bin`` -- a contributed directory ranked ahead
    of it would let a forged ``kiro-cli`` win. Where ``agent.sandbox="off"``
    delegates confinement to the CLI's own sandbox, that forged binary IS the
    sandbox, so this is a credential-exposure path and not merely a wrong
    binary. The contribution therefore lives on the MCP-only composition; these
    tests are the ratchet on that boundary.
    """

    @pytest.fixture(autouse=True)
    def _contributed(self, monkeypatch):
        monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
        monkeypatch.setattr(env_mod, "_config_path_dirs", ())
        monkeypatch.setattr(env_mod, "_warned_bad_bin_dirs", set())
        env_mod.publish_config_path_dirs(["/opt/attacker/bin"])
        register_mcp_path_dirs("/opt/attacker-registered/bin")
        yield

    def test_augmented_path_excludes_contributed_dirs(self) -> None:
        dirs = augmented_path("/usr/bin").split(os.pathsep)
        assert "/opt/attacker/bin" not in dirs
        assert "/opt/attacker-registered/bin" not in dirs

    def test_kiro_cli_search_dirs_exclude_contributed_dirs(self, tmp_path) -> None:
        from kiro_crew.kiro_cli import known_kiro_cli_dirs

        dirs = known_kiro_cli_dirs("linux", tmp_path, {"PATH": "/usr/bin"})
        assert "/opt/attacker/bin" not in dirs
        assert "/opt/attacker-registered/bin" not in dirs

    def test_contributed_dir_still_reaches_the_mcp_search_path(self) -> None:
        """The negative tests above must not pass by the feature being broken."""
        dirs = env_mod.mcp_search_path("").split(os.pathsep)
        assert "/opt/attacker/bin" in dirs
        assert "/opt/attacker-registered/bin" in dirs


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


class TestDedupPath:
    def test_keeps_first_occurrence_order(self) -> None:
        raw = os.pathsep.join(["/a", "/b", "/a", "/c", "/b"])
        assert env_mod.dedup_path(raw).split(os.pathsep) == ["/a", "/b", "/c"]

    def test_drops_empty_entries(self) -> None:
        raw = os.pathsep.join(["", "/a", "", "/b"])
        assert env_mod.dedup_path(raw).split(os.pathsep) == ["/a", "/b"]

    def test_dedup_keys_on_normalized_form(self) -> None:
        """Two spellings of one directory collapse; the first is emitted as-is."""
        raw = os.pathsep.join(["/usr/bin/", "/usr/bin", "/usr/./bin"])
        assert env_mod.dedup_path(raw) == "/usr/bin/"

    def test_empty_input_is_empty(self) -> None:
        assert env_mod.dedup_path("") == ""


class TestSpecEnvPath:
    """A spec's env.PATH must expand to a PATH the child can actually use."""

    def test_spec_entries_come_first(self, monkeypatch) -> None:
        """A spec that pins a toolchain must not be shadowed by the augmentation."""
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
        entries = env_mod.spec_env_path("/opt/shims").split(os.pathsep)
        assert entries[0] == "/opt/shims"

    def test_multiple_spec_entries_keep_their_order(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        declared = os.pathsep.join(["/opt/first", "/opt/second"])
        entries = env_mod.spec_env_path(declared).split(os.pathsep)
        assert entries[:2] == ["/opt/first", "/opt/second"]

    def test_inherited_path_is_retained(self, monkeypatch) -> None:
        """The whole point: the fragment does not become the child's ONLY PATH."""
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/sbin"]))
        entries = env_mod.spec_env_path("/opt/shims").split(os.pathsep)
        assert "/usr/bin" in entries
        assert "/sbin" in entries

    def test_result_is_deduped(self, monkeypatch) -> None:
        """A fragment already present in PATH must not be emitted twice."""
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/opt/shims"]))
        entries = env_mod.spec_env_path("/opt/shims").split(os.pathsep)
        assert entries.count("/opt/shims") == 1

    def test_idempotent(self, monkeypatch) -> None:
        """Re-expanding an already-expanded value is a no-op.

        install_agent rewrites the agent config on every start, so a
        non-idempotent expansion would grow PATH without bound.
        """
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
        once = env_mod.spec_env_path("/opt/shims")
        assert env_mod.spec_env_path(once) == once
        assert env_mod.spec_env_path(env_mod.spec_env_path(once)) == once

    def test_empty_fragment_still_yields_usable_path(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        assert "/usr/bin" in env_mod.spec_env_path("").split(os.pathsep)

    def test_entries_are_unique(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
        entries = env_mod.spec_env_path("/opt/a" + os.pathsep + "/opt/a").split(os.pathsep)
        assert len(entries) == len(set(entries))

    def test_relative_entries_are_dropped(self, monkeypatch) -> None:
        """A relative entry resolves against the CHILD's cwd, from the front."""
        monkeypatch.setenv("PATH", "/usr/bin")
        declared = os.pathsep.join(["bin", "./tools", "/opt/real"])
        entries = env_mod.spec_env_path(declared).split(os.pathsep)
        assert entries[0] == "/opt/real"
        assert "bin" not in entries
        assert "./tools" not in entries

    def test_nul_entry_is_dropped(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        entries = env_mod.spec_env_path("/opt/a\0b").split(os.pathsep)
        assert "/opt/a\0b" not in entries

    def test_non_string_degrades_to_no_override(self, monkeypatch) -> None:
        """Runs per candidate per server per rebuild — must not raise.

        One malformed value in any config file would otherwise turn a single bad
        entry into a failed gateway start.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        expected = env_mod.spec_env_path("")
        for bad in (None, 5, ["/opt/a", "/opt/b"], {"PATH": "/opt/a"}):
            assert env_mod.spec_env_path(bad) == expected  # type: ignore[arg-type]

    def test_trailing_separator_spelling_is_not_duplicated(self, monkeypatch) -> None:
        """normpath-keyed dedup: /usr/bin/ and /usr/bin are one directory."""
        monkeypatch.setenv("PATH", "/usr/bin")
        entries = env_mod.spec_env_path("/usr/bin/").split(os.pathsep)
        assert entries.count("/usr/bin") + entries.count("/usr/bin/") == 1


class TestEmitEnv:
    """emit_env is the single normalization point for every emitted spec env."""

    def test_path_is_expanded(self, monkeypatch) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        out = env_mod.emit_env({"PATH": "/opt/shims", "TOKEN": "x"})
        entries = out["PATH"].split(os.pathsep)
        assert entries[0] == "/opt/shims"
        assert "/usr/bin" in entries
        assert out["TOKEN"] == "x"

    def test_env_without_path_passes_through_equal(self, monkeypatch) -> None:
        src = {"API_KEY": "k", "MODE": "prod"}
        assert env_mod.emit_env(src) == src

    def test_returns_a_new_dict(self) -> None:
        """Sources are reached through shallow copies — never mutate through."""
        src = {"PATH": "/opt/shims"}
        out = env_mod.emit_env(src)
        assert out is not src
        assert src["PATH"] == "/opt/shims"

    def test_malformed_path_passes_through_verbatim(self) -> None:
        """A config error must stay visible, not hide behind a working PATH."""
        for bad in (["/opt/a"], 5, None):
            src = {"PATH": bad, "K": "v"}
            out = env_mod.emit_env(src)  # type: ignore[arg-type]
            assert out["PATH"] == bad
            assert out["K"] == "v"

    def test_empty_string_path_expands_like_the_probe(self, monkeypatch) -> None:
        """``{"PATH": ""}`` must not emit an empty PATH while the probe and the
        command resolver expand it — that IS the probe/session divergence."""
        monkeypatch.setenv("PATH", "/usr/bin")
        out = env_mod.emit_env({"PATH": ""})
        assert out["PATH"] == env_mod.spec_env_path("")
        assert "/usr/bin" in out["PATH"].split(os.pathsep)

    def test_idempotent(self, monkeypatch) -> None:
        """install_agent rewrites the config every start — re-emitting must not grow."""
        monkeypatch.setenv("PATH", "/usr/bin")
        once = env_mod.emit_env({"PATH": "/opt/shims"})
        twice = env_mod.emit_env(once)
        assert twice == once

    def test_windows_path_spelling_is_canonicalized(self, monkeypatch) -> None:
        """A Windows-authored spec says ``Path``; the expanded value is emitted
        under the canonical ``PATH`` so the session gets the same variable the
        probe pins. Emitting ``Path`` on POSIX would set a junk variable and
        leave the real search path unpinned — the probe/session split again."""
        monkeypatch.setenv("PATH", "/usr/bin")
        out = env_mod.emit_env({"Path": "/opt/shims", "K": "v"})
        assert "Path" not in out, "the alternate-case spelling must not survive"
        entries = out["PATH"].split(os.pathsep)
        assert entries[0] == "/opt/shims"
        assert "/usr/bin" in entries
        assert out["K"] == "v"

    def test_both_spellings_collapse_to_canonical_path(self, monkeypatch) -> None:
        """Both spellings present is ambiguous: the exact key wins and the
        alternate is dropped, so no consumer sees two competing search paths."""
        monkeypatch.setenv("PATH", "/usr/bin")
        out = env_mod.emit_env({"PATH": "/opt/exact", "Path": "/opt/other"})
        assert "Path" not in out
        assert out["PATH"].split(os.pathsep)[0] == "/opt/exact"

    def test_malformed_alternate_case_passes_through_verbatim(self) -> None:
        """A malformed value keeps the author's spelling: the config error must
        stay visible rather than being reshaped into a canonical-looking key."""
        out = env_mod.emit_env({"Path": ["/opt/a"], "K": "v"})  # type: ignore[dict-item]
        assert out == {"Path": ["/opt/a"], "K": "v"}


class TestSpecPathKey:
    """The shared PATH-key lookup all three spec readers use."""

    def test_exact_match(self) -> None:
        assert env_mod.spec_path_key({"PATH": "/x"}) == "PATH"

    def test_case_insensitive_match_returns_authored_spelling(self) -> None:
        assert env_mod.spec_path_key({"Path": "/x"}) == "Path"
        assert env_mod.spec_path_key({"path": "/x"}) == "path"

    def test_absent(self) -> None:
        assert env_mod.spec_path_key({"TOKEN": "t"}) is None

    def test_exact_preferred_when_both_present(self) -> None:
        assert env_mod.spec_path_key({"Path": "/a", "PATH": "/b"}) == "PATH"


class TestSanitizeSpecEnv:
    """Loader injection keys must never ride a spec env into a launcher's
    environment — they execute in every ELF binary in the spawn chain (the
    sandbox wrapper included), before confinement exists."""

    def test_loader_keys_are_dropped(self) -> None:
        out = env_mod.sanitize_spec_env(
            [
                ("LD_PRELOAD", "/tmp/evil.so"),
                ("LD_LIBRARY_PATH", "/tmp"),
                ("LD_AUDIT", "/tmp/audit.so"),
                ("DYLD_INSERT_LIBRARIES", "/tmp/evil.dylib"),
                ("API_TOKEN", "sekret"),
                ("PATH", "/opt/only"),
            ]
        )
        assert out == {"API_TOKEN": "sekret", "PATH": "/opt/only"}

    def test_python_env_is_dropped(self) -> None:
        """PYTHON* is a launcher-execution channel here, not a server setting.

        Kiro Crew's Linux sandbox launcher is itself a Python process
        (``[sys.executable, <generated script>, *argv]``), started with the env
        handed to ``Popen`` — so a declared ``PYTHONPATH`` carrying
        ``sitecustomize.py`` executes at interpreter startup, before ``unshare``
        and before the target is exec'd: arbitrary code OUTSIDE the sandbox.
        """
        out = env_mod.sanitize_spec_env(
            [
                ("PYTHONPATH", "/srv/lib"),
                ("PYTHONSTARTUP", "/srv/rc.py"),
                ("PYTHONHOME", "/srv"),
                ("TOKEN", "t"),
            ]
        )
        assert out == {"TOKEN": "t"}

    def test_pythonuserbase_is_dropped(self) -> None:
        """User-site relocation is the same startup-execution channel.

        ``PYTHONUSERBASE`` moves user-site, and ``site.py`` EXECUTES ``.pth`` lines
        found there during interpreter startup. The launcher now also runs ``-I
        -S`` (which is what truly closes this), but the sanitizer is the primary
        control for any future launcher that forgets those flags, so it must cover
        the key in its own right.
        """
        out = env_mod.sanitize_spec_env(
            [("PYTHONUSERBASE", "/tmp/evil"), ("pythonuserbase", "/tmp/evil2"), ("OK", "1")]
        )
        assert out == {"OK": "1"}

    def test_home_is_deliberately_not_dropped(self) -> None:
        """Documents a deliberate boundary, so a future reader does not "fix" it.

        ``HOME`` also derives user-site when ``PYTHONUSERBASE`` is unset, but many
        servers legitimately need it, so stripping it would break real configs. The
        launcher's ``-I -S`` closes that path instead: with site processing off, no
        ``.pth`` runs regardless of where the paths point.
        """
        assert env_mod.sanitize_spec_env([("HOME", "/home/u")]) == {"HOME": "/home/u"}

    def test_benign_env_passes_untouched(self) -> None:
        pairs = [("TOKEN", "t"), ("MODE", "prod"), ("LANG", "C")]
        assert env_mod.sanitize_spec_env(pairs) == dict(pairs)

    def test_matching_is_case_insensitive(self) -> None:
        """Windows env vars are case-insensitive: ``ld_preload`` reaches the
        loader exactly like ``LD_PRELOAD`` on a case-insensitive lookup, so a
        lowercase spelling must not slip through the filter."""
        out = env_mod.sanitize_spec_env(
            [("Ld_Preload", "/tmp/evil.so"), ("dyld_x", "y"), ("OK", "1")]
        )
        assert out == {"OK": "1"}

    def test_emit_env_does_not_sanitize(self, monkeypatch) -> None:
        """The denylist guards OUR launcher, not the emitted config.

        kiro-cli spawns the server itself with no Python launcher of ours in the
        chain, so the emitted spec keeps a declared PYTHONPATH — a legitimate
        way to configure a Python MCP server. Pinned so a future change cannot
        quietly extend the launcher guard into the emit path and break those
        servers in sessions.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        out = env_mod.emit_env({"PYTHONPATH": "/srv/lib", "LD_PRELOAD": "/x.so"})
        assert out["PYTHONPATH"] == "/srv/lib"
        assert out["LD_PRELOAD"] == "/x.so"


class TestSanitizeSpecEnvReservedNamespace:
    """SECURITY: a config-declared ``env`` may not author the ``KIROCREW_``
    namespace, because that namespace carries the caller identity our own
    authorization checks read.

    Distinct class from the loader prefixes: confinement does not mitigate it.
    A sandbox bounds what the child may touch, not whose scheduled jobs Kiro
    Crew believes the child is entitled to delete.
    """

    def test_caller_identity_keys_are_dropped(self) -> None:
        """The four vouched-for identity channels, and the shared secret.

        ``KIROCREW_SESSION_KEY``/``KIROCREW_HOST_PID`` are two of the three
        sources ``_resolve_session_key_strict`` accepts *on the grounds that an
        agent cannot write them*; ``KIROCREW_OWNER_ID`` is the Slack owner.
        ``KIROCREW_CLI`` was the cron admin-bypass flag, whose consumer #6624
        deleted rather than re-grounded (nothing in ``src/`` set it); it is kept in
        this list because the deny is on the NAMESPACE, and a key-by-key list is
        exactly what would fail open for the next identity variable added.
        """
        out = env_mod.sanitize_spec_env(
            [
                ("KIROCREW_CLI", "1"),
                ("KIROCREW_SESSION_KEY", "victim-session"),
                ("KIROCREW_HOST_PID", "4242"),
                ("KIROCREW_CHANNEL_ID", "C0DEADBEEF"),
                ("KIROCREW_OWNER_ID", "UATTACKER"),
                ("KIROCREW_INTERNAL_SECRET", "s3cret"),
                ("MCP_TOKEN", "keep-me"),
                ("PATH", "/opt/helper/bin"),
            ]
        )
        assert out == {"MCP_TOKEN": "keep-me", "PATH": "/opt/helper/bin"}

    def test_whole_namespace_is_denied_not_a_key_list(self) -> None:
        """The point of the prefix: a variable nobody has invented yet is
        already covered.

        A key-by-key denylist fails open for the next identity variable someone
        adds, which is why the control is stated as "a config cannot author our
        namespace" instead.
        """
        out = env_mod.sanitize_spec_env(
            [
                ("KIROCREW_SANDBOX_LEVEL", "none"),
                ("KIROCREW_APPROVAL_MODE", "yolo"),
                ("KIROCREW_NOT_A_REAL_VAR_YET", "x"),
                ("OK", "1"),
            ]
        )
        assert out == {"OK": "1"}

    def test_matching_is_case_insensitive(self) -> None:
        """Windows env names are case-insensitive, so ``kirocrew_cli`` reaches
        ``os.environ.get("KIROCREW_CLI")`` there exactly like the upper spelling.
        """
        out = env_mod.sanitize_spec_env(
            [("kirocrew_cli", "1"), ("KiroCrew_Session_Key", "v"), ("OK", "1")]
        )
        assert out == {"OK": "1"}

    def test_unrelated_namespaces_are_untouched(self) -> None:
        """Scoped deliberately: only OUR namespace is reserved.

        ``MC_*`` is the legacy prefix and carries no identity variable (only
        ``MC_MCP_SOCKET``/``MC_MCP_LOG``/``MC_GATEWAYD_LOG`` diagnostics), and a
        server's own ``KIRO*`` settings are its business.
        """
        pairs = [
            ("MC_MCP_LOG", "/tmp/x.log"),
            ("KIRO_SOMETHING", "1"),
            ("MY_KIROCREW_VAR", "1"),
        ]
        assert env_mod.sanitize_spec_env(pairs) == dict(pairs)

    def test_denying_the_overlay_cannot_strip_an_inherited_value(self) -> None:
        """Why the namespace deny is safe, stated as a test.

        Callers build the child env from ``os.environ`` FIRST and overlay the
        spec on top, so a gateway-authored ``KIROCREW_*`` value is inherited
        regardless. The sanitizer only decides whether a config may OVERRIDE
        one — so filtering the overlay removes the forgery and keeps the real
        value.
        """
        inherited = {"KIROCREW_HOME": "/real/home", "KIROCREW_CLI": ""}
        child = dict(inherited)
        child.update(env_mod.sanitize_spec_env([("KIROCREW_CLI", "1"), ("OK", "1")]))
        assert child["KIROCREW_HOME"] == "/real/home"
        assert child["KIROCREW_CLI"] == ""
        assert child["OK"] == "1"


class TestDeniedSpecEnvKeys:
    """The reporting counterpart of the sanitizer: what did policy remove?"""

    def test_names_what_the_sanitizer_would_drop(self) -> None:
        env = {"PYTHONPATH": "/srv", "LD_PRELOAD": "/x.so", "TOKEN": "t", "PATH": "/b"}
        assert sorted(env_mod.denied_spec_env_keys(env)) == ["LD_PRELOAD", "PYTHONPATH"]

    def test_matches_the_sanitizer_case_insensitively(self) -> None:
        """Both sides must agree, or a dropped key goes unexplained."""
        env = {"pythonpath": "/srv", "Ld_Preload": "/x.so", "ok": "1"}
        dropped = env_mod.denied_spec_env_keys(env)
        kept = env_mod.sanitize_spec_env([(k, str(v)) for k, v in env.items()])
        assert sorted(dropped) == ["Ld_Preload", "pythonpath"]
        assert set(dropped).isdisjoint(kept)

    def test_clean_env_names_nothing(self) -> None:
        assert env_mod.denied_spec_env_keys({"TOKEN": "t", "PATH": "/b"}) == []

    def test_non_string_keys_are_ignored(self) -> None:
        """Config JSON is unvalidated; a malformed key must not raise here."""
        assert env_mod.denied_spec_env_keys({1: "x"}) == []  # type: ignore[dict-item]

    def test_reserved_namespace_is_deliberately_not_reported(self) -> None:
        """Pinned so a future change does not "align" the two and make the
        dashboard lie.

        This function feeds ``_note_denied_env``, whose message says the key
        "execute[s] in the sandbox launcher before confinement, so the probe
        cannot honour them — a session still does". Every clause of that is false
        for ``KIROCREW_CLI``: it is not a launcher-execution channel, and a
        session must not honour a forged caller identity either. The sanitizer
        still drops it (log-only); only the user-facing explanation is scoped to
        the loader class.
        """
        env = {"KIROCREW_CLI": "1", "PYTHONPATH": "/srv", "TOKEN": "t"}
        assert env_mod.denied_spec_env_keys(env) == ["PYTHONPATH"]
        assert "KIROCREW_CLI" not in env_mod.sanitize_spec_env(
            [(k, v) for k, v in env.items()]
        )
