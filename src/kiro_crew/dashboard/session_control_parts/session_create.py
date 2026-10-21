"""``session_create``: opening a new session for a caller, and the spawn record a
created or forked child gets.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from kiro_crew import model_scope
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.config.loader import (
    KiroCrewConfig,
    _workspace_name_for_dir,
    default_project_dir,
    member_template_id,
    resolve_agent_bindings,
)
from kiro_crew.config.sections import (
    DEFAULT_MODEL,
    KiroCrewAgentConfig,
    coerce_effort,
    worker_session_defaults,
)
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard.chat_delivery import sanitize_outbound
from kiro_crew.dashboard.chat_folders import (
    _folder_declared_project,
    _resolve_folder_project_dir,
    _unhide_folder,
)
from kiro_crew.dashboard.chat_utils import _normalize_model, drained_to_thread, slot_history_key
from kiro_crew.dashboard.create_rate_limit import SESSION_CREATE, allow_create, has_create_budget
from kiro_crew.dashboard.session_control_parts.caller_access import (
    UNATTENDED_SLOT_PREFIXES,
    SessionControlError,
    _audit,
    _channel_link_of,
    _cron_caller,
    _delegation_lineage_fenced,
    _member_bypass,
    _member_caller,
    caller_slot_key,
    session_control_enabled,
)
from kiro_crew.dashboard.session_control_parts.session_titles import _clean_new_session_title
from kiro_crew.dashboard.session_control_parts.target_authorization import (
    _refuse_ineligible_creator,
    _refuse_moved_caller_identity,
)
from kiro_crew.dashboard.state import (
    MAX_LIVE_SLOTS,
    MAX_SLOTS_PER_CREATOR,
    SlotOrigin,
    _safe_folder_tree,
)
from kiro_crew.effort import EFFORT_LEVELS, is_valid_effort
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    member_config_for_id,
    read_session_execution,
    read_vouched_session_execution,
    refresh_vouched_session_execution,
    resolve_member_execution,
    revouch_at_verified_admission,
)
from kiro_crew.history import metadata_now_iso
from kiro_crew.memory_stores import named_store_or_empty
from kiro_crew.security import redact
from kiro_crew.validation import _MODEL_NAME_RE, MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


async def create_session(
    state: "DashboardState",
    *,
    caller_session_key: str,
    title: str = "",
    agent: str = "",
    agent_kind: str = "",
    folder_id: str = "",
    model: str = "",
    reasoning_effort: str | None = None,
    caller_fenced: bool | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Open a new session in the caller's workspace, persisted at birth.

    The new slot is an ordinary dashboard session -- it appears in the sidebar, the
    user can read it, type into it and close it -- so this gives a workstream a home
    of its own rather than a private channel the user cannot see. It starts empty:
    the person is the one who types the first message into it.

    The caller's own eligibility is checked against the SAME caller-side refusal
    set `authorize_target` applies (`authorize_target` cannot be reused here:
    there is no target yet), and the child inherits the caller's workspace. Both
    matter because a caller refusal missing here, or a workspace not inherited,
    would hand back a session outside the boundary the other verbs enforce.

    The caller's session POSTURE (``_trust`` / ``_trust_reads``) transfers to the
    child, so a trusted operator's dispatched worker does not stall on a prompt
    nobody is watching -- the posture ``parent_trusted`` already gives a
    ``spawn_run`` subagent. Per-command grants (``_trusted_patterns``) and a
    ``SafetyOverride`` scoped grant (``_trust_scope``) are both deliberately
    excluded, and the transferred value is the one held at allocation time rather
    than at entry, so revoking mid call yields an untrusted child. See the block
    around the assignment.

    ``folder_id`` files the slot as part of creation: it is assigned in
    the same synchronous window that configures the slot, the whole
    allocation-to-persist span runs under ``suspend_slots_push`` so the slot's
    first broadcast frame already shows it filed, and the placement rides in the
    persist-at-birth metadata so it survives a restart. An unknown folder
    refuses the whole create -- nothing exists yet, so refusal loses nothing,
    matching the move path's posture -- and existence is confirmed READ-ONLY
    under the folder-store lock (``read_folders``) before the allocation; the
    Model-B un-hide runs only after the filing has landed, so a refused create
    leaves no folder-tree mutation behind. Authorization needs no new path: the
    folder tree cannot be reshaped from here (the id must already exist), and
    every caller class the move path's app-ownership rule exists to stop is
    already refused above it -- an app-scoped caller cannot create a session at
    all (`app_scoped_caller`).

    ``model`` pins the model the child starts on, as the person's own pick in the
    model dropdown would: same rejection guard, same pick-generation bump, and
    recorded in the persist-at-birth metadata so an idle child keeps it across a
    restart. It is not a privilege the caller lacks -- ``spawn_run`` already
    takes a per-run ``model`` -- and it moves no memory boundary.
    ``reasoning_effort`` pins the level the same way, one of the five canonical
    levels ``session_set_model`` takes; any other value is refused
    ``effort_rejected`` before anything is allocated.

    Either one left EMPTY takes the matching explicit pin from the crewmate
    that is dispatching: the caller's member record
    (:func:`_dispatching_crewmate`) supplies its ``worker_model`` /
    ``worker_reasoning_effort`` when set, else its explicit ``model`` /
    ``reasoning_effort`` pin (:func:`worker_session_defaults`). With no matching
    profile pin, the child remains unpinned and its own agent's or the global
    default decides. An explicit argument always wins, and the inherited model
    runs the same guard an explicit one does.

    ``caller_fenced`` is the ownership-fence verdict the HTTP gate already settled
    on the caller's VERIFIED scope, carried in for the same reason
    ``authorize_target`` takes ``precomputed_ownership_fenced``: the inline
    predicate re-derives member status from the MUTABLE config record, and this
    coroutine suspends many times before it is consulted. ``None`` means "not
    settled", and the fence is then evaluated inline. It is read by the
    private-store authorization below and nothing else.

    ``agent_kind`` is the selection NAMESPACE for ``agent``, and this route takes
    one value: ``"template"`` names the shared provider template of that name,
    and ``""`` keeps the name-first resolution (a crew member wins over a
    same-named template). It exists for the one case the bare name cannot
    express: a template that a crew member of the same name shadows. The stock
    ``kirocrew-worker`` is such a name on installs where an older agent sync
    enrolled it as a crew member with its own private memory -- a bare
    ``agent="kirocrew-worker"`` then selects the MEMBER, and a conductor that may
    not bind that member's store (a member DM thread, a fenced deputy) is refused
    ``memory_delegation_denied`` for a worker that needs no memory of its own.
    ``agent_kind="template"`` dispatches the worker template on the caller's own
    store instead, exactly as the chat route's explicit template pick "never pins
    member memory". There is no ``"member"`` kind here: a bare name already
    selects the member, so the value would only restate that resolution. The kind
    is selection input, never authority: every gate below still applies, and a
    template child takes the caller's store the same way an unshadowed template
    already does. It is recorded on the slot (``agent_kind``) and in the birth
    metadata, so a restart rehydrates the child in the namespace it was committed
    in rather than re-resolving the bare name. A stated kind requires ``agent``:
    the namespace qualifies a selection, and an inherited caller agent is not one.

    ``dry_run`` runs every gate up to the allocation, including the two slot
    ceilings, and returns ``{"dry_run": True}`` instead of minting a slot. It
    spends no rate-limit token and writes nothing. The MCP ``session_create``
    asks it first when its ``folder`` path still has segments to create, so a
    create that would be refused is refused BEFORE any folder exists: without
    it the path walk's folders outlive the refusal as empty sidebar rows.
    ``folder_id`` is then the deepest folder that already exists, which is the
    one the new segments would inherit a project directory from.
    """
    # Validated before any gate, as `rename_target` does: a malformed title
    # needs no caller lookup and must be refused before anything is allocated.
    clean_title = _clean_new_session_title(title, cap=200, not_done="session not created")
    caller_key = caller_slot_key(state, caller_session_key)
    if not caller_key:
        raise SessionControlError(
            "caller session could not be identified", code="caller_unidentified"
        )
    # The caller is resolved BEFORE the config gate so a member DM session —
    # for which dispatching work into workers is the operating model, not an
    # opt-in — passes without `agent.session_control`. That member bypass is
    # itself gated by the operator ceiling `agent.member_dispatch` (default
    # true = today's behaviour); with it off the member falls back under the
    # switch like any other caller. Every other caller still needs the switch.
    # The member's automatic grant is bounded by ownership in `authorize_target`,
    # not here: creation makes the caller the owner by construction.
    if not session_control_enabled() and not _member_bypass(state, caller_key):
        raise SessionControlError(
            "session control is disabled in config (agent.session_control)",
            code="session_control_disabled",
        )
    if caller_key.startswith(UNATTENDED_SLOT_PREFIXES) and not _cron_caller(caller_key):
        raise SessionControlError(
            "unattended sessions (scheduled runs) cannot create sessions",
            code="unattended_caller",
        )
    caller_slot = state.get_slot(caller_key)
    if caller_slot is None:
        raise SessionControlError("caller session is not open", code="caller_not_open", status=404)
    _refuse_ineligible_creator(state, caller_slot)
    caller_memory_identity = (
        slot_history_key(caller_slot),
        caller_slot.agent,
        caller_slot.memory_store,
    )

    try:
        caller_execution = await asyncio.to_thread(
            read_session_execution, caller_memory_identity[0]
        )
    except (ValueError, OSError):
        raise SessionControlError(
            "caller execution context is unavailable", code="memory_unavailable"
        ) from None

    # This process's own word on the caller, snapshotted in the SAME breath as the
    # record above so the two sources the own-store admission compares below are
    # taken at one instant. Reading it down there instead would make a privacy
    # change landing mid-resolution surface as a delegation refusal, when the
    # re-gate further down exists precisely to name that case. A plain dict read
    # under a lock, so it adds no suspension point here.
    caller_vouched = read_vouched_session_execution(caller_memory_identity[0])

    if caller_execution is not None and caller_execution.memory_mode != "persistent":
        raise SessionControlError(
            "incognito and temporary sessions cannot create sessions", code="ephemeral_caller"
        )

    # The child is created in the CALLER'S workspace, not the default one.
    # Workspace is the memory boundary and `authorize_target` refuses a
    # cross-workspace target, so a child left in "default" would be a boundary
    # crossing its own creator could not then read or stop.
    workspace = getattr(caller_slot, "workspace", "default") or "default"

    # Match dashboard-native creation: filing a dispatched session in a
    # project-linked folder gives the child that folder's nearest inherited
    # project directory, while the workspace and memory boundary stay the
    # caller's. Capture the raw inherited value so the late folder re-check can
    # refuse a concurrent reparent or project edit instead of creating against
    # stale folder intent.
    folder_project_raw: str | None = None
    folder_project = ""
    if folder_id:
        folder_snapshot = await state.read_folders(
            lambda folders: [dict(folder) for folder in _safe_folder_tree(folders)]
        )
        if not any(str(folder.get("id") or "") == folder_id for folder in folder_snapshot):
            raise SessionControlError("folder not found", code="folder_not_found")
        folder_project_raw, folder_project_error = _folder_declared_project(
            folder_snapshot, folder_id
        )
        if folder_project_error:
            raise SessionControlError(
                f"invalid folder project: {folder_project_error}",
                code="folder_project_invalid",
            )
        folder_project, folder_project_error = await asyncio.to_thread(
            _resolve_folder_project_dir, folder_snapshot, folder_id
        )
        if folder_project_error:
            raise SessionControlError(
                f"invalid folder project: {folder_project_error}",
                code="folder_project_invalid",
            )
    # An unnamed agent inherits the CALLER'S, not the global default: the caller is
    # already running in this workspace, so its agent is the one bound here, and
    # falling to the global default would put the child on another workspace's
    # memory store the moment the default is bound elsewhere. It also matches what
    # creating a session to hand work to means -- the same kind of session.
    # Sanitized for the reason `title` is scrubbed at the top: this value arrives
    # from the calling model, is persisted verbatim to the metadata line, and is
    # pushed to every dashboard client. The schema caps its LENGTH; sanitizing is
    # what keeps a credential-shaped string out of storage and out of the sidebar.
    # An inherited caller agent is already internal, but running both through the
    # same call keeps the guard on the field rather than on one of its sources.
    # A crewmate's own chat runs on the conductor, and what it hands off runs on
    # its WORKER, whatever agent the model names: the gateway knows the caller
    # from its attested execution record, so the choice is not the prompt's to
    # make. The child is the worker template on the crewmate's own workspace and
    # store (the template arm below), stated as a template so a crew member that
    # happens to carry the name cannot shadow it. Naming another crewmate is the
    # one exception (see `_hands_off_to_own_worker`); it keeps its own
    # resolution, which refuses a peer bound elsewhere.
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    own_worker = agent_kind in ("", "template") and await asyncio.to_thread(
        _hands_off_to_own_worker, caller_slot, caller_execution, agent.strip(), agent_kind
    )
    if own_worker:
        agent, agent_kind = MATE_WORKER_AGENT, "template"
    agent_name = sanitize_outbound(agent.strip() or (getattr(caller_slot, "agent", "") or ""))
    # The selection namespace, validated HERE and not left to the HTTP route or
    # the MCP schema: `create_session` is also reached in-process, and a kind
    # outside the closed set would otherwise travel into `resolve_agent_bindings`
    # as a silent name-first resolution. A kind without an explicit `agent`
    # qualifies nothing -- the inherited caller agent is not a selection -- so it
    # is refused rather than ignored.
    if agent_kind not in ("", "template"):
        raise SessionControlError('agent_kind must be "template"', code="invalid_agent_kind")
    if agent_kind and not agent.strip():
        raise SessionControlError(
            "agent_kind qualifies an explicit agent; name the agent too",
            code="agent_kind_requires_agent",
        )

    log = state.conversation_log
    if log is None:
        # No durable store means the session cannot be persisted at birth, so it
        # would vanish on the next restart. Refusing is the honest answer;
        # returning a key would hand back a session that is dead on arrival.
        raise SessionControlError(
            "session history is unavailable, so the session cannot be persisted",
            code="history_unavailable",
        )

    # Resolved BEFORE the slot exists, because `get_or_create_slot` publishes into
    # the slot table and `await` is a suspension point: a slot that is visible
    # while its agent and project are still unset can be addressed in that window,
    # and `/api/chat` would then resolve bindings from a blank agent -- running the
    # turn against the DEFAULT workspace's memory store rather than this one.
    # `default_project_dir` needs only the workspace name, so nothing forces it to
    # run after construction.
    #
    # Offloaded: it resolves a realpath, stats the directory and screens it against
    # the sensitive-path list, so it is filesystem work the loop should not wait on.
    # The rule's own tiebreaker applies -- a leaked worker thread is survivable, a
    # frozen loop is not.
    project_dir = folder_project or await asyncio.to_thread(default_project_dir, workspace)

    # ONE invariant covers every branch of agent resolution: the agent that will
    # actually ANSWER must be bound to the caller's workspace. Authorization reads
    # `slot.workspace` while execution follows the agent's own binding, so any
    # branch where those disagree carries another workspace's memory store into
    # the child. Enumerating the branches instead of stating the invariant is how
    # the empty-agent case was missed:
    #
    #   agent given, binding matches   -> allowed, dispatches that agent
    #   agent given, binding differs   -> refused (agent_workspace_mismatch)
    #   agent given, name unresolvable -> refused (agent_unresolved), because the
    #                                     default would answer under the requested
    #                                     name
    #   agent omitted                  -> `resolve_agent_bindings` falls to
    #                                     config.default_agent, so the SAME check
    #                                     applies to whatever would answer; an
    #                                     omitted agent is not an unchecked one
    #   config unreadable              -> refused (agent_unverifiable), because
    #                                     "cannot verify" must not read as "fine"
    #   agent given, TEMPLATE, caller  -> allowed in the caller's workspace: a
    #   is a crew member in its own       template has no workspace of its own (the
    #   workspace                         resolver files it under the default
    #                                     one), and the template arm below binds the
    #                                     child to the caller's own store, so the
    #                                     agent that answers runs on the caller's
    #                                     workspace and memory. A member in a
    #                                     `member-<slug>` workspace could otherwise
    #                                     never open a worker template
    #
    # A crew MEMBER selection is never inherited this way: it carries its own
    # workspace and store, so naming a peer bound elsewhere stays refused.
    #
    # Resolved with the child's own `project_dir`: a materialized kiro agent is
    # declared per project directory rather than registered in `config.agents`, so
    # resolving without it reports an app's agent as unresolvable and would refuse
    # a name that does resolve for the session being created.
    try:
        # Offloaded: a cache miss reads and validates the config file, so leaving it
        # on the loop stalls every other gateway task, not just this request. It is
        # awaited HERE, still ahead of the caller re-resolve below, so the decisions
        # that authorize the allocation are all made after the last suspension.
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        # Resolved in the STATED namespace when the caller gave one, so a
        # template pick takes the template even when a crew member carries the
        # same name -- the chat create route's contract, kept identical here.
        bindings = await asyncio.to_thread(
            resolve_agent_bindings,
            cfg,
            agent_name,
            project_dir,
            validate_memory_files=False,
            selection_kind=agent_kind,
        )
    except Exception:
        raise SessionControlError(
            "cannot verify the effective agent's workspace binding",
            code="agent_unverifiable",
        ) from None
    agent_workspace = _workspace_name_for_dir(cfg, bindings.workspace_dir)
    if (
        agent_workspace != workspace
        and agent.strip()
        and bindings.selection_kind == "template"
        and _caller_member_workspace(cfg, caller_slot, caller_execution) == workspace
    ):
        agent_workspace = workspace
    if agent_workspace != workspace:
        who = repr(agent_name) if agent_name else "the default agent"
        raise SessionControlError(
            f"{who} is bound to workspace {agent_workspace!r}, not the caller's " f"{workspace!r}",
            code="agent_workspace_mismatch",
        )
    if not bindings.requested_resolved:
        # The workspace check above passed for whatever ANSWERS -- the default
        # agent -- so no memory boundary is crossed. What would be wrong is the
        # record: `slot.agent` stores this name, and `ResolvedBindings` states the
        # contract for exactly this caller class, that a caller storing the
        # requested name must not advertise it when the request was not honored.
        # A session that names one agent while another answers misleads every later
        # reader of the sidebar and of `list_sessions`.
        #
        # Refused rather than silently rewritten to the effective agent, because
        # the caller asked for a specific one and nothing is lost by refusing: no
        # session exists yet, and a corrected name is one retry away. (An existing
        # slot is the opposite case -- there the stored name is the user's own
        # intent and is kept verbatim, since a momentarily stale resolution must
        # not permanently rebind it.) A stated namespace never falls back to
        # whoever answers by default either, and its refusal names the namespace
        # so the caller can tell "no such template" from "no such agent".
        if agent_kind:
            raise SessionControlError(
                f"{agent_name!r} does not resolve to a configured {agent_kind}",
                code="agent_unresolved",
            )
        raise SessionControlError(
            f"{agent_name!r} does not resolve to a configured agent",
            code="agent_unresolved",
        )
    # The namespace the selection RESOLVED in, taken from the resolver's verdict
    # before `bindings` is rewritten below to carry the child's execution route
    # (whose `selection_kind` is the child's identity, which for a template child
    # of an id-less member is still "member"). This is what the slot records when
    # the caller stated a kind -- the chat create route's `chosen.selection_kind`.
    committed_kind = bindings.selection_kind if agent_kind else ""

    # The model and effort the child starts on, checked BEFORE anything is
    # allocated, where refusing loses nothing (the model guard is
    # `_model_pin_refusal`, the picker's own).
    #
    # The level is validated BEFORE the inheritance below fills the gaps: a malformed
    # explicit level is the caller's error and must be reported as such, not
    # papered over by the crewmate's default.
    if reasoning_effort is not None and reasoning_effort != "":
        if not is_valid_effort(reasoning_effort):
            raise SessionControlError(
                f"reasoning_effort must be one of: {', '.join(EFFORT_LEVELS)}",
                code="effort_rejected",
            )
    # What the caller left unsaid comes from the crewmate doing the dispatch,
    # read off the config snapshot already loaded above. `model`'s inherited
    # value goes through the SAME guard below as an explicit one -- a profile
    # field is operator input too, and `_model_rejected_reason` is what keeps a
    # display-only key out of `slot.model` whichever door it came through.
    #
    # Only a child that is NOT itself a crewmate inherits. A child selected as
    # a member (an explicit `agent` naming another crewmate) has a profile of
    # its own, and its turns already resolve that profile's model and effort
    # through the crew-pin tier; a pin copied from the dispatcher would outrank
    # it as a per-session pick and run that crewmate on someone else's model.
    inherited_fields: list[str] = []
    model_from_worker_pin = False
    effort_is_crewmates_own = False
    child_is_crewmate = bool(agent.strip()) and bindings.selection_kind == "member"
    if (not model.strip() or not reasoning_effort) and not child_is_crewmate:
        crewmate = _dispatching_crewmate(cfg, caller_slot, caller_execution)
        if crewmate is not None:
            worker_model, worker_effort = worker_session_defaults(crewmate)
            explicit_worker_effort = coerce_effort(crewmate.worker_reasoning_effort)
            if model.strip():
                # An explicit model is a model the crewmate's own level was not
                # chosen for, the same as a `worker_model` pin: only the worker
                # effort may ride it, never the crewmate's own.
                worker_effort = explicit_worker_effort
            elif worker_model:
                model = worker_model
                inherited_fields.append("model")
                # The profile's own worker pin was validated for the worker's
                # harness; the crewmate's own `model` was validated for the
                # crewmate's. The cross-harness drop below applies to the latter.
                model_from_worker_pin = bool(crewmate.worker_model.strip())
            if not reasoning_effort and worker_effort:
                reasoning_effort = worker_effort
                inherited_fields.append("reasoning_effort")
                # Whether that level is the crewmate's OWN (carried only because
                # the worker was to run the crewmate's model) rather than the
                # profile's worker effort: if the model is dropped below, so is it.
                effort_is_crewmates_own = worker_effort != explicit_worker_effort
    model_name = _normalize_model(model.strip())
    # An inherited pin the guard refuses is DROPPED, never fatal: a profile pin
    # goes stale (delisted, display-only, outside the account's entitlement, or an id only the
    # crewmate's own harness serves) without anyone looking at it, and a hard
    # refusal would stop every worker the crewmate opens until someone edits
    # the profile or names a model on every call -- the burden the inheritance
    # removes. The child then starts on the default, and the SEL record says
    # so. An explicit pin is the caller's own and stays a refusal.
    inherited_model_dropped = ""
    # The harness the child runs on: a crewmate's own worker runs on its
    # crewmate's backend (``members.select_provider_backend``), every other child
    # on the configured default. A worker on a backend that cannot carry its kit
    # is refused here, before anything is allocated.
    from kiro_crew.mate_worker import crewmate_worker_backend, worker_kit_backend_refusal

    child_backend = crewmate_worker_backend(cfg.agent) if own_worker else cfg.agent.acp_backend
    if own_worker:
        backend_reason = worker_kit_backend_refusal(child_backend)
        if backend_reason is not None:
            raise SessionControlError(backend_reason, code="worker_kit_harness_unsupported")
    model_reason = _model_pin_refusal(model_name, child_backend) if model_name else None
    if model_reason is None and "model" in inherited_fields:
        model_reason = _inherited_pin_foreign_to_child(
            model_name, cfg, child_backend, validated_for_worker=model_from_worker_pin
        )
    if model_reason is not None:
        if "model" not in inherited_fields:
            raise SessionControlError(model_reason, code="model_rejected")
        inherited_fields.remove("model")
        inherited_model_dropped = model_name
        model_name = ""
        if effort_is_crewmates_own and "reasoning_effort" in inherited_fields:
            # The crewmate's own level rode along only because the worker was to
            # run the crewmate's model; with that model gone it goes too. The
            # profile's explicit worker effort is kept.
            inherited_fields.remove("reasoning_effort")
            reasoning_effort = ""
    effort_name = reasoning_effort or ""

    # Capture the child route once. Inherited member/store identity survives
    # renamed aliases or changed config; an explicit member selection is resolved
    # from the config snapshot admitted above.
    try:
        if agent.strip() and bindings.selection_kind == "member":
            child_execution = resolve_member_execution(
                cfg,
                bindings.resolved_alias or agent_name,
                memory_mode=getattr(caller_slot, "memory_mode", "persistent"),
                validate_memory_files=False,
            )
            if caller_execution is not None:
                child_execution = child_execution.with_mode(caller_execution.memory_mode)
        elif caller_execution is not None:
            child_execution = caller_execution.with_mode(
                getattr(caller_slot, "memory_mode", "persistent")
            )
            if agent.strip():
                # An explicit template selects the child's PERSONA, never its memory:
                # the store, and the member identity that store is bound to, stay
                # the caller's, while the selection namespace becomes the template's
                # -- the same split the subagent admission gate makes for a
                # `spawn_run(agent=...)` delegate. ContextBuilder reads that
                # namespace: a member's delegate that was picked to do the work
                # itself keeps the member's identity and rules but is not handed
                # the member's operating protocol, which would send it to delegate
                # again. A member with no persisted id is named by its selection
                # alone, so the split would leave its child attributed to no member
                # and drop its [PERMANENT RULES] with its persona; that child keeps
                # the selection and takes only the template, whole desk included,
                # until the record can say "this member, under that template".
                if child_execution.member_id is None and child_execution.selection_kind == "member":
                    child_execution = replace(child_execution, template_id=bindings.kiro_agent)
                else:
                    child_execution = child_execution.with_template(bindings.kiro_agent, agent_name)
        elif bindings.selection_kind == "member":
            child_execution = resolve_member_execution(
                cfg,
                bindings.resolved_alias or agent_name,
                memory_mode=getattr(caller_slot, "memory_mode", "persistent"),
                validate_memory_files=False,
            )
        else:
            child_execution = ExecutionContext(
                None,
                MemoryStoreRef(bindings.memory_store_name or "default"),
                "template",
                bindings.kiro_agent,
                getattr(caller_slot, "memory_mode", "persistent"),
                selection_name=agent_name,
            )
        bindings = replace(
            bindings,
            execution_context=child_execution,
            memory_store_name=child_execution.store.legacy_name,
            kiro_agent=child_execution.template_id,
            selection_kind=child_execution.selection_kind,
        )
    except (ValueError, OSError):
        raise SessionControlError(
            "selected execution context is unavailable", code="memory_unavailable"
        ) from None

    # ONE authorization for the child's PRIVATE binding, at the single point where
    # every branch above has already produced its final route. A per-branch check
    # is not equivalent: the selected-member branch, the inherited-agent branch
    # and the caller-agent fallback all reach a member store, so a check on any
    # one of them leaves the others open.
    #
    # A private member store is reachable on exactly two authorities:
    #
    # * the store is the caller's OWN, read from its protected execution record --
    #   the same-store worker that is a private caller's own model. The record is
    #   the only admissible input: `slot.agent` and `slot.memory_store` are
    #   metadata a later write can change, and the SELECTION itself is a
    #   caller-supplied string, so deriving authority from either lets the request
    #   authorize itself by naming a member's agent.
    # * the caller is the owner's own dashboard session, which is what "not
    #   ownership-fenced" means. That is a shipped capability: an owner reopening
    #   member conversations and dispatching member workers. It is preserved here.
    #
    # Refused, therefore, is every population `_caller_is_ownership_fenced`
    # already treats as untrusted for the sessions it may reach: a cron slot, a
    # member's DM slot naming a PEER member's agent, and anything either of them
    # created -- the fenced caller's unfenced deputy the fence exists to catch.
    # An app-token caller never arrives (`_require_internal` refuses it) and an
    # app-scoped one cannot create at all, so the fence is the whole population.
    #
    # Fail-closed in both directions. An unbound caller has no own-store
    # admission, so it needs the unfenced one. A fence verdict is only ever read
    # as a REFUSAL here, so the mutable-record window the carried verdict exists
    # to close can widen nothing: a record that stops saying "member" between the
    # gate and this line turns a refusal into an admission the owner already has,
    # never the reverse.
    #
    # The own-store admission needs TWO sources to agree, and neither alone is
    # enough. `caller_execution` can come from the caller's own transcript record
    # when this process holds no carrier, and that record is written by the very
    # session being judged -- so on its own it answers "whose store is this?" with
    # the subject's own claim. `read_vouched_session_execution` answers with what
    # this process committed, which no session can write, but it can fall behind:
    # several modules publish an execution record without going through
    # `bind_session_execution`, so a legitimate reassignment can leave the vouched
    # entry stale.
    #
    # Requiring agreement fails closed against both. A forged record cannot match
    # a vouched entry it did not write. A stale vouched entry cannot match a record
    # that has moved on. Only an identity this process itself committed, and that
    # the record still carries, admits -- and a caller refused here is not
    # refused outright, it simply falls through to the fence below, which an owner
    # passes.
    if child_execution.member_id is not None:

        def _own_store_agreed() -> bool:
            return (
                caller_execution is not None
                and caller_execution.member_id is not None
                and caller_execution.store == child_execution.store
                and caller_vouched is not None
                and caller_vouched.store == caller_execution.store
            )

        own_store_agreed = _own_store_agreed()
        if not own_store_agreed and caller_vouched is None and caller_execution is not None:
            # The rehydrate self-heal. Agreement fails on the ONE shape
            # a restart or a cap eviction produces -- the durable record survives
            # but this process holds no vouched word -- so re-establish that word
            # at THIS gate-verified admission rather than stranding own-store
            # dispatch until the owner re-selects the agent. The trust source is
            # the VERIFIED session key: `revouch_at_verified_admission` re-vouches
            # a member DM key whose slug the durable record AGREES with, or any
            # other key whose disk vouch copy in `vouched-executions/` (written
            # when the gateway vouched it -- a member-born child) agrees with the
            # record. A caller that forged its record to name a peer's store gets
            # nothing: neither source names the peer. `caller_key` is the key the HTTP
            # gate authenticated; `caller_memory_identity[0]` is the same session's
            # history key, which carries the `member-<slug>` form for a member DM.
            # Off the loop: it resolves the member's store from config (filesystem
            # work), and `cfg` was already loaded off-loop above, so no blocking
            # read runs on the gateway loop.
            if await asyncio.to_thread(
                revouch_at_verified_admission, caller_memory_identity[0], caller_execution, cfg
            ):
                caller_vouched = read_vouched_session_execution(caller_memory_identity[0])
                own_store_agreed = _own_store_agreed()
        if own_store_agreed:
            # Recency follows USE, not birth, so a later overflow at the cap drops
            # an idle key rather than the member session still dispatching through
            # it. Grants nothing: an absent key is a no-op and the value is not
            # touched, so the only thing it changes is which entry is dropped.
            refresh_vouched_session_execution(caller_memory_identity[0])
        else:
            # The fence is read LIVE, and it reads the caller's channel link (an
            # owner-DM caller is fenced), so a caller whose identity moved during
            # the awaits above -- a link landing on it, a store or agent change --
            # must be named as such HERE, before any verdict is derived from its
            # new identity. The re-gate before allocation exists precisely to name
            # that case, and a link landing mid-resolution must surface as the
            # identity change it is, not as a delegation refusal.
            _refuse_moved_caller_identity(state, caller_key, caller_slot, caller_memory_identity)
            # Off-loop: the walk reads live slot state up the creation chain. Only
            # reached when the carried verdict is absent, and only for a private
            # selection, so an ordinary create pays nothing. Confined to this gate
            # on purpose: the owner-rooted allowance never widens the per-verb
            # ownership boundary in `authorize_target`.
            fenced = (
                caller_fenced
                if caller_fenced is not None
                else await asyncio.to_thread(_delegation_lineage_fenced, state, caller_key)
            )
            if fenced:
                # Server-side ONLY, and it says the one thing the caller's refusal
                # must not: WHICH source failed. An operator reading this can tell
                # authority this process never held or has since dropped (a restart,
                # or cap churn -- the session re-binds and recovers) from a record
                # that disagrees with what this process committed, which is the
                # forgery shape the agreement exists to refuse. Names neither the
                # store nor the member, so the log is not a second disclosure
                # channel for what the refusal withholds.
                #
                # Lineage is named FIRST and on its own, because it is the operative
                # condition for the caller class the vouched-identity causes below
                # cannot describe: a Global-store session an agent created is never
                # vouched (`bind_session_execution` vouches only a truthy
                # `member_id`), so without this every such refusal would log "no
                # vouched identity" and point an operator at restart/cap churn that
                # no re-bind will clear -- the fence here is `_created_by`, which is
                # immutable. The member and cron caller classes are fenced too but
                # are not reached as a private-member CREATE caller the way an
                # agent-created Global conductor is, so this names the condition
                # that actually reaches this line.
                #
                # A caller whose own record is on the SHARED store is named next,
                # for the same reason: it too is never vouched, so without this it
                # logs "no vouched identity" and points the operator at a re-bind
                # that cannot help -- a shared-store caller has no private store
                # of its own for the selected member's to match, so no vouch, and
                # no re-selection, ever admits it. This is the shape a Global-store
                # crew member's DM thread meets when it names a crew member that
                # owns private memory (the stock conductor dispatching a worker an
                # older agent sync enrolled with its own store); the way out is a
                # template dispatch (`agent_kind="template"`) or the owner's own tab,
                # and the log should say so instead of describing a lost vouch. The
                # store is tested beside the missing id because the id alone does
                # not place a record on the shared store: a V1 crew member bound to
                # its own named store also carries no `member_id`, and this line
                # would be false for it.
                caller_slot_now = state.get_slot(caller_key)
                if (
                    caller_fenced is None
                    and caller_slot_now is not None
                    and caller_slot_now._created_by
                    and not _member_caller(state, caller_key)
                    and not _cron_caller(caller_key)
                    and not _channel_link_of(caller_slot_now)
                ):
                    cause = "the caller is fenced by its creation lineage (_created_by)"
                elif (
                    caller_execution is not None
                    and caller_execution.member_id is None
                    and caller_execution.store.store_id == "default"
                ):
                    cause = (
                        "the caller runs on the shared store, so it holds no private "
                        "store of its own for the selected member's to match (dispatch "
                        "the template instead, or create from the owner's own tab)"
                    )
                elif caller_vouched is None:
                    cause = "this process holds no vouched identity for the caller"
                elif (
                    caller_execution is not None and caller_vouched.store != caller_execution.store
                ):
                    cause = "the caller's record disagrees with this process's vouched identity"
                else:
                    cause = "the caller's record does not name the selected store"
                logger.warning("memory delegation refused for %s: %s", caller_key, cause)
                # The delegation refusal, in the words and under the code this
                # surface already uses for it, so a caller sees one refusal for the
                # whole class. Deliberately says nothing about the store, the
                # member, or why this caller is fenced: a refusal must not confirm
                # which member owns the agent the caller guessed at.
                raise SessionControlError(
                    "cannot verify delegation within the caller's memory assignment",
                    code="memory_delegation_denied",
                    status=403,
                )

    # SlotOrigin.USER, not SYSTEM: the visibility semantics must match an
    # ordinary session, because the point of creating it here is that the user
    # can see and take over the work. SYSTEM-origin slots fall outside the
    # `slots:user` WS scope, which would hide it from the sidebar.
    #
    # A CRON caller is the exception, and it is the one case where USER would be
    # wrong rather than merely coarse. `inject_cron_result_to_dashboard` tags a
    # cron's own slot CRON precisely so its output stays out of `slots:user` ("a
    # USER label would expose it to any app holding `slots:user`"), and the trust
    # model states the same rule from the other side: inferring USER for a
    # background caller "put cron output inside `slots:user`". Minting a
    # USER-labelled child would hand a cron the exposure its own slot is denied,
    # by the simple route of creating a session and writing there instead.
    #
    # The tag therefore follows the caller's AUTHORITY, not its key prefix, and
    # for the same reason the ownership fence does: a created child INHERITS its
    # creator's agent, so a cron's child can itself call this verb, and a
    # prefix-only test mints that grandchild USER (its caller key is a plain
    # `chat-`) -- the two-hop version of the very route this comment says must be
    # denied. Reading the caller slot's own ``_origin`` closes it transitively: the
    # child carries CRON, so ITS children do too, at any depth.
    #
    # Nothing is lost by the narrower tag: only APP tokens are filtered by origin
    # (`_serialize_for_client` returns the unfiltered payload to a dashboard
    # user), so a CRON-origin descendant stays in the sidebar exactly as today's
    # cron tabs do -- which is the property the paragraph above is protecting.
    #
    # Computed from the RE-RESOLVED caller below rather than here, because it is a
    # decision input to the allocation and everything above this point was read
    # before the coroutine suspended.

    if folder_id:
        # Confirmed under the folder-store lock -- the only place existence and
        # inherited project intent cannot go stale against a concurrent delete,
        # reparent, or project edit. READ-ONLY on purpose: the Model-B un-hide is
        # a durable mutation and runs only after filing lands.
        def _current_folder_project(
            folders: list[dict[str, Any]],
        ) -> tuple[bool, str | None, str | None]:
            tree = _safe_folder_tree(folders)
            exists = any(str(folder.get("id") or "") == folder_id for folder in tree)
            raw_project, error = _folder_declared_project(tree, folder_id)
            return exists, raw_project, error

        folder_exists, current_folder_project, current_folder_error = await state.read_folders(
            _current_folder_project
        )
        if not folder_exists:
            raise SessionControlError("folder not found", code="folder_not_found")
        if current_folder_error or current_folder_project != folder_project_raw:
            raise SessionControlError(
                "folder project changed while the session was being created",
                code="folder_target_changed",
                status=409,
            )

    # Re-resolved and re-gated HERE, adjacent to the allocation, because every
    # decision above was made before this coroutine suspended -- for the
    # project directory, agent bindings, memory delegation and folder confirmation -- and the
    # inputs to those decisions are live state that can flip inside any of those
    # windows.
    #
    # Re-reading the slot TABLE is the part that matters most: closing the caller's
    # tab removes its slot, and a Python reference to the removed object stays
    # perfectly usable, so re-running the gate on the object resolved earlier would
    # authorize against a caller whose authority has already ended. Identity is
    # compared rather than mere presence, because the key can be re-minted onto a
    # different session inside the same window. `_has_channel_mirror` reads the
    # session store, so an outbound mirror link registered while this waited would
    # otherwise leave a now-channel-backed caller publishing a persistent session
    # outside its containment; `live_slot_count` reads the slot table, so two
    # concurrent creations could each pass the ceiling and then both land over it.
    #
    # Nothing suspends between this point and the fully-configured slot below, so
    # the gate and the act it authorizes stay adjacent -- the same discipline
    # `stop_target` keeps by prewarming its SEL logger ABOVE its gate rather than
    # between gate and act.
    live_caller = state.get_slot(caller_key)
    if live_caller is None or live_caller is not caller_slot:
        raise SessionControlError("caller session is not open", code="caller_not_open", status=404)
    # A slot that survived but MOVED workspaces has invalidated both decisions that
    # read it: the memory boundary the child inherits, and the agent-binding check
    # above, whose whole question was whether the answering agent is bound to THIS
    # workspace. Re-running that check here is not an option -- it needs
    # `KiroCrewConfig.load()`, which is filesystem work that must not run on the
    # event loop -- so a moved caller is refused instead of re-authorized.
    if (getattr(live_caller, "workspace", "default") or "default") != workspace:
        raise SessionControlError(
            "caller session changed workspace while the session was being created",
            code="caller_workspace_changed",
        )
    _refuse_moved_caller_identity(state, caller_key, caller_slot, caller_memory_identity)
    _refuse_ineligible_creator(state, live_caller)
    # The child's origin tag, read off the caller that is live NOW -- see the
    # reasoning above the folder gate. `_cron_caller` covers a cron's own tab;
    # `_origin` carries the tag onward to every descendant of one.
    child_origin = (
        SlotOrigin.CRON
        if _cron_caller(caller_key) or getattr(live_caller, "_origin", "") == SlotOrigin.CRON
        else SlotOrigin.USER
    )
    # The RATE guard, ahead of the capacity ceilings below. Those bound how many
    # sessions can exist; this bounds how fast one caller may open them, which is
    # the property an auto-approved verb loses -- a waived prompt leaves a loop
    # nothing to push back on. Deliberately the control that needs no durable
    # state: a lifetime quota means nothing across a restart unless every
    # rehydrate path carries its attribution, while a five-minute window buys a
    # restart one window rather than a clean slate.
    #
    # A dry run asks the same question without spending the token: a preview
    # must not use up the create it previews.
    admitted = (
        has_create_budget(SESSION_CREATE, caller_key)
        if dry_run
        else allow_create(SESSION_CREATE, caller_key)
    )
    if not admitted:
        raise SessionControlError(
            "too many sessions created recently; retry shortly",
            code="create_rate_limited",
            status=429,
        )
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        raise SessionControlError(
            f"slot cap reached ({MAX_LIVE_SLOTS})",
            code="slot_cap_reached",
            status=429,
        )
    # Then the per-creator sub-ceiling. The global cap above bounds the TOTAL but
    # not the distribution, so without this one caller can hold all 500 and the
    # person's own next chat tab gets the 429 -- the resource is bounded, but not
    # from anyone else's point of view. This is the bound that makes the verb safe
    # to auto-approve: the worst case of an automated creator looping on it is its
    # own 50 slots, not everyone's 500.
    if state.creator_slot_count(caller_key) >= MAX_SLOTS_PER_CREATOR:
        raise SessionControlError(
            f"per-caller slot cap reached ({MAX_SLOTS_PER_CREATOR})",
            code="creator_slot_cap_reached",
            status=429,
        )
    # The dry run ends HERE, after the last refusal and before the first write.
    # Any new refusal gate belongs above this line, or the preview would pass a
    # create the real call then refuses, and the MCP path walk would leave the
    # folders it made for that create empty.
    if dry_run:
        return {"dry_run": True}

    # The agent rides in the constructor rather than being assigned afterwards, for
    # the same reason: it decides which workspace actually EXECUTES the turn, so it
    # must never be observable as empty. Everything after this point is synchronous
    # until the slot is fully configured.
    #
    # The whole allocation-to-persist span runs under `suspend_slots_push`:
    # `get_or_create_slot` broadcasts on a leading edge, so without the suspend an
    # idle gateway serializes and sends the new slot BEFORE `folder_id` is
    # assigned -- every client (and any app on `slots:user`) would render the
    # session at the top level for a frame -- the observable unfiled state this
    # suspend removes. It also covers the persist and its failure retraction, so
    # a slot whose birth write fails is never broadcast at all. Same pattern the
    # move path uses ("file the slot before the coalesced broadcast").
    with state.suspend_slots_push():
        slot = state.get_or_create_slot(
            None, agent=agent_name, workspace=workspace, origin=child_origin
        )
        # Attribute the slot to the caller that asked for it, which is what makes the
        # per-creator ceiling above countable. Written here, inside the synchronous
        # window that follows the mint, so no suspension point separates the cap test
        # from this write -- otherwise two concurrent creates could both pass a ceiling
        # that one of them had already filled. Only this entry point sets it: a
        # person's own tab and a fork reach `get_or_create_slot` directly and stay
        # unattributed, so ordinary human use never consumes an automated caller's
        # share.
        slot._created_by = caller_key
        # Freeze the creator's ACP session id HERE, at mint, from the live caller
        # handle we just authorized -- not later at the child's first turn. The
        # creator slot can be closed and replaced between this mint and that turn,
        # and a replacement is a distinct handle with its own session id; reading
        # the id live at emit would then cite the replacement's crew log and corrupt
        # the child's immutable `session/opened` lineage with no recovery path.
        # `live_caller` is the same object the authorization gate above resolved,
        # so this is the id that was live when the child was made. Empty when the
        # caller's handle has no ACP session yet, which is recorded as absent.
        # Bounded HERE, at retention, by the one constant every store of a
        # backend-authored session id shares: an id past it is dropped, not
        # truncated, so an oversize backend id can neither grow the slot's
        # metadata nor make the child's ``session/opened`` entry too large to
        # land -- the sid is optional, its absence is a legal record.
        _creator_sid = crew_log_emit.session_id_of(getattr(live_caller, "_acp_client", None))
        slot._created_by_sid = _creator_sid if len(_creator_sid) <= MAX_ACP_SESSION_ID_LEN else ""
        # Witness that THIS process stamped the two fields above at mint. Neither
        # the flag nor the sid is persisted: the transcript is a file an agent's
        # file tools can edit, and the crew log is fenced from those tools exactly
        # so nothing in it can be forged as gateway-authored -- so the child's
        # first turn writes `session/opened.parent` only when this flag is set,
        # never from `created_by` read back off disk. A restart between mint and
        # the child's first turn therefore loses the link rather than trusting
        # metadata for it.
        slot._lineage_minted = True
        # The creator's interactive auto-approve grant follows the work it is
        # handing off. Without this a trusted operator dispatches a worker that
        # then blocks on an approval prompt nobody is watching -- the same failure
        # `parent_trusted` already closes for `spawn_run` subagents, which read the
        # parent's stored policy and start auto-approved. A dispatched session is
        # the same delegation with a sidebar tab, so it takes the same posture.
        #
        # Read off `live_caller`, not the entry-time `caller_slot`: the two are
        # identity-checked to be the same object above, but the grant itself is
        # mutable state the operator can revoke inside any of the suspensions this
        # coroutine took, so the value that transfers is the one held NOW, in the
        # synchronous window that follows the last gate. Revoking before the create
        # lands means the child is born untrusted, which is the direction that
        # fails safe.
        #
        # What transfers is SESSION POSTURE, and only that. Two fields carry, two
        # deliberately do not, and the exclusions are the load-bearing part:
        #
        # * `_trust` -- the human's "trust this session" click. It does not expire,
        #   the click is its own audit record, and copying it changes no property
        #   of the grant. The session-store half needs no write here: the child has
        #   no ACP session yet (`set_approval_policy` would silently no-op on a
        #   missing session), and `chat_runner` already assigns the persistable
        #   policy from `_trust` on every session create/resume, so the subagent
        #   spawn gate sees it from the child's first turn.
        # * `_trust_reads` -- the same posture, narrowed to read-only bash. It has
        #   to carry too, or the setting a CAUTIOUS operator picks is the one whose
        #   own workers still stall. Bounded by construction: what it admits has no
        #   side effects, which is what separates it from the command grants below.
        #
        # * `_trusted_patterns` -- NOT inherited. These are per-command grants
        #   ("`npm test` is fine"), not a posture, and the distinction decides it:
        #   a pattern is judged against the session the operator was LOOKING at,
        #   while a dispatched worker runs model-authored work they have not seen,
        #   so the same glob can admit a command the grant was never asked about.
        #   Inheriting them also buys nothing where it would be safe -- with
        #   `_trust` set the child already auto-approves via `_slot_is_trusted`, so
        #   the pattern list is dead weight; it changes the outcome ONLY when the
        #   operator withheld session trust and granted single commands instead,
        #   which is exactly the case that must keep asking. So the child starts
        #   with `_ChatSlot.__init__`'s empty set and earns its own grants.
        # * `_trust_scope` -- NOT inherited. It names a TTL-bounded, SEL-audited
        #   `SafetyOverride` grant that is re-checked on every approval; forking
        #   the key would hand a second session a credential whose revocation
        #   nothing here can observe. An unattended worker that needs one gets its
        #   own, armed by whatever owns its lifecycle.
        #
        # Not persisted at birth, matching every other slot: trust is in-memory by
        # construction, so a restart returns the child to interactive along with
        # its creator.
        #
        # A crewmate's own worker is the exception: it takes the crewmate's
        # Profile Permissions from the crew record (`crewmate_approval`), the
        # same mode it is given again at every restore, so the worker runs the
        # same way whether it was just opened or brought back after a restart.
        from kiro_crew.dashboard.crewmate_approval import seed_for_turn

        slot._trust = slot._trust_reads = False
        if not (own_worker and seed_for_turn(state, slot, cfg, child_execution)):
            slot._trust = bool(getattr(live_caller, "_trust", False))
            slot._trust_reads = bool(getattr(live_caller, "_trust_reads", False))
        inherited_trust = bool(slot._trust)
        inherited_trust_reads = bool(slot._trust_reads)
        # The agent's memory silo, from the bindings already resolved above. Held
        # on the slot so every later save can name it: `memory_store` is
        # slot-owned metadata, so a save that could not read it would drop the
        # key and silently return this session to the global store.
        slot.memory_store = bindings.memory_store_name
        slot.memory_mode = child_execution.memory_mode
        # The namespace the pick was committed in, recorded only when the caller
        # stated one -- the chat create route's rule ("a name-only pick writes no
        # agent_kind; only an explicit template pick says this was never a
        # member session"). The resolver's verdict rather than the request's word,
        # so the slot records what RESOLVED. Without it a rehydrated template child
        # of a shadowed name would read as a bare name and re-resolve member-first.
        if committed_kind:
            slot.agent_kind = committed_kind
        # cwd must follow the workspace too, or file search and project-scoped agents
        # resolve against a directory the slot does not claim -- the same
        # authorization-vs-execution split as the agent binding, one layer down.
        if not slot.project:
            slot.project = project_dir
        if folder_id:
            # Filed inside the same synchronous window that configures the slot, so
            # the session is never observable unfiled -- that atomicity is the point.
            # Existence was confirmed under the store lock above, and folder
            # mutations run on this loop, so the folder cannot have been deleted
            # between that check and this assignment. No `_folder_changed` flag: the
            # slot's first turn carries the armed first-turn breadcrumb injection
            # (`is_new` in chat_runner), so the [FOLDER] line reaches the model
            # without it.
            slot.folder_id = folder_id
        if model_name:
            # Pinned the way a person's pick in the model dropdown pins it: the
            # slot has no provider session yet, so there is nothing to switch --
            # the first turn starts on this model. The pick-generation bump marks
            # it as an explicit choice, so the fallback restore probe treats it
            # exactly as it treats a human pick rather than as a backfilled value.
            slot.model = model_name
            slot._model_pick_gen += 1
        if effort_name:
            # Pinned like the model: the setter bumps the effort pick
            # generation, and the first turn's cold start applies the level
            # through the same `reasoning_effort_override` the effort dropdown
            # relies on. One of the five canonical levels (checked above), so
            # the restore allowlist always admits it without a marker.
            slot.reasoning_effort = effort_name
        if clean_title:
            slot.title = clean_title
            slot._titled = True
        # Persist at birth. `save_slot_off_loop` cannot do this: the save it wraps
        # returns early on an empty message window -- a full save has nothing to
        # write -- so a freshly created session, which has no messages by
        # definition, would write nothing at all. The tool would then hand back a
        # session that does not survive a restart.
        #
        # Awaited, and a failure RETRACTS the slot rather than merely propagating: an
        # unpersisted slot stays in the table, usable in memory and addressable by its
        # creator, then vanishes on restart. Reporting the failure while leaving that
        # behind is the worse of the two outcomes, because the caller sees an error and
        # the session exists anyway. Same retraction the fork path uses on a failed
        # build.
        session_key = slot_history_key(slot)
        native_context = state.sessions.get_provider(session_key) is not None or bool(
            state.sessions.resumable_sid(session_key)
        )
        birth_persisted = False

        def _persist_birth(metadata: dict[str, Any]) -> None:
            nonlocal birth_persisted
            if read_session_execution(caller_memory_identity[0]) != caller_execution:
                raise SessionControlError(
                    "caller execution context changed during creation",
                    code="caller_memory_changed",
                )
            if (
                child_execution.member_id is not None
                and read_session_execution(session_key) is None
            ):
                if native_context or log.has_messages(session_key):
                    from kiro_crew.memory_stores import UnknownMemoryStore

                    raise UnknownMemoryStore("Existing session context requires a new conversation")
            # Establishing: this child's store came from the authorization above,
            # not from any record the child or its caller can write, so this is one
            # of the few publications entitled to vouch.
            bind_session_execution(session_key, child_execution, vouch=True)
            if own_worker:
                # Before its first turn allocates: the worker runs on the
                # crewmate's backend (``members.select_provider_backend``).
                from kiro_crew.mate_worker import note_worker_session

                note_worker_session(session_key, child_execution)
            log.update_metadata(session_key, metadata)
            birth_persisted = True

        try:
            await drained_to_thread(
                _persist_birth,
                {
                    "_type": "metadata",
                    # The slot's OWN durable identity, and its origin, both of which
                    # the normal save path writes -- but a slot created here may never
                    # reach that path: `_save_slot_to_history` runs a full save only
                    # when the window has messages, so for a session that is created
                    # and then sits idle THIS dict is the only record on disk.
                    # Omitting `origin` is silently destructive on the next restart:
                    # rehydrate falls back to the fail-closed empty sentinel, so a
                    # session opened as USER comes back unattributed and `slots:user`
                    # subscribers stop seeing it. Checked field-by-field against the
                    # save path; these are the only fields a slot carries at birth
                    # that it does not already write.
                    "tab_id": slot._tab_id,
                    "origin": slot._origin,
                    "created_at": metadata_now_iso(),
                    "workspace": slot.workspace,
                    "agent": slot.agent or "",
                    "project": slot.project or "",
                    "title": slot.title or "",
                    "memory_mode": getattr(slot, "memory_mode", "persistent"),
                    # The committed selection namespace, only when the caller
                    # stated one -- the metadata codec writes `agent_kind` the same
                    # way (truthy only), and for an idle newborn this dict is the
                    # only record, so without it a restart would rehydrate a
                    # template child of a shadowed name as a bare name.
                    **({"agent_kind": slot.agent_kind} if slot.agent_kind else {}),
                    # Only when filed, mirroring the normal save path, which omits
                    # `folder_id` from the metadata line when empty. Without this
                    # the filing would not survive a restart: for an idle newborn
                    # THIS dict is the only record of the placement on disk.
                    **({"folder_id": slot.folder_id} if slot.folder_id else {}),
                    # The pinned model, only when one was asked for -- the normal
                    # save path writes `model` too, but for an idle newborn this
                    # dict is the only record, and without it a restart would
                    # bring the session back on the default model.
                    **({"model": slot.model} if model_name else {}),
                    # The pinned effort, on the same grounds: the metadata codec
                    # restores `reasoning_effort` from this line, and a canonical
                    # level needs no validated-effort marker to come back.
                    **({"reasoning_effort": slot.reasoning_effort} if effort_name else {}),
                    # Creator attribution, only when this entry point set it. The
                    # member ownership boundary in `authorize_target` reads it, so
                    # losing it on restart would strand every worker a member
                    # dispatched — controllable in memory, orphaned after reboot.
                    **({"created_by": slot._created_by} if slot._created_by else {}),
                    # `created_by_sid` is deliberately NOT written: the transcript
                    # is agent-editable, so nothing read back from it may become
                    # crew-log lineage. The sid lives on the slot for this process
                    # only (see `_lineage_minted`).
                    # The agent's memory silo, recorded ONLY when it is not the
                    # default. This is what lets the consolidator write an agent's
                    # semantic, episodic and lesson rows into its own store
                    # instead of the global one, and this dict is the only record
                    # for a session that is created and then sits idle.
                    #
                    # Omitted for the default store on purpose: absence is the
                    # signal for "global", so a default user's metadata line stays
                    # byte-identical and a session written before crews had stores
                    # reads the same as one written now.
                    **(
                        {"memory_store": _named_store}
                        if (_named_store := named_store_or_empty(slot.memory_store))
                        else {}
                    ),
                },
            )
        except (Exception, asyncio.CancelledError):
            # Retract, but never at the cost of work already in flight. The slot is
            # addressable from the moment `get_or_create_slot` publishes it, which is
            # before this await, so a turn can have started on it while the write was
            # in the worker thread. Popping the slot then would leave that turn running
            # with nothing pointing at it -- unreachable, unstoppable, and invisible to
            # the stop verb. A phantom session that vanishes on the next restart is the
            # lesser harm, so liveness wins over tidiness and the slot stays.
            # Drain before deciding: cancellation cannot leave a worker writing
            # identity/history after this handler retracts its slot. A completed
            # birth survives cancellation just as a turn already in flight does.
            # A completed identity record remains available to a concurrent turn.
            if (
                not birth_persisted
                and not slot.running
                and not slot.messages
                and state._slots.get(slot.key) is slot
            ):
                state._slots.pop(slot.key, None)
            state.push_slots_update()
            raise
        if slot.folder_id:
            # Model-B un-hide, applied only NOW that the filing has actually
            # landed -- running it any earlier persists `hidden = False` for a
            # create a later gate can still refuse, durably reversing a choice
            # the user made for a call that failed. The move path holds the same
            # order (assign, confirm, then un-hide). If the folder was deleted
            # while the persist was in the worker thread, the delete's own sweep
            # already unfiled this slot (it is published), so the guard reads
            # the fresh value and skips; the metadata line can then briefly
            # carry a dangling folder_id, the same accepted residual a move
            # racing a delete leaves, and readers fall back to "(unfiled)".
            #
            # Best-effort: the create is already COMMITTED (slot published,
            # persisted at birth), so a folder-store write failure here must not
            # propagate -- the request would report failure for a session that
            # exists, and the caller's retry would create a duplicate. A folder
            # left hidden with a session inside is the recoverable lesser harm.
            try:
                await _unhide_folder(state, slot.folder_id)
            except Exception:
                logger.warning(
                    "create_session: filing committed for %s but un-hiding folder %s failed",
                    slot.key,
                    slot.folder_id,
                    exc_info=True,
                )
        state.push_slots_update()
    lineage_recorded = await _record_spawn(caller_key, slot)
    _audit(
        caller_session_key=caller_key,
        operation="create",
        slot_key=slot.key,
        outcome="allowed",
        detail={
            "agent": slot.agent or "",
            "folder_id": slot.folder_id or "",
            "model": model_name,
            "reasoning_effort": effort_name,
            # Which of the two the crewmate's profile supplied rather than the
            # call, so a worker found on an unexpected model is traceable to
            # the record that chose it. "" when the call named both or the
            # caller is not a crewmate.
            "inherited": ",".join(inherited_fields),
            # The profile model the picker guard refused and the create dropped
            # (the child started on the default instead). "" when none was.
            "inherited_model_dropped": redact(inherited_model_dropped),
            # What the child was BORN with, so an auto-approved tool call in it is
            # traceable to the creator's grant rather than appearing unexplained.
            # Always present: "false" is the record that the grant did not transfer.
            "inherited_trust": "true" if inherited_trust else "false",
            "inherited_trust_reads": "true" if inherited_trust_reads else "false",
            # Whether the creating edge reached the creator's crew log. "false" means
            # the child nests from this process's witness alone and would float at top
            # level after a restart. The audit log is this fact's durable home -- the
            # API reply has no reader for it (and _record_spawn also logs a warning).
            "lineage_recorded": "true" if lineage_recorded else "false",
        },
    )
    return {
        "ok": True,
        "target": slot.key,
        "title": slot.title or slot.key,
        **({"model": model_name} if model_name else {}),
        **({"reasoning_effort": effort_name} if effort_name else {}),
        **({"inherited": inherited_fields} if inherited_fields else {}),
        **({"inherited_model_dropped": True} if inherited_model_dropped else {}),
    }


