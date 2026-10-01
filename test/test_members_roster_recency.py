"""The Crewmates roster's Recent sort is ordered by the CREW LOG, and the user's
own send is one of the events it folds.

Sorted by Recent, typing into a crewmate's DM left its row in the middle of the
list. Three things had to be true at once for that, and each is pinned here:

1. ``Slot.append`` emitted ``member/message`` through the SSE broadcast hook,
   which it deliberately skips for a ``user`` row (the composer already drew its
   own bubble) and for any row on a slot with an HTTP stream reader. So the one
   action the user most expects to reorder the roster recorded nothing.
2. ``GET /api/members`` took ``last_active_ts`` from the DM transcript's last
   SPEECH row rather than from the folded ``roster`` projection, so a machinery
   turn and an unflushed send were both invisible to the ordering.
3. ``MembersPage`` merged the pushed projection's ``last_active_ts`` only when
   the row carried none, so a live frame could never move a row. That half is
   pinned in ``website/src/pages/members/MembersPage.test.tsx``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import members
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers.members import _as_epoch, _recency_for_row
from kiro_crew.eventlog import types
from kiro_crew.eventlog.service import get_service, set_service

CREW = "radar"


def _block(last_active_ts, *, as_of_seq: int = 7) -> dict:
    return {
        "asOfSeq": as_of_seq,
        "values": {types.PROJ_ROSTER: {"last_active_ts": last_active_ts}},
    }


class TestRecencyForRow:
    """The crew log is the authority; the transcript is its floor."""

    def test_the_fold_wins_over_an_older_transcript(self) -> None:
        # The case the bug was: the user just sent, so the log is ahead of the
        # transcript (the row has not been flushed to disk yet).
        assert _recency_for_row(_block(500.0), 100.0) == 500.0

    def test_the_transcript_floors_a_lagging_fold(self) -> None:
        # These events ride a best-effort hook a queue ceiling may drop, so the
        # transcript must never be overridden DOWNWARD by a fold behind it.
        assert _recency_for_row(_block(100.0), 500.0) == 500.0

    def test_no_log_falls_back_to_the_transcript(self) -> None:
        # An empty ``values`` is every case where the log cannot answer: no log
        # yet, a slug shared by two members, a read the store will not prove.
        assert _recency_for_row({"asOfSeq": 0, "values": {}}, 420.0) == 420.0
        assert _recency_for_row({}, 420.0) == 420.0

    def test_neither_side_answers(self) -> None:
        assert _recency_for_row({"asOfSeq": 0, "values": {}}, 0.0) == 0.0

    @pytest.mark.parametrize("bad", [None, "2026-09-30T02:22:00Z", True, False, -5, float("nan")])
    def test_a_non_epoch_fold_is_not_an_ordering_key(self, bad) -> None:
        """A projection field is whatever the event carried.

        ``member/message`` takes ``ts`` as is, so the fold can hold ``None`` (an
        event with no ``ts``), a string, or a bool. None of those may rank a
        member, and ``True`` in particular must not read as one second after the
        epoch -- ``bool`` is an ``int`` subclass, so a bare numeric check admits
        it. The transcript's answer stands instead.
        """
        assert _recency_for_row(_block(bad), 300.0) == 300.0

    def test_epoch_coercion(self) -> None:
        assert _as_epoch(12.5) == 12.5
        assert _as_epoch(0) == 0.0
        assert _as_epoch(float("inf")) == 0.0


class TestTheFoldNeverWalksRecencyBackwards:
    """``RosterProjection`` is monotone in ``last_active_ts``.

    ``reconcile_member_preview`` corrects a stale quote by appending a
    ``member/message`` carrying the TRANSCRIPT's epoch -- the last thing SAID,
    and therefore older than any machinery turn since. Under a last-wins fold
    that correction reset recency to the last speech on EVERY roster read, which
    is what made reading the fold in the handler insufficient on its own.
    """

    def _apply(self, state: dict, data: dict) -> dict:
        from kiro_crew.eventlog.members_projections import RosterProjection

        event = {"type": types.MEMBER_MESSAGE, "seq": 1, "time": 0, "data": data}
        return RosterProjection().apply(state, event)  # type: ignore[arg-type]

    def test_a_preview_correction_keeps_the_newer_recency(self) -> None:
        out = self._apply(
            {"last_active_ts": 4_000_000_000.0, "last_message": "\U0001f527 gh issue list"},
            {"ts": 100.0, "preview": "Triaged 7 new issues."},
        )
        assert out["last_active_ts"] == 4_000_000_000.0
        assert out["last_message"] == "Triaged 7 new issues."

    def test_a_newer_event_still_advances_it(self) -> None:
        out = self._apply({"last_active_ts": 100.0}, {"ts": 200.0})
        assert out["last_active_ts"] == 200.0

    def test_an_event_with_no_ts_leaves_it_alone(self) -> None:
        out = self._apply({"last_active_ts": 100.0}, {"preview": "hello"})
        assert out["last_active_ts"] == 100.0

    def test_an_iso_ts_is_normalised_to_epoch_seconds(self) -> None:
        """The roster row and the client both read this as a NUMBER."""
        out = self._apply({}, {"ts": "2026-09-30T02:22:00Z"})
        assert isinstance(out["last_active_ts"], float)
        assert out["last_active_ts"] > 0

    def test_the_state_version_retires_a_pre_monotone_savepoint(self) -> None:
        """A savepoint from a fold without the monotone rule must be DISCARDED.

        ``projection/checkpoint.py`` refuses a payload whose ``state_version``
        does not match the definition's, and that is the only thing standing
        between an upgraded gateway and a recency some earlier preview
        correction walked backwards: a resumed savepoint applies only the events
        AFTER it, so the regressed value would stand until the member next
        spoke. The fold's own rule is pinned above; this pins the retirement,
        without which those pins hold only for a store that has never saved.
        """
        from kiro_crew.eventlog.members_projections import RosterProjection

        assert RosterProjection.state_version >= 2


# ---------------------------------------------------------------------------
# GET /api/members ships the folded recency
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fresh_eventlog(tmp_path, monkeypatch):
    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    set_service(None)
    yield
    set_service(None)


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


def _cfg(monkeypatch):
    cfg = SimpleNamespace(
        agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew")},
        default_agent="kirocrew",
        memory_stores={},
        degraded_sections=frozenset(),
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
    return cfg


class TestRosterReadTakesTheFoldedRecency:
    @pytest.mark.asyncio
    async def test_a_machinery_turn_newer_than_the_last_speech_orders_the_row(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The repro in projection terms.

        The transcript's newest SPEECH is old; the crew log holds a far newer
        ``member/message``. Ordering by the transcript put this member below
        crewmates that had spoken longer ago than it was last active.
        """
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        members.write_dm_binding(slug, member=CREW, slot_key=f"member-{slug}")
        key = members.member_thread_session_alias(slug)
        state.conversation_log.append(key, "assistant", "Triaged 7 new issues.")

        svc = get_service()
        svc.ensure(slug, CREW)
        # A machinery row: recency only, no preview -- exactly what the roster
        # must order by while still quoting the last thing said.
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 4_000_000_000.0})

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["name"] == CREW)
        assert row["last_active_ts"] == 4_000_000_000.0
        # The preview still comes from the transcript's speech-only read.
        assert row["last_message"] == "Triaged 7 new issues."
        # The row and the projection block it ships agree, so the client's
        # higher-seq-wins merge has one value to reconcile rather than two.
        assert row["projections"]["values"]["roster"]["last_active_ts"] == 4_000_000_000.0

    @pytest.mark.asyncio
    async def test_a_member_with_no_log_keeps_the_transcript_reading(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A freshly bound crewmate has a transcript before it has a log."""
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        members.write_dm_binding(slug, member=CREW, slot_key=f"member-{slug}")
        key = members.member_thread_session_alias(slug)
        state.conversation_log.append(key, "user", "Take over the crew work.")

        app = _members_app(state)
        async with TestClient(TestServer(app)) as client:
            data = await (await client.get("/api/members")).json()
        row = next(r for r in data["members"] if r["name"] == CREW)
        assert row["last_active_ts"] > 0


# ---------------------------------------------------------------------------
# Slot.append records every live row, including the user's own
# ---------------------------------------------------------------------------
class TestAppendRecordsTheRow:
    """``Slot._on_row`` fires for the rows ``_on_message`` is skipped for.

    The recorded hook is asserted through the SLOT, not through the event log:
    the emit itself is queued on the ordered executor (a worker thread doing an
    fsync), so pinning the append here would pin the queue's timing rather than
    the decision under test -- which is WHICH rows reach the recorder at all.
    """

    def _slot(self, tmp_path: Path):
        # An ordinary slot: WHICH rows reach the recorder is decided in
        # ``Slot.append`` for every slot alike, and whether a row is a member's
        # is decided inside the recorder (``member_slug_for_slot``). Creating a
        # `member-` key here is refused on purpose -- those are minted only by
        # the member thread endpoint -- and mattering to this test would mean the
        # gate had been written in the wrong place.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(name="chat-recency-probe")
        seen: list[tuple[str, str]] = []
        slot._on_row = lambda key, msg: seen.append((key, msg.get("role", "")))
        return slot, seen

    def test_a_user_row_from_the_composer_is_recorded(self, tmp_path: Path) -> None:
        """``broadcast_user`` defaults False, so the SSE hook is skipped: a
        recorder reachable only through it never sees a message a person typed."""
        slot, seen = self._slot(tmp_path)
        slot.append("user", "Take over the crew work.", "msg msg-u")
        assert [role for _, role in seen] == ["user"]

    def test_an_assistant_row_is_recorded_even_with_a_stream_reader(self, tmp_path: Path) -> None:
        """``_has_reader`` suppresses the SSE frame because the HTTP stream is
        delivering it; it says nothing about whether the row happened."""
        slot, seen = self._slot(tmp_path)
        slot._has_reader_flag = True
        slot.append("assistant", "On it.", "msg msg-a")
        assert [role for _, role in seen] == ["assistant"]

    def test_a_replayed_row_is_not_recorded(self, tmp_path: Path) -> None:
        """``broadcast=False`` is a REPLAY (transcript rotation recovery, fork,
        session transfer). Recording it would stamp a member as active just now
        for a message sent hours ago and reorder the roster on a recovery."""
        slot, seen = self._slot(tmp_path)
        slot.append("user", "an old row", "msg msg-u", broadcast=False)
        slot.append("assistant", "an old reply", "msg msg-a", broadcast=False)
        assert seen == []

    def test_wire_only_roles_are_not_recorded(self, tmp_path: Path) -> None:
        """``chunk`` is one streamed token; ``done`` and ``streaming`` are
        markers. None is persisted, so none is a row that happened."""
        slot, seen = self._slot(tmp_path)
        for role in ("chunk", "done", "streaming"):
            slot.append(role, "x")
        assert seen == []

    def test_a_failing_recorder_does_not_break_the_send(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(name="chat-recency-probe")

        def _boom(key, msg):
            raise RuntimeError("log unavailable")

        slot._on_row = _boom
        row = slot.append("user", "still delivered", "msg msg-u")
        assert row["content"] == "still delivered"
        assert slot.messages[-1]["content"] == "still delivered"
