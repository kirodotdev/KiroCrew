"""Chat folder records and who may write them.

The folder cap and tag check, what a folder holds (history counts, filed slots,
archived sessions), the creator mark, caller attribution and the member and
channel refusals, the create errors and ``create_folder_record``, the store-side
create the folder routes and the scaffold share. New rules about a folder record
or its writer go here.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_folders_icons import _is_single_emoji, _is_valid_folder_color
from kiro_crew.dashboard.chat_folders_paths import (
    _folder_project_overlap_denied,
    _validate_project_dir,
    _validate_steering_dirs,
)
from kiro_crew.dashboard.chat_tags import tags_write_lock, validate_folder_tag_ids
from kiro_crew.dashboard.folder_ownership import _folder_owner_app, _is_descendant
from kiro_crew.dashboard.slot_ownership import slot_not_found
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import (
    KNOWN_INTERNAL_CALLERS,
    MEMBER_CHAT_PRINCIPAL_KEY,
    refuse_unattributable_caller,
    request_origin,
)
from kiro_crew.instances.crew_ref import clean_crew_ref
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.chat_folders")


#: The ceiling on chat folders, the folder-tree counterpart to
#: :data:`~kiro_crew.dashboard.state.MAX_LIVE_SLOTS`. Folder creation had no bound
#: at all: every other create path in the dashboard tests a ceiling, so an
#: automated caller looping on this one was the single way to grow durable
#: on-disk state without limit.
#:
#: Chosen to sit far above any hand-built tree -- a person organizing chats works
#: in tens of folders, not hundreds -- so the only thing that ever reaches it is a
#: runaway loop. Tested under ``mutate_folders``, not before it: the count is only
#: authoritative while the lock is held, which is the same reason the parent is
#: re-checked and ``order`` recounted there.
MAX_CHAT_FOLDERS = 500


def _validate_folder_tags(state: DashboardState, raw: Any) -> tuple[list[str] | None, str | None]:
    """Validate a folder ``tags`` payload against the tag vocabulary.

    Returns ``(clean_ids, None)`` on success or ``(None, error)`` on rejection.
    Only the payload SHAPE is rejected (``tags`` must be an array) — matching
    ``api_chat_slot_tags``, the sibling this endpoint mirrors, unknown ids and
    non-string entries are silently FILTERED, not 400ed. That leniency is
    load-bearing: a dangling id can legitimately exist on a folder (the
    acknowledged best-effort strip failure in ``api_chat_tag_delete``), and a
    strict endpoint would make every subsequent save of that folder fail —
    the "permanently uneditable folder" class. Filtering at the write means a
    stale reference is shed on the next save instead of bricking it.
    ``clean_ids`` is deduped preserving first-seen order, with no count cap
    (vocabulary membership plus dedupe already bounds the list). An empty list
    is valid — it means "no tags", the same way an absent ``color`` means
    "default color".

    Only the payload SHAPE is owned here (``tags`` must be an array → 400,
    matching the sibling ``api_chat_slot_tags``); everything after the shape
    check DELEGATES to ``validate_folder_tag_ids`` — the single definition of
    a usable folder tag id — so the filter-not-400 leniency, dedupe, string
    guard, and the authority-gated fail-open vocabulary intersection cannot
    drift from the inheritance paths that read these same ids back. See that
    helper's docstring for why filtering (not 400) and failing open (not
    intersecting an unknown vocabulary) are both load-bearing.
    """
    if not isinstance(raw, list):
        return None, "tags must be an array"

    return validate_folder_tag_ids(raw, state), None


def _folder_history_counts(state: DashboardState) -> dict[str, int]:
    """Count on-disk (history) sessions filed in each folder, keyed by folder_id.

    Authoritative per-folder archived-session count computed from the full
    session list, NOT the paginated client history window. The sidebar uses it
    to decide whether an empty folder can be hidden (it has an archived session
    that can revive it) or must be deleted instead (nothing could revive it).
    """
    counts: dict[str, int] = {}
    if not state.conversation_log:
        return counts
    for session in state.conversation_log.list_sessions():
        fid = session.get("folder_id")
        if fid:
            counts[fid] = counts.get(fid, 0) + 1
    return counts


def _folders_with_history_counts(state: DashboardState) -> list[dict]:
    """Folders enriched with a computed, non-persisted `history_count` field."""
    counts = _folder_history_counts(state)
    return [{**f, "history_count": counts.get(f["id"], 0)} for f in state._folders]


def chat_folder_subtree_ids(state: DashboardState, folder_id: str) -> frozenset[str]:
    """*folder_id* and every chat folder nested under it, or empty when it is unknown.

    The sidebar is a tree, so a folder holds the chats filed in its sub-folders
    too. An id that names no folder in the tree yields an empty set, never the
    whole tree.
    """
    folders = getattr(state, "_folders", None)
    if not folder_id or not isinstance(folders, list):
        return frozenset()
    rows = [f for f in folders if isinstance(f, dict) and isinstance(f.get("id"), str)]
    if not any(f["id"] == folder_id for f in rows):
        return frozenset()
    return frozenset(
        f["id"] for f in rows if _is_descendant(rows, ancestor_id=folder_id, folder_id=f["id"])
    )


def live_slot_names_in_folders(state: DashboardState, folder_ids: frozenset[str]) -> set[str]:
    """Names of the open chat slots filed in any of *folder_ids*.

    Reads the live slot table, so it runs on the event loop that mutates it.
    """
    slots = getattr(state, "_slots", None)
    if not folder_ids or not isinstance(slots, dict):
        return set()
    return {
        str(name) for name, slot in list(slots.items()) if _slot_home_folder_id(slot) in folder_ids
    }


def archived_session_names_in_folders(
    state: DashboardState, folder_ids: frozenset[str]
) -> set[str]:
    """Slot names of the on-disk sessions filed in any of *folder_ids*.

    A dashboard transcript is stored as ``dashboard_<slot>`` (stacked prefixes
    from resume round-trips fold to one), so ``ledger_key`` drops the prefix to
    get the slot name an artifact's provenance records. A channel transcript's
    stem is already the folded channel key that provenance compares against, so
    it passes through unchanged. Scans the transcript directory: call it off the
    event loop.
    """
    from kiro_crew.session_ledger import ledger_key

    log = getattr(state, "conversation_log", None)
    if not folder_ids or not log:
        return set()
    names: set[str] = set()
    for session in log.list_sessions():
        if session.get("folder_id") not in folder_ids:
            continue
        key = ledger_key(str(session.get("key") or ""))
        if key:
            names.add(key)
    return names


def note_folder_filed(state: DashboardState, folder_id: str) -> None:
    """Record that a session was durably filed into *folder_id* by hand.

    Occupancy evidence for :func:`arrival_folders.discard_arrival_folders`, whose
    own guard reads LIVE slots: a person files a session into a folder, the tab
    closes, the slot is popped out of that mapping, and the row it points at then
    looks unoccupied to a concurrent import's rollback. That import created the
    row moments earlier, so the rollback would delete a placement an archived
    session still names, leaving a dangling ``folder_id``. This set is what the
    rollback consults instead, and it is written HERE -- past the durable save, so
    a placement that was refused records nothing.

    Held in memory on *state* rather than stamped on the folder. An arrival row is
    deliberately indistinguishable from a hand-made one, so a flag on the row
    would have to be written for EVERY destination and would leave bookkeeping on
    ordinary folders that a successful import is supposed to leave clean. Memory
    is also the right lifetime: the rollback this protects runs seconds later in
    this same process, and a restart has no in-flight import to roll back. The set
    holds ids, so it is bounded by the number of distinct folders filed into
    rather than by how often they are filed.

    An id is never dropped. Moving the session out again leaves the row spared,
    which errs toward keeping a folder the person can delete themselves over
    deleting one something still points at.

    Attached to *state* lazily, with the same defensive pair the rollback's own
    live-slot read uses, so the attribute costs nothing on a state that never
    files a session anywhere.
    """
    if not folder_id:
        return
    ids = getattr(state, "_folders_filed_into", None)
    if not isinstance(ids, set):
        ids = set()
        setattr(state, "_folders_filed_into", ids)
    ids.add(str(folder_id))


def folder_ids_filed_into(state: DashboardState) -> set[str]:
    """Folder ids a session was durably filed into during this process's life.

    A copy, so a caller reading it inside its own store transaction cannot edit
    the record by accident. Empty when nothing has been filed.
    """
    ids = getattr(state, "_folders_filed_into", None)
    return set(ids) if isinstance(ids, set) else set()


#: The folder field naming the agent session that created it. Present only on a
#: folder an agent's MCP call created and the person has not touched since: a
#: person renaming, moving, restyling or hiding it, filing a session into it, or
#: nesting a folder under it removes the field, and nothing ever puts it back.
#: ``chat_folder_delete`` deletes only a folder that still carries the CALLER's
#: own session key here, so a folder the person made or reused is never removed
#: by an agent. A row from before this field existed has none, and reads as the
#: person's.
CREATED_BY_SESSION = "created_by_session"


def agent_creator_key(state: DashboardState, request: web.Request) -> str:
    """The live session key an agent create is attributed to, or ``""``.

    Only an internal (MCP) request names an agent: the browser never carries
    ``X-Internal-Secret``, so a person's create is never stamped. The key must
    name a live dashboard slot, the same key ``chat_folder_delete`` later sends
    for the same session.
    """
    if request_origin(request, what="folder write", log=logger)[0] == "dashboard":
        return ""
    key = str(request.headers.get("X-Session-Key") or "").strip()
    if not key.startswith("dashboard:"):
        return ""
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict) or key[len("dashboard:") :] not in slots:
        return ""
    return key


async def _unhide_folder(
    state: DashboardState, folder_id: str, *, claim_for_person: bool = False
) -> bool:
    """Clear a folder's `hidden` flag when a session re-engages it.

    Model-B semantics: reviving or moving a session into a folder un-hides it so
    it stays visible until the user hides it again. Persists on change; the
    caller is responsible for pushing the slots update.

    Returns whether the folder EXISTS. Existence is reported from inside the
    store lock, which is the only place it can be checked without a race: a
    caller that validated against ``state._folders`` beforehand and then assigned
    can have the folder deleted in between, and would persist a placement into a
    folder that is gone.

    ``claim_for_person`` is set when the PERSON filed the session: the folder is
    then theirs, so its :data:`CREATED_BY_SESSION` mark is removed in the same
    locked step.
    """
    if not folder_id:
        return True

    def _clear(folders: list[dict[str, Any]]) -> tuple[bool, bool]:
        for f in folders:
            if f["id"] == folder_id:
                claimed = bool(claim_for_person and f.pop(CREATED_BY_SESSION, None))
                if f.get("hidden"):
                    f["hidden"] = False
                    return True, True
                if claimed:
                    return True, True
                # Present and already visible: report no change so the store is
                # not rewritten. This runs on every session move, so a needless
                # write here would be a write per move.
                return False, True
        return False, False

    return await state.mutate_folders(_clear)


# The internal callers this module recognizes on ``X-Internal-Caller`` — the
# shared set, ratcheted in ``test_chat_folder_audit_origin.py`` under this name.
_KNOWN_INTERNAL_CALLERS = KNOWN_INTERNAL_CALLERS


def _audit_origin(request: web.Request) -> tuple[str, str]:
    """SEL ``(source, caller)`` for a folder mutation.

    The rule is :func:`token_auth.request_origin`, shared with the tag routes so
    a new internal caller is classified once. The warning for an unrecognized
    caller is emitted under this module's logger, where the folder audit tests
    listen for it.
    """
    return request_origin(request, what="folder write", log=logger)


def _refuse_unattributable_caller(
    state: DashboardState, request: web.Request
) -> web.Response | None:
    """403 when the caller NAMES a dashboard slot that is gone, else None.

    The rule and its rationale are :func:`token_auth.refuse_unattributable_caller`,
    shared with the tag routes; this wrapper fixes the audit ``operation`` for the
    folder-tree writes and keeps the name the folder routes call.
    """
    return refuse_unattributable_caller(state, request, "chat.folder_write")


def member_slot_write_refused(
    state: DashboardState, request: web.Request, slot: Any, operation: str
) -> web.Response | None:
    """403/404 when a crew-MEMBER caller may not file/tag *slot*, else ``None``.

    The member analogue of the ``_app`` / ``app_owns_transcript`` fence the two
    slot-write handlers (``api_chat_slot_folder`` and
    ``chat_tags.api_chat_slot_tags``) apply to APP callers, and it MUST run
    beside that app fence: a member carries NO app claim, so
    ``effective_request_app`` returns ``""`` for it and the app fence's
    ``if request_app`` guard is falsy -- without this a member would reach the
    handler (the gate admits it) and file or tag ANY session. Shared by both
    handlers so filing and tagging cannot drift on which sessions a member owns.

    A member is recognised by the principal the chat gate stamped on the
    VERIFIED scope (``token_auth.MEMBER_CHAT_PRINCIPAL_KEY``); a non-member
    caller (person, app) makes this a no-op and the app fence beside it decides.
    An admitted member may write ONLY a slot it owns for filing:
    :func:`session_control.member_owns_slot` -- its own session or one it
    created. Anything else is the same indistinguishable 404 the app fence
    returns, so the route is not an existence oracle for sessions the member
    cannot see.
    """
    principal = str(request.get(MEMBER_CHAT_PRINCIPAL_KEY) or "")
    if not principal.startswith("member:"):
        return None
    caller_key = request.headers.get("X-Session-Key", "").strip()
    from kiro_crew.dashboard import session_control as sc

    if sc.member_owns_slot(state, slot, caller_key):
        return None
    sel().log_api_access(
        caller=principal,
        operation=operation,
        outcome="denied",
        source="member_isolation",
        resources=f"slot={getattr(slot, 'key', '')}",
        error="member can only file or tag its own or created sessions",
    )
    return slot_not_found()


def caller_home_slot(state: DashboardState, request: web.Request, principal: str) -> Any:
    """The calling session's OWN slot, when a non-person *principal* may nest from it.

    A conductor running as an app or a crew member often sits in a folder the
    person filed it in. Its workers belong under that folder, so the tree fence
    lets the principal create a child directly under the folder its own calling
    session is filed in (its "home" folder), and nowhere else in the person's
    tree. This returns the slot whose ``folder_id`` names that home, or ``None``.

    The slot must BE the caller's, bound to the same principal, or a caller
    naming someone else's session in ``X-Session-Key`` would borrow that
    session's folder:

    * an app caller: the slot's ``_app`` must be the app itself;
    * a crew member: the chat gate stamped this principal from THIS request's
      verified session key (``MEMBER_CHAT_PRINCIPAL_KEY``), so the key's slot is
      the member's own session.

    The slot object is returned, not its folder id, so the create reads
    ``folder_id`` under the folder-store lock at the moment it decides.
    """
    if not principal:
        return None
    key = str(request.headers.get("X-Session-Key") or "").strip()
    if not key.startswith("dashboard:"):
        return None
    slots = getattr(state, "_slots", None)
    slot = slots.get(key[len("dashboard:") :]) if isinstance(slots, dict) else None
    if slot is None:
        return None
    if principal.startswith("member:"):
        if str(request.get(MEMBER_CHAT_PRINCIPAL_KEY) or "") != principal:
            return None
    elif str(getattr(slot, "_app", "") or "") != principal:
        return None
    return slot


def _slot_home_folder_id(slot: Any) -> str:
    """The folder *slot* is filed in, or ``""`` for none (or no slot)."""
    folder_id = getattr(slot, "folder_id", "") if slot is not None else ""
    return folder_id if isinstance(folder_id, str) else ""


class FolderCreateError(ValueError):
    """A folder could not be created because the request was refused.

    Carries the two halves of the folder API's 400 body: ``str(exc)`` is the
    advisory prose a surface renders, ``code`` the machine-readable id it
    branches on (empty for the refusals the folder API answers without one).
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class FolderOwnershipError(FolderCreateError):
    """Refused because an app tried to nest under a folder it does not own.

    Split from :class:`FolderCreateError` because the folder API answers it
    differently (403 plus a denied SEL entry, not a plain 400), and the
    response shape belongs to the caller — so the caller must be able to tell
    this refusal apart without string-matching the message.
    """

    def __init__(self) -> None:
        super().__init__(
            "cannot create a folder inside one this app does not own",
            "folder_not_owned",
        )