def _model_pin_refusal(model_name: str, backend: str) -> str | None:
    """Why ``model_name`` may not be pinned on a new session, or ``None`` to allow it.

    The same guard the dashboard's model picker runs (``_model_rejected_reason``),
    against *backend*, the harness the child runs on, so an id the picker would
    refuse is refused here too. Length and charset are bounded
    HERE, not left to the MCP schema: ``SESSION_CREATE_SCHEMA`` runs only in
    ``mcp_dashboard``, and the HTTP route forwards the body's model string as-is,
    so without this bound an internal-secret caller could persist, broadcast and
    audit-log an arbitrarily long or arbitrarily shaped value. A credential-shaped
    id is refused without being echoed: the reason is stored and broadcast.
    """
    if len(model_name) > MAX_SHORT_STRING or not _MODEL_NAME_RE.fullmatch(model_name):
        return "model id is too long or contains characters outside the model-id charset"
    # Never persist or broadcast a value the security scrubber classifies.
    if redact(model_name) != model_name:
        return "model id looks like a credential and was refused"
    # circular import: chat_handlers imports session_control lazily, and this
    # module is imported by the dashboard package before chat_handlers loads.
    from kiro_crew.dashboard.chat_handlers import _model_rejected_reason

    return _model_rejected_reason(model_name, backend or "")


