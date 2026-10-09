"""The durable boundary a "New conversation" leaves behind.

A reset discards the native conversation and deliberately deletes nothing: the
slot stays open, its key is unchanged, and its transcript stays on disk. That is
the honest record of what happened -- and it is exactly why the boundary has to
be written down somewhere. Nothing in the slot's own files says where the
discarded conversation ends and the fresh one begins, so a pane reading the
transcript alone draws messages the model does not remember as current
context.

Four things carry that boundary, and each fails quietly rather than loudly:

**The member log's ``slot/reset``.** The one durable statement of where the
current conversation starts. Written only on a PERFORMED discard -- an entry
written beside a refusal would move a boundary on a conversation that is still
whole -- and AWAITED, with its outcome in the response: a reset acknowledged
with no boundary recorded leaves forgotten messages rendering as current with
nothing on screen saying so.

**The roster fold.** The boundary reaches the pane through the projection the
roster read already serves, so it survives a reload and a gateway restart. A
fold that trusted its payload would let a damaged line hide an arbitrary prefix
of a live conversation.

**The discarded session's ``session/closed {discarded}``.** Nothing wrote this
reason before, so a discarded conversation's log ended mid-sentence and read as
open forever.

**That reason being NON-terminal.** A terminal close authorizes DELETING the
unit, and this is the one teardown whose contract is that the record survives
it -- the transcript is still on screen behind "Show earlier messages".
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from off_loop_helpers import off_loop

from kiro_crew import crew_log as lg
from kiro_crew import eventlog_hooks
from kiro_crew.crew_log import CrewLog, store
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.dashboard.chat_handlers import api_chat_slot_reset_conversation
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.eventlog import types
from kiro_crew.eventlog.members_projections import RosterProjection
from kiro_crew.eventlog.service import get_service

DAY_MS = 86_400_000
#: A real member DM slot key: the owner is resolved from this shape alone.
DM_SLOT = "member-alice"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Every test writes into its own data home, never the live one.

    Through ``_floor_monkeypatch``, not the shared ``monkeypatch``: an autouse
    patch on the shared one is lifted by any test in the run that calls
    ``monkeypatch.undo()``, and this one is what keeps the suite off the real
    data home. The floor fixture is undone independently.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


# --------------------------------------------------------------------------- #
# the member log's slot/reset
# --------------------------------------------------------------------------- #
def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler):
        request["app"] = ""  # a dashboard user, not an app token
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post(
        "/api/chat/slots/{slot}/reset-conversation", api_chat_slot_reset_conversation
    )
    return app


def _state(slot: _ChatSlot, *, performed: bool = True) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.discard_conversation = AsyncMock(return_value=performed)
    # No live provider: a slot restored from history, which is what the busy
    # probe has nothing to ask.
    state.sessions.get_provider = MagicMock(return_value=None)
    state.subagents = None
    return state


def _dm_slot(*, running: bool = False) -> _ChatSlot:
    slot = _ChatSlot(DM_SLOT)
    slot._app = ""
    if running:
        slot.task = MagicMock(done=MagicMock(return_value=False))
    return slot


async def _post(state: DashboardState, slot: str):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{slot}/reset-conversation", json={"replay": False}
        )
        return resp.status, await resp.json()


def _member_events(slug: str = "alice") -> list[dict]:
    """Every event in *slug*'s member log, after the queued appends have landed."""
    assert eventlog_hooks.drain_for_shutdown(10.0)
    path = store.crew_log_path(KIND_MEMBER, slug)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != types.HEADER_TYPE:
            out.append(entry)
    return out


def _resets(slug: str = "alice") -> list[dict]:
    return [e for e in _member_events(slug) if e.get("type") == types.SLOT_RESET]