class FolderNameExistsError(FolderCreateError):
    """Refused because the parent already holds a folder of this name.

    Raised only for callers that opt in with ``refuse_duplicate_name`` (an
    agent writing through the internal transport). Split out because the
    folder API answers it with 409, which the MCP path walk reads as "someone
    else made this segment first" and resolves by re-reading the tree, rather
    than as a plain validation 400.
    """

    def __init__(self) -> None:
        super().__init__(
            "a folder with this name already exists under that parent",
            "folder_name_exists",
        )


class FolderCapError(FolderCreateError):
    """Refused because the folder store is at its ceiling.

    Split from :class:`FolderCreateError` for the same reason
    :class:`FolderOwnershipError` is: the folder API answers it differently
    (429, a retryable capacity refusal, not a plain 400), so a caller must be
    able to tell it apart without string-matching the message.
    """

    def __init__(self) -> None:
        super().__init__(
            f"folder cap reached ({MAX_CHAT_FOLDERS})",
            "folder_cap_reached",
        )


# Named by the path callers import them from: a traceback or a log line renders a raised
# class as ``module.qualname``, so the owner module must not leak into it.
FolderCreateError.__module__ = "kiro_crew.dashboard.chat_folders"
FolderOwnershipError.__module__ = "kiro_crew.dashboard.chat_folders"
FolderNameExistsError.__module__ = "kiro_crew.dashboard.chat_folders"
FolderCapError.__module__ = "kiro_crew.dashboard.chat_folders"


