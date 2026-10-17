"""PPTX Maker — the LibreOffice install hint names only a package manager the host has.

The hint is shown as "install it with: <command>", so its first executable must exist on
this host and its package name must resolve there. The apt family is decided by lineage.
The rpm family carries ``dnf``, ``yum`` or ``microdnf`` depending on the generation, so the
manager is probed the way the browser installer probes it (``os_deps.rpm_package_manager``).
Amazon Linux 2023 reports ``ID_LIKE=fedora`` but packages no LibreOffice, SUSE's manager is
``zypper``, and an unrecognized distribution is unknown: those get no command at all rather
than a guessed ``apt`` or ``dnf`` line.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kiro_crew.apps.builtins.pptx_maker.backend import preview_tools
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
    """Pretend to be Linux with an os-release and package managers, never the developer's own."""
    monkeypatch.setattr(preview_tools.sys, "platform", "linux")
    preview_tools._linux_soffice_hint.cache_clear()

    def _set(
        release: dict[str, str] | None, managers: set[str] | frozenset[str] = frozenset()
    ) -> None:
        preview_tools._linux_soffice_hint.cache_clear()
        if release is None:
            fake = mock.Mock(side_effect=OSError("no os-release"))
        else:
            fake = mock.Mock(return_value=release)
        monkeypatch.setattr(os_deps.platform, "freedesktop_os_release", fake)
        monkeypatch.setattr(
            os_deps.shutil, "which", lambda name: f"/usr/bin/{name}" if name in managers else None
        )

    yield _set
    preview_tools._linux_soffice_hint.cache_clear()


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
    """Controls: the hosts the hint already served keep exactly the command they had."""
    linux_host(release, managers)
    assert preview_tools.soffice_hint() == expected


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
    assert preview_tools.soffice_hint() == expected


def test_an_rpm_host_with_no_supported_manager_gets_no_hint(linux_host) -> None:
    linux_host(_CENTOS7, set())
    assert preview_tools.soffice_hint() is None


@pytest.mark.parametrize(
    "managers", [set(), {"dnf"}, {"yum"}, {"dnf", "yum"}], ids=["none", "dnf", "yum", "both"]
)
@pytest.mark.parametrize("release", [_LEAP, _SLES], ids=["leap", "sles"])
def test_a_suse_host_gets_no_hint_whichever_binaries_it_carries(
    linux_host, release, managers
) -> None:
    """SUSE's native manager is ``zypper``; lineage decides, not which binaries are installed."""
    linux_host(release, managers)
    assert preview_tools.soffice_hint() is None


@pytest.mark.parametrize(
    "release", [None, {"ID": "arch"}, {"ID": "alpine"}], ids=["no-os-release", "arch", "alpine"]
)
def test_an_unrecognized_linux_gets_no_hint(linux_host, release) -> None:
    """With ``apt`` and ``dnf`` both on PATH the answer is still none: the family is unknown."""
    linux_host(release, {"apt", "dnf"})
    assert preview_tools.soffice_hint() is None


@pytest.mark.parametrize(
    ("platform_name", "expected"),
    [
        ("darwin", "brew install --cask libreoffice"),
        ("win32", "winget install TheDocumentFoundation.LibreOffice"),
    ],
)
def test_macos_and_windows_keep_their_one_manager(monkeypatch, platform_name, expected) -> None:
    """Controls: one conventional manager per OS, not probed from a gateway's PATH."""
    monkeypatch.setattr(preview_tools.sys, "platform", platform_name)
    assert preview_tools.soffice_hint() == expected


def test_deps_carry_the_probed_manager(linux_host, monkeypatch) -> None:
    """``/deps`` forwards the hint through the real ``_deps_status``, not a stand-in."""
    from kiro_crew.apps.builtins.pptx_maker.backend import routes

    linux_host(_CENTOS7, {"yum"})
    monkeypatch.setattr(routes.engine, "optional_dep_path", lambda name: None)
    assert routes._deps_status()["hints"] == {"soffice": _YUM}


@pytest.mark.parametrize(
    "release", [_AL2023, _LEAP, {"ID": "arch"}], ids=["al2023", "suse-leap", "arch"]
)
def test_deps_omit_a_missing_hint(linux_host, monkeypatch, release) -> None:
    """``/deps`` drops the hint key rather than sending an empty command."""
    from kiro_crew.apps.builtins.pptx_maker.backend import routes

    linux_host(release, {"dnf", "yum"})
    monkeypatch.setattr(routes.engine, "optional_dep_path", lambda name: None)
    assert routes._deps_status()["hints"] == {}
