"""``kirocrew gateway-pid`` prints this home's gateway-lock holder as JSON.

Read-only: it resolves the incumbent through the same socket-independent
:func:`gateway_lock.lock_holder` oracle that ``stop --expect-pid``/``restart``
use, so it names a draining gateway that has released its listener socket but
still holds ``gateway.lock`` — the identity the desktop's listener snapshot
cannot see. The lock oracle is faked; every branch
asserts the exact JSON and exit code the desktop supervisor parses.
"""

import json

import pytest

from kiro_crew import cli_server
from kiro_crew.gateway_lock import LockHolder, LockProbeError


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(cli_server, "config_dir", lambda: tmp_path)
    return tmp_path


def _patch_holder(monkeypatch, result):
    def holder(_home):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cli_server, "lock_holder", holder)


def test_live_holder_is_named_as_process(home, monkeypatch, capsys):
    """A live holder prints holder=process with its pid, so the caller can wait on it."""
    _patch_holder(monkeypatch, LockHolder(pid=4242, alive=True, source="flock_owner"))

    cli_server._gateway_pid()

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {
        "holder": "process",
        "pid": 4242,
        "alive": True,
        "source": "flock_owner",
    }


def test_recorded_pid_fallback_is_named(home, monkeypatch, capsys):
    """The non-Linux recorded-pid holder (Windows) is named too."""
    _patch_holder(monkeypatch, LockHolder(pid=777, alive=True, source="recorded_pid"))

    cli_server._gateway_pid()

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["holder"] == "process"
    assert payload["pid"] == 777
    assert payload["source"] == "recorded_pid"


def test_free_lock_is_nobody(home, monkeypatch, capsys):
    """A free lock (pid=None) prints holder=nobody and exits 0 — safe to spawn now."""
    _patch_holder(monkeypatch, LockHolder(pid=None, alive=False, source="none"))

    cli_server._gateway_pid()

    assert json.loads(capsys.readouterr().out.strip()) == {"holder": "nobody"}


def test_dead_recorded_pid_is_nobody(home, monkeypatch, capsys):
    """A holder whose named pid is not alive is reported as nobody, never as a live holder."""
    _patch_holder(monkeypatch, LockHolder(pid=4242, alive=False, source="recorded_pid"))

    cli_server._gateway_pid()

    assert json.loads(capsys.readouterr().out.strip()) == {"holder": "nobody"}


def test_indeterminate_probe_exits_2(home, monkeypatch, capsys):
    """A probe that cannot establish the lock's state must NOT read as free.

    This is the 'could not even tell if it is held' case (held=False): nothing
    is known, so spawning blind would race and the caller must not wait
    unbounded either. The probe prints holder=indeterminate and exits non-zero.
    """
    _patch_holder(
        monkeypatch,
        LockProbeError(home / "gateway.lock", OSError("cannot open the lock file")),
    )

    with pytest.raises(SystemExit) as exc:
        cli_server._gateway_pid()

    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["holder"] == "indeterminate"
    assert "reason" in payload


def test_held_but_unnameable_is_holder_held(home, monkeypatch, capsys):
    """A lock positively HELD but with an unnameable owner is reported as held.

    This is the Windows mandatory-lock state: the draining gateway holds the lock but
    its recorded pid is unreadable under the mandatory lock. A gateway IS alive,
    so the caller waits for release rather than giving up -> holder=held, exit 0
    (no SystemExit).
    """
    _patch_holder(
        monkeypatch,
        LockProbeError(
            home / "gateway.lock",
            OSError("the lock is held but no live holder pid can be established"),
            held=True,
        ),
    )

    cli_server._gateway_pid()  # must NOT raise SystemExit

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["holder"] == "held"
    assert "reason" in payload
