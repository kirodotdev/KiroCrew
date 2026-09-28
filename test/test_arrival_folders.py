"""Arrival provenance filing — the folder resolution behind an arriving session.

Unit-level cover for :mod:`kiro_crew.dashboard.arrival_folders`. The handler's own
use of it (both arrival routes, the late resolution, the mid-import repair) is
pinned in ``test/test_session_transfer.py``; what is pinned HERE is the resolution
itself, and specifically the five folder-lifecycle constraints
``docs/request-for-change/rfc-arrival-provenance-filing.md`` §5 records. Each of
those constraints is a real defect class in the folder lifecycle rather than in
the filing idea, which is why each one has its own test below.

The store stub models ``DashboardState.mutate_folders`` / ``read_folders``
faithfully in the one way that matters for these tests: the mutator runs against
the live list under a notional lock and its ``(changed, value)`` pair is the
contract. A stub that let two mutators interleave would make the atomicity
assertion below unfalsifiable, so the transaction COUNT is asserted instead of
simulated concurrency.
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import arrival_folders as af
from kiro_crew.dashboard.chat_folders import (
    MAX_CHAT_FOLDERS,
    folder_ids_filed_into,
    note_folder_filed,
)


def _store(folders=None):
    """A minimal folder store with ``mutate_folders`` / ``read_folders``."""
    state = SimpleNamespace(_folders=list(folders or []), mutations=0, reads=0)

    async def _mutate(mutate, on_committed=None):
        state.mutations += 1
        changed, value = mutate(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    async def _read(read):
        state.reads += 1
        return read(state._folders)

    state.mutate_folders = _mutate
    state.read_folders = _read
    return state


def _row(name, parent_id="", **over):
    row = {
        "id": f"id-{name.replace(' ', '-')}",
        "name": name,
        "order": 0,
        "collapsed": False,
        "hidden": False,
        "parent_id": parent_id,
        "project_dir": "",
        "default_agent": "",
    }
    row.update(over)
    return row


def _tree(state):
    """``{name: parent_id}``, for assertions that read like the sidebar."""
    return {str(f["name"]): str(f.get("parent_id", "")) for f in state._folders}


# ── names ────────────────────────────────────────────────────────────────


def test_the_mirrored_clip_matches_the_folder_store_itself():
    """Constraint 4 has a NUMBER in it, and a number restated in two places
    drifts. This reads the clip out of ``create_folder_record``'s own source
    rather than asserting a literal, so raising the store's limit without
    following it here fails HERE instead of shipping a duplicate folder per
    arrival from a long-named sender.
    """
    from kiro_crew.dashboard.chat_folders import create_folder_record

    source = inspect.getsource(create_folder_record)
    found = re.search(r"name = name\.strip\(\)\[:(\d+)\]", source)
    assert found, "create_folder_record does not clip the name the way this mirrors"
    assert af.FOLDER_NAME_MAX == int(found.group(1))


def test_a_sender_name_is_clipped_the_way_the_store_would_clip_it():
    long_origin = "x" * 300
    name = af.sender_folder_name(long_origin)
    # Both sides spelled independently of the function: the length against the
    # mirrored constant, the value against the transformation written out. A
    # comparison against ``af.store_folder_name`` would move with the code and
    # could not fail.
    assert len(name) == af.FOLDER_NAME_MAX
    assert name == f"from {long_origin}".strip()[:100]


def test_no_sender_yields_no_sender_folder_name():
    """A missing ``origin`` must not become "from unknown" — that would make an
    absent field look like a peer."""
    assert af.sender_folder_name("") == ""
    assert af.sender_folder_name("   ") == ""


def test_the_folder_names_are_ascii_english_literals():
    """Constraint 6. A folder created here becomes an ordinary sidebar row a
    person can rename; a name re-derived per render from the active locale would
    fight that rename and move the folder whenever the interface language
    changed."""
    for name in (af.IMPORTED_FOLDER_NAME, af.sender_folder_name("laptop")):
        assert name.isascii(), name
    assert af.IMPORTED_FOLDER_NAME == "Imported"
    assert af.sender_folder_name("laptop") == "from laptop"


# ── create and adopt ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_first_arrival_creates_the_group_and_the_sender_folder():
    state = _store()
    folder_id = (await af.arrival_folder_id(state, origin="mac", request_app="")).folder_id

    group = next(f for f in state._folders if f["name"] == "Imported")
    child = next(f for f in state._folders if f["name"] == "from mac")
    assert _tree(state) == {"Imported": "", "from mac": str(group["id"])}
    assert folder_id == str(child["id"])


@pytest.mark.asyncio
async def test_both_levels_are_settled_in_one_transaction():
    """Constraint 3. Two transactions would leave a window in which a delete of
    ``Imported`` lands between the two appends and the child is persisted with a
    dangling ``parent_id``.

    The count is the assertion because the stub cannot interleave mutators: a
    resolution that took two transactions would be observable here and nowhere
    else in this file.
    """
    state = _store()
    (await af.arrival_folder_id(state, origin="mac", request_app="")).folder_id
    assert state.mutations == 1


@pytest.mark.asyncio
async def test_a_second_arrival_from_one_sender_adopts_the_same_folder():
    state = _store()
    first = (await af.arrival_folder_id(state, origin="mac", request_app="")).folder_id
    second = (await af.arrival_folder_id(state, origin="mac", request_app="")).folder_id

    assert first == second
    assert len(state._folders) == 2, _tree(state)


@pytest.mark.asyncio
async def test_a_clipped_row_is_adopted_rather_than_duplicated():
    """Constraint 4, in the direction that actually bites: the store WROTE a
    clipped name, and a lookup comparing the untruncated one misses it.

    Mutation-checked: dropping the clip from ``store_folder_name`` makes the
    lookup compare a 305-character name against the 100-character row, misses,
    and appends a second folder — which this test reads as a third row.
    """
    long_origin = "y" * 300
    # Spelled out INDEPENDENTLY of the function under test, which is the whole
    # point: deriving this from ``sender_folder_name`` would make the fixture move
    # with the mutation and the test could never fail (measured -- it survived
    # exactly that way before this line was written).
    clipped = f"from {long_origin}".strip()[:100]
    group = _row("Imported")
    # The seeded child carries the CLIPPED name, exactly as a previous arrival
    # would have written it.
    state = _store([group, _row(clipped, parent_id=group["id"], id="child-1")])

    folder_id = (await af.arrival_folder_id(state, origin=long_origin, request_app="")).folder_id

    assert len(state._folders) == 2, _tree(state)
    assert folder_id == str(state._folders[1]["id"])


@pytest.mark.asyncio
async def test_a_renamed_capitalisation_still_owns_the_folder():
    """Matched case-insensitively on the stored name, so a person who fixed the
    capitalisation keeps the folder the next arrival lands in."""
    group = _row("imported")
    state = _store([group, _row("From Mac", parent_id=group["id"])])

    folder_id = (await af.arrival_folder_id(state, origin="mac", request_app="")).folder_id

    assert len(state._folders) == 2, _tree(state)
    assert folder_id == str(state._folders[1]["id"])


@pytest.mark.asyncio
async def test_a_top_level_folder_with_the_sender_name_is_not_the_child():
    """The lookup is scoped to the parent. An unrelated top-level ``from mac``
    the person made for their own purposes must not be mistaken for the child of
    ``Imported`` — filing into it would move peer content into a folder that is
    not part of this tree."""
    state = _store([_row("from mac")])

    folder_id = (await af.arrival_folder_id(state, origin="mac", request_app="")).folder_id

    group = next(f for f in state._folders if f["name"] == "Imported")
    # Three rows: the pre-existing top-level one, the group, and the real child
    # under the group.
    assert len(state._folders) == 3, _tree(state)
    assert folder_id != str(state._folders[0]["id"])
    child = next(f for f in state._folders if str(f["id"]) == folder_id)
    assert child["name"] == "from mac"
    assert str(child["parent_id"]) == str(group["id"])


@pytest.mark.asyncio
async def test_a_bundle_with_no_sender_is_filed_under_the_group_directly():
    state = _store()
    folder_id = (await af.arrival_folder_id(state, origin="", request_app="")).folder_id

    assert _tree(state) == {"Imported": ""}
    assert folder_id == str(state._folders[0]["id"])


@pytest.mark.asyncio
async def test_a_hidden_folder_is_re_engaged_on_arrival():
    """Matches the folder CRUD handler's unhide-on-assign rule: a folder that
    receives a session is not left hidden.

    Applied on the LANDED path, not in the resolving transaction. Adopted rows are
    absent from ``created_ids``, so no rollback can put the flag back; flipping it
    during resolution means an import that later fails has changed the person's
    visibility preference on a row it never filed anything into. So the filing
    REPORTS the hidden rows and leaves them hidden, and the landed write flips
    them.

    Mutation-checked: flipping ``hidden`` inside ``_resolve`` again reddens the
    first assertion, and dropping the unhide from the landed write reddens the
    last.
    """
    group = _row("Imported", hidden=True)
    state = _store([group, _row("from mac", parent_id=group["id"], hidden=True)])

    filing = await af.arrival_folder_id(state, origin="mac", request_app="")

    assert [f["hidden"] for f in state._folders] == [True, True], (
        "resolution must leave the flags alone: an import that fails after this "
        "point cannot put them back"
    )
    assert set(filing.hidden_ids) == {
        str(group["id"]),
        str(state._folders[1]["id"]),
    }, "both adopted rows it found hidden must be reported for the landed path"

    assert await af.mark_arrival_folder_shared(
        state, filing.folder_id, unhide=filing.hidden_ids
    ), "the premise: the landed write succeeded"

    assert [f["hidden"] for f in state._folders] == [False, False], (
        "a landed arrival un-hides every adopted row it was filed through, so the "
        "session is actually visible"
    )


@pytest.mark.asyncio
async def test_a_failed_import_leaves_a_hidden_folder_hidden():
    """The companion, and the whole point of deferring the flip.

    Without the deferral this is the deterministic bug: any import that fails
    after resolution has flipped a hidden adopted row to visible, and no rollback
    undoes it because adopted rows are not this import's to reclaim.
    """
    group = _row("Imported", hidden=True)
    state = _store([group, _row("from mac", parent_id=group["id"], hidden=True)])

    filing = await af.arrival_folder_id(state, origin="mac", request_app="")
    # The import fails here: the landed write never runs.
    await af.discard_arrival_folders(state, filing.created_rows)

    assert [f["hidden"] for f in state._folders] == [
        True,
        True,
    ], "a failed import must leave the visibility preference exactly as it found it"


# ── refusals ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_app_scoped_caller_creates_no_folder():
    """Constraint 1. The store has a global ceiling, so an app token that could
    create a folder per arrival could loop imports with distinct origins until
    the ceiling is full and the person is refused a folder of their own."""
    state = _store()
    folder_id = (await af.arrival_folder_id(state, origin="mac", request_app="some-app")).folder_id

    assert folder_id == ""
    assert state._folders == []
    assert state.mutations == 0, "an app arrival must not even open a transaction"


@pytest.mark.asyncio
async def test_an_app_scoped_caller_adopts_no_folder_either():
    """The stricter half, and the reason refusing only CREATION is not enough:
    adopting the person's folder would file peer content into a row they own."""
    group = _row("Imported")
    state = _store([group, _row("from mac", parent_id=group["id"])])

    folder_id = (await af.arrival_folder_id(state, origin="mac", request_app="some-app")).folder_id

    assert folder_id == ""
    assert state.mutations == 0


