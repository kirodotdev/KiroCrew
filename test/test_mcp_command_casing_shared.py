"""The Windows command-casing repair is shared by every resolver, not one.

``shutil.which`` spells the extension it appends exactly as ``PATHEXT`` spells
it, so a bare ``demo-mcp`` resolves to ``demo-mcp.EXE`` while the file on disk is
``demo-mcp.exe``. A launcher that dispatches on its own ``argv[0]`` basename
case-sensitively refuses to run under the synthesized spelling.

The repair lives in ``kiro_crew.env`` beside ``mcp_search_path`` and is applied
by all three consumers of a ``which`` result: the agent-config resolver (whose
result is PERSISTED as the spec's absolute ``command``), the dashboard probe, and
gatewayd's rewriter. The rewriter's own coverage lives in
``test_mcp_gateway_command_casing.py``; these tests pin the helper and the two
other call sites.

Windows is forced per call, not per test: forcing ``IS_WINDOWS`` across a whole
rebuild or probe would switch every other platform branch too, so the wrapper
below flips it only while the helper itself runs.
"""

from __future__ import annotations

import json
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mcp_merge_helpers import bundled_defaults as _bundled_defaults
from mcp_merge_helpers import run_install_mcp_merge as _run_install_mcp_merge

from kiro_crew import env, platform_compat
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION
from kiro_crew.mcp_discovery import McpServerInfo, _probe_cache, probe_server


def _windows_casing(path: str | None) -> str:
    """The real helper, run as if on Windows, for exactly one call."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(platform_compat, "IS_WINDOWS", True)
        return env.resolved_command_casing(path)


def _launcher(tmp_path: Path) -> tuple[str, str]:
    """A launcher on disk as ``demo-mcp.exe``, and the ``.EXE`` spelling of it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    actual = bin_dir / "demo-mcp.exe"
    actual.write_text("#!/bin/sh\n")
    actual.chmod(0o755)
    return str(actual), str(bin_dir / "demo-mcp.EXE")


class TestSharedHelper:
    def test_repairs_a_pathext_synthesized_extension(self, tmp_path: Path) -> None:
        actual, raw = _launcher(tmp_path)
        assert _windows_casing(raw) == actual

    def test_repairs_a_stem_that_differs_in_case_too(self, tmp_path: Path) -> None:
        """A launcher compares the whole basename, so the stem is repaired too."""
        actual, _ = _launcher(tmp_path)
        assert _windows_casing(str(tmp_path / "bin" / "DEMO-MCP.exe")) == actual

    def test_an_exact_match_is_returned_unchanged(self, tmp_path: Path) -> None:
        actual, _ = _launcher(tmp_path)
        assert _windows_casing(actual) == actual

    @pytest.mark.parametrize("path", [None, ""])
    def test_an_unresolved_command_becomes_the_empty_string(self, path: str | None) -> None:
        assert _windows_casing(path) == ""
        assert env.resolved_command_casing(path) == ""

    def test_posix_paths_are_never_touched(self, tmp_path: Path, monkeypatch) -> None:
        """Case is part of the name on a case-sensitive filesystem."""
        _, raw = _launcher(tmp_path)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        assert env.resolved_command_casing(raw) == raw

    @pytest.mark.parametrize(
        "entries, expected_name",
        [
            # Control: one case-insensitive match is repaired, which proves the
            # faked listing below is what the helper actually reads.
            (["demo-mcp.exe"], "demo-mcp.exe"),
            (["demo-mcp.exe", "Demo-Mcp.exe"], "demo-mcp.EXE"),
            (["Demo-Mcp.exe", "DEMO-MCP.exe", "unrelated.exe"], "demo-mcp.EXE"),
        ],
    )
    def test_an_ambiguous_directory_keeps_the_which_result(
        self, tmp_path: Path, entries: list[str], expected_name: str
    ) -> None:
        """Two case-insensitive matches: never pick a different directory entry.

        A case-sensitive Windows directory may hold both spellings, but the
        host's filesystem cannot stand in for one: NTFS and APFS fold the second
        write onto the first file, so a listing of real files would hold one
        entry and the helper would correctly repair it. The directory is
        therefore described to the helper through ``os.scandir``, which is the
        only thing it reads, so the verdict does not depend on the host.
        """
        raw = str(tmp_path / "bin" / "demo-mcp.EXE")
        listed: list[str] = []

        def _scandir(parent: str) -> nullcontext:
            listed.append(parent)
            return nullcontext(iter(SimpleNamespace(name=name) for name in entries))

        # Scoped to the one call: ``os.scandir`` is process-wide, and fixture
        # teardown (``tmp_path`` cleanup) must still list real directories.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(env.os, "scandir", _scandir)
            repaired = _windows_casing(raw)
        assert repaired == str(tmp_path / "bin" / expected_name)
        assert listed == [str(tmp_path / "bin")], "the helper did not read the faked listing"

    def test_an_unreadable_parent_keeps_the_which_result(self, tmp_path: Path) -> None:
        raw = str(tmp_path / "missing-dir" / "demo-mcp.EXE")
        assert _windows_casing(raw) == raw