@pytest.mark.asyncio
async def test_a_performed_reset_records_the_boundary_on_the_members_log():
    """The entry a pane reads to know where the current conversation starts.

    The slot key and the envelope's own ``time`` are the whole payload: the
    boundary is a MOMENT, and that is what the pane applies. A row count and the
    discarded session id were written here at first and had no reader -- the
    client holds a bounded tail of the transcript, so an absolute row index
    cannot be applied to it, and the session chain is already in the successor's
    own opening entry.
    """
    slot = _dm_slot()
    state = _state(slot)

    status, body = await _post(state, DM_SLOT)

    assert status == 200
    assert body["reset"] is True
    (entry,) = _resets()
    assert entry["data"]["slot_key"] == DM_SLOT
    # The instant is the DISCARD's, carried in the payload, not this append's.
    assert entry["data"]["ts"] > 0


@pytest.mark.asyncio
async def test_the_response_waits_for_the_boundary_and_names_its_outcome():
    """``persist-before-you-publish``: the acknowledgement carries the record.

    The teardown cannot be undone by a failed append, so the answer is 200
    naming what did and did not happen rather than a 500 inviting a caller to
    retry a reset that already ran. What must never happen is a clean-looking
    200 over a pane still drawing forgotten messages as current.
    """
    state = _state(_dm_slot())

    status, body = await _post(state, DM_SLOT)

    assert status == 200
    assert body["boundary"] == eventlog_hooks.BOUNDARY_RECORDED


@pytest.mark.asyncio
async def test_a_boundary_that_could_not_be_written_is_named_in_the_answer():
    """A reset whose record did not land, named rather than reported as clean."""
    state = _state(_dm_slot())
    with patch.object(
        eventlog_hooks, "record_slot_reset", return_value=eventlog_hooks.BOUNDARY_FAILED
    ):
        status, body = await _post(state, DM_SLOT)

    assert status == 200
    assert body["reset"] is True
    assert body["boundary"] == eventlog_hooks.BOUNDARY_FAILED


@pytest.mark.asyncio
async def test_a_refused_reset_records_no_boundary():
    """A 409 left the conversation whole, so there is no boundary to record.

    The refusal under test is the ATOMIC one -- the discard's own ``skip_if_busy``
    declining after the route has committed -- because that is the one that
    happens past every pre-check, which is where an entry written too eagerly
    would land.
    """
    slot = _dm_slot()
    state = _state(slot, performed=False)

    status, body = await _post(state, DM_SLOT)

    assert status == 409
    assert body["code"] == "turn_in_flight"
    assert _resets() == []


@pytest.mark.asyncio
async def test_a_busy_slot_is_refused_before_the_teardown():
    """The pre-check the button's disabled state mirrors."""
    slot = _dm_slot(running=True)
    state = _state(slot)

    status, body = await _post(state, DM_SLOT)

    assert status == 409
    assert body["code"] == "turn_in_flight"
    state.sessions.discard_conversation.assert_not_awaited()
    assert _resets() == []


@pytest.mark.asyncio
async def test_a_slot_with_no_member_owner_records_nothing():
    """An ordinary chat slot's reset still works and writes no member event.

    The owner is resolved from the slot KEY, so a key that names no member names
    no log to write into -- and guessing one would put a boundary into a
    crewmate's record for a conversation that was never theirs.
    """
    slot = _ChatSlot("chat-1-foo")
    slot._app = ""
    state = _state(slot)
    state._slots = {"chat-1-foo": slot}

    status, body = await _post(state, "chat-1-foo")

    assert status == 200
    assert body["reset"] is True
    # Named as not owed, which is not the same fact as a failure.
    assert body["boundary"] == eventlog_hooks.BOUNDARY_NOT_OWED
    assert _member_events() == []


