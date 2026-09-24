"""Arrival provenance filing — file an arriving session under ``Imported``.

A session can arrive on this instance two ways: a peer pushes one over an
Instances tunnel, and a person installs one exported to a file. Both reach the
single server route :func:`~kiro_crew.dashboard.session_transfer.api_chat_slot_import`,
and both are filed here into ``Imported`` / ``from <sender>`` so "where did this
session come from" has an answer that outlives the arriving tab's title.

The decision record is
[rfc-arrival-provenance-filing.md](../../../docs/request-for-change/rfc-arrival-provenance-filing.md);
the tunnel half is a CHANGED DEFAULT for a route that has one, which is why that
document exists. The properties below are the ones that document commits to, each
written where it is enforced:

* **The destination does not depend on the body's format.** One function serves
  both arrival routes. A gzipped file from a person and a plain-JSON bundle from
  the tunnel are the same event carried by different transport, so the encoding
  must not decide placement.
* **``origin`` names a folder and confers nothing.** It is a field of an
  untrusted bundle, so it is read as a LABEL only — never as an identity, never
  compared against a credential. The authorization identity is the caller's,
  resolved by the shared ``effective_request_app`` rule and passed in.
* **An app-scoped arrival touches the folder store not at all.** It neither
  creates nor adopts: the store has a global ceiling
  (:data:`~kiro_crew.dashboard.chat_folders.MAX_CHAT_FOLDERS`), so an app token
  looping imports with distinct ``origin`` values could otherwise fill the cap
  until the person is refused a folder of their own — and adopting the person's
  folder instead would file peer content into a row they own.
* **Find-or-create is atomic.** Both levels are settled inside ONE
  ``mutate_folders`` transaction, the shape
  :func:`~kiro_crew.dashboard.channel_folders.ensure_channel_folder` already
  uses. Two arrivals from one peer racing each other cannot each observe the
  folder absent and each create one.
* **A folder is looked up in the form the store WRITES it.**
  ``create_folder_record`` trims and clips a name before appending it, so
  :func:`store_folder_name` applies the same transformation before the lookup —
  otherwise a long sender name is compared untruncated, misses the clipped row a
  previous arrival wrote, and creates a duplicate on every arrival.
* **Filing is best-effort.** A refusal — the ceiling, a store write failure —
  returns ``""`` and the session lands unfiled. The transcript is the payload and
  the grouping is convenience, so this must never fail an import that works
  today.

Folder names are ASCII English literals. A folder created here is an ordinary
sidebar row the person can rename, reparent or delete, and a name re-derived per
render from the active locale would fight that rename and would move the folder
every time the interface language changed.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.dashboard.chat_folders import MAX_CHAT_FOLDERS, folder_ids_filed_into
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The group every arriving session is filed under. An English literal, not a
#: translated string — see the module docstring.
IMPORTED_FOLDER_NAME = "Imported"

#: Prefix for the per-sender child folder. ``from laptop``, not ``laptop``, so
#: the row reads as provenance rather than as a name the person chose.
_SENDER_FOLDER_PREFIX = "from "

#: The folder store's own name clip, mirrored so a lookup compares the value the
#: store WRITES. Kept equal to ``create_folder_record``'s ``name.strip()[:100]``
#: by ``test/test_arrival_folders.py``, which reads the clip out of that
#: function's source rather than restating the number a second time.
FOLDER_NAME_MAX = 100

#: Set on a folder row once an arrival that ADOPTED it -- filed into a row it did
#: not create -- has durably landed. The rollback reads it to answer a question
#: the live-slot occupancy check cannot: an ARCHIVED session is popped out of
#: ``state._slots``, so a row it is filed into looks unoccupied, and a rollback
#: belonging to whichever import originally created that row would delete a
#: placement the archived session still points at. Recorded rather than measured
#: at rollback time on purpose -- the alternative is reading persisted sessions,
#: which is a scan this best-effort path must not carry.
#:
#: WRITTEN ON THE LANDED PATH, NOT AT ADOPTION. An adoption that never becomes a
#: session needs no protection, and a mark placed at adoption cannot be taken
#: back: two concurrent same-origin imports both failing their durable save each
#: mark the other's brand-new rows, so each rollback spares rows nothing is ever
#: filed into and the pair leaks for good.
#: :func:`mark_arrival_folder_shared` writes it past the last point that can
#: refuse the import, which is also once the slot is back in ``state._slots`` --
#: so through the retracted stretch before that the occupancy check covers the
#: row, and a delete landing in the sliver between them is the "arrival folder
#: went away"
#: case the handler's own repair already answers by leaving the session unfiled.
#:
#: An optional key, the shape ``create_folder_record`` already uses for ``color``,
#: ``icon``, ``tags`` and ``owner_app``: absent on a row no arrival ever adopted,
#: so a hand-made folder keeps the record it has on disk today.
#:
#: One-way. Once a row has been shared it is never reclaimed by a rollback again,
#: which errs toward leaving an empty row the person can delete rather than
#: toward removing one somebody is filed into.
ARRIVAL_ADOPTED_KEY = "arrival_adopted"


def store_folder_name(raw: str) -> str:
    """The name the folder store would persist for *raw*.

    Mirrors ``create_folder_record``'s own ``name.strip()[:100]``. Every lookup
    and every create in this module goes through here, so the two can never
    compare different spellings of one folder.
    """
    return (raw or "").strip()[:FOLDER_NAME_MAX]


def sender_folder_name(origin: str) -> str:
    """The per-sender folder name for *origin*, or ``""`` when there is none.

    ``""`` means the bundle named no sender, and the arrival is filed under
    :data:`IMPORTED_FOLDER_NAME` directly. Synthesising a name ("from unknown")
    would make a missing field look like a peer.
    """
    origin = (origin or "").strip()
    if not origin:
        return ""
    return store_folder_name(f"{_SENDER_FOLDER_PREFIX}{origin}")


def _find(folders: list[dict[str, Any]], name: str, parent_id: str) -> dict[str, Any] | None:
    """The folder called *name* under *parent_id*, or ``None``.

    Matched case-insensitively on the STORED name so a person who fixed the
    capitalisation still owns the folder the next arrival lands in, and scoped to
    *parent_id* so an unrelated top-level ``from laptop`` is not mistaken for the
    child of ``Imported``.
    """
    target = name.strip().lower()
    if not target:
        return None
    for folder in folders:
        if str(folder.get("name", "")).strip().lower() != target:
            continue
        if str(folder.get("parent_id", "") or "") == parent_id:
            return folder
    return None


def _mark_adopted(folder: dict[str, Any]) -> bool:
    """Record that an arrival adopted *folder*. True when this call changed it.

    Returns False on a row already marked, so a repeat adoption does not report a
    store change and make the transaction write for nothing.
    """
    if folder.get(ARRIVAL_ADOPTED_KEY):
        return False
    folder[ARRIVAL_ADOPTED_KEY] = True
    return True


def _new_folder(name: str, parent_id: str, order: int) -> dict[str, Any]:
    """A folder record shaped exactly like ``create_folder_record``'s output.

    The optional keys that function adds only when non-empty (``color``,
    ``icon``, ``tags``, ``owner_app``) are absent here by the same rule: an
    arrival names no colour, generates no icon, inherits no tags, and belongs to
    the person, so a folder it creates carries the record a hand-made one does.
    """
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "order": order,
        "collapsed": False,
        "hidden": False,
        "parent_id": parent_id,
        "project_dir": "",
        "default_agent": "",
        # Same stamp ``create_folder_record`` writes: the sidebar's ``created``
        # folder sort reads it, and an arrival row must sort like a hand-made one.
        "created_at": time.time(),
    }


#: The fields of a created arrival row that carry CONTENT a person can invest in.
#: Compared by :func:`_is_untouched_arrival_row`; ``id`` and ``order`` are left out
#: because a row's identity and its sidebar position are not content, and so are
#: ``collapsed`` and ``hidden``, which are view state that carries nothing a person
#: loses when an EMPTY auto-created row is removed.
_ARRIVAL_ROW_CONTENT = ("name", "parent_id", "project_dir", "default_agent")


def _is_untouched_arrival_row(folder: dict[str, Any], *, name: str, parent_id: str) -> bool:
    """Whether *folder* still reads as the row :func:`_new_folder` wrote.

    The rollback's fourth guard. The first three ask whether somebody else is
    USING the row; this one asks whether somebody has EDITED it, which the other
    three cannot see: a person who renames, recolours, re-icons, re-tags, moves or
    points a brand-new arrival folder somewhere leaves no session filed into it,
    no adopt marker and no surviving child, so all three pass and the edit is
    deleted with the row.

    Two halves. The named content fields must still equal what
    :func:`_new_folder` emits for this *name* and *parent_id*; and the row must
    carry NO key that function does not emit, which is what covers ``color``,
    ``icon``, ``tags`` and ``owner_app`` without naming them -- ``_new_folder``'s
    own docstring is the anchor, since it states an arrival row carries none of
    them. A key added to the folder record later is covered by the same test on
    the day it is added rather than needing this list extended.

    A case-only rename counts as an edit here, even though :func:`_find` treats it
    as the same folder on purpose so a person who fixed the capitalisation still
    owns where the next arrival lands. Lookup wants those to be one folder;
    deletion wants to know a person touched this one.
    """
    expected = _new_folder(name, parent_id, 0)
    for key in _ARRIVAL_ROW_CONTENT:
        if str(folder.get(key, "") or "") != str(expected.get(key, "") or ""):
            return False
    return all(key in expected for key in folder)


class ArrivalFiling(NamedTuple):
    """Where an arrival was filed, and which rows the filing itself created.

    ``created_ids`` exists so a failed import can unwind exactly its own rows and
    nothing else. It is a strict subset of the filing: a folder that was ADOPTED
    is absent from it, because the person already owned that row and a failed
    import has no business deleting it. Parent before child, the order the
    resolver appends them in, so an unwinding caller walks it in reverse.

    ``created_rows`` is the same rows plus the snapshot the rollback compares
    against: ``(id, name, parent_id)`` as this resolver WROTE them, so
    :func:`discard_arrival_folders` can tell a row still holding what an arrival
    created from one a person has since edited. Carried rather than stamped on the
    row, which keeps the folder record identical to a hand-made one and leaves
    nothing behind in the store when the import succeeds. It defaults to empty,
    and an empty snapshot deletes NOTHING -- a filing that created no rows and a
    caller that lost the snapshot both reach the safe answer.
    """

    folder_id: str
    created_ids: tuple[str, ...]
    created_rows: tuple[tuple[str, str, str], ...] = ()
    hidden_ids: tuple[str, ...] = ()


async def arrival_folder_id(
    state: "DashboardState", *, origin: str, request_app: str
) -> ArrivalFiling:
    """The folder an arriving session is filed into, creating it if needed.

    Returns an :class:`ArrivalFiling`: the folder id, or ``""`` to leave the
    session unfiled, plus the ids this call CREATED. Never raises: every refusal
    path answers an empty filing (see the module docstring's best-effort
    property), so a caller can assign the result unconditionally.

    *request_app* is the caller's app identity from the shared
    :func:`~kiro_crew.dashboard.token_auth.effective_request_app` rule, and must
    never come from the request body. A non-empty value answers an empty filing
    before the store is read at all.

    **Call this only once the slot exists.** The import handler re-checks the
    live-slot cap after its last ``await`` and can answer ``429`` there; a folder
    created in front of that check is left behind when it fires, which is the
    same store exhaustion with a narrower trigger.

    **A caller that can still fail owes a rollback.** The row is committed here,
    before the transcript's durable save, so every later failure path must pass
    ``created_ids`` to :func:`discard_arrival_folders` or leave an empty folder
    behind. That is why the ids are returned rather than kept private.

    Both levels are resolved in ONE transaction. ``Imported`` is found or
    created, then ``from <sender>`` beneath it, so a concurrent delete of the
    parent cannot land between the two and leave the child with a dangling
    ``parent_id``.
    """
    if request_app:
        # An app arrival is never filed. Not even by adopting an existing folder:
        # that would file peer content into a row the person owns, and the
        # ceiling argument in the module docstring is about what an app can make
        # the store do, not only about what it can name.
        #
        # Audited, not merely logged at debug. ``AUTOSDE.yaml``'s
        # ``backend-security-controls`` wants a SEL event for every permission
        # decision, and this module already records the GRANT below — auditing
        # one side of a decision is the gap, because an auditor filtering
        # ``source="session_import"`` would see every folder an arrival created
        # and no trace of the arrivals refused one. Same ``operation`` as the
        # grant so the pair reads as one decision with two outcomes, and the
        # app-isolation reason travels in ``error`` rather than splitting the
        # pathway in ``source``.
        #
        # No dedup window, unlike the per-frame event gate: this is one event per
        # import request, a rate the handler's live-slot cap already bounds.
        #
        # Non-critical on purpose (the default): a SEL write failure swallows and
        # warns rather than raising, which is what keeps this function's
        # never-raises contract true. Filing is best-effort, so an unwritable
        # audit log must not fail an import whose transcript landed.
        sel().log_api_access(
            caller=request_app,
            operation="chat.arrival_folder_create",
            outcome="denied",
            source="session_import",
            resources=f"origin={origin}",
            error="an app-scoped arrival is never filed",
        )
        logger.debug(
            "arrival folder: app-scoped import from %r leaves the session unfiled", request_app
        )
        return ArrivalFiling("", ())

    group_name = store_folder_name(IMPORTED_FOLDER_NAME)
    child_name = sender_folder_name(origin)
    created: list[tuple[str, str, str]] = []
    hidden_rows: list[str] = []

    def _resolve(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        created.clear()
        hidden_rows.clear()
        changed = False
        group = _find(folders, group_name, "")
        if group is None:
            # Both rows are new: no child of ``Imported`` can exist while
            # ``Imported`` itself does not. So the ceiling is tested against the
            # PAIR, which is what stops it half-landing — creating the group and
            # then being refused the child would leave a bare ``Imported`` folder
            # no arrival is filed into.
            needed = 2 if child_name else 1
            if len(folders) + needed > MAX_CHAT_FOLDERS:
                # Refused under the lock, where ``len(folders)`` is authoritative
                # — the same reason ``create_folder_record`` tests the ceiling
                # inside ``mutate_folders`` rather than before it.
                return False, ""
            group = _new_folder(group_name, "", len(folders))
            folders.append(group)
            created.append((str(group["id"]), group_name, ""))
            changed = True
        else:
            # ADOPTED: the row already existed, so this arrival is sharing a
            # placement with whoever else is filed into it. NOT recorded here --
            # see :data:`ARRIVAL_ADOPTED_KEY`. An adoption that never becomes a
            # session needs no protection, and a mark written here could not be
            # taken back when this import failed.
            if group.get("hidden"):
                # DEFERRED, not applied. Re-engaging a hidden folder un-hides it,
                # matching the folder CRUD handler's unhide-on-assign rule -- but
                # this transaction persists, adopted rows are absent from
                # ``created``, and so no rollback can put the flag back. Applied
                # here, a later failure leaves the person's visibility preference
                # flipped by an import that never landed. Recorded for the landed
                # path instead, which is where every other write this filing owes
                # an adopted row already happens.
                hidden_rows.append(str(group.get("id", "")))
        group_id = str(group.get("id", ""))
        if not child_name:
            # No sender to name: file directly under the group.
            return changed, group_id
        child = _find(folders, child_name, group_id)
        if child is None:
            if len(folders) >= MAX_CHAT_FOLDERS:
                # Reachable only when ``Imported`` already existed and the store
                # is exactly full: the group-absent branch above already refused
                # the pair. File under the group, which is a real destination —
                # refusing here would discard a placement that costs no new row.
                return changed, group_id
            child = _new_folder(child_name, group_id, len(folders))
            folders.append(child)
            created.append((str(child["id"]), child_name, group_id))
            changed = True
        else:
            # ADOPTED, the same case as the group above, and deferred to the same
            # place: on the landed path, not here.
            if child.get("hidden"):
                hidden_rows.append(str(child.get("id", "")))
        return changed, str(child.get("id", ""))

    try:
        folder_id = await state.mutate_folders(_resolve)
    except Exception:  # noqa: BLE001 — filing must never fail an import
        # ``mutate_folders`` confirms the write and restores the in-memory list
        # before re-raising, both inside the store lock, so there is nothing to
        # undo here and no window in which an unpersisted folder was visible.
        logger.warning("arrival folder: folder store write raised", exc_info=True)
        return ArrivalFiling("", ())
    for created_id, created_name, _created_parent in created:
        sel().log_api_access(
            caller="dashboard",
            operation="chat.arrival_folder_create",
            outcome="allowed",
            source="session_import",
            resources=created_id,
        )
        logger.info("Created arrival folder %r (%s)", created_name, created_id)
    # Parent before child, the order ``_resolve`` appends them in, so a caller
    # unwinding them walks the tuple in REVERSE and meets the child first.
    return ArrivalFiling(
        str(folder_id or ""),
        tuple(cid for cid, _, _ in created),
        tuple(created),
        tuple(dict.fromkeys(fid for fid in hidden_rows if fid)),
    )


async def mark_arrival_folder_shared(
    state: "DashboardState", folder_id: str, *, unhide: Sequence[str] = ()
) -> bool:
    """Record that an arrival which ADOPTED *folder_id* has durably landed.

    Call it for the destination of a filing this import did not create, and only
    past the last point that can refuse the import -- see
    :data:`ARRIVAL_ADOPTED_KEY` for why the mark belongs here rather than in the
    resolver. Only the DESTINATION is marked: an adopted PARENT keeps a surviving
    child, which is the second guard :func:`discard_arrival_folders` already
    applies.

    *unhide* is :attr:`ArrivalFiling.hidden_ids` -- adopted rows the filing found
    HIDDEN. Re-engaging a hidden folder un-hides it, matching the folder CRUD
    handler's unhide-on-assign rule, but the flip is applied HERE rather than in
    the resolving transaction: adopted rows are absent from ``created_ids``, so no
    rollback can put the flag back, and an import that fails after flipping it
    leaves the person's visibility preference changed by an arrival that never
    landed. Both writes share this one transaction, so a landed import costs no
    extra round trip for them.

    Never raises, and never retried. Returns whether the MARK is recorded, which
    the caller MUST act on: ``False`` means this session's placement has no
    protection, because the mark is the only thing that spares an adopted row once
    the session archives out of the live-slot occupancy read. A concurrent
    creator's rollback can then reclaim the row while the transcript still points
    at it, leaving a dangling ``folder_id`` -- so the caller unfiles the session
    instead, which is the recoverable state the folder-gone repair already
    produces. Reporting rather than raising keeps filing from failing an import
    whose transcript has landed.

    ``False`` covers both a write that failed and a row that was already gone: in
    either case nothing is protecting the placement, and the remedy is the same.
    An empty *folder_id* reports ``True`` -- there is no placement to protect --
    even when *unhide* work was requested, because a failed unhide leaves a row
    merely hidden, which the person can reverse and which costs the placement
    nothing.
    """
    unhide_ids = {fid for fid in unhide if fid}
    if not folder_id and not unhide_ids:
        return True

    def _mark(folders: list[dict[str, Any]]) -> tuple[bool, bool]:
        changed = False
        found = not folder_id
        for folder in folders:
            fid = str(folder.get("id", ""))
            if fid in unhide_ids and folder.get("hidden"):
                folder["hidden"] = False
                changed = True
            if folder_id and fid == folder_id:
                changed = _mark_adopted(folder) or changed
                found = True
        return changed, found

    try:
        found = await state.mutate_folders(_mark)
    except Exception:  # noqa: BLE001 — filing must never fail a landed import
        logger.warning("arrival folder: shared-row mark write raised", exc_info=True)
        return not folder_id
    if not found:
        logger.debug("arrival folder: %s was gone before the shared-row mark", folder_id)
        return False
    return True


async def discard_arrival_folders(
    state: "DashboardState", created_rows: Sequence[tuple[str, str, str]]
) -> None:
    """Delete folders an import CREATED, once that import has failed.

    The folder is committed before the transcript's durable save, so every
    failure path after that point would otherwise leave an empty ``Imported`` /
    ``from <sender>`` row behind. Pass
    :attr:`ArrivalFiling.created_rows` — only rows THIS import created, never a
    row it adopted, because adopting means the person already owned it. Each entry
    is ``(id, name, parent_id)`` as the resolver wrote it, which is what the
    fourth guard compares against; an empty sequence deletes nothing.

    Four guards, all inside the one transaction so no answer can go stale between
    the check and the delete. The first three ask whether somebody else is USING
    the row, the fourth whether somebody has EDITED it:

    * **Still empty of sessions.** A concurrent arrival, or a person moving a
      session, can have filed into the row between its creation and this call.
      Deleting it then would unfile somebody else's session, so a row any slot
      points at is LEFT ALONE.
    * **Still empty of folders.** A row whose child survived — because the child
      was adopted rather than created — keeps that child from being orphaned.

    Walked in REVERSE so the child is considered before its parent; once the
    child goes the parent satisfies the second guard on the same pass.

    Never raises, and never partially reports: a store failure leaves the rows in
    place, which is the state that existed before this function was written. The
    caller is already answering a failure, so nothing it does depends on the
    outcome.

    **Cancellation is not covered.** The handler's ``CancelledError`` arm rolls
    back synchronously on purpose — awaiting inside a cancelled task is not
    dependable — and this is async because the store lock is. A shutdown or
    disconnect mid-import can still leave a row behind.
    """
    wanted = [(fid, name, parent) for fid, name, parent in created_rows if fid]
    if not wanted:
        return

    def _discard(folders: list[dict[str, Any]]) -> tuple[bool, list[str]]:
        # Read the occupancy INSIDE the transaction, not before it. Computed
        # outside, this snapshot could be taken and then a session filed into one
        # of these rows before the delete ran, so the guard would pass on a row
        # that had since become occupied -- the precise staleness the enclosing
        # lock exists to rule out.
        filed_on = {
            str(getattr(slot, "folder_id", "") or "")
            for slot in getattr(state, "_slots", {}).values()
        }
        # WHAT THE READ ABOVE CANNOT SEE. It reads LIVE slots, and an archived
        # session is popped out of that mapping, so a row an archived session is
        # filed into looks unoccupied, and the parent guard below only spares a row
        # with a surviving CHILD. Two archived placements reach that, and each has
        # its own closure here, neither of which reads persisted sessions.
        #
        # One: a session a PERSON filed into this row by hand and then closed. The
        # folder handler records the id past its own durable save
        # (:func:`chat_folders.note_folder_filed`), so a refused placement leaves
        # nothing. Unioned into the same set because the question it answers is
        # identical -- something points at this row.
        filed_on |= folder_ids_filed_into(state)
        # Two: a row a second same-sender import ADOPTED and then archived. The
        # mark is written once that import has LANDED
        # (:data:`ARRIVAL_ADOPTED_KEY`), and an import that FAILED leaves no mark,
        # so this rollback still reclaims what it created. One pass to build the
        # set, the same shape as ``filed_on``, then the per-row test below is a
        # membership check.
        adopted = {str(f.get("id", "")) for f in folders if f.get(ARRIVAL_ADOPTED_KEY)}
        removed: list[str] = []
        by_id = {str(f.get("id", "")): f for f in folders}
        for fid, name, parent in reversed(wanted):
            if fid in filed_on:
                continue
            if fid in adopted:
                continue
            if any(str(f.get("parent_id", "") or "") == fid for f in folders):
                continue
            # THE FOURTH GUARD: a row a person has EDITED is left alone. The three
            # above ask whether somebody else is USING this row, and none of them
            # can see an edit: renaming, recolouring, re-iconing, re-tagging,
            # moving or re-pointing a brand-new arrival folder files no session
            # into it, writes no adopt marker and leaves no surviving child, so
            # all three pass and the edit would be deleted along with the row.
            #
            # The window is not the single failing save it first looks like. This
            # rollback runs from three call sites, and the last of them is the
            # finalization tail's delete-witness refusal, which sits past the
            # transcript save, the folder-existence await and the shared-row mark.
            # A person therefore has the whole tail, awaits included, to rename a
            # row that is already visible in their sidebar.
            #
            # Compared against the snapshot the RESOLVER recorded rather than
            # anything stamped on the row, so a surviving row's record stays
            # identical to a hand-made folder's and a successful import leaves no
            # bookkeeping behind. A row that vanished is absent from ``by_id`` and
            # needs no guard: the delete below is a no-op and reports nothing.
            row = by_id.get(fid)
            if row is not None and not _is_untouched_arrival_row(row, name=name, parent_id=parent):
                logger.info(
                    "arrival folder: %s was edited after this import created it; "
                    "leaving it in place rather than deleting the change",
                    fid,
                )
                continue
            before = len(folders)
            folders[:] = [f for f in folders if str(f.get("id", "")) != fid]
            if len(folders) != before:
                removed.append(fid)
        return bool(removed), removed

    try:
        removed = await state.mutate_folders(_discard)
    except Exception:  # noqa: BLE001 — a rollback must never mask the real failure
        logger.warning("arrival folder: rollback write raised", exc_info=True)
        return
    for fid in removed or []:
        sel().log_api_access(
            caller="dashboard",
            operation="chat.arrival_folder_create",
            outcome="rolled_back",
            source="session_import",
            resources=fid,
            error="the import that created this folder failed",
        )
        logger.info("Rolled back arrival folder %s after a failed import", fid)


async def arrival_folder_exists(state: "DashboardState", folder_id: str) -> bool:
    """Whether *folder_id* is still a folder in COMMITTED store state.

    Read through ``read_folders`` so it sees only committed rows: an unlocked
    read can land mid-transaction and report a folder whose write is then rolled
    back, which is exactly the dangling id this check exists to prevent.

    The import handler calls this twice, and both calls are load-bearing. Once
    immediately before its durable save, so a folder deleted while the arrival
    was writing Layer B is not persisted onto the slot. Once after the slot is
    registered in ``state._slots`` again, because for the whole finalisation
    stretch the slot is RETRACTED from that mapping and the folder delete
    handler's unfile sweep — which iterates ``state._slots.values()`` — cannot
    see it. Without the second call a delete landing in that window leaves the
    session pointing at a row that is gone.
    """
    if not folder_id:
        return False

    def _read(folders: list[dict[str, Any]]) -> bool:
        return any(str(f.get("id", "")) == folder_id for f in folders)

    try:
        return await state.read_folders(_read)
    except Exception:  # noqa: BLE001 — a repair check must never fail an import
        # Fail CLOSED to "gone": clearing a folder_id renders the session at the
        # top level, which the sidebar handles, while keeping an unverified one
        # can strand it pointing at nothing.
        logger.warning("arrival folder: existence read raised for %s", folder_id, exc_info=True)
        return False
