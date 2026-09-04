"""Regression tests for the symlink capability probes.

The probes answer one question — can THIS process create a real symlink — and
they answer it at conftest IMPORT time (``_HAS_SYMLINKS = _can_create_symlink()``
in ``test/conftest.py`` and ``_ROOT_HAS_REAL_SYMLINKS = _root_can_create_real_symlink()``
in repo-root ``conftest.py``).
That placement is what fixes their error contract: a raise here is not a loud
signal on one test, it is a collection error that takes the whole session down,
including every test that never touches a symlink. So the probes treat any
failure to create one as "this host cannot", and the capability tests they guard
skip rather than the suite failing to start.

The pull the other way is real and worth naming: swallowing everything means an
unexpected filesystem fault silently disables symlink coverage instead of
reporting itself. That trade is settled in favour of the suite still running,
because the alternative failure mode is total and hits contributors who changed
nothing in this area — an errno allowlist has to enumerate every way a
filesystem can decline, and the ones it misses (a read-only or full temp dir, an
overlay/network mount returning EINVAL) are exactly the environments least
likely to have been anticipated.
"""

from __future__ import annotations

import errno
import os
import pathlib
from typing import Any, Callable

import pytest

import conftest as test_conftest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def root_conftest_module(request: pytest.FixtureRequest) -> Any:
    """The live rootdir ``conftest.py`` module object."""
    plugin = request.config.pluginmanager.get_plugin(str(_REPO_ROOT / "conftest.py"))
    assert plugin is not None, "the rootdir conftest is not registered as a plugin"
    return plugin


@pytest.fixture(params=["test_conftest", "root_conftest"])
def symlink_probe(request: pytest.FixtureRequest, root_conftest_module: Any) -> Callable[[], bool]:
    """Provide both the ``test/conftest.py`` probe and the rootdir probe."""
    if request.param == "test_conftest":
        return test_conftest._can_create_symlink
    return root_conftest_module._root_can_create_real_symlink


@pytest.mark.parametrize(
    "error_number",
    [errno.EPERM, errno.EACCES, getattr(errno, "EOPNOTSUPP", errno.EPERM), errno.ENOSYS],
)
def test_capability_errnos_disable_symlink_tests_without_aborting_collection(
    monkeypatch: pytest.MonkeyPatch,
    symlink_probe: Callable[[], bool],
    error_number: int,
) -> None:
    """The ordinary ways a host declines: reported as "no capability"."""

    def _unsupported(*args: object, **kwargs: object) -> None:
        raise OSError(error_number, "symlink capability unavailable")

    monkeypatch.setattr(os, "symlink", _unsupported)

    assert symlink_probe() is False


def test_windows_privilege_error_disables_symlink_tests(
    monkeypatch: pytest.MonkeyPatch,
    symlink_probe: Callable[[], bool],
) -> None:
    """The Windows shape: ``SeCreateSymbolicLinkPrivilege`` not held.

    Carries an unrelated errno alongside ``winerror`` 1314, which is what an
    errno-keyed probe would miss — the privilege case has to survive on the
    ``OSError`` type alone.
    """
    unavailable = OSError(errno.EIO, "privilege not held")
    unavailable.winerror = 1314  # type: ignore[attr-defined]

    def _unsupported(*args: object, **kwargs: object) -> None:
        raise unavailable

    monkeypatch.setattr(os, "symlink", _unsupported)

    assert symlink_probe() is False


def test_an_unanticipated_oserror_still_only_disables_the_capability(
    monkeypatch: pytest.MonkeyPatch,
    symlink_probe: Callable[[], bool],
) -> None:
    """THE ONE THAT MATTERS: an errno nobody enumerated must not abort the run.

    A read-only or full temp dir, or an overlay/network mount that declines with
    something outside the usual set, has to degrade to "no symlinks here" like
    any other decline. Letting it propagate turns a capability probe into a
    conftest import error, and a contributor who touched none of this gets a
    suite that will not collect at all.
    """

    def _broken(*args: object, **kwargs: object) -> None:
        raise OSError(errno.EIO, "unexpected filesystem failure")

    monkeypatch.setattr(os, "symlink", _broken)

    assert symlink_probe() is False


def test_a_host_that_has_no_symlink_call_at_all_is_handled(
    monkeypatch: pytest.MonkeyPatch,
    symlink_probe: Callable[[], bool],
) -> None:
    """``os.symlink`` is not guaranteed to exist or be implemented everywhere."""

    def _absent(*args: object, **kwargs: object) -> None:
        raise NotImplementedError("no symlink on this platform")

    monkeypatch.setattr(os, "symlink", _absent)

    assert symlink_probe() is False


def test_missing_os_symlink_attribute_is_handled(
    monkeypatch: pytest.MonkeyPatch,
    symlink_probe: Callable[[], bool],
) -> None:
    """Platforms where os has no symlink attribute at all (e.g. stripped runtime)."""
    monkeypatch.delattr(os, "symlink", raising=False)

    assert symlink_probe() is False
