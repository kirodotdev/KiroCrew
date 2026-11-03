"""The repository's one ``soffice`` discovery and install hint (``kiro_crew.soffice_discovery``).

The file panel's slide previews depend on these, so they are a core capability that
must hold with no app installed; PPTX Maker imports the same helpers. The hint is shown
as "install it with: <command>", so its first executable must exist on this host and
its package name must resolve there. The apt family is decided by lineage. The rpm
family carries ``dnf``, ``yum`` or ``microdnf`` depending on the generation, so the
manager is probed the way the browser installer probes it
(``os_deps.rpm_package_manager``). Amazon Linux 2023 reports ``ID_LIKE=fedora`` but
packages no LibreOffice, SUSE's manager is ``zypper``, and an unrecognized
distribution is unknown: those get no command at all rather than a guessed ``apt`` or
``dnf`` line.

The resolver is stubbed and the platform flag monkeypatched, so a test never depends
on what the host has installed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import platform_compat, soffice_discovery
from kiro_crew.browser_cli import os_deps

_APT = "sudo apt install libreoffice"
_DNF = "sudo dnf install libreoffice"
_YUM = "sudo yum install libreoffice"
_MICRODNF = "sudo microdnf install libreoffice"

_AL2023 = {"ID": "amzn", "ID_LIKE": "fedora", "VERSION_ID": "2023"}
_CENTOS7 = {"ID": "centos", "ID_LIKE": "rhel fedora", "VERSION_ID": "7"}
_UBI = {"ID": "rhel", "ID_LIKE": "fedora", "VERSION_ID": "9.4"}
_LEAP = {"ID": "opensuse-leap", "ID_LIKE": "suse opensuse"}
_SLES = {"ID": "sles", "ID_LIKE": "suse"}


@pytest.fixture
def linux_host(monkeypatch):
    """Pretend to be Linux with an os-release and package managers, never the host's own."""
    monkeypatch.setattr(soffice_discovery.sys, "platform", "linux")
    soffice_discovery._linux_hint.cache_clear()

    def _set(
        release: dict[str, str] | None, managers: set[str] | frozenset[str] = frozenset()
    ) -> None:
        soffice_discovery._linux_hint.cache_clear()
        if release is None:
            fake = mock.Mock(side_effect=OSError("no os-release"))
        else:
            fake = mock.Mock(return_value=release)
        monkeypatch.setattr(os_deps.platform, "freedesktop_os_release", fake)
        monkeypatch.setattr(
            os_deps.shutil, "which", lambda name: f"/usr/bin/{name}" if name in managers else None
        )

    yield _set
    soffice_discovery._linux_hint.cache_clear()


@pytest.mark.parametrize(
    ("release", "managers", "expected"),
    [
        (_AL2023, {"dnf", "yum"}, None),
        ({"ID": "ubuntu", "ID_LIKE": "debian"}, set(), _APT),
        ({"ID": "debian"}, set(), _APT),
        ({"ID": "linuxmint", "ID_LIKE": "ubuntu debian"}, set(), _APT),
        ({"ID": "fedora"}, {"dnf"}, _DNF),
        ({"ID": "rocky", "ID_LIKE": "rhel centos fedora"}, {"dnf", "yum"}, _DNF),
    ],
    ids=["al2023-with-dnf", "ubuntu", "debian", "mint", "fedora", "rocky-dnf-and-yum"],
)
def test_hint_matches_the_distribution(linux_host, release, managers, expected) -> None:
    linux_host(release, managers)
    assert soffice_discovery.soffice_install_hint() == expected


@pytest.mark.parametrize(
    ("release", "managers", "expected"),
    [
        (_CENTOS7, {"yum"}, _YUM),
        (_UBI, {"microdnf"}, _MICRODNF),
        (_CENTOS7, {"yum", "microdnf"}, _YUM),
    ],
    ids=["yum-only", "microdnf-only", "yum-before-microdnf"],
)
def test_an_rpm_host_is_told_the_manager_it_has(linux_host, release, managers, expected) -> None:
    linux_host(release, managers)
    assert soffice_discovery.soffice_install_hint() == expected


def test_an_rpm_host_with_no_supported_manager_gets_no_hint(linux_host) -> None:
    linux_host(_CENTOS7, set())
    assert soffice_discovery.soffice_install_hint() is None


@pytest.mark.parametrize(
    "managers", [set(), {"dnf"}, {"yum"}, {"dnf", "yum"}], ids=["none", "dnf", "yum", "both"]
)
@pytest.mark.parametrize("release", [_LEAP, _SLES], ids=["leap", "sles"])
def test_a_suse_host_gets_no_hint_whichever_binaries_it_carries(
    linux_host, release, managers
) -> None:
    """SUSE's native manager is ``zypper``; lineage decides, not which binaries are installed."""
    linux_host(release, managers)
    assert soffice_discovery.soffice_install_hint() is None