@pytest.fixture
def _pinned_kiro_cli_version(monkeypatch):
    """Pin the kiro-cli release the spec ``permissions`` gate believes is installed.

    The rebuild ends in ``_write_derived_permissions``, which otherwise spawns
    the HOST's ``kiro-cli --version`` once per worker.
    """
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version",
        lambda: SPEC_PERMISSIONS_MIN_VERSION,
    )


class TestAgentConfigResolver:
    """The persist site: what ``_resolve_command`` returns is written to disk.

    An absolute ``command`` is accepted verbatim on every later pass, so an
    uppercase spelling written once would read as the operator's own forever.
    The repair therefore has to happen inside the resolver, on both of its
    return paths.
    """

    @pytest.mark.usefixtures("_pinned_kiro_cli_version")
    def test_a_bare_command_is_persisted_with_its_on_disk_casing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        actual, raw = _launcher(tmp_path)
        monkeypatch.setattr("kiro_crew.agent.resolved_command_casing", _windows_casing)
        cfg_dir = _bundled_defaults(tmp_path)

        emitted = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"demo": {"command": "demo-mcp"}},
            which_side_effect=lambda c, **kw: raw if c == "demo-mcp" else c,
        )["mcpServers"]

        assert (
            emitted["demo"]["command"] == actual
        ), "the PATH-search branch of _resolve_command persisted the PATHEXT spelling"
        spec = json.loads((tmp_path / "kiro_agents" / "kirocrew.json").read_text())
        assert spec["mcpServers"]["demo"]["command"] == actual

    @pytest.mark.usefixtures("_pinned_kiro_cli_version")
    def test_a_directory_qualified_command_is_repaired_on_its_own_branch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A command with a directory component skips the PATH search but not the repair.

        ``bin/demo-mcp`` names no file (the launcher is ``bin/demo-mcp.exe``), so
        the absolute-and-executable shortcut does not fire and the lookup runs
        through ``which`` on the directory-component branch.
        """
        monkeypatch.setenv("PATH", "/usr/bin")
        actual, raw = _launcher(tmp_path)
        qualified = str(tmp_path / "bin" / "demo-mcp")
        assert not os.path.exists(qualified)
        monkeypatch.setattr("kiro_crew.agent.resolved_command_casing", _windows_casing)
        cfg_dir = _bundled_defaults(tmp_path)

        emitted = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"demo": {"command": qualified}},
            which_side_effect=lambda c, **kw: raw if c == qualified else c,
        )["mcpServers"]

        assert (
            emitted["demo"]["command"] == actual
        ), "the directory-component branch of _resolve_command persisted the PATHEXT spelling"


class TestDashboardProbe:
    """The probe spawns the same spelling the session will be handed."""

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_the_probe_spawns_the_on_disk_casing(self, tmp_path: Path, monkeypatch) -> None:
        actual, raw = _launcher(tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_discovery.resolved_command_casing", _windows_casing)
        captured: list[list[str]] = []

        def _wrap(argv, *a, env=None, **k):
            captured.append(list(argv))
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)
        server = McpServerInfo(name="demo", command="demo-mcp", args=["--stdio"])

        with (
            patch("kiro_crew.mcp_discovery.shutil.which", return_value=raw),
            patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
                side_effect=OSError("stop after argv capture"),
            ),
        ):
            result = await probe_server(server)

        assert result.status == "error"
        assert captured, "the probe never reached its spawn"
        assert captured[0] == [
            actual,
            "--stdio",
        ], "the probe spawned the PATHEXT spelling the launcher shim cannot dispatch"

    @pytest.mark.asyncio
    async def test_an_absolute_command_keeps_the_operator_spelling(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An operator-declared absolute command is spawned verbatim.

        The agent-config resolver persists an absolute, executable command
        unchanged and the rewriter preserves it, so a probe that repairs its
        casing probes a different spelling than the session execs. The fake
        repair stands in for a case-insensitive directory whose real entry is
        lowercase: it reports ``demo-mcp.exe`` for the operator's
        ``DEMO-MCP.exe``, and the probe must ignore that for an absolute
        command that exists and is executable.
        """
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        declared = bin_dir / "DEMO-MCP.exe"
        declared.write_text("#!/bin/sh\n")
        declared.chmod(0o755)

        def _would_repair(path: str | None) -> str:
            return str(Path(path).with_name("demo-mcp.exe")) if path else ""

        monkeypatch.setattr("kiro_crew.mcp_discovery.resolved_command_casing", _would_repair)
        captured: list[list[str]] = []

        def _wrap(argv, *a, env=None, **k):
            captured.append(list(argv))
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)
        server = McpServerInfo(name="demo", command=str(declared), args=["--stdio"])

        with (
            patch("kiro_crew.mcp_discovery.shutil.which", return_value=str(declared)),
            patch(
                "kiro_crew.mcp_discovery.create_subprocess_limited",
                new_callable=AsyncMock,
                side_effect=OSError("stop after argv capture"),
            ),
        ):
            result = await probe_server(server)

        assert result.status == "error"
        assert captured, "the probe never reached its spawn"
        assert captured[0] == [
            str(declared),
            "--stdio",
        ], "the probe rewrote an operator-declared absolute command"


