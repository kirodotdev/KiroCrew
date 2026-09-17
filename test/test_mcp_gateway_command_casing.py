"""Executable spelling must survive MCP command resolution and stub emission."""

from __future__ import annotations

import os
import shlex
import shutil
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew.mcp_gateway import rewriter
from kiro_crew.mcp_gateway.hashing import expand_stub_flags, hash_command


@pytest.mark.parametrize("is_windows", [False, True])
@pytest.mark.parametrize("absolute", [False, True])
def test_resolution_preserves_platform_command_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, is_windows: bool, absolute: bool
) -> None:
    raw = str(tmp_path / "Demo-Mcp.EXE")
    actual = str(tmp_path / "Demo-Mcp.exe")
    Path(actual).touch()
    Path(actual).chmod(0o755)
    monkeypatch.setattr(rewriter, "mcp_search_path", lambda _: str(tmp_path))
    monkeypatch.setattr(rewriter.shutil, "which", lambda command, path: raw)
    notes = rewriter._RewritePassNotes()

    with monkeypatch.context() as patch:
        patch.setattr(rewriter.platform_compat, "IS_WINDOWS", is_windows)
        patch.setattr(rewriter.os.path, "isfile", lambda path: True)
        patch.setattr(rewriter.os, "access", lambda path, mode: True)
        # The pre-fix implementation takes this branch. Returning a different
        # spelling makes its absolute-command regression an assertion failure.
        patch.setattr(rewriter.os.path, "realpath", lambda path: actual)
        resolved = rewriter._resolve_target_command(raw if absolute else "demo-mcp", {}, notes)

    expected = actual if is_windows and not absolute else raw
    assert resolved == expected
    assert notes.which_results == ({} if absolute else {f"demo-mcp\0{tmp_path}": expected})


@pytest.mark.parametrize("command", ["", "unknown-mcp", "absolute"])
def test_unresolved_command_does_not_normalize_an_empty_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.setattr(rewriter, "mcp_search_path", lambda _: str(tmp_path))
    monkeypatch.setattr(rewriter.shutil, "which", lambda command, path: None)
    if command == "absolute":
        command = str(tmp_path / "missing.exe")

    def unexpected_realpath(path: str) -> str:
        pytest.fail("an unresolved command must not become the working directory")

    with monkeypatch.context() as patch:
        patch.setattr(rewriter.platform_compat, "IS_WINDOWS", True)
        patch.setattr(rewriter.os.path, "realpath", unexpected_realpath)
        assert rewriter._resolve_target_command(command, {}, None) == ""


