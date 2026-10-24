"""How an imported bundle lands as a slot: its rows, its construction window and its folder.

Pins the rows an import replays (silently, each with a minted mid), the slot kept out of
``_slots`` until construction finishes yet counted against the cap throughout (the count
released on every exit, the cap rechecked after the pre-creation awaits), and arrival
provenance filing: the folder an arrival is filed under, the finalisation guards around a
close or delete, and what a refused import takes back or discloses. The folder resolution
itself is pinned in ``test_arrival_folders.py``; the import endpoint's answers and its
transcript prefix live in ``test_session_transfer.py``, and Layer B's write and rollback in
``test_session_transfer_layer_b.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from session_transfer_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _export_gz,
    _isolate_import_tmp,
    _make_request,
    _run_import,
    _stub_state,
    _valid,
)


@pytest.mark.asyncio
async def test_imported_rows_are_replayed_silently_not_broadcast(monkeypatch):
    """Import is a silent replay onto a RETRACTED slot: no row may broadcast.

    The materialiser keeps the slot out of ``_slots`` during hydration so nothing
    can interleave. ``_ChatSlot.append`` also broadcasts a live ``chat_message``
    SSE event when ``broadcast=True`` (its docstring names session_transfer among
    the replay callers that must pass False), which would push a retracted slot's
    peer content to every client and retire live question cards. Every appended
    row must therefore carry ``broadcast=False``.
    """
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.chat_handlers import _ChatSlot

    seen_broadcast: list[bool] = []
    real_append = _ChatSlot.append

    def _spy_append(self, role, content, cls="", ts="", *, broadcast=True, **kw):
        seen_broadcast.append(broadcast)
        return real_append(self, role, content, cls, ts, broadcast=broadcast, **kw)

    monkeypatch.setattr(_ChatSlot, "append", _spy_append)

    msgs = [
        {"role": "user", "content": "q", "ts": ""},
        {"role": "assistant", "content": "a", "ts": ""},
    ]
    await _run_import(st, monkeypatch, _valid(messages=msgs))

    assert seen_broadcast, "no rows were appended; fixture did not engage"
    assert all(b is False for b in seen_broadcast), (
        f"an imported row was appended with broadcast=True ({seen_broadcast}); it "
        "would fan a retracted slot's content out as a live SSE event"
    )


@pytest.mark.asyncio
async def test_imported_rows_carry_a_minted_mid(monkeypatch):
    """Bundle rows have no message id, so the materialiser must mint one.

    The old importer left ``mint_mid`` at its default (True) and minted a mid per
    row; the shared path defaults to False for resume, whose disk rows already
    carry mids. Import passes ``mint_missing_mids=True`` -- without it, imported
    rows land permanently id-less and drop out of every mid-keyed feature
    (row-identity dedup, the non-legacy Fork path).
    """
    from kiro_crew.dashboard import session_transfer as st

    msgs = [
        {"role": "user", "content": "q", "ts": ""},
        {"role": "assistant", "content": "a", "ts": ""},
    ]
    slot = await _run_import(st, monkeypatch, _valid(messages=msgs), return_slot=True)

    for m in slot.messages:
        mid = (m.get("meta") or {}).get("mid")
        assert isinstance(mid, str) and mid, f"an imported row landed without a minted mid: {m!r}"


@pytest.mark.asyncio
async def test_slot_is_unreachable_until_construction_finishes(monkeypatch):
    """A slot mid-import is retracted from ``_slots`` for its async tail.

    The materialiser returns the slot registered, but the import handler pops it
    from ``state._slots`` for the async Layer B write/join + durable save, so no
    raw ``state._slots.get`` acquirer (delete/close, regenerate, rewind) can reach
    it mid-finalization. It stays under ``begin_slot_construction`` for the count,
    and is re-registered on the success path once finalization lands. So at the
    Layer B write the slot must be ABSENT from ``_slots`` and UNDER CONSTRUCTION;
    after the handler returns it is registered and out of the construction set.
    Retracting is safe here because import mints its own key (no concurrent
    same-key request can target it), unlike a client-supplied resume key.
    """
    from kiro_crew.dashboard import session_transfer as st

    seen_registered: list[bool] = []
    seen_under_construction: list[bool] = []
    state = _stub_state(st, monkeypatch)

    def _probe(*_a, **_k):
        # Sampled from inside the build, standing in for a concurrent lookup.
        s = state._imported_slot
        seen_registered.append(s.key in state._slots)
        seen_under_construction.append(s.key in state._slots_under_construction)
        return "new-sid"

    monkeypatch.setattr(st, "_write_layer_b_files", _probe)
    monkeypatch.setattr(st, "_join_layer_b", lambda *_a, **_k: True)

    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 200
    # RETRACTED (absent from _slots, so no raw acquirer can find it) AND under
    # construction (the count is still open) during the async tail.
    assert seen_registered == [False], (
        "the slot must be retracted from _slots during the async Layer B tail so "
        "no raw acquirer can close/mutate it mid-finalization"
    )
    assert seen_under_construction == [True], (
        "the slot must stay under construction during the tail (count open, "
        "released in the finally)"
    )
    # ...and re-registered + published once everything landed.
    slot = state._imported_slot
    assert state._slots.get(slot.key) is slot
    assert (
        slot.key not in state._slots_under_construction
    ), "construction was never ended; the slot would stay hidden forever"


# ── slot-cap accounting across the construction window ───────────────────


def test_retracted_import_slot_still_counts_against_the_cap():
    """A slot retracted for construction is unreachable but ALLOCATED.

    The cap is sampled before creation and reads the live count, so if a
    retracted slot stopped counting, concurrent imports would each sample a
    total that excluded every other import in flight -- and all of them would be
    waved past a cap that was already full.
    """
    from kiro_crew.dashboard.state import DashboardState

    state = DashboardState.__new__(DashboardState)
    state._slots = {"chat-1": object()}
    state._slots_under_construction = set()

    assert state.live_slot_count() == 1

    state.begin_slot_construction("chat-2")
    assert state.live_slot_count() == 2, "an in-flight slot vanished from the cap"

    # Idempotent, and releasing restores the count.
    state.end_slot_construction("chat-2")
    state.end_slot_construction("chat-2")
    assert state.live_slot_count() == 1


@pytest.mark.asyncio
async def test_import_releases_the_construction_count_on_every_exit(monkeypatch):
    """A leaked construction key inflates the cap for the process lifetime --
    every later import would be refused with the cap never actually reached.

    Drives the failure path (the durable save refuses, which returns 503 from
    inside the ``try``) and asserts the release happened anyway.
    """
    from kiro_crew.dashboard import session_transfer as st

    async def _boom(*_a, **_k):
        raise RuntimeError("disk gone")

    state = _stub_state(st, monkeypatch, save=_boom)
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 503
    assert state._slots_under_construction == set(), "construction count leaked"
    # And the slot was not published either -- a refused import leaves no tab.
    assert state._slots == {}


@pytest.mark.asyncio
async def test_import_releases_the_construction_count_on_success(monkeypatch):
    """The success path publishes the slot and stops counting it separately, so
    the total does not double-count a landed import."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 200
    assert state._slots_under_construction == set()
    assert state.live_slot_count() == 1


