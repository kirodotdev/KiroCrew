"""Advisor reviewer runtime: bounded, shared, self-healing.

Contract under test (see docs/system-specs/modules/advisor.md):

- One shared reviewer runtime serves every enabled parent session (v1 has a
  single global reviewer model), with one isolated reviewer session per
  parent.
- The runtime process is registered with orphan-sweep protection while
  alive and unregistered when killed.
- When the last reviewer session is released the runtime is reaped after an
  idle grace; a later acquire re-spawns it.
- Review prompts run through a bounded concurrency semaphore and never
  propagate reviewer failures to the caller: a failed review degrades the
  session's advisor status visibly instead.
- The built-in reviewer agent's tool allowlist is strictly read-only.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from kiro_crew.advisor.composition import _REVIEWER_READ_ONLY_TOOLS
from kiro_crew.advisor.runtime import (
    AdvisorReviewerRuntime,
)

_pid_counter = itertools.count(4000)


class FakeAcpRuntime:
    """Mirrors the real runtime surface: pid property, is_alive() method."""

    def __init__(self) -> None:
        self.pid = next(_pid_counter)
        self._alive = False
        self.kill_calls = 0

    def is_alive(self) -> bool:
        return self._alive

    async def spawn(self) -> None:
        self._alive = True

    async def kill(self, *, expected: bool = False) -> None:
        self._alive = False
        self.kill_calls += 1


class Harness:
    """Injected collaborators for the runtime under test."""

    def __init__(self) -> None:
        self.created: list[FakeAcpRuntime] = []

    def factory(self) -> FakeAcpRuntime:
        rt = FakeAcpRuntime()
        self.created.append(rt)
        return rt


def make_runtime(
    harness: Harness,
    *,
    max_concurrent: int = 2,
    idle_grace_secs: float = 0.02,
    prompt_fn=None,
) -> AdvisorReviewerRuntime:
    async def default_prompt(session, payload):
        return {"notes": []}

    return AdvisorReviewerRuntime(
        runtime_factory=harness.factory,
        max_concurrent=max_concurrent,
        idle_grace_secs=idle_grace_secs,
        prompt_fn=prompt_fn or default_prompt,
    )


class TestSharedRuntimeLifecycle:
    @pytest.mark.asyncio
    async def test_first_acquire_spawns_shared_runtime_once(self):
        h = Harness()
        pool = make_runtime(h)
        await pool.acquire_session("dashboard:a")
        await pool.acquire_session("dashboard:b")
        assert len(h.created) == 1
        assert h.created[0].is_alive() is True
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_same_parent_reuses_its_reviewer_session(self):
        h = Harness()
        pool = make_runtime(h)
        first = await pool.acquire_session("dashboard:a")
        second = await pool.acquire_session("dashboard:a")
        assert first is second
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_distinct_parents_get_isolated_sessions(self):
        h = Harness()
        pool = make_runtime(h)
        a = await pool.acquire_session("dashboard:a")
        b = await pool.acquire_session("dashboard:b")
        assert a is not b
        assert a.session_id != b.session_id
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_kills_runtime_and_clears_sessions(self):
        h = Harness()
        pool = make_runtime(h)
        await pool.acquire_session("dashboard:a")
        await pool.shutdown()
        assert h.created[0].kill_calls == 1
        assert pool._sessions == {}


class TestIdleReaping:
    @pytest.mark.asyncio
    async def test_last_release_reaps_after_idle_grace(self):
        h = Harness()
        pool = make_runtime(h, idle_grace_secs=0.01)
        await pool.acquire_session("dashboard:a")
        await pool.release_session("dashboard:a")
        await asyncio.sleep(0.08)
        assert h.created[0].kill_calls == 1

    @pytest.mark.asyncio
    async def test_release_with_other_sessions_live_does_not_reap(self):
        h = Harness()
        pool = make_runtime(h, idle_grace_secs=0.01)
        await pool.acquire_session("dashboard:a")
        await pool.acquire_session("dashboard:b")
        await pool.release_session("dashboard:a")
        await asyncio.sleep(0.08)
        assert h.created[0].kill_calls == 0
        assert h.created[0].is_alive() is True
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_reacquire_within_grace_cancels_reap(self):
        h = Harness()
        pool = make_runtime(h, idle_grace_secs=0.05)
        await pool.acquire_session("dashboard:a")
        await pool.release_session("dashboard:a")
        await pool.acquire_session("dashboard:a")
        await asyncio.sleep(0.12)
        assert h.created[0].kill_calls == 0
        assert h.created[0].is_alive() is True
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_acquire_after_reap_respawns(self):
        h = Harness()
        pool = make_runtime(h, idle_grace_secs=0.01)
        await pool.acquire_session("dashboard:a")
        await pool.release_session("dashboard:a")
        await asyncio.sleep(0.08)
        await pool.acquire_session("dashboard:a")
        assert len(h.created) == 2
        assert h.created[1].is_alive() is True
        await pool.shutdown()


class TestCrashSelfHeal:
    @pytest.mark.asyncio
    async def test_dead_runtime_is_replaced_on_next_acquire(self):
        h = Harness()
        pool = make_runtime(h)
        await pool.acquire_session("dashboard:a")
        h.created[0]._alive = False  # simulate crash
        await pool.acquire_session("dashboard:b")
        assert len(h.created) == 2
        assert h.created[1].is_alive() is True
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_crash_replaces_dead_runtime_on_next_acquire(self):
        h = Harness()
        pool = make_runtime(h)
        await pool.acquire_session("dashboard:a")
        h.created[0]._alive = False
        await pool.acquire_session("dashboard:b")
        assert len(h.created) == 2 and h.created[1].is_alive()
        await pool.shutdown()


class TestBoundedReview:
    @pytest.mark.asyncio
    async def test_reviews_respect_concurrency_bound(self):
        h = Harness()
        in_flight = 0
        peak = 0

        async def slow_prompt(session, payload):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            return {"notes": []}

        pool = make_runtime(h, max_concurrent=2, prompt_fn=slow_prompt)
        for key in ("a", "b", "c", "d"):
            await pool.acquire_session(f"dashboard:{key}")
        await asyncio.gather(
            *(pool.review(f"dashboard:{k}", {"seq": 1}) for k in ("a", "b", "c", "d"))
        )
        assert peak <= 2
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_review_failure_degrades_session_not_caller(self, caplog):
        h = Harness()

        async def failing_prompt(session, payload):
            raise RuntimeError("reviewer exploded")

        pool = make_runtime(h, prompt_fn=failing_prompt)
        await pool.acquire_session("dashboard:a")
        result = await pool.review("dashboard:a", {"seq": 1})
        assert result is None
        assert any(
            "degrading" in r.getMessage() for r in caplog.records
        ), "degradation must be logged"
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_successful_review_returns_the_prompt_result(self):
        h = Harness()
        pool = make_runtime(h)
        await pool.acquire_session("dashboard:a")
        assert await pool.review("dashboard:a", {"seq": 1}) == {"notes": []}
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_review_for_unknown_parent_degrades_to_none(self):
        # A rebind can move the slot's key between acquire and review; the
        # documented contract is degrade-to-None, never an exception out of
        # a fire-and-forget pump task.
        h = Harness()
        pool = make_runtime(h)
        assert await pool.review("dashboard:ghost", {"seq": 1}) is None


class TestReviewerAgentAllowlist:
    def test_allowlist_is_read_only(self):
        forbidden = {
            "execute_bash",
            "shell",
            "fs_write",
            "spawn_run",
            "spawn_sub_agents",
            "send_message",
            "session_create",
            "browser",
            "learn_add",
        }
        assert forbidden.isdisjoint(set(_REVIEWER_READ_ONLY_TOOLS))

    def test_allowlist_includes_read_evidence_tools(self):
        for tool in ("fs_read", "grep"):
            assert tool in _REVIEWER_READ_ONLY_TOOLS

    def test_packaged_agent_spec_matches_the_allowlist(self):
        import json
        from pathlib import Path

        import kiro_crew.advisor as advisor_pkg

        spec_path = Path(advisor_pkg.__file__).parent / "agents" / "kirocrew-advisor.json"
        spec = json.loads(spec_path.read_text())
        assert spec["name"] == "kirocrew-advisor"
        assert set(spec["tools"]) == _REVIEWER_READ_ONLY_TOOLS
        assert spec["includeMcpJson"] is False


class TestReviewRuntimeHandoff:
    """review() hands the live runtime to prompt_fn via payload['_runtime'].

    Found by the live-gateway e2e: the composition prompt_fn opens its
    reviewer session on the shared runtime, which only the pool holds.
    """

    @pytest.mark.asyncio
    async def test_review_injects_the_live_runtime(self):
        h = Harness()
        seen = {}

        async def capture(session, payload):
            seen["runtime"] = payload.get("_runtime")
            seen["own_copy"] = payload
            return {"notes": []}

        pool = make_runtime(h, prompt_fn=capture)
        await pool.acquire_session("dashboard:a")
        original = {"prompt": "x", "seq": 1}
        await pool.review("dashboard:a", original)
        assert seen["runtime"] is h.created[0]
        assert "_runtime" not in original, "caller's payload must not be mutated"
        await pool.shutdown()

    @pytest.mark.asyncio
    async def test_live_predicate_reaches_the_prompt_function(self):
        """The prompt function opens the reviewer session itself (an await), so
        it must be able to re-check authorization after that: the pool hands
        the caller's ``_authorized`` predicate through instead of stripping it."""
        h = Harness()
        seen = {}

        async def capture(session, payload):
            seen["authorized"] = payload.get("_authorized")
            return {"notes": []}

        pool = make_runtime(h, prompt_fn=capture)
        await pool.acquire_session("dashboard:a")
        pred = lambda: True  # noqa: E731
        await pool.review("dashboard:a", {"prompt": "x", "_authorized": pred})
        assert seen["authorized"] is pred
        await pool.shutdown()


