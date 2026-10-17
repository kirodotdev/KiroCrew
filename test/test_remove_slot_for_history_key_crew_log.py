"""Tests for the session ledgers and crew-log units a permanent history delete removes.

A permanent delete removes the conversation's proved crew-log units and its session
ledger, keeps the units another holder, the trash or a successor still owns, and
leaves the work ledger intact.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from remove_slot_for_history_key_helpers import (
    _guard_work_ledger_cleanup,
    _make_slot,
    _make_state,
    _remove_slot_for_history_key,
)

from kiro_crew import session_ledger
from kiro_crew.dashboard.handlers import sessions as sessions_module
from kiro_crew.dashboard.handlers.sessions import (
    _capture_history_delete_claim,
    _exclude_slot_units,
    _HistoryDeleteClaim,
    _resolve_history_delete_claim,
)


class _LedgerExclusionError(RuntimeError):
    """Stands in for ``session_ledger.LedgerExclusionError``."""


class TestSessionLedgerOnPermanentDelete:
    """Permanently deleting a session takes its append-only ledger with it.

    The work ledger is deliberately PRESERVED by this same funnel, and these
    tests hold both halves of that line at once. The difference is mechanical:
    a work ledger is keyed by the recycled SLOT KEY, so a successor tab in the
    same slot legitimately inherits it and no check here can prove one is not
    coming; a session ledger is keyed by the ACP SESSION ID, which never names a
    different conversation, and it carries a write lease so the removal is
    refused rather than racing a writer.
    """

    @staticmethod
    def _ledger(session_id: str, slot: str = "dashboard_chat-1-100"):
        """A session ledger whose header names *slot*, as a real one does.

        The slot is not decoration: the removal requires the unit's own header to
        name a slot this delete proved, because the session id reaches it through
        ``session_map.json`` while the header is written once inside the fenced
        ledger tree. A fixture with no slot is a unit the funnel must refuse, which
        is what the two tests below use it for.
        """
        from kiro_crew import crew_log as lg

        return lg.CrewLog.create(
            lg.KIND_SESSION, session_id, owner="default", agent="kirocrew", slot=slot
        )

    @staticmethod
    def _exists(session_id: str) -> bool:
        from kiro_crew import crew_log as lg

        return lg.CrewLog.exists(lg.KIND_SESSION, session_id)

    @pytest.mark.asyncio
    async def test_the_deleted_sessions_ledger_is_removed(self, monkeypatch):
        """The gap the retention writer exists to close, on the delete path.

        Bodies live in the ledger now, so a delete that took only the transcript
        left the larger half of the same history on disk.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        self._ledger("acp-doomed")
        assert self._exists("acp-doomed")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-doomed")

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert not self._exists("acp-doomed")

    @pytest.mark.asyncio
    async def test_no_other_sessions_ledger_is_touched(self, monkeypatch):
        """Only the id this claim proved. A sibling conversation is not collateral."""
        _guard_work_ledger_cleanup(monkeypatch)
        self._ledger("acp-doomed")
        self._ledger("acp-bystander")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-doomed")

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert not self._exists("acp-doomed")
        assert self._exists("acp-bystander")

    @pytest.mark.asyncio
    async def test_a_ledger_the_emitter_itself_opened_is_still_removed(self, monkeypatch):
        """The PRODUCTION shape: the gateway that deletes is also the writer.

        ``CrewLog.create`` in the other tests leaves nothing holding the unit, but
        the real gateway does -- the emitter keeps a per-session handle, and that
        handle carries the unit's write lease. The removal claims the lease
        ``sole``, which refuses a handle held anywhere in this process, so the
        funnel cannot collect a real ledger until that handle is released. What
        releases it is the ``session/closed`` entry landing, which
        ``session_lifecycle.destroy`` writes and this funnel then FLUSHES before
        claiming the lease.

        The stub teardown below emits that close itself, because the real
        ``destroy`` does, and a stub that omitted it would be testing a gateway
        that does not exist -- which is precisely how this defect stayed
        invisible. ``destroy``'s own emit is pinned separately, driven through the
        real method, in ``test_crew_log_retention.py``.

        The pre-assertion is the point of the test. Asserting only that the ledger
        is gone would pass against a fixture that never held the lease, which is
        exactly how the rest of this class was green while the funnel could not
        remove a real one.
        """
        from kiro_crew import crew_log as lg
        from kiro_crew.crew_log import emit
        from kiro_crew.crew_log.store import REMOVE_OWNED, remove_unit
        from kiro_crew.metrics.sessions import END_REASON_DESTROYED

        _guard_work_ledger_cleanup(monkeypatch)
        monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
        emit.reset_caches()
        try:
            emit.on_session_opened(
                "acp-emitted", agent="kirocrew", slot="dashboard_chat-1-100", cwd="/tmp"
            )
            assert emit.flush(timeout=5.0)
            assert self._exists("acp-emitted")
            assert (
                remove_unit(lg.KIND_SESSION, "acp-emitted", guard=lambda _dir: True) == REMOVE_OWNED
            ), "fixture did not reproduce the emitter-held lease this test exists for"

            slot = _make_slot("dashboard_chat-1-100")
            state = _make_state({"dashboard_chat-1-100": slot})
            state.sessions.resumable_sid = MagicMock(return_value="acp-emitted")

            async def _destroy_like_the_real_one(*_args, **_kwargs):
                emit.on_session_closed("acp-emitted", END_REASON_DESTROYED)
                return True

            state.sessions.destroy_if = _destroy_like_the_real_one

            await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

            assert not self._exists("acp-emitted")
        finally:
            emit.reset_caches()

    @pytest.mark.asyncio
    async def test_the_teardown_entry_never_starts_a_ledger_for_a_session_without_one(
        self, monkeypatch
    ):
        """Recording the teardown must not CREATE the file it exists to release.

        ``_handle`` never creates a ledger, so a close for a session that has none
        is a policy no-op. Without that, the funnel would write a header for a
        conversation it is deleting and then have to remove it -- and would leave
        one behind on any pass where the removal was refused.

        The removal is refused HERE for that reason. A test that let it succeed
        could not see this property at all: a ledger created and then removed
        leaves exactly the state of one that was never created, so the end-state
        assertion would pass either way. Refusing the removal is the one reading
        where the two diverge.
        """
        from kiro_crew.crew_log import emit
        from kiro_crew.crew_log import store as crew_log_store

        _guard_work_ledger_cleanup(monkeypatch)
        monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
        monkeypatch.setattr(
            crew_log_store, "remove_unit", lambda *_a, **_k: crew_log_store.REMOVE_OWNED
        )
        emit.reset_caches()
        try:
            slot = _make_slot("dashboard_chat-1-100")
            state = _make_state({"dashboard_chat-1-100": slot})
            state.sessions.resumable_sid = MagicMock(return_value="acp-never-opened")

            await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

            assert not self._exists("acp-never-opened")
        finally:
            emit.reset_caches()

    @pytest.mark.asyncio
    async def test_a_ledger_whose_header_names_another_slot_is_never_removed(self, monkeypatch):
        """The forged-mapping path, closed by the unit's own header.

        The session id reaches this funnel through ``session_map.json``, which lives
        inside the agent-visible tree, so a mapping that named a victim's session
        would aim the removal at the victim's ledger. The header is the independent
        answer: written once inside the fenced ledger tree and never rewritten, so
        it does not move when a mapping does.

        The victim ledger here is a REAL, removable unit -- same kind, closed, no
        writer -- so the only thing standing between the removal and it is the slot
        check. A fixture the removal would have refused anyway could not tell the
        two readings apart.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        self._ledger("acp-victim", slot="dashboard_chat-9-900")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        # The forgery: this slot's mapping points at the victim's session.
        state.sessions.resumable_sid = MagicMock(return_value="acp-victim")

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert self._exists("acp-victim"), "a mapping alone redirected the removal"

    @pytest.mark.asyncio
    async def test_a_ledger_with_no_slot_in_its_header_is_left_to_retention(self, monkeypatch):
        """Cannot prove means do not remove, rather than assume it is ours.

        A session that never ran on a dashboard slot writes no slot in its header.
        There is then no second answer to check the mapping against, so the funnel
        removes nothing and the sweep collects the unit on age once its close has
        landed.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        from kiro_crew import crew_log as lg

        lg.CrewLog.create(lg.KIND_SESSION, "acp-slotless", owner="default", agent="kirocrew")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-slotless")

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert self._exists("acp-slotless")

    @pytest.mark.asyncio
    async def test_a_ledger_another_key_still_maps_to_is_left_to_retention(self, monkeypatch):
        """A second key on the same session id shares THIS ledger and can resume it.

        The unit is keyed by the ACP id, and the teardown removed only one mapping,
        so a surviving key still resolves that id -- its holder can resume, and the
        log is still needed. Two keys on one sid is a state the system itself
        produces: importing a transferred session twice allocates a new slot key
        each time and deliberately leaves the source intact.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        self._ledger("acp-shared")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-shared")
        state.sessions.find_key_by_sid = MagicMock(return_value="dashboard_chat-7-700")

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert self._exists("acp-shared"), "a ledger another key can still resume was removed"

    @pytest.mark.asyncio
    async def test_an_unreadable_map_leaves_the_ledger_rather_than_removing_it(self, monkeypatch):
        """Cannot read means cannot prove, and the safe answer keeps the history."""
        _guard_work_ledger_cleanup(monkeypatch)
        self._ledger("acp-unknown-map")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-unknown-map")
        state.sessions.find_key_by_sid = MagicMock(side_effect=RuntimeError("map unreadable"))

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert self._exists("acp-unknown-map")

    @pytest.mark.asyncio
    async def test_an_unresolvable_acp_id_removes_nothing(self, monkeypatch):
        """No proven id, no removal -- retention collects it on age instead.

        Reading the id is best-effort and happens LAST, so a session manager that
        cannot answer must not turn a delete into a guess about which ledger to
        remove.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        self._ledger("acp-unknown")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(side_effect=RuntimeError("store unreadable"))

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert self._exists("acp-unknown")

    @pytest.mark.asyncio
    async def test_a_non_string_session_id_is_read_as_absent(self, monkeypatch):
        """This value goes on to ADDRESS a directory, so it is type-checked.

        Anything that is not a real id must read as absent rather than being
        folded into a path.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        self._ledger("acp-safe")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value=MagicMock())

        claim = _capture_history_delete_claim(state, "dashboard_chat-1-100")
        assert claim.acp_session_id is None
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert self._exists("acp-safe")

    @pytest.mark.asyncio
    async def test_a_claim_that_lost_its_slot_also_loses_the_ledger_id(self):
        """The id travels with ``session_key``, on every path that disowns the claim.

        A claim the worker proved does not own the transcript must not still name
        that session's ledger -- otherwise resolving ownership DOWN would still
        authorize the removal it just withdrew.
        """
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-should-not-travel")
        claim = replace(
            _capture_history_delete_claim(state, "dashboard_chat-1-100"),
            path_match_verified=False,
            history_key=None,
        )
        assert claim.acp_session_id == "acp-should-not-travel"

        resolved = _resolve_history_delete_claim(MagicMock(), "dashboard_chat-1-100", claim)

        assert resolved.session_key is None
        assert resolved.acp_session_id is None

    @pytest.mark.asyncio
    async def test_a_ledger_a_writer_still_owns_is_left_to_retention(self, monkeypatch):
        """``owned`` is an ordinary answer here, not an error.

        A queued write can still hold the lease when the teardown runs. The delete
        does not wait for it and does not fail: the retention sweep collects the
        ledger once its writer is gone.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        held = self._ledger("acp-busy")
        # The append is what claims the lease -- ownership is taken lazily, on a
        # handle's first write.
        held.append(
            "session/opened",
            {
                "agent": "kirocrew",
                "slot": "dashboard_chat-1-100",
                "model": "",
                "cwd": "",
                "owner": "default",
                "resumed": False,
            },
            src="gateway",
        )
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-busy")

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert self._exists("acp-busy")
        assert held.path.exists()

    @pytest.mark.asyncio
    async def test_a_refused_teardown_keeps_the_ledger(self, monkeypatch):
        """Gated on the teardown SUCCEEDING, not on having been attempted.

        ``destroy_if`` refuses when the session was replaced by a successor
        generation, is busy, or still has a live slot owner -- and in each case the
        session it names is preserved and may be writing right now. The write lease
        is no substitute: ownership ends BETWEEN turns by design, so an
        idle-but-live session holds nothing for the removal to be refused by.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        self._ledger("acp-preserved")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-preserved")
        # A successor generation won the slot, so the conditional destroy refuses.
        state.sessions.destroy_if = AsyncMock(return_value=False)

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert self._exists("acp-preserved")

    @pytest.mark.asyncio
    async def test_a_raising_teardown_keeps_the_ledger(self, monkeypatch):
        """A destroy that threw proves nothing about whether the session is gone."""
        _guard_work_ledger_cleanup(monkeypatch)
        self._ledger("acp-unknown-state")
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-unknown-state")
        state.sessions.destroy_if = AsyncMock(side_effect=RuntimeError("manager unreachable"))

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert self._exists("acp-unknown-state")

    @pytest.mark.asyncio
    async def test_a_failing_ledger_removal_never_fails_the_delete(self, monkeypatch):
        """The transcript row is already gone by the time this runs.

        Raising would ask the user to retry a delete against a row that is absent,
        and the sweep collects the ledger on age regardless.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-explodes")

        from kiro_crew.crew_log import store as crew_log_store

        def _boom(*_args, **_kwargs):
            raise RuntimeError("ledger tree unreadable")

        monkeypatch.setattr(crew_log_store, "remove_unit", _boom)

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        assert "dashboard_chat-1-100" not in state._slots