def _inherited_pin_foreign_to_child(
    model_name: str, cfg: KiroCrewConfig, child_backend: str, *, validated_for_worker: bool
) -> str | None:
    """Why a crewmate's model pin cannot follow its worker onto the child's harness, or ``None``.

    A crewmate's own thread runs on ``agent.member_acp_backend`` and its own
    ``model`` was validated against THAT harness's catalog
    (``_pin_entitlement_backend``). A child that inherits is never a crewmate
    itself: it runs on *child_backend* -- the crewmate's own backend for its
    worker, ``agent.acp_backend`` for any other child, as
    ``select_provider_backend`` picks them -- and an id one harness serves is
    refused on the wire by the other. Two reads, both keyed on
    the child's backend: the two harnesses disagree on model-id namespace (a
    claude or codex member pin cannot ride a kiro worker, and vice versa), or the
    child's namespace has advertised a list the pin is not on while another
    harness claims it (:func:`model_scope.pin_applies`). ``"auto"`` is every
    harness's own word and always passes.

    ``validated_for_worker`` names the profile's explicit ``worker_model``,
    which ``PUT /api/agents/{name}`` validates against the WORKER's harness
    (``mate_worker.crewmate_worker_backend``), so the namespace read does not apply to it -- the
    person pinned it for exactly this harness -- and only the advertised-list
    read remains. Only consulted for an INHERITED pin: an explicit model on the
    call is the caller's own choice and keeps today's guard alone.
    """
    if model_name == DEFAULT_MODEL:
        return None
    child_namespace = capabilities_for(child_backend).model_id_namespace
    member_namespace = capabilities_for(cfg.agent.member_acp_backend).model_id_namespace
    if not validated_for_worker and child_namespace != member_namespace:
        return (
            "the crewmate's model pin was chosen for its own harness "
            f"({cfg.agent.member_acp_backend or 'default'}), and the worker runs on "
            f"{child_backend or 'the default backend'}"
        )
    if not model_scope.pin_applies(model_name, child_namespace):
        return "the crewmate's model pin is not served by the harness the worker runs on"
    return None