class TestPreSpawnHook:
    """Round-29: agent-spec materialization is file I/O and must run OFF the
    event loop, before the runtime spawns -- the pool takes an async
    pre_spawn hook, and its failure aborts acquisition (fail closed)."""

    @pytest.mark.asyncio
    async def test_pre_spawn_runs_before_factory(self):
        h = Harness()
        order = []

        async def pre_spawn():
            order.append("pre_spawn")

        def factory():
            order.append("factory")
            return h.factory()

        async def prompt(session, payload):
            return {"notes": []}

        pool = AdvisorReviewerRuntime(
            runtime_factory=factory,
            prompt_fn=prompt,
            pre_spawn=pre_spawn,
        )
        await pool.acquire_session("dashboard:p")
        await pool.review("dashboard:p", {"prompt": "x"})
        assert order[:2] == ["pre_spawn", "factory"]

    @pytest.mark.asyncio
    async def test_pre_spawn_failure_aborts_acquisition(self):
        h = Harness()

        async def pre_spawn():
            raise RuntimeError("spec hardening could not persist")

        async def prompt(session, payload):
            return {"notes": []}

        pool = AdvisorReviewerRuntime(
            runtime_factory=h.factory,
            prompt_fn=prompt,
            pre_spawn=pre_spawn,
        )
        # Acquisition ABORTS (the pump catches this, retains the update, and
        # the session degrades) -- and no runtime is ever spawned, so a spec
        # that cannot be hardened never launches.
        with pytest.raises(RuntimeError):
            await pool.acquire_session("dashboard:q")
        assert h.created == []  # never spawned


