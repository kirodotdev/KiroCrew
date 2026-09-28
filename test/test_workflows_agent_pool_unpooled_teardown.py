"""Regression: ``_run_unpooled`` teardown is best-effort and never masks the step.

The unpooled path (a ``session=`` named call, or the identity-cap overflow
valve) tears its session down in a ``finally``: ``release(cleanup=False)`` for a
named conversation, ``destroy()`` for a one-shot key. A teardown failure raised
from that ``finally`` would REPLACE the step's real outcome — a successful
result would vanish behind a session error, and the body's own exception (a
provider failure, a private-memory ``validate()`` rejection, a cancellation)
would be swallowed. These tests pin the contract that teardown failures are
logged (type name only — the private diagnostics contract forbids leaking the
exception text) and the body's outcome always wins.

Fake SessionManager throughout — no kiro-cli spawns.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from kiro_crew.workflow_memory import WorkflowMemoryError
from kiro_crew.workflows.agent_pool import build_pooled_agent_fn

_LOGGER = "kiro_crew.workflows.agent_pool"
_TEARDOWN_SECRET = "teardown-detail-token-9f3a"


class _TeardownError(RuntimeError):
    """Distinct type so a test can prove which exception surfaced."""


class _FakeProvider:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    def is_process_alive(self) -> bool:
        return True


class _FailingSessions:
    """Fake SessionManager whose teardown calls can be made to fail."""

    def __init__(self, *, destroy_fails: bool = False, release_fails: bool = False) -> None:
        self.destroy_fails = destroy_fails
        self.release_fails = release_fails
        self.live: dict[str, _FakeProvider] = {}
        self.destroy_calls: list[str] = []
        self.release_calls: list[tuple[str, bool]] = []

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None, extra_env=None):
        if key in self.live:
            return self.live[key], False, False
        prov = _FakeProvider(tag=key)
        self.live[key] = prov
        return prov, True, False

    def release(self, key, *, cleanup=False):
        self.release_calls.append((key, cleanup))
        if self.release_fails:
            raise _TeardownError(_TEARDOWN_SECRET)
        if cleanup:
            self.live.pop(key, None)

    async def reset(self, key):
        self.live.pop(key, None)

    async def destroy(self, key):
        self.destroy_calls.append(key)
        if self.destroy_fails:
            raise _TeardownError(_TEARDOWN_SECRET)
        self.live.pop(key, None)


class _FakeScope:
    """Minimal WorkflowScope stand-in: ``validate`` rejects on the Nth call."""

    def __init__(self, *, reject_on_call: int | None = None) -> None:
        self.reject_on_call = reject_on_call
        self.validate_calls = 0

    async def validate(self) -> None:
        self.validate_calls += 1
        if self.validate_calls == self.reject_on_call:
            raise WorkflowMemoryError("Workflow execution identity changed")

    def worker_key(self, label: str) -> str:
        return f"wf-worker:run:{label}"

    async def prepare(self, context, key: str) -> str:
        return ""

    async def prompt(self, context, key, prompt, **kwargs) -> str:
        return prompt


async def _ok_stream(provider, prompt, **kwargs):
    await asyncio.sleep(0)
    return f"[{provider.tag}] {prompt}"


def _unpooled_only(fail):
    """Stream fake that misbehaves ONLY on the unpooled overflow session, so the
    warm ``m1`` call that fills the identity slot still completes normally."""

    async def _stream(provider, prompt, **kwargs):
        if provider.tag.startswith("wf-unpooled:"):
            return await fail(provider, prompt)
        return await _ok_stream(provider, prompt, **kwargs)

    return _stream


async def _boom(provider, prompt):
    raise ValueError("provider failed")


@pytest.fixture(autouse=True)
def _patch_redaction(monkeypatch):
    monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", _ok_stream)
    monkeypatch.setattr("kiro_crew.workflows.agent_pool.redact", lambda t: t)


def _build(sessions, **kwargs):
    # max_identities=1: the first non-default identity gets a sub-pool, the
    # second overflows onto the unpooled destroy path under test.
    return build_pooled_agent_fn(sessions, run_id="run", max_workers=1, max_identities=1, **kwargs)


async def _run_overflow(agent_fn, prompt: str = "overflow"):
    await agent_fn("warm", {"model": "m1"})  # occupies the single identity slot
    return await agent_fn(prompt, {"model": "m2"})  # unpooled, torn down via destroy()


def _teardown_records(caplog):
    return [r for r in caplog.records if r.name == _LOGGER and "teardown" in r.getMessage()]


@pytest.mark.asyncio
async def test_success_survives_destroy_failure(caplog):
    sessions = _FailingSessions(destroy_fails=True)
    agent_fn, pool = _build(sessions)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        result = await _run_overflow(agent_fn)
    assert result == "[wf-unpooled:run:0] overflow"
    assert sessions.destroy_calls == ["wf-unpooled:run:0"]
    records = _teardown_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    # Type-name-only summary: no exception text, no traceback attached.
    assert "_TeardownError" in records[0].getMessage()
    assert _TEARDOWN_SECRET not in caplog.text
    assert records[0].exc_info is None
    await pool.shutdown()


@pytest.mark.asyncio
async def test_body_error_wins_over_destroy_failure(monkeypatch, caplog):
    monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", _unpooled_only(_boom))
    sessions = _FailingSessions(destroy_fails=True)
    agent_fn, pool = _build(sessions)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        with pytest.raises(ValueError, match="provider failed"):
            await _run_overflow(agent_fn)
    assert sessions.destroy_calls == ["wf-unpooled:run:0"]
    assert _TEARDOWN_SECRET not in caplog.text
    await pool.shutdown()


@pytest.mark.asyncio
async def test_named_release_failure_does_not_mask_result(caplog):
    sessions = _FailingSessions(release_fails=True)
    agent_fn, pool = _build(sessions)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        result = await agent_fn("step", {"session": "chain-A"})
    assert result == "[chain-A] step"
    # Lease returned (attempted), conversation retained — never cleanup=True.
    assert sessions.release_calls == [("chain-A", False)]
    assert "chain-A" in sessions.live
    assert sessions.destroy_calls == []
    records = _teardown_records(caplog)
    assert len(records) == 1 and "_TeardownError" in records[0].getMessage()
    assert _TEARDOWN_SECRET not in caplog.text
    await pool.shutdown()


@pytest.mark.asyncio
async def test_named_release_failure_does_not_mask_body_error(monkeypatch):
    async def _named_boom(provider, prompt, **kwargs):
        return await _boom(provider, prompt)

    monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", _named_boom)
    sessions = _FailingSessions(release_fails=True)
    agent_fn, pool = _build(sessions)
    with pytest.raises(ValueError, match="provider failed"):
        await agent_fn("step", {"session": "chain-A"})
    assert sessions.release_calls == [("chain-A", False)]
    await pool.shutdown()


@pytest.mark.asyncio
async def test_cancellation_propagates_and_still_tears_down(monkeypatch):
    started = asyncio.Event()

    async def _hang(provider, prompt):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", _unpooled_only(_hang))
    sessions = _FailingSessions(destroy_fails=True)
    agent_fn, pool = _build(sessions)
    await agent_fn("warm", {"model": "m1"})
    task = asyncio.ensure_future(agent_fn("overflow", {"model": "m2"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Teardown was attempted; its failure neither masked nor swallowed the cancel.
    assert sessions.destroy_calls == ["wf-unpooled:run:0"]
    await pool.shutdown()


@pytest.mark.asyncio
async def test_private_validate_rejection_propagates_over_destroy_failure(caplog):
    # validate() call order on the overflow call: agent_fn entry (1), then the
    # post-step validate inside _run_unpooled (2). The warm call before it also
    # validates twice (entry + worker.send_message), so the overflow's
    # post-step validate is the 4th call overall.
    scope = _FakeScope(reject_on_call=4)
    sessions = _FailingSessions(destroy_fails=True)
    agent_fn, pool = _build(sessions, memory_scope=scope, context_builder=object())
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        with pytest.raises(WorkflowMemoryError, match="identity changed"):
            await _run_overflow(agent_fn)
    assert scope.validate_calls == 4
    assert sessions.destroy_calls == ["wf-unpooled:run:0"]
    assert _TEARDOWN_SECRET not in caplog.text
    await pool.shutdown()


@pytest.mark.asyncio
async def test_private_validate_rejection_before_step_still_rejects():
    # Entry validate (call 1 on the very first agent_fn call) rejects: no session
    # is created and the rejection is unchanged by this fix.
    scope = _FakeScope(reject_on_call=1)
    sessions = _FailingSessions(destroy_fails=True)
    agent_fn, pool = _build(sessions, memory_scope=scope, context_builder=object())
    with pytest.raises(WorkflowMemoryError):
        await agent_fn("step", {"session": "chain-A"})
    assert sessions.live == {}
    assert sessions.destroy_calls == [] and sessions.release_calls == []
    await pool.shutdown()
