"""The Windows fail-closed contract's ``config.json`` read survives the boot write.

``test_windows_fail_closed_optin.py`` asserts that a fresh Windows install does
not carry the unsandboxed-exec opt-in, and it reads ``config.json`` to do it. The
gateway writes that same file while it boots, through
``config.loader.write_config_atomically``, which stages a temp file and publishes
it with ``os.replace``. On Windows a reader that opens the destination inside that
rename window fails with ``PermissionError`` (``WinError 32``): the file is whole,
the writer is correct, and the read still loses. The contract under test says
nothing about that window, so a read that spends one attempt in it reports a
break that did not happen.

These live OUTSIDE that file for two reasons, both load-bearing:

* That file runs only on win32 and is skipped everywhere else, so a pin placed in
  it would be unobservable on the shards that run on every pull request -- exactly
  the property this pin exists to hold.
* ``ci.yml``'s ``backend-test-windows-fail-closed`` canary runs that file by node
  id and greps an exact pass count, so a test added there is a workflow edit as
  well as a test.

``windows_sim.read_sharing_violation`` reproduces the fault deterministically on
any OS by faulting ``Path.read_bytes``, which is the call
``atomic_write.read_bytes_with_retry`` makes. It does not reach ``Path.read_text``
-- CPython's pathlib text read goes through the C ``_io`` layer and never touches
the patched attribute -- so the unretried control below is spelled with
``read_bytes`` to share one interception point with the helper it is the control
for. What these prove is that the retry is wired, bounded, gated to Windows, and
limited to ``PermissionError``; the real OS behaviour is proved by the win32
canary itself.
"""

from __future__ import annotations

import json

import pytest
from test_windows_fail_closed_optin import _read_config_json
from windows_sim import read_sharing_violation

from kiro_crew import atomic_write as aw
from kiro_crew import platform_compat

#: The document the gateway writes at boot on a host that refuses unsandboxed
#: exec, reduced to the two keys the contract test reads out of it.
_SETTINGS = {"agent": {"provider": "acp", "sandbox_allow_unsandboxed_exec": False}}


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch):
    """Keep the bounded retry loop instant; attempt COUNT is what these pin."""
    monkeypatch.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0)


@pytest.fixture
def config_path(tmp_path):
    """A written ``config.json``, named as the contract test's read names it."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_SETTINGS), encoding="utf-8")
    return path


def test_one_contended_read_is_survived_rather_than_reported(config_path, monkeypatch):
    """A sharing violation then a real read: the settings still parse.

    The faulted attempt and the successful one are both counted, so ``n == 2``
    states that the recovery came from a second read and not from the fault having
    been skipped.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

    with read_sharing_violation(match="config.json", times=1) as sim:
        assert _read_config_json(config_path) == _SETTINGS

    assert sim["n"] == 2


def test_a_window_that_fills_the_budget_is_still_survived(config_path, monkeypatch):
    """Contention up to the last attempt recovers on that attempt.

    This pins that the helper spends the shared budget rather than retrying a
    token once, and that the final attempt sits outside the loop: a read faulted
    ``_REPLACE_MAX_ATTEMPTS - 1`` times has exactly one attempt left and must use
    it.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

    with read_sharing_violation(match="config.json", times=aw._REPLACE_MAX_ATTEMPTS - 1) as sim:
        assert _read_config_json(config_path) == _SETTINGS

    assert sim["n"] == aw._REPLACE_MAX_ATTEMPTS


def test_an_unretried_read_loses_the_same_window(config_path, monkeypatch):
    """The control: a single-attempt read of the same file in the same window fails.

    Without this the passing cases above would also pass a simulator that faults
    nothing, and the pin would prove only that a readable file is readable.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

    with read_sharing_violation(match="config.json", times=1):
        with pytest.raises(PermissionError):
            json.loads(config_path.read_bytes().decode("utf-8"))


def test_a_posix_permission_error_is_not_slept_over(config_path, monkeypatch):
    """On POSIX the same fault surfaces at once, because there it is a real fault.

    The platform gate's non-vacuity proof: with identical simulator settings the
    Windows case recovers and this one must not, so the recovery above is the
    Windows-gated retry doing it.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)

    with read_sharing_violation(match="config.json", times=1):
        with pytest.raises(PermissionError):
            _read_config_json(config_path)


def test_a_missing_file_fails_instead_of_being_retried(tmp_path, monkeypatch):
    """Only a sharing violation is transient, so absence must still fail.

    A ``config.json`` that is not there when the read runs is a real result about
    the gateway's boot, and sleeping over it would turn a contract failure into a
    slow pass.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

    with pytest.raises(FileNotFoundError):
        _read_config_json(tmp_path / "config.json")