class TestEveryCrewLogOfTheConversationGoes:
    """A conversation owns one crew-log unit per ACP id it ran under.

    A reset, an agent or model switch and a provider swap each start a new unit, so a
    delete that took only the current one left the rest of the history on disk.
    """

    @staticmethod
    def _unit(session_id: str, slot: str, *, closed: bool = True) -> None:
        from crew_log_type_helpers import minimal_data

        from kiro_crew import crew_log as lg

        log = lg.CrewLog.create(
            lg.KIND_SESSION, session_id, owner="default", agent="kirocrew", slot=slot
        )
        log.append("session/opened", minimal_data(lg.KIND_SESSION, "session/opened"), src="gateway")
        if closed:
            log.append("session/closed", {"reason": "reset"}, src="gateway")
        del log

    _exists = staticmethod(TestSessionLedgerOnPermanentDelete._exists)

    @pytest.mark.asyncio
    async def test_a_reset_predecessor_goes_with_the_current_unit(self, monkeypatch):
        _guard_work_ledger_cleanup(monkeypatch)
        self._unit("acp-before-reset", "dashboard_chat-1-100")
        self._unit("acp-current", "dashboard_chat-1-100", closed=False)
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.resumable_sid = MagicMock(return_value="acp-current")
        # What the locked pre-unlink transaction recorded for this slot.
        claim = replace(
            _capture_history_delete_claim(state, "dashboard_chat-1-100"),
            ledger_excluded_units=frozenset({"acp-before-reset", "acp-current"}),
        )

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100", delete_claim=claim)

        assert not self._exists("acp-before-reset")
        assert not self._exists("acp-current")

    @pytest.mark.asyncio
    async def test_a_row_with_no_open_tab_takes_no_crew_log(self, monkeypatch):
        """No tab proves which units were this row's, and its slot key may be reused.

        The other conversation's superseded unit names the same recycled key and is
        absent from the session map, so a header match would take it. None is taken.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        self._unit("acp-this-row", "chat-7")
        self._unit("acp-other-superseded", "chat-7")
        self._unit("acp-other-current", "chat-7", closed=False)
        state = _make_state({})

        await _remove_slot_for_history_key(state, "dashboard:chat-7")
        await _remove_slot_for_history_key(state, "dashboard_chat-7")

        assert self._exists("acp-other-superseded")
        assert self._exists("acp-other-current")
        assert self._exists("acp-this-row")

    @staticmethod
    def _remove(state, units, *, live=lambda: frozenset(), protected=frozenset()):
        sessions_module._remove_session_crew_logs(
            state.sessions,
            "dashboard:chat-7",
            frozenset({"chat-7"}),
            frozenset(units),
            frozenset(protected),
            live_units=live,
        )

    def test_a_proved_unit_that_resumes_before_its_removal_is_kept(self, monkeypatch):
        """The map then names the deleted row's own key and the lease is free between
        turns, so only a fresh read of what is running tells it from a finished unit."""
        from kiro_crew.crew_log import store as crew_log_store

        _guard_work_ledger_cleanup(monkeypatch)
        self._unit("acp-resumed", "chat-7")
        self._unit("acp-finished", "chat-7")
        state = _make_state({})
        running: set[str] = set()
        real_remove = crew_log_store.remove_unit

        def _resume_then_remove(kind, unit, *, guard):
            running.add("acp-resumed")
            state.sessions.find_key_by_sid = MagicMock(
                side_effect=lambda sid, exclude="": (
                    "dashboard:chat-7" if sid == "acp-resumed" and not exclude else None
                )
            )
            return real_remove(kind, unit, guard=guard)

        monkeypatch.setattr(crew_log_store, "remove_unit", _resume_then_remove)

        self._remove(state, {"acp-resumed", "acp-finished"}, live=lambda: frozenset(running))

        assert self._exists("acp-resumed")
        assert not self._exists("acp-finished")

    def test_a_proved_unit_the_trash_still_holds_is_kept(self, monkeypatch):
        """A unit a failed restore left live and held belongs to a session in the trash."""
        from kiro_crew import crew_log as lg
        from kiro_crew.crew_log.store import hold_unit

        _guard_work_ledger_cleanup(monkeypatch)
        self._unit("acp-trashed", "chat-7")
        self._unit("acp-this-row", "chat-7")
        assert hold_unit(lg.KIND_SESSION, "acp-trashed")
        state = _make_state({})

        self._remove(state, {"acp-trashed", "acp-this-row"})

        assert self._exists("acp-trashed")
        assert not self._exists("acp-this-row")

    def test_a_proved_unit_another_holder_still_uses_is_kept(self, monkeypatch):
        """Mapped to another conversation, served by a live slot, or running: all kept.

        A mapping under the deleted row's own key is not a veto: its transcript is the
        one this delete removed.
        """
        _guard_work_ledger_cleanup(monkeypatch)
        self._unit("acp-shared", "chat-7")
        self._unit("acp-served", "chat-7")
        self._unit("acp-running", "chat-7", closed=False)
        self._unit("acp-own-mapping", "chat-7")
        mapping = {"acp-shared": "dashboard:chat-9", "acp-own-mapping": "dashboard:chat-7"}
        state = _make_state({})
        state.sessions.find_key_by_sid = MagicMock(
            side_effect=lambda sid, exclude="": (
                None if mapping.get(sid) == exclude else mapping.get(sid)
            )
        )

        self._remove(
            state,
            {"acp-shared", "acp-served", "acp-running", "acp-own-mapping"},
            protected={"acp-served"},
            live=lambda: frozenset({"acp-served", "acp-running"}),
        )

        assert self._exists("acp-shared")
        assert self._exists("acp-served")
        assert self._exists("acp-running")
        assert not self._exists("acp-own-mapping")


class TestAKeptCrewLogIsNotLeftExcluded:
    """A unit kept because it is someone else's stays in its slot's ledger fold.

    A unit of the deleted conversation that could not be removed stays excluded: giving
    its exclusion back would let a later session on the recycled slot key fold it.
    """

    @staticmethod
    def _run(monkeypatch, *, status=None, raises=False, mapped_after_exclusion=False, held=False):
        from kiro_crew.crew_log import store as crew_log_store

        excluded: list[tuple[str, tuple[str, ...]]] = []
        given_back: list[tuple[str, tuple[str, ...]]] = []

        def _exclude(slot_key, units, **kwargs):
            # Past the unlink there is nothing left to refuse.
            assert kwargs == {"refusable": False}
            excluded.append((slot_key, tuple(units)))
            return session_ledger.SlotExclusion(tuple(units), False)

        def _unexclude(slot_key, units, **_k):
            given_back.append((slot_key, tuple(units)))

        monkeypatch.setattr(session_ledger, "exclude_units", _exclude)
        monkeypatch.setattr(session_ledger, "unexclude_units", _unexclude)
        monkeypatch.setattr(
            crew_log_store, "session_units_by_slot", lambda **_k: {"chat-7": ("acp-kept",)}
        )

        monkeypatch.setattr(crew_log_store, "is_trash_held", lambda _directory: held)

        def _remove(*_a, guard, **_k):
            if raises:
                raise RuntimeError("tree unreadable")
            if not guard(Path("unit-dir")):
                return crew_log_store.REMOVE_OWNED
            return status

        monkeypatch.setattr(crew_log_store, "remove_unit", _remove)
        sessions = MagicMock()
        calls = {"n": 0}

        def _find(sid, exclude=""):
            calls["n"] += 1
            if mapped_after_exclusion and calls["n"] > 1:
                return "dashboard:chat-9"
            return None

        sessions.find_key_by_sid = _find
        sessions_module._remove_session_crew_logs(
            sessions,
            "dashboard:chat-7",
            frozenset({"chat-7"}),
            frozenset({"acp-kept"}),
            frozenset(),
        )
        return excluded, given_back

    @pytest.mark.parametrize("status", ["owned", "failed"])
    def test_an_own_unit_the_removal_could_not_take_stays_excluded(self, monkeypatch, status):
        excluded, given_back = self._run(monkeypatch, status=status)
        assert excluded == [("chat-7", ("acp-kept",))]
        assert given_back == []

    def test_a_removal_that_raised_keeps_the_exclusion(self, monkeypatch):
        _excluded, given_back = self._run(monkeypatch, raises=True)
        assert given_back == []

    def test_a_unit_the_trash_holds_has_its_exclusion_given_back(self, monkeypatch):
        """The guard refused it: it belongs to a session still waiting in the trash."""
        _excluded, given_back = self._run(monkeypatch, status="removed", held=True)
        assert given_back == [("chat-7", ("acp-kept",))]

    def test_a_unit_mapped_again_after_the_exclusion_is_given_back(self, monkeypatch):
        _excluded, given_back = self._run(
            monkeypatch, status="removed", mapped_after_exclusion=True
        )
        assert given_back == [("chat-7", ("acp-kept",))]

    def test_a_later_slot_s_failed_exclusion_gives_back_the_earlier_one(self, monkeypatch):
        """Nothing is removed when any exclusion fails, so none of them may stand."""
        from kiro_crew.crew_log import store as crew_log_store

        given_back: list = []

        def _exclude(slot_key, units, **_k):
            if slot_key == "chat-8":
                raise session_ledger.LedgerExclusionError("unwritable")
            return session_ledger.SlotExclusion(tuple(units), False)

        monkeypatch.setattr(session_ledger, "exclude_units", _exclude)
        monkeypatch.setattr(
            session_ledger,
            "unexclude_units",
            lambda slot_key, units, **_k: given_back.append((slot_key, tuple(units))),
        )
        monkeypatch.setattr(
            crew_log_store,
            "session_units_by_slot",
            lambda **_k: {"chat-7": ("acp-a",), "chat-8": ("acp-b",)},
        )
        removed: list = []
        monkeypatch.setattr(crew_log_store, "remove_unit", lambda *a, **k: removed.append(a))
        sessions = MagicMock()
        sessions.find_key_by_sid = lambda sid, exclude="": None
        sessions_module._remove_session_crew_logs(
            sessions,
            "dashboard:chat-7",
            frozenset({"chat-7", "chat-8"}),
            frozenset({"acp-a", "acp-b"}),
            frozenset(),
        )
        assert removed == []
        assert given_back == [("chat-7", ("acp-a",))]

    @pytest.mark.parametrize("status", ["removed", "absent"])
    def test_a_unit_that_is_gone_stays_excluded(self, monkeypatch, status):
        _excluded, given_back = self._run(monkeypatch, status=status)
        assert given_back == []


class TestTheExclusionMustNotCatchASuccessorUnit:
    """The listing is by RECYCLABLE slot key, so what it returns needs re-proving."""

    def _claim(self) -> _HistoryDeleteClaim:
        return _HistoryDeleteClaim(
            registry_key="chat-1",
            slot=None,
            history_key="chat-1",
            session_key="chat-1",
            session_generation=1,
            path_match_verified=True,
            complete=True,
            acp_session_id="acp-being-deleted",
        )

    def test_a_reset_during_the_listing_refuses_instead_of_excluding(self, monkeypatch):
        """A successor's unit can enter the listing after the generation was proved.

        The successor wrote it, which means it published its reservation on the slot key
        first and so ADVANCED the generation -- a fact only a read taken AFTER the
        listing can see. Refusing leaves the row deletable again; persisting would empty
        the live successor's record for good.
        """
        state = _make_state({})
        session_ledger = MagicMock()
        session_ledger.LedgerExclusionError = _LedgerExclusionError

        def listing_with_a_reset(slot_key: str, **_kwargs):
            # The reset lands here, between the first generation read and the ids being
            # persisted: the successor is live and its unit is already on disk.
            state.sessions.session_generation.return_value = 2
            return ("acp-being-deleted", "acp-successor")

        monkeypatch.setattr("kiro_crew.crew_log.store.session_units_for_slot", listing_with_a_reset)

        with pytest.raises(_LedgerExclusionError):
            _exclude_slot_units(state, session_ledger, self._claim())

        session_ledger.exclude_units.assert_not_called()

    def test_an_unchanged_generation_still_excludes_the_conversations_units(self, monkeypatch):
        """The control: no reset, so the same listing is excluded as before."""
        state = _make_state({})
        session_ledger = MagicMock()
        session_ledger.LedgerExclusionError = _LedgerExclusionError
        monkeypatch.setattr(
            "kiro_crew.crew_log.store.session_units_for_slot",
            lambda slot_key, **_kwargs: ("acp-being-deleted", "acp-earlier"),
        )

        _exclude_slot_units(state, session_ledger, self._claim())

        session_ledger.exclude_units.assert_called_once_with(
            "chat-1", ("acp-being-deleted", "acp-earlier")
        )

    def test_an_established_unit_whose_header_will_not_read_refuses_the_delete(self, monkeypatch):
        """Left out of a short listing, the unit would stay foldable on the reused key."""
        from kiro_crew import crew_log as lg
        from kiro_crew.crew_log import store as crew_log_store

        TestEveryCrewLogOfTheConversationGoes._unit("acp-being-deleted", "chat-1")
        TestEveryCrewLogOfTheConversationGoes._unit("acp-unreadable", "chat-1")
        unreadable = crew_log_store.unit_dir_for(lg.KIND_SESSION, "acp-unreadable")
        real_proved = crew_log_store._proved_header

        def _proved(directory):
            return None if directory == unreadable else real_proved(directory)

        monkeypatch.setattr(crew_log_store, "_proved_header", _proved)
        monkeypatch.setattr(crew_log_store, "_slot_index", None)
        state = _make_state({})
        session_ledger = MagicMock()
        session_ledger.LedgerExclusionError = _LedgerExclusionError

        with pytest.raises(_LedgerExclusionError, match="could not all be listed"):
            _exclude_slot_units(state, session_ledger, self._claim())

        session_ledger.exclude_units.assert_not_called()

    def test_a_crew_log_store_never_created_excludes_nothing_and_allows_the_delete(self):
        state = _make_state({})
        session_ledger = MagicMock()
        session_ledger.LedgerExclusionError = _LedgerExclusionError

        _exclude_slot_units(state, session_ledger, self._claim())

        session_ledger.exclude_units.assert_called_once_with("chat-1", ())
