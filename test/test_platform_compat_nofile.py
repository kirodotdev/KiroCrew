"""``platform_compat.nofile_soft_limit`` is the one reader of ``RLIMIT_NOFILE``.

The host budget's fd dimension and the adaptive controller's fd signal both
size themselves from this value; keeping the ``resource`` access in
``platform_compat`` beside ``raise_nofile_soft_limit`` is what lets the Windows
answer (``0``, no rlimit) be declared once.
"""

from __future__ import annotations

import pytest

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import host_budget as hb


@pytest.mark.skipif(not pc.IS_POSIX, reason="rlimits are POSIX")
def test_posix_reads_the_soft_limit() -> None:
    import resource

    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    expected = 0 if soft == resource.RLIM_INFINITY else int(soft)
    assert pc.nofile_soft_limit() == expected


def test_windows_has_no_rlimit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc, "IS_POSIX", False)
    assert pc.nofile_soft_limit() == 0


@pytest.mark.skipif(not pc.IS_POSIX, reason="rlimits are POSIX")
def test_unreadable_rlimit_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_which):
        raise OSError("no rlimit")

    monkeypatch.setattr(pc.resource, "getrlimit", boom)
    assert pc.nofile_soft_limit() == 0


@pytest.mark.skipif(not pc.IS_POSIX, reason="rlimits are POSIX")
def test_infinity_is_unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pc.resource,
        "getrlimit",
        lambda _which: (pc.resource.RLIM_INFINITY, pc.resource.RLIM_INFINITY),
    )
    assert pc.nofile_soft_limit() == 0


def test_host_budget_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc, "nofile_soft_limit", lambda: 4321)
    assert hb._nofile_soft_limit() == 4321