@pytest.mark.asyncio
async def test_the_app_scoped_refusal_is_audited_as_a_denial(monkeypatch):
    """``backend-security-controls`` wants a SEL event for every permission
    decision, and this module audits the grant. Auditing only the grant is the
    gap: an auditor filtering ``source="session_import"`` would see every folder
    an arrival created and no trace of the arrivals refused one.

    Asserted on the DISCRIMINATING fields rather than on the call count, so the
    test cannot pass against a grant-shaped event: it is the ``denied`` outcome
    carrying the caller's own app identity that the refusal owes the log.
    """
    recorded: list[dict] = []
    monkeypatch.setattr(
        af, "sel", lambda: SimpleNamespace(log_api_access=lambda **kw: recorded.append(kw))
    )
    state = _store()

    folder_id = (await af.arrival_folder_id(state, origin="mac", request_app="some-app")).folder_id

    assert folder_id == ""
    assert len(recorded) == 1, "the refusal owes the log exactly one event"
    event = recorded[0]
    assert event["outcome"] == "denied"
    assert event["caller"] == "some-app", "the app's own identity, not 'dashboard'"
    assert event["source"] == "session_import", "same pathway as the grant it pairs with"
    assert event["operation"] == "chat.arrival_folder_create", "one decision, two outcomes"
    # ``origin`` is a label off an untrusted bundle, so the record names it as
    # the resource that was refused -- never as an identity.
    assert "mac" in event["resources"]
    assert event.get("critical") in (None, False), (
        "filing is best-effort: an unwritable audit log must not fail an import "
        "whose transcript landed, which is what keeps this function's "
        "never-raises contract true"
    )