@pytest.mark.parametrize("absolute", [False, True])
def test_unavailable_casing_preserves_the_resolved_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, absolute: bool
) -> None:
    raw = str(tmp_path / "demo-mcp.EXE")
    Path(raw).touch()
    Path(raw).chmod(0o755)
    monkeypatch.setattr(rewriter, "mcp_search_path", lambda _: str(tmp_path))
    monkeypatch.setattr(rewriter.shutil, "which", lambda command, path: raw)

    def unreadable(path: str) -> None:
        raise OSError("executable metadata temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(rewriter.platform_compat, "IS_WINDOWS", True)
        patch.setattr(rewriter.os, "scandir", unreadable)
        assert rewriter._resolve_target_command(raw if absolute else "demo-mcp", {}, None) == raw


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows executable aliases")
@requires_symlinks
def test_native_windows_file_alias_keeps_its_lexical_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Shared-Launcher.exe"
    target.touch()
    alias = tmp_path / "Demo-Mcp.exe"
    alias.symlink_to(target)
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setattr(rewriter, "mcp_search_path", lambda _: str(tmp_path))

    raw = shutil.which("Demo-Mcp", path=str(tmp_path))
    assert raw == str(tmp_path / "Demo-Mcp.EXE")
    resolved = rewriter._resolve_target_command("Demo-Mcp", {}, rewriter._RewritePassNotes())

    assert resolved == str(alias)
    assert resolved != str(target)


@pytest.mark.skipif(os.name != "nt", reason="requires a native Windows directory junction")
def test_native_windows_junction_route_keeps_its_lexical_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_dir = tmp_path / "Real Launchers"
    real_dir.mkdir()
    executable = real_dir / "Mixed-Mcp.exe"
    executable.touch()
    alias_dir = tmp_path / "Launcher Alias"
    make_dir_link(alias_dir, real_dir)
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setattr(rewriter, "mcp_search_path", lambda _: str(alias_dir))

    raw = shutil.which("mixed-mcp", path=str(alias_dir))
    assert raw == str(alias_dir / "mixed-mcp.EXE")
    resolved = rewriter._resolve_target_command("mixed-mcp", {}, rewriter._RewritePassNotes())

    assert resolved == str(alias_dir / "Mixed-Mcp.exe")
    assert resolved != str(executable)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows PATHEXT and path casing")
@pytest.mark.parametrize("filename", ["demo-mcp.exe", "MiXeD-mcp.eXe"])
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("injected", [False, True])
def test_native_windows_stub_and_daemon_use_on_disk_casing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    absolute: bool,
    injected: bool,
) -> None:
    """Use real PATHEXT resolution and overlay writers without launching a server."""
    bin_dir = tmp_path / "Mixed Bin"
    bin_dir.mkdir()
    exe = bin_dir / filename
    exe.touch()
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setenv("PATH", str(bin_dir))
    bare = exe.stem.lower()
    raw = shutil.which(bare, path=str(bin_dir))
    assert raw == str(bin_dir / f"{bare}.EXE")
    assert raw != str(exe)
    servers = {"demo": {"command": raw if absolute else bare, "args": ["--stdio"]}}
    spec = {"name": "case-agent", "mcpServers": {} if injected else servers}
    notes = rewriter._RewritePassNotes()
    result, wrapped = rewriter._rewrite_single_spec(
        spec,
        stubs_dir=tmp_path / "stubs",
        socket_path=tmp_path / "gw.sock",
        work_dir=tmp_path / "wd",
        sandbox_mode="auto",
        approval_mode="interactive",
        stub_servers=frozenset({"demo"}),
        inject_servers=servers if injected else None,
        notes=notes,
    )

    assert wrapped == 1
    expected_command = raw if absolute else str(exe)
    flags = expand_stub_flags(result["mcpServers"]["demo"]["args"])
    assert flags[flags.index("--target-command") + 1] == expected_command
    targets: dict[str, str] = {}
    rewriter._collect_target_env(result["mcpServers"], targets)
    key = "KIROCREW_MCP_TARGET_DEMO__" + hash_command(expected_command, ["--stdio"])
    assert shlex.split(targets[key]) == [expected_command, "--stdio"]
    assert targets["KIROCREW_MCP_TARGET_DEMO"] == targets[key]
    assert list(notes.which_results.values()) == ([] if absolute else [str(exe)])


@pytest.mark.parametrize(
    "entries, expected",
    [
        (["unrelated.exe"], "demo-mcp.EXE"),
        (["Demo-Mcp.exe"], "Demo-Mcp.exe"),
        (["Demo-Mcp.exe", "DEMO-MCP.exe"], "demo-mcp.EXE"),
        (["Demo-Mcp.exe", "demo-mcp.EXE"], "demo-mcp.EXE"),
    ],
)
def test_basename_matching_never_selects_an_ambiguous_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entries: list[str],
    expected: str,
) -> None:
    raw = str(tmp_path / "demo-mcp.EXE")
    monkeypatch.setattr(rewriter, "mcp_search_path", lambda _: str(tmp_path))
    monkeypatch.setattr(rewriter.shutil, "which", lambda command, path: raw)
    with monkeypatch.context() as patch:
        patch.setattr(rewriter.platform_compat, "IS_WINDOWS", True)
        patch.setattr(
            rewriter.os,
            "scandir",
            lambda parent: nullcontext(iter(SimpleNamespace(name=name) for name in entries)),
        )
        resolved = rewriter._resolve_target_command("demo-mcp", {}, None)
    assert resolved == str(tmp_path / expected)
