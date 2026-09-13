"""Scoped spawn SDK: owner/repository-bound job receipts for app-owned model jobs.

The backend wraps a host spawn implementation (dispatch, lookup, cancel) and
must never let a job be read, cancelled or re-dispatched outside the scope that
started it. These tests drive it with an in-memory fake host.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.apps import scoped_spawn_sdk as sdk


class FakeHost:
    """Records dispatches and lets a test flip each job's lifecycle flags."""

    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self.infos: dict[str, SimpleNamespace] = {}
        self.cancelled: list[str] = []
        self.accept_cancel = True

    async def dispatch(self, task, agent, _flag, _channel, app, **kw):
        job = "job-%d" % (len(self.dispatched) + 1)
        self.dispatched.append({"job": job, "task": task, "agent": agent, "app": app, **kw})
        self.infos[job] = SimpleNamespace(
            app=app, done=False, queued=True, user_stopped=False, error="",
            resolved_model="", app_cleanup_confirmed=None,
            app_result_text=None, app_result_error="",
            app_result_limit=kw.get("_capture_bytes", 0),
        )
        return job

    def lookup(self, job):
        return self.infos.get(job)

    async def cancel(self, job):
        self.cancelled.append(job)
        return self.accept_cancel


def _backend(host: FakeHost, **kw) -> sdk.ScopedSpawnBackend:
    return sdk.ScopedSpawnBackend(host.dispatch, host.lookup, host.cancel, **kw)


def _scope(**over) -> sdk.JobScope:
    base = dict(app="review-app", owner="alice", provider="public-github",
                repository="octo/widgets")
    base.update(over)
    return sdk.JobScope(**base)


def run(coro):
    return asyncio.run(coro)


# ── JobScope ────────────────────────────────────────────────────────────────


def test_scope_rejects_unknown_provider_and_bad_labels():
    with pytest.raises(sdk.ScopedSpawnError, match="provider"):
        _scope(provider="gitlab")
    with pytest.raises(sdk.ScopedSpawnError, match="invalid owner"):
        _scope(owner="")
    with pytest.raises(sdk.ScopedSpawnError, match="invalid repository"):
        _scope(repository="octo/\nwidgets")
    with pytest.raises(sdk.ScopedSpawnError, match="invalid app"):
        _scope(app="x" * 257)


def test_scope_is_frozen_and_comparable():
    a, b = _scope(), _scope()
    assert a == b
    with pytest.raises(Exception):
        a.owner = "mallory"  # type: ignore[misc]


# ── start / idempotent attempts ─────────────────────────────────────────────


def test_start_dispatches_once_per_attempt_and_returns_the_same_receipt():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "public-github", "octo/widgets")
    first = run(jobs.run("plan this", "text", attempt="a1", purpose="plan"))
    again = run(jobs.run("plan this", "text", attempt="a1", purpose="plan"))
    assert first == again == "job-1"
    assert len(host.dispatched) == 1
    sent = host.dispatched[0]
    assert sent["app"] == "review-app"
    assert sent["_scope_key"].startswith("appjob:") and len(sent["_scope_key"]) == 7 + 64
    assert sent["_capture_bytes"] == 131072
    assert "_native_text" not in sent


def test_same_attempt_with_a_different_request_is_refused():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "public-github", "octo/widgets")
    run(jobs.run("plan this", "text", attempt="a1", purpose="plan"))
    with pytest.raises(sdk.ScopedSpawnError, match="already bound"):
        run(jobs.run("plan THAT", "text", attempt="a1", purpose="plan"))
    with pytest.raises(sdk.ScopedSpawnError, match="already bound"):
        run(jobs.run_isolated("plan this", "text", attempt="a1", purpose="plan"))
    assert len(host.dispatched) == 1


def test_start_validates_purpose_prompt_and_limit():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "public-github", "octo/widgets")
    with pytest.raises(sdk.ScopedSpawnError, match="purpose"):
        run(jobs.run("x", "text", attempt="a1", purpose="deploy"))
    with pytest.raises(sdk.ScopedSpawnError, match="prompt"):
        run(jobs.run("   ", "text", attempt="a1", purpose="plan"))
    with pytest.raises(sdk.ScopedSpawnError, match="prompt"):
        run(jobs.run("x" * 120001, "text", attempt="a1", purpose="plan"))
    with pytest.raises(sdk.ScopedSpawnError, match="output limit"):
        run(jobs.run("x", "text", attempt="a1", purpose="plan", max_output_bytes=0))
    with pytest.raises(sdk.ScopedSpawnError, match="output limit"):
        run(jobs.run("x", "text", attempt="a1", purpose="plan", max_output_bytes=True))
    assert host.dispatched == []