@pytest.mark.asyncio
async def test_slot_cap_is_rechecked_after_the_pre_creation_awaits(monkeypatch):
    """The first cap test is necessary but not sufficient: body parsing and agent
    resolution both await, so concurrent imports near the cap all clear it before
    any of them allocates. The second test sits with no await before creation.

    Simulated by filling the map DURING the awaited agent resolution -- exactly
    what a sibling request would do.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    def _resolve_then_fill(_hint):
        # A concurrent import lands while this one is awaiting agent resolution
        # (the awaited step between the two cap checks). Runs in a worker thread
        # via the real ``asyncio.to_thread``; filling a plain dict is GIL-safe.
        state._slots.update({f"s{i}": object() for i in range(500)})
        return ""

    monkeypatch.setattr(st, "_resolve_agent", _resolve_then_fill)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(agent="some-agent")))

    assert resp.status == 429
    assert json.loads(resp.body)["code"] == "transfer_slot_cap"
    # Nothing was allocated by the request that lost the race.
    assert "imported-1" not in state._slots


# ── arrival provenance filing ────────────────────────────────────────────
#
# docs/request-for-change/rfc-arrival-provenance-filing.md. The unit-level
# behaviour of the folder resolution lives in test/test_arrival_folders.py; these
# pin what the HANDLER does with it, which is where the two properties that matter
# most are observable: both arrival routes file, and placement is resolved late
# enough that a refusal cannot leave a folder behind.


def _folder_names(state):
    """``{name: parent_id}`` for the stub store, for readable assertions."""
    return {str(f["name"]): str(f.get("parent_id", "")) for f in state._folders}


def _filed_folder(state):
    """The folder the imported slot was filed into, as a name; "" when unfiled."""
    slot = state._imported_slot
    fid = getattr(slot, "folder_id", "")
    if not fid:
        return ""
    return next((str(f["name"]) for f in state._folders if str(f["id"]) == fid), "<dangling>")


@pytest.mark.asyncio
async def test_a_plain_json_arrival_is_filed_under_imported_from_sender(monkeypatch):
    """The TUNNEL shape: a peer posts a plain-JSON bundle.

    This is the changed default the RFC exists for — a peer-pushed session used
    to land at the top level.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 200, resp.body
    assert _folder_names(state) == {
        "Imported": "",
        "from mac": next(f["id"] for f in state._folders if f["name"] == "Imported"),
    }
    assert _filed_folder(state) == "from mac"
    # The answer names that folder, so the importing tab can open its path.
    assert json.loads(resp.body)["folder_id"] == state._imported_slot.folder_id != ""


