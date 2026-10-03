"""The Crewmates roster's listing rule asks the CREW LOG whether a crewmate's
DM thread holds a message.

The roster lists a row unasked when it was created on the dashboard or when its
DM thread holds a message; anything else is reached through the search box. That
second fact was decided by opening the DM transcript on every read, which makes
a transcript -- a file that is compacted, rotated and rewritten -- the record of
something the crew log already owns.

Three things have to hold for the fold to own it instead, and each is pinned
here:

1. ``RosterProjection`` carries ``has_message``, set by ``member/message`` and
   cleared only by a binding onto a new private memory generation, which is a
   new and empty thread.
2. ``GET /api/members`` reads that field, with the transcript and the live slot
   as floors rather than as the decider.
3. A member the fold has no event for -- the emit is fire-and-forget, and an
   install older than the member event log recorded nothing about threads it
   already holds -- has the transcript's answer written BACK into its log, so
   the floor is needed once per member rather than on every read.
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
from kiro_crew.dashboard.handlers.members import _has_dm_message_for_row
from kiro_crew.eventlog import types
from kiro_crew.eventlog.members_projections import RosterProjection
from kiro_crew.eventlog.service import get_service, set_service

CREW = "radar"


def _block(roster: dict | None, *, as_of_seq: int = 7) -> dict:
    return {"asOfSeq": as_of_seq, "values": {} if roster is None else {types.PROJ_ROSTER: roster}}


# ---------------------------------------------------------------------------
# The reader: the fold, floored by the transcript and the live slot
# ---------------------------------------------------------------------------
class TestHasDmMessageForRow:
    def test_the_fold_alone_lists_the_row(self) -> None:
        """No transcript answer and no live slot: the log is the authority."""
        assert _has_dm_message_for_row(_block({"has_message": True}), False, False, False) is True

    def test_the_transcript_floors_a_fold_with_no_event(self) -> None:
        # The lossy-emit case and the pre-event-log install, which is why a
        # floor exists at all.
        assert _has_dm_message_for_row(_block({"has_message": False}), True, False, False) is True

    def test_a_live_slots_unflushed_rows_floor_it_too(self) -> None:
        # A greeting appended in memory is a message before it reaches disk.
        assert _has_dm_message_for_row(_block({}), False, False, True) is True

    def test_no_source_answers_yes(self) -> None:
        assert _has_dm_message_for_row(_block({"has_message": False}), False, False, False) is False

    def test_a_member_with_no_log_falls_back_to_the_floors(self) -> None:
        """An empty ``values`` is every case the log cannot answer: no log yet,
        a slug shared by two members, a read the store will not prove."""
        assert _has_dm_message_for_row(_block(None), True, False, False) is True
        assert _has_dm_message_for_row(_block(None), False, False, False) is False
        assert _has_dm_message_for_row({}, False, False, False) is False

    @pytest.mark.parametrize("held", ["yes", 1, [0], {"a": 1}])
    def test_a_non_bool_fold_value_still_ships_a_bool(self, held) -> None:
        """A projection field is whatever the event carried, and the response is
        a network-boundary contract the client reads as a boolean."""
        out = _has_dm_message_for_row(_block({"has_message": held}), False, False, False)
        assert out is True

    @pytest.mark.parametrize("held", [None, 0, "", []])
    def test_a_falsy_fold_value_is_not_an_answer(self, held) -> None:
        assert _has_dm_message_for_row(_block({"has_message": held}), False, False, False) is False

    def test_an_unreadable_transcript_lists_the_row(self) -> None:
        """A transcript that cannot be read is no evidence either way, and
        listing the row is the safe guess: hiding a crewmate is not."""
        assert _has_dm_message_for_row(_block({"has_message": False}), False, True, False) is True
        assert _has_dm_message_for_row(_block(None), False, True, False) is True


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------
class TestTheFoldRecordsThatTheThreadHasAMessage:
    def _apply(self, state: dict, etype: str, data: dict) -> dict:
        event = {"type": etype, "seq": 1, "time": 0, "data": data}
        return RosterProjection().apply(state, event)  # type: ignore[arg-type]

    def test_a_message_sets_it(self) -> None:
        out = self._apply({}, types.MEMBER_MESSAGE, {"ts": 100.0, "preview": "hey"})
        assert out["has_message"] is True

    def test_a_machinery_row_sets_it_too(self) -> None:
        """A tool call or a patrol turn is still a row in the thread, which is
        the question the listing rule asks -- it is ``last_message`` that is
        reserved for speech."""
        out = self._apply({}, types.MEMBER_MESSAGE, {"ts": 100.0})
        assert out["has_message"] is True
        assert "last_message" not in out

    def test_a_repeat_message_emits_nothing(self) -> None:
        """Set once, so a chatted crewmate publishes this transition exactly
        once per thread rather than on every message. ``apply`` returning the
        SAME object is what the registry reads as no change."""
        held = {"has_message": True, "last_active_ts": 100.0}
        out = self._apply(held, types.MEMBER_MESSAGE, {"ts": 100.0})
        assert out is held

    def test_an_explicitly_empty_preview_does_not_set_it(self) -> None:
        """An event whose ``preview`` is present and EMPTY is not a message.

        That shape has exactly one writer: ``reconcile_member_preview``
        correcting a stale quote down to blank. ``member_message_payload``
        omits the key entirely unless the preview is non-empty, so no live row
        produces it. Reading it as a message is backwards -- it says the last
        thing SAID in this thread is nothing.

        The reachable sequence it would break: a crewmate chats, its memory
        generation rotates, the binding clears the flag over the new empty
        thread, and the roster's first read of that thread finds a stale quote
        to blank. That correction would re-set the flag and list a crewmate
        nobody has written to.
        """
        out = self._apply({"last_message": "hey"}, types.MEMBER_MESSAGE, {"ts": 5.0, "preview": ""})
        assert out.get("has_message") is not True
        # It is SILENT, not a clear: the correction speaks about speech, and a
        # thread can hold machinery rows with nothing said in it.
        held = {"has_message": True, "last_message": "hey"}
        kept = self._apply(held, types.MEMBER_MESSAGE, {"ts": 5.0, "preview": ""})
        assert kept["has_message"] is True
        # And it still does its own job.
        assert kept["last_message"] == ""

    def test_a_blank_preview_still_bumps_recency(self) -> None:
        """The correction carries the transcript's epoch, and recency is
        monotone; withholding ``has_message`` must not change that."""
        out = self._apply({"last_active_ts": 1.0}, types.MEMBER_MESSAGE, {"ts": 9.0, "preview": ""})
        assert out["last_active_ts"] == 9.0

    def test_a_new_generations_binding_clears_it(self) -> None:
        """A member's DM slot key carries its private memory generation, so a
        new generation is a new slot key over a new and empty transcript. The
        fact describes the thread the member is bound to now."""
        out = self._apply(
            {"slot_key": "dm:radar.memory-radar-mem-1", "has_message": True},
            types.MEMBER_BINDING,
            {"slot_key": "dm:radar.memory-radar-mem-2"},
        )
        assert out["slot_key"] == "dm:radar.memory-radar-mem-2"
        assert out["has_message"] is False

    def test_the_first_binding_recorded_does_not_clear_it(self) -> None:
        """The legacy fold writes ``member/binding`` from ``dm.json`` the first
        time it reads a log, which can land AFTER the live hook's own
        ``member/message`` events. That binding names the very thread those
        messages are in, so reading it as a change would discard the fact.
        """
        out = self._apply(
            {"has_message": True, "last_active_ts": 100.0},
            types.MEMBER_BINDING,
            {"slot_key": "dm:radar"},
        )
        assert out["slot_key"] == "dm:radar"
        assert out["has_message"] is True

    def test_a_repeat_binding_emits_nothing(self) -> None:
        held = {"slot_key": "dm:radar", "has_message": True}
        out = self._apply(held, types.MEMBER_BINDING, {"slot_key": "dm:radar"})
        assert out is held

    def test_the_state_version_retires_a_savepoint_from_before_the_field(self) -> None:
        """``projection/checkpoint.py`` refuses a payload whose ``state_version``
        does not match the definition's, and that is the only thing standing
        between an upgraded gateway and a member whose ``member/message`` events
        all sit below a savepoint's watermark. A resumed savepoint applies only
        the events AFTER it, so such a member would answer "no message" -- and
        the listing rule would leave a chatted crewmate off the list -- until it
        was next written to.
        """
        assert RosterProjection.state_version >= 3


# ---------------------------------------------------------------------------
# GET /api/members
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
        agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew", source="radar-app")},
        default_agent="kirocrew",
        memory_stores={},
        degraded_sections=frozenset(),
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
    return cfg


async def _roster_row(state) -> dict:
    async with TestClient(TestServer(_members_app(state))) as client:
        body = await (await client.get("/api/members")).json()
    return next(r for r in body["members"] if r["name"] == CREW)


def _bind(state, slug: str) -> None:
    members.write_dm_binding(slug, member=CREW, slot_key=f"member-{slug}")


class TestTheRosterReadsTheFold:
    @pytest.mark.asyncio
    async def test_the_fold_lists_a_row_whose_transcript_is_gone(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The reason the fold owns this fact: a transcript is compacted,
        rotated and rewritten, and the crew log is not. A member whose log
        records a message is listed with no transcript on disk at all."""
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        _bind(state, slug)

        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 4_000_000_000.0, "preview": "hey"})

        assert (
            state.conversation_log.has_messages(members.member_thread_session_alias(slug)) is False
        ), "precondition: nothing on disk to read"

        row = await _roster_row(state)
        assert row["has_dm_message"] is True
        assert row["projections"]["values"]["roster"]["has_message"] is True

    @pytest.mark.asyncio
    async def test_a_transcript_only_member_is_recorded_in_its_log(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The lossy-emit and old-install case, fixed in the RECORD.

        ``_record_member_row`` hands its append to the ordered executor and
        never reads the result, so the fold can miss a message the transcript
        holds. The roster asks the transcript for exactly those members and
        writes the answer back, after which the log answers on its own.

        The thread here holds a say-nothing reply -- a bare zero-width space, a
        row the chat draws nothing for. It is a message by the listing rule's
        own test and is NOT speech, so the preview correction has nothing to
        say about it and this is the only writer that records it.
        """
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        _bind(state, slug)
        state.conversation_log.append(
            members.member_thread_session_alias(slug), "assistant", "\u200b"
        )
        svc = get_service()
        svc.ensure(slug, CREW)
        assert not any(
            e["type"] == types.MEMBER_MESSAGE for e in svc.history(slug, limit=100)
        ), "precondition: the log records no message"

        row = await _roster_row(state)
        assert row["has_dm_message"] is True
        assert row["last_message"] == "", "precondition: nothing was said, so nothing is quoted"
        # The correction landed in the log, not merely in the response, and it
        # carries the machinery-row shape: a recency and no quote.
        assert svc.snapshot(slug)["values"][types.PROJ_ROSTER]["has_message"] is True
        assert [
            e["data"] for e in svc.history(slug, limit=100) if e["type"] == types.MEMBER_MESSAGE
        ] == [{"ts": row["last_active_ts"]}]

    @pytest.mark.asyncio
    async def test_a_spoken_transcript_is_recorded_once(self, tmp_path: Path, monkeypatch) -> None:
        """Two corrections read the same transcript on the same request, and
        the preview's own ``member/message`` already carries the fact.

        Its append is re-asked under the write lock against the CURRENT fold,
        so the second correction finds the fact recorded and writes nothing.
        Without that the roster would append a redundant event -- and publish a
        projection frame for it -- on the first read of every member whose
        quote had drifted.
        """
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        _bind(state, slug)
        state.conversation_log.append(
            members.member_thread_session_alias(slug), "user", "Take over the crew work."
        )
        svc = get_service()
        svc.ensure(slug, CREW)

        row = await _roster_row(state)
        assert row["has_dm_message"] is True
        assert svc.snapshot(slug)["values"][types.PROJ_ROSTER]["has_message"] is True
        recorded = [
            e["data"] for e in svc.history(slug, limit=100) if e["type"] == types.MEMBER_MESSAGE
        ]
        assert recorded == [{"ts": row["last_active_ts"], "preview": "Take over the crew work."}]

    @pytest.mark.asyncio
    async def test_the_answer_is_written_once_however_often_it_is_read(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The correction converges: the log gains ONE event, not one per poll.

        This is what makes it a migration rather than a second record. The
        transcript is still read every poll -- it is this row's floor, and the
        same file is opened for the preview anyway -- but once the fold holds
        the fact, ``reconcile_member_has_message`` returns before appending.
        """
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        _bind(state, slug)
        state.conversation_log.append(
            members.member_thread_session_alias(slug), "assistant", "\u200b"
        )
        svc = get_service()
        svc.ensure(slug, CREW)

        first = await _roster_row(state)
        assert first["has_dm_message"] is True
        after_one = [
            e["data"] for e in svc.history(slug, limit=100) if e["type"] == types.MEMBER_MESSAGE
        ]
        assert after_one == [{"ts": first["last_active_ts"]}]

        for _ in range(3):
            row = await _roster_row(state)
            assert row["has_dm_message"] is True
        assert [
            e["data"] for e in svc.history(slug, limit=100) if e["type"] == types.MEMBER_MESSAGE
        ] == after_one, "the fold already held it, so nothing more was appended"

    @pytest.mark.asyncio
    async def test_an_opened_but_silent_thread_is_still_not_listed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A thread opened with nothing said holds a metadata line and no row.
        Nothing is written to the log for it either, so the probe stays open
        and the next read asks again."""
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        _bind(state, slug)
        state.conversation_log.set_title(members.member_thread_session_alias(slug), "Opened")
        svc = get_service()
        svc.ensure(slug, CREW)

        row = await _roster_row(state)
        assert row["has_dm_message"] is False
        assert not any(e["type"] == types.MEMBER_MESSAGE for e in svc.history(slug, limit=100))

    @pytest.mark.asyncio
    async def test_a_rotated_generation_is_not_listed_by_the_preview_correction(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A crewmate whose memory generation rotated stays off the list until
        somebody writes to the NEW thread.

        The whole sequence, which is what makes the fold's blank-preview rule
        load-bearing: the old thread held a message and a quote, the binding
        onto the new generation cleared the flag but not the quote, and the new
        thread's transcript holds only a metadata line. The roster's first read
        of it finds a stale quote and corrects it to blank -- and that
        correction is a ``member/message``. Counting it as a message would list
        a crewmate nobody has written to since the rotation.
        """
        store = "radar-mem-2"
        cfg = SimpleNamespace(
            agents={CREW: KiroCrewAgentConfig(kiro_agent="kirocrew", source="radar-app")},
            default_agent="kirocrew",
            memory_stores={},
            degraded_sections=frozenset(),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        new_key = members.member_slot_key(slug, store)
        members.write_dm_binding(slug, member=CREW, slot_key=new_key, memory_store=store)

        svc = get_service()
        svc.ensure(slug, CREW)
        # The old generation: a message, and a quote the fold still holds.
        svc.append(slug, types.MEMBER_BINDING, {"slot_key": members.member_slot_key(slug)})
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 100.0, "preview": "hey"})
        # The rotation.
        svc.append(slug, types.MEMBER_BINDING, {"slot_key": new_key})
        roster = svc.snapshot(slug)["values"][types.PROJ_ROSTER]
        assert roster["has_message"] is False, "precondition: the binding cleared it"
        assert roster["last_message"] == "hey", "precondition: the quote is stale"

        # The new thread was opened and nothing was said in it.
        state.conversation_log.set_title(members.member_thread_session_alias(slug, store), "Opened")

        row = await _roster_row(state)
        assert row["has_dm_message"] is False
        # The correction DID run -- the stale quote is gone -- and it still did
        # not make the thread look written to.
        assert svc.snapshot(slug)["values"][types.PROJ_ROSTER]["last_message"] == ""
        assert svc.snapshot(slug)["values"][types.PROJ_ROSTER]["has_message"] is False

    @pytest.mark.asyncio
    async def test_a_failed_projection_reread_does_not_unlist_a_chatted_crewmate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A failed read of the member log must not unlist a chatted crewmate.

        This is why the transcript is asked for every bound row rather than
        only for the rows the fold cannot answer: the projection read is a
        separate open of the same log, several awaits later, and
        ``_project_rows`` answers a failure with the refusal sentinel and an
        empty ``values``. The transcript is then the row's only source.
        """
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        _bind(state, slug)
        state.conversation_log.append(
            members.member_thread_session_alias(slug), "user", "Take over the crew work."
        )
        svc = get_service()
        svc.ensure(slug, CREW)

        real = svc.snapshot
        calls = {"n": 0}

        def _failing(target_slug: str):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("member log unreadable")
            return real(target_slug)

        monkeypatch.setattr(svc, "snapshot", _failing)

        row = await _roster_row(state)
        assert row["projections"]["values"] == {}, "precondition: the re-read was refused"
        assert calls["n"] > 1, "precondition: a later snapshot was attempted"
        assert row["has_dm_message"] is True

    @pytest.mark.asyncio
    async def test_an_unreadable_transcript_is_not_written_into_the_log(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A fail-open guess lists the row and is never made a record.

        ``has_messages`` raising is no evidence that the thread holds a
        message, and ``has_message`` never falls back to false, so appending
        one would turn a single failed read into a permanent claim.
        """
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        _bind(state, slug)
        svc = get_service()
        svc.ensure(slug, CREW)

        def _unreadable(key: str) -> bool:
            raise OSError("transcript unreadable")

        monkeypatch.setattr(state.conversation_log, "has_messages", _unreadable)

        row = await _roster_row(state)
        assert row["has_dm_message"] is True
        assert not any(e["type"] == types.MEMBER_MESSAGE for e in svc.history(slug, limit=100))
        assert svc.snapshot(slug)["values"][types.PROJ_ROSTER].get("has_message") is not True

    @pytest.mark.asyncio
    async def test_a_collided_slug_still_lists_from_its_transcript(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Two crew names folding onto one slug are served an EMPTY projection,
        so neither row may skip its transcript question on the strength of that
        shared log's fold -- the answer it holds is not attributable to either.

        The transcript holds nothing here, which is what makes this
        discriminating: a stranger's fold saying yes must not list a row whose
        own thread is empty. The sibling case below covers the transcript
        answering yes.
        """
        cfg = SimpleNamespace(
            agents={
                "Radar_Agent": KiroCrewAgentConfig(kiro_agent="kirocrew", source="radar-app"),
                "radar-agent": KiroCrewAgentConfig(kiro_agent="kirocrew", source="radar-app"),
            },
            default_agent="kirocrew",
            memory_stores={},
            degraded_sections=frozenset(),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name("Radar_Agent")
        assert slug == members.slug_for_name("radar-agent"), "precondition: the slugs collide"
        members.write_dm_binding(slug, member="Radar_Agent", slot_key=f"member-{slug}")
        state.conversation_log.set_title(members.member_thread_session_alias(slug), "Opened")
        svc = get_service()
        svc.ensure(slug, "Radar_Agent")
        # The shared log holds the fact, and the roster will not attribute it.
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 500.0})

        async with TestClient(TestServer(_members_app(state))) as client:
            body = await (await client.get("/api/members")).json()
        rows = {r["name"]: r for r in body["members"]}
        assert rows["Radar_Agent"]["projections"]["values"] == {}, "precondition: not attributed"
        assert rows["Radar_Agent"]["has_dm_message"] is False

    @pytest.mark.asyncio
    async def test_a_collided_slug_with_its_own_message_is_listed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The healthy side: a collided row whose own transcript holds a
        message is still listed, from that transcript."""
        cfg = SimpleNamespace(
            agents={
                "Radar_Agent": KiroCrewAgentConfig(kiro_agent="kirocrew", source="radar-app"),
                "radar-agent": KiroCrewAgentConfig(kiro_agent="kirocrew", source="radar-app"),
            },
            default_agent="kirocrew",
            memory_stores={},
            degraded_sections=frozenset(),
        )
        monkeypatch.setattr("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", lambda: cfg)
        state = _make_state(tmp_path)
        slug = members.slug_for_name("Radar_Agent")
        members.write_dm_binding(slug, member="Radar_Agent", slot_key=f"member-{slug}")
        state.conversation_log.append(
            members.member_thread_session_alias(slug), "user", "Take over the crew work."
        )
        get_service().ensure(slug, "Radar_Agent")

        async with TestClient(TestServer(_members_app(state))) as client:
            body = await (await client.get("/api/members")).json()
        rows = {r["name"]: r for r in body["members"]}
        assert rows["Radar_Agent"]["projections"]["values"] == {}, "precondition: not attributed"
        assert rows["Radar_Agent"]["has_dm_message"] is True

    @pytest.mark.asyncio
    async def test_a_log_whose_header_names_another_member_does_not_answer(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A log whose header names a third member is served an empty
        projection too, so its fold cannot answer for this row either.

        Its transcript is empty, so a foreign fold saying yes is the only thing
        that could list the row -- and must not.
        """
        _cfg(monkeypatch)
        state = _make_state(tmp_path)
        slug = members.slug_for_name(CREW)
        _bind(state, slug)
        state.conversation_log.set_title(members.member_thread_session_alias(slug), "Opened")
        svc = get_service()
        # The header is written once, while the log is fresh, and it names
        # somebody this row is not.
        svc.ensure(slug, "someone-else")
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 500.0})

        row = await _roster_row(state)
        assert row["projections"]["values"] == {}, "precondition: not attributed"
        assert row["has_dm_message"] is False


# ---------------------------------------------------------------------------
# The write guard
# ---------------------------------------------------------------------------
class TestTheCorrectionIsCompareAndAppend:
    """``reconcile_member_has_message`` writes through
    ``append_closer_if_still_applies`` with
    ``_has_message_is_still_unrecorded_at``, re-asked under the per-slug write
    lock. Two things can land in the window, and they refuse for different
    reasons: a live ``member/message`` records the fact better, and a
    ``member/binding`` means the thread the caller read is not the thread the
    member is on any more.
    """

    def test_the_predicate_refuses_a_recorded_fact_and_a_moved_thread(self) -> None:
        from kiro_crew.eventlog_hooks import _has_message_is_still_unrecorded_at

        then = {types.PROJ_ROSTER: {"slot_key": "dm:radar", "has_message": False}}
        assert _has_message_is_still_unrecorded_at(dict(then), then)
        # An unrelated field moving must not starve it.
        moved_elsewhere = {
            types.PROJ_ROSTER: {"slot_key": "dm:radar", "has_message": False, "model": "m2"}
        }
        assert _has_message_is_still_unrecorded_at(moved_elsewhere, then)
        # A live message already recorded it.
        assert not _has_message_is_still_unrecorded_at(
            {types.PROJ_ROSTER: {"slot_key": "dm:radar", "has_message": True}}, then
        )
        # A binding rotated the member onto another thread.
        assert not _has_message_is_still_unrecorded_at(
            {types.PROJ_ROSTER: {"slot_key": "dm:radar.memory-gen2", "has_message": False}}, then
        )

    def test_a_binding_in_the_window_refuses_the_correction(self, tmp_path, monkeypatch) -> None:
        """The guard's binding half, end to end.

        The caller decides from a roster it observed, and a generation rotation
        lands before the write. Appending then would stamp the new, empty
        thread with evidence read off the old one -- and because the log is
        append-only and ``has_message`` never falls back to false on its own,
        that crewmate would be listed for good.
        """
        from kiro_crew.eventlog_hooks import reconcile_member_has_message

        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_BINDING, {"slot_key": members.member_slot_key(slug)})
        observed = dict(svc.snapshot(slug)["values"][types.PROJ_ROSTER])
        assert observed["slot_key"] == members.member_slot_key(slug)
        assert not observed.get("has_message")

        # The rotation lands between the caller's observation and the write.
        rotated = members.member_slot_key(slug, "radar-mem-2")
        svc.append(slug, types.MEMBER_BINDING, {"slot_key": rotated})

        assert reconcile_member_has_message(slug, CREW, 500.0, observed) is False
        roster = svc.snapshot(slug)["values"][types.PROJ_ROSTER]
        assert roster.get("has_message") is not True
        assert not any(e["type"] == types.MEMBER_MESSAGE for e in svc.history(slug, limit=100))

    def test_it_applies_when_the_thread_did_not_move(self, tmp_path, monkeypatch) -> None:
        """The healthy side of the same fence: without it, a pass on the
        refused side alone would also pass a guard that refuses everything."""
        from kiro_crew.eventlog_hooks import reconcile_member_has_message

        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        svc.append(slug, types.MEMBER_BINDING, {"slot_key": members.member_slot_key(slug)})
        observed = dict(svc.snapshot(slug)["values"][types.PROJ_ROSTER])

        assert reconcile_member_has_message(slug, CREW, 500.0, observed) is True
        assert svc.snapshot(slug)["values"][types.PROJ_ROSTER]["has_message"] is True
        assert [
            e["data"] for e in svc.history(slug, limit=100) if e["type"] == types.MEMBER_MESSAGE
        ] == [{"ts": 500.0}]

    def test_a_live_message_in_the_window_refuses_the_correction(self, tmp_path) -> None:
        from kiro_crew.eventlog_hooks import reconcile_member_has_message

        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        observed = {"slot_key": members.member_slot_key(slug), "has_message": False}
        # The crewmate speaks before the correction lands.
        svc.append(slug, types.MEMBER_MESSAGE, {"ts": 900.0, "preview": "hey"})

        assert reconcile_member_has_message(slug, CREW, 500.0, observed) is False
        assert [
            e["data"] for e in svc.history(slug, limit=100) if e["type"] == types.MEMBER_MESSAGE
        ] == [{"ts": 900.0, "preview": "hey"}]


class TestCorrectionsAreFencedOnTheThreadTheyRead:
    """Both roster corrections carry evidence out of ONE transcript, opened at
    the key the bindings read at the top of the request. A ``member/binding``
    landing after that read rotates the member onto a new, EMPTY thread, and
    the caller's own observation cannot see it: the observation is taken AFTER
    the bindings, so the rotation is already in it and the observed key matches
    the current key. Only the key the transcript was read at separates them.
    """

    def test_the_fence_refuses_a_fold_naming_another_thread(self) -> None:
        from kiro_crew.eventlog_hooks import _fold_still_names_the_thread

        rotated = {types.PROJ_ROSTER: {"slot_key": "dm:radar.memory-gen2"}}
        assert not _fold_still_names_the_thread(rotated, "dm:radar")
        assert _fold_still_names_the_thread(rotated, "dm:radar.memory-gen2")
        # A log with no binding event yet contradicts nothing.
        assert _fold_still_names_the_thread({types.PROJ_ROSTER: {}}, "dm:radar")
        assert _fold_still_names_the_thread({}, "dm:radar")
        # No key to vouch with: the predicate claims nothing about the thread.
        assert _fold_still_names_the_thread(rotated, "")

    def test_a_rotation_after_the_bindings_read_refuses_the_message_correction(self) -> None:
        """GPT's reachable race, in projection terms.

        The observation and the current fold BOTH name the new thread, so the
        pre-existing observation check passes. The transcript evidence is from
        the old one, and without the key fence it would durably set
        ``has_message`` on a thread nobody has written in.
        """
        from kiro_crew.eventlog_hooks import _has_message_is_still_unrecorded_at

        rotated = {types.PROJ_ROSTER: {"slot_key": "dm:radar.memory-gen2", "has_message": False}}
        observed = {types.PROJ_ROSTER: {"slot_key": "dm:radar.memory-gen2", "has_message": False}}
        assert _has_message_is_still_unrecorded_at(rotated, observed, "dm:radar.memory-gen2")
        assert not _has_message_is_still_unrecorded_at(rotated, observed, "dm:radar")

    def test_a_rotation_after_the_bindings_read_refuses_the_preview_correction(self) -> None:
        """The same transcript, the same window, the same key: a quote read off
        the old thread must not be written onto the new one either."""
        from kiro_crew.eventlog_hooks import _preview_is_still_at

        rotated = {types.PROJ_ROSTER: {"slot_key": "dm:radar.memory-gen2", "last_message": "hey"}}
        observed = {types.PROJ_ROSTER: {"slot_key": "dm:radar.memory-gen2", "last_message": "hey"}}
        assert _preview_is_still_at(rotated, observed, "dm:radar.memory-gen2")
        assert not _preview_is_still_at(rotated, observed, "dm:radar")
        # Unkeyed callers keep the behaviour they had before the fence.
        assert _preview_is_still_at(rotated, observed)

    def test_the_message_correction_refuses_end_to_end(self, tmp_path) -> None:
        from kiro_crew.eventlog_hooks import reconcile_member_has_message

        slug = members.slug_for_name(CREW)
        svc = get_service()
        svc.ensure(slug, CREW)
        old_key = members.member_slot_key(slug)
        svc.append(slug, types.MEMBER_BINDING, {"slot_key": old_key})
        # The rotation lands before the roster observes, so the observation
        # already names the new thread -- the case the old check could not see.
        rotated = members.member_slot_key(slug, "radar-mem-2")
        svc.append(slug, types.MEMBER_BINDING, {"slot_key": rotated})
        observed = dict(svc.snapshot(slug)["values"][types.PROJ_ROSTER])
        assert observed["slot_key"] == rotated

        assert reconcile_member_has_message(slug, CREW, 500.0, observed, slot_key=old_key) is False
        assert not any(e["type"] == types.MEMBER_MESSAGE for e in svc.history(slug, limit=100))
        # And it still applies for evidence read at the thread the fold names.
        assert reconcile_member_has_message(slug, CREW, 500.0, observed, slot_key=rotated) is True
        assert svc.snapshot(slug)["values"][types.PROJ_ROSTER]["has_message"] is True
