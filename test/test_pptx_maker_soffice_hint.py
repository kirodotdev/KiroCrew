"""PPTX Maker — ``/deps`` forwards the core's LibreOffice install hint.

The hint itself (which command each host is told, and which hosts get none) is the
core's ``kiro_crew.soffice_discovery.soffice_install_hint``, pinned by
``test/test_soffice_discovery.py``. What is pinned here is that this app's ``/deps``
carries that hint through the real ``_deps_status`` and drops the key when there is no
command to give.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kiro_crew import soffice_discovery
from kiro_crew.browser_cli import os_deps

_YUM = "sudo yum install libreoffice"

_AL2023 = {"ID": "amzn", "ID_LIKE": "fedora", "VERSION_ID": "2023"}
_CENTOS7 = {"ID": "centos", "ID_LIKE": "rhel fedora", "VERSION_ID": "7"}
_LEAP = {"ID": "opensuse-leap", "ID_LIKE": "suse opensuse"}


@pytest.fixture
def linux_host(monkeypatch):
    """Pretend to be Linux with an os-release and package managers, never the developer's own."""
    monkeypatch.setattr(soffice_discovery.sys, "platform", "linux")
    soffice_discovery._linux_hint.cache_clear()

    def _set(release: dict[str, str], managers: set[str]) -> None:
        soffice_discovery._linux_hint.cache_clear()
        monkeypatch.setattr(
            os_deps.platform, "freedesktop_os_release", mock.Mock(return_value=release)
        )
        monkeypatch.setattr(
            os_deps.shutil, "which", lambda name: f"/usr/bin/{name}" if name in managers else None
        )

    yield _set
    soffice_discovery._linux_hint.cache_clear()


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
