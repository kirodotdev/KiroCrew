"""Pins for sharing one ``AcpRuntime`` process across top-level chat sessions."""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from kiro_crew.acp.chat_runtime_pool import (
    ChatRuntimeKey,
    ChatRuntimePool,
    eligible_for_chat_sharing,
)
from kiro_crew.acp_backends import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_SESSION_SHARING,
)


class FakeRuntime:
    """Stands in for ``AcpRuntime``: the pool only probes ``is_alive``/``pid``."""

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def die(self) -> None:
        self._alive = False


def spawner(*runtimes: FakeRuntime):
    """An ``acquire`` spawn callback handing back *runtimes* in order."""
    pending = list(runtimes)
    calls: list[FakeRuntime] = []

    async def spawn() -> FakeRuntime:
        rt = pending.pop(0)
        calls.append(rt)
        return rt

    spawn.calls = calls  # type: ignore[attr-defined]
    return spawn


def a_key(**overrides) -> ChatRuntimeKey:
    base = dict(
        work_dir="/home/u/.kirocrew/workspace",
        agent="kirocrew",
        sandbox_mode="auto",
        extra_env={"A": "1", "B": "2"},
        acp_backend="kiro",
        tool_search=None,
        member_context=False,
        memory_mode="persistent",
        shared_scratch=None,
        mcp_gateway_overlay=None,
        mcp_gateway_socket=None,
    )
    base.update(overrides)
    return ChatRuntimeKey.build(**base)


class TestChatSharingEligibility:
    """D10: only a dashboard chat slot in persistent memory mode may share."""

    def test_dashboard_chat_slot_is_eligible(self):
        assert eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )

    def test_bare_chat_slot_key_is_eligible(self):
        assert eligible_for_chat_sharing(
            session_key="chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )

    @pytest.mark.parametrize("mode", ["incognito", "temporary"])
    def test_non_persistent_never_shares(self, mode):
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode=mode,
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )

    def test_member_session_never_shares(self):
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=True,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )

    def test_flag_off_disables_sharing(self):
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=False,
            backend=ACP_BACKEND_KIRO,
        )

    def test_a_backend_without_multiplexed_sessions_is_not_eligible(self):
        """A chat slot is necessary but not sufficient: the host must multiplex.

        Such a host is still served by AcpRuntime, but its destroy() is an
        irreversible server-side session delete, and the pooled teardown must
        destroy the handle to leave a process it may not kill -- so pooling there
        would discard the session's own resume record on every ordinary close.
        """
        assert ACP_BACKEND_KAS not in ACP_BACKENDS_SESSION_SHARING
        assert not eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KAS,
        )
        # Everything else identical, on a host that DOES multiplex.
        assert eligible_for_chat_sharing(
            session_key="dashboard:chat-12-1790000000",
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )

    @pytest.mark.parametrize(
        "key",
        [
            "cron:nightly-digest",
            "hook:pre-commit",
            "task:runner-7",
            "subagent:abc123",
            "telegram:kirocrew:direct:8743158320",
            None,
            "",
        ],
    )
    def test_non_chat_origins_keep_their_own_runtime(self, key):
        assert not eligible_for_chat_sharing(
            session_key=key,
            memory_mode="persistent",
            member_context=False,
            sharing_enabled=True,
            backend=ACP_BACKEND_KIRO,
        )