@pytest.mark.asyncio
async def test_a_gzipped_arrival_is_filed_exactly_like_the_plain_one(monkeypatch):
    """The FILE shape, and the property the RFC's §3 turns on: the destination
    must not depend on the body's format.

    A gzip body and a plain body carrying the same ``origin`` must reach the same
    folder. Asserted by importing both into one store and requiring ONE pair of
    folders plus one shared child id — a per-format destination would show up as
    two children or two groups.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    plain = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert plain.status == 200, plain.body
    first = state._imported_slot.folder_id

    gz = _export_gz(_valid(origin="mac"))
    second_resp = await st.api_chat_slot_import(_make_request(state, None, gz=gz))
    assert second_resp.status == 200, second_resp.body
    second = state._imported_slot.folder_id

    assert first and second and first == second
    assert sorted(_folder_names(state)) == ["Imported", "from mac"]


@pytest.mark.asyncio
async def test_an_app_scoped_arrival_lands_unfiled_and_creates_no_folder(monkeypatch):
    """RFC §5.1. The folder store has a global ceiling, so an app token that
    could create a folder per arrival could loop imports with distinct origins
    until the person is refused a folder of their own."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    request = _make_request(state, _valid(origin="mac"))
    request.get = lambda k, default="": "some-app" if k == "app" else default

    resp = await st.api_chat_slot_import(request)

    assert resp.status == 200, resp.body
    assert state._folders == []
    assert _filed_folder(state) == ""
    assert json.loads(resp.body)["folder_id"] == ""


@pytest.mark.asyncio
async def test_filing_identity_comes_from_the_shared_caller_rule(monkeypatch):
    """RFC §5.1's second half: identity from ``effective_request_app``, never
    from ``request.get("app")`` alone.

    The internal-secret transport publishes no app claim, so the shared rule
    DERIVES one from the calling session's key. A handler reading only the claim
    sees the dashboard owner here and would create a folder for an app caller —
    which is the ceiling loop §5.1 refuses. Mutation-checked: swapping the
    handler's ``effective_request_app`` back to ``request.get("app", "")`` files
    this arrival and reddens the assertion below.
    """
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.dashboard.chat_handlers import _ChatSlot

    state = _stub_state(st, monkeypatch)
    # A slot an app owns, named by the caller's own session key. Nothing about
    # the request carries an app claim.
    caller = _ChatSlot("app-caller")
    caller._app = "some-app"
    state._slots["app-caller"] = caller

    request = _make_request(state, _valid(origin="mac"))
    request.headers = {"X-Session-Key": "dashboard:app-caller"}
    assert request.get("app", "") == "", "the premise: no app claim on the request"

    resp = await st.api_chat_slot_import(request)

    assert resp.status == 200, resp.body
    assert state._folders == []
    assert _filed_folder(state) == ""


