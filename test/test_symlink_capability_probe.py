"""Regression tests for the root conftest's symlink capability probe.

The probe answers one question — can THIS process create a real symlink — and
it answers it at conftest IMPORT time (``_HAS_SYMLINKS = _can_create_symlink()``).
That placement is what fixes its error contract: a raise here is not a loud
signal on one test, it is a collection error that takes the whole session down,
including every test that never touches a symlink. So the probe treats any
failure to create one as "this host cannot", and the capability tests it guards
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

import pytest

import conftest as root_conftest


@pytest.mark.parametrize(
    "error_number",
    [errno.EPERM, errno.EACCES, getattr(errno, "EOPNOTSUPP", errno.EPERM), errno.ENOSYS],
)
def test_capability_errnos_disable_symlink_tests_without_aborting_collection(
    monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    """The ordinary ways a host declines: reported as "no capability"."""

    def _unsupported(*args: object, **kwargs: object) -> None:
        raise OSError(error_number, "symlink capability unavailable")

    monkeypatch.setattr(root_conftest.os, "symlink", _unsupported)

    assert root_conftest._can_create_symlink() is False


def test_windows_privilege_error_disables_symlink_tests(
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr(root_conftest.os, "symlink", _unsupported)

    assert root_conftest._can_create_symlink() is False


def test_an_unanticipated_oserror_still_only_disables_the_capability(
    monkeypatch: pytest.MonkeyPatch,
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

    monkeypatch.setattr(root_conftest.os, "symlink", _broken)

    assert root_conftest._can_create_symlink() is False


def test_a_host_that_has_no_symlink_call_at_all_is_handled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.symlink`` is not guaranteed to exist or be implemented everywhere."""

    def _absent(*args: object, **kwargs: object) -> None:
        raise NotImplementedError("no symlink on this platform")

    monkeypatch.setattr(root_conftest.os, "symlink", _absent)

    assert root_conftest._can_create_symlink() is False
