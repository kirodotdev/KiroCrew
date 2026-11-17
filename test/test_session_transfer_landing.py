"""How an imported bundle lands as a slot: its rows, its construction window and its folder.

Folder resolution itself is in ``test_arrival_folders.py``, the endpoint's answers in
``test_session_transfer.py``, and Layer B's write and rollback in
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

    ``broadcast=True`` would push a live ``chat_message`` SSE event, fanning the
    retracted slot's peer content to every client and retiring live question cards.
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

    The shared path defaults to False for resume, whose disk rows already carry mids.
    Without ``mint_missing_mids=True`` imported rows drop out of every mid-keyed
    feature (row-identity dedup, the non-legacy Fork path).
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

    During the Layer B write and durable save it is absent from ``_slots`` (so no raw
    acquirer such as delete, regenerate or rewind can reach it) but still under
    construction for the cap. Retracting is safe because import mints its own key.
    """
    from kiro_crew import members
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
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
    # A kiro-cli pair is written only for a slot that starts on kiro-cli.
    monkeypatch.setattr(members, "select_provider_backend", lambda *_a, **_k: ACP_BACKEND_KIRO)

    resp = await st.api_chat_slot_import(
        _make_request(state, _valid(layer_b={"envelope": {}, "events": "e"}))
    )

    assert resp.status == 200
    assert seen_registered == [False], (
        "the slot must be retracted from _slots during the async Layer B tail so "
        "no raw acquirer can close/mutate it mid-finalization"
    )
    assert seen_under_construction == [True], (
        "the slot must stay under construction during the tail (count open, "
        "released in the finally)"
    )
    slot = state._imported_slot
    assert state._slots.get(slot.key) is slot
    assert (
        slot.key not in state._slots_under_construction
    ), "construction was never ended; the slot would stay hidden forever"


# ── slot-cap accounting across the construction window ───────────────────


def test_retracted_import_slot_still_counts_against_the_cap():
    """A slot retracted for construction is unreachable but ALLOCATED.

    Otherwise concurrent imports would each sample a total excluding the others in
    flight, and all would pass a cap that was already full.
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
    """A failed save (503 from inside the ``try``) still releases the construction key.

    A leaked key would inflate the cap for the process lifetime.
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
async def test_a_landed_import_refreshes_the_older_sessions_listing(monkeypatch):
    """The slot is out of ``_slots`` while its transcript is saved, so a listing
    served then shows it under Older Sessions. Creation pushes the first listing
    refresh, the landing's sync pushes the one that corrects that listing, and a
    refused import pushes only the first."""
    from kiro_crew.dashboard import chat_utils_slots
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(chat_utils_slots, "set_dashboard_surfaced", lambda _keys, _tabs=None: None)

    def _really_synced(save):
        state = _stub_state(st, monkeypatch, save=save)
        state.sessions.set_active_dashboard_slots = lambda _keys: None
        monkeypatch.setattr(st, "_sync_dashboard_slots", chat_utils_slots._sync_dashboard_slots)
        create = state.get_or_create_slot

        def _create_and_sync(*args, **kwargs):
            # As production's get_or_create_slot does, before construction begins.
            slot = create(*args, **kwargs)
            chat_utils_slots._sync_dashboard_slots(state)
            return slot

        state.get_or_create_slot = _create_and_sync
        return state

    listed: list[list[str]] = []

    async def _save_while_listed(*_a, **_k):
        listed.append([s.key for s in chat_utils_slots.listing_slot_snapshot(state)])
        return True

    state = _really_synced(_save_while_listed)
    resp = await st.api_chat_slot_import(_make_request(state, _valid()))

    assert resp.status == 200
    assert listed and listed[0] == [], f"the save did not run with the slot retracted: {listed}"
    assert state.push_listing_refresh.call_count == 2

    async def _boom(*_a, **_k):
        raise RuntimeError("disk gone")

    refused = _really_synced(_boom)
    resp = await st.api_chat_slot_import(_make_request(refused, _valid()))

    assert resp.status == 503
    assert refused.push_listing_refresh.call_count == 1