def test_isolated_run_asks_the_host_for_the_native_text_profile():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "amazon-internal", "pkg/Widgets")
    run(jobs.run_isolated("review this", "text", attempt="r1", purpose="review"))
    assert host.dispatched[0]["_native_text"] is True


def test_capacity_bound_refuses_new_receipts():
    host = FakeHost()
    jobs = _backend(host, capacity=1).bind("review-app", "alice", "public-github", "octo/widgets")
    run(jobs.run("one", "text", attempt="a1", purpose="plan"))
    with pytest.raises(sdk.ScopedSpawnError, match="capacity"):
        run(jobs.run("two", "text", attempt="a2", purpose="plan"))


def test_host_returning_a_foreign_or_reused_job_is_refused():
    host = FakeHost()
    backend = _backend(host)
    jobs = backend.bind("review-app", "alice", "public-github", "octo/widgets")

    async def foreign_dispatch(task, agent, _flag, _channel, app, **kw):
        job = await host.dispatch(task, agent, _flag, _channel, app, **kw)
        host.infos[job].app = "other-app"
        return job

    backend._dispatch = foreign_dispatch
    with pytest.raises(sdk.ScopedSpawnError, match="unowned"):
        run(jobs.run("plan", "text", attempt="a1", purpose="plan"))


# ── ownership of reads and cancels ──────────────────────────────────────────


def test_another_scope_cannot_read_or_cancel_the_job():
    host = FakeHost()
    backend = _backend(host)
    alice = backend.bind("review-app", "alice", "public-github", "octo/widgets")
    bob = backend.bind("review-app", "bob", "public-github", "octo/widgets")
    other_repo = backend.bind("review-app", "alice", "public-github", "octo/gadgets")
    job = run(alice.run("plan", "text", attempt="a1", purpose="plan"))
    for stranger in (bob, other_repo):
        with pytest.raises(sdk.ScopedSpawnError, match="unavailable"):
            stranger.result(job)
        with pytest.raises(sdk.ScopedSpawnError, match="unavailable"):
            run(stranger.cancel(job))
    with pytest.raises(sdk.ScopedSpawnError, match="unavailable"):
        alice.result("job-does-not-exist")
    with pytest.raises(sdk.ScopedSpawnError, match="unavailable"):
        alice.result(None)  # type: ignore[arg-type]
    assert host.cancelled == []


def test_cancel_reports_the_host_decision_without_claiming_reaped():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "public-github", "octo/widgets")
    job = run(jobs.run("plan", "text", attempt="a1", purpose="plan"))
    accepted = run(jobs.cancel(job))
    assert accepted == {"requested": True, "reason": "host-cancel-requested",
                        "cleanup_confirmed": None}
    host.accept_cancel = False
    assert run(jobs.cancel(job))["reason"] == "host-did-not-accept-cancel"
    assert host.cancelled == [job, job]


# ── snapshot states ─────────────────────────────────────────────────────────


def _job(host, jobs, **flags):
    job = run(jobs.run("plan", "text", attempt="a1", purpose="plan", max_output_bytes=64))
    for key, value in flags.items():
        setattr(host.infos[job], key, value)
    return job


def test_snapshot_walks_queued_running_settling_completed():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "public-github", "octo/widgets")
    job = _job(host, jobs)
    assert jobs.result(job)["state"] == "queued"
    host.infos[job].queued = False
    assert jobs.result(job)["state"] == "running"
    host.infos[job].done = True
    assert jobs.result(job)["state"] == "settling"  # cleanup not yet confirmed
    host.infos[job].app_cleanup_confirmed = True
    host.infos[job].app_result_text = "the plan"
    host.infos[job].resolved_model = "some-model"
    receipt = jobs.result(job)
    assert receipt["state"] == "completed"
    assert receipt["text"] == "the plan"
    assert receipt["cleanup_confirmed"] is True
    assert receipt["observed_model"] == "some-model"
    assert receipt["native_tool_isolation"] is None  # not an isolated run
    assert len(receipt["request_sha256"]) == 64


