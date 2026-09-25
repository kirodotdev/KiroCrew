"""Fork session — copy messages into a new tab."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop, session_was_deleted
from kiro_crew.dashboard.chat_utils import (
    _sync_dashboard_slots,
    drained_to_thread,
    effective_session_key,
    history_corpus_unreadable,
    slot_history_key,
)
from kiro_crew.dashboard.handlers.memory import _store_unavailable_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.state import (
    _TRANSIENT_ROLES,
    MAX_LIVE_SLOTS,
    VALID_MEMORY_MODES,
    DashboardState,
    request_slot_origin,
)
from kiro_crew.execution_context import clear_session_execution
from kiro_crew.history import carry_provenance
from kiro_crew.history_projection import drop_persisted_tail_prefix as _drop_persisted_tail_prefix
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot

logger = logging.getLogger(__name__)

_FORK_TITLE_MARKER = "↳ "

# Attempts to land a transcript read and the unpersisted tail on ONE consistent
# view of the slot. Matches session_transfer._SNAPSHOT_ATTEMPTS and
# chat_persistence._FLUSH_SNAPSHOT_RETRIES, which bound the same race.
_SNAPSHOT_ATTEMPTS = 4

# Fork direction: "head" copies messages up to and including the fork point
# (the default); "tail" copies only the messages after it.
_FORK_DIRECTION_HEAD = "head"
_FORK_DIRECTION_TAIL = "tail"
_FORK_DIRECTIONS = (_FORK_DIRECTION_HEAD, _FORK_DIRECTION_TAIL)
_MAX_MESSAGE_ID_CHARS = 256

# System note appended to a promoted slot's transcript (see
# ``api_chat_slot_promote``), so a reader of the new persistent session can
# tell where it came from without cross-referencing the SEL log. Backend
# transcript notices are hardcoded English, matching every other synthetic
# system message this module and ``chat_utils``/``state.py`` append (e.g. the
# auto-compact notice) -- the frontend i18n catalog only covers UI chrome, not
# transcript content that already gets written to disk in one language.
_PROMOTION_NOTE = (
    "Kept from a private chat: this conversation is now a regular, persistent "
    "session. Memory writes are allowed from here on, and this transcript will "
    "be included in future history search and summaries."
)
_PROMOTION_NOTE_TEMPORARY_SUFFIX = (
    " The turns above ran in temporary mode, without reading stored memory, so "
    "they may not reflect the context a regular session would have had."
)


def drop_persisted_tail_prefix(full_disk: list[dict], tail: list[dict]) -> list[dict]:
    """Re-exported from ``history_projection``, which owns the identity rule.

    Kept importable from here because this module was its first consumer and is
    where callers and tests reach for it.
    """
    return _drop_persisted_tail_prefix(full_disk, tail)


def _fork_execution_context(
    session_key: str,
    agent: str,
    recorded_store: str,
    memory_mode: str,
):
    """Capture the parent's execution without opening learned memory."""
    from kiro_crew.execution_context import (
        ExecutionContext,
        MemoryStoreRef,
        read_session_execution,
    )
    from kiro_crew.memory_stores import UnknownMemoryStore, require_memory_store

    execution = read_session_execution(session_key)
    if execution is None:
        cfg = KiroCrewConfig.load()
        store = require_memory_store(
            recorded_store or "default", config=cfg, require_directory=False
        )
        if getattr(cfg.memory_stores.get(store), "memory_version", 1) == 2:
            raise UnknownMemoryStore("The fork source's execution identity is unavailable")
        execution = ExecutionContext(None, MemoryStoreRef(store), "template", agent or "kirocrew")
    if recorded_store and (recorded_store or "default") != execution.store.store_id:
        raise UnknownMemoryStore("The fork source's recorded memory binding has changed")
    return execution.with_mode(memory_mode)


def _bind_fork_execution(source, child_key: str, execution, child_mode: str | None = None) -> None:
    from kiro_crew.execution_context import bind_session_execution
    from kiro_crew.memory_stores import UnknownMemoryStore

    if _fork_execution_context(*source) != execution:
        raise UnknownMemoryStore("The fork source's execution changed")
    # Promotion is the one fork that LOOSENS the mode: validate against the
    # source's real (ephemeral) execution above, then bind the child with the
    # persistent mode it is actually becoming, so its persisted metadata and
    # retention gate read back as a regular session rather than the private one
    # it was copied from. ``replace`` forces the mode directly -- ``with_mode``
    # only ever TIGHTENS (stricter_memory_mode), so it could never turn an
    # incognito/temporary parent into a persistent child.
    if child_mode is not None:
        execution = replace(execution, memory_mode=child_mode)
    bind_session_execution(child_key, execution)


#: SEL operation name the human fork route records under. The session-control
#: route passes its own so the two entry points stay distinguishable in the audit.
FORK_AUDIT_OPERATION = "chat.slot_fork"


@dataclass(frozen=True)
class ForkSource:
    """A fork parent with its memory identity frozen at the moment it was checked.

    ``identity`` is the six-tuple :func:`fork_slot` re-compares the live slot
    against before binding and again before copying, so a parent whose agent,
    store, mode or WORKSPACE moved under the fork is refused rather than copied.
    Workspace is in the tuple because the child is born in ``slot.workspace``
    read live: an agent caller's containment check (``authorize_target``) ran
    against the workspace the source had at the time, and a concurrent owner
    switch of the source (``api_chat_slot_workspace``) would otherwise carry the
    transcript into a workspace that check never admitted.
    """

    slot: "_ChatSlot"
    execution: Any
    identity: tuple[str, str, str, str, str, str]


@dataclass(frozen=True)
class ForkResult:
    """What :func:`fork_slot` hands back once the child is persisted and acknowledged."""

    slot: "_ChatSlot"
    messages: int
    direction: str


async def resolve_fork_source(
    slot: "_ChatSlot", *, audit_caller: str, audit_operation: str = FORK_AUDIT_OPERATION
) -> "ForkSource | web.Response":
    """Freeze the source's memory identity before anything else is read.

    The first half of a fork, split from :func:`fork_slot` so the human route
    keeps its refusal precedence (a source refused here is refused before the
    request body is even parsed) and the session-control route can run the same
    check without a request. A refusal is returned as the finished response,
    which is the shape every refusal in this module has; callers that are not
    HTTP handlers translate it (``session_control.fork_session``).
    """
    # The child inherits the parent's mode, so the parent's value is what the
    # slot constructor validates against ``VALID_MEMORY_MODES``. The API checks
    # the field on the way in, but rehydration copies the transcript header's
    # ``memory_mode`` onto the slot as written, so a hand-edited or partially
    # written header can leave an unrecognised value on a live parent. That
    # value is refused HERE, with a code and before any child exists, rather
    # than raising out of ``_ChatSlot.__init__`` as a 500. Fail closed: a mode
    # this code cannot read is a memory boundary it cannot honour.
    inherited_memory_mode = slot.memory_mode
    if inherited_memory_mode not in VALID_MEMORY_MODES:
        sel().log_api_access(
            caller=audit_caller,
            operation=audit_operation,
            outcome="denied",
            source="dashboard",
            resources=f"slot={slot.key},memory_mode={inherited_memory_mode!r}",
            error="source slot memory_mode is not a recognised mode",
        )
        return web.json_response(
            {
                "error": "the source session's memory mode is not recognised",
                "code": "fork_source_memory_mode_invalid",
            },
            status=409,
        )

    source_memory_identity = (
        effective_session_key(slot),
        slot.agent,
        slot.memory_store,
        slot.memory_mode,
        slot_history_key(slot),
        str(getattr(slot, "workspace", "default") or "default"),
    )

    try:
        inherited_execution = await asyncio.to_thread(
            _fork_execution_context, *source_memory_identity[:4]
        )
    except (OSError, ValueError) as exc:
        return _store_unavailable_response(source_memory_identity[2], exc)
    return ForkSource(slot=slot, execution=inherited_execution, identity=source_memory_identity)


