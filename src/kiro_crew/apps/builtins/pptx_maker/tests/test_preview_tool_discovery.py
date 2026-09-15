"""PPTX Maker — discovery of the optional preview binaries.

Slide thumbnails need ``soffice``, and LibreOffice's Windows installer writes it into
its own ``program`` directory while adding nothing to ``PATH`` — so a by-name lookup
alone cannot see an install that is present and working. Two consumers have to agree
about where it is: ``/deps`` (what the UI reports, through
:func:`engine.optional_dep_path`) and the engine's own MCP server (what actually
rasterizes, through the ``PATH`` :func:`provision.mcp_tools_path` renders). A fix to
only the first reports LibreOffice present while every thumbnail still fails, so both
are pinned here.

The resolver is stubbed and the platform flag monkeypatched, per the repo rule that a
test must not depend on what the host has installed: these assertions run identically
on a Windows shard with LibreOffice, one without, and on Linux CI.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.pptx_maker.backend import engine, provision


def _fake_which(on_path: dict[str, str], in_dirs: dict[str, dict[str, str]]):
    """A ``shutil.which`` stand-in.

    *on_path* answers a bare lookup (no ``path=``), *in_dirs* maps a probed directory
    to the tools it holds. Stubbing the RESOLVER rather than a command's output is
    what keeps the result the same on every shard.
    """

    def _which(name: str, path: str | None = None) -> str | None:
        if path is None:
            return on_path.get(name)
        return in_dirs.get(path, {}).get(name)

    return _which


@pytest.fixture
def install_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A fake fixed install root for ``soffice``, registered as Windows' own."""
    root = tmp_path / "LibreOffice" / "program"
    root.mkdir(parents=True)
    monkeypatch.setattr(engine, "_SOFFICE_WINDOWS_DIRS", (str(root),))
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    return str(root)


def test_a_fixed_install_root_resolves_a_tool_that_path_misses(
    install_root: str, monkeypatch: pytest.MonkeyPatch
):
    """The whole point: an install ``PATH`` cannot reach is still the user's install.

    Without this the UI shows the ``winget install`` hint to a user who has already
    installed LibreOffice, and calls the tool missing.
    """
    soffice = os.path.join(install_root, "soffice.com")
    monkeypatch.setattr(
        engine.shutil, "which", _fake_which({}, {install_root: {"soffice": soffice}})
    )

    assert engine.optional_dep_path("soffice") == soffice
    assert "soffice" not in engine.missing_optional_deps()