@pytest.mark.asyncio
async def test_the_ceiling_refuses_the_pair_rather_than_half_landing_it():
    """Constraint 1's mechanism, and constraint 6's posture.

    With exactly one slot left under the ceiling, creating the group and then
    being refused the child would leave a bare ``Imported`` folder no arrival is
    ever filed into. The pair is counted before either append, so the whole
    resolution is refused and the session lands unfiled.
    """
    state = _store([_row(f"f{i}") for i in range(MAX_CHAT_FOLDERS - 1)])

    folder_id = (await af.arrival_folder_id(state, origin="mac", request_app="")).folder_id

    assert folder_id == ""
    assert len(state._folders) == MAX_CHAT_FOLDERS - 1
    assert "Imported" not in _tree(state)


@pytest.mark.asyncio
async def test_a_full_store_still_files_under_an_existing_group():
    """Reachable only when ``Imported`` already exists and the store is exactly
    full: refusing here would discard a real destination that costs no new row."""
    group = _row("Imported")
    state = _store([group] + [_row(f"f{i}") for i in range(MAX_CHAT_FOLDERS - 1)])

    folder_id = (await af.arrival_folder_id(state, origin="mac", request_app="")).folder_id

    assert folder_id == str(group["id"])
    assert len(state._folders) == MAX_CHAT_FOLDERS


