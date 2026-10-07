"""PPTX Maker — the LibreOffice install hint follows the Linux distribution.

The hint is shown as "install it with: <command>", so it must be a command that
works on this host. Amazon Linux 2023 reports ``ID_LIKE=fedora`` but packages no
LibreOffice, so it gets no command at all rather than a ``dnf`` line that fails.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kiro_crew.apps.builtins.pptx_maker.backend import preview_tools
from kiro_crew.browser_cli import os_deps


@pytest.fixture
def linux_host(monkeypatch):
    """Pretend to be Linux with a given os-release, never the developer's own."""
    monkeypatch.setattr(preview_tools.sys, "platform", "linux")
    preview_tools._linux_soffice_hint.cache_clear()

    def _set(release: dict[str, str] | None) -> None:
        preview_tools._linux_soffice_hint.cache_clear()
        if release is None:
            fake = mock.Mock(side_effect=OSError("no os-release"))
        else:
            fake = mock.Mock(return_value=release)
        monkeypatch.setattr(os_deps.platform, "freedesktop_os_release", fake)

    yield _set
    preview_tools._linux_soffice_hint.cache_clear()


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        ({"ID": "amzn", "ID_LIKE": "fedora", "VERSION_ID": "2023"}, None),
        ({"ID": "ubuntu", "ID_LIKE": "debian"}, "sudo apt install libreoffice"),
        ({"ID": "debian"}, "sudo apt install libreoffice"),
        ({"ID": "linuxmint", "ID_LIKE": "ubuntu debian"}, "sudo apt install libreoffice"),
        ({"ID": "fedora"}, "sudo dnf install libreoffice"),
        ({"ID": "rocky", "ID_LIKE": "rhel centos fedora"}, "sudo dnf install libreoffice"),
    ],
    ids=["al2023", "ubuntu", "debian", "mint", "fedora", "rocky"],
)
def test_hint_matches_the_distribution(linux_host, release, expected) -> None:
    linux_host(release)
    assert preview_tools.soffice_hint() == expected


@pytest.mark.parametrize("release", [None, {"ID": "arch"}], ids=["no-os-release", "unknown"])
def test_unrecognized_linux_keeps_the_generic_hint(linux_host, release) -> None:
    linux_host(release)
    assert preview_tools.soffice_hint() == preview_tools._SOFFICE_HINTS["linux"]


def test_al2023_hint_never_names_dnf(linux_host) -> None:
    linux_host({"ID": "amzn", "ID_LIKE": "fedora", "VERSION_ID": "2023"})
    assert "dnf" not in str(preview_tools.soffice_hint())


def test_deps_omit_a_missing_hint(linux_host, monkeypatch) -> None:
    """``/deps`` drops the hint key rather than sending an empty command."""
    from kiro_crew.apps.builtins.pptx_maker.backend import routes

    linux_host({"ID": "amzn", "ID_LIKE": "fedora", "VERSION_ID": "2023"})
    monkeypatch.setattr(routes.engine, "optional_dep_path", lambda name: None)
    assert routes._deps_status()["hints"] == {}