class TestChatRuntimeKey:
    """D4: the key is every process-level spawn input, and nothing per-session."""

    def test_identical_inputs_compare_equal_and_hash_equal(self):
        assert a_key() == a_key()
        assert len({a_key(), a_key()}) == 1

    def test_env_order_does_not_fragment_the_pool(self):
        assert a_key(extra_env={"A": "1", "B": "2"}) == a_key(extra_env={"B": "2", "A": "1"})

    @pytest.mark.parametrize(
        "field,value",
        [
            ("work_dir", "/home/u/oss/other-worktree"),
            ("agent", "kirocrew-lite"),
            ("sandbox_mode", "strict"),
            ("extra_env", {"A": "9"}),
            ("acp_backend", "kas"),
            ("member_context", True),
            ("memory_mode", "incognito"),
            ("shared_scratch", Path("/scratch/tree-a")),
            ("mcp_gateway_overlay", "/overlay/a.json"),
            ("mcp_gateway_socket", "/run/gw.sock"),
            ("model", "some-model-id"),
        ],
    )
    def test_every_process_level_field_splits_the_key(self, field, value):
        assert a_key() != a_key(**{field: value})

    def test_path_and_string_spellings_of_work_dir_agree(self):
        assert a_key(work_dir=Path("/home/u/.kirocrew/workspace")) == a_key()

    def test_a_trailing_separator_is_the_same_directory(self):
        assert a_key(work_dir="/home/u/.kirocrew/workspace/") == a_key()

    @pytest.mark.parametrize(
        "spelling",
        ["/home/u/.kirocrew/workspace", "/home/u/.kirocrew/workspace/"],
    )
    def test_freeze_path_normalizes_every_spelling_of_one_directory(self, spelling):
        from kiro_crew.acp.chat_runtime_pool import _freeze_path

        assert _freeze_path(spelling) == _freeze_path(Path(spelling))
        assert _freeze_path(spelling) == _freeze_path("/home/u/.kirocrew/workspace")

    @pytest.mark.parametrize("unset", [None, ""])
    def test_freeze_path_reports_an_unset_field_as_empty(self, unset):
        from kiro_crew.acp.chat_runtime_pool import _freeze_path

        assert _freeze_path(unset) == ""

    def test_different_directories_still_split_the_key(self):
        from kiro_crew.acp.chat_runtime_pool import _freeze_path

        assert _freeze_path("/home/u/a") != _freeze_path("/home/u/b")

    def test_reasoning_effort_splits_the_key(self):
        """Two slots asking for different effort cannot share one process.

        The level is written into the work directory's cli.json overlay, one file
        per work directory keyed by model, read once at startup -- so sharing
        would make the last writer decide for everyone on that process.
        """
        assert a_key(reasoning_effort="high") != a_key(reasoning_effort="low")
        assert a_key(reasoning_effort="high") != a_key()
        assert a_key(reasoning_effort="high") == a_key(reasoning_effort="high")
        assert a_key(reasoning_effort=None) == a_key()

    def test_tool_search_settings_are_part_of_the_key(self):
        class TS:
            def __init__(self, enabled, min_pct, min_tokens):
                self.enabled = enabled
                self.min_pct = min_pct
                self.min_tokens = min_tokens

        on = a_key(tool_search=TS(True, 5, 50000))
        off = a_key(tool_search=TS(False, 5, 50000))
        assert on != off
        assert on != a_key(tool_search=None)


class TestRefcountedRegistry:
    """D6: a runtime lives while any session holds it, and D2 caps the sharing."""

    def test_second_compatible_session_joins_one_process(self):
        pool = ChatRuntimePool()
        rt = FakeRuntime()
        spawn = spawner(rt)

        first = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        second = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))

        assert first.joined is False
        assert second.joined is True
        assert first.runtime is second.runtime
        assert len(spawn.calls) == 1
        assert first.leases_on_runtime == 1
        assert second.leases_on_runtime == 2

    def test_incompatible_session_gets_its_own_process(self):
        pool = ChatRuntimePool()
        spawn = spawner(FakeRuntime(1), FakeRuntime(2))

        asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        other = asyncio.run(pool.acquire(a_key(work_dir="/home/u/oss/wt"), "chat-2", spawn, cap=10))

        assert other.joined is False
        assert len(spawn.calls) == 2

    def test_cap_overflow_spawns_another_runtime(self):
        pool = ChatRuntimePool()
        first_rt, second_rt = FakeRuntime(1), FakeRuntime(2)
        spawn = spawner(first_rt, second_rt)

        one = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=2))
        two = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=2))
        third = asyncio.run(pool.acquire(a_key(), "chat-3", spawn, cap=2))

        assert one.runtime is first_rt and two.runtime is first_rt
        assert two.leases_on_runtime == 2, "the cap must be reached before it overflows"
        assert third.runtime is second_rt
        assert third.joined is False
        assert third.leases_on_runtime == 1

    def test_a_second_acquisition_for_one_session_key_takes_its_own_lease(self):
        """Two starts for one key are ordinary; each must be releasable alone."""
        pool = ChatRuntimePool()
        rt = FakeRuntime()
        spawn = spawner(rt)

        first = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        again = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))

        assert again.runtime is rt
        assert again.lease != first.lease
        assert again.leases_on_runtime == 2
        assert len(spawn.calls) == 1

    def test_only_the_last_release_hands_back_the_runtime_to_kill(self):
        pool = ChatRuntimePool()
        rt = FakeRuntime()
        spawn = spawner(rt)
        one = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        two = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))

        assert asyncio.run(pool.release(one.lease)) is None
        assert asyncio.run(pool.release(two.lease)) is rt
        # Released by both, so the next session founds a fresh process rather
        # than joining the one that was handed back to be killed.
        replacement = FakeRuntime(2)
        spawn2 = spawner(replacement)
        again = asyncio.run(pool.acquire(a_key(), "chat-3", spawn2, cap=10))
        assert again.runtime is replacement
        assert again.joined is False

    def test_releasing_an_unheld_session_is_a_no_op(self):
        pool = ChatRuntimePool()
        assert asyncio.run(pool.release("no-such-lease")) is None

    def test_a_second_process_for_the_same_key_takes_the_overflow_only(self):
        """Two processes for one key exist only past the cap, and fill in order."""
        pool = ChatRuntimePool()
        first, second = FakeRuntime(1), FakeRuntime(2)
        spawn = spawner(first, second)

        a = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=2))
        b = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=2))
        c = asyncio.run(pool.acquire(a_key(), "chat-3", spawn, cap=2))
        d = asyncio.run(pool.acquire(a_key(), "chat-4", spawn, cap=2))

        assert [a.runtime, b.runtime] == [first, first]
        assert [c.runtime, d.runtime] == [second, second]
        assert d.leases_on_runtime == 2
        assert len(spawn.calls) == 2, "no third process for a key still under its cap"