@pytest.mark.asyncio
async def test_a_second_arrival_from_one_sender_lands_beside_the_first(monkeypatch):
    """The RFC's first acceptance bullet: not in a second folder of the same
    name."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    first_id = state._imported_slot.folder_id

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert second.status == 200, second.body
    second_id = state._imported_slot.folder_id

    assert first_id == second_id
    assert len(state._folders) == 2, _folder_names(state)


@pytest.mark.asyncio
async def test_a_landed_arrival_records_the_row_it_shares(monkeypatch):
    """The call site a failed creator's rollback depends on.

    Written on the landed path, immediately above the final witness, and only for
    a row the filing ADOPTED. The first import CREATED its rows, so no other
    import holds those ids and no other rollback can reach them: it records
    nothing. The second ADOPTED, so its landing is what stops the first one's
    rollback deleting a placement this session points at once it archives and goes
    invisible to the live-slot check.

    Destination only. The second import adopted the GROUP as well, and that row is
    deliberately left unmarked: the rollback's second guard already spares a row
    whose child survived.

    Mutation-checked: dropping the call reddens the shared-row assertion, and
    dropping the ``not in created_folders`` condition reddens the first import's.
    """
    from kiro_crew.dashboard import arrival_folders as af
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    shared_id = state._imported_slot.folder_id
    assert not any(bool(f.get(af.ARRIVAL_ADOPTED_KEY)) for f in state._folders), (
        "an import that CREATED its rows records nothing: no other import holds "
        "those ids, so no other rollback can reach them"
    )

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert second.status == 200, second.body
    assert state._imported_slot.folder_id == shared_id, "the premise: the second adopted the row"

    rows = {str(f["id"]): f for f in state._folders}
    group_id = next(str(f["id"]) for f in state._folders if str(f["name"]) == "Imported")
    assert rows[shared_id].get(af.ARRIVAL_ADOPTED_KEY), (
        "a landed arrival that adopted its destination must record it, or the "
        "rollback of whichever import created that row deletes a placement this "
        "session still points at"
    )
    assert not rows[group_id].get(af.ARRIVAL_ADOPTED_KEY), (
        "the adopted PARENT is left unmarked on purpose -- a row whose child "
        "survives is already spared by the rollback's second guard"
    )


@pytest.mark.asyncio
async def test_a_delete_landing_in_the_shared_row_mark_refuses(monkeypatch):
    """Why the shared-row mark sits ABOVE the final witness rather than below it.

    ``mark_arrival_folder_shared`` is the last await on the path, so a
    ``DELETE /api/sessions/{key}`` can land inside it. Below the witness that
    delete arrives past the last check: it removes the transcript and pops the
    slot, and the handler still answers ``200 ok``, with nothing downstream to
    correct it -- the success return does not re-arm ``_dirty`` and the delete's
    pop is terminal. Above the witness the same delete is caught and the request
    refuses.

    The delete is injected BY the mark, so the witness can only observe it if the
    witness runs afterwards. That states the ordering as behaviour rather than as
    a line number, which a rearrangement cannot satisfy by accident.

    Mutation-checked: moving the mark back below the witness makes this read 200,
    and so does dropping the witness call.
    """
    from kiro_crew.dashboard import arrival_folders as af
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    shared_id = state._imported_slot.folder_id

    deleted: list[str] = []
    real_mark = st.mark_arrival_folder_shared

    async def _mark_then_delete(mark_state, folder_id, **_kw):
        recorded = await real_mark(mark_state, folder_id)
        # The delete lands INSIDE this await, which is the whole point of where
        # the call sits: a witness above it cannot see this.
        deleted.append(str(folder_id))
        # Report what the real mark reported, so this exercises the ORDERING and
        # not the separate unfiling branch a failed mark takes.
        return recorded

    monkeypatch.setattr(st, "mark_arrival_folder_shared", _mark_then_delete)
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: bool(deleted))

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert deleted, "the premise: the second import adopted a row and marked it"
    assert second.status == 409, (
        "a delete landing in the shared-row mark must refuse -- below the witness "
        "that await yields the loop past the last check and the handler publishes "
        "ok for a transcript the delete has already removed"
    )
    assert json.loads(second.body).get("code", "").startswith("transfer_import_deleted")
    rows = {str(f["id"]): f for f in state._folders}
    assert rows[shared_id].get(af.ARRIVAL_ADOPTED_KEY), (
        "the mark stands through the refusal: taking one back needs each import's "
        "own claim recorded on the row, so this leaves one visible deletable row "
        "rather than stripping a claim that may belong to another import"
    )


@pytest.mark.asyncio
async def test_a_mark_that_cannot_be_recorded_unfiles_the_session(monkeypatch):
    """What an unrecorded mark owes, and why reporting it is not enough.

    The mark is the ONLY thing sparing an adopted row once this session archives:
    the rollback's occupancy guard reads live slots, and an archived session is
    popped out of that mapping. So a mark that never landed means a concurrent
    creator's rollback can reclaim the row while this transcript still points at
    it, and the person is left filed into a folder that does not exist.

    The remedy is to unfile, which is the state the folder-gone repair already
    produces -- cleared and persisted, rather than a dangling id left behind.

    Mutation-checked: making the mark's failure path report success, or dropping
    the unfiling branch, both leave the stale ``folder_id`` on the slot.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    shared_id = state._imported_slot.folder_id

    async def _mark_fails(_state, _folder_id, **_kw):
        return False

    monkeypatch.setattr(st, "mark_arrival_folder_shared", _mark_fails)

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert second.status == 200, second.body
    assert state._imported_slot.folder_id == "", (
        "an unrecorded mark must unfile the session: leaving the id set points the "
        "transcript at a row a concurrent creator's rollback can reclaim"
    )
    assert shared_id, "the premise: the first import created a row to adopt"


