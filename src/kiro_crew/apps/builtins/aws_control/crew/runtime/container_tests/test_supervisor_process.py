"""Tests for ``container.supervisor.process`` -- process-group launch and drain.

These use real OS processes (never a Kiro Crew gateway) so that killpg,
grandchild reaping and SIGKILL escalation are exercised for real.
"""

from __future__ import annotations

import os
import signal
import time

from container.supervisor.process import spawn_process_group

from .test_supervisor_fakes import (
    fake_argv,
    pid_alive,
    write_fake_backend,
)


def _wait_for(path, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return False


def test_terminate_reaps_the_whole_group_including_a_grandchild(tmp_path):
    # A launcher plus a forked grandchild -- the two-process shape a real
    # kiro-cli worker has. Draining must take out both.
    script = write_fake_backend(tmp_path)
    parent_pidfile = tmp_path / "parent.pid"
    child_pidfile = tmp_path / "child.pid"
    pg = spawn_process_group(
        "backend",
        fake_argv(
            script,
            pidfile=parent_pidfile,
            child_pidfile=child_pidfile,
            spawn_child=True,
            ttl=30,
        ),
    )
    assert _wait_for(parent_pidfile) and _wait_for(child_pidfile)
    child_pid = int(child_pidfile.read_text())

    pg.terminate(drain_timeout=5.0)

    assert pg.poll() is not None
    # The orphan that would survive a pid-only kill is gone.
    deadline = time.monotonic() + 3.0
    while pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not pid_alive(child_pid), "grandchild survived group teardown"


def test_terminate_drains_a_cooperative_child_without_sigkill(tmp_path):
    # The child exits cleanly on SIGTERM well within the drain window.
    script = write_fake_backend(tmp_path)
    pidfile = tmp_path / "p.pid"
    pg = spawn_process_group("backend", fake_argv(script, pidfile=pidfile, ttl=30))
    assert _wait_for(pidfile)

    t0 = time.monotonic()
    code = pg.terminate(drain_timeout=5.0)
    elapsed = time.monotonic() - t0

    assert code == 0, "cooperative child should exit 0 on SIGTERM"
    assert elapsed < 4.0, "should not have waited out the whole drain window"


def test_terminate_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path):
    # A child that ignores SIGTERM must still be gone after the drain window.
    script = write_fake_backend(tmp_path)
    pidfile = tmp_path / "p.pid"
    pg = spawn_process_group(
        "backend", fake_argv(script, pidfile=pidfile, ignore_sigterm=True, ttl=30)
    )
    assert _wait_for(pidfile)

    code = pg.terminate(drain_timeout=0.5)

    assert pg.poll() is not None
    # SIGKILL shows up as a negative return code equal to -SIGKILL.
    assert code == -9, f"expected SIGKILL escalation, got {code}"


def test_terminate_is_idempotent_on_an_already_dead_group(tmp_path):
    script = write_fake_backend(tmp_path)
    pg = spawn_process_group("backend", fake_argv(script, ttl=0.2))
    # Let it exit on its own.
    deadline = time.monotonic() + 3.0
    while pg.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pg.poll() is not None
    # A second teardown must not raise.
    pg.terminate(drain_timeout=1.0)


# --- Escaped-worker topology -----------------------------------------------
# A real kiro-cli worker spawns with start_new_session (runtime.py:1321), so it
# setsid's into its OWN process group and ESCAPES the backend's group. These two
# tests model that: killpg of the backend group cannot reach the worker, so the
# worker is reaped by the backend's own SIGTERM shutdown -- which is why the
# supervisor DRAINS the backend (SIGTERM, wait) before it ever SIGKILLs.


def test_drain_reaps_a_worker_that_escaped_into_its_own_session(tmp_path):
    # Backend spawns a setsid child (an "escaped worker") and, like the real
    # gateway, reaps it on SIGTERM. Draining must leave nothing behind.
    script = write_fake_backend(tmp_path)
    ppf, cpf = tmp_path / "b.pid", tmp_path / "w.pid"
    pg = spawn_process_group(
        "backend",
        fake_argv(
            script,
            pidfile=ppf,
            child_pidfile=cpf,
            spawn_child=True,
            child_setsid=True,
            reap_on_term=True,
            ttl=30,
        ),
    )
    assert _wait_for(ppf) and _wait_for(cpf)
    worker = int(cpf.read_text())
    # The worker really is in a different process group than the backend.
    assert os.getpgid(worker) != pg.pgid

    pg.terminate(drain_timeout=5.0)

    assert pg.poll() is not None
    deadline = time.monotonic() + 3.0
    while pid_alive(worker) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not pid_alive(worker), "backend's drain should have reaped the escaped worker"


def test_group_kill_alone_cannot_reach_an_escaped_worker(tmp_path):
    # Documents the limitation the drain exists to cover: if the backend never
    # reaps (here it ignores SIGTERM and is SIGKILLed), killpg of the backend's
    # group does NOT reach a worker that setsid'd out. The supervisor must rely
    # on the backend's graceful shutdown, not on the group kill, for workers.
    script = write_fake_backend(tmp_path)
    ppf, cpf = tmp_path / "b.pid", tmp_path / "w.pid"
    pg = spawn_process_group(
        "backend",
        fake_argv(
            script,
            pidfile=ppf,
            child_pidfile=cpf,
            spawn_child=True,
            child_setsid=True,
            ignore_sigterm=True,
            ttl=6,
        ),
    )
    assert _wait_for(ppf) and _wait_for(cpf)
    worker = int(cpf.read_text())
    try:
        pg.terminate(drain_timeout=0.5)  # SIGTERM ignored -> SIGKILL the backend group
        assert pg.poll() is not None
        # The escaped worker is in its own group, so the backend-group SIGKILL
        # never touched it.
        assert pid_alive(worker), "escaped worker unexpectedly died from group kill"
    finally:
        try:
            os.killpg(worker, signal.SIGKILL)  # its own group leader: pgid == pid
        except ProcessLookupError:
            pass