@pytest.mark.asyncio
async def test_a_store_write_failure_leaves_the_session_unfiled():
    """Constraint 6. The transcript is the payload and the grouping is
    convenience, so filing must never fail an import that works today."""
    state = _store()

    async def _boom(*_a, **_k):
        raise OSError("folders.json is not writable")

    state.mutate_folders = _boom
    assert (await af.arrival_folder_id(state, origin="mac", request_app="")).folder_id == ""


# ── the rollback for a failed import ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_import_takes_back_the_folder_it_created():
    """The row is committed before the transcript's durable save, so a failure
    after that point would leave an empty ``Imported`` / ``from mac`` pair
    behind. Both levels go, and the store is left exactly as it started."""
    state = _store()
    filing = await af.arrival_folder_id(state, origin="mac", request_app="")

    assert len(filing.created_ids) == 2, "the premise: this filing created both levels"
    await af.discard_arrival_folders(state, filing.created_rows)

    assert state._folders == [], "a failed import leaves no trace in the store"


@pytest.mark.asyncio
async def test_the_rollback_never_deletes_a_folder_it_only_adopted():
    """The guard that matters most. Adopting means the person already owned the
    row, so a failed import has no business deleting it -- and ``created_ids``
    is what encodes the difference, not the destination id."""
    group = _row("Imported")
    child = _row("from mac", parent_id=group["id"])
    state = _store([group, child])
    filing = await af.arrival_folder_id(state, origin="mac", request_app="")

    assert filing.folder_id == child["id"], "the premise: it filed into the existing row"
    assert filing.created_ids == (), "the premise: it created nothing, it adopted"
    await af.discard_arrival_folders(state, filing.created_rows)

    assert state._folders == [group, child], "the person's own folders survive untouched"


