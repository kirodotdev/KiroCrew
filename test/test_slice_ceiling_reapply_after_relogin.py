"""The agent slice's aggregate ceilings must come back after the user logs in again.

Both slice ceilings (``MemoryHigh``, and ``MemoryMax``/``TasksMax``) are
``systemctl --user set-property --runtime`` properties, so they vanish with the
login session. ``_probe_cgroup_scope`` follows the session and flips back to
"available" inside one gateway process, and it then counts a new enforcement
epoch. ``MemoryHigh`` state recorded under an older epoch (an applied value, a
failed apply) must not stop the next spawn from applying it to the new user
manager. The hard ceiling has its own re-apply in the pressure sampler once it
was applied; the spawn path only covers a session that never got it.

Every ``systemctl`` call here goes to a fake ``subprocess.run``; the host's user
manager is never contacted.
"""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat, sandbox


@pytest.fixture
def systemctl(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fake ``systemctl`` that records each ``set-property`` call and can be made to fail."""
    state = SimpleNamespace(calls=[], returncode=0)

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        state.calls.append(list(argv))
        return SimpleNamespace(returncode=state.returncode, stdout="", stderr="bus gone")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(platform_compat, "trusted_system_bin", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sandbox, "_default_slice_memory_high_mb", lambda: 512)
    monkeypatch.setattr(sandbox, "_slice_limits_from_config", lambda: (2048, 4096))
    monkeypatch.setattr(sandbox, "_CGROUP_SCOPE_EPOCH", 1)
    monkeypatch.setattr(sandbox, "_SLICE_MEMHIGH_APPLIED", None)
    monkeypatch.setattr(sandbox, "_SLICE_MEMHIGH_DISABLED", False)
    monkeypatch.setattr(sandbox, "_SLICE_MEMHIGH_EPOCH", None)
    monkeypatch.setattr(sandbox, "_SLICE_LIMITS_APPLIED", False)
    monkeypatch.setattr(sandbox, "_SLICE_LIMITS_EPOCH", None)
    return state


def _memhigh_calls(state: SimpleNamespace) -> list[list[str]]:
    return [c for c in state.calls if any(a.startswith("MemoryHigh=") for a in c)]


def _limits_calls(state: SimpleNamespace) -> list[list[str]]:
    return [c for c in state.calls if any(a.startswith("MemoryMax=") for a in c)]


def _relogin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_CGROUP_SCOPE_EPOCH", sandbox._cgroup_scope_epoch() + 1)


def test_memory_high_is_applied_again_after_a_relogin(systemctl, monkeypatch) -> None:
    sandbox._ensure_agent_slice_memory_high()
    sandbox._ensure_agent_slice_memory_high()  # same session: a string compare
    assert len(_memhigh_calls(systemctl)) == 1

    _relogin(monkeypatch)
    sandbox._ensure_agent_slice_memory_high()

    assert len(_memhigh_calls(systemctl)) == 2
    assert sandbox._SLICE_MEMHIGH_APPLIED == "512M"


def test_a_failure_seen_at_logout_disarms_only_that_session(systemctl, monkeypatch) -> None:
    systemctl.returncode = 1
    sandbox._ensure_agent_slice_memory_high()
    sandbox._ensure_agent_slice_memory_high()  # disarmed: no retry in this session
    assert len(_memhigh_calls(systemctl)) == 1
    assert sandbox._SLICE_MEMHIGH_DISABLED is True

    systemctl.returncode = 0
    _relogin(monkeypatch)
    sandbox._ensure_agent_slice_memory_high()

    assert len(_memhigh_calls(systemctl)) == 2
    assert sandbox._SLICE_MEMHIGH_DISABLED is False
    assert sandbox._SLICE_MEMHIGH_APPLIED == "512M"


def test_a_kill_switch_set_outside_a_reconcile_survives_a_relogin(systemctl, monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "_SLICE_MEMHIGH_DISABLED", True)
    _relogin(monkeypatch)

    sandbox._ensure_agent_slice_memory_high()

    assert systemctl.calls == []
    assert sandbox._SLICE_MEMHIGH_DISABLED is True


def test_the_spawn_path_schedules_a_reconcile_after_a_failure_in_an_older_session(
    systemctl, monkeypatch
) -> None:
    systemctl.returncode = 1
    sandbox._ensure_agent_slice_memory_high()
    assert sandbox._SLICE_MEMHIGH_DISABLED is True
    systemctl.returncode = 0
    _relogin(monkeypatch)
    ran = threading.Event()
    monkeypatch.setattr(sandbox, "_reapply_slice_limits_for_new_session", lambda: None)
    monkeypatch.setattr(sandbox, "_check_slice_memory_pressure", ran.set)

    sandbox._reconcile_slice_memory_high_off_thread()

    assert ran.wait(5.0), "the reconcile worker never ran"
    with sandbox._SLICE_MEMHIGH_MUTEX:
        assert len(_memhigh_calls(systemctl)) == 2


def test_slice_limits_follow_a_gateway_that_started_logged_out(systemctl, monkeypatch) -> None:
    """Startup saw no user manager; the first spawn after login applies the hard ceiling."""
    probe = {"answer": (False, "no XDG_RUNTIME_DIR (no systemd user session)")}
    monkeypatch.setattr(sandbox, "_probe_cgroup_scope", lambda: probe["answer"])
    monkeypatch.setattr(sandbox, "_CGROUP_WARNED", True)
    assert sandbox.ensure_agents_slice_limits() is False
    assert _limits_calls(systemctl) == []

    probe["answer"] = (True, "ok")
    _relogin(monkeypatch)
    sandbox._reapply_slice_limits_for_new_session()
    sandbox._reapply_slice_limits_for_new_session()  # once per session

    assert len(_limits_calls(systemctl)) == 1
    assert "MemoryMax=2048M" in _limits_calls(systemctl)[0]
    assert sandbox._SLICE_LIMITS_APPLIED is True


def test_an_applied_slice_ceiling_is_left_to_the_sampler_self_heal(systemctl, monkeypatch) -> None:
    """A ceiling that vanished at a logout is the pressure sampler's to re-apply, not this path's."""
    monkeypatch.setattr(sandbox, "_probe_cgroup_scope", lambda: (True, "ok"))
    assert sandbox.ensure_agents_slice_limits() is True
    assert len(_limits_calls(systemctl)) == 1

    _relogin(monkeypatch)
    sandbox._reapply_slice_limits_for_new_session()

    assert len(_limits_calls(systemctl)) == 1


def test_a_failed_slice_limits_apply_is_not_retried_in_the_same_session(
    systemctl, monkeypatch
) -> None:
    monkeypatch.setattr(sandbox, "_probe_cgroup_scope", lambda: (True, "ok"))
    systemctl.returncode = 1
    assert sandbox.ensure_agents_slice_limits() is False

    for _ in range(3):
        sandbox._reapply_slice_limits_for_new_session()

    assert len(_limits_calls(systemctl)) == 1


def test_the_probe_counts_a_new_epoch_per_session_it_finds_enforcement_in(monkeypatch) -> None:
    answers = iter([(True, "ok"), (True, "ok"), (False, "bus gone"), (True, "ok"), (True, "ok")])
    sessions = iter(["s1", "s1", "gone", "s2", "s3"])
    current = {"session": "s1"}
    monkeypatch.setattr(sandbox, "_compute_cgroup_scope_probe", lambda: next(answers))
    monkeypatch.setattr(sandbox, "_cgroup_scope_session_fingerprint", lambda: current["session"])
    monkeypatch.setattr(sandbox, "_log_cgroup_scope_transition", lambda _result: None)
    monkeypatch.setattr(sandbox, "_CGROUP_SCOPE_PROBE", None)
    monkeypatch.setattr(sandbox, "_CGROUP_SCOPE_PROBE_SESSION", None)
    monkeypatch.setattr(sandbox, "_CGROUP_SCOPE_PROBE_TTL_SECONDS", 0.0)
    monkeypatch.setattr(sandbox, "_CPU_DELEGATED", None)
    monkeypatch.setattr(sandbox, "_CGROUP_SCOPE_EPOCH", 0)

    seen = []
    for _ in range(5):
        current["session"] = next(sessions)
        sandbox._probe_cgroup_scope()
        seen.append(sandbox._cgroup_scope_epoch())

    # first answer, same session (TTL recompute), logout, login, a new session
    assert seen == [1, 1, 1, 2, 3]