async def create_folder_record(
    state: DashboardState,
    *,
    name: str,
    parent_id: str = "",
    project_dir: str = "",
    default_agent: str = "",
    default_destination_crew: str = "",
    color: str = "",
    icon: str = "",
    request_app: str = "",
    tags: list[str] | None = None,
    steering_dirs: list[str] | None = None,
    unique_project_dir: bool = False,
    require_resolved_project_dir: bool = False,
    refuse_duplicate_name: bool = False,
    home_slot: Any = None,
    created_by_session: str = "",
    claim_parent_for_person: bool = False,
) -> dict[str, Any]:
    """Validate one folder and append it to the store under the folders lock.

    The single create path. Callers that build folders for the user — the folder
    API below, project scaffolding — go through here, so none of them can end up
    with weaker path validation, a dangling ``parent_id``, weaker app-ownership
    isolation, or an unserialized store write than the others get. What stays
    with the caller is what differs between them: the response shape, the audit
    entry, and when to push a slots update (once per folder for a single create,
    once for a whole scaffold).

    ``request_app`` is the calling app's identity (empty when a person is
    calling): it is stamped as ``owner_app`` and gates nesting under folders
    other apps own. Never taken from a request body — a caller that could name
    its own owner could name someone else's (see ``_folder_owner_app``).

    ``tags`` is a shape-checked list of tag ids the caller wants copied onto
    every new chat filed into this folder (the folder API validates the request
    shape and answers 400 ``tags_invalid`` itself). The AUTHORITATIVE vocabulary
    intersection still runs here, under ``tags_write_lock``, at the point of
    application — the invariant every tag consumer follows (see
    api_chat_slot_tags / the channel filing): a tag deletion committing between
    the caller's shape check and this write must not be persisted onto the new
    folder, and the strip pass a deletion runs cannot see a folder that is not
    yet in the store. Included in the folder dict only when the intersection is
    non-empty — the same optional-key shape as ``color``, so a tagless folder
    keeps the record it has on disk today.

    ``icon`` is an optional explicit emoji for the folder glyph, validated
    grapheme-exact like every stored icon and included in the folder dict only
    when non-empty — the same optional-key shape as ``color``. An absent icon
    never triggers generation: creation leaves the folder on the default
    glyph, and the only path that spawns the generator is an explicit
    ``regenerate_icon`` request against an existing folder.

    Returns:
        The created folder, exactly as it was appended to the store.

    ``unique_project_dir`` makes "one folder per directory" atomic: the check
    runs inside the locked append, so two concurrent creators of the same
    ``project_dir`` cannot both observe it absent and both persist a folder —
    the loser is refused with code ``folder_project_dir_exists`` and can read
    the winner's folder after the fact. Off by default because the folder API
    has always allowed a person to point two folders at one directory by hand;
    only a caller whose own contract is "additive, skip what exists" (the
    scaffold) opts in.

    ``require_resolved_project_dir`` refuses a ``project_dir`` whose validation
    resolves to a different path than the caller named (code
    ``folder_project_dir_moved``). A caller that sets it is asserting its input
    is already canonical — the scaffold passes paths its scan just resolved and
    walked, so a resolution that lands elsewhere means a path component was
    swapped (typically for a symlink) between the scan and this create, and
    persisting the resolved target would bind the folder to a directory the
    scan never confirmed. Off by default because the folder API proper accepts
    ``~`` and symlinked paths from a person by design; resolution moving those
    is the feature, not an attack.

    ``refuse_duplicate_name`` makes "one folder of this name per parent" atomic
    the same way: the sibling test runs inside the locked append, against EVERY
    sibling whoever owns it, and a collision raises
    :class:`FolderNameExistsError`. Names compare trimmed and case-folded, the
    rule the MCP path walk matches segments by. Off by default because the
    sidebar lets a person hold two folders of one name; the folder API turns it
    on for agent callers, whose mkdir -p would otherwise fork a duplicate when
    two walks race or when the caller cannot see the existing folder. When the
    one colliding sibling already belongs to the caller's own principal, it is
    returned instead (with ``"reused": True`` on the returned copy), so a lost
    race resolves to the winner's folder in the same request.

    ``home_slot`` is the caller's own session (see :func:`caller_home_slot`).
    A non-person principal may nest directly under the folder that session is
    filed in, even when the person owns that folder. The slot's ``folder_id``
    is read under the lock, where the parent is decided.

    ``created_by_session`` is the agent session this create is attributed to
    (see :func:`agent_creator_key`); it is stored as :data:`CREATED_BY_SESSION`
    on a NEW folder only, never on a reused one. ``claim_parent_for_person`` is
    set when the person is creating: nesting into a folder claims that parent,
    so its mark is removed under the same lock as the append.

    Raises:
        FolderCreateError: if the folder was refused (unusable name, missing
            parent, unusable ``project_dir``, unknown color, non-emoji
            ``icon``, or a ``unique_project_dir`` collision).
        FolderOwnershipError: if an app tried to nest under a folder it does
            not own.
        FolderNameExistsError: if ``refuse_duplicate_name`` is set and the
            parent already holds a folder of this name.
    """

    name = name.strip()[:100]
    if not name:
        raise FolderCreateError("name required")
    if parent_id and not any(f["id"] == parent_id for f in state._folders):
        raise FolderCreateError("parent folder not found")
    requested_dir = project_dir.strip()
    # Off-loop: realpath + isdir + the sensitive-path scan touch the filesystem,
    # and the scaffold calls this once per folder in a loop, so a slow or
    # network-mounted directory would otherwise stall every other request.
    project_dir, err = await asyncio.to_thread(_validate_project_dir, requested_dir)
    if err:
        raise FolderCreateError(err)
    if require_resolved_project_dir and project_dir != requested_dir:
        # The caller vouched its path was already canonical, so a resolution
        # that lands elsewhere means the directory on disk is not the one
        # the caller confirmed — refuse rather than persist the substitute.
        raise FolderCreateError(
            "That directory was moved or replaced after the scan — re-scan and retry",
            "folder_project_dir_moved",
        )
    if project_dir:
        # Off-loop: the shared scan primes runtime paths on first use.
        conflict = await asyncio.to_thread(_folder_project_overlap_denied, project_dir)
        if conflict is not None:
            raise FolderCreateError(conflict, "workspace_overlaps_data_home")
    color = color.strip().lower()
    if color and not _is_valid_folder_color(color):
        # `code` is the contract, `error` is advisory prose (RFC 9457 3.1.3) —
        # the dashboard renders `error` verbatim into a localized UI, so a new
        # error response without an id is untranslatable by construction.
        raise FolderCreateError("color must be one of the folder palette values", "color_invalid")
    icon = icon.strip()
    if icon and not _is_single_emoji(icon):
        # Same contract shape as ``color``: a stored icon is always a single
        # grapheme-exact emoji, whichever caller created the folder.
        raise FolderCreateError("icon must be a single emoji", "icon_invalid")
    # Off-loop like project_dir: each entry's realpath + isdir + sensitive-path
    # scan touches the filesystem, and the scaffold calls this once per folder.
    resolved_steering: list[str] = []
    if steering_dirs:
        resolved_steering, steering_err = await asyncio.to_thread(
            _validate_steering_dirs, steering_dirs
        )
        if steering_err:
            raise FolderCreateError(steering_err, "steering_dirs_invalid")
    folder: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "order": len(state._folders),
        "collapsed": False,
        "hidden": False,
        "parent_id": parent_id,
        "project_dir": project_dir,
        "default_agent": default_agent,
        "default_destination_crew": clean_crew_ref(default_destination_crew),
        # Epoch seconds, a JSON number, so the sidebar and the MCP tree read it
        # with the same rule they read ``order`` by. It is what the sidebar's
        # ``created`` folder sort orders on; a row from before this key existed
        # has none and sorts as older than every stamped row.
        "created_at": time.time(),
    }
    if color:
        folder["color"] = color
    if icon:
        folder["icon"] = icon
    if resolved_steering:
        # Omitted when empty, like ``color``/``tags``: "absent means none" stays
        # the single on-disk representation.
        folder["steering_dirs"] = resolved_steering
    if request_app:
        folder["owner_app"] = request_app
    if created_by_session:
        folder[CREATED_BY_SESSION] = created_by_session

    reused: list[dict[str, Any]] = []

    def _append(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        # Re-check the parent under the lock. Its existence was validated before
        # the lock was taken, so a concurrent delete of that parent would
        # otherwise land this folder with a dangling parent_id — the same
        # pre-lock/post-lock gap the reparent path re-tests.
        parent = next((f for f in folders if f["id"] == parent_id), None) if parent_id else None
        if parent_id and parent is None:
            return False, "parent_not_found"
        # The ceiling is tested here, under the lock, for the same reason the parent
        # is re-checked here: `len(folders)` is only authoritative while the lock is
        # held, so a pre-lock test lets concurrent creators each pass a cap that is
        # already full.
        if len(folders) >= MAX_CHAT_FOLDERS:
            return False, "folder_cap_reached"
        # Nesting into a folder writes to THAT folder's child list, so an app may
        # only nest under one of its own. The top level is not a folder row and
        # so has no owner to violate — that is where an app's own tree starts.
        # Decided here rather than pre-lock because a reparent racing this
        # request can change who the parent belongs to.
        if (
            request_app
            and parent is not None
            and _folder_owner_app(parent) != request_app
            and parent_id != _slot_home_folder_id(home_slot)
        ):
            return False, "forbidden_parent"
        # Under the lock, not pre-lock: a pre-lock read is exactly the
        # check-then-act gap that lets two concurrent creators both see the
        # directory unclaimed.
        if (
            unique_project_dir
            and project_dir
            and any(str(f.get("project_dir") or "") == project_dir for f in folders)
        ):
            return False, "project_dir_exists"
        # Under the lock for the same check-then-act reason: two agents walking
        # the same path each read a tree without the segment, and only this
        # test, taken while the store is held, sees the other's append.
        if refuse_duplicate_name:
            folded = name.casefold()
            twins = [
                f
                for f in folders
                if str(f.get("parent_id") or "") == parent_id
                and str(f.get("name") or "").strip().casefold() == folded
            ]
            if len(twins) == 1 and _folder_owner_app(twins[0]) == request_app:
                reused.append(dict(twins[0], reused=True))
                return False, "reused"
            if twins:
                return False, "name_exists"
        folder["order"] = len(folders)  # recount under the lock
        if claim_parent_for_person and parent is not None:
            parent.pop(CREATED_BY_SESSION, None)
        folders.append(folder)
        return True, ""

    if tags:
        async with tags_write_lock(state):
            refreshed, _ = _validate_folder_tags(state, tags)
            if refreshed:
                folder["tags"] = refreshed
            create_err = await state.mutate_folders(_append)
    else:
        create_err = await state.mutate_folders(_append)
    if create_err == "folder_cap_reached":
        raise FolderCapError()
    if create_err == "parent_not_found":
        # The parent was deleted while this request waited for the lock.
        raise FolderCreateError("parent folder not found", "folder_parent_not_found")
    if create_err == "forbidden_parent":
        raise FolderOwnershipError()
    if create_err == "project_dir_exists":
        raise FolderCreateError(
            "a folder for this directory already exists", "folder_project_dir_exists"
        )
    if create_err == "name_exists":
        raise FolderNameExistsError()
    if create_err == "reused":
        return reused[0]
    return folder


def _caller_reaches_a_channel(request: web.Request, caller_key: str) -> bool:
    """Whether a channel can deliver turns into the session behind *caller_key*.

    The mirror and Slack-thread reading is the work-ledger gate's own
    ``_reaches_a_channel``, called rather than re-composed, so both gates
    resolve the caller's slot the same way and give one answer per session.
    It fails closed on an unreadable store.

    Two clauses sit on top of it, because the ledger reaches them elsewhere:
    a ``channel:`` key (an agent spawned for a channel thread, which
    ``is_channel_session_key`` does not name) and a channel-born slot
    (``session_control._channel_link_of``). The ledger's entry gate covers a
    channel-born caller through ``_caller_admission``; this endpoint has no
    such gate, so it reads the slot's link here, on the slot found by the
    ledger's normalisation of the key (``session_ledger.ledger_key``).
    """
    # Imported here: session_control (and the handlers that import it) import
    # this module at load.
    from kiro_crew import session_ledger
    from kiro_crew.dashboard import session_control
    from kiro_crew.dashboard.handlers.work_ledger import _reaches_a_channel

    sk = (caller_key or "").strip()
    if not sk:
        return False
    if sk.startswith("channel:") or is_channel_session_key(sk):
        return True
    if _reaches_a_channel(request, sk):
        return True
    state: DashboardState = request.app["state"]
    key = session_ledger.ledger_key(sk)
    for candidate in (sk, key, f"dashboard_{key}"):
        slot = state.get_slot(candidate)
        if slot is not None and session_control._channel_link_of(slot):
            return True
    return False


def _agent_in_channel_reachable_session(request: web.Request) -> bool:
    """A conversation resumed from a channel keeps the session's dashboard: key."""
    return _audit_origin(request)[0] != "dashboard" and _caller_reaches_a_channel(
        request, request.headers.get("X-Session-Key", "").strip()
    )