@pytest.mark.asyncio
async def test_the_rollback_spares_a_row_a_landed_arrival_adopted():
    """The guard ``filed_on`` cannot supply, because it reads LIVE slots only.

    An archived session is popped out of ``state._slots``, so a row it is filed
    into looks unoccupied, and the parent guard only spares a row with a
    surviving child. Two same-sender imports reach it: the first created
    ``from mac``, the second adopted it, LANDED, and then archived, and the first
    one's failure would delete a placement that archived session still points at.

    The marker the landed second import writes is what closes it, and it is per
    ROW: a row nobody adopted is still reclaimed in the very same rollback, which
    is the leak this rollback exists to close. Adopting alone records nothing --
    asserted here, because that is what
    ``test_two_failed_same_sender_imports_leave_no_row_behind`` depends on.

    Mutation-checked, and one test kills both variants. Not writing the marker on
    the landed path deletes ``from mac``; inverting the test to skip an UNMARKED
    row spares ``from pc`` and deletes ``from mac``, so either way this fails.
    """
    state = _store()
    first = await af.arrival_folder_id(state, origin="mac", request_app="")
    second = await af.arrival_folder_id(state, origin="mac", request_app="")
    other = await af.arrival_folder_id(state, origin="pc", request_app="")

    assert second.created_ids == (), "the premise: the second arrival adopted, it created nothing"
    assert second.folder_id == first.folder_id, "the premise: both landed in the one row"
    assert other.created_ids == (
        other.folder_id,
    ), "the premise: the other sender's row is this arrival's own creation"

    rows = {str(f["id"]): f for f in state._folders}
    assert not rows[second.folder_id].get(
        af.ARRIVAL_ADOPTED_KEY
    ), "the premise: adopting records nothing on its own -- the mark is the landed path's"

    # The second import reached the end of its handler: past every refusal, it
    # records the row it shares with whoever created it.
    await af.mark_arrival_folder_shared(state, second.folder_id)

    rows = {str(f["id"]): f for f in state._folders}
    assert rows[first.folder_id].get(
        af.ARRIVAL_ADOPTED_KEY
    ), "the premise: landing is what recorded the sharing"
    assert not rows[other.folder_id].get(
        af.ARRIVAL_ADOPTED_KEY
    ), "the premise: a row only ever created carries no marker"

    # The session that adopted it has archived, so nothing live is filed anywhere.
    state._slots = {}
    await af.discard_arrival_folders(state, (*first.created_rows, *other.created_rows))

    survivors = {str(f["id"]) for f in state._folders}
    assert first.folder_id in survivors, (
        "the row an archived session is filed into must survive -- no live slot "
        "reports it, so only the adoption marker can spare it"
    )
    assert other.folder_id not in survivors, (
        "a row nobody adopted is still reclaimed, in this same call -- the guard "
        "is per row, not a switch that spares the whole rollback"
    )


@pytest.mark.asyncio
async def test_two_failed_same_sender_imports_leave_no_row_behind():
    """An adoption that never became a session must not pin the creator's rows.

    The leak that moving the marker onto the landed path closes: with it written
    at adoption time, a second same-sender import marked the first one's
    brand-new rows, and when BOTH imports then failed their durable save the
    first one's rollback spared a pair nothing was ever filed into. The marker is
    one-way, so nothing reclaimed them afterwards and they consumed the folder
    cap for good.

    Mutation-checked: restoring either ``_mark_adopted`` call in the resolver
    marks the pair here, the rollback then spares both rows, and this fails.
    """
    state = _store()
    first = await af.arrival_folder_id(state, origin="mac", request_app="")
    second = await af.arrival_folder_id(state, origin="mac", request_app="")

    assert len(first.created_ids) == 2, "the premise: the first arrival created group and child"
    assert second.created_ids == (), "the premise: the second arrival adopted both rows"
    assert second.folder_id == first.folder_id, "the premise: both resolved the one destination"

    # Neither import landed, so neither recorded a shared row, and no session is
    # filed anywhere.
    state._slots = {}
    await af.discard_arrival_folders(state, first.created_rows)

    assert state._folders == [], (
        "both imports failed, so nothing points at either row and the pair is the "
        "creator's to reclaim -- a mark written merely because the second import "
        "passed through would pin them for good"
    )


@pytest.mark.asyncio
async def test_the_rollback_leaves_a_folder_another_session_has_moved_into():
    """A concurrent arrival, or a person dragging a session in, can fill the row
    between its creation and the rollback. Deleting it then would unfile
    somebody else's session, so an occupied row is left alone."""
    state = _store()
    filing = await af.arrival_folder_id(state, origin="mac", request_app="")
    child_id = filing.folder_id
    state._slots = {"other": SimpleNamespace(folder_id=child_id)}

    await af.discard_arrival_folders(state, filing.created_rows)

    survivors = {str(f["id"]) for f in state._folders}
    assert child_id in survivors, "the occupied row must survive"
    # Its parent survives too, because deleting a parent whose child remains
    # would orphan that child -- the second guard, exercised by the same case.
    assert len(state._folders) == 2, "the parent of a surviving child stays as well"