@pytest.mark.asyncio
async def test_a_recorded_mark_leaves_the_session_filed(monkeypatch):
    """The companion. Without it the unfiling could fire unconditionally and this
    would still look correct, because an unfiled session is also a valid state."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    first = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    assert first.status == 200, first.body
    shared_id = state._imported_slot.folder_id

    second = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert second.status == 200, second.body
    assert (
        state._imported_slot.folder_id == shared_id
    ), "a mark that was recorded must leave the session filed where it landed"


@pytest.mark.asyncio
async def test_two_senders_get_two_children_under_one_group(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    for origin in ("mac", "linux-desk"):
        resp = await st.api_chat_slot_import(_make_request(state, _valid(origin=origin)))
        assert resp.status == 200, resp.body

    group_id = next(f["id"] for f in state._folders if f["name"] == "Imported")
    assert _folder_names(state) == {
        "Imported": "",
        "from mac": group_id,
        "from linux-desk": group_id,
    }


@pytest.mark.asyncio
async def test_a_bundle_with_no_sender_is_filed_under_the_group(monkeypatch):
    """A missing ``origin`` names no peer, so inventing "from unknown" would make
    an absent field look like one."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="")))

    assert resp.status == 200, resp.body
    assert _folder_names(state) == {"Imported": ""}
    assert _filed_folder(state) == "Imported"


