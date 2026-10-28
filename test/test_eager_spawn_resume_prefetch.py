"""The resume prefetch: the TTL backstop's conditional removal, chat_runner's ``allow_resume``
wiring and the session recap a resume shows, the Tool Search replay gate checked before a
spawn, and the sid a speculative resume keeps when it falls back.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eager_spawn_helpers import (  # noqa: F401 -- fixtures are used by name
    _cfg,
    _isolate_armed_registry,
    _mock_state,
    _native_resume_cfg,
    _pin_prewarm_allowance,
    _stub_factory,
)

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.testing.wait import LoopHop, finished
from kiro_crew.warm_task_brief_contract import (
    WARM_TASK_BRIEF_CLAIM_KIND,
    WARM_TASK_BRIEF_RESULT_KIND,
)


class TestRemoveIfUnclaimed:
    """The TTL backstop's conditional removal."""

    @pytest.fixture
    def cfg(self):
        from kiro_crew.config.loader import KiroCrewConfig

        c = KiroCrewConfig()
        c.agent.provider = "acp"
        c.session.pool_size = 0
        return c

    @pytest.mark.asyncio
    async def test_removes_armed_idle_session_and_preserves_map(self, cfg):
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:ttl-a"
        provider, _, _ = await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        mgr._session_map.set(key, "sid-ttl-a")
        assert await mgr.remove_if_unclaimed(key) is True
        assert key not in mgr._sessions
        provider.shutdown.assert_awaited()
        # The mapping survives so the next open resumes normally. Bypass
        # SessionMap.get's transcript-existence pruning — only presence in
        # the store matters here.
        assert mgr._session_map._data.get(key) is not None

    @pytest.mark.asyncio
    async def test_noops_after_real_claim(self, cfg):
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:ttl-b"
        await mgr.get_or_create(key, speculative=True)
        mgr.release(key)
        await mgr.get_or_create(key)  # real turn consumes the marker
        mgr.release(key)
        assert await mgr.remove_if_unclaimed(key) is False
        assert key in mgr._sessions

    @pytest.mark.asyncio
    async def test_noops_while_semaphore_held(self, cfg):
        """A claimant mid-acquire (semaphore held) must never lose the
        session under it."""
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        key = "dashboard:ttl-c"
        await mgr.get_or_create(key, speculative=True)  # release NOT called
        assert await mgr.remove_if_unclaimed(key) is False
        assert key in mgr._sessions
        mgr.release(key)

    @pytest.mark.asyncio
    async def test_noops_on_missing_key(self, cfg):
        from kiro_crew.session import SessionManager

        mgr = SessionManager(cfg, provider_factory=_stub_factory())
        assert await mgr.remove_if_unclaimed("dashboard:absent") is False


class _ParkedRecap:
    """A resumed provider whose runtime slot holds one recap until it is taken."""

    def __init__(self, recap: str) -> None:
        self.parked: str | None = recap
        self.takes = 0

    def take_session_recap(self) -> str | None:
        self.takes += 1
        recap, self.parked = self.parked, None
        return recap


class _PendingWindowProbe:
    """A lazy transcript window that records where its blocking read ran."""

    def __init__(
        self,
        rows: list[dict],
        *,
        on_read: Callable[[], None] | None = None,
    ) -> None:
        self._rows = rows
        self._on_read = on_read
        self.read_thread: int | None = None

    def read(self, _slot: _ChatSlot) -> tuple[list[dict], list[set[str]]]:
        self.read_thread = threading.get_ident()
        if self._on_read is not None:
            self._on_read()
        return self._rows, []

    def __call__(
        self,
        slot: _ChatSlot,
        rows: list[dict] | None = None,
        _claims: list[set[str]] | None = None,
    ) -> None:
        if rows is None:
            rows, _claims = self.read(slot)
        slot._messages = list(rows)


async def _cancel_prefetch_ttl(slot: _ChatSlot) -> None:
    ttl = getattr(slot, "_prefetch_ttl_task", None)
    assert ttl is not None
    ttl.cancel()
    await asyncio.gather(ttl, return_exceptions=True)


def _recap_rows(slot: _ChatSlot) -> list[str]:
    """The text of every recap notice row in *slot*'s transcript, in order."""
    return [m["content"] for m in slot.messages if (m.get("meta") or {}).get("kind") == "recap"]