def _saved_launcher(tmp_path: Path) -> tuple[str, str]:
    """A saved ``demo-mcp.EXE`` command whose directory entry is ``demo-mcp.exe``.

    The ``.EXE`` file is real, so the resolvers' absolute-and-executable check
    passes on any host. What a case-insensitive Windows directory would list
    for it is described through ``os.scandir`` in :func:`_windows_saved`.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    saved = bin_dir / "demo-mcp.EXE"
    saved.write_text("#!/bin/sh\n")
    saved.chmod(0o755)
    return str(bin_dir / "demo-mcp.exe"), str(saved)


def _windows_saved(path: str, listing: tuple[str, ...] = ("demo-mcp.exe",)) -> str:
    """The real ``saved_command_casing``, as on Windows, over a faked listing."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(platform_compat, "IS_WINDOWS", True)
        mp.setattr(
            env.os,
            "scandir",
            lambda parent: nullcontext(iter(SimpleNamespace(name=n) for n in listing)),
        )
        return env.saved_command_casing(path)


class TestSavedCommandCasing:
    """An absolute command an older build persisted heals only its extension."""

    def test_a_saved_pathext_extension_is_healed(self, tmp_path: Path) -> None:
        actual, saved = _saved_launcher(tmp_path)
        assert _windows_saved(saved) == actual

    def test_a_stem_the_operator_spelled_differently_is_kept(self, tmp_path: Path) -> None:
        saved = str(tmp_path / "bin" / "DEMO-MCP.exe")
        assert _windows_saved(saved) == saved

    def test_a_stem_and_extension_both_differing_is_kept(self, tmp_path: Path) -> None:
        saved = str(tmp_path / "bin" / "DEMO-MCP.EXE")
        assert _windows_saved(saved) == saved

    def test_an_exact_match_is_kept(self, tmp_path: Path) -> None:
        actual, _ = _saved_launcher(tmp_path)
        assert _windows_saved(actual) == actual

    def test_posix_paths_are_never_touched(self, tmp_path: Path, monkeypatch) -> None:
        _, saved = _saved_launcher(tmp_path)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        assert env.saved_command_casing(saved) == saved