@pytest.mark.asyncio
async def test_placement_is_resolved_after_the_slot_exists(monkeypatch):
    """RFC §5.2. The post-await slot-cap re-check can answer 429, and a folder
    written in FRONT of it is left behind when it fires — the same folder-store
    exhaustion with a narrower trigger.

    The cap is made to bite only AFTER the handler's early check, which is the
    shape a real concurrent burst produces, and the assertion is that the refused
    import left the store empty. Mutation-checked: moving the filing call up to
    the ``meta`` construction reddens this.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    calls = {"n": 0}

    def _count():
        calls["n"] += 1
        # First call is the handler's early check (must pass); every later call is
        # the post-await re-check (must refuse).
        return 0 if calls["n"] == 1 else st.MAX_LIVE_SLOTS

    state.live_slot_count = _count
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 429, resp.body
    assert json.loads(resp.body)["code"] == "transfer_slot_cap"
    assert state._folders == [], "a refused import must leave no folder behind"


@pytest.mark.asyncio
async def test_a_folder_deleted_during_finalisation_leaves_no_dangling_id(monkeypatch):
    """RFC §5.5, the folder-lifecycle defect that is hardest to see from the code.

    For the whole finalisation stretch the slot is RETRACTED from
    ``state._slots``, and that mapping is what the folder delete handler's unfile
    sweep iterates — so a delete committing here cannot see the session to unfile
    it. The repair after re-registration is what closes it. Mutation-checked:
    removing the post-registration re-check leaves ``folder_id`` pointing at a
    row that is gone, which this test reads as ``<dangling>``.
    """
    from kiro_crew.dashboard import session_transfer as st

    saved: list[str] = []

    async def _save_then_delete(_state, slot, *_a, **_k):
        saved.append(getattr(slot, "folder_id", ""))
        # The person deletes the folder while the durable save is in flight.
        state._folders.clear()
        return True

    state = _stub_state(st, monkeypatch, save=_save_then_delete)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 200, resp.body
    assert saved and saved[0], "the premise: the save carried a folder_id"
    assert state._imported_slot.folder_id == ""
    assert _filed_folder(state) == ""


@pytest.mark.asyncio
async def test_a_refused_repair_save_is_not_reported_as_a_landed_import(monkeypatch):
    """A refusal is not a commit.

    ``save_slot_off_loop`` turns an exception into ``True`` under best-effort,
    but it returns ``False`` CLEANLY when the delete-won guard fires, i.e. this
    session's transcript was deleted while the import was finishing. That
    ``False`` is terminal: the guard returns cleanly so the flush clears
    ``_dirty`` and the delete stands, so nothing re-arms and no later flush
    corrects a reported success. Ignoring it answered ``ok: true`` for a session
    whose file is gone and left the slot published as a zombie.

    Mutation-checked: dropping the ``if not`` around the repair save makes this
    read 200 with ``ok: true`` and the slot still in ``_slots``.
    """
    from kiro_crew.dashboard import session_transfer as st

    calls: list[bool] = []
    pushes: list[int] = []

    async def _save_then_lose_to_a_delete(_state, _slot, *_a, **_k):
        first = not calls
        calls.append(True)
        if first:
            # The durable save lands, and the person deletes the folder while it
            # is in flight -- which is what arms the repair below.
            state._folders.clear()
            return True
        # The repair save now meets the delete-won guard: this session's file was
        # removed, so the write is refused cleanly rather than raising.
        return False

    state = _stub_state(st, monkeypatch, save=_save_then_lose_to_a_delete)
    state.push_slots_update = lambda: pushes.append(1)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    key = state._imported_slot.key

    assert len(calls) == 2, "the premise: the repair save actually ran"
    assert resp.status == 409, resp.body
    body = json.loads(resp.body)
    assert body.get("code") == "transfer_import_deleted"
    assert "ok" not in body, "a refused import must not answer ok at all"
    assert key not in state._slots, "the zombie slot must not stay registered"
    assert pushes == [], "a refused import must not publish the slot as landed"


@pytest.mark.asyncio
async def test_the_finalisation_saves_are_pinned_to_this_slot(monkeypatch):
    """Why the synchronous identity check is not enough on its own.

    Both finalisation saves check ``state._slots.get(slot.key) is slot`` and then
    await. The executor wait frees the event loop, so a close landing in that gap
    pops the slot and persists ``closed=True`` while the in-flight save still holds
    ``closed=False`` in memory. ``chat_persistence`` has the atomic answer -- it
    re-reads the map at the commit boundary with no await before the write -- but
    ONLY when ``expected_slot_name`` is passed; absent it, that recheck is skipped
    entirely and the stale snapshot lands on top of the dismissal.

    Asserted on the ARGUMENT rather than through behaviour on purpose: a stub save
    cannot exercise the real commit-boundary recheck, so a behavioural test here
    would pass with the pin removed.

    Mutation-checked: dropping ``expected_slot_name`` from the repair save reddens
    this.
    """
    from kiro_crew.dashboard import session_transfer as st

    seen: list[dict] = []

    async def _record_then_lose_the_folder(_state, _slot, *_a, **kw):
        seen.append(dict(kw))
        if len(seen) == 1:
            # Arms the repair: the person deletes the folder mid-import.
            state._folders.clear()
        return True

    state = _stub_state(st, monkeypatch, save=_record_then_lose_the_folder)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    key = state._imported_slot.key

    assert resp.status == 200, resp.body
    assert len(seen) >= 2, "the premise: the repair save ran after the durable save"
    assert seen[-1].get("expected_slot_name") == key, (
        "the repair save must pin the slot name, or chat_persistence skips its "
        "commit-boundary recheck and a concurrent close can be overwritten"
    )


@pytest.mark.asyncio
async def test_a_close_at_the_repair_saves_commit_boundary_is_not_a_deletion(monkeypatch):
    """The pin's refusal and the delete-won guard both answer ``False``, and they
    need opposite handling.

    A pin refusal means a close or replacement won the map, so the import DID land
    and the close is the person's own later action: skip, and answer success.
    Reporting it as a deletion would claim the transcript was destroyed when it was
    not, and would drop a slot that belongs to somebody else now.

    The sibling above covers the other direction: a ``False`` with this slot still
    in the map is the delete-won guard, which is terminal and answers 409.

    Mutation-checked: treating every ``False`` as a deletion makes this read 409.
    """
    from kiro_crew.dashboard import session_transfer as st

    calls: list[bool] = []

    async def _save_then_lose_the_slot(_state, slot, *_a, **_k):
        first = not calls
        calls.append(True)
        if first:
            state._folders.clear()
            return True
        # The close landed inside this save's executor wait: it popped the slot and
        # persisted its own ``closed=True``, so the commit-boundary recheck refuses
        # this stale write. Nothing was deleted.
        state._slots.pop(slot.key, None)
        return False

    state = _stub_state(st, monkeypatch, save=_save_then_lose_the_slot)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    body = json.loads(resp.body)

    assert len(calls) == 2, "the premise: the repair save actually ran"
    assert resp.status == 200, resp.body
    assert body.get("ok") is True, (
        "a pin refusal is not a deletion: the transcript landed, so the import "
        "reports success and leaves the close alone"
    )
    assert (
        "transfer_import_deleted" not in resp.body.decode()
    ), "claiming deletion here would report data loss that did not happen"


@pytest.mark.asyncio
async def test_a_close_during_the_folder_check_is_not_undone_by_the_repair(monkeypatch):
    """The repair save may only write a slot it still owns.

    The folder-existence check awaits, and a close landing inside that await pops
    the slot and THEN persists ``closed=True``. The imported slot object's own
    ``closed`` is still False, so an unguarded repair save writes that flag back
    off: the archived record loses the dismissal and the tab the person closed
    resurfaces, while both requests report success.

    The import itself landed, so this asserts the repair is SKIPPED rather than
    refused -- and that the close's flag survives.

    Mutation-checked two ways: dropping the guard runs the repair save and clears
    the flag, and writing it with ``_slot_still_ours`` polarity (an absent key
    counting as still ours) does exactly the same, because a close pops first.
    """
    from kiro_crew.dashboard import session_transfer as st

    saves: list[str] = []
    archived = {"closed": False}

    async def _count_then_delete_the_folder(_state, slot, *_a, **_k):
        first = not saves
        saves.append(getattr(slot, "folder_id", ""))
        if first:
            # The person deletes the arrival folder while the durable save is in
            # flight, which is what arms the repair below.
            state._folders.clear()
        return True

    state = _stub_state(st, monkeypatch, save=_count_then_delete_the_folder)
    real_exists = st.arrival_folder_exists

    async def _a_close_lands_inside_the_check(_state, folder_id):
        answer = await real_exists(_state, folder_id)
        if not answer:
            # The folder is gone, so this is the call that arms the repair. The
            # person closes the tab in the same window: a close pops the slot
            # FIRST, then persists the dismissal.
            slot = state._imported_slot
            state._slots.pop(slot.key, None)
            archived["closed"] = True
        return answer

    monkeypatch.setattr(st, "arrival_folder_exists", _a_close_lands_inside_the_check)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert saves and saves[0], "the premise: the durable save carried a folder_id"
    assert len(saves) == 1, (
        "the repair save must not run once the slot has left _slots -- it would "
        "write this object's closed=False over the close's own record"
    )
    assert archived["closed"] is True, "the person's dismissal must survive"
    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True, (
        "the transcript landed and the close is the person's own later action, so "
        "the import is not a failure"
    )


@pytest.mark.asyncio
async def test_a_delete_during_the_folder_check_is_not_reported_as_landed(monkeypatch):
    """The folder-gone guard alone leaves the COMMON case unchecked.

    A ``DELETE /api/sessions/{key}`` landing in the folder-existence await removes
    this transcript and pops the slot while the folder it points at is still
    perfectly fine. The repair branch is keyed on the FOLDER being gone, so it is
    skipped, and without the unconditional witness the handler falls straight
    through to ``ok: true`` for a session that has already been destroyed. Nothing
    corrects it afterwards: the success return does not re-arm ``_dirty`` and the
    delete's pop is terminal.

    The folder is deliberately left INTACT here. That is the whole point -- with
    the folder gone this would pass on the older guard alone and prove nothing.

    Mutation-checked: deleting the witness call makes this read 200 with
    ``ok: true`` and the slot still registered.
    """
    from kiro_crew.dashboard import session_transfer as st

    pushes: list[int] = []
    existed: list[bool] = []
    state = _stub_state(st, monkeypatch)
    state.push_slots_update = lambda: pushes.append(1)

    real_exists = st.arrival_folder_exists

    async def _watch_exists(_state, folder_id):
        answer = await real_exists(_state, folder_id)
        existed.append(answer)
        return answer

    monkeypatch.setattr(st, "arrival_folder_exists", _watch_exists)
    # The folder survives the check; only the SESSION is deleted.
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    key = state._imported_slot.key

    assert existed and existed[-1] is True, (
        "the premise: the folder was still THERE at the repair check, so the "
        "folder-gone branch was not the one that produced this refusal"
    )
    assert resp.status == 409, resp.body
    body = json.loads(resp.body)
    assert body.get("code") == "transfer_import_deleted"
    assert "ok" not in body, "a refused import must not answer ok at all"
    assert key not in state._slots, "the zombie slot must not stay registered"
    assert pushes == [], "a refused import must not publish the slot as landed"


@pytest.mark.asyncio
async def test_the_delete_witness_runs_on_the_ordinary_success_path(monkeypatch):
    """The companion pin, and the one that keeps the guard above from going dead.

    A witness reached only through the folder-gone branch would leave the case
    that finding was about unguarded while every test still passed. So assert it
    is consulted on the plain import too -- folder intact, nothing deleted, 200 --
    which is exactly the path the older code returned ``ok`` on without asking.
    """
    from kiro_crew.dashboard import session_transfer as st

    asked: list[str] = []
    state = _stub_state(st, monkeypatch)

    def _witness(_state, slot):
        asked.append(slot.key)
        return False

    monkeypatch.setattr(st, "session_was_deleted", _witness)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True
    assert asked == [
        state._imported_slot.key
    ], "the witness must be consulted once on the success path, for THIS slot"


@pytest.mark.asyncio
async def test_the_witness_still_runs_when_the_repair_save_reported_success(monkeypatch):
    """Why the witness is unconditional and not an ``elif`` on the folder branch.

    ``save_slot_off_loop`` returning ``True`` does NOT mean the delete-won guard
    reached a decision: under best-effort it converts a RAISING save to ``True``
    and marks the slot dirty. So a shape that only asks the witness when the
    folder branch was skipped would trust a ``True`` that decided nothing.

    Here the folder IS gone, the repair save reports success the way a swallowed
    exception does, and the session was deleted underneath. An ``elif`` answers
    200; the unconditional check answers 409. This test is the difference.
    """
    from kiro_crew.dashboard import session_transfer as st

    saves: list[bool] = []

    async def _save_then_lose_the_folder(_state, _slot, *_a, **_k):
        first = not saves
        saves.append(True)
        if first:
            # The person deletes the folder while the durable save is in flight,
            # which is what arms the repair branch below.
            state._folders.clear()
        # Both saves report success -- the repair one the way best-effort does
        # when it swallows an exception, having decided nothing.
        return True

    state = _stub_state(st, monkeypatch, save=_save_then_lose_the_folder)
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert len(saves) == 2, "the premise: the repair save ran and reported success"
    assert not state._folders, "the premise: the folder branch was the one taken"
    assert resp.status == 409, resp.body
    assert json.loads(resp.body).get("code") == "transfer_import_deleted"


@pytest.mark.asyncio
async def test_a_refused_import_takes_back_the_folder_it_created(monkeypatch):
    """The folder is committed before the transcript's durable save, so a 503
    would otherwise leave an empty row behind for every distinct sender. Nothing
    reclaims it afterwards -- there is no orphan-folder sweep -- so the handler
    unwinds its own rows on the way out, exactly as it unwinds the Layer B files.

    Mutation-checked: dropping the rollback call leaves the folders in the store.
    """
    from kiro_crew.dashboard import session_transfer as st

    async def _save_fails(*_a, **_k):
        raise OSError("sessions dir is not writable")

    state = _stub_state(st, monkeypatch, save=_save_fails)
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 503, resp.body
    assert json.loads(resp.body).get("code") == "transfer_import_save_failed"
    assert state._folders == [], "a refused import must not leave its folders behind"


@pytest.mark.asyncio
async def test_a_refusal_discloses_a_transcript_it_cannot_remove(monkeypatch):
    """A refusal may only claim what it achieved.

    The transcript is persisted BEFORE the witness runs, and the witness reports
    "deleted" for three different situations: the file is gone, the file belongs
    to a NEW incarnation, and existence is unverifiable. Only the first makes
    "nothing was kept" true. The other two leave a file this instance must not
    unlink -- a new incarnation is somebody else's session, and an unverifiable
    read names nothing safe to remove -- so the answer discloses it.

    Mutation-checked: answering the plain code regardless makes this read
    ``transfer_import_deleted`` with a body promising nothing was kept.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)
    monkeypatch.setattr(st, "session_transcript_remains", lambda _s, _slot: True)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    body = json.loads(resp.body)

    assert resp.status == 409, resp.body
    assert body.get("code") == "transfer_import_deleted_partial"
    assert "remains on disk" in body.get("error", ""), "the leftover must be disclosed"
    assert "nothing was kept" not in body.get(
        "error", ""
    ), "the clean-slate promise is exactly what this case cannot make"