def test_snapshot_failure_shapes_never_carry_host_error_text():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "public-github", "octo/widgets")
    job = _job(host, jobs, done=True, app_cleanup_confirmed=False)
    assert jobs.result(job)["reason"] == "host-cleanup-unconfirmed"
    host.infos[job].app_cleanup_confirmed = True
    host.infos[job].user_stopped = True
    assert jobs.result(job)["state"] == "cancelled"
    host.infos[job].user_stopped = False
    host.infos[job].error = "Traceback: /srv/example-home/.secrets leaked path"
    failed = jobs.result(job)
    assert failed["state"] == "failed" and failed["reason"] == "host-job-failed"
    assert "secrets" not in repr(failed)
    host.infos[job].error = ""
    host.infos[job].app_result_text = "x" * 65  # over the 64-byte limit
    assert jobs.result(job)["reason"] == "output-limit-exceeded"
    host.infos[job].app_result_text = None
    host.infos[job].app_result_error = "empty-model-output"
    assert jobs.result(job)["reason"] == "empty-model-output"


def test_isolated_snapshot_requires_the_native_receipt():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "public-github", "octo/widgets")
    job = run(jobs.run_isolated("plan", "text", attempt="a1", purpose="plan"))
    info = host.infos[job]
    info.done, info.app_cleanup_confirmed, info.app_result_text = True, True, "ok"
    info.app_native_text_profile = None
    assert jobs.result(job)["reason"] == "native-isolation-unconfirmed"
    info.app_native_text_profile = SimpleNamespace(
        receipt=lambda: {"policy": "kiro-v2-private-text-v1", "prompt_requests": 1})
    receipt = jobs.result(job)
    assert receipt["state"] == "completed"
    assert receipt["native_tool_isolation"] is True
    assert receipt["prompt_requests"] == 1


def test_lost_host_record_reads_unavailable_never_a_fabricated_result():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "public-github", "octo/widgets")
    job = _job(host, jobs)
    del host.infos[job]  # the manager lost it (restart, eviction)
    receipt = jobs.result(job)
    assert receipt["state"] == "unavailable"
    assert receipt["reason"] == "host-record-unavailable"
    assert run(jobs.cancel(job))["requested"] is False


def test_wait_returns_on_terminal_state_or_times_out():
    host = FakeHost()
    jobs = _backend(host).bind("review-app", "alice", "public-github", "octo/widgets")
    job = _job(host, jobs)
    timed = run(jobs.wait(job, timeout_s=0))
    assert timed["state"] == "queued" and timed["wait_timed_out"] is True
    host.infos[job].done = True
    host.infos[job].app_cleanup_confirmed = True
    host.infos[job].app_result_text = "done"
    assert run(jobs.wait(job, timeout_s=1))["state"] == "completed"
    with pytest.raises(sdk.ScopedSpawnError, match="timeout"):
        run(jobs.wait(job, timeout_s=901))


# ── capture_app_result ──────────────────────────────────────────────────────


def test_capture_keeps_clean_text_and_refuses_redacted_output():
    info = SimpleNamespace(app="review-app", app_result_limit=1024)
    sdk.capture_app_result(info, "a clean answer")
    assert info.app_result_text == "a clean answer" and info.app_result_error == ""

    leaky = SimpleNamespace(app="review-app", app_result_limit=1024)
    sdk.capture_app_result(leaky, "token: ghp_" + "A" * 36 + " see https://evil.example/x?k=v")
    assert leaky.app_result_text is None
    assert leaky.app_result_error == "model-output-redacted"


def test_capture_bounds_and_legacy_jobs():
    legacy = SimpleNamespace(app="review-app", app_result_limit=0)
    sdk.capture_app_result(legacy, "ignored")
    assert not hasattr(legacy, "app_result_text")

    small = SimpleNamespace(app="review-app", app_result_limit=4)
    sdk.capture_app_result(small, "toolong")
    assert small.app_result_error == "output-limit-exceeded"

    empty = SimpleNamespace(app="review-app", app_result_limit=4)
    sdk.capture_app_result(empty, "")
    assert empty.app_result_error == "empty-model-output"

    unowned = SimpleNamespace(app="", app_result_limit=4)
    sdk.capture_app_result(unowned, "x")
    assert unowned.app_result_error == "invalid-app-result-capture"
