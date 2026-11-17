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


def _held(monkeypatch):
    _patch_holder(monkeypatch, LockProbeError("gateway.lock", OSError("hidden stamp"), held=True))


def test_a_held_lock_is_named_from_the_ports_fresh_run_record(home, monkeypatch, capsys):
    """With --port, a Windows lock that hides its stamp is named from the run record."""
    _held(monkeypatch)
    seen: list[int] = []
    monkeypatch.setattr(cli_server, "_recorded_gateway_pid", lambda port: seen.append(port) or 4242)

    cli_server._gateway_pid(5476)

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"holder": "process", "pid": 4242, "alive": True, "source": "run_record"}
    assert seen == [5476]


def test_a_held_lock_with_no_fresh_run_record_stays_held(home, monkeypatch, capsys):
    _held(monkeypatch)
    monkeypatch.setattr(cli_server, "_recorded_gateway_pid", lambda _port: None)

    cli_server._gateway_pid(5476)

    assert json.loads(capsys.readouterr().out.strip())["holder"] == "held"


def test_a_held_lock_without_a_port_never_reads_a_run_record(home, monkeypatch, capsys):
    _held(monkeypatch)

    def unexpected(_port):
        raise AssertionError("no --port, so no run record is read")

    monkeypatch.setattr(cli_server, "_recorded_gateway_pid", unexpected)

    cli_server._gateway_pid()

    assert json.loads(capsys.readouterr().out.strip())["holder"] == "held"


def test_an_indeterminate_lock_is_not_named_from_a_run_record(home, monkeypatch, capsys):
    _patch_holder(monkeypatch, LockProbeError("gateway.lock", OSError("unmeasurable"), held=False))
    monkeypatch.setattr(cli_server, "_recorded_gateway_pid", lambda _port: 4242)

    with pytest.raises(SystemExit) as excinfo:
        cli_server._gateway_pid(5476)

    assert excinfo.value.code == 2
    assert json.loads(capsys.readouterr().out.strip())["holder"] == "indeterminate"