@pytest.mark.asyncio
async def test_the_boundary_instant_is_the_discards_not_the_appends():
    """The race the append time loses.

    ``discard_conversation`` releases the registry lock after its pop and then
    awaits a provider shutdown it documents as slow, so a turn admitted during
    that window belongs to the SUCCESSOR conversation. A boundary stamped when
    the append finally runs lands after that turn's own rows, and a pane cutting
    on it hides the successor's live prompt as discarded history -- durably,
    because the boundary IS the durable record. So the instant is taken before
    the teardown and persisted, and the fold reads that rather than the envelope.
    """
    # The teardown reports the instant it was ENTERED, which is what the
    # boundary has to precede. Observed rather than waited out: a sleep here
    # would widen the gap the assertion measures without making it hold, and the
    # ordering is exact without one.
    entered: list[float] = []

    async def _watching_discard(key, *, replay=True, skip_if_busy=False):
        entered.append(time.time() * 1000)
        return True

    state = _state(_dm_slot())
    state.sessions.discard_conversation = _watching_discard

    status, _ = await _post(state, DM_SLOT)

    assert status == 200
    (entry,) = _resets()
    assert len(entered) == 1
    # Stamped before the teardown, so it cannot sit after a row the successor
    # conversation wrote while that teardown awaited the provider shutdown.
    assert entry["data"]["ts"] <= entered[0]

    # And the FOLD reads that instant, not the envelope's: an append a minute
    # later must not move the boundary a minute forward.
    view = _fold({**entry, "time": entry["time"] + 60_000})
    assert view["conversation_starts"][DM_SLOT] == {"ts": entry["data"]["ts"]}


def test_the_fold_falls_back_to_the_envelope_when_an_entry_carries_no_instant():
    """The closest moment a line with no own `ts` can be placed at."""
    view = _fold(
        {
            "type": types.SLOT_RESET,
            "seq": 1,
            "time": 1_700_000_000_000,
            "data": {"slot_key": DM_SLOT},
        }
    )

    assert view["conversation_starts"][DM_SLOT] == {"ts": 1_700_000_000_000}


def test_the_hook_answers_whether_the_entry_LANDED():
    """Not whether it was queued, which is what the ordered executor answers.

    A queued append cannot be inspected and the queue refuses at its ceiling, so
    a caller reading "queued" as "recorded" acknowledges a reset it has no record
    of. This is why this writer does not go through ``submit``.
    """
    assert (
        eventlog_hooks.record_slot_reset(DM_SLOT, 1_700_000_000_000)
        == eventlog_hooks.BOUNDARY_RECORDED
    )

    (entry,) = _resets()
    assert entry["data"] == {"slot_key": DM_SLOT, "ts": 1_700_000_000_000}


def test_the_hook_refuses_a_boundary_that_is_not_a_moment():
    """The log is append-only, so a line that cannot be placed is permanent.

    Refusing answers ``failed``, which is the truth the caller then reports: it
    has no boundary. Writing one with a nonsense instant would be worse -- the
    pane would cut a live conversation on it.
    """
    assert (
        eventlog_hooks.record_slot_reset(DM_SLOT, None)  # type: ignore[arg-type]
        == eventlog_hooks.BOUNDARY_FAILED
    )
    assert _resets() == []


# --------------------------------------------------------------------------- #
# the roster fold
# --------------------------------------------------------------------------- #
def _event(slot_key, ts, *, etype=types.SLOT_RESET):
    # The envelope `time` deliberately differs from the payload's `ts`, so a fold
    # reading the wrong one shows up as a wrong value rather than a coincidence.
    return {"type": etype, "seq": 1, "time": 1, "data": {"slot_key": slot_key, "ts": ts}}


def _fold(*events):
    unit = RosterProjection()
    state = unit.init()
    for event in events:
        state = unit.apply(state, event)
    return unit.view(state)


def test_the_roster_fold_carries_the_current_conversations_start():
    view = _fold(_event(DM_SLOT, 1_700_000_000_000))

    assert view["conversation_starts"] == {DM_SLOT: {"ts": 1_700_000_000_000}}


def test_the_boundary_only_ever_moves_forward():
    """Two resets on one slot can land out of APPEND order.

    The first awaits a provider shutdown while the second, started later, gets
    its entry in first. Last-wins would then move the boundary backwards onto
    messages the second reset discarded and show them as current -- durably,
    since this is the durable record. "Where the current conversation starts" is
    an answer that only moves forward, the same rule `last_active_ts` keeps.
    """
    view = _fold(
        _event(DM_SLOT, 1_700_000_900_000),
        _event(DM_SLOT, 1_700_000_000_000),
    )

    assert view["conversation_starts"][DM_SLOT] == {"ts": 1_700_000_900_000}