def test_the_install_root_wins_over_the_managed_launcher(
    install_root: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Order matters for HONESTY, not just for which binary runs.

    ``/deps`` derives its ``managed`` flag from whether the resolved path sits in the
    app's own bin dir, and that flag is how the UI says "installed by Kiro Crew".
    Resolving a system LibreOffice through the managed probe would make the app claim
    it installed a system package it deliberately refuses to install.
    """
    managed_dir = tmp_path / "preview-tools" / "bin"
    managed_dir.mkdir(parents=True)
    monkeypatch.setattr(engine.paths, "preview_tools_bin", lambda: managed_dir)
    system = os.path.join(install_root, "soffice.com")
    monkeypatch.setattr(
        engine.shutil,
        "which",
        _fake_which(
            {},
            {
                install_root: {"soffice": system},
                str(managed_dir): {"soffice": str(managed_dir / "soffice.cmd")},
            },
        ),
    )

    assert engine.optional_dep_path("soffice") == system


def test_path_still_wins_over_the_install_root(install_root: str, monkeypatch: pytest.MonkeyPatch):
    """A user who put their own LibreOffice on ``PATH`` keeps it.

    The fallback widens the search; it never re-orders the existing precedence.
    """
    on_path = os.path.join(install_root, "..", "own", "soffice")
    monkeypatch.setattr(
        engine.shutil,
        "which",
        _fake_which({"soffice": on_path}, {install_root: {"soffice": "unreachable-install-root"}}),
    )

    assert engine.optional_dep_path("soffice") == on_path


def test_the_install_root_table_is_not_consulted_off_windows(
    install_root: str, monkeypatch: pytest.MonkeyPatch
):
    """Off Windows the ladder stays ``PATH`` then the managed dir.

    A POSIX package manager puts ``soffice`` on ``PATH``, so probing a Windows install
    root there would only widen what an unrelated platform resolves.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(
        engine.shutil,
        "which",
        _fake_which({}, {install_root: {"soffice": os.path.join(install_root, "soffice.com")}}),
    )

    assert engine.optional_dep_path("soffice") is None


def test_soffice_install_dir_reports_only_what_path_cannot_reach(
    install_root: str, monkeypatch: pytest.MonkeyPatch
):
    """What gets appended to the engine's ``PATH`` — and what does not."""
    soffice = os.path.join(install_root, "soffice.com")
    monkeypatch.setattr(
        engine.shutil, "which", _fake_which({}, {install_root: {"soffice": soffice}})
    )
    assert engine.soffice_install_dir() == install_root

    monkeypatch.setattr(
        engine.shutil,
        "which",
        _fake_which({"soffice": soffice}, {install_root: {"soffice": soffice}}),
    )
    assert engine.soffice_install_dir() is None


def test_the_rendered_engine_path_reaches_the_install_root(
    install_root: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The half that makes previews actually work.

    ``skill/sdpm/api.py`` resolves ``soffice`` by name inside the MCP server kiro-cli
    spawns, so the install root has to be in the ``PATH`` rendered into the agent
    configs. Position is asserted, not just membership: after the inherited ``PATH``
    so nothing resolvable by name is shadowed, before the managed dir so a real tool
    still beats the shim.
    """
    inherited = tmp_path / "inherited-bin"
    managed_dir = tmp_path / "preview-tools" / "bin"
    inherited.mkdir()
    managed_dir.mkdir(parents=True)
    monkeypatch.setattr(provision.paths, "preview_tools_bin", lambda: managed_dir)
    monkeypatch.setenv("PATH", str(inherited))
    monkeypatch.setattr(
        engine.shutil,
        "which",
        _fake_which({}, {install_root: {"soffice": os.path.join(install_root, "soffice.com")}}),
    )

    entries = provision.mcp_tools_path().split(os.pathsep)

    assert entries.index(str(inherited)) < entries.index(install_root)
    assert entries.index(install_root) < entries.index(str(managed_dir))


def test_the_rendered_path_never_gains_an_empty_element(
    install_root: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An empty ``PATH`` element means the CWD on POSIX, so tool resolution would
    depend on wherever the MCP server was started."""
    managed_dir = tmp_path / "preview-tools" / "bin"
    managed_dir.mkdir(parents=True)
    monkeypatch.setattr(provision.paths, "preview_tools_bin", lambda: managed_dir)
    monkeypatch.delenv("PATH", raising=False)
    monkeypatch.setattr(
        engine.shutil,
        "which",
        _fake_which({}, {install_root: {"soffice": os.path.join(install_root, "soffice.com")}}),
    )

    assert "" not in provision.mcp_tools_path().split(os.pathsep)


def test_the_shipped_table_resolves_through_no_environment_variable():
    """``%ProgramFiles%`` is attacker-writable without elevation.

    Resolving through it would let a poisoned value redirect the lookup into a
    directory the user can plant a binary in, which is what pinning literal roots
    avoids — the same reasoning ``platform_compat._WINDOWS_GIT_DIRS`` records.
    """
    entries = engine._SOFFICE_WINDOWS_DIRS
    assert entries
    for entry in entries:
        assert "%" not in entry
        assert "$" not in entry
        assert entry.startswith("C:")
        assert "LibreOffice" in entry


def test_which_within_rejects_a_hit_from_outside_the_asked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolver answering with a path outside the probed directory is refused.

    Platform-independent on purpose: the parent comparison is pure path logic, and the
    Windows CWD precedence that makes it necessary is measured separately. Gating this
    on Windows would leave the guard unexercised on every other shard.
    """
    asked = tmp_path / "trusted-root"
    asked.mkdir()
    elsewhere = tmp_path / "somewhere-writable"
    elsewhere.mkdir()
    monkeypatch.setattr(engine.shutil, "which", lambda *a, **k: str(elsewhere / "soffice"))
    assert engine._which_within("soffice", asked) is None


def test_which_within_accepts_a_hit_inside_the_asked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not reject a genuine install, and returns it absolute."""
    asked = tmp_path / "trusted-root"
    asked.mkdir()
    monkeypatch.setattr(engine.shutil, "which", lambda *a, **k: str(asked / "soffice"))
    found = engine._which_within("soffice", asked)
    assert found is not None
    assert os.path.isabs(found), "a relative hit is what reaches PATH as `.`"
    assert Path(found).parent == asked


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="Windows CWD precedence")
def test_a_planted_binary_in_the_working_directory_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement the guard exists for -- real ``shutil.which``, no stub.

    ``shutil.which(path=...)`` searches the working directory FIRST on a default
    Windows host: ``_winapi.NeedCurrentDirectoryForExePath`` says to unless
    ``NoDefaultCurrentDirectoryInExePath`` is set, and a host that sets it is the
    exception. This test DELETES that variable rather than trusting the developer's own
    environment -- measured on 3.12.14, with it left alone the hazard is invisible and
    the assertion passes for the wrong reason.

    Unguarded, the call answers ``.\\soffice.COM`` from the working directory and
    :func:`engine.system_install_dirs` hands ``.`` -- a RELATIVE entry -- to the MCP
    ``PATH``, which is arbitrary code execution in the process that rasterizes slides.
    """
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    planted = tmp_path / "agent-writable-cwd"
    planted.mkdir()
    (planted / "soffice.com").write_bytes(b"@echo planted\n")
    empty_root = tmp_path / "fake-libreoffice-program"
    empty_root.mkdir()
    monkeypatch.chdir(planted)

    # The plant is real: without this a broken fixture reads as a working guard.
    assert shutil.which(os.path.join(".", "soffice")) is not None
    assert engine._which_within("soffice", empty_root) is None