def _dispatching_crewmate(
    cfg: KiroCrewConfig, caller_slot: Any, caller_execution: ExecutionContext | None
) -> KiroCrewAgentConfig | None:
    """The ``config.agents`` record of the crewmate ``caller_slot`` runs as, or ``None``.

    The caller's protected execution record is the first word: a ``member``
    selection names the crewmate by its persisted id (which survives a rename)
    or, for a member that never got one, by its selection name AND its own
    template. A caller whose record is a TEMPLATE is not a crewmate -- a worker
    a crewmate dispatched keeps that crewmate's store but runs as the template,
    and its own children must not re-inherit a profile it does not carry; the
    id-less member's worker keeps ``member`` as its selection, so there the
    template is what tells it apart. A caller with no record at
    all (a slot from before records were written) falls back to the slot's
    agent name, which is how :func:`resolve_crew_identity` reads the same slot
    for every other surface.

    Read-only: a lookup miss is ``None``, never a refusal, because inheriting a
    model is a convenience and a missing record simply leaves the default.
    """
    if caller_execution is not None:
        if caller_execution.selection_kind != "member":
            return None
        if caller_execution.member_id is not None:
            try:
                return member_config_for_id(cfg, caller_execution.member_id)[1]
            except Exception:  # noqa: BLE001 - a missing or ambiguous id is "no crewmate"
                return None
        record = cfg.agents.get(caller_execution.selection_name)
        # An id-less member's template worker keeps the member's selection
        # (the record cannot say "this member, under that template"), so the
        # template is the only thing that tells the worker from the crewmate:
        # a record running under another template is such a worker. Read through
        # the chat rule, so a record naming the crewmate's own file is the
        # conductor it chats on.
        from kiro_crew.mate_worker import member_chat_template

        if record is None or member_chat_template(
            caller_execution.template_id
        ) != member_template_id(record):
            return None
        return record
    agent_name = getattr(caller_slot, "agent", "") or ""
    return cfg.agents.get(agent_name) if agent_name else None


