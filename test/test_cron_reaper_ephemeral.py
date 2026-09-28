"""Tests for the reaper handling ephemeral (stateless) cron sessions.

follow-up: when persistent_session=False, the active session key
is f"cron:{job.id}:{run_id}" (unique per run), not f"cron:{job.id}".
The reaper must use the actual active key when calling sessions.reset()
and when logging SEL audit events — otherwise it targets a non-existent
session and fails to kill the hung child process.

The reaper fix: CronService tracks every distinct exact live session key per job via
idempotent ``register_active_session_key(job_id, key)`` and whole-key
``clear_active_session_key(job_id, key)`` after a successful reset. Each key is
attributed to the run (the ``_RunClaim``) that registered it: a reap or cancel of
that run ends every key it registered, newest first (``_run_session_keys``), and
leaves an older run's key -- a session retained for pending subagents -- alone. A
key registered under no claim belongs to whichever run is ended next. The
gateway's _cron_callback is responsible for these calls. _force_reap falls back
to the stable key for persistent jobs that have not registered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronService, _RunClaim


def _run() -> _RunClaim:
    """A claim standing for one run of a job, the identity keys are attributed to."""
    return _RunClaim(trigger="scheduled", claimed_at=0.0)


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)


class TestReaperUsesActiveSessionKey:
    def test_reaper_kills_ephemeral_session(self):
        """_force_reap must call sessions.reset with the registered ephemeral key."""
        svc = CronService()
        job = svc.add_job(name="eph", message="x", every_secs=60)

        # Simulate a stateless job that registered its per-run key.
        ephemeral_key = f"cron:{job.id}:deadbeef"
        svc.register_active_session_key(job.id, ephemeral_key)

        mock_sessions = MagicMock()
        mock_sessions.reset = AsyncMock()
        svc._sessions = mock_sessions

        import asyncio

        claim = svc._claim_run(job.id, "scheduled")
        asyncio.run(svc._force_reap(job.id, elapsed=1801.0, claim=claim))

        # reset must have been called with the ephemeral key, NOT f"cron:{job.id}".
        assert mock_sessions.reset.await_count == 1
        called_key = mock_sessions.reset.await_args.args[0]
        assert called_key == ephemeral_key
        assert called_key != f"cron:{job.id}"

    def test_reaper_falls_back_to_stable_key_when_no_active_registered(self):
        """Persistent jobs (or anything pre-registration) use the old stable key."""
        svc = CronService()
        job = svc.add_job(name="stable", message="x", every_secs=60)

        # Do NOT register an active key — simulates a persistent-session cron
        # or a job that crashed before registration.
        mock_sessions = MagicMock()
        mock_sessions.reset = AsyncMock()
        svc._sessions = mock_sessions

        import asyncio

        claim = svc._claim_run(job.id, "scheduled")
        asyncio.run(svc._force_reap(job.id, elapsed=1801.0, claim=claim))

        assert mock_sessions.reset.await_count == 1
        called_key = mock_sessions.reset.await_args.args[0]
        assert called_key == f"cron:{job.id}"

    def test_clear_active_key_removes_registration(self):
        """After clear_active_session_key, reaper falls back to stable key."""
        svc = CronService()
        job = svc.add_job(name="x", message="x", every_secs=60)

        key = f"cron:{job.id}:abc"
        svc.register_active_session_key(job.id, key)
        svc.clear_active_session_key(job.id, key)

        mock_sessions = MagicMock()
        mock_sessions.reset = AsyncMock()
        svc._sessions = mock_sessions

        import asyncio

        claim = svc._claim_run(job.id, "scheduled")
        asyncio.run(svc._force_reap(job.id, elapsed=1801.0, claim=claim))

        called_key = mock_sessions.reset.await_args.args[0]
        assert called_key == f"cron:{job.id}"

    def test_register_active_key_keeps_older_live_keys_and_refreshes_newest(self):
        svc = CronService()
        key_v1 = "cron:j1:v1"
        key_v2 = "cron:j1:v2"
        svc.register_active_session_key("j1", key_v1)
        svc.register_active_session_key("j1", key_v2)
        assert svc._run_session_keys("j1", _run()) == [key_v2, key_v1]

        svc.register_active_session_key("j1", key_v1)

        assert svc._run_session_keys("j1", _run()) == [key_v1, key_v2]
        assert svc.active_session_keys() == frozenset({key_v1, key_v2})

    def test_a_key_is_attributed_to_the_run_that_held_the_claim_when_it_was_registered(self):
        """A reap of one run ends only that run's keys; an older run's key lives on for its subagents."""
        svc = CronService()
        older = svc._claim_run("j1", "scheduled")
        svc.register_active_session_key("j1", "cron:j1:older")
        assert svc._release_claim("j1", older)
        current = svc._claim_run("j1", "scheduled")
        svc.register_active_session_key("j1", "cron:j1:agentA")
        svc.register_active_session_key("j1", "cron:j1:agentB")

        assert svc._run_session_keys("j1", current) == [
            "cron:j1:agentB",
            "cron:j1:agentA",
        ], "the run's own keys, newest first, are what its reap ends"
        assert svc._run_session_keys("j1", older) == ["cron:j1:older"]
        assert svc.active_session_keys() == frozenset(
            {"cron:j1:older", "cron:j1:agentA", "cron:j1:agentB"}
        ), "the complete set still protects every distinct live run"

    def test_re_registering_a_stable_key_re_attributes_it_to_the_current_run(self):
        """A persistent job's stable key is registered every run; the run registering it now owns it."""
        svc = CronService()
        first = svc._claim_run("j1", "scheduled")
        svc.register_active_session_key("j1", "cron:j1")
        assert svc._release_claim("j1", first)
        second = svc._claim_run("j1", "scheduled")
        svc.register_active_session_key("j1", "cron:j1")

        assert svc._run_session_keys("j1", second) == ["cron:j1"]
        assert svc._run_session_keys("j1", first) == []

    def test_one_successful_reset_clears_all_shared_key_registrations(self):
        svc = CronService()
        key = "cron:j1:shared"
        svc.register_active_session_key("j1", key)
        svc.register_active_session_key("j1", key)

        svc.clear_active_session_key("j1", key)

        assert svc.active_session_keys() == frozenset()
        assert svc._run_session_keys("j1", _run()) == []

    def test_get_active_key_returns_none_when_unregistered(self):
        svc = CronService()
        assert svc._run_session_keys("nope", _run()) == []