@pytest.mark.asyncio
async def test_slot_cap_is_rechecked_after_the_pre_creation_awaits(monkeypatch):
    """Body parsing and agent resolution await, so the cap is checked again before creation.

    A sibling request is simulated by filling the map during agent resolution.
    """
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)

    def _resolve_then_fill(_hint):
        # Runs in a worker thread via the real ``asyncio.to_thread``; filling a
        # plain dict is GIL-safe.
        state._slots.update({f"s{i}": object() for i in range(500)})
        return ""

    monkeypatch.setattr(st, "_resolve_agent", _resolve_then_fill)

    resp = await st.api_chat_slot_import(_make_request(state, _valid(agent="some-agent")))

    assert resp.status == 409
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

    A peer-pushed session is filed, not left at the top level.
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
    """The FILE shape: the destination must not depend on the body's format (RFC §3).

    Both go into one store, so a per-format destination would show as two folders.
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
    """Filing identity comes from ``effective_request_app``, not ``request.get("app")``.

    The internal-secret transport carries no app claim, so the shared rule derives one
    from the caller's session key; reading the claim alone would file for an app caller.
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
    """Only an ADOPTED destination row is marked shared, so a creator's rollback spares it.

    A CREATED row needs no mark: no other import holds its id. The adopted group row
    stays unmarked because the rollback already spares a row whose child survived.
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

    ``mark_arrival_folder_shared`` is the last await, so a ``DELETE /api/sessions/{key}``
    can land inside it; below the witness the handler would answer ``200 ok`` for a
    removed transcript. The delete is injected by the mark, so only a later witness sees it.
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
        # The delete lands inside this await.
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

    Once archived, the session leaves the live slots the rollback's occupancy guard
    reads, so only the mark spares the row. Without it the session is unfiled, as the
    folder-gone repair does, rather than left pointing at a row that may vanish.
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
    """A 409 from the post-await cap re-check leaves no folder behind (RFC §5.2).

    The cap bites only after the early check, as a real concurrent burst does.
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

    assert resp.status == 409, resp.body
    assert json.loads(resp.body)["code"] == "transfer_slot_cap"
    assert state._folders == [], "a refused import must leave no folder behind"


@pytest.mark.asyncio
async def test_a_folder_deleted_during_finalisation_leaves_no_dangling_id(monkeypatch):
    """A folder deleted during finalisation is repaired after re-registration (RFC §5.5).

    The slot is retracted from ``state._slots``, which the folder delete's unfile sweep
    iterates, so the delete cannot unfile it itself.
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

    ``save_slot_off_loop`` returns ``False`` cleanly when the delete-won guard fires.
    That is terminal: ``_dirty`` is cleared, so no later flush would correct an ``ok``
    answer and the slot would stay published as a zombie.
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

    A close can land in the save's executor wait. ``chat_persistence`` rechecks the map
    at the commit boundary only when ``expected_slot_name`` is passed. Asserted on the
    argument because a stub save cannot exercise the real recheck.
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
    """A pin refusal (slot gone from the map) answers success, not a deletion.

    Both it and the delete-won guard return ``False``. Here a close won the map and
    the import did land; a ``False`` with the slot still mapped is the 409 case.
    """
    from kiro_crew.dashboard import session_transfer as st

    calls: list[bool] = []

    async def _save_then_lose_the_slot(_state, slot, *_a, **_k):
        first = not calls
        calls.append(True)
        if first:
            state._folders.clear()
            return True
        # A close landed in this save's executor wait, so the commit-boundary
        # recheck refuses this stale write. Nothing was deleted.
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

    A close inside the folder-existence await pops the slot, then persists
    ``closed=True``; the slot object still says False, so a repair save would revive the
    closed tab. The repair is skipped, not refused. An absent key must not count as ours.
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
            # A close pops the slot first, then persists the dismissal.
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
    """A session delete during the folder check refuses even with the folder intact.

    The repair branch is keyed on the folder being gone, so only the unconditional
    witness catches this. The folder stays intact on purpose, or the test proves nothing.
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
    """The witness is consulted on a plain import too, not only in the folder-gone branch."""
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

    Under best-effort ``save_slot_off_loop`` turns a raising save into ``True``, so a
    ``True`` from the repair save decided nothing about a delete.
    """
    from kiro_crew.dashboard import session_transfer as st

    saves: list[bool] = []

    async def _save_then_lose_the_folder(_state, _slot, *_a, **_k):
        first = not saves
        saves.append(True)
        if first:
            # Deleting the folder mid-save arms the repair branch.
            state._folders.clear()
        # The repair save reports success as best-effort does for a swallowed error.
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
    """The folder is committed before the durable save, so a 503 must unwind it.

    There is no orphan-folder sweep to reclaim it later.
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

    The witness says "deleted" when the file is gone, belongs to a new incarnation, or
    cannot be verified. Only the first makes "nothing was kept" true; the others leave a
    file this instance must not unlink, so the answer discloses it.
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

    A replacement can take the key during the finalisation tail; popping or forgetting
    by key would lose a session this import never owned.
    """
    from kiro_crew.dashboard import session_transfer as st

    forgotten: list[str] = []
    state = _stub_state(st, monkeypatch)
    state.sessions.forget_conversation = lambda k: (forgotten.append(k), "")[1]
    monkeypatch.setattr(st, "session_was_deleted", lambda _s, _slot: True)

    replacement = SimpleNamespace(key="imported-1", folder_id="")

    def _swap(_state, _slot):
        # A replacement takes the key while the tail is running.
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