@pytest.mark.parametrize(
    "ts",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("-inf"), id="minus-inf"),
        pytest.param(10**400, id="int-too-big-for-a-float"),
    ],
)
def test_a_boundary_that_is_not_a_finite_number_is_dropped(ts):
    """A pair of REFUSALS lets a NaN through: `nan <= 0` and `nan > ceiling` are
    both False. The range is asserted positively instead.

    It matters past tidiness: this value is serialized into the roster response,
    where a NaN is not valid JSON and takes the whole crewmates read down in the
    browser.

    The last case is why the range assertion must also be the WHOLE check. A
    Python int has no size limit, so a line carrying ``1e400`` in full digits
    reaches the fold as an int no float can hold -- and every way of asking
    whether it is finite has to convert it first, which raises instead of
    dropping it. Comparing it against the ceiling is exact and cannot.
    """
    unit = RosterProjection()
    state = unit.init()

    assert unit.apply(state, _event(DM_SLOT, ts)) is state


@pytest.mark.parametrize(
    "held",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(10**400, id="int-too-big-for-a-float"),
    ],
)
def test_a_held_boundary_that_is_not_a_real_moment_does_not_block_a_real_one(held):
    """The monotone compare must not be the way a damaged line sticks.

    The held value is not only ever one this fold stored: a savepoint restored
    off disk lands in this state whole, so it can hold either of these -- and the
    huge int is why the held side is range-checked rather than float-checked
    too. A moment outside the range is no moment, so the real reset takes it.
    """
    unit = RosterProjection()
    state = {"conversation_starts": {DM_SLOT: {"ts": held}}}

    after = unit.apply(state, _event(DM_SLOT, 1_700_000_000_000))

    assert after["conversation_starts"][DM_SLOT] == {"ts": 1_700_000_000_000}


def test_a_second_reset_supersedes_the_first_for_that_slot():
    """ "The CURRENT conversation starts here" is one answer per slot.

    Keeping both would need the reader to decide which is current, and the
    superseded boundary is already in the log for anybody asking when it happened.
    """
    view = _fold(
        _event(DM_SLOT, 1_700_000_000_000),
        _event(DM_SLOT, 1_700_000_900_000),
        _event("member-bob", 1_700_000_500_000),
    )

    assert view["conversation_starts"][DM_SLOT] == {"ts": 1_700_000_900_000}
    assert view["conversation_starts"]["member-bob"] == {"ts": 1_700_000_500_000}


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({"ts": 1_700_000_000_000}, id="no-slot-key"),
        pytest.param({"slot_key": "", "ts": 1_700_000_000_000}, id="empty-slot-key"),
        pytest.param({"slot_key": 7, "ts": 1_700_000_000_000}, id="slot-key-not-a-string"),
        pytest.param(
            {"slot_key": "chat-1-foo", "ts": 1_700_000_000_000}, id="slot-key-not-a-dm-key"
        ),
        pytest.param(
            {"slot_key": "member-" + "x" * 200, "ts": 1_700_000_000_000},
            id="slot-key-past-the-length-cap",
        ),
    ],
)
def test_a_boundary_the_fold_cannot_stand_behind_is_dropped(data):
    """These bytes come off a file the fold does not control.

    A damaged committed line, or an edit inside the member's own log directory,
    is exactly the input the writer's own clamp never saw -- and the value is
    served to a browser on every roster read, where a bad boundary hides an
    arbitrary prefix of a conversation the model does remember.
    """
    unit = RosterProjection()
    state = unit.init()

    after = unit.apply(
        state,
        {"type": types.SLOT_RESET, "seq": 1, "time": 1_700_000_000_000, "data": data},
    )

    # The SAME object back, which is how the registry reads "nothing moved" and
    # why a dropped line publishes no frame.
    assert after is state
    assert "conversation_starts" not in unit.view(state)