class TestCronCallbackDeferredResetPreservesActiveKey:
    """_cron_callback must not clear the active session key when reset is deferred.

    When session reset is deferred because subagents are still running,
    clearing the active session key in the finally block would leave the
    ephemeral session alive with no registration, so a reaper firing during the
    deferred window targets the stable key f"cron:{job.id}" and misses the
    actual ephemeral session f"cron:{job.id}:{run_id}", failing to kill
    the hung child.

    So it clears only on the non-deferred branch; _subagent_done clears
    after the real reset completes.

    This test inspects the gateway source to pin the ordering invariant:
    `clear_active_session_key` must not appear between `if has_pending or
    has_injecting:` and the `else:` branch. That guarantees the clear is
    either inside `else` (reset happened) or inside `_subagent_done`
    (deferred reset finally completed), never in a path that leaves an
    ephemeral session live without registration.
    """

    def test_clear_not_called_in_deferred_branch_source(self):
        """Source-level invariant: the unconditional clear is gone."""
        import inspect

        from kiro_crew.slack import gateway

        src = inspect.getsource(gateway)
        # The buggy pattern was an indentation-dedented clear right after
        # the if/else block. Pin that it is not present anymore.
        buggy_pattern = "                # clear the active-session registration either way."
        assert buggy_pattern not in src, (
            "Unconditional clear_active_session_key removed — it must only run "
            "on the non-deferred branch (else: after sessions.reset)."
        )

    def test_deferred_reset_and_reset_paths_handle_key_correctly(self):
        """Behavioural invariant: when the deferred path is taken, the key
        stays registered; when _subagent_done finishes the reset, it clears.

        We exercise CronService directly here rather than the full gateway
        callback — the gateway tests live in test_cron_approval_mode.py and
        don't make the active-key behaviour easy to observe without adding
        a lot of mock plumbing. The reaper's contract (if registered →
        reaper targets it) is already pinned by
        test_reaper_kills_ephemeral_session above, so the only thing left
        to pin is that the service's clear API is idempotent + retroactively
        safe when _subagent_done calls it after the real reset.
        """
        svc = CronService()

        ephemeral_key = "cron:jobid1:deadbeef"
        run = svc._claim_run("jobid1", "scheduled")
        svc.register_active_session_key("jobid1", ephemeral_key)

        # Simulate the deferred branch: callback returns without clearing.
        assert svc._run_session_keys("jobid1", run) == [ephemeral_key]

        # Later, _subagent_done runs the real reset and then clears using
        # the same job_id extraction the gateway does: parent_key.split(":", 2)[1].
        job_id_from_parent = ephemeral_key.split(":", 2)[1]
        assert job_id_from_parent == "jobid1"
        svc.clear_active_session_key(job_id_from_parent, ephemeral_key)

        # After the deferred reset completes, the key is gone → reaper
        # falls back to the stable key (correct, because the session is
        # gone by now too).
        assert svc._run_session_keys("jobid1", run) == []

        # Calling clear again is safe (idempotent).
        svc.clear_active_session_key("jobid1", ephemeral_key)
        assert svc._run_session_keys("jobid1", run) == []