class TestProjectionSkipOnJoin:
    """The one branch that differs for a joining session, driven directly.

    ``_activate_mode_bracketed`` is where the process-wide skill projection is
    rebuilt, and a joining session must not rebuild it. Reached here rather than
    through a whole ``create_session``, so the assertion is about this branch and
    nothing else, and both directions are checked -- a test that only proved the
    skip would pass on an implementation that never projected at all.
    """

    @staticmethod
    def _runtime(monkeypatch, calls):
        import kiro_crew.acp.skill_projection as proj_mod
        from kiro_crew.acp.runtime import AcpRuntime

        def fake_prepare(work_dir, *, enabled=None):
            calls.append((str(work_dir), enabled))
            return "projection-object"

        monkeypatch.setattr(proj_mod, "prepare_native_skill_projection", fake_prepare)

        import kiro_crew.agent as agent_mod

        monkeypatch.setattr(agent_mod, "require_fresh_derived_spec", lambda *a, **k: "snap")
        monkeypatch.setattr(agent_mod, "require_unchanged_derived_spec", lambda *a, **k: None)

        rt = AcpRuntime.__new__(AcpRuntime)
        rt._work_dir = Path("/home/u/.kirocrew/workspace")
        rt._native_skill_projection = "already-live"
        sent: list = []

        async def fake_send(method, params, timeout=None):
            sent.append(method)
            return {}

        rt._send_and_await = fake_send  # type: ignore[method-assign]
        rt.sent = sent  # type: ignore[attr-defined]
        return rt

    def test_a_joining_session_does_not_rebuild_the_projection(self, monkeypatch):
        calls: list = []
        rt = self._runtime(monkeypatch, calls)

        asyncio.run(
            rt._activate_mode_bracketed(
                "sid-1",
                "kirocrew",
                budget=1.0,
                payload_snapshot="snap",
                wire_registered=True,
                skip_projection_refresh=True,
            )
        )

        assert calls == [], "a joining session rewrote the process-wide projection"
        assert rt.sent, "set_mode was not sent, so the branch under test was not reached"

    def test_a_founding_session_does_rebuild_it(self, monkeypatch):
        calls: list = []
        rt = self._runtime(monkeypatch, calls)

        asyncio.run(
            rt._activate_mode_bracketed(
                "sid-1",
                "kirocrew",
                budget=1.0,
                payload_snapshot="snap",
                wire_registered=True,
                skip_projection_refresh=False,
            )
        )

        assert len(calls) == 1, "the founding session must still refresh the projection"
        assert calls[0][1] is True