class TestReviewUnderUnacquiredKey:
    """Round-30 (Opus): review() must honor its own contract for a key that
    was never acquired -- degrade to None, never raise KeyError out of a
    fire-and-forget pump task."""

    @pytest.mark.asyncio
    async def test_unacquired_key_returns_none(self):
        h = Harness()
        pool = make_runtime(h)
        result = await pool.review("dashboard:never-acquired", {"prompt": "x"})
        assert result is None


class TestAuthorizationRevalidatedBeforeTransmission:
    """Round-35: an opt-out/reset landing while a queued review waits on the
    pool semaphore must cancel it -- the payload's `_authorized` predicate
    is re-checked after the semaphore, immediately before the prompt is
    transmitted."""

    @pytest.mark.asyncio
    async def test_revoked_authorization_cancels_transmission(self):
        import asyncio

        h = Harness()
        calls = []
        first_running = asyncio.Event()
        release_first = asyncio.Event()

        async def prompt(session, payload):
            calls.append(payload["seq"])
            if payload["seq"] == 1:
                first_running.set()
                await release_first.wait()
            return {"notes": []}

        pool = make_runtime(h, max_concurrent=1, prompt_fn=prompt)
        await pool.acquire_session("dashboard:a")
        await pool.acquire_session("dashboard:b")
        authorized = {"b": True}
        t1 = asyncio.create_task(pool.review("dashboard:a", {"seq": 1}))
        await first_running.wait()
        t2 = asyncio.create_task(
            pool.review("dashboard:b", {"seq": 2, "_authorized": lambda: authorized["b"]})
        )
        await asyncio.sleep(0)  # t2 is now queued on the semaphore
        authorized["b"] = False  # opt-out while queued
        release_first.set()
        r1, r2 = await asyncio.gather(t1, t2)
        assert r2 is None
        assert calls == [1], "the revoked review must never reach prompt_fn"