@pytest.mark.asyncio
async def test_the_rollback_walks_child_before_parent():
    """Order is load-bearing, not cosmetic. ``created_ids`` is parent-first, so
    the rollback reverses it: taking the parent first would meet its own
    still-present child and refuse, stranding the pair it was asked to remove."""
    state = _store()
    filing = await af.arrival_folder_id(state, origin="mac", request_app="")
    parent_id, child_id = filing.created_ids

    assert any(
        str(f.get("parent_id")) == parent_id for f in state._folders
    ), "the premise: the second id really is a child of the first"
    await af.discard_arrival_folders(state, filing.created_rows)

    gone = {str(f["id"]) for f in state._folders}
    assert parent_id not in gone and child_id not in gone


@pytest.mark.asyncio
async def test_the_mark_reports_whether_the_row_is_protected():
    """The handler branches on this value, so the value itself needs pinning.

    ``False`` covers both a write that raised and a row already gone: in either
    case nothing is protecting the placement, and the caller's remedy is the same.
    An empty id reports ``True`` because there is no placement to protect.
    """
    state = _store()
    filing = await af.arrival_folder_id(state, origin="mac", request_app="")
    assert filing.folder_id, "the premise: the arrival was filed somewhere"

    assert (
        await af.mark_arrival_folder_shared(state, filing.folder_id) is True
    ), "a row that is present and written must report protected"
    assert (
        await af.mark_arrival_folder_shared(state, "") is True
    ), "no placement means nothing to protect, so this is not a failure"
    assert (
        await af.mark_arrival_folder_shared(state, "no-such-row") is False
    ), "a row that is already gone protects nothing, so the caller must unfile"

    async def _boom(*_a, **_k):
        raise OSError("folders.json is not writable")

    state.mutate_folders = _boom
    assert await af.mark_arrival_folder_shared(state, filing.folder_id) is False, (
        "a write that raised leaves the row unmarked, so it reports unprotected "
        "rather than swallowing the failure into an apparent success"
    )


@pytest.mark.asyncio
async def test_the_rollback_never_fails_the_refusal_it_is_cleaning_up_after():
    """The caller is already answering a failure, so a store error here must
    stay swallowed: raising would replace a precise coded refusal with a 500."""
    state = _store()
    filing = await af.arrival_folder_id(state, origin="mac", request_app="")

    async def _boom(*_a, **_k):
        raise OSError("folders.json is not writable")

    state.mutate_folders = _boom
    await af.discard_arrival_folders(state, filing.created_rows)


@pytest.mark.asyncio
async def test_the_rollback_does_nothing_when_the_filing_created_nothing():
    """An app-scoped or unfiled arrival has no rows to take back, and must not
    open a transaction to discover that."""
    state = _store()
    before = state.mutations

    await af.discard_arrival_folders(state, ())

    assert state.mutations == before, "no rows to remove means no transaction"


@pytest.mark.asyncio
async def test_a_row_the_person_renamed_survives_the_rollback():
    """The fourth guard, and the gap the first three cannot see.

    A person who renames a brand-new arrival folder files no session into it,
    writes no adopt marker and leaves no surviving child, so all three earlier
    guards pass and the rename would be deleted with the row. The window is not a
    single failing save: the last of the three rollback call sites is the
    finalization tail's delete-witness refusal, past the transcript save, the
    folder-existence await and the shared-row mark.

    Mutation-checked: dropping the fourth guard deletes the renamed row, and
    comparing the row against itself instead of the snapshot spares every row and
    reddens the companion below.
    """
    state = _store()
    filing = await af.arrival_folder_id(state, origin="mac", request_app="")
    assert len(filing.created_rows) == 2, "the premise: this filing created both levels"

    child_id = filing.folder_id
    rows = {str(f["id"]): f for f in state._folders}
    rows[child_id]["name"] = "Work in progress"
    state._slots = {}

    await af.discard_arrival_folders(state, filing.created_rows)

    survivors = {str(f["id"]) for f in state._folders}
    assert child_id in survivors, (
        "a row the person renamed must survive: no slot is filed into it, it "
        "carries no adopt marker and it has no child, so nothing else spares it"
    )