class TestRuntimeDeathRecovery:
    """D7: no session stays bound to a dead runtime."""

    def test_forget_unbinds_every_session_on_the_lost_runtime(self):
        pool = ChatRuntimePool()
        rt = FakeRuntime()
        spawn = spawner(rt)
        asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))
        asyncio.run(pool.acquire(a_key(), "chat-3", spawn, cap=10))

        rt.die()
        orphaned = asyncio.run(pool.forget(rt))

        assert orphaned == ["chat-1", "chat-2", "chat-3"]
        # Unbound means each one founds or joins afresh rather than being handed
        # the dead process back.
        replacement = FakeRuntime(2)
        spawn2 = spawner(replacement)
        for key in orphaned:
            back = asyncio.run(pool.acquire(a_key(), key, spawn2, cap=10))
            assert back.runtime is replacement
        assert len(spawn2.calls) == 1, "the three orphans must land on ONE replacement"

    def test_orphaned_sessions_land_together_on_one_replacement(self):
        pool = ChatRuntimePool()
        dead, replacement = FakeRuntime(1), FakeRuntime(2)
        spawn = spawner(dead, replacement)
        asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))

        dead.die()
        asyncio.run(pool.forget(dead))

        first_back = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        second_back = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))

        assert first_back.runtime is replacement
        assert first_back.joined is False
        assert second_back.runtime is replacement
        assert second_back.joined is True
        assert len(spawn.calls) == 2

    def test_a_dead_runtime_is_never_handed_to_a_new_session(self):
        pool = ChatRuntimePool()
        dead, fresh = FakeRuntime(1), FakeRuntime(2)
        spawn = spawner(dead, fresh)
        asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))

        dead.die()
        joined = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))

        assert joined.runtime is fresh
        assert joined.joined is False
        # The session that was on the dead process is unbound, so its own next
        # turn lands it on the replacement as a join rather than a fresh spawn.
        back = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        assert back.runtime is fresh
        assert back.joined is True
        assert len(spawn.calls) == 2


class KillableRuntime(FakeRuntime):
    """A runtime that records whether anything killed it."""

    def __init__(self, pid: int = 4242) -> None:
        super().__init__(pid)
        self.kills: list[str] = []

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        self.kills.append(reason)
        self._alive = False


class FakeHandle:
    """The handle methods the pooled shutdown branch reaches."""

    def __init__(self) -> None:
        self.destroyed = 0
        self.cancelled = 0
        self.is_turn_active = False
        self.keep_transcript = False
        self.memory_mode = "persistent"

    async def destroy(self) -> None:
        self.destroyed += 1

    async def cancel(self) -> None:
        self.cancelled += 1