@pytest.mark.asyncio
async def test_a_refusal_still_promises_a_clean_slate_when_the_file_is_gone(monkeypatch):
    """The companion, and what keeps the disclosure from swallowing the plain
    case: when the transcript really is gone, the refusal says so plainly."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)
    monkeypatch.setattr(st, "session_transcript_remains", lambda _s, _slot: False)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))
    body = json.loads(resp.body)

    assert resp.status == 409, resp.body
    assert body.get("code") == "transfer_import_deleted"
    assert "nothing was kept" in body.get("error", "")


@pytest.mark.asyncio
async def test_a_refusal_leaves_a_same_key_replacement_alone(monkeypatch):
    """The refusal must not clean up by key alone.

    The witness fires because THIS slot's session was deleted, and a replacement
    can land at the same key while the finalisation tail runs. Popping by key
    would drop the replacement's slot and forgetting the join by key would take
    its mapping and its files -- silent loss of a session this import never owned.

    Mutation-checked: removing the identity guard pops the replacement.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    state = _stub_state(st, monkeypatch)
    state.sessions.forget_conversation = lambda k: (forgotten.append(k), "")[1]
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)

    replacement = SimpleNamespace(key="imported-1", folder_id="")

    def _swap(_state, _slot):
        # A replacement takes the key while the tail is running, which is exactly
        # the situation the witness reports as "deleted".
        state._slots["imported-1"] = replacement
        return False

    monkeypatch.setattr(st, "session_transcript_remains", _swap)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 409, resp.body
    assert (
        state._slots.get("imported-1") is replacement
    ), "the replacement's slot must survive a refusal that is not about it"
    assert forgotten == [], "the replacement's Layer B mapping must not be forgotten"


@pytest.mark.asyncio
async def test_a_folder_store_failure_leaves_the_session_filed_nowhere(monkeypatch):
    """RFC §5.6. Filing is convenience; the transcript is the payload, so a store
    failure must not fail an import that works today."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    async def _boom(*_a, **_k):
        raise OSError("folders.json is not writable")

    state.mutate_folders = _boom
    resp = await st.api_chat_slot_import(_make_request(state, _valid(origin="mac")))

    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True
    assert _filed_folder(state) == ""