@pytest.mark.asyncio
async def test_an_untouched_row_is_still_reclaimed():
    """The companion, and what stops the fourth guard sparing everything.

    Without it the guard could compare a row against itself and always agree,
    which would disable the rollback the module exists to provide.
    """
    state = _store()
    filing = await af.arrival_folder_id(state, origin="mac", request_app="")
    state._slots = {}

    await af.discard_arrival_folders(state, filing.created_rows)

    assert state._folders == [], "an untouched pair is reclaimed exactly as before"


@pytest.mark.asyncio
async def test_a_row_a_hand_filed_archived_session_points_at_survives():
    """The fifth guard: occupancy the live-slot read cannot see.

    A person drags an unrelated session into a brand-new arrival folder and closes
    its tab. The slot is popped out of ``_slots``, so the placement is invisible to
    the live-slot read; the row carries no adopt marker, because a hand move is not
    an adopting import; and it has no surviving child. Every earlier guard passes,
    so a failing import would delete a row that archived session still names,
    leaving a dangling ``folder_id``.

    Mutation-checked: dropping the union deletes the row out from under the
    archived session, and the companion below is what stops the union sparing
    everything.
    """
    state = _store()
    filing = await af.arrival_folder_id(state, origin="mac", request_app="")
    child_id = filing.folder_id
    note_folder_filed(state, child_id)
    state._slots = {}

    await af.discard_arrival_folders(state, filing.created_rows)

    survivors = {str(f["id"]) for f in state._folders}
    assert child_id in survivors, (
        "a row a hand-filed archived session points at must survive: no live slot "
        "shows the placement, no adopt marker covers a hand move, and it has no child"
    )


def test_the_recorder_ignores_an_unfiling():
    """Clearing a session's folder records no occupancy.

    An empty destination is an unfiling, so there is nothing pointing at a row to
    protect. Without this the caller would have to branch before every call.
    """
    state = _store()

    note_folder_filed(state, "")

    assert folder_ids_filed_into(state) == set(), "an unfiling claims no row"


def test_the_reader_hands_back_a_copy():
    """The record survives a caller that edits what it was given.

    The one caller unions this into its own occupancy set inside a store
    transaction; handing out the live set would let a future caller drop the
    protection for every later rollback in the process.
    """
    state = _store()
    note_folder_filed(state, "f1")

    handed_out = folder_ids_filed_into(state)
    handed_out.add("f2")
    handed_out.discard("f1")

    assert folder_ids_filed_into(state) == {"f1"}, "the record is not the copy"


@pytest.mark.asyncio
async def test_filing_a_session_through_the_route_records_the_row(tmp_path, monkeypatch):
    """The wiring: the guard above is inert unless the handler records.

    Driven through the real route rather than by calling the recorder, because the
    mutation that matters is a handler that never records at all -- the unit tests
    above all keep passing while the protection is gone.
    """
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_folder_app, _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("myslot")
    slot.append("user", "hello")
    slot.drain()
    state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False, "parent_id": ""}]
    client = TestClient(TestServer(_make_folder_app(state)))
    await client.start_server()
    try:
        resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
        assert resp.status == 200, "the premise: this placement succeeded"
    finally:
        await client.close()

    assert folder_ids_filed_into(state) == {"f1"}, (
        "a durable placement must record the row, or a concurrent import's rollback "
        "deletes it once this session archives"
    )


@pytest.mark.asyncio
async def test_a_refused_placement_records_nothing(tmp_path, monkeypatch):
    """Recorded past the point that can refuse, not before it.

    A destination that does not exist is rejected, and a claim written before that
    check would spare a row nothing points at -- turning the rollback off for a row
    it is supposed to reclaim.
    """
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_folder_app, _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("myslot")
    slot.append("user", "hello")
    slot.drain()
    state._folders = []
    client = TestClient(TestServer(_make_folder_app(state)))
    await client.start_server()
    try:
        resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "gone"})
        assert resp.status == 400, "the premise: this placement was refused"
    finally:
        await client.close()

    assert folder_ids_filed_into(state) == set(), "a refused placement claims no row"