async def api_chat_slot_fork(
    request: web.Request, *, _promote_from_ephemeral: bool = False
) -> web.Response:
    """POST /api/chat/slots/{slot}/fork — fork session into a new tab.

    With ``direction="head"`` (default) copies messages up to and including
    ``at_message_index``. With ``direction="tail"`` copies
    only the messages after ``at_message_index``; the head is dropped.
    An optional ``prompt`` is returned so the frontend can send it.

    Body: ``{ at_message_index?: number, at_message_id?: string, prompt?: string,
    mode?: string, direction?: "head"|"tail" }``

    ``_promote_from_ephemeral`` is an internal-only, keyword-only escape
    hatch -- never reachable from an HTTP body -- and is the ONE consent-gated
    exception to the mode-inheritance rule below (an ordinary fork always
    inherits the parent's ``memory_mode``; this forces the child to
    ``persistent`` regardless of the parent's). It is set exclusively by
    :func:`api_chat_slot_promote` (``POST .../promote``), which is itself a
    dashboard-only, human-initiated action with no MCP or CLI surface (see
    that function's docstring and
    ``docs/system-specs/modules/history.md`` § "Promoting an ephemeral
    session"). When set, the whole transcript is copied (any body field
    narrowing the scope is ignored), and the new slot is titled and
    annotated as a promotion rather than a fork.
    """
    _operation = "chat.slot_promote" if _promote_from_ephemeral else "chat.slot_fork"

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    request_app = request.get("app", "")
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # Rate/resource guard: reject if we're already at the cap. Counts slots still
    # under construction too (``live_slot_count``): the import path retracts a
    # slot from ``_slots`` while it is built, and those are allocated memory this
    # cap would otherwise ignore.
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation=_operation,
            outcome="denied",
            source="rate_limit",
            resources=f"slot={name},slot_count={state.live_slot_count()}",
            error="slot cap reached",
        )
        return web.json_response(
            {
                "error": f"slot cap reached ({MAX_LIVE_SLOTS})",
                "code": "slot_cap_reached",
            },
            status=429,
        )

    # App ownership check (App Kit §5.2)
    if request_app:
        if not slot._app:
            sel().log_api_access(
                caller=request_app,
                operation=_operation,
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app cannot fork unscoped slots",
            )
            return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
        if slot._app != request_app:
            sel().log_api_access(
                caller=request_app,
                operation=_operation,
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app does not own this slot",
            )
            # Return 404 (not 403) so a slot owned by another app / an unscoped
            # slot is indistinguishable from a non-existent one — prevents an
            # app-scoped caller enumerating slots across the isolation boundary
            # (CWE-204). The true reason is recorded server-side via SEL above.
            return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    source = await resolve_fork_source(slot, audit_caller=request_app or "dashboard")
    if isinstance(source, web.Response):
        return source

    # Restricted forks copy only the live conversation and inherit the parent's
    # mode before any row is copied. Neither branch persists restricted bodies,
    # and the request cannot loosen the inherited mode.
    # Incognito and temporary sessions fork like any other. Nothing about a fork
    # engages what those modes actually guarantee -- no consolidation or lessons
    # (``is_restricted``), no memory-context injection (``blocks_reads``) -- and
    # the transcript being copied is already on disk: ``_save_slot_to_history``
    # has no ``memory_mode`` gate, so the parent's JSONL holds it for tab recovery
    # (see docs/system-specs/modules/history.md). A refusal here would buy no
    # privacy; it would only force the user to reselect the mode and lose the
    # conversation.
    #
    # The one thing an ORDINARY fork must never do is LOOSEN the mode: copying
    # an incognito transcript into a persistent slot would hand content the
    # user marked no-write to consolidation. So the child inherits the parent's
    # mode below, and the request body carries no way to pick one -- promotion
    # (below) is the one deliberate, consent-gated exception to that rule.
    if _promote_from_ephemeral and slot.memory_mode == "persistent":
        # The dashboard only offers "Keep this chat" on an ephemeral slot; a
        # persistent slot reaching here has nothing to promote.
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation=_operation,
            outcome="denied",
            source="dashboard",
            resources=f"slot={name},memory_mode={slot.memory_mode}",
            error="slot already persistent; nothing to promote",
        )
        return web.json_response(
            {
                "error": "this session is already persistent",
                "code": "slot_already_persistent",
            },
            status=400,
        )
    if request.body_exists:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "invalid JSON body", "code": "invalid_json"}, status=400
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"error": "body must be a JSON object", "code": "body_not_object"},
                status=400,
            )
    else:
        body = {}
    if _promote_from_ephemeral:
        # Promotion always copies the WHOLE transcript with no prompt or mode
        # override -- the confirmation dialog promises "everything is kept",
        # so no body field may narrow that scope.
        body = {}
    at_index = body.get("at_message_index")
    at_message_id = body.get("at_message_id")
    if at_message_id is not None and (
        not isinstance(at_message_id, str)
        or not at_message_id.strip()
        or len(at_message_id) > _MAX_MESSAGE_ID_CHARS
    ):
        return web.json_response(
            {
                "error": (
                    "at_message_id must be a non-empty string of at most "
                    f"{_MAX_MESSAGE_ID_CHARS} characters"
                ),
                "code": "invalid_field_type",
            },
            status=400,
        )
    prompt = body.get("prompt")
    mode_override = body.get("mode")
    if mode_override is not None and mode_override not in ("", "orchestrator"):
        return web.json_response(
            {
                "error": "mode must be '' or 'orchestrator'",
                "code": "invalid_mode",
            },
            status=400,
        )
    direction = body.get("direction", _FORK_DIRECTION_HEAD)
    if direction not in _FORK_DIRECTIONS:
        return web.json_response(
            {
                "error": f"direction must be one of {list(_FORK_DIRECTIONS)}",
                "code": "invalid_direction",
            },
            status=400,
        )
    if direction == _FORK_DIRECTION_TAIL and not KiroCrewConfig.load().dashboard.tail_fork_enabled:
        # Server-side gate: tail-fork requested but disabled in config —
        # fall back to a normal head-fork rather than reject the request outright.
        # outcome="allowed" (not "denied"): the request still succeeds, just as a
        # head-fork instead of the requested tail-fork; "denied" would misleadingly
        # suggest the fork itself was rejected.
        sel().log_api_access(
            caller=request_app or "dashboard",
            operation=_operation,
            outcome="allowed",
            source="dashboard",
            resources=f"slot={name},direction=tail",
            error="tail_fork_enabled is False; falling back to head-fork",
        )
        direction = _FORK_DIRECTION_HEAD
    if prompt is not None and not isinstance(prompt, str):
        return web.json_response(
            {"error": "prompt must be a string", "code": "invalid_field_type"},
            status=400,
        )
    prompt = (prompt or "").strip()
    if len(prompt) > 32_768:
        return web.json_response(
            {
                "error": "prompt too long (max 32768 chars)",
                "code": "prompt_too_long",
            },
            status=400,
        )

    result = await fork_slot(
        state,
        source,
        at_index=at_index,
        at_message_id=at_message_id,
        direction=direction,
        prompt=prompt,
        mode_override=mode_override,
        request_app=request_app,
        origin=request_slot_origin(request_app),
        # Human request-layer path: a person forking a conversation. The
        # origin conjunct in state.py still excludes app-token callers.
        count_user_session=True,
        # Inheriting is arming a SECOND routed session, so it answers to the
        # same owner predicate as the arm itself: this route is gated on app
        # ownership, which an allow-listed non-owner passes for a slot the
        # owner armed.
        jev_route_allowed=is_owner_dashboard_request(request),
        audit_caller=request_app or "dashboard",
        audit_operation=_operation,
        _promote_from_ephemeral=_promote_from_ephemeral,
    )
    if isinstance(result, web.Response):
        return result
    return web.json_response(
        {
            "ok": True,
            "key": result.slot.key,
            "title": result.slot.title,
            "messages": result.messages,
            "prompt": prompt,
            "folder_id": result.slot.folder_id or None,
            "direction": result.direction,
            # The mode the child was born with (always the parent's), so the tab
            # can render the incognito/temporary badge before the slots refresh.
            "memory_mode": result.slot.memory_mode,
        }
    )