class TestSavedCommandAtEachResolver:
    """Each resolver heals a saved ``.EXE`` absolute command on its absolute branch."""

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.usefixtures("_pinned_kiro_cli_version")
    def test_the_agent_config_resolver_persists_the_healed_spelling(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("PATH", "/usr/bin")
        actual, saved = _saved_launcher(tmp_path)
        monkeypatch.setattr("kiro_crew.agent.saved_command_casing", _windows_saved)
        cfg_dir = _bundled_defaults(tmp_path)

        emitted = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"demo": {"command": saved}},
            which_side_effect=lambda c, **kw: c,
        )["mcpServers"]

        assert emitted["demo"]["command"] == actual
        spec = json.loads((tmp_path / "kiro_agents" / "kirocrew.json").read_text())
        assert spec["mcpServers"]["demo"]["command"] == actual

    @pytest.mark.asyncio
    async def test_the_probe_spawns_the_healed_spelling(self, tmp_path: Path, monkeypatch) -> None:
        actual, saved = _saved_launcher(tmp_path)
        monkeypatch.setattr("kiro_crew.mcp_discovery.saved_command_casing", _windows_saved)
        captured: list[list[str]] = []

        def _wrap(argv, *a, env=None, **k):
            captured.append(list(argv))
            return list(argv), dict(env if env is not None else os.environ), None

        monkeypatch.setattr("kiro_crew.mcp_discovery.sandboxed_spawn_argv", _wrap)
        server = McpServerInfo(name="demo", command=saved, args=["--stdio"])

        with patch(
            "kiro_crew.mcp_discovery.create_subprocess_limited",
            new_callable=AsyncMock,
            side_effect=OSError("stop after argv capture"),
        ):
            await probe_server(server)

        assert captured, "the probe never reached its spawn"
        assert captured[0] == [actual, "--stdio"]

    def test_the_gateway_rewriter_emits_the_healed_spelling(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from kiro_crew.mcp_gateway import rewriter

        actual, saved = _saved_launcher(tmp_path)
        monkeypatch.setattr(rewriter, "saved_command_casing", _windows_saved)
        assert rewriter._resolve_target_command(saved, {}, None) == actual


class TestHealedCommandKeepsItsProvenance:
    """A kept record names the healed command, so the next pass still owns it."""

    @pytest.mark.usefixtures("_pinned_kiro_cli_version")
    def test_a_kept_record_follows_the_healed_command(self, tmp_path: Path, monkeypatch) -> None:
        """The source stops resolving while the saved ``.EXE`` path still exists.

        The record is kept so a later PATH fix can rebind the server, but its
        ``emitted`` value must be the healed spelling actually written. An
        ``emitted`` value that differs from the command reads as a hand edit on
        the next pass, and the record is then dropped.
        """
        from kiro_crew.mcp_provenance import DERIVED_KEY

        monkeypatch.setenv("PATH", "/usr/bin")
        actual, saved = _saved_launcher(tmp_path)
        cfg_dir = _bundled_defaults(tmp_path)
        _run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={}, kiro_servers={})
        spec_path = tmp_path / "kiro_agents" / "kirocrew.json"
        cfg = json.loads(spec_path.read_text())
        cfg["mcpServers"]["demo"] = {"command": "demo-mcp"}
        spec_path.write_text(json.dumps(cfg))

        # First rebuild: the bare source resolves to the PATHEXT spelling, unrepaired.
        first = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={},
            which_side_effect=lambda c, **kw: saved if c == "demo-mcp" else c,
        )["mcpServers"]
        assert first["demo"][DERIVED_KEY] == {"from": "demo-mcp", "emitted": saved}

        # This build: the source does not resolve, and the saved path heals.
        monkeypatch.setattr("kiro_crew.agent.saved_command_casing", _windows_saved)
        second = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={},
            which_side_effect=lambda c, **kw: None,
        )["mcpServers"]
        assert second["demo"]["command"] == actual
        assert second["demo"][DERIVED_KEY] == {"from": "demo-mcp", "emitted": actual}

    @pytest.mark.usefixtures("_pinned_kiro_cli_version")
    def test_a_scope_owned_record_follows_the_healed_command(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An inert record on a server another scope declares heals with it too.

        The record is not acted on while the scope owns the command, but it must
        still name the command written: once the scope entry goes away, the
        server is agent-only again and only a matching ``emitted`` value lets a
        later rebuild re-derive it.
        """
        from kiro_crew.mcp_provenance import DERIVED_KEY

        monkeypatch.setenv("PATH", "/usr/bin")
        actual, saved = _saved_launcher(tmp_path)
        cfg_dir = _bundled_defaults(tmp_path)
        _run_install_mcp_merge(tmp_path, cfg_dir, cc_servers={}, kiro_servers={})
        spec_path = tmp_path / "kiro_agents" / "kirocrew.json"
        cfg = json.loads(spec_path.read_text())
        cfg["mcpServers"]["demo"] = {"command": "demo-mcp"}
        spec_path.write_text(json.dumps(cfg))
        first = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={},
            which_side_effect=lambda c, **kw: saved if c == "demo-mcp" else c,
        )["mcpServers"]
        assert first["demo"][DERIVED_KEY] == {"from": "demo-mcp", "emitted": saved}

        # The same server is now declared in mcp.json with the saved spelling.
        monkeypatch.setattr("kiro_crew.agent.saved_command_casing", _windows_saved)
        second = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"demo": {"command": saved}},
            which_side_effect=lambda c, **kw: c,
        )["mcpServers"]
        assert second["demo"]["command"] == actual
        assert second["demo"][DERIVED_KEY] == {"from": "demo-mcp", "emitted": actual}