@pytest.mark.parametrize(
    "release", [None, {"ID": "arch"}, {"ID": "alpine"}], ids=["no-os-release", "arch", "alpine"]
)
def test_an_unrecognized_linux_gets_no_hint(linux_host, release) -> None:
    """With ``apt`` and ``dnf`` both on PATH the answer is still none: the family is unknown."""
    linux_host(release, {"apt", "dnf"})
    assert soffice_discovery.soffice_install_hint() is None


@pytest.mark.parametrize(
    ("platform_name", "expected"),
    [
        ("darwin", "brew install --cask libreoffice"),
        ("win32", "winget install TheDocumentFoundation.LibreOffice"),
    ],
)
def test_macos_and_windows_keep_their_one_manager(monkeypatch, platform_name, expected) -> None:
    """Controls: one conventional manager per OS, not probed from a gateway's PATH."""
    monkeypatch.setattr(soffice_discovery.sys, "platform", platform_name)
    assert soffice_discovery.soffice_install_hint() == expected


def test_path_hit_wins(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def which(name: str, path: object = None) -> str | None:
        calls.append((name, path))
        return "/usr/bin/soffice" if path is None else None

    monkeypatch.setattr(soffice_discovery.shutil, "which", which)
    assert soffice_discovery.find_soffice() == "/usr/bin/soffice"
    assert calls == [("soffice", None)]


def test_off_windows_a_path_miss_is_none(monkeypatch) -> None:
    """The fixed install roots are Windows-only; nothing else is probed."""
    monkeypatch.setattr(soffice_discovery.platform_compat, "IS_WINDOWS", False)
    which = mock.Mock(return_value=None)
    monkeypatch.setattr(soffice_discovery.shutil, "which", which)
    assert soffice_discovery.find_soffice() is None
    assert soffice_discovery.windows_install_path() is None
    which.assert_called_once_with("soffice")


def test_windows_falls_back_to_the_fixed_install_root(monkeypatch, tmp_path) -> None:
    root = tmp_path / "LibreOffice" / "program"
    root.mkdir(parents=True)
    hit = str(root / "soffice.exe")
    monkeypatch.setattr(soffice_discovery.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(soffice_discovery, "_SOFFICE_WINDOWS_DIRS", (str(root),))
    monkeypatch.setattr(
        soffice_discovery.shutil,
        "which",
        lambda name, path=None: hit if path == str(root) else None,
    )
    assert soffice_discovery.find_soffice() == hit
    assert soffice_discovery.windows_install_path() == hit


def test_a_hit_outside_the_asked_root_is_refused(monkeypatch, tmp_path) -> None:
    """``shutil.which(path=...)`` can answer from the working directory on Windows; a hit
    whose parent is not the root asked about is never returned, because it is executed."""
    root = tmp_path / "program"
    root.mkdir()
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.setattr(soffice_discovery.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(soffice_discovery, "_SOFFICE_WINDOWS_DIRS", (str(root),))
    monkeypatch.setattr(
        soffice_discovery.shutil,
        "which",
        lambda name, path=None: str(elsewhere / "soffice.COM") if path else None,
    )
    assert soffice_discovery.find_soffice() is None


def test_the_shipped_table_resolves_through_no_environment_variable():
    """``%ProgramFiles%`` is attacker-writable without elevation.

    Resolving through it would let a poisoned value redirect the lookup into a
    directory the user can plant a binary in, which is what pinning literal roots
    avoids — the same reasoning ``platform_compat._WINDOWS_GIT_DIRS`` records.
    """
    entries = soffice_discovery._SOFFICE_WINDOWS_DIRS
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
    monkeypatch.setattr(
        soffice_discovery.shutil, "which", lambda *a, **k: str(elsewhere / "soffice")
    )
    assert soffice_discovery.which_within("soffice", asked) is None


def test_which_within_accepts_a_hit_inside_the_asked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not reject a genuine install, and returns it absolute."""
    asked = tmp_path / "trusted-root"
    asked.mkdir()
    monkeypatch.setattr(soffice_discovery.shutil, "which", lambda *a, **k: str(asked / "soffice"))
    found = soffice_discovery.which_within("soffice", asked)
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

    Unguarded, the call answers ``.\\soffice.COM`` from the working directory, and a
    caller that puts its parent on a ``PATH`` hands it ``.`` -- a RELATIVE entry --
    which is arbitrary code execution in the process that rasterizes slides.
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
    assert soffice_discovery.which_within("soffice", empty_root) is None
