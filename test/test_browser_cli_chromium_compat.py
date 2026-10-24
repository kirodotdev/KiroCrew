"""The old-libcups Chromium fallback reaches the playwright-cli launch config.

A Chromium build that hard-requires ``ippValidateAttributes`` exits 127 on a host
whose libcups does not export it. These tests drive
:func:`chromium_compat.compatible_chromium_executable` and
:func:`launch.desired_config` over a fake Playwright cache, a fake system libcups
and a fake ``nm``, so nothing here depends on the host the suite runs on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import chromium_compat, launch

_HARD = "                 U ippValidateAttributes\n"
_WEAK = "                 w ippValidateAttributes\n"
_EXPORTED = "0000000000047f20 T ippValidateAttributes\n"
_ABSENT = "                 U cupsGetDests\n"


class _Host:
    """A fake host: a Playwright cache, a system libcups, and what ``nm -D`` prints."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cache = root / "ms-playwright"
        self.cache.mkdir()
        self.libdir = root / "lib64"
        self.libdir.mkdir()
        self.libcups = self.libdir / "libcups.so.2"
        self.symbols: dict[str, str] = {}
        self.required: dict[str, str] | None = {"chromium": "1247"}
        self.nm_calls: list[str] = []
        #: Extra ``ldconfig -p`` lines, before the 64-bit libcups entry.
        self.ldconfig_extra: list[str] = []
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(chromium_compat, "_browsers_cache_dir", lambda: self.cache)
        monkeypatch.setattr(chromium_compat, "_required_revisions", lambda: self.required)
        monkeypatch.setattr(
            platform_compat,
            "trusted_system_bin",
            lambda name: {"nm": "/usr/bin/nm", "ldconfig": "/sbin/ldconfig"}.get(name),
        )
        # Scoped to this module's reference, so no other caller of the real
        # ``subprocess.run`` in this worker sees the fake.
        monkeypatch.setattr(
            chromium_compat,
            "subprocess",
            types.SimpleNamespace(
                run=self._fake_run,
                CompletedProcess=subprocess.CompletedProcess,
                TimeoutExpired=subprocess.TimeoutExpired,
            ),
        )

    def _fake_run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("encoding") == "utf-8"
        if argv == ["/sbin/ldconfig", "-p"]:
            lines = ["123 libs found in cache `/etc/ld.so.cache'", *self.ldconfig_extra]
            if self.libcups.is_file():
                lines.append(f"\tlibcups.so.2 (libc6,x86-64) => {self.libcups}")
            lines.append("\tlibc.so.6 (libc6,x86-64) => /lib64/libc.so.6")
            return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")
        assert argv[:2] == ["/usr/bin/nm", "-D"]
        target = argv[2]
        self.nm_calls.append(target)
        if target not in self.symbols:
            return subprocess.CompletedProcess(argv, 1, "", "nm: no symbols\n")
        return subprocess.CompletedProcess(argv, 0, self.symbols[target], "")

    def chromium(
        self, revision: int, nm_output: str, sub: str = "chrome-linux64", complete: bool = True
    ) -> Path:
        """A cached build; ``complete=False`` leaves out Playwright's install marker."""
        entry = self.cache / f"chromium-{revision}"
        binary = entry / sub / "chrome"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"\x7fELF")
        if complete:
            (entry / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
        self.symbols[str(binary)] = nm_output
        return binary

    def system_libcups(self, nm_output: str) -> None:
        self.libcups.write_bytes(b"\x7fELF")
        self.symbols[str(self.libcups)] = nm_output


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    return _Host(tmp_path, monkeypatch)


# -- the fallback path ----------------------------------------------------------


def test_old_libcups_points_the_cli_at_a_weakly_linked_build(host: _Host) -> None:
    """The reported host: the required build needs the symbol, libcups lacks it."""
    host.chromium(1247, _HARD)
    older = host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() == str(older)
    assert launch.desired_config() == {
        "browser": {"browserName": "chromium", "launchOptions": {"executablePath": str(older)}}
    }


def test_the_pin_reaches_the_file_the_cli_reads(host: _Host) -> None:
    """``PLAYWRIGHT_MCP_CONFIG`` names this file, so the pin must be in it."""
    host.chromium(1247, _HARD)
    older = host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)

    path = launch.write_config()

    assert path is not None
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["browser"]["launchOptions"] == {"executablePath": str(older)}


def test_the_newest_compatible_build_wins(host: _Host) -> None:
    host.chromium(1247, _HARD)
    host.chromium(1240, _HARD)
    newest_ok = host.chromium(1217, _WEAK)
    host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() == str(newest_ok)


def test_an_incomplete_newer_build_loses_to_a_complete_compatible_one(host: _Host) -> None:
    """An interrupted extraction at a higher revision is never the fallback."""
    host.chromium(1247, _HARD)
    host.chromium(1230, _WEAK, complete=False)
    complete = host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() == str(complete)


def test_only_incomplete_fallbacks_leave_the_config_unchanged(host: _Host) -> None:
    host.chromium(1247, _HARD)
    host.chromium(1208, _WEAK, complete=False)
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() is None