@pytest.mark.parametrize(
    "ts",
    [
        pytest.param(None, id="absent"),
        pytest.param("1700000000000", id="a-string"),
        pytest.param(True, id="a-flag"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(9223372036854775807, id="past-every-clock"),
    ],
)
def test_a_moment_the_fold_cannot_stand_behind_is_dropped(ts):
    """A boundary with no readable moment is not a boundary, and one in the
    future is not a moment anybody can be past.

    The ceiling is the sharper of the two. The browser turns this number into a
    ``Date``, and a value outside that type's range -- a hand-edited
    ``9223372036854775807`` -- makes the pane's own marker construction raise
    instead of drawing a line, which replaces the whole DM with an error
    fallback.
    """
    unit = RosterProjection()
    state = unit.init()
    # BOTH fields, because the envelope is the payload's fallback: a case that
    # left `time` readable would prove the fallback works, not the bound.
    event = {
        "type": types.SLOT_RESET,
        "seq": 1,
        "time": ts,
        "data": {"slot_key": DM_SLOT, "ts": ts},
    }

    assert unit.apply(state, event) is state


def test_the_key_is_bounded_in_length_not_only_in_count():
    """The entry cap bounds how many keys the map holds, not how big one is.

    This value leaves the machine on every roster read and every
    ``member_projection`` frame, so a damaged line's multi-megabyte key would
    ride into the browser inside a map the entry cap reports as small.
    """
    from kiro_crew.members import DM_SLOT_KEY_MAX_CHARS

    unit = RosterProjection()
    state = unit.init()
    oversized = "member-" + "x" * DM_SLOT_KEY_MAX_CHARS

    assert len(oversized) > DM_SLOT_KEY_MAX_CHARS
    assert unit.apply(state, _event(oversized, 1_700_000_000_000)) is state
    # The same key inside the cap is kept, so the bound is a ceiling and not a
    # refusal of every long-ish key.
    fits = "member-" + "x" * (DM_SLOT_KEY_MAX_CHARS - len("member-"))
    assert len(fits) == DM_SLOT_KEY_MAX_CHARS
    assert unit.apply(state, _event(fits, 1_700_000_000_000)) is not state


def test_a_v2_key_at_both_name_caps_still_folds():
    """The length bound must admit every key the BUILDER can produce.

    A V2 member's DM key is the prefix plus a slug plus the memory-store suffix
    plus a private store name, and both names have caps of their own. A bound
    picked as a round number below their sum drops such a key silently: the
    reset reports ``boundary: recorded``, the fold discards the line, and the
    discarded messages come back as current on the next load. So build the real
    key through ``member_slot_key`` with both names at their caps and fold it.
    """
    from kiro_crew.members import (
        DM_SLOT_KEY_MAX_CHARS,
        SLUG_MAX_CHARS,
        member_slot_key,
    )
    from kiro_crew.memory_stores import MEMORY_STORE_NAME_MAX

    longest = member_slot_key("a" * SLUG_MAX_CHARS, "b" * MEMORY_STORE_NAME_MAX)
    # The builder's own longest output is exactly what the bound admits; any
    # slack here would mean the two were derived from different inputs.
    assert len(longest) == DM_SLOT_KEY_MAX_CHARS

    view = _fold(_event(longest, 1_700_000_000_000))

    assert view["conversation_starts"] == {longest: {"ts": 1_700_000_000_000}}


def test_slug_max_chars_agrees_with_validate_slug():
    """``SLUG_MAX_CHARS`` is a name for what ``_SLUG_RE`` already enforces.

    ``DM_SLOT_KEY_MAX_CHARS`` is summed from it, so the two drifting apart would
    put the fold's bound under the longest key the builder accepts -- which is
    the silent drop this file's V2 test exists to catch.
    """
    from kiro_crew.members import SLUG_MAX_CHARS, MemberSlugError, validate_slug

    assert validate_slug("a" * SLUG_MAX_CHARS) == "a" * SLUG_MAX_CHARS
    with pytest.raises(MemberSlugError):
        validate_slug("a" * (SLUG_MAX_CHARS + 1))


def test_the_boundary_map_is_bounded_and_evicts_the_oldest():
    """One key per slot read off an event, in an append-only log with no
    compaction, served on every roster read. Without a ceiling that is a row
    that grows for the life of the store."""
    from kiro_crew.eventlog.members_projections import _CONVERSATION_STARTS_MAX

    over = _CONVERSATION_STARTS_MAX + 3
    # Newest last, so the first three are the oldest and are the ones to go.
    view = _fold(*[_event(f"member-{n:02d}", 1_700_000_000_000 + n) for n in range(over)])

    starts = view["conversation_starts"]
    assert len(starts) == _CONVERSATION_STARTS_MAX
    assert "member-00" not in starts
    assert "member-02" not in starts
    assert f"member-{over - 1:02d}" in starts


def test_an_evicted_boundary_is_counted_and_not_dropped_quietly():
    """A dropped key reads exactly like a slot nobody ever reset.

    That is the one state this feature exists to prevent -- the pane draws a
    discarded conversation as current -- so the cap may bound the map but may
    not leave the row claiming the boundary never existed. The count says how
    many, never which: the moments themselves are gone.
    """
    from kiro_crew.eventlog.members_projections import (
        _CONVERSATION_STARTS_MAX,
        CONVERSATION_STARTS_EVICTED,
    )

    # One past the cap: exactly one slot has to go, so the count is checkable
    # rather than merely non-zero.
    over = _CONVERSATION_STARTS_MAX + 1
    events = [_event(f"member-{n:02d}", 1_700_000_000_000 + n) for n in range(over)]

    view = _fold(*events)

    assert len(view["conversation_starts"]) == _CONVERSATION_STARTS_MAX
    assert "member-00" not in view["conversation_starts"]
    assert view[CONVERSATION_STARTS_EVICTED] == 1

    # And it ACCUMULATES: a log that goes on resetting more slots keeps raising
    # the count, because every one of those is another boundary let go.
    more = _fold(*events, _event(f"member-{over:02d}", 1_700_000_000_000 + over))
    assert more[CONVERSATION_STARTS_EVICTED] == 2


def test_a_row_under_the_cap_carries_no_eviction_field_at_all():
    """Absent means zero, and the ordinary row is the absent case.

    A member drives one DM slot, so a field on every roster read would be a
    permanent nothing-to-report in a response served on every crewmates load.
    """
    from kiro_crew.eventlog.members_projections import CONVERSATION_STARTS_EVICTED

    view = _fold(_event(DM_SLOT, 1_700_000_000_000))

    assert CONVERSATION_STARTS_EVICTED not in view


@pytest.mark.parametrize(
    "held",
    [
        pytest.param("3", id="a-string"),
        pytest.param(True, id="a-flag"),
        pytest.param(-1, id="negative"),
        pytest.param(2.5, id="a-float"),
    ],
)
def test_a_damaged_eviction_count_is_replaced_by_a_real_one(held):
    """A savepoint restored off disk lands in this state whole.

    So the held count can be any of these, and the next eviction has to add to
    it. Adding to a string raises and adding to a float or a `bool` writes a
    value the pane cannot read, so the held value is validated to an `int`
    first: an unreadable one counts as no answer, which only ever
    UNDER-reports. That is the safe direction -- the field exists to be
    BELIEVED when it is non-zero, so a damaged one must not invent losses.
    """
    from kiro_crew.eventlog.members_projections import (
        _CONVERSATION_STARTS_MAX,
        CONVERSATION_STARTS_EVICTED,
    )

    unit = RosterProjection()
    state = {CONVERSATION_STARTS_EVICTED: held}
    # One past the cap, so this apply evicts and therefore has to write the
    # field rather than leave the damaged value standing.
    for n in range(_CONVERSATION_STARTS_MAX + 1):
        state = unit.apply(state, _event(f"member-{n:02d}", 1_700_000_000_000 + n))

    counted = unit.view(state)[CONVERSATION_STARTS_EVICTED]
    assert isinstance(counted, int) and not isinstance(counted, bool)
    assert counted == 1


def test_the_roster_folds_version_moved_with_the_new_field():
    """A savepoint written by a fold with no ``conversation_starts`` holds no
    boundary at all, and a resumed fold applies only the events past its
    watermark -- so that member's pane would show a discarded conversation as
    current for the life of the store. The version bump is what discards such a
    file; leaving it at 2 is the regression this pins."""
    assert RosterProjection.state_version == 3


def test_a_log_whose_only_event_is_a_reset_folds_to_the_boundary():
    """The cold-fold path the version bump sends every old savepoint down."""
    svc = get_service()
    svc.ensure("alice", "Alice")
    svc.append("alice", types.SLOT_RESET, {"slot_key": DM_SLOT, "ts": 1_700_000_000_000})

    roster = get_service().snapshot("alice")["values"][types.PROJ_ROSTER]

    assert roster["conversation_starts"][DM_SLOT]["ts"] == 1_700_000_000_000


@pytest.mark.parametrize(
    "ts",
    [
        pytest.param(10**400, id="int-too-big-for-a-float"),
        pytest.param(float("nan"), id="nan"),
    ],
)
def test_a_damaged_boundary_line_does_not_break_the_cold_read(ts):
    """The whole read, not just the fold, has to survive a damaged line.

    This log is append-only with no compaction, so a line that makes the fold
    RAISE is not a bad boundary once -- it is a member whose every projection
    read raises from then on, with nothing that can take the line back out. So
    the read returns, and returns without the boundary.
    """
    svc = get_service()
    svc.ensure("alice", "Alice")
    svc.append("alice", types.SLOT_RESET, {"slot_key": DM_SLOT, "ts": ts})

    roster = get_service().snapshot("alice")["values"][types.PROJ_ROSTER]

    assert roster.get("conversation_starts", {}) == {}
    # And the rest of the row is still served, so the damaged line costs the
    # boundary alone rather than the crewmate.
    assert roster["name"] == "Alice"


# --------------------------------------------------------------------------- #
# the discarded session's own log
# --------------------------------------------------------------------------- #
def _provider_factory_reporting(session_id: str):
    """A provider whose mock reports *session_id* as a real ``str``.

    ``session_id_of`` requires one, so a bare ``AsyncMock`` attribute makes every
    emitter call a no-op and the test would pass by writing nothing at all.
    """

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        provider.context_window_tokens = lambda: 0
        provider.has_active_turn = lambda: False
        provider.runtime_abort_target = lambda: None
        provider.session_id = session_id
        return provider

    return factory


def _unit_path(unit_id: str) -> Path:
    return store.crew_log_path(lg.KIND_SESSION, unit_id)


def _closes(unit_id: str) -> list[dict]:
    path = _unit_path(unit_id)
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "session/closed":
            out.append(entry)
    return out


@pytest.mark.asyncio
async def test_discarding_a_conversation_records_its_teardown(monkeypatch):
    """Driven through the REAL ``discard_conversation``, not a stub.

    A mocked teardown is how the missing entry stayed invisible: the route's own
    tests replace the session manager, so the only thing that can prove the close
    is written is the manager itself.
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.crew_log import emit
    from kiro_crew.session import SessionManager

    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    try:
        cfg = KiroCrewConfig()
        manager = SessionManager(cfg, provider_factory=_provider_factory_reporting("acp-discarded"))
        await manager.get_or_create("thread-reset")
        emit.on_session_opened("acp-discarded", agent="kirocrew", slot="thread-reset")
        assert emit.flush(timeout=5.0)

        assert await manager.discard_conversation("thread-reset", replay=False) is True
        assert emit.flush(timeout=5.0)

        assert [e["data"]["reason"] for e in _closes("acp-discarded")] == ["discarded"]
    finally:
        emit.reset_caches()


def test_discarded_is_deliberately_not_a_terminal_close_reason():
    """The set is the authorization to DELETE a unit, not a resumability verdict.

    A discard does clear the sid unconditionally, so the id is genuinely
    unresumable and the intuitive reading would admit it. What that would also do
    is expire the log of a conversation still on screen behind "Show earlier
    messages": the transcript is deliberately kept, so the record outlives the
    context.
    """
    log = CrewLog.create(lg.KIND_SESSION, "acp-aged-discard", owner="qa", agent="kirocrew")
    from crew_log_type_helpers import minimal_data

    log.append(
        "session/opened",
        {**minimal_data(lg.KIND_SESSION, "session/opened"), "resumed": False},
        src="gateway",
    )
    log.append("session/closed", {"reason": "discarded"}, src="gateway")
    # Age the close past any retention the sweep could be asked for.
    path = _unit_path("acp-aged-discard")
    lines = path.read_text(encoding="utf-8").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        entry = json.loads(lines[index])
        if entry.get("type") == "session/closed":
            entry["time"] = store.now_ms() - 400 * DAY_MS
            lines[index] = json.dumps(entry, separators=(",", ":"), sort_keys=True)
            break
    else:  # pragma: no cover - the append above already proved it is there
        raise AssertionError("no close to age")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    del log

    assert off_loop(store.sweep_expired, 30) == (0, 0)
    assert CrewLog.exists(lg.KIND_SESSION, "acp-aged-discard")


# --------------------------------------------------------------------------- #
# the successor's citation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_successor_cites_the_discarded_session(monkeypatch):
    """The chain the boundary is read against, in the two steps that make it.

    A discard empties the key's ``sid`` and STASHES it as ``discarded_sid``,
    which is what keeps the id nameable: ``mapped_sid`` answers the stash, and
    that is the value the next allocation hands its ``session/opened``. Reading
    the resumable id instead would answer nothing here -- the whole point of a
    discard is that the id is not resumable -- and the successor would claim to
    be the slot's first conversation.
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.crew_log import emit
    from kiro_crew.session import SessionManager

    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    try:
        cfg = KiroCrewConfig()
        manager = SessionManager(cfg, provider_factory=_provider_factory_reporting("acp-first"))
        await manager.get_or_create("thread-chain")
        # Mapped explicitly: the ACP provider maps its own id on a real
        # allocation, and the mock here is not one. The mapping is the INPUT to
        # this test, not its subject -- what is under test is what a discard
        # leaves behind in it.
        manager._session_map.set("thread-chain", "acp-first", provider="acp", cwd="")
        emit.on_session_opened("acp-first", agent="kirocrew", slot="thread-chain")
        assert emit.flush(timeout=5.0)
        assert await manager.discard_conversation("thread-chain", replay=False) is True

        # Step one: the id is gone as a resume target and still nameable.
        previous = manager.mapped_sid("thread-chain")
        assert previous == "acp-first"

        # Step two: the successor's own opening entry cites it.
        emit.on_session_opened(
            "acp-second", agent="kirocrew", slot="thread-chain", previous_sid=previous
        )
        assert emit.flush(timeout=5.0)

        opened = [
            json.loads(line)
            for line in _unit_path("acp-second").read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("type") == "session/opened"
        ]
        assert [e["data"].get("previous", {}).get("sid") for e in opened] == ["acp-first"]
    finally:
        emit.reset_caches()


def test_slot_reset_is_in_the_logs_own_vocabulary():
    """``MemberLog.append`` refuses a type the vocabulary does not declare, and
    ``slot`` is a RESERVED namespace -- so a type registered in one place and not
    the other is refused as a typo'd built-in rather than written."""
    assert types.SLOT_RESET in types.ALL_EVENT_TYPES
    assert types.is_known_event_type(types.SLOT_RESET)
    assert not types.is_contributed_event_type(types.SLOT_RESET)


def test_the_hook_resolves_the_owner_from_the_slot_key_alone():
    """Including the ``.memory-<store>`` suffix a V2 member's DM key carries,
    which is part of the SLOT's identity and never of the slug."""
    assert (
        eventlog_hooks.record_slot_reset("member-alice.memory-notes", 1_700_000_000_000)
        == eventlog_hooks.BOUNDARY_RECORDED
    )

    # Written into "alice"'s log, not into a log named for the suffixed key.
    (entry,) = _resets("alice")
    assert entry["data"]["slot_key"] == "member-alice.memory-notes"