async def fork_slot(
    state: DashboardState,
    source: "ForkSource",
    *,
    at_index: Any,
    at_message_id: str | None,
    direction: str,
    prompt: str,
    mode_override: str | None,
    request_app: str,
    origin: str,
    count_user_session: bool,
    jev_route_allowed: bool,
    audit_caller: str,
    audit_operation: str = FORK_AUDIT_OPERATION,
    stamp: "Callable[[_ChatSlot], None] | None" = None,
    recheck: "Callable[[], None] | None" = None,
    _promote_from_ephemeral: bool = False,
) -> "ForkResult | web.Response":
    """Copy *source*'s transcript up to (or after) the fork point into a new slot.

    The second half of a fork: everything from the transcript snapshot to the
    acknowledged, persisted child. ``source`` is what :func:`resolve_fork_source`
    returned for the parent, and every argument is already validated -- this
    function checks only what it can check against the transcript it reads
    (``at_index`` against the visible-row count, ``at_message_id`` against the
    rows' ids). It has no request: the pieces the human route derives from one
    (``request_app``, ``origin``, ``count_user_session``, ``jev_route_allowed``,
    ``audit_caller``) are passed in, so ``session_control.fork_session`` can run
    the identical copy for an agent caller with its own answers to them.

    ``stamp``, when given, is called on the child once it is fully shaped
    (title, folder, tags, inherited memory identity) and BEFORE the transcript
    copy is saved -- so whatever it sets rides the child's own birth save and is
    on disk before ``push_slots_update`` broadcasts the slot. This is how
    ``session_control.fork_session`` lands creator attribution and its optional
    title/folder in the same write as the transcript, with no second persistence
    window in which a persisted, broadcast child exists unattributed. It must
    only assign in-memory fields; it is not awaited and must not raise for
    ordinary input (a raise here is treated as fork finalisation failing, and
    the child is withdrawn).

    ``recheck``, when given, is a SYNCHRONOUS re-assertion of whatever the caller
    decided before handing over: it runs immediately before the child is minted
    and again immediately before the transcript is copied into it, adjacent to
    the two points where this function re-compares the source's own frozen
    identity (when no memory bind runs, nothing suspends after the mint, so the
    first call covers the copy too). It may raise; a raise before the mint
    leaves nothing behind, and a raise after the bind withdraws the empty child. This is how
    ``session_control.fork_session`` keeps its containment answers -- caller
    eligibility, the source's addressability, the folder's existence -- true at
    the act rather than at the moment they were first read, across the
    suspensions this function takes for the transcript read and the memory bind.

    Returns a :class:`ForkResult` on success. A refusal is returned as the
    finished ``web.Response`` -- this module's refusal shape, kept so its coded
    error sites stay where the error-code ratchet pins them -- and a failure
    that is not a refusal raises. On success the child is already saved,
    ``_sync_dashboard_slots`` has run and the slots update is pushed.
    """
    slot = source.slot
    inherited_execution = source.execution
    source_memory_identity = source.identity
    # Read disk FIRST (full history). Stable message IDs are resolved against this
    # complete corpus; the legacy index fallback also has to use the same chained
    # view the fully-loaded frontend renders. Without the chained read, an archived
    # target is reported missing (ID path) or indices past the current file boundary
    # fail out of range (legacy path).
    async with slot._fork_lock:
        all_messages: list[dict] = []
        new_msgs: list[dict] = []
        # Two tail candidates, because a boundary ahead of the resident window has
        # TWO causes needing OPPOSITE remedies and they can only be told apart
        # once the true on-disk length is known -- i.e. after the read. Both are
        # snapshotted ON THE LOOP so whichever is chosen still pairs with the
        # boundary the read observed.
        tail: list[dict] | None = None
        capped_tail: list[dict] | None = None
        # True when the read proved disk holds rows the slot's counters do not
        # represent (the capped-restore signature). Gates BOTH flush sites in this
        # handler, because the save's frozen prefix cannot protect those rows.
        disk_holds_unrepresented = False
        if state.conversation_log:
            # Pair the disk read with the unpersisted tail on ONE consistent view
            # of the slot, using the idiom session_transfer._snapshot_transcript
            # already uses: capture the boundary, read, re-check, retry on change.
            #
            # The offset is ``_disk_window_len`` -- "how many window messages are
            # now on disk", advanced by the save path (chat_persistence
            # ``_save_slot_to_history``). Two nearby counters cannot serve here:
            #
            #   * ``_resumed_count`` records only how many messages were loaded
            #     when the slot was rehydrated. The flush never advances it, so a
            #     persisted tail stays inside the slice and reconciles in twice.
            #     session_transfer hit exactly this and documents it.
            #   * a length captured on a ``_dirty`` transition fails the same way.
            #     ``_dirty`` is a boolean, so it cannot distinguish "never flushed"
            #     from "flushed, then re-dirtied by an append" -- and in that
            #     interleaving no transition registers at all.
            #
            # ``_dirty_gen`` (monotonic, bumped centrally by the ``_dirty`` setter)
            # catches in-place edits that move neither boundary nor length; the
            # length is a backstop for any path that mutates ``slot.messages``
            # without marking dirty. There is deliberately no ``_dirty`` gate on
            # the merge below: the boundary alone is authoritative and the slice is
            # empty when everything is persisted, so a flush clearing ``_dirty``
            # mid-read cannot skip the reconciliation and drop the tail.
            # ``pending_retry`` carries a SUSPICION across the ``continue`` below.
            # The pending-rewrite save below clears ``_pending_rewrite``
            # unconditionally once the archive-safe rewrite succeeds
            # (``chat_persistence``: ``if rewrite:
            # slot._pending_rewrite = False``) with NO check that the flag it clears
            # is the one its own snapshot was taken for. So a rewind landing while
            # that save is suspended has its flag erased, and without this carry the
            # next attempt reads ``False`` and falls through to a disk read holding
            # the turns the rewind just discarded.
            pending_retry = False
            for _ in range(_SNAPSHOT_ATTEMPTS):
                if slot._pending_rewrite or pending_retry:
                    # Disk is KNOWN stale: a rewind/regenerate discarded a tail in
                    # memory and the truncating rewrite has not been written yet, so
                    # the file still holds the PRE-EDIT transcript. None of the four
                    # counters captured below carries that state -- ``chat_rewind``
                    # sets ``_dirty``, zeroes ``_resumed_count`` and sets this flag,
                    # but never touches ``_disk_window_len`` -- so the boundary keeps
                    # its pre-rewind value and can still satisfy the authoritative
                    # predicate. The read would then return the discarded turns and
                    # the post-await re-check would PASS, because nothing moved
                    # during the read: it measures stability, not correctness.
                    # ``session_transfer._guard_snapshot`` refuses on exactly this
                    # flag for exactly this reason.
                    #
                    # SAVE and retry rather than refuse outright: a rewrite save
                    # clears the flag (chat_persistence sets ``_pending_rewrite =
                    # False`` once the archive-safe rewrite succeeds), so this is
                    # the recoverable path, and a fork is required to still succeed.
                    # The 503 below is the terminal arm for a source that cannot be
                    # persisted, and the loop's own 503 covers a flag that keeps
                    # being re-set within the attempt budget.
                    # Capture the generation BEFORE the suspension point, so a
                    # re-dirty that lands while the save is awaited is witnessed.
                    gen_at_save = slot._dirty_gen
                    try:
                        # ``rewrite=True`` UNCONDITIONALLY, because every entry into
                        # this arm means "disk is stale because an EDIT truncated the
                        # window", and that is exactly what the archive-safe path is
                        # for. ``_save_slot_to_history`` only ever PROMOTES this flag
                        # (``if messages is not None or slot._pending_rewrite: rewrite
                        # = True``) and gates both the archive-diff (``if rewrite and
                        # path.exists()``) and the ``rotation_generation`` bump behind
                        # it -- so on the RETRY, where no snapshot is passed and
                        # ``_pending_rewrite`` has already been cleared by the first
                        # save, neither promotion input is present. Without this the
                        # retry would persist the rewind's truncation through the
                        # PLAIN path, deleting the discarded turns with no archive
                        # copy: strictly worse than the bug it recovers from, which
                        # at least left them readable on disk.
                        #
                        # Passing it on the first entry too is a no-op rather than a
                        # widening -- ``_pending_rewrite`` is still set there, so the
                        # promotion above already produces True
                        # (``test_a_first_entry_pending_rewrite_save_archives_as_
                        # before`` pins that). Stating it here removes the dependence
                        # on that promotion, which is the thing that silently failed.
                        saved = await save_slot_off_loop(
                            state,
                            slot,
                            rewrite=True,
                            best_effort=False,
                            # An authorized transcript key makes this a GUARDED
                            # write, which is what puts it under the same ordering
                            # as the other truncating saves: it registers in the
                            # slot's guarded-write registry, so a retraction of the
                            # slot's name waits for it instead of popping while its
                            # worker is on the way to the rename; it is refused
                            # while a retraction is already past that wait; and it
                            # engages the in-lock routing re-read. Without the key
                            # this rewrite was the LEAST ordered truncating save in
                            # the codebase, not the most, and the fork is the one
                            # caller that then republishes the file it wrote.
                            expected_history_key=slot_history_key(slot),
                        )
                    except Exception:
                        logger.warning(
                            "chat_fork: could not persist the pending rewrite for "
                            "slot=%s; refusing the fork rather than copying the "
                            "discarded turns still on disk",
                            slot.key,
                            exc_info=True,
                        )
                        return web.json_response(
                            {
                                "error": "the source session is being written to; " "please retry",
                                "code": "fork_snapshot_unstable",
                            },
                            status=503,
                        )
                    if not saved:
                        # The save declined WITHOUT writing, and the three reasons
                        # it can decline are indistinguishable from a bool: the
                        # source session was permanently deleted while this flush
                        # awaited the lock, the routing moved off the transcript the
                        # key authorizes, or a retraction of this slot's name is
                        # already past the point where it can wait for this write.
                        # Every one of them says the same thing about forking: the
                        # file on disk still holds the discarded turns, so copying
                        # it would republish them under a fresh key. The response
                        # code is kept as it is because clients match on it; the
                        # message states the class rather than picking one cause.
                        logger.warning(
                            "chat_fork: the pending rewrite for slot=%s was not "
                            "persisted (deleted, rerouted, or being closed); "
                            "aborting fork rather than copying the discarded turns "
                            "still on disk",
                            slot.key,
                        )
                        return web.json_response(
                            {
                                "error": "the source session could not be saved; retry the fork",
                                "code": "fork_source_deleted",
                            },
                            status=409,
                        )
                    # A moved generation means a genuine re-dirty landed across the
                    # await -- i.e. a rewind, whose ``_pending_rewrite`` this save
                    # has just erased along with its own. Two properties make the
                    # witness clean rather than a permanent trip: ``_dirty_gen``
                    # advances ONLY on a True assignment (see the ``_dirty`` setter
                    # in ``state.py``), so this save clearing ``_dirty`` cannot move
                    # it; and ``best_effort=False`` PROPAGATES a failure instead of
                    # re-marking the slot dirty, so the save cannot bump it either.
                    #
                    # RETRY rather than refuse, per the reasoning above: this is the
                    # recoverable path and a fork is required to still succeed. The
                    # next attempt re-enters this arm and persists the rewind that
                    # was missed. A flag that keeps being re-set simply spends the
                    # attempt budget and is caught by the loop's own terminal 503 --
                    # exactly the division of labour described above. Mirrors
                    # ``flush_slot_now``'s generation compare in ``state.py``, which
                    # distinguishes "the True I started this save under" from "a NEW
                    # True set during it" for this same reason.
                    pending_retry = slot._dirty_gen != gen_at_save
                    continue
                disk_len_before = slot._disk_window_len
                gen_before = slot._dirty_gen
                count_before = len(slot.messages)
                older_before = slot._disk_older_count
                # Witnessed for STABILITY ONLY -- see the guard below. Captured here
                # so the branch selector at ``elif slot._dirty`` and the post-await
                # check read the same value.
                dirty_before = slot._dirty
                # Snapshot the tail ON THE LOOP, before the await, so it pairs
                # with the boundary the read is about to observe.
                if disk_len_before <= count_before:
                    # The boundary is a usable index and authoritative: the slice
                    # is empty exactly when everything is persisted. Deliberately
                    # NO ``_dirty`` gate here -- a gate is what lets a flush
                    # clearing ``_dirty`` mid-read skip the merge and drop the tail.
                    tail = list(slot.messages[disk_len_before:])
                elif slot._dirty:
                    # The boundary can run AHEAD of the resident window, and is
                    # then unusable as an index. It has TWO causes and they need
                    # OPPOSITE remedies, so this branch must not pick one blind:
                    #
                    #   * a CAPPED RESTORE dropped leading messages from memory
                    #     without bumping ``_disk_older_count``. Disk legitimately
                    #     holds MORE than memory, and flushing would write the
                    #     smaller window over it. The frozen prefix is keyed on
                    #     ``_disk_older_count`` (chat_persistence
                    #     ``_load_frozen_prefix``), which the cap never moved, so
                    #     the prefix is EMPTY and the save truncates disk to the
                    #     window -- destroying every persisted message the cap
                    #     dropped. ``test_fork_preserves_full_history_when_dirty_
                    #     and_capped`` is the guard for exactly that.
                    #   * a mid-stream ``_flush_segment`` reassigned
                    #     ``slot.messages`` to drop a trailing chunk run. Disk and
                    #     the counters still agree, so flushing is safe and is what
                    #     re-syncs the boundary.
                    #
                    # ``_resumed_count`` is not a blanket substitute either: the
                    # save never advances it -- ``_save_slot_to_history`` only READS
                    # it, in its no-op skip -- so for a slot created in this gateway
                    # run it stays 0 and slicing from it appends the whole resident
                    # window onto the disk read, duplicating every persisted turn.
                    # It IS the right offset in the capped-restore case, where the
                    # restore sets it to the capped length, which is precisely "how
                    # many resident messages came from disk".
                    #
                    # So snapshot that candidate here and decide below, once the
                    # read has supplied the only authoritative discriminator: the
                    # true on-disk length.
                    tail = None
                    capped_tail = list(slot.messages[slot._resumed_count :])
                else:
                    tail = []
                all_messages = await asyncio.to_thread(
                    state.conversation_log.read_messages_chained, slot_history_key(slot)
                )
                if not (
                    slot._disk_window_len == disk_len_before
                    and slot._dirty_gen == gen_before
                    and len(slot.messages) == count_before
                    # ``_disk_older_count`` too: it is half of the window's identity
                    # (channel_slots: the window is
                    # ``messages[_disk_older_count:][:len(window)]``), and the
                    # discriminator below is computed from it, so a move here
                    # invalidates the pairing exactly as a boundary move does.
                    and slot._disk_older_count == older_before
                    # ``_pending_rewrite`` is checked on BOTH sides of the await, as
                    # session_transfer's guard documents. It was False when this
                    # attempt began (the branch above continues otherwise), so True
                    # here means a rewind landed DURING the threaded read -- which it
                    # can, because ``slot._fork_lock`` has exactly one acquirer in the
                    # tree and no rewind path takes it. Equality against the other
                    # counters cannot see this: a rewind moves none of them back.
                    and not slot._pending_rewrite
                    # ``_dirty`` as a STABILITY WITNESS, never as a merge gate. The
                    # other four cannot see a save that merely COMPLETES under the
                    # read: an in-place content edit (a variant switch) moves neither
                    # ``len(slot.messages)`` nor ``_disk_older_count``, the save
                    # re-assigns ``_disk_window_len`` to the value it already had
                    # because the window length did not change, and CLEARING
                    # ``_dirty`` cannot move ``_dirty_gen`` (the setter advances it
                    # only on a True assignment). So the threaded read can return
                    # pre-save bytes while the slot reports everything persisted, and
                    # the boundary-derived tail is empty -- leaving the fork to adopt
                    # the stale read verbatim and carry the SUPERSEDED content.
                    #
                    # A mismatch RETRIES; it does not skip the reconciliation. That
                    # distinction is the whole reason this is safe to add: the two
                    # comments above rejecting a ``_dirty`` gate are about the MERGE
                    # decision, which stays keyed on the boundary alone. The next
                    # attempt re-reads a disk that now holds the completed save.
                    and slot._dirty == dirty_before
                ):
                    logger.debug(
                        "chat_fork: slot=%s changed during the transcript read; retrying",
                        slot.key,
                    )
                    continue
                if tail is not None:
                    new_msgs = tail
                    break
                # Boundary ahead AND dirty. Discriminate on the invariant
                # channel_slots states for these counters: disk holds
                # ``_disk_older_count`` frozen rows followed by the window, so
                # ``older + window`` is everything the counters claim is on disk.
                # Disk holding MORE than that means rows exist which the counters
                # do not represent -- the capped-restore signature. channel_slots
                # ``_window_matches_disk`` tests the same arithmetic in the
                # opposite direction.
                if len(all_messages) > older_before + count_before:
                    # Do NOT flush: it would truncate those unrepresented rows.
                    # Merge from ``_resumed_count`` instead, which the restore set.
                    disk_holds_unrepresented = True
                    new_msgs = capped_tail or []
                    break
                # Counters agree with disk, so the window shrank mid-stream and the
                # flush is what re-syncs the boundary: the save assigns
                # ``_disk_window_len = len(window)`` and never READS the boundary,
                # so flushing while it is ahead cannot mislead the write. Then spend
                # the attempt; the next one re-derives from the authoritative branch
                # above. If it still does not settle the loop's 503 refuses, which
                # is what session_transfer's ``_guard_snapshot`` does here -- but
                # retrying first is what keeps a fork succeeding once the stream
                # that moved the window has finalized.
                try:
                    await save_slot_off_loop(state, slot, best_effort=False)
                except Exception:
                    logger.warning(
                        "chat_fork: could not persist slot=%s to re-sync the "
                        "persisted boundary; refusing the fork",
                        slot.key,
                        exc_info=True,
                    )
                    return web.json_response(
                        {
                            "error": "the source session is being written to; " "please retry",
                            "code": "fork_snapshot_unstable",
                        },
                        status=503,
                    )
                continue
            else:
                # Do NOT fall through with the mismatched pair: that is precisely
                # the state the loop exists to reject, and taking it either drops
                # the tail or duplicates it. Both are silent; a retryable 503 is
                # not, and this handler already uses that shape below.
                logger.warning(
                    "chat_fork: slot=%s did not settle in %d attempts; refusing the fork",
                    slot.key,
                    _SNAPSHOT_ATTEMPTS,
                )
                return web.json_response(
                    {
                        "error": "the source session is being written to; please retry",
                        "code": "fork_snapshot_unstable",
                    },
                    status=503,
                )
        _fork_tail_len = len(new_msgs) if (all_messages and new_msgs) else 0
        if all_messages and new_msgs:
            # REBIND, never ``extend``. ``read_messages_chained`` hands back the
            # SHARED ``_msg_cache`` list BY IDENTITY whenever it falls through to
            # ``_read_messages``: for a session with no ``tab_id``, for a tid whose
            # index resolves to no keys, and when every chained read comes back
            # empty. ``_read_messages``'s docstring makes the contract explicit --
            # callers MUST treat the result as immutable and slice or ``list(...)``
            # it before mutating.
            #
            # An in-place mutation is unsafe here because this handler can finish
            # with no durable save at all, and only a save invalidates the cache
            # entry: the skip-the-save branch below is deliberate, and both save
            # arms are gated on ``slot._dirty``, so neither runs when it is
            # already False -- while the tail snapshot above is ungated and can
            # still be non-empty. Nothing in this handler then corrects the entry,
            # so readers of this key see the UNPERSISTED tail as though it were
            # history until the next write to this transcript invalidates it, or
            # the LRU evicts it.
            #
            # A rebind rather than ``list(all_messages)`` + ``extend``: one
            # expression, the surrounding control flow untouched, and it leaves no
            # in-place mutation of this object anywhere in the handler for a future
            # edit to reintroduce. Nothing below depends on the identity -- the
            # remaining uses are a truthiness test, a rebind and a comprehension.
            all_messages = all_messages + new_msgs
        if slot._dirty and disk_holds_unrepresented:
            # Same rule as the boundary-ahead branch above, applied to the second
            # flush site in this handler: NEVER flush a slot whose disk holds rows
            # its counters do not represent. The save's frozen prefix is
            # ``body[:_disk_older_count]``, so rows outside that are not protected
            # and the write truncates them -- 250 -> 52 in the capped-restore case.
            #
            # Skipping it leaves ``_dirty`` SET deliberately, so the unwritten
            # source messages stay queued for the periodic flusher rather than
            # being stranded by a premature clean-mark. The fork itself does not
            # need the flush: it already holds the full transcript from disk plus
            # the merged tail.
            #
            # NOTE (pre-existing, not introduced here): the periodic flusher can
            # still truncate a slot left in this state, because the destructive
            # write lives in the save path. This only stops the FORK from causing
            # it; the durable fix is for ``_disk_older_count`` to cover the rows a
            # cap drops, which is out of scope for this handler.
            logger.warning(
                "chat_fork: slot=%s has %d frozen-prefix rows for a %d-message "
                "window but disk holds more; skipping the durable save so it "
                "cannot truncate the persisted history",
                slot.key,
                slot._disk_older_count,
                len(slot.messages),
            )
        elif slot._dirty:
            # Persist with best_effort=False so a lock timeout / I/O failure
            # PROPAGATES instead of being swallowed. The fork treats disk as the
            # source of truth (it re-reads the full history above) and clears
            # ``_dirty`` below — which also disables the periodic retry that
            # would otherwise re-flush the slot. Clearing ``_dirty`` after a
            # silently-dropped save would strand the unwritten source messages
            # and lose them permanently on the next gateway restart. Only mark
            # the slot clean once the durable write is CONFIRMED; on failure,
            # abort the fork (leaving ``_dirty`` set) rather than fork from a
            # partially-persisted source.
            try:
                saved = await save_slot_off_loop(state, slot, best_effort=False)
            except Exception:
                logger.warning(
                    "chat_fork: durable save of source slot=%s failed; "
                    "aborting fork to avoid losing unwritten messages",
                    slot.key,
                    exc_info=True,
                )
                return web.json_response(
                    {
                        "error": "could not persist source session before fork; " "please retry",
                        "code": "source_save_failed",
                    },
                    status=503,
                )
            if not saved:
                # The delete-won guard skipped the write: the source session was
                # permanently deleted while the flush awaited the lock. Forking
                # now would republish the destroyed conversation under a fresh
                # key (a brand-new slot carries no delete evidence, so ITS save
                # would proceed) — the exact resurrection the guard exists to
                # prevent, laundered through a copy. Abort instead; the delete's
                # reported success stands.
                logger.warning(
                    "chat_fork: source slot=%s was permanently deleted during "
                    "the pre-fork flush; aborting fork",
                    slot.key,
                )
                return web.json_response(
                    {
                        "error": "the source session was permanently deleted",
                        "code": "fork_source_deleted",
                    },
                    status=409,
                )
            slot._resumed_count = len(slot.messages)
            slot._dirty = False
        if not all_messages:
            # The whole corpus is the in-memory window: disk contributed
            # nothing, so for the mid-rotation rebuild below it is ALL tail.
            all_messages = list(slot.messages)
            _fork_tail_len = len(all_messages)
        # Direct delete check, independent of the flush arms above: if the
        # periodic 5s flush hit the delete-won guard first, it cleared
        # ``_dirty`` and this handler's own flush arms never ran — the disk
        # read came back empty and ``all_messages`` just fell back to the
        # in-memory window of a permanently deleted conversation. The two
        # ``saved``-checks above only cover a delete observed by THIS
        # handler's flush; this probe answers regardless of who consumed the
        # signal.
        if session_was_deleted(state, slot):
            logger.warning(
                "chat_fork: source slot=%s belongs to a permanently deleted "
                "session; refusing to fork it",
                slot.key,
            )
            return web.json_response(
                {
                    "error": "the source session was permanently deleted",
                    "code": "fork_source_deleted",
                },
                status=409,
            )
    # Fork indices arrive in the PAGINATED corpus's visible-row space, which
    # prepends each chain key's size-rotated archive head
    # (read_messages_chained_full). Mirror that corpus here, or every index
    # sent after the reader paged past a rotation boundary resolves short by
    # the archived visible-row count, silently forking the WRONG message —
    # and archived rows could not be forked at all. Rotated rows are BY
    # DEFINITION no longer in the live files, so this cannot duplicate
    # anything the flush arms above already placed in ``all_messages``.
    #
    # Two shapes, matching the slot-detail handler exactly:
    # - Archive only on the FIRST chain member: the archived rows are a
    #   contiguous prefix of the chained corpus, so a flat prepend is exact.
    # - A LATER member also rotated (``chain_mid_rotation``): the paginated
    #   corpus interleaves rot/live per key, so a flat prepend would shift
    #   ``at_message_index`` by the sandwiched rows. Rebuild the disk part
    #   from the true chained corpus and re-append the unflushed tail the
    #   arms above collected (``_fork_tail_len`` rows).
    if state.conversation_log:
        try:
            _rotated_head = await asyncio.to_thread(
                state.conversation_log.read_rotated_messages_chained,
                slot_history_key(slot),
            )
        except Exception:
            # NOT `_rotated_head = []`. Empty means "no archive" here, so folding
            # the failure into it drops the archived head and shifts every index
            # below it -- and this path forks BY INDEX, so it would copy a
            # different cutoff than the one the reader pointed at, silently. Same
            # retryable shape the snapshot loop below already returns.
            logger.warning("rotated-archive read failed for fork", exc_info=True)
            return history_corpus_unreadable("fork_corpus_unreadable")
        if _rotated_head:
            _mid_rotation = False
            try:
                _mid_rotation = await asyncio.to_thread(
                    state.conversation_log.chain_mid_rotation,
                    slot_history_key(slot),
                )
            except Exception:
                # Same reasoning as the slot-detail probe: a False fallback picks
                # the flat-prepend path, which is only correct when the rotation is
                # on the first chain member. Getting that wrong here forks BY INDEX
                # off a misindexed corpus, so it copies different messages than the
                # ones the reader pointed at.
                logger.warning("mid-rotation probe failed for fork", exc_info=True)
                return history_corpus_unreadable("fork_corpus_unreadable")
            _rebuilt = False
            if _mid_rotation:
                try:
                    _full_disk = await asyncio.to_thread(
                        state.conversation_log.read_messages_chained_full,
                        slot_history_key(slot),
                    )
                    _tail = (
                        all_messages[len(all_messages) - _fork_tail_len :] if _fork_tail_len else []
                    )
                    # `_tail` was derived as "unflushed" against the corpus the
                    # snapshot loop read. THIS is a later read, and two
                    # `to_thread` suspensions separate them, so a save landing in
                    # that window puts those same rows on disk — appending the
                    # tail blind then duplicates them, surfacing as an ambiguous
                    # fork id or a doubled fork tail. The loop's stability
                    # guarantee does not reach across this read, so re-derive
                    # against what this read actually returned.
                    all_messages = _full_disk + drop_persisted_tail_prefix(_full_disk, _tail)
                    _rebuilt = True
                except Exception:
                    # FAIL CLOSED. The flat prepend below puts only THIS key's
                    # rotated head in front, so when the rotation is on a later
                    # chain member the earlier members' rotated rows are still
                    # missing and every index shifts. An index-addressed fork
                    # then copies DIFFERENT messages than the ones the reader
                    # pointed at, silently — worse than not forking at all,
                    # which the reader can see and retry. Same retryable shape
                    # the snapshot loop above already returns.
                    logger.warning("chained-full fork corpus read failed", exc_info=True)
                    return history_corpus_unreadable("fork_corpus_unreadable")
            if not _rebuilt:
                # Same crash window as `read_messages_chained_full`'s concatenation,
                # reached by a different branch: rotation archives the dropped lines
                # first and rewrites the live file's head second, so a kill between
                # those two steps leaves the archived rows in BOTH files. Prepending
                # blind then serves them twice, and in this corpus a duplicated row
                # is what makes a legacy fork index select the wrong cutoff.
                #
                # This branch is not covered by that function's own guard because it
                # runs when the chained-full read was not used at all.
                all_messages = _rotated_head + drop_persisted_tail_prefix(
                    _rotated_head, all_messages
                )
    if _promote_from_ephemeral:
        # Promotion copies the WHOLE conversation verbatim into the kept session,
        # so it must preserve every persisted, renderable row -- including the
        # system rows an ordinary fork drops (a Stop notice, a compaction card,
        # an error row). Dropping them would silently lose transcript content the
        # ephemeral original recorded, and the original is the copy being left
        # behind. Only genuinely transient streaming rows (never persisted) are
        # excluded. Promotion forces an empty body above, so there is no fork
        # point to select among these rows.
        visible = [m for m in all_messages if m.get("role") not in _TRANSIENT_ROLES]
    else:
        visible = [m for m in all_messages if m.get("role") in ("user", "assistant")]
    if not visible:
        return web.json_response(
            {"error": "no messages to fork", "code": "no_messages_to_fork"},
            status=400,
        )
    if at_message_id is not None:
        matches = []
        for index, message in enumerate(visible):
            meta = message.get("meta")
            mid = meta.get("mid") if isinstance(meta, dict) else None
            if mid == at_message_id:
                matches.append(index)
        if not matches:
            return web.json_response(
                {
                    "error": "the selected message is no longer present in this session",
                    "code": "fork_message_not_found",
                },
                status=409,
            )
        if len(matches) > 1:
            # ``meta`` can originate with a caller. Never guess when a malformed
            # transcript reuses an id: choosing either occurrence silently forks
            # from a different point than the user selected.
            return web.json_response(
                {
                    "error": "the selected message id is ambiguous in this session",
                    "code": "fork_message_ambiguous",
                },
                status=409,
            )
        at_index = matches[0]
    elif at_index is not None:
        if isinstance(at_index, bool) or not isinstance(at_index, int) or at_index < 0:
            return web.json_response(
                {
                    "error": "at_message_index must be a non-negative integer",
                    "code": "invalid_field_type",
                },
                status=400,
            )
        if at_index >= len(visible):
            return web.json_response(
                {
                    "error": f"at_message_index {at_index} out of range (have {len(visible)} visible messages)",
                    "code": "value_out_of_range",
                },
                status=400,
            )

    head_messages: list[dict] = []
    if direction == _FORK_DIRECTION_TAIL:
        if at_index is None:
            return web.json_response(
                {
                    "error": "at_message_index is required for a tail fork",
                    "code": "at_message_index_required",
                },
                status=400,
            )
        head_messages = visible[: at_index + 1]
        visible = visible[at_index + 1 :]
        if not visible:
            return web.json_response(
                {
                    "error": "no messages after the fork point",
                    "code": "no_messages_after_fork_point",
                },
                status=400,
            )
    elif at_index is not None:
        visible = visible[: at_index + 1]

    # A fork of a member DM thread is an ordinary chat, never a second "member"
    # slot: the fork mints a chat-* key, so member mode would make it invisible
    # everywhere (excluded from Sessions by surface mode, and absent from the
    # roster, whose threads live only on member-<slug> keys). The override
    # allowlist deliberately cannot name "member".
    if mode_override is not None:
        fork_mode = mode_override
    elif slot.mode == "member":
        fork_mode = ""
    else:
        fork_mode = slot.mode

    from kiro_crew.memory_stores import UnknownMemoryStore

    def _source_identity_unchanged() -> bool:
        return state._slots.get(slot.key) is slot and source_memory_identity == (
            effective_session_key(slot),
            slot.agent,
            slot.memory_store,
            slot.memory_mode,
            slot_history_key(slot),
            str(getattr(slot, "workspace", "default") or "default"),
        )

    try:
        inherited_store = inherited_execution.store.legacy_name
        inherited_memory_mode = inherited_execution.memory_mode
        if not _source_identity_unchanged():
            raise UnknownMemoryStore("The fork source changed while its memory was verified")
    except (OSError, ValueError) as exc:
        return _store_unavailable_response(source_memory_identity[2], exc)
    if recheck is not None:
        # Adjacent to the mint: nothing suspends between here and
        # `get_or_create_slot`, so what this asserts is true of the child's birth.
        recheck()

    new_slot = state.get_or_create_slot(
        name=None,
        agent=slot.agent,
        workspace=slot.workspace,
        model=slot.model,
        mode=fork_mode,
        # Inherited at BIRTH, not stamped afterwards: get_or_create_slot is what
        # registers the child's ``dashboard:`` key as restricted, and every memory
        # gate (lessons, consolidation, artifact registration) reads that
        # registry. A later assignment would leave a window where the child of
        # an incognito parent is a persistent slot. The value is the one
        # validated above, so the constructor cannot raise on it.
        #
        # Promotion is the ONE deliberate exception to inheriting the parent's
        # mode: "Keep this chat" exists precisely to LOOSEN it, so the child is
        # forced to "persistent" here rather than carrying the ephemeral
        # parent's mode forward onto what is supposed to be the kept copy.
        memory_mode="persistent" if _promote_from_ephemeral else inherited_memory_mode,
        app=request_app,
        origin=origin,
        count_user_session=count_user_session,
    )
    if inherited_execution is not None:
        try:
            # Bind the frozen identity and strict mode while the child is empty.
            # Promotion binds the child as persistent (the mode it is becoming),
            # not the ephemeral parent's, so its metadata reads back correctly.
            await drained_to_thread(
                _bind_fork_execution,
                source_memory_identity[:4],
                effective_session_key(new_slot),
                inherited_execution,
                "persistent" if _promote_from_ephemeral else None,
            )
            if recheck is not None:
                # The bind suspended; re-assert the caller's containment answers
                # first, so a source that became unaddressable meanwhile is
                # refused with ITS code rather than as an identity drift. A raise
                # here takes the withdrawal path below (no rows copied yet), and
                # nothing suspends between here and the copy.
                recheck()
            if not _source_identity_unchanged():
                raise UnknownMemoryStore("The fork source changed before its history was copied")
            new_slot.memory_store = inherited_store
        except BaseException as exc:
            child_key = effective_session_key(new_slot)
            # A promotion forces the persistent bind arm, so bind_session_execution
            # has already written a DURABLE metadata-only transcript record before
            # the post-bind identity check can fail. Clearing the execution and the
            # in-memory slot is not enough here either: the on-disk metadata line
            # would surface the same ghost session in history. Delete the destination
            # transcript too, mirroring the finalization rollback below.
            if _promote_from_ephemeral and state.conversation_log is not None:
                try:
                    await drained_to_thread(
                        state.conversation_log.delete_session, slot_history_key(new_slot)
                    )
                except Exception:
                    logger.exception(
                        "chat_fork: failed to delete ghost transcript for aborted "
                        "promotion bind to=%s",
                        new_slot.key,
                    )
            clear_session_execution(child_key)
            state._slots.pop(new_slot.key, None)
            state._restricted_keys.discard(child_key)
            if isinstance(exc, (OSError, ValueError)):
                return _store_unavailable_response(inherited_store, exc)
            raise
    new_slot.forked_from = effective_session_key(slot)
    new_slot.reasoning_effort = slot.reasoning_effort
    # Inherited beside the model it belongs to: the constructor takes `model` and
    # the routing choice is the other half of the same answer, so a fork of an
    # "Auto (Jev)" session that arrived pinned would run the parent's next turns
    # on a model the parent had explicitly stopped choosing by hand.
    # Inheriting is arming a SECOND routed session, so it answers to the same owner
    # predicate as the arm itself; the caller says whether it passed that predicate.
    new_slot.jev_route = slot.jev_route and jev_route_allowed
    # Inherit the active project directory so the fork keeps the parent's working
    # context (agent resolution, steering files, CWD) instead of falling back to
    # the config/workspace default on first message.
    new_slot.project = slot.project
    # Inherit the sidebar folder so the fork appears next to its parent in the UI.
    new_slot.folder_id = slot.folder_id
    # Inherit tags (copied, so later edits to either slot's list stay independent).
    new_slot.tags = list(slot.tags)
    # "tags changed => revision changed": the slot was constructed with an empty
    # list under its birth revision; a snapshot of that newborn state (a slot
    # fetch racing the fork) must not share a revision with the inherited list,
    # or a delayed empty frame could become a client's next toggle base.
    new_slot.bump_tags_revision()
    parent_title = slot.title if slot._titled else "Untitled"
    parent_title, _ = redact_exfiltration_urls(parent_title)
    parent_title, _ = redact_credentials(parent_title)
    # Strip a leading marker from the parent so it never compounds on a
    # fork-of-a-fork.
    parent_title = parent_title.removeprefix(_FORK_TITLE_MARKER)
    if _promote_from_ephemeral:
        # Keep the prefix short: the sidebar row truncates, and a long
        # "Kept from private chat:" prefix fills the whole row before the
        # parent title starts, so every promoted session reads identically
        # there. "Kept:" leaves room for the title that actually distinguishes
        # them. The "from a private chat" detail lives in the transcript note.
        fork_word = "Kept:"
    elif direction == _FORK_DIRECTION_TAIL:
        fork_word = "Tail of"
    else:
        fork_word = "Fork of"
    new_slot.title = f"{_FORK_TITLE_MARKER}{fork_word} {parent_title}"
    new_slot._titled = True

    try:
        if stamp is not None:
            stamp(new_slot)
        for m in visible:
            role = m.get("role", "assistant")
            content = m.get("content", "")
            if role != "user":
                content, _ = redact_exfiltration_urls(content)
                content, _ = redact_credentials(content)
            # Preserve a stored ``cls`` when the row carries one: a system
            # Stop-event row stores its JSON envelope discriminator in ``cls``,
            # and unconditionally recomputing it as ``msg msg-a`` would render
            # that raw JSON as assistant text in the copied transcript. Fall
            # back to the role-derived class only when the row has none.
            cls = m.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
            new_slot.append(
                role, content, cls, ts=m.get("ts", ""), meta=m.get("meta"), broadcast=False
            )
            # A fork copies the parent's messages into a new session. Origin is
            # a property of the message, not of the file, so a copied inbound
            # channel turn keeps the origin it actually had.
            carry_provenance(new_slot.messages[-1], m)
        if _promote_from_ephemeral:
            # A visible marker in the NEW (now-persistent) transcript, so a
            # reader does not have to cross-reference the SEL log to learn
            # this session started life private. A temporary source
            # additionally ran with memory READS suppressed, so the earlier
            # turns above this note were produced without that context --
            # worth surfacing, not worth blocking on.
            note = _PROMOTION_NOTE
            if slot.memory_mode == "temporary":
                note += _PROMOTION_NOTE_TEMPORARY_SUFFIX
            new_slot.append(
                "system", note, "msg msg-sys", meta={"kind": "session_promoted"}, broadcast=False
            )
        new_slot.drain()
        # Promotion is a one-way copy whose SOURCE is ephemeral: the original is
        # left private and effectively discarded, so a swallowed destination
        # save would 200 the request and switch the UI to a session that
        # vanishes on restart, losing the only durable copy. Confirm the write
        # (best_effort=False) so a storage failure raises here and is cleaned up
        # below, rather than being acknowledged. An ordinary fork keeps its
        # persistent source, so its best-effort save (periodic-flush retry) is
        # fine and stays the default.
        await save_slot_off_loop(state, new_slot, best_effort=not _promote_from_ephemeral)
        new_slot._resumed_count = len(new_slot.messages)
    except Exception:
        state._slots.pop(new_slot.key, None)
        # A promotion whose durable save failed must leave nothing behind that a
        # later read could mistake for a real session. Binding the child's
        # execution already wrote a DURABLE metadata-only transcript record, so
        # dropping the in-memory slot and the live execution is not enough: the
        # on-disk metadata line would still surface the ghost in history. Delete
        # the destination transcript too, then clear the execution binding and
        # the restricted-key registration made before the copy.
        if _promote_from_ephemeral:
            child_key = effective_session_key(new_slot)
            clear_session_execution(child_key)
            state._restricted_keys.discard(child_key)
            if state.conversation_log is not None:
                try:
                    await drained_to_thread(
                        state.conversation_log.delete_session, slot_history_key(new_slot)
                    )
                except Exception:
                    logger.exception(
                        "chat_fork: failed to delete ghost transcript for aborted "
                        "promotion to=%s",
                        new_slot.key,
                    )
        sel().log_api_access(
            caller=audit_caller,
            operation=audit_operation,
            outcome="error",
            source="dashboard",
            resources=f"from={slot.key},to={new_slot.key}",
            error="fork finalisation failed",
        )
        if _promote_from_ephemeral:
            # Promotion has cleaned up its half-made child above. Answer a typed
            # error the dashboard can surface ("Couldn't keep this chat") rather
            # than letting the exception become an untyped 500 -- and crucially
            # NOT a 200, so the UI never switches to a session that did not
            # durably land. The ephemeral original is left untouched.
            return web.json_response(
                {
                    "error": "the chat could not be kept; nothing was changed",
                    "code": "promote_save_failed",
                },
                status=500,
            )
        raise
    # Acknowledgment boundary. The destination save above is the last await
    # before this fork is answered, and it takes the DESTINATION's history lock
    # -- nothing in it serialises against the SOURCE's permanent delete. So a
    # delete that began after the pre-copy probe passed can commit inside that
    # await, and the copy now on disk is a conversation the user has since
    # destroyed. ``build_transfer_bundle_async`` already closes its equivalent
    # window by re-probing after bundle assembly and before the peer send is
    # acknowledged; this is the same rule at the same boundary, for the copy
    # this handler makes.
    #
    # A delete that commits AFTER this point is deliberately out of scope: a
    # fork acknowledged while its source was alive is its own session and
    # survives the source, the way a repo fork outlives what it came from.
    # Acknowledgment is the only boundary a handler owns, so it is the line
    # drawn here.
    if session_was_deleted(state, slot):
        # Roll the destination back. It has been neither acknowledged nor
        # broadcast (``push_slots_update`` is below, and every ``append`` above
        # passed ``broadcast=False``), so nothing outside this handler has seen
        # it: removing it leaves no trace of the deleted conversation instead of
        # a fresh key holding a full copy. Removing the destination cannot harm
        # the source -- different key, different lock -- so a false positive
        # here (``session_was_deleted`` fails CLOSED when a stat or metadata
        # read is unverifiable) costs a retryable 409 against a still-live
        # source, not data.
        removed = False
        if state.conversation_log is not None:
            try:
                removed = bool(
                    await asyncio.to_thread(
                        state.conversation_log.delete_session,
                        slot_history_key(new_slot),
                    )
                )
            except Exception:
                removed = False
                logger.warning(
                    "chat_fork: removing the unacknowledged fork transcript for %s raised",
                    new_slot.key,
                    exc_info=True,
                )
        state._slots.pop(new_slot.key, None)
        if removed:
            logger.warning(
                "chat_fork: source slot=%s was permanently deleted during the "
                "destination save; removed the unacknowledged fork %s",
                slot.key,
                new_slot.key,
            )
        else:
            # The copy is still on disk and WILL be listed in Older Sessions.
            # Loud, because this is the resurrection the guard exists to
            # prevent and it now needs a human -- but still a 409: reporting
            # success would additionally hide it.
            logger.error(
                "chat_fork: source slot=%s was permanently deleted during the "
                "destination save, but the fork transcript for %s could not be "
                "removed; a copy of the deleted conversation remains on disk",
                slot.key,
                new_slot.key,
            )
        sel().log_api_access(
            caller=audit_caller,
            operation=audit_operation,
            outcome="denied",
            source="dashboard",
            resources=(
                f"from={slot.key},to={new_slot.key},"
                f"rollback={'removed' if removed else 'failed'}"
            ),
            error="source session permanently deleted during the destination save",
        )
        return web.json_response(
            {
                "error": "the source session was permanently deleted",
                "code": "fork_source_deleted",
            },
            status=409,
        )
    sel().log_api_access(
        caller=audit_caller,
        operation=audit_operation,
        outcome="allowed",
        source="dashboard",
        resources=(
            f"from={slot.key},to={new_slot.key},messages={len(visible)},"
            f"at_index={at_index if at_index is not None else 'last'},"
            f"direction={direction},"
            f"head_count={len(head_messages)},"
            f"prompt_len={len(prompt)},mode={new_slot.mode},"
            f"memory_mode={new_slot.memory_mode}"
            + (f",promoted_from={slot.memory_mode}" if _promote_from_ephemeral else "")
        ),
    )
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return ForkResult(slot=new_slot, messages=len(visible), direction=direction)