def pooled_provider(pool: ChatRuntimePool, runtime, session_key: str, lease: str):
    """A provider whose teardown releases exactly the lease it was given."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    handle = FakeHandle()
    provider = AcpSessionProvider(
        handle,  # type: ignore[arg-type]
        runtime,
        session_key=session_key,
        runtime_release=lambda: pool.release(lease),
    )
    return provider, handle


class TestPooledShutdownRefcount:
    """D6 at the teardown seam: the pool, not the provider, decides the kill."""

    def test_a_joining_session_leaving_does_not_kill_the_shared_process(self):
        pool = ChatRuntimePool()
        rt = KillableRuntime()
        spawn = spawner(rt)
        one = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        two = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))

        provider, handle = pooled_provider(pool, rt, "chat-2", two.lease)
        asyncio.run(provider.shutdown())

        assert handle.destroyed == 1
        assert rt.kills == []
        assert rt.is_alive()
        # The remaining lease still holds it, so ITS release is the one that
        # hands the runtime back to be killed.
        assert asyncio.run(pool.release(one.lease)) is rt

    def test_a_same_key_race_loser_does_not_kill_the_winners_process(self):
        """Two starts for ONE session key; the loser tears down; the winner lives.

        The allocator produces this shape on purpose -- it carries a race budget
        for it -- so the loser's shutdown must not end the registered winner's
        process. Refcounting by session key gives the two starts a single
        reference, and this is the pin that catches that.
        """
        pool = ChatRuntimePool()
        rt = KillableRuntime()
        spawn = spawner(rt)

        winner = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        loser = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        assert winner.runtime is rt and loser.runtime is rt

        losing_provider, losing_handle = pooled_provider(pool, rt, "chat-1", loser.lease)
        asyncio.run(losing_provider.shutdown())

        # The substance first, so THIS is what a regression trips on rather than
        # the lease-identity check below.
        assert rt.kills == [], "the race loser killed the winner's process"
        assert rt.is_alive()
        assert losing_handle.destroyed == 1, "the loser must still leave the process"
        # And the winner is still HELD, not merely alive: its own release is what
        # finally hands the runtime back.
        assert asyncio.run(pool.release(winner.lease)) is rt
        assert winner.lease != loser.lease

    def test_the_founder_leaving_first_does_not_kill_it_either(self):
        pool = ChatRuntimePool()
        rt = KillableRuntime()
        spawn = spawner(rt)
        one = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))

        provider, _ = pooled_provider(pool, rt, "chat-1", one.lease)
        asyncio.run(provider.shutdown())

        assert rt.kills == []
        assert rt.is_alive()

    def test_the_last_holder_leaving_kills_the_process(self):
        pool = ChatRuntimePool()
        rt = KillableRuntime()
        spawn = spawner(rt)
        held = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))

        provider, handle = pooled_provider(pool, rt, "chat-1", held.lease)
        asyncio.run(provider.shutdown())

        assert handle.destroyed == 1
        assert len(rt.kills) == 1
        assert not rt.is_alive()

    def test_a_failing_release_still_destroys_this_session_handle(self):
        from kiro_crew.acp.session_provider import AcpSessionProvider

        rt = KillableRuntime()
        handle = FakeHandle()

        async def boom():
            raise RuntimeError("registry unavailable")

        provider = AcpSessionProvider(
            handle,  # type: ignore[arg-type]
            rt,
            session_key="chat-1",
            runtime_release=boom,
        )
        asyncio.run(provider.shutdown())

        assert handle.destroyed == 1
        assert rt.kills == []

    def test_a_pooled_teardown_keeps_the_native_transcript(self):
        """destroy() unlinks the transcript unless asked; a pooled session must ask."""
        pool = ChatRuntimePool()
        rt = KillableRuntime()
        spawn = spawner(rt)
        held = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))

        provider, handle = pooled_provider(pool, rt, "chat-1", held.lease)
        asyncio.run(provider.shutdown())

        assert handle.keep_transcript is True
        assert handle.destroyed == 1

    def test_a_pooled_teardown_cancels_an_in_flight_turn_first(self):
        """An abandoned prompt would keep running on a process nothing may kill."""
        pool = ChatRuntimePool()
        rt = KillableRuntime()
        spawn = spawner(rt)
        asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        two = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))

        provider, handle = pooled_provider(pool, rt, "chat-2", two.lease)
        handle.is_turn_active = True
        asyncio.run(provider.shutdown())

        assert handle.cancelled == 1
        assert handle.destroyed == 1
        assert rt.kills == []


class TestResetSurvivorSeesUnregisteredTenants:
    """A joiner holds its lease before it is a registered session.

    ``provider.start`` takes the pool lease and only RETURNS afterwards, so a
    co-tenant that is still starting is invisible to the live session table for
    the whole of a multi-second cold start. The reset guard has to see it
    anyway, or it force-kills a shared process out from under a session that is
    mid-start.
    """

    def test_a_lease_with_no_registered_session_is_still_a_tenant(self, monkeypatch):
        from kiro_crew.agent_sdk import chat_runtime_pid_has_tenants

        pool = ChatRuntimePool()
        runtime = FakeRuntime(pid=48271)
        spawn = spawner(runtime)
        acq = asyncio.run(pool.acquire(a_key(), "dashboard:chat-1-1", spawn, cap=10))
        assert acq.leases_on_runtime == 1

        monkeypatch.setattr("kiro_crew.acp.chat_runtime_pool.CHAT_RUNTIME_POOL", pool, raising=True)
        # No session table is consulted here at all: the lease alone answers,
        # which is the whole point -- a starting joiner is in no session table.
        assert chat_runtime_pid_has_tenants(48271) is True
        assert chat_runtime_pid_has_tenants(48272) is False

        # Once the last lease goes the pid stops being a tenant, so a process
        # that really did outlive its only session is still reapable.
        asyncio.run(pool.release(acq.lease))
        assert chat_runtime_pid_has_tenants(48271) is False

    def test_a_lease_on_a_dropped_runtime_does_not_protect_the_pid(self):
        """The opposite hole: a dead runtime must not be shielded by stale leases."""
        pool = ChatRuntimePool()
        runtime = FakeRuntime(pid=48273)
        acq = asyncio.run(pool.acquire(a_key(), "dashboard:chat-2-1", spawner(runtime), cap=10))
        assert acq.leases_on_runtime == 1

        asyncio.run(pool.forget(runtime))
        assert pool.pid_has_outstanding_leases(48273) is False


class TestRetiredCredentialRuntimeIsNotClaimable:
    """A credential change retires the sessions on a process, not the process.

    The sweep can only MARK a busy session, so its lease keeps the entry alive
    and otherwise claimable -- and the compatibility key cannot tell the two eras
    apart, because a session asks to spawn with the same inputs before and after
    the change. Left claimable, the next start on that key lands on the process
    holding the retired credential.
    """

    def test_a_retired_runtime_is_not_joined_even_with_room(self):
        pool = ChatRuntimePool()
        first, second = FakeRuntime(pid=9001), FakeRuntime(pid=9002)
        spawn = spawner(first, second)
        one = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        assert one.runtime is first and one.joined is False

        assert pool.retire_pid(9001) == 1

        two = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))
        assert two.joined is False, "a retired-credential runtime was joined"
        assert two.runtime is second
        # The sessions already on it keep it: retirement stops new arrivals, it
        # does not evict or kill.
        assert first.is_alive()
        assert asyncio.run(pool.release(one.lease)) is first

    def test_retirement_marks_only_the_named_pid(self):
        pool = ChatRuntimePool()
        rt = FakeRuntime(pid=9003)
        asyncio.run(pool.acquire(a_key(), "chat-1", spawner(rt), cap=10))
        assert pool.retire_pid(9999) == 0
        joined = asyncio.run(pool.acquire(a_key(), "chat-2", spawner(rt), cap=10))
        assert joined.joined is True

    def test_an_unreadable_identity_never_refuses_a_join(self):
        """Absence must not become refusal -- the mirror of the inverse hole.

        Nothing about an entry's credential is known unless something retires it.
        If an unknown were treated as retired, one unreadable identity would make
        the pool never hit and the feature would silently do nothing.
        """
        pool = ChatRuntimePool()
        rt = FakeRuntime(pid=9004)
        spawn = spawner(rt)
        asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        second = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))
        assert second.joined is True
        assert second.leases_on_runtime == 2


class TestPooledShutdownSurvivesCancellation:
    """``CancelledError`` is a ``BaseException``, so ``except Exception`` misses it.

    The restart path runs this shutdown under an ``asyncio.wait_for`` whose
    budget is larger than the inner cancel budget, so a slow cancel is where the
    cancellation lands. Sequentially that skipped the release, and a lease that
    is never returned keeps its entry alive: the runtime is reclaimed by neither
    the dead-entry drop nor ``forget``, and nothing retries.
    """

    def _cancelled_on_cancel(self, pool, rt, session_key, lease):
        provider, handle = pooled_provider(pool, rt, session_key, lease)
        handle.is_turn_active = True

        async def cancel_then_cancelled() -> None:
            handle.cancelled += 1
            raise asyncio.CancelledError

        handle.cancel = cancel_then_cancelled  # type: ignore[assignment]
        return provider, handle

    def test_a_cancellation_through_shutdown_still_returns_the_lease(self):
        pool = ChatRuntimePool()
        rt = KillableRuntime()
        spawn = spawner(rt)
        one = asyncio.run(pool.acquire(a_key(), "chat-1", spawn, cap=10))
        two = asyncio.run(pool.acquire(a_key(), "chat-2", spawn, cap=10))
        provider, handle = self._cancelled_on_cancel(pool, rt, "chat-2", two.lease)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(provider.shutdown())

        # The cancellation propagates -- callers must still see it -- but the
        # lease is gone and the handle is destroyed, so the co-tenant's own
        # release is what finally hands the runtime back.
        assert handle.cancelled == 1
        assert handle.destroyed == 1
        assert rt.kills == []
        assert asyncio.run(pool.release(one.lease)) is rt

    def test_a_cancellation_on_the_last_session_still_kills_the_runtime(self):
        pool = ChatRuntimePool()
        rt = KillableRuntime()
        only = asyncio.run(pool.acquire(a_key(), "chat-1", spawner(rt), cap=10))
        provider, handle = self._cancelled_on_cancel(pool, rt, "chat-1", only.lease)

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(provider.shutdown())

        assert handle.destroyed == 1
        assert rt.kills, "the last holder's runtime was left running by a cancelled shutdown"
        assert pool.pid_has_outstanding_leases(getattr(rt, "pid", -1)) is False


class TestDeadProcessIsNeverATenant:
    """The window between a runtime's death and the next acquisition.

    Dead entries are dropped inside ``acquire``, so a crash leaves the entry
    registered with its leases intact and every co-tenant session still bound to
    the pid. If either source counted that state, the reset would skip both the
    SIGKILL and the child sweep of a process that is already gone, stranding the
    children that escaped it.
    """

    def test_a_crashed_runtime_with_leases_intact_is_not_a_tenant(self):
        pool = ChatRuntimePool()
        runtime = FakeRuntime(pid=48291)
        acq = asyncio.run(pool.acquire(a_key(), "dashboard:chat-3-1", spawner(runtime), cap=10))
        assert acq.leases_on_runtime == 1
        assert pool.pid_has_outstanding_leases(48291) is True

        # The crash alone, with no acquisition behind it to run the sweep: this
        # is the state the guard actually meets, so the pin has to assert the
        # window exists rather than testing the tidied-up state after it.
        runtime.die()
        assert len(pool._entries) == 1
        assert pool._entries[0].leases

        assert pool.pid_has_outstanding_leases(48291) is False

    def test_a_registered_session_on_a_dead_process_is_not_a_live_tenant(self):
        from kiro_crew.session_lifecycle import _pid_has_live_tenant

        def tenant(pid: int, *, alive: bool):
            client = types.SimpleNamespace(_pid=pid)
            provider = types.SimpleNamespace(
                _client=client,
                is_process_alive=lambda: alive,
            )
            return types.SimpleNamespace(provider=provider)

        no_pool = {"pool_tenancy": lambda pid: False}

        # A co-tenant whose process is alive is a survivor, which is the case
        # the guard exists for.
        assert (
            _pid_has_live_tenant(
                7001,
                sessions={"dashboard:chat-9-1": tenant(7001, alive=True)},
                exclude_key="dashboard:chat-8-1",
                exclude_session=None,
                **no_pool,
            )
            is True
        )

        # The same session table entry for a process that has died: still bound
        # to the pid, and not a reason to leave it or its children alone.
        assert (
            _pid_has_live_tenant(
                7001,
                sessions={"dashboard:chat-9-1": tenant(7001, alive=False)},
                exclude_key="dashboard:chat-8-1",
                exclude_session=None,
                **no_pool,
            )
            is False
        )

    def test_a_starting_joiner_with_no_session_row_is_still_a_live_tenant(self):
        """The cold-start window stays closed: liveness is of the PROCESS."""
        from kiro_crew.session_lifecycle import _pid_has_live_tenant

        assert (
            _pid_has_live_tenant(
                7002,
                sessions={},
                exclude_key="dashboard:chat-8-1",
                exclude_session=None,
                pool_tenancy=lambda pid: pid == 7002,
            )
            is True
        )

    def test_with_no_co_tenant_at_all_nothing_is_suppressed(self):
        """The invariant: no co-tenant means the reap and the sweep go ahead.

        Three shapes of "no co-tenant": an empty session table, a table whose
        only row IS the session being torn down named by its key, and the same
        row reached by identity when the key has already been replaced. In each,
        the answer has to be False, or a shutdown with nothing else on the
        process would leave the process and its escaped children behind.
        """
        from kiro_crew.session_lifecycle import _pid_has_live_tenant

        doomed = types.SimpleNamespace(
            provider=types.SimpleNamespace(
                _client=types.SimpleNamespace(_pid=7101),
                is_process_alive=lambda: True,
            )
        )
        no_pool = {"pool_tenancy": lambda pid: False}

        assert (
            _pid_has_live_tenant(
                7101,
                sessions={},
                exclude_key="dashboard:chat-7-1",
                exclude_session=None,
                **no_pool,
            )
            is False
        )
        assert (
            _pid_has_live_tenant(
                7101,
                sessions={"dashboard:chat-7-1": doomed},
                exclude_key="dashboard:chat-7-1",
                exclude_session=None,
                **no_pool,
            )
            is False
        )
        assert (
            _pid_has_live_tenant(
                7101,
                sessions={"dashboard:chat-7-2": doomed},
                exclude_key="a-replaced-key",
                exclude_session=doomed,
                **no_pool,
            )
            is False
        )