def test_an_in_place_restart_still_runs_the_probe(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-exec inherits ``PLAYWRIGHT_MCP_CONFIG`` naming our own file. That is not
    an operator choice, so the file is rewritten and the probe runs again."""
    host.chromium(1247, _HARD)
    older = host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)
    own = launch.launch_config_path()
    own.parent.mkdir(parents=True, exist_ok=True)
    own.write_text(json.dumps({"browser": {"browserName": "chromium"}}), encoding="utf-8")
    monkeypatch.setenv(launch.CONFIG_ENV, str(own))

    assert launch.cli_env_overrides() == {launch.CONFIG_ENV: str(own)}
    assert str(host.cache / "chromium-1247" / "chrome-linux64" / "chrome") in host.nm_calls
    written = json.loads(own.read_text(encoding="utf-8"))
    assert written["browser"]["launchOptions"] == {"executablePath": str(older)}


def test_a_build_without_the_symbol_counts_as_compatible(host: _Host) -> None:
    host.chromium(1247, _HARD)
    older = host.chromium(1100, _ABSENT, sub="chrome-linux")
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() == str(older)


def test_unknown_required_revision_judges_the_newest_cached_build(host: _Host) -> None:
    host.required = None
    host.chromium(1247, _HARD)
    older = host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() == str(older)


# -- the normal path: the config stays exactly as before ------------------------


def test_current_libcups_keeps_the_default_build(host: _Host) -> None:
    """A host whose libcups exports the symbol runs the new build; never downgrade it."""
    host.chromium(1247, _HARD)
    host.chromium(1208, _WEAK)
    host.system_libcups(_EXPORTED)

    assert chromium_compat.compatible_chromium_executable() is None
    assert launch.desired_config() == {"browser": {"browserName": "chromium"}}


def test_a_weakly_linked_default_needs_no_fallback(host: _Host) -> None:
    host.chromium(1247, _WEAK)
    host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() is None
    assert str(host.libcups) not in host.nm_calls


def test_no_compatible_build_leaves_the_config_unchanged(host: _Host) -> None:
    host.chromium(1247, _HARD)
    host.chromium(1240, _HARD)
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() is None
    assert launch.desired_config() == {"browser": {"browserName": "chromium"}}


def test_no_system_libcups_is_no_evidence(host: _Host) -> None:
    host.chromium(1247, _HARD)
    host.chromium(1208, _WEAK)

    assert chromium_compat.compatible_chromium_executable() is None


def test_unreadable_libcups_is_no_evidence(host: _Host) -> None:
    host.chromium(1247, _HARD)
    host.chromium(1208, _WEAK)
    host.libcups.write_bytes(b"\x7fELF")  # present, but nm fails on it

    assert chromium_compat.compatible_chromium_executable() is None


def test_the_64_bit_libcups_is_judged_over_a_32_bit_copy(host: _Host, tmp_path: Path) -> None:
    """On a multilib host the bare ``(libc6)`` entry is the 32-bit copy Chromium never loads."""
    host.chromium(1247, _HARD)
    older = host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)
    lib32 = tmp_path / "lib32" / "libcups.so.2"
    lib32.parent.mkdir()
    lib32.write_bytes(b"\x7fELF")
    host.symbols[str(lib32)] = _EXPORTED
    host.ldconfig_extra = [f"\tlibcups.so.2 (libc6) => {lib32}"]

    assert chromium_compat.compatible_chromium_executable() == str(older)
    assert str(lib32) not in host.nm_calls


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permission bits that bind the current user",
)
def test_an_untraversable_build_is_skipped_not_raised(host: _Host) -> None:
    """A shared cache holding another owner's ``chromium-*`` directory must not
    crash gateway startup: ``Path.is_file`` raises ``PermissionError`` there."""
    host.chromium(1247, _HARD)
    locked = host.cache / "chromium-1230"
    locked.mkdir()
    older = host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)
    locked.chmod(0)
    try:
        with pytest.raises(PermissionError):
            (locked / "chrome-linux64" / "chrome").is_file()
        assert chromium_compat.compatible_chromium_executable() == str(older)
    finally:
        locked.chmod(0o700)


def test_a_filesystem_error_in_the_probe_leaves_the_config_unchanged(
    host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe runs before ``write_config`` reaches its own handlers, so any
    ``OSError`` must come back as 'no evidence', never escape into startup."""
    host.chromium(1247, _HARD)
    host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)

    def broken_cache() -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(chromium_compat, "_browsers_cache_dir", broken_cache)

    assert chromium_compat.compatible_chromium_executable() is None
    path = launch.write_config()
    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8")) == {"browser": {"browserName": "chromium"}}


def test_missing_nm_is_no_evidence(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    host.chromium(1247, _HARD)
    host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda _name: None)

    assert chromium_compat.compatible_chromium_executable() is None


def test_non_linux_never_probes(host: _Host, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    host.chromium(1247, _HARD)
    host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() is None
    assert host.nm_calls == []


def test_empty_cache_never_probes(host: _Host) -> None:
    host.system_libcups(_ABSENT)

    assert chromium_compat.compatible_chromium_executable() is None
    assert host.nm_calls == []


def test_a_stale_pin_converges_when_the_host_is_fixed(host: _Host) -> None:
    """After cups-libs is upgraded the next write drops the pin."""
    host.chromium(1247, _HARD)
    host.chromium(1208, _WEAK)
    host.system_libcups(_ABSENT)
    path = launch.write_config()
    assert path is not None
    assert "executablePath" in path.read_text(encoding="utf-8")

    host.system_libcups(_EXPORTED)
    launch.write_config()

    assert json.loads(path.read_text(encoding="utf-8")) == {"browser": {"browserName": "chromium"}}