async def api_chat_slot_promote(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/promote — promote an ephemeral session to persistent.

    Ephemeral sessions (``memory_mode`` ``incognito``/``temporary``) are
    otherwise a one-way door: once a conversation starts private there is no
    way to keep it, only to lose it or hand-copy it into a fresh persistent
    chat. "Keep this chat" is the explicit, user-initiated action that closes
    that gap, mirroring the reverse persistent->ephemeral action the welcome
    view already offers ("Switch to ephemeral mode").

    **Design: fork, not in-place rewrite.** Promotion forks the slot's ENTIRE
    transcript into a brand-new persistent slot and leaves the ephemeral
    original untouched, rather than flipping the ``memory_mode`` header on the
    existing transcript in place. Rewriting the header in place would make
    every earlier turn -- sent when the user believed the session was private
    -- retroactively eligible for history search and consolidation, a
    scope the confirmation dialog cannot honestly describe up front. Forking
    instead lets the confirmation dialog truthfully say "the whole
    conversation is copied into a new, regular session", because that is
    exactly what happens, and the source stays exactly as private as it was.

    This is a THIN wrapper over :func:`api_chat_slot_fork`'s
    ``_promote_from_ephemeral`` escape hatch -- the ONLY caller allowed to set
    it. An ordinary fork always inherits the parent's ``memory_mode`` (forking
    an incognito session yields another incognito session; fork never
    LOOSENS the mode on its own), and this escape hatch is the one deliberate
    exception: it forces the child to ``persistent`` regardless of what the
    parent's mode is. It also fails closed on an already-persistent slot
    (``slot_already_persistent`` -- nothing to promote).

    **Strictly human-initiated, never agent-reachable.** Admission requires
    POSITIVE proof of the dashboard user -- a PRESENT, empty ``app`` claim,
    which ``token_auth_middleware`` publishes only for the dashboard itself.
    Identity here is never the mere absence of an app, because the
    internal-secret transport (loopback + ``X-Internal-Secret``, reachable
    from ``/api/chat``'s mixed-internal prefix) deliberately leaves the claim
    ABSENT when it cannot place the caller, so "no app" spans both the human
    and a secret-holding agent. The same positive-proof idiom gates the
    dashboard-only surfaces in ``handlers/source_providers`` and the
    human-at-the-dashboard trust grant in ``handlers/taskrunner``. This is
    also a dashboard route only -- no ``mcp_core``/``mcp_dashboard`` tool, no
    CLI command, and no body field on any other endpoint reaches this
    behavior, so promotion has exactly one entry point.
    """
    if "app" not in request or request["app"] != "":
        # Deny by default. A resolved app id is refused regardless of whether
        # it owns the slot -- slot ownership is the wrong question here, since
        # api_chat_slot_fork's app-isolation branch answers "may this app act
        # on this slot" while this route asks "may anything but a human
        # confirm this dialog". An ABSENT claim is refused for the same
        # reason: it is what an unplaceable internal-secret caller looks like.
        sel().log_api_access(
            caller=request.get("app") or "unidentified",
            operation="chat.slot_promote",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={request.match_info.get('slot', '')}",
            error="promotion requires a positively identified dashboard user",
        )
        return web.json_response(
            {
                "error": "promoting a session requires a signed-in dashboard user",
                "code": "promote_requires_dashboard_user",
            },
            status=403,
        )
    return await api_chat_slot_fork(request, _promote_from_ephemeral=True)
