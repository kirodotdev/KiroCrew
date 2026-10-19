"""Who is calling: the caller's identity, the session-control enablement gates, the
member, cron and delegation ownership fences, and the audit record every verb writes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.history import transcript_stem
from kiro_crew.sel import sel

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


# A cron run's own slot (``cron-<job_id>``, minted by ``inject_cron_result_to_dashboard``).
CRON_SLOT_PREFIX = "cron-"


# A background workflow's result slot (``workflow-<run_id>``, minted by
# ``workflow_inject`` only when the originating tab is gone).
WORKFLOW_SLOT_PREFIX = "workflow-"


# Slot-key prefixes for sessions no human is watching. As a TARGET both are
# always refused: a message would start a fresh agent turn in a display-only slot
# nobody reads.
#
# As a SOURCE the two differ, and the difference is ownership. What must not
# happen is a scheduled job reaching the user's OWN conversations, which is a
# question about scope rather than about attendance -- an unattended job already
# starts turns in the session that owns it every time it delivers with
# ``send_message(session="origin")``. A cron slot can be held to that scope,
# because `authorize_target` fences it to slots carrying its own
# ``_created_by`` (see :func:`_cron_caller`), so it is admitted as a source. A
# workflow result slot cannot: it exists only when the originating tab is already
# gone, so there is no owning session to fence it to and nothing it would
# legitimately dispatch. It stays refused.
#
# Membership here is the fail direction for a prefix added later: a new
# unattended surface is refused as a source until it is given a fence of its own.
UNATTENDED_SLOT_PREFIXES = (CRON_SLOT_PREFIX, WORKFLOW_SLOT_PREFIX)


# The ``linked_session_key`` a cron slot carries (``cron:<job_id>``), written by
# ``inject_cron_result_to_dashboard`` to bind the run's transcript to the tab.
# It is NOT a channel link, and the caller-side linked refusals exist for channel
# links: they keep a session whose conversation is mirrored to Slack/Telegram from
# reading a peer's transcript into that thread. A cron link mirrors nothing and
# has no audience, so it is exempted where the caller is judged. The TARGET-side
# refusal is deliberately not exempted -- a cron tab is already unreachable as a
# target by prefix, and the exemption would otherwise widen to every linked slot.
CRON_LINK_PREFIX = "cron:"


# The ``created_by`` tag an app's own cron job carries (``app:<app_name>``,
# written by ``apps/cron_sdk.py``). Spelled here rather than imported: this
# module sits below the apps package, and the value is a persisted data format
# rather than something that package exports.
APP_CRON_OWNER_PREFIX = "app:"


def _member_caller(state: "DashboardState", caller_key: str) -> bool:
    """Whether *caller_key* is a crew member acting through one of its slots.

    A crew member runs in TWO kinds of slot, and both are the member operating
    model rather than an optional capability, so the surface authorizes either
    WITHOUT the global ``agent.session_control`` opt-in. What bounds them
    instead is ownership: :func:`authorize_target` restricts a member caller to
    slots it created itself, so the automatic grant never reaches the user's
    own sessions.

    * (a) a pinned DM slot, keyed ``member-<slug>`` — recognised by the members
      module's own prefix constant (imported lazily, since ``members`` imports
      ``validation`` which sits below this module in the layering) rather than a
      restated literal, so the two cannot drift; and
    * (b) an ORDINARY dashboard chat slot (``chat-<n>-<ts>``) whose bound memory
      store is that member's private V2 store — the same store a DM slot would
      be bound to. A member's whole operating model (``session_create`` /
      ``session_send`` / ``session_read_message`` / ``session_stop`` /
      ``session_close``) also runs from such a chat slot, so refusing it there
      would leave the member chat-only in the surface it exists to drive. The
      store, not the key, is the member's identity here: it is what
      :func:`_store_is_member_owned` reads off the config record.

    Case (b) needs the caller's slot to read its bound store, hence *state*;
    ``member_dispatch`` gates the bypass either way (:func:`_member_bypass`).

    This predicate decides the switch BYPASS, re-read live at every gate so a
    member the operator un-assigns loses it at once — a change that can only
    tighten. It is not what keeps an admitted member creator-FENCED: the config
    record case (b) reads is mutable, so the HTTP gate carries its verified
    admission into :func:`authorize_target` (``precomputed_ownership_fenced``)
    rather than letting the fence re-derive it here a beat later.
    """
    from kiro_crew.members import DM_SLOT_KEY_PREFIX

    if caller_key.startswith(DM_SLOT_KEY_PREFIX):
        return True
    slot = state.get_slot(caller_key)
    if slot is None:
        return False
    return _store_is_member_owned(getattr(slot, "memory_store", "") or "")


def _store_is_member_owned(store: str) -> bool:
    """Whether *store* is a crew member's private V2 memory store, right now.

    The ONE predicate that recognises a member store, shared by the HTTP gate
    (``handlers/session_control.py``'s ``_private_caller_refusal``) and the inner
    switch bypass here (:func:`_member_caller` case (b)), so the two layers cannot
    disagree on what a member store is. It answers from the CONFIG RECORD, never
    the on-disk ownership manifest: it runs at :func:`authorize_target`'s
    synchronous gate, where a manifest ``stat`` / ``read`` would be blocking IO on
    the event loop. ``KiroCrewConfig.load()`` is the cached read the switch gate
    beside it already performs (warmed by :func:`prewarm_enabled_check`), and the
    fields it reads are in-memory attributes of the loaded record.

    ``True`` requires ALL of: a record for *store*; ``memory_version == 2``; a
    non-empty ``owner_member``; and that owner still an ACTIVE agent bound to
    exactly this store (the live-binding test ``active_member_memory_stores``
    applies). A crew can be deleted while a chat slot bound to its store is still
    live — the store record is retained with ``owner_member`` set but the agent is
    gone from ``cfg.agents`` — and a retired owner must not keep the switch bypass
    past a governance switch the operator turned off.

    Everything else is ``False``, and every ``False`` is FAIL-CLOSED for what this
    predicate decides — admission and the switch bypass: ``default``/empty, a
    missing record, a non-V2 store, an ownerless V2 store, a retired or re-bound
    owner, an unreadable config, or a degraded ``memory_stores`` section all
    withhold member status, and a caller then falls back under the global switch
    like any other. Withdrawing the case-(b) admission can never open the surface
    wider than it is.

    It is deliberately NOT the ownership FENCE's source of truth. The record is
    mutable — an operator's own config writer can un-assign the member, drop
    ``memory_version`` (the loader coerces a missing key to ``1``), or drop the
    entry outright — and any of those can land between the HTTP gate's admission
    and the inner authorization. The fence therefore does not re-derive member
    status from this record: the gate carries its VERIFIED admission into
    :func:`authorize_target` as ``precomputed_ownership_fenced`` (see
    ``handlers/session_control.py``), so a member admitted as one stays
    creator-fenced for the whole request whatever the record says a beat later.
    """
    if not store or store == "default":
        return False
    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        logger.warning(
            "session_control: config read failed — store %r is not treated as a member store",
            store,
            exc_info=True,
        )
        return False
    if cfg.degraded_sections & {DEGRADED_WHOLE_CONFIG, "memory_stores"}:
        logger.warning(
            "session_control: memory_stores config section degraded — store %r is not "
            "treated as a member store",
            store,
        )
        return False
    record = cfg.memory_stores.get(store)
    if record is None or getattr(record, "memory_version", 1) != 2:
        return False
    owner = getattr(record, "owner_member", "")
    if not owner:
        return False
    owners_bound_here = [
        member
        for member, agent in cfg.agents.items()
        if getattr(agent, "memory_store", None) == store
    ]
    return owners_bound_here == [owner]


def _cron_caller(caller_key: str) -> bool:
    """Whether *caller_key* is a cron job's own slot.

    A cron caller is admitted to the surface DESPITE being unattended, and is
    bounded the same way a crew member is: :func:`authorize_target` refuses it on
    any slot it did not create itself, so its reach covers the sessions it
    dispatched and never the user's own conversations.

    It differs from a member caller in one way that matters. A member bypasses
    the global ``agent.session_control`` switch, because dispatching into workers
    is the member operating model rather than an opt-in. A cron does NOT: the
    switch is the user's statement that agents may open and drive sessions at
    all, and a job running while they are asleep is the last caller that should
    be exempt from it.

    Keyed on the slot-key prefix rather than on the slot's ``linked_session_key``
    or its ``SlotOrigin``, so the answer is available before the slot is resolved
    and cannot change under a caller: a slot key is immutable, while both of the
    others are fields a later write could alter.
    """
    return caller_key.startswith(CRON_SLOT_PREFIX)


def _caller_is_ownership_fenced(state: "DashboardState", caller_key: str) -> bool:
    """Whether *caller_key* may only reach slots it created itself.

    Four populations, one predicate, so the fence and the admissions that depend
    on it cannot drift apart:

    * a crew member's DM slot, which bypasses the config switch;
    * a cron job's own slot, which bypasses the unattended refusal;
    * a channel-born slot admitted as the owner's own DM (:func:`owner_dm_refusal`
      answering ``""``), which bypasses the channel-link refusals. Every non-cron
      link is fenced, not only the admitted ones: the only linked caller that gets
      past those refusals is an owner DM, and fencing on the link rather than on
      the admission keeps the fence readable without the transport roster or the
      session store. What it buys is the bound on a wrong audience inference: the
      DM reaches the workers it dispatched and never the person's own tabs, the
      same reach a crew member has. A DASHBOARD-born slot the same predicate
      admits on its outbound mirror is deliberately not a fifth population: the
      fence follows authority, and that slot's authority is the person's own
      dashboard session -- the words that drive it are typed into the dashboard by
      the authenticated owner, and the mirror the predicate admitted changes who
      READS them (the owner, alone) and not who authored them. Fencing it on the
      mirror would take the owner's own reach away for the price of linking a tab
      to their phone, which is the one thing the admission exists to allow. When
      an AGENT made that slot it is fenced anyway, by the mark below;
    * **anything any of them created**, which is the part a key prefix cannot
      see. A created child is minted with a plain ``chat-`` key and INHERITS the
      creator's agent, so a fenced caller running a session-control agent would
      otherwise get an unfenced deputy for free: create a child, seed it, and the
      child -- an ordinary caller by key -- reads any same-workspace session and
      reports back through the transcript its creator is allowed to read. The
      fence has to follow authority, not spelling.

    ``_created_by`` is the marker for that last population and needs no lineage
    walk: :func:`create_session` is its ONLY writer (a person's own tab and a fork
    reach ``get_or_create_slot`` directly and stay unattributed), so a non-empty
    value means "an agent made this session" at any depth. An agent-created
    session is fenced regardless of which population its creator belonged to: the
    predicate is read for every verb through :func:`authorize_target`, so it
    answers "whose authority is this session" from the slot's own stamp alone and
    never widens on a creator that happened to be unfenced. The owner-rooted
    private-member dispatch that :func:`create_session` must still permit is
    decided at that gate (see :func:`_delegation_lineage_fenced`), not here, so
    this predicate's containment of every agent-created session stays intact.

    There is deliberately NO attendance exemption. ``_ChatSlot._human_seen`` looks
    like the right hatch and is not: it records that a human has EVER driven the
    slot, is monotonic and persisted, and says nothing about who authored the turn
    running now. Releasing the fence on it would hand the creator its deputy back
    for the price of the user glancing at the tab once -- cron creates the child,
    the user types into it, and from then on every cron-authored turn in that child
    runs unfenced. The question this predicate can answer is "whose authority is
    this session", not "is a person at the keyboard", so a person working in an
    agent-created session keeps that session's reach rather than their own.
    """
    if _member_caller(state, caller_key) or _cron_caller(caller_key):
        return True
    slot = state.get_slot(caller_key)
    if slot is None:
        return False
    if _channel_link_of(slot):
        return True
    if is_code_channel_slot(state, slot):
        # A Slack code channel's own session: dashboard-born, so no channel link,
        # and made by the code-channel runtime, so no ``_created_by`` -- by key it
        # would read as a person's own tab. It is not one: its words come from the
        # channel, and what it reads is published there. The code-channel dispatch
        # exemption admits it as a conductor ONLY on this fence, so it reaches the
        # workers it created and never the person's own sessions. Read on identity
        # alone (not on the exemption's live verdict), so the fence holds even
        # while the exemption is withheld.
        return True
    if getattr(slot, "_created_by", ""):
        return True
    # A code channel's worker whose transcript ``created_by`` was edited away is
    # still fenced: its lineage is also recorded in the write-protected code-channel
    # store at birth (``code_channel_root``), which only answers from memory here.
    return bool(code_channel_root(state, slot))


def _delegation_lineage_fenced(state: "DashboardState", caller_key: str) -> bool:
    """Whether *caller_key* may NOT dispatch a private-member worker.

    The private-member delegation gate in :func:`create_session` needs the one
    thing :func:`_caller_is_ownership_fenced` deliberately does not give it: a
    conductor the owner started in their own tab must be allowed to mint private
    workers, while a conductor rooted in a cron, channel link or crew member must
    not. The shared predicate answers "every agent-created session is fenced" for
    the ownership boundary read on every verb, and that answer must stay intact;
    this walk is read ONLY here, at the once-per-create delegation gate, so it
    never widens :func:`authorize_target`.

    It climbs the ``_created_by`` chain LIVE at each hop -- a creator that has
    since become a crew member, acquired a channel link, or is a cron tab fences
    the whole chain the moment it does, so a mid-chain takeover cannot leave a
    stale "unfenced" behind (there is no frozen verdict to go stale). The root of
    an owner-rooted chain is a person's own unattributed tab, which reaches
    ``get_or_create_slot`` directly and carries no ``_created_by`` and no fence
    source, so the walk ends unfenced. It fails CLOSED on any gap: a hop whose
    creator slot is gone, or a chain longer than the depth bound, is fenced, so a
    chain whose middle slot was closed loses dispatch rather than widening.
    """
    seen: set[str] = set()
    key = caller_key
    # The chain is at most as deep as live slots, but a bound keeps a corrupted
    # ``_created_by`` cycle from spinning; any chain this long is treated as a
    # gap and fails closed.
    for _ in range(64):
        if key in seen:
            return True
        seen.add(key)
        if _member_caller(state, key) or _cron_caller(key):
            return True
        slot = state.get_slot(key)
        if slot is None:
            # Mid-chain creator gone: cannot prove the root is the owner's, so
            # fail closed rather than treat an unreadable ancestor as unfenced.
            return key != caller_key
        if _channel_link_of(slot):
            return True
        if is_code_channel_slot(state, slot):
            # A code channel's session is not the owner's own tab: its words come
            # from the channel. A chain rooted there may not mint private workers.
            return True
        parent = getattr(slot, "_created_by", "")
        if not parent:
            # Unattributed root -- a person's own tab or a fork. Owner-rooted:
            # the one chain the delegation gate exists to permit.
            return False
        key = parent
    return True


def _channel_link_of(slot: Any) -> str:
    """*slot*'s channel link, or ``""`` for an unlinked slot and for a cron tab.

    The one reading of "this slot is channel-born" the caller-side gates share: a
    ``cron:<job_id>`` link names the job's own run transcript and republishes to
    nobody, so it is not a channel link (see ``CRON_LINK_PREFIX``).
    """
    link = str(getattr(slot, "linked_session_key", "") or "")
    if not link or link.startswith(CRON_LINK_PREFIX):
        return ""
    return link


def _created_by_other(slot: Any, caller_key: str) -> bool:
    """Whether *slot* was not created by *caller_key*.

    Fail-closed on an unowned slot, which is what an ownerless rehydrate looks like:
    a blank ``_created_by`` matches no caller, so a fenced caller does not reach it.
    """
    return getattr(slot, "_created_by", "") != caller_key


def _app_owned_cron_refusal(state: "DashboardState", caller_key: str) -> tuple[str, str] | None:
    """``(message, code)`` when *caller_key* is an APP's cron, else ``None``.

    An app-scoped SESSION is refused by the ``_app`` check that sits beside every
    call site of this one, but a cron tab does not carry that tag:
    ``inject_cron_result_to_dashboard`` mints it without ``app=``, so an app's own
    scheduled job reaches this surface with ``_app == ""`` and would pass. Left
    unchecked, an app could create a persistent, sidebar-visible session that is
    NOT app-scoped -- precisely the confinement escape the ``_app`` refusal exists
    to prevent, reached through the app's scheduled job instead of its session.

    App ownership therefore has to be read from the JOB, and it has TWO spellings
    there because two writers record it differently: the app cron SDK tags
    ``created_by = "app:{app_name}"``, while ``mcp_cron``'s own ``cron_add``
    records the calling session in ``session_key`` and never writes ``created_by``
    at all -- so an app-scoped session's job carries its authority only in the
    second. Both are checked, and the second delegates to ``_app`` on the owning
    slot rather than re-deriving app-ness, so there is one definition of "is this
    an app" and not a third. **A new field on the job that can name a principal is
    a hole here until it is added to this function.**

    Fail-CLOSED when ``session_key`` names a closed session: its
    ``_app`` cannot be read, and "could not verify the owner is not an app" must
    not read as "has no owner". ``mcp_cron``'s ``cron_add`` records an app's
    authority ONLY in ``session_key`` -- so once that slot is gone, allowing the
    job would let an app escape confinement through a cron it authored and then
    abandoned by closing its session. The cost is a genuinely user-created
    dispatching cron whose authoring tab has closed is refused too; that caller
    can reopen a tab, whereas an app session minted outside its confinement cannot
    be undone. This is the same fail-closed direction the missing-job case below
    takes, and what still bounds the app case beyond it is that a LIVE app session
    has its jobs refused directly by the ``_app`` read.

    Fail-CLOSED on a job that cannot be found, or a registry that cannot answer:
    "could not verify the owner" must not read as "has no owner", the same
    direction ``agent_unverifiable`` takes on its own unreadable input. Nothing
    legitimate is refused by it, because a cron whose job is gone is not running.

    Returns rather than raises so each call site keeps its own idiom -- the create
    path raises ``SessionControlError`` directly, while ``authorize_target`` must
    go through its ``deny`` closure to get the audit record and the 403.
    """
    if not _cron_caller(caller_key):
        return None
    job_id = caller_key[len(CRON_SLOT_PREFIX) :]
    found: Any = None
    try:
        for job in state.crons.list_jobs(include_disabled=True):
            if str(getattr(job, "id", "")) == job_id:
                found = job
                break
    except Exception:
        logger.warning(
            "session_control: cron owner lookup failed for %s -- refusing",
            caller_key,
            exc_info=True,
        )
        found = None
    if found is None:
        return (
            "the scheduled job behind this session could not be found, so its "
            "ownership cannot be verified",
            "cron_owner_unverifiable",
        )
    refusal = (
        "app-owned scheduled jobs cannot create or control sessions",
        "app_owned_cron_caller",
    )
    if str(getattr(found, "created_by", "") or "").startswith(APP_CRON_OWNER_PREFIX):
        return refusal
    owning_key = str(getattr(found, "session_key", "") or "")
    if owning_key:
        # Resolved through this module's own :func:`caller_slot_key` rather than a
        # ``removeprefix("dashboard:")``: chat_utils documents that the naive strip
        # is wrong for every non-dashboard session key, and the resolver already
        # matches on the identity each slot actually writes.
        owning_slot = state.get_slot(caller_slot_key(state, owning_key))
        if owning_slot is None:
            # The job names an owning session, but no live slot carries that key
            # any more -- the authoring tab was closed or evicted. Its ``_app``
            # tag lived only on that slot (``mcp_cron``'s ``cron_add`` records the
            # caller in ``session_key`` and never writes ``created_by``), so the
            # one place app-ness could be read is gone. That is precisely the
            # confinement escape: an app creates a cron through ``cron_add``,
            # closes its session, and its scheduled job then dispatches a
            # persistent, non-app, sidebar-visible session this gate then cannot
            # recognise as the app's.
            #
            # "Could not verify the owner is not an app" therefore fails CLOSED,
            # the same direction the missing-job and unreadable-registry cases
            # above take. This narrows the docstring's former "known residual":
            # the residual was an accepted fail-OPEN, and a fail-open on an
            # unresolvable owner is a security-gate defect (anchor
            # backend-security-controls). The cost is that a genuinely
            # user-created dispatching cron whose authoring tab has closed is
            # refused too -- but that caller can reopen a tab, whereas nothing can
            # undo an app session minted outside its confinement.
            return (
                "the session that authored this scheduled job is no longer open, "
                "so its ownership cannot be verified",
                "cron_owner_unverifiable",
            )
        if str(getattr(owning_slot, "_app", None) or ""):
            return refusal
    return None


class SessionControlError(Exception):
    """A refusal carrying the HTTP status AND the machine-readable reason.

    ``code`` is the contract the dashboard and the MCP tools match on; ``message``
    is advisory English prose (RFC 9457 3.1.3). Prose alone would be
    untranslatable by construction, since callers render it verbatim.
    """

    def __init__(
        self, message: str, status: int = 400, code: str = "session_control_error"
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def session_control_enabled() -> bool:
    """Whether the session-control surface is switched on in config.

    A config read that RAISES resolves to disabled, not to the field's default.
    ``load()`` can fail on a malformed section that has nothing to do with this
    feature, and treating that as "enabled" would let unrelated corruption
    silently undo an explicit ``session_control: false`` — the one switch
    standing between two of the user's sessions. Failing closed costs a
    refusal the user can diagnose from the log line; failing open costs the
    opt-out.
    """
    try:
        return bool(KiroCrewConfig.load().agent.session_control)
    except Exception:
        logger.warning(
            "session_control: config read failed — refusing until config loads", exc_info=True
        )
        return False


def member_dispatch_enabled() -> bool:
    """Whether a crew member's DM session may bypass the ``session_control`` switch.

    The operator ceiling on the zero-configuration member grant. Default true
    reproduces today's behaviour exactly: a member caller bypasses
    ``agent.session_control`` and dispatches into workers it created. Set
    ``agent.member_dispatch`` to false and a member caller stops bypassing —
    it falls back under ``session_control_enabled()`` like any ordinary caller,
    so an operator who turned session control off keeps member DM threads
    chat-only without disabling the member itself.

    Fails CLOSED in BOTH ways the ceiling can lose the operator's value, the
    same direction :func:`session_control_enabled` does:

    * a config read that RAISES resolves to false; and
    * a config that LOADS but discarded the ``agent`` section (or the whole
      file) resolves to false too. ``load()`` does not raise on a malformed
      section -- it coerces it away, falls back to the field default (which is
      ``member_dispatch=True``, permissive), and records the loss in
      ``degraded_sections``. Without this second check a degraded ``agent``
      overlay carrying ``member_dispatch: false`` would silently revert to the
      bypass the operator meant to withdraw -- a governance-ceiling fail-open.
      This is the same "could not read it" vs "was never set" distinction
      :func:`tailnet_identity_unknown` and the publish gate already draw from
      ``degraded_sections``. The bypass never fails open.
    """
    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        logger.warning(
            "member_dispatch: config read failed — withdrawing member bypass until config loads",
            exc_info=True,
        )
        return False
    if cfg.degraded_sections & {DEGRADED_WHOLE_CONFIG, "agent"}:
        # The agent section (or the whole file) was discarded, so a stored
        # `member_dispatch: false` was replaced by the permissive default.
        # Withdraw the bypass rather than trust that default.
        logger.warning("member_dispatch: agent config section degraded — withdrawing member bypass")
        return False
    return bool(cfg.agent.member_dispatch)


def member_admitted_to_scoped_surface(session_key: str, store: str) -> bool:
    """Whether a scoped caller is a crew member reaching a member-open surface.

    The ONE predicate the two surface gates that admit member callers share --
    the session-control HTTP gate (``handlers/session_control.py``'s
    ``_private_caller_refusal``) and the chat folder/tag gate
    (``handlers/_shared_route_guards.py``'s ``private_chat_route_refusal``) -- so the two
    cannot drift on who a member is or when the surface is reachable for it.

    A caller is admitted when BOTH hold:

    * it is a crew member -- either an ``member-*`` DM slot key
      (:func:`is_member_session_key`) OR an ordinary chat slot bound to a crew
      member's private V2 store (:func:`_store_is_member_owned`, reading the
      VERIFIED scope the gate already resolved); AND
    * the surface is reachable for a member -- its own ``agent.member_dispatch``
      bypass OR the global ``agent.session_control`` switch it otherwise falls
      back under, since ``member_dispatch`` is a bypass ON TOP of the switch.

    This is the surface-level, caller-independent reachability only. It never
    decides which folders or sessions the admitted member may touch -- that
    ownership fence is per-resource and lives with each route
    (``member_owns_slot`` for filing/tagging, ``owner_app``/``folder_principal``
    for the tree). All reads run off the loop and fail closed on an unreadable
    config, so this gate can never open wider than the two switches behind it.
    """
    from kiro_crew.members import is_member_session_key

    is_member = is_member_session_key(session_key) or _store_is_member_owned(store)
    return is_member and (member_dispatch_enabled() or session_control_enabled())


def member_owns_slot(state: "DashboardState", slot: Any, caller_key: str) -> bool:
    """Whether *caller_key* (a member) may FILE or TAG *slot*.

    The member analogue of the app path's ``_app`` / ``app_owns_transcript``
    ownership check, and the SAME fence :func:`_caller_is_ownership_fenced`
    draws for session-control targets, so a member reaches through the chat
    folder/tag routes exactly the sessions it reaches through session-control
    and no others:

    * (a) its OWN session -- the slot whose slot key is the caller's own; and
    * (b) a session it CREATED -- ``_created_by == <caller's slot key>``, the
      value :func:`create_session` stamps on every child a member mints.

    ``caller_key`` is the VERIFIED ``X-Session-Key`` the gate authorized on --
    a SESSION key (``effective_session_key``, e.g. ``dashboard:chat-20-...``),
    never a body value. :func:`create_session` stamps ``_created_by`` with a
    SLOT key (``caller_slot_key(state, ...)``, e.g. ``chat-20-...``), so the two
    live in DIFFERENT key spaces: a raw ``_created_by == caller_key`` compare
    never matches a created child, because it compares a slot key to a session
    key. Resolve the caller to its slot key ONCE through :func:`caller_slot_key`
    (the same map :func:`create_session` writes the field through) and compare in
    that one space: ``_created_by`` and the slot's own ``key`` are both slot
    keys.

    The ``effective_session_key(slot) == caller_key`` arm stays as a
    session-space fallback: a channel-born slot whose key the live slot map
    cannot resolve (``caller_slot_key`` returns ``""``) is still owned when its
    session key IS the caller. Everything else is refused: the person's own
    sessions, an app's sessions, and another member's sessions all fail every
    arm.
    """
    if not caller_key or slot is None:
        return False
    resolved_slot_key = caller_slot_key(state, caller_key)
    if resolved_slot_key:
        created_by = getattr(slot, "_created_by", "")
        if created_by and created_by == resolved_slot_key:
            return True
        if getattr(slot, "key", "") == resolved_slot_key:
            return True
    return effective_session_key(slot) == caller_key


def _member_bypass(state: "DashboardState", caller_key: str) -> bool:
    """Whether *caller_key* may skip the ``session_control`` switch as a member.

    The single expression both switch gates key on, extracted rather than
    copy-pasted so the member-bypass condition cannot drift between
    :func:`create_session` and :func:`authorize_target`. A member caller
    bypasses only while the operator ceiling ``agent.member_dispatch`` is on;
    turned off, the member is not exempt and the switch gate applies to
    it like any other caller.

    Keyed on :func:`_member_caller` (a ``member-`` DM slot OR a chat slot bound
    to a member's V2 store — hence *state*) AND the config ceiling: the two
    together decide the bypass, and neither is a proxy for it.
    ``member_dispatch_enabled`` is read at the gate, synchronously, right before
    the act, exactly as ``session_control_enabled`` is beside it.
    """
    return _member_caller(state, caller_key) and member_dispatch_enabled()


async def prewarm_enabled_check() -> None:
    """Warm the config cache in a thread so the sync gate reads the cached path.

    :func:`session_control_enabled` cannot await -- ``authorize_target`` is
    synchronous, and ``read_messages`` is synchronous with it -- but its
    ``KiroCrewConfig.load()`` re-reads and validates the file on the FIRST call
    after a config edit, and doing that inline blocks the loop for every other
    session.

    Must be called with NOTHING that suspends between it and the gate. An
    ``await`` in that gap reopens exactly the hole this closes: a config edit
    landing in the window changes the fingerprint, so the gate's own read misses
    the cache and does the synchronous read anyway. That is why this is not done
    once at the top of each handler -- reading a request body suspends, and so
    does the SEL prewarm inside ``stop_target``.

    Lives here rather than in the handlers so the three call sites share one
    implementation, and so ``stop_target`` can warm it after its own prewarm
    without the handler layer reaching back into it.

    Best-effort: a failure is the gate's business, and the gate fails closed on
    its own.
    """
    # The code-channel store too: the ownership fence and the worker depth check
    # read its loaded snapshot synchronously (``code_channel_root``), and a store the
    # Slack runtime never loaded (Slack not connected after a restart) would leave a
    # worker whose transcript lineage was edited away unrecognised. Loaded first, so
    # the config warm below stays the LAST await before the gate.
    await load_code_channel_lineage()
    try:
        await asyncio.to_thread(session_control_enabled)
    except Exception:  # pragma: no cover - the gate re-reads and decides
        logger.debug("session-control config prewarm failed; the gate will read it inline")


def caller_slot_key(state: "DashboardState", session_key: str) -> str:
    """Map a caller's session key to its slot key, or ``""`` when unknown.

    The MCP process authenticates as a session key (the history key), while
    every operation here is slot-keyed. Resolution walks the live slots and
    matches on the key each slot actually writes, which is the same identity
    ``list_sessions`` reports — so "who am I" cannot disagree between the two.

    An unresolvable caller is not fatal: it only means the self-target guard has
    nothing to compare against, which :func:`authorize_target` treats as a
    refusal rather than a pass.
    """
    if not session_key:
        return ""
    for slot in list(state._slots.values()):
        try:
            history_key = slot_history_key(slot)
            if session_key in (history_key, slot.key, transcript_stem(history_key)):
                return slot.key
        except Exception:
            continue
    return ""


@contextmanager
def _audit_denials(*, caller_session_key: str, operation: str, slot_key: str) -> Iterator[None]:
    """Record a verb's OWN refusals as denied, then re-raise them untouched.

    :func:`_audit` is easy to reach on the success path and easy to miss on every other
    one, because a refusal LEAVES the verb by raising -- so the allowed call at the end
    is simply not executed and the denial goes unrecorded. That is exactly the shape a
    caller probing a permission boundary produces: every attempt refused, and none of
    them appearing anywhere. ``backend-security-controls`` requires a SEL event for each
    permission decision, and a boundary with no trail is the one most worth a trail.

    One helper rather than a handler written into each verb: two would drift, and the
    audited fact is the same in both. It wraps the region AFTER the authorization gate
    has named a target, which is what gives the record a ``slot_key`` to be about -- a
    refusal with no resolved target has nothing to attribute.

    The exception is re-raised unchanged, so this only ever ADDS the record: the status,
    code and message the caller receives stay the verb's own.
    """
    try:
        yield
    except SessionControlError as exc:
        _audit(
            caller_session_key=caller_session_key,
            operation=operation,
            slot_key=slot_key,
            outcome="denied",
            detail={"code": exc.code},
        )
        raise


def _audit(
    *,
    caller_session_key: str,
    operation: str,
    slot_key: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record one completed session-control operation in the SEL.

    Logged as a tool invocation rather than an API access because that is what
    it is from the caller's side, and because it carries the per-call detail
    (how the message landed, whether a stop escalated) that makes the audit
    line answer "what actually happened to the other session".

    Dispatched OFF the loop when one is running, mirroring
    ``update_metadata_off_loop``. ``log_tool_invocation`` only enqueues, but the
    FIRST ``sel()`` of a process CONSTRUCTS the log -- trust-dir creation and
    key validation, blocking file IO -- and this can genuinely
    be that first call: ``sel_audit_middleware`` logs AFTER ``await handler(...)``,
    so on a fresh gateway the first authenticated request constructs the log
    inside whatever handler runs first. Offloading here covers every call site
    without adding a step to the boot path -- which the boot-path rule forbids and
    a background prewarm would only race rather than close.
    """

    def _do() -> None:
        sel().log_tool_invocation(
            session_key=caller_session_key,
            agent="",
            source="mcp",
            tool_name=f"session_{operation}",
            tool_kind="command",
            outcome=outcome,
            resources=f"target={slot_key}",
            metadata=dict(detail or {}, target=slot_key),
        )

    _sel_off_loop(_do, "session-control audit")


def _sel_off_loop(write: "Callable[[], None]", what: str) -> None:
    """Run one SEL write off the event loop, best-effort.

    Shared by every session-control SEL write so the property holds in one place
    instead of per call site -- the denial audit was the THIRD site of this class
    to be found separately, having been missed while the other two were fixed.

    Two failure modes, both handled: a loop-blocking construct (a ``sel()`` that
    creates the trust dir and validates keys — blocking file IO), and a construct
    that RAISES, which unguarded turns a 403 into a
    500 -- losing the refusal in order to report it. An audit that cannot be
    written must never change what the caller is told.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            write()
        except Exception:  # noqa: BLE001 - an audit failure must not fail the op
            logger.warning("%s failed inline", what, exc_info=True)
        return

    def _report(fut: "asyncio.Future[None]") -> None:
        exc = fut.exception()
        if exc is not None:
            logger.warning("%s failed off-loop: %r", what, exc)

    loop.run_in_executor(None, write).add_done_callback(_report)


# ── Code-channel identity ──────────────────────────────────────────────────
# Whether a slot IS a Slack code channel's own session, read server-side: the
# code-channel store (``slack-code-channels.json``, write-protected on disk and
# held in memory once loaded) must hold a binding naming the slot's key, the
# binding's anchor must resolve to the SAME slot object through the live
# Slack-link index, and the slot's own Slack link must name the binding's
# channel. A title, key prefix or agent name is never read. It lives in this,
# the identity layer, because the creator fence below reads it; the dispatch
# exemption (``code_channel_exemption``) builds on it.

_SLACK = "slack"


def _bare_channel(value: Any) -> str:
    """A Slack channel id with any ``slack:`` namespace removed."""
    text = str(value or "")
    return text.split(":", 1)[1] if text.startswith(f"{_SLACK}:") else text


def code_channel_binding_of(state: "DashboardState | Any", slot: Any) -> Any | None:
    """The code-channel binding whose session IS *slot*, or ``None``.

    Synchronous with no disk access (callable inside a no-suspension window): the
    store's in-memory snapshot (``None`` before it is loaded, which reads as "not a
    code channel"), cross-checked against the live Slack-link index and the slot's
    own link. A forged or stale row naming some slot fails unless that slot is ALSO
    linked to the channel's own anchor.
    """
    key = str(getattr(slot, "key", "") or "")
    if state is None or slot is None or not key:
        return None
    try:
        from kiro_crew.slack.code_channels.store import store

        bindings = store().loaded_bindings()
    except Exception:
        logger.debug("code-channel store unreadable", exc_info=True)
        return None
    if not bindings:
        return None
    for binding in bindings:
        if binding.session_key != key or not binding.anchor_ts:
            continue
        try:
            linked = state.get_linked_slot(binding.anchor_ts)
        except Exception:
            logger.debug("Slack link lookup failed", exc_info=True)
            return None
        if linked is not slot:
            continue
        if _bare_channel(getattr(slot, "_slack_channel", "")) != binding.channel_id:
            continue
        return binding
    return None


def is_code_channel_slot(state: "DashboardState | Any", slot: Any) -> bool:
    """Whether *slot* is a code channel's own session (identity only; fails as "no")."""
    try:
        return code_channel_binding_of(state, slot) is not None
    except Exception:  # pragma: no cover - the reader fails closed itself
        logger.debug("code-channel identity check failed", exc_info=True)
        return False


def _slots_sharing_identity(state: "DashboardState | Any", slot: Any) -> list[Any]:
    """*slot* and every other live slot a caller key resolving to it could name.

    ``caller_slot_key`` returns the FIRST slot whose history key, slot key or
    transcript stem matches; two slots that answer to the same key are therefore
    indistinguishable to a caller, and a fence decided on one must hold for both.
    """
    out = [slot]
    try:
        names = {str(getattr(slot, "key", "") or "")}
        history = slot_history_key(slot)
        names |= {history, transcript_stem(history)}
    except Exception:
        return out
    names.discard("")
    for other in list(getattr(state, "_slots", {}).values()):
        if other is slot:
            continue
        try:
            other_history = slot_history_key(other)
            other_names = {other.key, other_history, transcript_stem(other_history)}
        except Exception:
            continue
        if names & other_names:
            out.append(other)
    return out


async def load_code_channel_lineage() -> None:
    """Load the code-channel store OFF the event loop, before a gate reads it.

    :func:`code_channel_root` runs inside synchronous gates on the loop and reads
    only the loaded snapshot, refusing when there is none; :func:`prewarm_enabled_check`
    -- which every session-control verb awaits right before its synchronous gates --
    and the work-ledger write route await this first, so the one cold read never
    blocks the loop and no verb judges a fence on an unloaded store. A store
    the Slack runtime already loaded makes this a no-op. Never raises: a failed
    read is remembered by the store and the gate refuses on it.
    """
    try:
        from kiro_crew.slack.code_channels.store import store

        current = store()
        if current.loaded_bindings() is None:  # loaded once; afterwards no thread hop
            await asyncio.to_thread(current.load)
    except Exception:  # pragma: no cover - the store swallows its own read errors
        logger.debug("code-channel store load failed", exc_info=True)
    # The dispatch switch's config-path reading, for the same reason: the gate that
    # reads it is synchronous and must not stat files on the loop. Skipped (no
    # thread hop) while code channels are off: the switch refuses before it looks.
    from kiro_crew.dashboard.session_control_parts import code_channel_exemption as _cce
    from kiro_crew.slack.code_channels.binding import code_channels_on

    try:
        on = code_channels_on(_cce._live_config())
    except Exception:  # pragma: no cover - an unreadable config: the switch is off anyway
        on = False
    if on:
        await _cce.refresh_config_path_check()


#: What :func:`code_channel_root` answers when the code-channel store's file could
#: not be read: no session can be shown NOT to be a code-channel worker, so every
#: gate reading the answer as truthy fences and refuses. Not a slot key (slot keys
#: never contain ``<``), so it can never name a real session.
LINEAGE_UNAVAILABLE = "<code-channel-lineage-unavailable>"

#: The refusal a caller sees while :data:`LINEAGE_UNAVAILABLE` holds.
LINEAGE_UNAVAILABLE_MESSAGE = (
    "the Slack code-channel store (slack-code-channels.json) could not be read, so "
    "this session cannot be shown not to be a code-channel worker; repair the file "
    "and restart the gateway"
)


def _code_channel_store_unreadable() -> bool:
    """Whether the loaded code-channel store's file could not be read. No disk access."""
    try:
        from kiro_crew.slack.code_channels.store import store

        return bool(store().unreadable)
    except Exception:  # pragma: no cover - fail closed on an import failure
        return True


def code_channel_root(state: "DashboardState | Any", slot: Any) -> str:
    """The key of the code-channel session *slot* was created under, ``""`` if none.

    A session a code channel's session created -- at any depth -- is a WORKER, which
    the dispatch exemption keeps one level deep: it may not create sessions or
    conduct. Decided from PERSISTED facts first, so it holds after the conductor's
    tab is closed and the gateway restarts: the worker record ``create_session``
    writes into the code-channel store at birth (server-owned, so editing the
    worker's own transcript cannot erase it), then the slot's own ``_created_by`` (written
    at birth, rehydrated on restart) checked against every session key a code
    channel runs in or ever ran in (the store's ``session_keys``, never evicted;
    a store that is not loaded or could not be read refuses -- callers load it
    off the loop first with :func:`load_code_channel_lineage`). The live
    ``_created_by`` chain is then climbed for a deeper ancestor, bounded so a
    corrupted cycle cannot spin.
    """
    parent = str(getattr(slot, "_created_by", "") or "")
    try:
        from kiro_crew.slack.code_channels.store import store

        # First the record ``create_session`` wrote into the code-channel store at
        # the worker's birth: server-owned, so it holds even if the worker's own
        # transcript ``created_by`` was edited away before a restart. Checked for
        # EVERY live slot sharing this slot's identity, not only the one resolved:
        # a transcript's editable linkage can make a worker present another
        # session's key, and resolution takes the first match.
        for same in _slots_sharing_identity(state, slot):
            recorded_conductor = store().worker_conductor(str(getattr(same, "key", "") or ""))
            if recorded_conductor:
                return recorded_conductor
        if not parent:
            return ""
        recorded = store().session_keys()
    except Exception:
        # An unreadable store cannot vouch that this session is NOT a code-channel
        # worker, and the slot's own ``_created_by`` cannot either: a worker can
        # edit it away in its transcript before a restart. So a store whose file
        # could not be read fences EVERY session the gate judges, whatever its
        # ``created_by`` says, until a person repairs the file and restarts
        # (:data:`LINEAGE_UNAVAILABLE`). A store merely not loaded yet -- a caller
        # that skipped :func:`load_code_channel_lineage` -- keeps the transcript
        # answer.
        logger.debug("code-channel store unreadable", exc_info=True)
        if _code_channel_store_unreadable():
            return LINEAGE_UNAVAILABLE
        return parent
    seen: set[str] = set()
    for _ in range(64):
        if not parent or parent in seen:
            return ""
        if parent in recorded:
            return parent
        seen.add(parent)
        try:
            creator = state.get_slot(parent)
        except Exception:
            return ""
        if creator is None:
            return ""
        if is_code_channel_slot(state, creator):
            return parent
        parent = str(getattr(creator, "_created_by", "") or "")
    return ""