def _conducts_for_a_worker(caller_slot: Any, caller_execution: ExecutionContext | None) -> bool:
    """Whether the caller is a crewmate's own chat running on the mate conductor.

    That chat hands its work to the crewmate's worker (``kiro_crew.mate_worker``).
    Read from the caller's protected execution record through
    :func:`_dispatching_crewmate`, so a worker the crewmate already opened (a
    template selection) is not a conductor and its own children keep today's
    inheritance. Blocking config read; callers run it off the loop. Any failure
    answers False, which keeps the plain inheritance.
    """
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

    try:
        cfg = KiroCrewConfig.load()
        crewmate = _dispatching_crewmate(cfg, caller_slot, caller_execution)
        return crewmate is not None and member_template_id(crewmate) == MATE_CONDUCTOR_AGENT
    except Exception:  # noqa: BLE001 - "not a conductor" is the conservative answer
        return False


def _hands_off_to_own_worker(
    caller_slot: Any, caller_execution: ExecutionContext | None, requested: str, agent_kind: str
) -> bool:
    """Whether this ``session_create`` opens the calling crewmate's own worker.

    True for every request from a crewmate's conductor chat except one that
    selects ANOTHER crewmate by name (a name-first request naming a
    ``config.agents`` row that is not the caller's own): that keeps its own
    resolution, so a peer bound elsewhere stays refused. Everything else -- no
    agent, the worker, the stock worker, the crewmate's own name, any template,
    a name nothing resolves -- is the crewmate's worker. False for every caller
    that is not a conductor, and for every caller while Crewmate 2.0 is off
    (:mod:`kiro_crew.crewmate_v2`). Blocking config read; callers run it off the loop.
    """
    from kiro_crew import crewmate_v2

    if not crewmate_v2.enabled():
        return False
    if not _conducts_for_a_worker(caller_slot, caller_execution):
        return False
    if agent_kind == "template" or not requested:
        return True
    try:
        cfg = KiroCrewConfig.load()
        own = _dispatching_crewmate(cfg, caller_slot, caller_execution)
    except Exception:  # noqa: BLE001 - unreadable config: keep the named resolution
        return False
    named = cfg.agents.get(requested)
    return named is None or named is own


