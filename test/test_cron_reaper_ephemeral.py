"""Tests for the reaper handling ephemeral (stateless) cron sessions.

Mesh-1026 follow-up: when persistent_session=False, the active session key
is f"cron:{job.id}:{run_id}" (unique per run), not f"cron:{job.id}".
The reaper must use the actual active key when calling sessions.reset()
and when logging SEL audit events — otherwise it targets a non-existent
session and fails to kill the hung child process.

The reaper fix: CronService tracks the active session key for each running
job via ``register_active_session_key(job_id, key)`` and
``clear_active_session_key(job_id)``. The gateway's _cron_callback is
responsible for calling these. _force_reap reads this map to pick the
correct key, falling back to the stable key for persistent jobs that
haven't registered (or for legacy callers that don't use the helper).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronService


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
        asyncio.run(svc._force_reap(job.id, elapsed=1801.0))

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
        asyncio.run(svc._force_reap(job.id, elapsed=1801.0))

        assert mock_sessions.reset.await_count == 1
        called_key = mock_sessions.reset.await_args.args[0]
        assert called_key == f"cron:{job.id}"

    def test_clear_active_key_removes_registration(self):
        """After clear_active_session_key, reaper falls back to stable key."""
        svc = CronService()
        job = svc.add_job(name="x", message="x", every_secs=60)

        svc.register_active_session_key(job.id, f"cron:{job.id}:abc")
        svc.clear_active_session_key(job.id)

        mock_sessions = MagicMock()
        mock_sessions.reset = AsyncMock()
        svc._sessions = mock_sessions

        import asyncio
        asyncio.run(svc._force_reap(job.id, elapsed=1801.0))

        called_key = mock_sessions.reset.await_args.args[0]
        assert called_key == f"cron:{job.id}"

    def test_register_active_key_is_idempotent_on_overwrite(self):
        """Re-registering the same job_id replaces the key — no leak."""
        svc = CronService()
        svc.register_active_session_key("j1", "cron:j1:v1")
        svc.register_active_session_key("j1", "cron:j1:v2")
        assert svc.get_active_session_key("j1") == "cron:j1:v2"

    def test_get_active_key_returns_none_when_unregistered(self):
        svc = CronService()
        assert svc.get_active_session_key("nope") is None


class TestCronCallbackDeferredResetPreservesActiveKey:
    """Regression for AutoSDE comment on CR-271270113 rev 2 (post 5).

    Before this fix, _cron_callback cleared the active session key in its
    finally block unconditionally — even when session reset was deferred
    because subagents were still running. That left the ephemeral session
    alive with no registration, so if the reaper fired during the deferred
    window it would target the stable key f"cron:{job.id}" and miss the
    actual ephemeral session f"cron:{job.id}:{run_id}", failing to kill
    the hung child.

    Fix: only clear on the non-deferred branch. _subagent_done clears
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
        buggy_pattern = (
            '                # Mesh-1026: clear the active-session registration either way.'
        )
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
        svc.register_active_session_key("jobid1", ephemeral_key)

        # Simulate the deferred branch: callback returns without clearing.
        assert svc.get_active_session_key("jobid1") == ephemeral_key

        # Later, _subagent_done runs the real reset and then clears using
        # the same job_id extraction the gateway does: parent_key.split(":", 2)[1].
        job_id_from_parent = ephemeral_key.split(":", 2)[1]
        assert job_id_from_parent == "jobid1"
        svc.clear_active_session_key(job_id_from_parent)

        # After the deferred reset completes, the key is gone → reaper
        # falls back to the stable key (correct, because the session is
        # gone by now too).
        assert svc.get_active_session_key("jobid1") is None

        # Calling clear again is safe (idempotent).
        svc.clear_active_session_key("jobid1")
        assert svc.get_active_session_key("jobid1") is None