async def _refuse_unhide(state, folder_id):
    """Stand in for a folder deleted between the unlocked check and the locked one."""
    return False


@pytest.mark.asyncio
async def test_a_placement_refused_under_the_lock_records_nothing(tmp_path, monkeypatch):
    """The narrow window an earlier refusal cannot cover.

    A destination that never existed is rejected before the live field is even
    assigned, so that path says nothing about where the record belongs. This one is
    the real ordering test: the folder passes the unlocked pre-check and is gone by
    the locked re-check, so the handler assigns, rolls back and refuses. A record
    written beside the assignment would survive that rollback and spare a row
    nothing points at, switching the rollback off for a row it must reclaim.
    """
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_folder_app, _make_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_folders._unhide_folder",
        _refuse_unhide,
    )
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("myslot")
    slot.append("user", "hello")
    slot.drain()
    state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False, "parent_id": ""}]
    client = TestClient(TestServer(_make_folder_app(state)))
    await client.start_server()
    try:
        resp = await client.patch("/api/chat/slots/myslot/folder", json={"folder_id": "f1"})
        assert resp.status == 400, "the premise: the folder vanished under the lock"
    finally:
        await client.close()

    assert slot.folder_id == "", "the premise: the live field was assigned and rolled back"
    assert (
        folder_ids_filed_into(state) == set()
    ), "a placement refused after the field was assigned claims no row"


def test_the_untouched_test_reads_content_edits_and_ignores_view_state():
    """Which fields the fourth guard treats as content, per field.

    ``name``, ``parent_id``, ``project_dir`` and ``default_agent`` are content a
    person invests in. ``order``, ``collapsed`` and ``hidden`` are sidebar
    position and view state, which carry nothing a person loses when an EMPTY
    auto-created row is removed -- so they must NOT pin a row in place. Any key
    the created record does not carry counts as an edit without being named, which
    is what covers ``color``, ``icon``, ``tags`` and ``owner_app``.
    """
    base = af._new_folder("from mac", "parent", 3)
    assert af._is_untouched_arrival_row(base, name="from mac", parent_id="parent")

    for field, value in (
        ("name", "Work"),
        ("parent_id", "elsewhere"),
        ("project_dir", "/home/me/code"),
        ("default_agent", "kirocrew"),
    ):
        edited = {**base, field: value}
        assert not af._is_untouched_arrival_row(
            edited, name="from mac", parent_id="parent"
        ), f"{field} is content, so editing it must spare the row"

    for field, value in (("order", 99), ("collapsed", True), ("hidden", True)):
        moved = {**base, field: value}
        assert af._is_untouched_arrival_row(
            moved, name="from mac", parent_id="parent"
        ), f"{field} is view state, so it must not pin an empty row in place"

    for key, value in (
        ("color", "#ff0000"),
        ("icon", "\N{ROCKET}"),
        ("tags", ["t1"]),
        ("owner_app", "some-app"),
    ):
        assert not af._is_untouched_arrival_row(
            {**base, key: value}, name="from mac", parent_id="parent"
        ), f"{key} is a key a created arrival row never carries, so it is an edit"


# ── the existence re-check ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_existence_reads_committed_store_state():
    state = _store([_row("Imported")])

    assert await af.arrival_folder_exists(state, "id-Imported") is True
    assert await af.arrival_folder_exists(state, "id-gone") is False
    assert state.reads == 2, "the read must go through read_folders, not _folders"


@pytest.mark.asyncio
async def test_an_empty_id_is_never_reported_as_present():
    state = _store([{"id": "", "name": "malformed", "parent_id": ""}])
    assert await af.arrival_folder_exists(state, "") is False


@pytest.mark.asyncio
async def test_a_failed_existence_read_fails_closed_to_gone():
    """Clearing a ``folder_id`` renders the session at the top level, which the
    sidebar handles; keeping an unverified one can strand it pointing at nothing.
    So the unreadable case takes the recoverable answer."""
    state = _store([_row("Imported")])

    async def _boom(*_a, **_k):
        raise OSError("folders.json is not readable")

    state.read_folders = _boom
    assert await af.arrival_folder_exists(state, "id-Imported") is False