def _caller_member_workspace(
    cfg: KiroCrewConfig, caller_slot: Any, caller_execution: ExecutionContext | None
) -> str | None:
    """The declared workspace of the crew member ``caller_slot`` runs as, or ``None``.

    The workspace an explicit TEMPLATE child inherits from its creator. A record
    is required: the template arm of ``create_session`` binds the child to the
    caller's store only when the caller has one, and without it the child would
    fall to the template's default store while running in the member's
    workspace. A workspace name absent from ``config.workspaces`` is ``None``,
    because the member itself then resolves to ``default_workspace`` and the
    ordinary check already covers it.
    """
    if caller_execution is None:
        return None
    crewmate = _dispatching_crewmate(cfg, caller_slot, caller_execution)
    if crewmate is None:
        return None
    name = crewmate.workspace or ""
    return name if name in cfg.workspaces else None


async def _record_spawn(caller_key: str, child: Any) -> bool:
    """Write the child's creating edge to the CREATOR's crew log (``session/spawned``)
    and wait, bounded, for it to land. Returns whether the edge is on disk.

    The mint witness (``_lineage_minted``) lives only in this process, and the child's
    own ``session/opened.parent`` is not written until its first turn, so neither
    survives a restart in between. The creator's log already exists, so the edge is
    recorded there, and the caller is told the create succeeded only after this has
    settled: a reply sent while the entry is still queued would be lost with it on a
    crash, and the child would restore at the top level.

    Taken from the values stamped at mint, and only when this process stamped them:
    the creator sid frozen at mint names the log that was live when the child was
    made. ``True`` with nothing written when there is no edge to record -- no witness,
    or a creator with no ACP session yet, which has no log and keeps the in-memory
    witness alone. Never raises: the create has already committed, so a failure here
    is reported as ``False`` rather than as a failed create, which a caller would
    retry into a duplicate session.

    The write is queued before the wait, so a cancellation of the wait does not
    withdraw it: the entry still lands with the birth it belongs to.
    """
    if not bool(getattr(child, "_lineage_minted", False)):
        return True
    creator_slot = str(getattr(child, "_created_by", "") or "")
    creator_sid = str(getattr(child, "_created_by_sid", "") or "")
    child_slot = str(getattr(child, "key", "") or "")
    if not creator_sid or not creator_slot or creator_slot != caller_key:
        return True
    try:
        if not crew_log_emit.enabled():
            return True
        landed = await crew_log_emit.awaiting_commit(
            lambda settled: crew_log_emit.on_session_spawned(
                creator_sid,
                creator_slot=creator_slot,
                child_slot=child_slot,
                on_settled=settled,
            ),
            what="session/spawned",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("session_control: could not record session/spawned", exc_info=True)
        return False
    if not landed:
        logger.warning(
            "session_control: the creating edge for %s did not land in its creator's crew "
            "log; it nests under %s only until this gateway restarts",
            child_slot,
            creator_slot,
        )
    return landed