class TestResumePrefetchWiring:
    """chat_runner's allow_resume path: flag pass-through and the TTL arm."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @pytest.mark.asyncio
    async def test_allow_resume_passes_speculative_resume(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        kwargs = state.sessions.get_or_create.await_args.kwargs
        assert kwargs["speculative"] is True
        assert kwargs["speculative_resume"] is True

    @pytest.mark.asyncio
    async def test_default_path_does_not_opt_in(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)):
            await chat_runner._eager_spawn(state, slot)
        assert state.sessions.get_or_create.await_args.kwargs["speculative_resume"] is False

    @pytest.mark.asyncio
    async def test_resumed_prefetch_arms_ttl(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, True))
        with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        ttl = getattr(slot, "_prefetch_ttl_task", None)
        assert ttl is not None and not ttl.done()
        ttl.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ttl

    @pytest.mark.asyncio
    async def test_fresh_spawn_does_not_arm_ttl(self):
        """A non-resumed session holds no prior transcript's native lock —
        the idle sweep alone owns its lifetime."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)  # get_or_create returns resumed=False
        with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        assert getattr(slot, "_prefetch_ttl_task", None) is None

    @pytest.mark.asyncio
    async def test_resumed_prefetch_shows_the_recap_before_any_prompt(self):
        """The recap KAS replayed during the speculative load lands in the
        transcript now, while the user has not typed anything yet."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        provider = MagicMock()
        provider.take_session_recap = MagicMock(return_value="Goal: X. Next: Y.")
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, True))
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()),
            patch.object(chat_runner, "_append_recap_notice") as notice,
        ):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        notice.assert_called_once_with(state, slot, "Goal: X. Next: Y.")
        slot._prefetch_ttl_task.cancel()

    @pytest.mark.asyncio
    async def test_a_pending_window_loads_off_loop_before_the_recap_check(self):
        loop_thread = threading.get_ident()
        slot = _ChatSlot("t1")
        pending = _PendingWindowProbe([])
        slot._lazy_window = pending
        assert slot.window_pending
        state = _mock_state(slot)
        provider = _ParkedRecap("Goal: X. Next: Y.")
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, True))

        try:
            with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
                await chat_runner._eager_spawn(state, slot, allow_resume=True)
        finally:
            await _cancel_prefetch_ttl(slot)

        assert pending.read_thread is not None
        assert pending.read_thread != loop_thread, "the pending window read ran on the event loop"
        assert not slot.window_pending
        assert _recap_rows(slot) == ["Where you left off: Goal: X. Next: Y."]
        assert (provider.parked, provider.takes) == (None, 1)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "change",
        [
            pytest.param("turn_started", id="turn-started"),
            pytest.param("slot_replaced", id="slot-replaced"),
            pytest.param("slot_deleted", id="slot-deleted"),
        ],
    )
    async def test_a_slot_change_during_the_window_load_suppresses_the_recap(self, change):
        loop_thread = threading.get_ident()
        loop_hop = LoopHop(asyncio.get_running_loop())
        slot = _ChatSlot("t1")
        live_slot: list[_ChatSlot | None] = [slot]

        def _change_slot() -> None:
            if change == "turn_started":
                slot._turn_admission_reserved = True
                assert slot.running
            elif change == "slot_replaced":
                live_slot[0] = _ChatSlot("t1")
            else:
                live_slot[0] = None

        async def _change_on_loop() -> None:
            _change_slot()

        def _change_during_read() -> None:
            assert (provider.parked, provider.takes) == (None, 1)
            if threading.get_ident() == loop_thread:
                _change_slot()
            else:
                loop_hop.run(_change_on_loop(), "the slot change during the pending read")

        pending = _PendingWindowProbe([], on_read=_change_during_read)
        slot._lazy_window = pending
        assert slot.window_pending
        state = _mock_state(slot)
        state.get_slot = MagicMock(side_effect=lambda _key: live_slot[0])
        provider = _ParkedRecap("Goal: X. Next: Y.")
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, True))

        try:
            with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
                await chat_runner._eager_spawn(state, slot, allow_resume=True)
        finally:
            slot._turn_admission_reserved = False
            await _cancel_prefetch_ttl(slot)
        loop_hop.check()

        assert pending.read_thread is not None
        assert (provider.parked, provider.takes) == (None, 1)
        assert _recap_rows(slot) == []
        assert pending.read_thread != loop_thread, "the slot changed outside an awaited worker read"

    @pytest.mark.asyncio
    async def test_a_turn_that_owns_the_slot_keeps_the_recap_parked(self):
        """A message sent during the load is already in the transcript, so the
        recap is not taken: it stays parked until the runtime drops it."""
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        provider = MagicMock()
        provider.take_session_recap = MagicMock(return_value="Goal: X. Next: Y.")
        # slot.running derives from slot.task being a live task.
        _turn = asyncio.get_running_loop().create_future()
        _task = asyncio.ensure_future(_turn)

        async def _claim(*_a, **_kw):
            slot.task = _task  # the user's message started a turn mid-load
            return (provider, True, True)

        state.sessions.get_or_create = AsyncMock(side_effect=_claim)
        try:
            with (
                patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()),
                patch.object(chat_runner, "_append_recap_notice") as notice,
            ):
                await chat_runner._eager_spawn(state, slot, allow_resume=True)
        finally:
            _turn.set_result(None)
            await _task
        state.sessions.get_or_create.assert_awaited_once()
        assert (
            getattr(slot, "_prefetch_ttl_task", None) is not None
        ), "the resumed path was not reached"
        slot._prefetch_ttl_task.cancel()
        notice.assert_not_called()
        provider.take_session_recap.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_recap_shows_nothing(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        provider = MagicMock()
        provider.take_session_recap = MagicMock(return_value=None)
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, True))
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()),
            patch.object(chat_runner, "_append_recap_notice") as notice,
        ):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        notice.assert_not_called()
        slot._prefetch_ttl_task.cancel()

    @pytest.mark.asyncio
    async def test_a_fresh_prefetch_never_reads_a_recap(self):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        provider = MagicMock()
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()),
            patch.object(chat_runner, "_append_recap_notice") as notice,
        ):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        provider.take_session_recap.assert_not_called()
        notice.assert_not_called()

    @staticmethod
    async def _resume(state, slot, provider) -> None:
        """One focus: a speculative resume of *provider*, then its TTL teardown.

        The TTL is pinned to 0 by the caller, so awaiting the task the resume
        armed runs the unclaimed-prefetch removal to its end.
        """
        state.sessions.get_or_create = AsyncMock(return_value=(provider, True, True))
        with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        await finished(slot._prefetch_ttl_task, "the resume prefetch's TTL teardown")

    @pytest.mark.asyncio
    async def test_a_reopen_with_no_turn_between_keeps_one_recap_row(self, monkeypatch):
        """Focus, the prefetch TTL tears the session down, focus again: no real
        message follows the first recap row, so the second resume adds none.
        The two recaps differ on purpose: the skip reads row kinds, never the
        recap text."""
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)

        await self._resume(state, slot, _ParkedRecap("Goal: X. Next: Y."))
        state.sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:t1")
        await self._resume(state, slot, _ParkedRecap("Goal: X. Next: Z."))

        assert _recap_rows(slot) == ["Where you left off: Goal: X. Next: Y."]
        assert len(slot.messages) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("role", "content", "cls", "meta"),
        [
            pytest.param(
                "assistant",
                "Session reloaded.",
                "msg msg-a",
                {"kind": "session_reload"},
                id="reload",
            ),
            pytest.param(
                "notice",
                "",
                "",
                {"kind": WARM_TASK_BRIEF_CLAIM_KIND, "fingerprint": "f1"},
                id="warm_brief_claim",
            ),
            pytest.param(
                "assistant",
                "Where this task stands: tests pass.",
                "msg msg-a",
                {"kind": WARM_TASK_BRIEF_RESULT_KIND},
                id="warm_brief_result",
            ),
            pytest.param(
                "error",
                "This chat's agent failed to start 3 times.",
                "msg msg-err",
                None,
                id="start_error",
            ),
        ],
    )
    async def test_a_status_row_after_the_recap_keeps_one_recap_row(
        self, monkeypatch, role, content, cls, meta
    ):
        """A row no turn wrote does not end the walk: Reload session's notice,
        a warm task brief and its hidden claim (main's history reads skip both),
        or the background-start stop error. KAS would replay the same recap, so
        the second resume adds none."""
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)

        await self._resume(state, slot, _ParkedRecap("Goal: X. Next: Y."))
        slot.append(role, content, cls, broadcast=False, meta=meta)
        await self._resume(state, slot, _ParkedRecap("Goal: X. Next: Y."))

        assert _recap_rows(slot) == ["Where you left off: Goal: X. Next: Y."]

    @pytest.mark.asyncio
    async def test_a_turn_between_reopens_shows_a_second_recap_row(self, monkeypatch):
        """A turn puts real messages after the recap row, so the next reopen
        appends the recap again, even when KAS replays the same text."""
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)

        await self._resume(state, slot, _ParkedRecap("Goal: X. Next: Y."))
        slot.append("user", "run the tests", "msg msg-u")
        slot.append("assistant", "All tests pass.", "msg msg-a")
        await self._resume(state, slot, _ParkedRecap("Goal: X. Next: Y."))

        assert _recap_rows(slot) == [
            "Where you left off: Goal: X. Next: Y.",
            "Where you left off: Goal: X. Next: Y.",
        ]
        assert [m["role"] for m in slot.messages] == ["assistant", "user", "assistant", "assistant"]

    @pytest.mark.asyncio
    async def test_a_skipped_recap_is_still_taken(self, monkeypatch):
        """Skipping the row does not leave the recap in the provider's slot:
        it is taken on every resume no turn owns."""
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        first = _ParkedRecap("Goal: X. Next: Y.")
        second = _ParkedRecap("Goal: X. Next: Y.")

        await self._resume(state, slot, first)
        await self._resume(state, slot, second)

        assert len(_recap_rows(slot)) == 1
        assert (first.parked, first.takes) == (None, 1)
        assert (second.parked, second.takes) == (None, 1)

    @pytest.mark.asyncio
    async def test_prefetch_ttl_removes_unclaimed_session(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        await chat_runner._prefetch_ttl(state, slot, "dashboard:t1")
        state.sessions.remove_if_unclaimed.assert_awaited_once_with("dashboard:t1")

    @pytest.mark.asyncio
    async def test_prefetch_ttl_bails_when_slot_replaced(self, monkeypatch):
        """A DIFFERENT slot object under the same key owns the key now."""
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.get_slot = MagicMock(return_value=_ChatSlot("t1"))  # replaced
        state.sessions.remove_if_unclaimed = AsyncMock()
        await chat_runner._prefetch_ttl(state, slot, "dashboard:t1")
        state.sessions.remove_if_unclaimed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prefetch_ttl_still_reaps_after_slot_deletion(self, monkeypatch):
        """Slot DELETION must not skip the reap: the delete handler removes
        the slot-key-derived history session, not a linked session key a
        channel-born slot's prefetch registered under — returning early would
        leak that process holding the native lock. The conditional removal is
        safe to run: it no-ops on an already-removed key and never touches a
        claimed session."""
        monkeypatch.setattr(chat_runner, "_RESUME_PREFETCH_TTL_SECS", 0)
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        state.get_slot = MagicMock(return_value=None)  # slot deleted
        state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
        await chat_runner._prefetch_ttl(state, slot, "slack:12345.678")
        state.sessions.remove_if_unclaimed.assert_awaited_once_with("slack:12345.678")


class TestToolSearchReplayPrefetchGate:
    """The resume prefetch asks the provider's replay predicate before it spawns.

    With Tool Search on, the kiro backend answers a direct dashboard resume with
    a fresh session plus conversation replay instead of ``session/load``, so the
    speculative load comes back ``resumed=False`` and is refused -- after a
    whole runtime was spawned and torn down. ``_eager_spawn`` reads the same
    predicate the provider does and leaves such a slot to its first turn.
    """

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _no_debounce(self, monkeypatch):
        monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)

    @pytest.mark.asyncio
    async def test_replayed_resume_spawns_nothing(self, caplog):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        with (
            patch.object(chat_runner.KiroCrewConfig, "load", _cfg(True)),
            caplog.at_level("INFO", logger=chat_runner.logger.name),
        ):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        state.sessions.get_or_create.assert_not_awaited()
        reasons = [r.getMessage() for r in caplog.records if "left to first turn" in r.getMessage()]
        assert reasons == ["Eager spawn: dashboard:t1 left to first turn (tool-search replay)"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "native",
        [
            pytest.param({"tool_search": False}, id="tool-search-off"),
            pytest.param({"acp_backend": ACP_BACKEND_KAS}, id="non-kiro-backend"),
        ],
    )
    async def test_native_resume_still_prefetches(self, native):
        slot = _ChatSlot("t1")
        state = _mock_state(slot)
        loader = _cfg(True)
        for name, value in native.items():
            setattr(loader.return_value.agent, name, value)
        with patch.object(chat_runner.KiroCrewConfig, "load", loader):
            await chat_runner._eager_spawn(state, slot, allow_resume=True)
        assert state.sessions.get_or_create.await_args.kwargs["speculative_resume"] is True


class TestSpecResumeFallbackMapGuard:
    """A speculative resume that fell back must not overwrite the sid."""

    @pytest.fixture
    def cfg(self):
        from kiro_crew.config.loader import KiroCrewConfig

        c = KiroCrewConfig()
        c.agent.provider = "acp"
        c.session.pool_size = 0
        return c

    @pytest.mark.asyncio
    async def test_fallback_keeps_original_sid(self, cfg, tmp_path, monkeypatch):
        from kiro_crew.session import SessionManager

        old_sid = "real-transcript-sid"
        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        (sessions_dir / f"{old_sid}.json").write_text("{}")
        (sessions_dir / f"{old_sid}.jsonl").write_text("x" * 32)

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            m.is_process_alive = lambda: True
            m.cwd = "/tmp"
            m.client = MagicMock()
            m.client.resumed = False  # the load FELL BACK to a fresh session
            m.client._session_id = "empty-fallback-sid"
            return m

        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", object)
        mgr = SessionManager(cfg, provider_factory=factory)
        key = "dashboard:fallback-a"
        mgr._session_map.set(key, old_sid)

        from kiro_crew.session import SpeculativeResumeRefused

        # A failed speculative resume is rejected BEFORE registration: no
        # claimable fallback session may ever exist — a real turn queued
        # during the load would claim it and strand its exchanges behind the
        # preserved old sid on the next reopen.
        with pytest.raises(SpeculativeResumeRefused):
            await mgr.get_or_create(key, speculative=True, speculative_resume=True)
        # Nothing registered; the pointer to the real transcript survives.
        assert key not in mgr._sessions
        assert mgr._session_map.get(key) == old_sid

    @pytest.mark.asyncio
    async def test_provider_switch_fallback_never_persists_empty_sid(
        self, cfg, tmp_path, monkeypatch
    ):
        """The switch branch mutates ``resume_sid`` to
        None, so a classification keyed on ``resume_sid`` misreads the
        provider-switch fallback as a normal fresh session and persists the
        EMPTY speculative sid — the next real open would resume that empty
        session (resumed=True) and skip the history replay, losing the prior
        context. Classification must key on the caller's ``speculative_resume``
        opt-in, which no branch mutates."""
        from kiro_crew.session import SessionManager

        old_sid = "real-transcript-sid"
        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        (sessions_dir / f"{old_sid}.json").write_text("{}")
        (sessions_dir / f"{old_sid}.jsonl").write_text("x" * 32)

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            m.is_process_alive = lambda: True
            m.cwd = "/tmp"
            m.client = MagicMock()
            m.client.resumed = False  # no session/load ran: sid was discarded
            m.client._session_id = "empty-fallback-sid"
            return m

        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", object)
        # Force the switch branch: resume_sid is cleared and the stored sid
        # wiped, exactly the mutation the classification must be immune to.
        monkeypatch.setattr("kiro_crew.session.detect_provider_switch", lambda *a: True)
        mgr = SessionManager(cfg, provider_factory=factory)
        key = "dashboard:fallback-switch"
        mgr._session_map.set(key, old_sid)

        from kiro_crew.session import SpeculativeResumeRefused

        # The provider-switch branch clears resume_sid mid-flight; the
        # rejection must key on the caller's opt-in and fire anyway, so the
        # empty speculative sid can never be persisted or claimed.
        with pytest.raises(SpeculativeResumeRefused):
            await mgr.get_or_create(key, speculative=True, speculative_resume=True)
        assert key not in mgr._sessions
        # The empty speculative sid must never land in the map: the next real
        # open cold-starts fresh (resumed=False) and injects history normally.
        assert mgr._session_map.get(key) != "empty-fallback-sid"

    @pytest.mark.asyncio
    async def test_refusal_kills_provider_via_nonblocking_dispatch(
        self, cfg, tmp_path, monkeypatch
    ):
        """The refusal raise lands in the post-start BaseException handler,
        which is ROUTINE under resume prefetch (every failed load passes
        through it). It must kill the orphaned provider via the executor
        dispatch, never inline — _sync_kill_provider blocks the event loop
        (os.waitpid / taskkill)."""
        from kiro_crew.session import SessionManager, SpeculativeResumeRefused

        old_sid = "real-transcript-sid"
        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._kiro_sessions_dir", lambda: sessions_dir)
        (sessions_dir / f"{old_sid}.json").write_text("{}")
        (sessions_dir / f"{old_sid}.jsonl").write_text("x" * 32)

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            m.is_process_alive = lambda: True
            m.cwd = "/tmp"
            m.client = MagicMock()
            m.client.resumed = False  # the load FELL BACK
            m.client._session_id = "empty-fallback-sid"
            return m

        monkeypatch.setattr("kiro_crew.providers.acp.AcpProvider", object)
        mgr = SessionManager(cfg, provider_factory=factory)
        key = "dashboard:kill-dispatch"
        mgr._session_map.set(key, old_sid)

        with patch.object(SessionManager, "_dispatch_hard_kill") as dispatch:
            with pytest.raises(SpeculativeResumeRefused):
                await mgr.get_or_create(key, speculative=True, speculative_resume=True)
        dispatch.assert_called_once()
