"""A dashboard turn's admission phase and whether it may run on the session it is bound to:
``_turn_admit`` (the admission wait, the dispatch lock, the bindings and the session claim),
the current binding and its requirement, the owner check and the dispatch lock's release."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

from kiro_crew.config.loader import normalize_agent_model
from kiro_crew.context import discard_memory_prefetch
from kiro_crew.dashboard.chat_utils import (
    _KIRO_ONLY_BLOCKED_SLASH_COMMANDS,
    APP_AGENT_REFUSED_KIND,
    _normalize_model,
    slot_history_key,
    tighten_live_slot_memory_mode,
)
from kiro_crew.dashboard.state import durable_row_count
from kiro_crew.llm_helpers import _billing_stats
from kiro_crew.members import DM_SLOT_MODE, is_dispatchable_member_name, member_lifecycle
from kiro_crew.platform import redact_log_via_context
from kiro_crew.quick_prompts import QUICK_PROMPTS

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        ACP_BACKEND_KIRO,
        MAX_PROMPT_BYTES,
        KiroCrewConfig,
        ResolvedBindings,
        Routing,
        SessionBusyError,
        _AppAgentNotLoaded,
        _MemoryUnavailable,
        capabilities_of,
        crew_log_emit,
        effective_session_key,
        expire_slack_options,
        invalidate_stale_kas_session,
        logger,
        publish_turn_identity,
        record_activity,
        record_agent_selection,
        refresh_materialized_agents,
        reproject_claimed_session,
        resolve_agent_bindings,
        resolve_session_agent_bindings,
        routing_for,
        save_slot_off_loop,
        sel,
        slot_switch_session_lock,
        subprocess_executor,
        telemetry_channel_of,
        warm_project_agent_names,
    )
    from kiro_crew.dashboard.chat_turn.account_identity import _retire_sessions_on_identity_change
    from kiro_crew.dashboard.chat_turn.app_agent_binding import (
        _app_agent_host_allowed,
        _app_agent_turn_backend,
        _provider_backend,
        _recover_app_agent_binding,
        _retire_app_agent_session,
        _runs_app_agent_launch,
        app_agent_binding_applies,
    )
    from kiro_crew.dashboard.chat_turn.context_cap import _cap_conductor_context
    from kiro_crew.dashboard.chat_turn.crew_log_attribution import (
        _crew_log_class,
        _crew_log_inherited_parent,
        _crew_log_lineage,
        _crew_log_model,
        _crew_log_seed_tree,
        _crew_log_tree_depth,
        _crew_log_workspace,
        _slot_predecessor_store,
    )
    from kiro_crew.dashboard.chat_turn.eager_spawn import _clear_eager_spawn_failures
    from kiro_crew.dashboard.chat_turn.mcp_session import (
        _drain_session_init_oauth_requests,
        _publish_session_mcp_report,
    )
    from kiro_crew.dashboard.chat_turn.model_fallback import (
        _app_allocation_kwargs,
        _inherited_default_model,
        _settle_session_model,
    )
    from kiro_crew.dashboard.chat_turn.prompt_assembly import (
        _expand_prompt_mention_off_loop,
        _prewarm_startup_query,
    )
    from kiro_crew.dashboard.chat_turn.session_reset import _consume_pending_reset
    from kiro_crew.dashboard.chat_turn.slash_commands import _refuse_blocked_slash
    from kiro_crew.dashboard.chat_turn.tool_approval import _persistable_session_policy
    from kiro_crew.dashboard.chat_turn.turn_context import (
        _dm_member_binding_changed,
        _message_rewritten_before_build,
        _read_and_tighten_turn_execution,
        unavailable_agent_message,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow
    from kiro_crew.dashboard.chat_turn.turn_marker import (
        _begin_local_turn_marker,
        _local_turn_generation_for,
    )
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import _steer_segment_cut
    from kiro_crew.dashboard.chat_turn.turn_spec_hooks import (
        _notice_seed_declined,
        _notice_steer_upgrade,
        _prepare_spec_hooks,
        _surface_agent_welcome,
    )


def _current_binding(turn) -> tuple:
    slot = turn.slot
    return (
        slot.agent,
        slot.project,
        slot.workspace,
        slot.memory_store,
        slot._memory_assignment_from_history,
        effective_session_key(slot),
        slot.agent_kind,
    )


def _require_current_binding(turn, *, project_may_be_staged: bool = False) -> None:
    # ``project_may_be_staged``: once the session is registered the
    # project route may stage a pick for the NEXT turn (it writes
    # slot.project and arms the deferred reset). Checks made after that
    # point compare every other binding field and leave the project
    # out, because this turn runs on its start-of-turn project and the
    # staged pick is not a change to it.
    registered_slot = turn.registered_slot
    slot = turn.slot
    state = turn.state
    current = _current_binding(turn)
    expected = turn.selected_binding
    if project_may_be_staged:
        current = current[:1] + current[2:]
        expected = expected[:1] + expected[2:]
    if (registered_slot is slot and state._slots.get(slot.key) is not slot) or current != expected:
        raise _MemoryUnavailable(
            "memory_unavailable: conversation binding changed during preparation; " "retry the turn"
        )


def _check_owner(turn) -> tuple[str | None, str]:
    """The refusal of an app-owned turn (None when it may run) and the launch copy it runs as.

    The copy's name is empty unless the turn runs on kiro-cli.
    """
    # Only kiro-cli and KAS launch the app's named spec; checked first,
    # since the name check below means nothing on another backend.
    from kiro_crew.apps.agent_launch import AppAgentLaunchError, publish_app_agent_launch
    from kiro_crew.apps.manager import (
        app_agent_backend_refusal,
        app_agent_launch_refusal,
        app_command_agent_refusal,
    )

    _owner_backend = turn._owner_backend
    _owner_host_allowed = turn._owner_host_allowed
    owner_agent = turn.owner_agent
    owner_app = turn.owner_app
    if not _owner_host_allowed:
        return app_agent_backend_refusal(owner_app), ""
    # Reads the user-level specs. No checkout is consulted: on both allowed
    # backends the turn runs the app's registered file, never a checkout's
    # same-named spec (apps.manager.app_command_agent_refusal).
    refused = app_command_agent_refusal(owner_app, owner_agent)
    if refused:
        return refused, ""
    if _owner_backend == ACP_BACKEND_KIRO:
        try:
            launch = publish_app_agent_launch(owner_app, owner_agent)
        except AppAgentLaunchError as exc:
            return app_agent_launch_refusal(owner_app, exc.detail), ""
        # kiro-cli reads no checkout spec for the copy's name: it runs from the
        # empty launch directory (apps.agent_launch.launch_process_cwd).
        return None, launch
    return None, ""


def _release_dispatch_lock(turn) -> None:
    _dispatch_lock = turn._dispatch_lock
    if turn._dispatch_lock_held:
        turn._dispatch_lock_held = False
        _dispatch_lock.release()


async def _turn_admit(turn: _ChatTurn) -> _Flow | None:
    """Admit the turn onto its session: the admission wait, the dispatch lock, the bindings and the session claim."""
    _spec_hooks: list
    _spec_hooks_cwd: str | None
    _turn_stats0: object
    _attachments = turn._attachments
    _crew_log_sid = turn._crew_log_sid
    _crew_log_turn_no = turn._crew_log_turn_no
    _deferred_options_expiry = turn._deferred_options_expiry
    _is_refusal_retry_turn = turn._is_refusal_retry_turn
    _local_turn_marker_generation = turn._local_turn_marker_generation
    _prompt_depth = turn._prompt_depth
    _synthetic_recovery_turn = turn._synthetic_recovery_turn
    _turn_priority = turn._turn_priority
    client = turn.client
    first_word = turn.first_word
    is_slash = turn.is_slash
    message = turn.message
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # Publish the immutable identity BEFORE the first admission await, as
    # the first statement _run_chat's finally covers. From here down the
    # local session_key is what the turn acquires, audits and releases
    # while slot.linked_session_key remains mutable underneath it -- and
    # the admission awaits below are exactly where a rebind can land: a
    # cron injection reassigns the routing with no ``running`` gate, so a
    # key published only after admission would leave this turn's session
    # invisible to every reader of the turn key (the cancel routes and
    # the slot-switch busy scan, ``_switch_target_busy``) for the whole
    # memory-preparation wait, while ``slot.task`` already says a turn is
    # running. A turn refused at admission (timeout, Stop) leaves through
    # the same finally, whose _turn_release_session (chat_turn/session_reset.py)
    # compare-and-clears only an identity this turn actually published, so a
    # successor's key is never wiped.
    slot._active_turn_session_key = session_key

    if slot.mode == DM_SLOT_MODE and not is_dispatchable_member_name(slot.agent):
        logger.warning(
            "refusing to run member slot %s with a non-dispatchable pin",
            redact_log_via_context(slot.key),
        )
        slot.append(
            "error",
            "This thread's crew name cannot be dispatched. Rename or recreate the Crew Member.",
            "msg msg-err",
        )
        return _Flow.END

    # The gateway publishes this shared task before READY, then performs the
    # restore/open/rebuild work after READY. Wait at the one dashboard turn
    # admission seam before expiring controls, resolving bindings,
    # allocating a provider or writing metadata. The turn's grace period
    # is bounded; timeout and Stop leave the shared worker alive and
    # retain queued intent until a later admitted turn.
    from kiro_crew.memory_startup import wait_for_memory_preparation

    await wait_for_memory_preparation(getattr(state, "memory_startup_task", None))
    _memory_preparation_admitted = True
    turn._memory_preparation_admitted = _memory_preparation_admitted

    # A new turn supersedes whatever question the previous one ended on, so
    # any OPTIONS control still live in this session's Slack thread stops
    # being answerable. Guarded on _prompt_depth so the in-turn re-entry that
    # expands a /prompts reference does not count as a new turn. Local
    # commands returned in _turn_slash_gate, except the capability-gated commands whose
    # authority is the acquired provider. Defer those until their gate below.
    if _prompt_depth == 0 and first_word not in _KIRO_ONLY_BLOCKED_SLASH_COMMANDS:
        await expire_slack_options(state, session_key)

    # Durable "turn in flight" marker, written at the same boundary as the
    # active-turn identity: pre-acquisition local commands have returned,
    # no provider work has begun. Not gated on ``_prompt_depth``: the expanded re-entry
    # is the only call that reaches here for a /prompts mention. The
    # generation is bound before the save is awaited so a cancellation
    # inside it still reaches _run_chat's finally with something to retire.
    _local_turn_marker_generation = _local_turn_generation_for(slot)
    turn._local_turn_marker_generation = _local_turn_marker_generation
    await _begin_local_turn_marker(state, slot, _local_turn_marker_generation)

    # Resolve agent bindings early so we pass the correct kiro-cli
    # agent name (e.g. "kirocrew") instead of the Kiro Crew slot name
    # (e.g. "default") which has no matching ~/.kiro/agents/ config.
    kiro_agent: str | None = None
    turn.kiro_agent = kiro_agent
    memory_store: str | None = None
    turn.memory_store = memory_store
    private_member = ""
    turn.private_member = private_member
    execution_context = None
    turn.execution_context = execution_context
    # The Kiro Crew agent's own default model ("" = inherit). Ranks below the
    # slot's explicit pick and above the bound kiro agent's pin / the global
    # agent.model fallback.
    agent_model = ""
    turn.agent_model = agent_model
    # Bound only when the config loaded: `cfg` itself is unbound on a load
    # failure (see `provider_name`), and the default-model resolve below
    # needs the loaded object.
    loaded_cfg: KiroCrewConfig | None = None
    bindings: ResolvedBindings | None = None
    # Read the provider into a local alongside the other bindings. Both model
    # branches below need it, and `cfg` is only bound inside the try — a
    # malformed config raises, the except swallows it, and touching
    # `cfg.agent.provider` afterwards would raise UnboundLocalError and kill
    # the turn. "" is the honest value for "config unreadable": it is not
    # "claude_code", so the model helpers fall through to their live-client
    # guards (the advertised-list capability, the advertised ids) rather than trusting a
    # provider name that could not be read.
    provider_name = ""
    turn.provider_name = provider_name
    # Canonical crew identity for watchdog overrides — same seeding rule
    # as the eager-spawn path (the two must agree): slot value until the
    # resolver supplies its alias, which covers the default crew on an
    # empty slot.
    crew_alias = slot.agent or ""
    # An app-owned slot whose agent never resolved (see the fail-loud guard
    # after the resolve block). Captured inside the try so the raise below
    # lives OUTSIDE it and is not swallowed by the resolve except.
    _app_agent_unresolved = False

    # Serialize this turn's binding capture and session registration
    # against the slot-switch transaction of every alias on this session
    # (the same ``slot_switch_session_lock`` the switch handlers hold
    # across their busy scan and reset, and the refusal-fallback restore
    # takes during the turn). Without it a switch can scan (no sibling
    # yet), this dispatch can capture the pre-switch bindings, the reset
    # can find no registered session and "succeed", and the session then
    # registers on the old bindings. Under the lock the two are atomic
    # with respect to each other: this turn either registers first (the
    # scan then sees it and refuses) or captures after the switch's commit
    # and reset (so it starts on the NEW bindings). Held only through the
    # COLD-START registration inside ``get_or_create`` and released right
    # after -- never while waiting for an existing session's turn lease
    # (see the allocation site below) and never across the turn itself,
    # whose refusal-fallback helpers take this same non-reentrant lock.
    # Every await inside the span is a thread offload, a config/binding
    # read, or a SessionManager call that takes no lock another turn holds
    # while wanting this one. Lock order: this task holds no slot lock,
    # so a switch handler holding ``slot._lock`` and waiting here cannot
    # be waited on in turn. Exits between acquire and release (a binding
    # that changed during preparation, a provider that failed to start,
    # cancellation) reach _run_chat's finally, which releases first
    # thing, in _turn_record_outcome (chat_turn/queue_cycle.py).
    _dispatch_lock = slot_switch_session_lock(session_key)
    turn._dispatch_lock = _dispatch_lock
    await _dispatch_lock.acquire()
    turn._dispatch_lock_held = True
    selected_binding = _current_binding(turn)
    turn.selected_binding = selected_binding
    registered_slot = state._slots.get(slot.key)
    turn.registered_slot = registered_slot

    try:
        cfg = KiroCrewConfig.load()
        loaded_cfg = cfg
        provider_name = cfg.agent.provider
        turn.provider_name = provider_name
        # Project discovery and canonical session reads both access files.
        # Resolve the captured selection off-loop, then reject any concurrent
        # change before publishing memory authority or allocating a provider.
        # source="unknown", not "dashboard": Slack-triggered turns reach
        # _run_chat too (slack/gateway.py and slack/handler.py call it
        # directly), so this hop serves multiple channels.
        await warm_project_agent_names(selected_binding[1], operation="chat_turn", source="unknown")
        bindings = await asyncio.to_thread(
            functools.partial(resolve_session_agent_bindings, agent_kind=selected_binding[6]),
            resolve_agent_bindings,
            cfg,
            session_key,
            selected_binding[0] or None,
            selected_binding[1] or None,
        )
        _require_current_binding(turn)
        kiro_agent = bindings.kiro_agent
        turn.kiro_agent = kiro_agent
        crew_alias = bindings.resolved_alias
        memory_store = bindings.memory_store_name
        turn.memory_store = memory_store
        agent_model = normalize_agent_model(bindings.model)
        turn.agent_model = agent_model
        # SELF-HEAL an app-owned slot whose agent did not resolve. An app's
        # agents live only in ``~/.kiro/agents/<app>--<agent>.json`` (never in
        # ``config.agents``), so resolve_agent_bindings can honor them only via
        # the materialized-agent snapshot — which is COLD on the event loop
        # until the boot / registration warm lands (both run off the loop). A
        # cold read makes the resolver fall back to the default agent with
        # ``requested_resolved=False``, silently running the generic default
        # with none of the app's MCP tools and NO error. Recover in two
        # escalating steps: (1) RESCAN the snapshot off the loop with the SAME
        # pattern server.py uses at boot (safe to await: refresh_materialized_
        # agents never raises) then re-resolve ONCE — covers "spec on disk but
        # snapshot cold"; (2) if STILL unresolved, RE-REGISTER this app's
        # agents FROM SOURCE off the loop (refresh_app_agents rewrites the
        # specs + publishes the snapshot synchronously) then re-resolve again —
        # covers "spec never materialized though source intact". A residual
        # miss falls through to the fail-loud below. Strictly guarded: the
        # common hot path (no ``_app``, or already resolved) does zero extra
        # work and zero extra I/O.
        #
        # Step (1) also covers a named non-app slot while the installed snapshot
        # is incomplete: a scan that could not read a spec, or a boot
        # scan that has not landed, may be missing a template whose file is on
        # disk, and the turn below would otherwise fail memory_unavailable for
        # it. One rescan, then the same fail-loud. A complete snapshot is
        # trusted, so a really removed agent costs no I/O here.
        from kiro_crew.config.loader_snapshots import materialized_snapshot_complete

        if not bindings.requested_resolved and (
            slot._app or (slot.agent and not materialized_snapshot_complete())
        ):
            try:
                await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), refresh_materialized_agents
                )
            except Exception:  # noqa: BLE001 — warm failure only costs the fail-loud below
                logger.warning(
                    "Failed to warm materialized agents for slot %s",
                    slot.key,
                    exc_info=True,
                )
            bindings = await asyncio.to_thread(
                functools.partial(resolve_session_agent_bindings, agent_kind=selected_binding[6]),
                resolve_agent_bindings,
                cfg,
                session_key,
                selected_binding[0] or None,
                selected_binding[1] or None,
            )
            _require_current_binding(turn)
            kiro_agent = bindings.kiro_agent
            turn.kiro_agent = kiro_agent
            crew_alias = bindings.resolved_alias
            memory_store = bindings.memory_store_name
            turn.memory_store = memory_store
            agent_model = normalize_agent_model(bindings.model)
            turn.agent_model = agent_model
            if slot._app and not bindings.requested_resolved:
                bindings = await _recover_app_agent_binding(
                    cfg, slot, project=selected_binding[1] or None
                )
                _require_current_binding(turn)
                kiro_agent = bindings.kiro_agent
                turn.kiro_agent = kiro_agent
                crew_alias = bindings.resolved_alias
                memory_store = bindings.memory_store_name
                turn.memory_store = memory_store
                agent_model = normalize_agent_model(bindings.model)
                turn.agent_model = agent_model
        # A fallback alias supplies defaults, not this template's member identity.
        if bindings.selection_kind == "template":
            crew_alias = ""
        _app_agent_unresolved = bool(slot._app) and not bindings.requested_resolved
        if slot.agent and not slot._app and not bindings.requested_resolved:
            from kiro_crew.memory_stores import UnknownMemoryStore  # noqa: F811

            raise UnknownMemoryStore(unavailable_agent_message(slot.agent))
    except Exception as exc:
        logger.warning("Failed to resolve agent bindings in _run_chat", exc_info=True)
        from kiro_crew.memory_stores import UnknownMemoryStore

        if isinstance(exc, _MemoryUnavailable):
            raise
        if slot.memory_store or isinstance(exc, UnknownMemoryStore):
            raise _MemoryUnavailable(f"memory_unavailable: {exc}") from exc

    _require_current_binding(turn)
    if (
        bindings is not None
        and bindings.selection_kind == "member"
        and not is_dispatchable_member_name(crew_alias)
    ):
        logger.warning(
            "refusing a non-dispatchable member alias for slot %s",
            redact_log_via_context(slot.key),
        )
        slot.append(
            "error",
            "This thread's crew name cannot be dispatched. Rename or recreate the Crew Member.",
            "msg msg-err",
        )
        return _Flow.END
    # A session a palette command opened to run as its app's OWN agent runs the
    # app's registered spec and nothing else. Refuse the turn -- before any
    # app-authored prompt reaches the provider -- when that spec is not the one
    # the turn can run; otherwise the prompt would run with another spec's tools
    # and auto-approvals.
    owner_app, _, owner_agent = (slot.app_agent_owner or "").partition("/")
    turn.owner_app = owner_app
    turn.owner_agent = owner_agent
    # On kiro-cli the turn launches the app's launch copy, by a name Crew
    # writes, instead of the bare name kiro-cli would resolve project-first
    # (apps.agent_launch). Empty on every other turn.
    _app_launch_agent = ""
    # The spec file this turn's session is pinned to (``skill_scope_pin``): the
    # app's registered file for an admitted app-owned turn, on kiro-cli and KAS
    # alike, else none. A pinned session's skill scope and injected prompt are
    # read from that file by path, so a checkout's same-named spec sets neither.
    # Published only once this turn holds the session's lease, so a sibling turn
    # waiting for it cannot clear the pin from under the turn that is running.
    _pinned_spec = None
    if not app_agent_binding_applies(slot):
        from kiro_crew.apps.agent_launch import unbind_session_app_agent

        # This turn's session is not the app's: a binding left by an earlier
        # app-owned turn of the key (the slot switched agents, or was deleted and
        # recreated under the same name) must not decide what it projects.
        unbind_session_app_agent(session_key)
    if app_agent_binding_applies(slot):
        _owner_backend = await _app_agent_turn_backend(state, session_key, loaded_cfg)
        turn._owner_backend = _owner_backend
        _owner_host_allowed = (
            _owner_backend is not None and routing_for(_owner_backend) is Routing.AGENT_SPEC
        )
        turn._owner_host_allowed = _owner_host_allowed

        refusal, _app_launch_agent = await asyncio.to_thread(_check_owner, turn)
        if not refusal:
            from kiro_crew.apps.agent_launch import bind_session_app_agent, registered_spec_path

            _pinned_spec = registered_spec_path(owner_app, owner_agent)
            # Before the claim, so a KAS session started for this turn projects the
            # registered file by its path (acp.harness.kas), never the bare name.
            bind_session_app_agent(session_key, owner_app, owner_agent)
        if not refusal and _app_launch_agent:
            # A session already registered under the key was started as
            # something other than the copy (the bare name, before this turn
            # chose the copy); it must not serve this turn, so the claim
            # below cold-starts under the copy.
            live = state.sessions.get_provider(session_key)
            if live is not None and not _runs_app_agent_launch(live, _app_launch_agent):
                await _retire_app_agent_session(state, slot, session_key)
        if refusal:
            logger.warning(
                "refusing an app-owned agent turn for slot %s: %s",
                redact_log_via_context(slot.key),
                refusal,
            )
            # SEL: an authorization denial belongs in the audit chain, not only
            # the process log. Best-effort: the refusal stands if auditing fails.
            try:
                sel().log_api_access(
                    caller=f"slot={slot.key}",
                    operation="chat_runner.app_command_agent",
                    outcome="denied",
                    source="app_command",
                    resources=f"slot={slot.key} app={owner_app} agent={owner_agent}",
                    error=refusal,
                )
            except Exception:  # noqa: BLE001 - auditing must not unblock the turn
                logger.warning(
                    "SEL denial audit failed for an app-owned agent turn -- refusal stands",
                    exc_info=True,
                )
            # A speculative session may already be registered under this key,
            # resolved against the shadowing spec. Cancel any pending eager
            # start and retire what it registered, so a later turn that passes
            # the check cold-starts on the app's spec instead of reusing it.
            await _retire_app_agent_session(state, slot, session_key)
            slot.append("error", refusal, "msg msg-err", meta={"kind": APP_AGENT_REFUSED_KIND})
            return _Flow.END
    if bindings is not None:
        from kiro_crew.execution_context import (  # noqa: F811
            ExecutionContext,
            MemoryStoreRef,
            bind_session_execution,
            read_session_execution,
            resolve_member_execution,
        )

        previous_execution = await asyncio.to_thread(read_session_execution, session_key)
        _require_current_binding(turn)
        if previous_execution is not None:
            execution_context = previous_execution.with_mode(slot.memory_mode)
            turn.execution_context = execution_context
            if execution_context.store.store_id != (memory_store or "default"):
                raise _MemoryUnavailable(
                    "memory_unavailable: this conversation's member binding changed; "
                    "open a new conversation"
                )
        elif bindings.selection_kind == "member":
            execution_context = resolve_member_execution(
                loaded_cfg,
                crew_alias,
                memory_mode=slot.memory_mode,
                app=slot._app or "",
                validate_memory_files=False,
            )
            turn.execution_context = execution_context
            # A RESTRICTED session (incognito/temporary) persists no durable
            # execution carrier -- its line names no ``execution_context`` and
            # no ``memory_store`` -- so ``previous_execution`` is None and this
            # branch re-selects the member from the ALIAS the restored slot
            # carries. The alias is mutable: a member removed and a new one
            # created under the same name reassigns it to a DIFFERENT immutable
            # id, and an incognito session READS memory, so a bare alias
            # re-selection would bind the new member's PRIVATE store and
            # surface memory this chat never ran as.
            #
            # Binding that private store is sound only when a durable identity
            # the session itself carries -- one that is NOT the agent-writable
            # metadata line, which the pin must never re-derive identity from
            # -- confirms the alias still names the same member. The sole such
            # identity is the slot KEY (the session's address, not an editable
            # payload): a DM slot key encodes the member_id (id-based for a
            # member with a persisted identity, durable across the original
            # member's removal). An ordinary ``chat-N`` slot's key encodes NO
            # id, so there is nothing un-plantable to confirm the re-selection
            # -- but the leak this guards is specific to a DM thread whose
            # alias has been reassigned to a different member, so only that
            # case refuses:
            #  * DM slot whose encoded id DIFFERS from the resolved member's
            #    (the alias was reassigned): the thread belongs to a specific
            #    member and the alias now names another. Refuse fail-closed --
            #    the same "open a new conversation" answer the durable-carrier
            #    path above gives on a store mismatch, leaving the member's
            #    memory intact to bind cleanly in a new conversation.
            bound_member_id = execution_context.member_id or ""
            if _dm_member_binding_changed(bound_member_id, slot.key):
                raise _MemoryUnavailable(
                    "memory_unavailable: this conversation's member "
                    "binding changed; open a new conversation"
                )
        else:
            execution_context = ExecutionContext(
                None,
                MemoryStoreRef(memory_store or "default"),
                "template",
                kiro_agent or "kirocrew",
                slot.memory_mode,
                slot._app or "",
                selected_binding[0] or kiro_agent or "kirocrew",
            )
            turn.execution_context = execution_context
        bindings.execution_context = execution_context
        private_member = execution_context.member_id or ""
        turn.private_member = private_member
        slot.memory_store = execution_context.store.legacy_name
        selected_binding = _current_binding(turn)
        turn.selected_binding = selected_binding
        if selected_binding[0] or bindings.resolved_alias:
            await asyncio.to_thread(
                record_agent_selection,
                session_key,
                selected_binding[0],
                bindings,
                memory_mode=slot.memory_mode,
            )
        else:
            await asyncio.to_thread(
                bind_session_execution,
                session_key,
                execution_context,
                expected=previous_execution,
                replace_existing=previous_execution is not None,
            )
        _require_current_binding(turn)
        tightened_execution = await asyncio.to_thread(
            _read_and_tighten_turn_execution,
            state.conversation_log,
            session_key,
            slot_history_key(slot),
        )
        _require_current_binding(turn)
        if tightened_execution is not None and tighten_live_slot_memory_mode(
            state, slot.key, tightened_execution.memory_mode, expected_slot=slot
        ):
            execution_context = tightened_execution
            turn.execution_context = execution_context
            bindings.execution_context = tightened_execution
        # A crewmate's conductor chat and each of its workers take the
        # crewmate's Profile Permissions before the provider is claimed, so a
        # session restored after a restart (slot trust is in memory only) does
        # not fall back to asking for every tool.
        from kiro_crew.dashboard.crewmate_approval import seed_for_turn

        seed_for_turn(state, slot, loaded_cfg, execution_context)
        # A crewmate's worker runs in the crewmate's own git worktree of the
        # repository this turn is about, never in the shared checkout.
        from kiro_crew.dashboard.crewmate_worktree_scope import scope_worker_turn

        if await scope_worker_turn(state, slot, loaded_cfg, execution_context, message):
            # The project is part of the captured binding; this turn now
            # runs on the worktree, under the dispatch lock it still holds.
            selected_binding = _current_binding(turn)
            turn.selected_binding = selected_binding

    # FAIL-LOUD: an app-owned slot whose agent STILL did not resolve after the
    # self-heal must NOT run the default agent — that generic-substitution is
    # the bug this whole path guards against. End the turn with a clear card
    # naming the requested agent, surfaced through _run_chat's SAME outer try/except
    # error path every other fatal turn error uses (see the
    # ``except _AppAgentNotLoaded`` arm beside the terminal handlers). Raised
    # here — after the resolve except, before get_or_create, while ``_acquired``
    # is still False — so the abort runs _run_chat's standard finally teardown without
    # ever creating a session or dispatching an agent.
    if _app_agent_unresolved:
        raise _AppAgentNotLoaded(
            f"The app agent '{slot.agent or ''}' isn't loaded yet — "
            "try again in a moment, or restart the gateway."
        )

    state.broadcast_ws(
        "activity_event", {"slot": slot.key, "kind": "status", "text": "Creating session…"}
    )
    slot.model = _normalize_model(slot.model or "") or ""
    # The default a slot that pins nothing inherits, kept OUT of `slot.model`:
    # that field is persisted and re-sent as a `set_model` override on every
    # resume, so an empty value means "inherit" and the slot follows a later
    # change to `agent.model` or to the agent's pin. Read from the session
    # layer's own resolver (see _inherited_default_model) only to tell
    # whether the live session still runs it; the allocation below is asked
    # for no model and resolves the ladder itself.
    default_model = ""
    if not slot.model and not agent_model:
        default_model = await _inherited_default_model(
            state.sessions,
            slot,
            session_key,
            agent=kiro_agent or slot.agent or None,
            crew_agent=crew_alias,
        )
    # Commit a pending session_set_model pick before get_or_create reads
    # slot.model. The pick's authorization is re-checked in the same
    # synchronous step as the write; when the model changed, queue a reset
    # so the consume below cold-starts this turn on the new model.
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard.session_control import (
        apply_deferred_model_switch,
        apply_pending_model_pick,
        arm_model_switch_reset,
        prewarm_enabled_check,
    )

    if slot._pending_model_pick is not None:
        # Warms the config the pick's fence re-check reads, so the
        # synchronous gate below does no file IO on the loop.
        await prewarm_enabled_check()
    if apply_pending_model_pick(state, slot):
        arm_model_switch_reset(state, slot)
        # The commit above is memory-only; persist it before the provider is
        # acquired, so a gateway exit mid-turn does not restore the old pin.
        # Awaiting here is safe: the gate and the write already happened in
        # one synchronous step. A refused save leaves the slot dirty for the
        # periodic flush.
        _pick_history_key = slot_history_key(slot)
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=_pick_history_key
        ):
            slot._dirty = True
    # An inheriting slot (no slot pin, no crew pin: only then is
    # ``default_model`` non-empty) follows a change to the inherited default.
    # A settings write rebuilds the factory but leaves live sessions alone, so
    # the session this slot already holds can still be on the previous
    # default. Its allocation stamp says which model it was started on; when
    # that differs from what the slot inherits now, queue the same reset a
    # model pick queues. The consume below runs it with ``skip_if_busy``, so a
    # turn another surface is streaming on the session is never cut: the flag
    # stays armed and lands at a later consume. An empty stamp is unknown, not
    # stale, and is left alone.
    if default_model:
        _live_key = effective_session_key(slot)
        _stamped = state.sessions.allocation_requested_model(_live_key)
        if (
            isinstance(_stamped, str)
            and _stamped
            and _stamped != default_model
            and state.sessions.get_provider(_live_key) is not None
        ):
            logger.info(
                "Slot %s inherits %s but its live session started on %s; "
                "resetting it onto the new default",
                slot.key,
                default_model,
                _stamped,
            )
            arm_model_switch_reset(state, slot)
    # A model pick the picker took while a turn ran on this session, not yet
    # landed: normally the end of that turn lands it (``_end_turn_tail``), and
    # this is the backstop for a turn that ended without its tail or ran on a
    # sibling alias. Before the consume below, because landing it may ARM the
    # reset that consume spends. ``session_lock_held=True``: this turn already
    # holds ``slot_switch_session_lock(session_key)`` for its cold start, so the
    # helper must take only ``_model_pick_lock`` -- that lock is not reentrant,
    # and re-acquiring it here would hang the turn at session creation.
    await apply_deferred_model_switch(state, slot, session_lock_held=True)
    # Consume a deferred project-change reset queued while idle, before
    # get_or_create or we'd reuse the stale session for one turn. Safe even
    # though the dispatch lock (slot_switch_session_lock) is held here: reset()
    # does not take that lock, and driven from the gateway rather than from
    # inside the session's own process group it cannot self-kill its caller.
    await _consume_pending_reset(state, slot)
    # Same "before get_or_create or we reuse a stale session for one turn"
    # reasoning as the reset above, for a staleness the session map cannot
    # see: the child is alive and healthy, but the account it authenticated
    # as is gone. Retiring it here means this turn cold-starts on the current
    # account instead of running as the previous one.
    await _retire_sessions_on_identity_change(state)
    _require_current_binding(turn)
    # The conductor context cap, before the claim for the same reason as the two
    # above and after the binding check, so it reads the binding this turn runs on: a
    # goal conductor's KAS chat over its marks is trimmed, and this turn starts a new
    # session seeded from the transcript instead of re-sending it all.
    await _cap_conductor_context(state, slot, session_key, loaded_cfg, is_slash=is_slash)
    # A linked channel can retain a dashboard-owned key. Resolve both the
    # dedicated Slack field and any inbound-capable channel-neutral mirror
    # before provider construction, so a restart cannot erase the dispatcher
    # signal that distinguishes a linked turn from a direct dashboard turn.
    # Outbound-only mirrors do not own inbound resume and stay direct turns.
    _mirror_link = state.sessions.get_mirror_link(session_key)
    _mirror_resumes = state.sessions.mirror_accepts_inbound(session_key)
    _provider_channel_id = getattr(slot, "_slack_channel", "") or (
        getattr(_mirror_link, "channel_id", "")
        if _mirror_link is not None and _mirror_resumes
        else ""
    )
    # The model this turn SELECTS for the session: the slot's own pin, then the
    # crew's pin. With neither, the turn selects nothing and the allocation
    # resolves the default-model ladder itself (the allocation's stamp then
    # names what it chose). Selection is not transmission -- the provider
    # withholds a model this account cannot run rather than sending it -- so
    # this names what was chosen, not what reached the backend. Bound to a
    # local because two consumers must not diverge: the provider call below,
    # which decides whether to send it, and the crew log's `session/opened`,
    # which records the choice.
    _requested_model = slot.model or agent_model or ""
    _allocation_kwargs: dict[str, Any] = dict(
        agent=kiro_agent or slot.agent or None,
        # Same canonical crew identity as the eager-spawn path — the two
        # must agree or an eager session and its real first turn would
        # carry different watchdog windows.
        crew_agent=crew_alias,
        model=_requested_model or None,
        cwd=slot.project or None,
        # The persisted channel stays separate from the dashboard-owned key
        # so provider startup can distinguish a linked dispatcher from a
        # direct dashboard turn.
        channel_id=_provider_channel_id or None,
        reasoning_effort_override=slot.reasoning_effort or None,
        start_priority=_turn_priority,
        **_app_allocation_kwargs(slot),
    )
    if _app_launch_agent:
        # An app-owned kiro-cli turn asks kiro-cli for the app's launch copy;
        # the session keeps the agent's own name as its identity.
        _allocation_kwargs["app_launch_agent"] = _app_launch_agent
    turn._allocation_kwargs = _allocation_kwargs

    # The switch lock is held for the COLD START only: the window it
    # closes is "no session registered yet, so the switch handlers' busy
    # scan and reset see nothing". A session that is already registered
    # is already visible to that scan, so the lock has nothing left to
    # protect -- and waiting on that session's turn lease while holding it
    # would deadlock: the lease is held by a sibling alias's live turn,
    # whose refusal-fallback restore takes this same lock. So: registered
    # -> release, then wait for the lease outside the lock. Not registered
    # -> allocate under the lock but never wait for a lease there
    # (``wait_if_busy=False``): if a session was registered by a path that
    # does not take this lock (an eager prewarm, a channel-side turn)
    # between the check and the claim, the busy refusal comes back
    # instead of a wait, and the claim is retried outside the lock.
    #
    # First, a live KAS session whose registered agent batch auto-approves a
    # capability a PreToolUse hook now covers is reset: that batch withheld
    # auto-approval only for the hooks of its day, so a hook added since would
    # never see such a call. The claim below then resumes it with a fresh
    # projection. A busy session is left for its next turn.
    await invalidate_stale_kas_session(state.sessions, session_key, kiro_agent or slot.agent or "")
    if state.sessions.has_session(session_key):
        _release_dispatch_lock(turn)
        _wait_if_busy = True
    else:
        _wait_if_busy = False
    # Nothing is registered, so this turn starts the session itself and
    # session/new is on its send path: the first message's embedding starts now
    # and runs beside it. A resumable key restores its transcript instead and
    # builds no session-start memory.
    if (
        not _wait_if_busy
        and not is_slash
        and not state.sessions.resumable_hint(session_key)
        and not _message_rewritten_before_build(slot, message)
    ):
        await _prewarm_startup_query(
            state,
            slot,
            message,
            memory_store=memory_store,
            execution_context=execution_context,
            prefetch_memory=True,
        )
    # The crew log this slot was last writing to, read BEFORE allocation
    # publishes the successor's id over it. A slot outlives its ACP session, so
    # when the session below cold-starts under a NEW id the crew log gains a new
    # store and this is the only moment the previous one is still nameable: the
    # emitter records it as `session/opened.data.previous`, a citation only.
    # That crew log's own dangling turn stays open; nothing here writes into it.
    # A resume answers the same id, and the emitter compares and writes no edge.
    #
    # Through the slot's write-once latch, not straight into a local, because
    # THIS is not always the first allocation for the slot: an eager prefetch
    # allocates ahead of the first turn and maps its own session over the key,
    # so by the time this line runs the mapping can already name the successor.
    # The latch keeps whichever observation came first, which is the only one
    # that saw the predecessor; when no prefetch ran, the latch is empty and
    # this read is that first observation.
    #
    # `mapped_sid` is the FALLBACK here, and it is the right one of the two
    # mapping accessors. `resumable_sid` asks "can this id still be resumed": it
    # stats the ACP transcript on the calling thread, which is a synchronous
    # store read this coroutine must not make, and it PRUNES the entry when that
    # file is gone or empty. Both consequences are wrong for a history citation.
    # The stat is work on the loop for a fact that needs no file, and the prune
    # erases the id exactly when the two stores disagree -- a crew log unit can
    # outlive a truncated ACP transcript, and that unit is the one whose tail
    # most needs closing. `mapped_sid` is one dict lookup, no disk and no
    # mutation, and it still answers when a resume would not: it asks what the
    # key was last serving, not what can still be resumed.
    #
    # What the mapping cannot answer is a replay-pending allocation, which keeps
    # the prior resumable id here so a restart can still resume it. The mapping
    # is then a generation behind the store the slot is writing. So the slot's
    # own units are read first, off the loop through `_slot_predecessor_store`,
    # and they are durable -- which is what makes the answer survive a restart.
    # Ahead of BOTH sits the slot's own record of the store it opened, which the
    # latch prefers: this process's `session/opened` is queued to the writer
    # thread, so a second allocation inside that window finds no unit on disk and
    # only the record can name the store it must cite.
    _previous = await _slot_predecessor_store(state.sessions, slot, session_key)
    slot.latch_crew_log_previous(
        _previous.sid,
        undecided=_previous.undecided,
        from_mapping=_previous.from_mapping,
    )

    # ONE allocation site (the crew-log latch above must sit right before
    # it): the cold-start branch claims under the lock without waiting for
    # a lease; a busy refusal there means a session registered underneath
    # us, so drop the lock and claim again with the normal lease wait. The
    # re-projection below claims through the same site; the latch is
    # write-once, so the store it cites stays the one this turn found.
    async def _claim_session(wait_if_busy: bool) -> tuple[Any, bool, bool]:
        return await state.sessions.get_or_create(
            session_key, wait_if_busy=wait_if_busy, **_allocation_kwargs
        )

    for _claim in (0, 1):
        try:
            client, is_new, resumed = await _claim_session(_wait_if_busy)
            turn.is_new = is_new
            turn.resumed = resumed
            turn.client = client
            break
        except SessionBusyError:
            if _wait_if_busy or _claim:
                discard_memory_prefetch(slot.workspace or None, memory_store, message)
                raise
            _release_dispatch_lock(turn)
            _wait_if_busy = True
        except BaseException:
            # A start that failed builds nothing: drop the memory read early for it.
            discard_memory_prefetch(slot.workspace or None, memory_store, message)
            raise
    # Registered: the switch handlers' busy scan sees this session from
    # here on, so the lock has done its job and the turn must not hold it.
    _release_dispatch_lock(turn)
    # The project this turn prepared on and allocated its process in. Once
    # the lock is released the project route may write slot.project (a
    # pick made while this reply runs applies from the next turn), so the
    # rest of the turn reads this snapshot, not the live field: the prompt
    # context, the activity record and the crew log must describe the same
    # directory kiro-cli is running in.
    _turn_project = selected_binding[1] or ""
    turn._turn_project = _turn_project
    # The pre-claim reset is declined for a session another turn holds, and
    # this claim may have waited for exactly that turn. So the decision is made
    # again on the session this turn now holds, under its lease, before any of
    # its tools run.
    try:
        client, is_new, resumed = await reproject_claimed_session(
            state.sessions,
            session_key,
            kiro_agent or slot.agent or "",
            (client, turn.is_new, turn.resumed),
            lambda: _claim_session(True),
        )
        turn.is_new = is_new
        turn.resumed = resumed
        turn.client = client
    except BaseException:
        state.sessions.release(session_key)
        raise
    _clear_eager_spawn_failures(slot)
    if not is_new and slot._pending_reset_history_key == session_key and client.cwd:
        # The deferred project reset is still armed for the session this
        # turn holds (attached sub-agents keep the process alive), so the
        # process runs in the directory it was spawned in, not the staged
        # pick. The turn describes that directory.
        _turn_project = client.cwd
        turn._turn_project = _turn_project
    if is_new and not resumed:
        # An observation this call CONSUMED, which is not the same as an
        # allocation this call made: a prewarmed session arms
        # `FirstTurnState.FRESH`, whose `is_new` is True and `resumed` is
        # False, so a claim of one lands here indistinguishably from a genuine
        # cold start. The return value cannot separate them.
        #
        # So the stamp is read FIRST. It is the allocation's own selection by
        # construction -- stamped at registration from the local handed to the
        # provider -- which is what this field is defined to name, and it is
        # correct for both cases the branch cannot tell apart. Reading this
        # turn's `_requested_model` first would be correct only for the cold
        # start: on a prewarmed claim it is a fresh resolution of a config that
        # may have moved since the session was allocated, and writing it would
        # report a model that session never ran on, into an append-only entry.
        #
        # `_requested_model` remains the fallback for a session registered by a
        # path that resolves no model and therefore stamps nothing.
        slot._session_requested_model = (
            state.sessions.allocation_requested_model(session_key) or _requested_model
        )
    _acquired = True
    turn._acquired = _acquired
    # The acquired provider is the final authority on the backend, as it is
    # for the slash gates below: the pre-claim check above read the config's
    # selection, which can move before the claim. An app-owned turn on a
    # backend that does not launch from the agent spec would run the
    # app-authored prompt without the app's spec, so it is refused here
    # before anything is sent, and the session is reset so the next turn
    # allocates afresh from the current selection.
    # On kiro-cli the claimed provider must also have been started as the
    # app's launch copy and nothing else: the pre-claim read may have chosen
    # another backend, so this turn passed the bare name, which kiro-cli would
    # resolve project-first.
    _claimed_off_spec = app_agent_binding_applies(slot) and (
        not _app_agent_host_allowed(client)
        or (
            _provider_backend(client) == ACP_BACKEND_KIRO
            and not _runs_app_agent_launch(client, _app_launch_agent)
        )
    )
    if _claimed_off_spec:
        from kiro_crew.apps import manager as app_manager

        if _app_agent_host_allowed(client):
            refusal = await asyncio.to_thread(
                app_manager.app_agent_launch_refusal,
                owner_app,
                "this chat's session did not start from the app's own agent spec. "
                "Run the command again from the command palette.",
            )
        else:
            refusal = await asyncio.to_thread(app_manager.app_agent_backend_refusal, owner_app)
        logger.warning(
            "refusing an app-owned agent turn for slot %s: claimed session off the app spec",
            redact_log_via_context(slot.key),
        )
        try:
            sel().log_api_access(
                caller=f"slot={slot.key}",
                operation="chat_runner.app_command_agent",
                outcome="denied",
                source="app_command",
                resources=f"slot={slot.key} app={owner_app} agent={owner_agent}",
                error=refusal,
            )
        except Exception:  # noqa: BLE001 - auditing must not unblock the turn
            logger.warning(
                "SEL denial audit failed for an app-owned agent turn -- refusal stands",
                exc_info=True,
            )
        needs_session_reset = True
        turn.needs_session_reset = needs_session_reset
        slot.append("error", refusal, "msg msg-err", meta={"kind": APP_AGENT_REFUSED_KIND})
        return _Flow.END
    # This turn holds the session now: the launch copy's skills and prompt are read
    # from the app's registered file, and every other turn of the key reads by name.
    from kiro_crew.skill_scope_pin import pin_session_skill_spec

    pin_session_skill_spec(session_key, _pinned_spec)
    # The acquired provider is the final authority for harness-specific slash
    # gates. A dead live row can be replaced during the get_or_create call
    # above. The positive capability fails closed for unknown adapters and
    # does not infer support from any backend identity.
    _refused_native_todos = (
        first_word in _KIRO_ONLY_BLOCKED_SLASH_COMMANDS
        and not capabilities_of(client).supports_native_todos
    )
    if _refused_native_todos:
        _deferred_options_expiry = False
        turn._deferred_options_expiry = _deferred_options_expiry
    if _refused_native_todos and is_new and not resumed:
        # ``get_or_create`` has consumed the session's one-shot FRESH
        # observation, but this refusal dispatches no prompt. The native
        # session holds no history, so re-use the replay lease: the next
        # ordinary turn is reconstructed as session start (history replay
        # plus member rules). A warm refusal has no observation to restore
        # and must not manufacture a duplicate.
        state.sessions.mark_provider_switch_replay(session_key)
    elif _refused_native_todos and is_new and resumed:
        # A speculative resume also arms is_new. Its native transcript
        # survives (``resumed`` must stay True so the next turn does not
        # replay history the session already holds -- the invariant that a
        # replay lease forces a resumed=False next turn), but this claimant
        # still owes the member's session-start section. Re-arm only the
        # re-injection.
        state.sessions.mark_needs_reinjection(session_key)
    # A fresh provider can still owe Kiro Crew history after its one-shot
    # ``is_new`` observation was consumed by a slash command. Keep that debt
    # separate from provider creation: slash commands bypass ContextBuilder,
    # while the next ordinary prompt must behave as the context-bearing first
    # turn and acknowledge replay only after assembly succeeds.
    _replay_pending = state.sessions.provider_switch_replay_pending(session_key) is True
    turn._replay_pending = _replay_pending
    # A SECOND producer of the same debt the comment above names: the FRESH
    # ``is_new`` observation is consumed at claim time, so a context-bearing
    # first turn that fails BEFORE the provider accepts it (a pre-output
    # backend 5xx the transient path re-queues onto this live session) spends
    # the observation while history never lands. The re-queued turn would read
    # ``is_new=False`` and send the user's message bare, with no history and no
    # SESSION RESUMED marker. The durable ``first_turn_history_owed`` flag,
    # armed below from this turn's FRESH observation and settled once a turn
    # keeps its history with the provider, carries the debt across that
    # failure. Read without clearing, exactly like ``_replay_pending``. The
    # debt is settled when the predicate in _turn_settle_debts holds: a normal
    # ``STOP_REASON_END_TURN`` (non-synthetic, no empty-response verdict), OR
    # any exit that KEPT the session (``_turn_emitted`` and not cancelled and
    # no empty-response verdict — a non-transient error / timeout after output
    # still left the live session holding the history), OR a completed
    # mid-turn compaction — plus the post-token same-session recovery, where
    # the streamed turn already handed the live session its history.
    _first_turn_history_owed = state.sessions.first_turn_history_owed_pending(session_key) is True
    _context_is_new = is_new or _replay_pending or _first_turn_history_owed
    turn._context_is_new = _context_is_new
    # Arm the durable debt from a FRESH observation (``is_new`` with no native
    # resume): the context-bearing first turn is owed its history until a turn
    # durably delivers it. ``resumed`` sessions replayed their transcript
    # natively and must NOT re-inject, so they never arm. The settle in
    # _turn_settle_debts (chat_turn/prompt_assembly.py) consumes it on a kept-session
    # exit: a normal ``end_turn``, any
    # ``_turn_emitted`` exit that was not cancelled and had no empty-response
    # verdict (output stayed with the provider even if an error followed), or
    # a completed compaction. A cancelled / empty-response / pre-output-5xx
    # turn is discarded or re-queued, so it leaves the debt armed for the
    # next turn.
    if is_new and not resumed:
        state.sessions.mark_first_turn_history_owed(session_key)
    if state.sessions.unlanded_marker_pending(session_key) is True:
        # The sid's unlanded marker must land before the turn does, or a
        # crash in the debounce window keeps the sid without it. Awaited
        # off the loop; a failed write fails the turn before it is sent,
        # and _turn_release_session (chat_turn/session_reset.py) hands the session back.
        # A marker left by an earlier turn whose write failed is flushed again here, as a
        # warm retry is not ``is_new``. Only a backend whose resume honours
        # the marker ever carries one, so other turns await nothing.
        await state.sessions.aflush()
    # AcpProvider exposes an exact bool; ``is True`` prevents a truthy mock
    # or fallback from authorizing replay. KAS and every future backend fail
    # closed even if they populate a familiar ``tool_name``.
    _builtin_identity_trusted = client.is_kiro_backend is True
    turn._builtin_identity_trusted = _builtin_identity_trusted
    # The agent spec's own hooks, for a backend that never receives them. Read
    # per turn, so an edit to the spec takes effect the way it does where
    # kiro-cli reads it.
    _spec_hooks, _spec_hooks_unreadable, _spec_hooks_cwd = await _prepare_spec_hooks(
        state, slot, client, kiro_agent or slot.agent or "", is_new=is_new
    )
    turn._spec_hooks_cwd = _spec_hooks_cwd
    turn._spec_hooks_unreadable = _spec_hooks_unreadable
    turn._spec_hooks = _spec_hooks
    turn._spec_hooks_unreadable = _spec_hooks_unreadable
    turn._spec_hooks_cwd = _spec_hooks_cwd
    if is_new:
        _notice_steer_upgrade(state, slot, client)
        await _notice_seed_declined(state, slot, client)
    # A member DM's first turn carries the four-layer member section as
    # session-start context. Record that it is at stake HERE — the moment
    # the session client exists — not at the context build: every early
    # return between this point and build_message (a blocked/oversized
    # @prompt expansion, a pre-build abort) leaves a warm session whose
    # member section was never delivered, and the re-arm in _turn_settle_debts
    # (chat_turn/prompt_assembly.py) reads this flag to make the next turn re-deliver it. The
    # delivery-at-stake verdict is the chokepoint's own
    # (MemberLifecycle.delivers_section — the same source
    # member_turn_context reads), keyed on the slot's member MODE rather
    # than the member name: the name is resolved later, at the context
    # build, and delivery is at stake from this point regardless.
    # needs_reinjection is pinned False: the one-shot flag is not consumed
    # until the context build, and an aborted WARM_REINJECTION delivery is
    # covered by _needs_reinjection in the same _turn_settle_debts re-arm.
    _member_session_start_pending = (
        slot.mode == "member"
        and member_lifecycle(
            is_new_session=_context_is_new,
            resumed=resumed,
            minimal_context=False,
            needs_reinjection=False,
        ).delivers_section
    )
    turn._member_session_start_pending = _member_session_start_pending
    # Session start on a named agent: the greeting belongs to the moment
    # the agent becomes the one answering, which for a cold start is here.
    # `kiro_agent` FIRST, because that is the spec that answers: a crew
    # alias's `slot.agent` is the member the human picked, which names no
    # agent spec, so the roster lookup finds nothing and the running
    # template's hint is dropped. Falls back to `slot.agent` for an ordinary
    # slot, where no template resolution happened and the two are the same
    # name. Both empty means the default crew, which has no spec to carry a
    # hint. Guarded per activation, so the cold start that an agent switch's
    # own session reset produces does not re-emit the row it appended.
    if is_new:
        await _surface_agent_welcome(state, slot, kiro_agent or slot.agent)
    # Member activity pointer — once per SESSION, not per turn: the log
    # answers "which sessions did this member take part in", so a per-turn
    # append would inflate every count taken from it. `slot.agent` is the
    # member the human picked; `kiro_agent` is the template it resolved to,
    # and only the member identity is recorded.
    #
    # Offloaded: this opens and appends to a file, and `_run_chat` shares the
    # single gateway event loop with every other session — matching the
    # to_thread offloads used for the other file IO in this function.
    # record_activity is total, so no guard is needed here.
    if is_new and slot.agent:
        await asyncio.to_thread(
            record_activity,
            slot.agent,
            session_key,
            slot.memory_mode or "",
            project=_turn_project,
            via="chat",
            dedupe_session=True,
        )
    # First-claim bookkeeping above also belongs to an unsupported command:
    # no later claimant sees is_new. Refuse before spending Slack OPTIONS;
    # the replay lease and member re-injection preserve undelivered context.
    if _refused_native_todos:
        _refuse_blocked_slash(turn)
        return _Flow.END
    if _deferred_options_expiry:
        _deferred_options_expiry = False
        turn._deferred_options_expiry = _deferred_options_expiry
        await expire_slack_options(state, session_key)
    # Publish the live inner AcpClient onto the slot so a concurrent request
    # (the dashboard steer handler) can reach the running session's client
    # to inject a mid-turn steer. Cleared in _turn_release_handles (chat_turn/turn_entry.py).
    slot._acp_client = getattr(client, "client", None)
    # Billing stats as they stand BEFORE this turn's prompt is sent. The
    # runtime installs a fresh per-turn stats object only inside the dispatch
    # in _run_chat; a turn that dies before that (expired credential, broken pipe)
    # still carries the PREVIOUS turn's object, whose credits were already
    # recorded. The abnormal seam compares identity against this snapshot and
    # bills nothing when they match, rather than double-billing that turn.
    _turn_stats0 = _billing_stats(client)
    turn._turn_stats0 = _turn_stats0
    # Append-only the session's log (flag-gated, fail-soft). The id is read once here and
    # reused at every later emit site in _turn_admit, _turn_dispatch_gates and later phases, so
    # a turn that never got a session id emits nothing rather than guessing one. ``owner`` is
    # "default" because a crew-bound slot never reaches this local runner.
    _crew_log_sid = crew_log_emit.session_id_of(client)
    turn._crew_log_sid = _crew_log_sid
    # This consumer implements the low-fidelity child downgrade (the
    # interactive card) — opt in so the handle-level fail-close gate
    # yields those events here instead of rejecting them itself.
    # setattr: the LLMProvider interface doesn't declare the attribute;
    # AcpSessionProvider forwards it to the handle, other providers ignore.
    try:
        setattr(client, "child_fidelity_aware", True)
    except Exception:  # pragma: no cover - providers without the attr
        pass
    # This consumer can put an MCP server's elicitation to the person (the
    # elicitation card) -- opt in so the driver yields those events here
    # instead of declining them itself. Same attribute shape as above.
    try:
        setattr(client, "accepts_elicitation", True)
    except Exception:  # pragma: no cover - providers without the attr
        pass
    # Companion steer handle: lets the steer handler cut the current text
    # segment at the steer boundary (see _steer_segment_cut). Same
    # lifecycle as _acp_client.
    slot._steer_segment_cut = functools.partial(_steer_segment_cut, turn)
    withheld_pin = await _settle_session_model(
        slot,
        client,
        provider_name,
        message,
        is_new=is_new,
        resumed=resumed,
        _is_refusal_retry_turn=_is_refusal_retry_turn,
        _synthetic_recovery_turn=_synthetic_recovery_turn,
        crew_model=agent_model,
    )
    # Append-only the session's log (flag-gated, fail-soft). Written HERE rather
    # than at session acquisition, because `model` has to name what the session
    # RUNS on and that is not settled until the verdict above: a withheld pin is
    # kept on the slot, and an unpinned slot learns its served model, both after
    # the handle exists. This is still ahead of every turn entry, and the announce
    # decision itself is latched inside the emitter, so a retry after the header
    # landed cannot skip it.
    #
    # `_created_by` is `session_create`'s attribution, stamped on the slot at
    # mint -- so it is already settled here, at the child's FIRST turn. The
    # creator's session id is read from `_created_by_sid`, FROZEN at mint from
    # the live caller handle, NOT re-read live here: the creator slot can be
    # closed and replaced between mint and this first turn, and a replacement
    # is a distinct handle with its own session id, so a live read would cite
    # the replacement's crew log and corrupt this child's immutable
    # `session/opened` lineage with no recovery. The creator's crew log is keyed
    # by that frozen id, so `parent_sid` is what lets a fold open the unit that
    # was live when this child was made; a creator whose handle had no session
    # at mint gets its slot recorded, sid absent.
    #
    # Both fields are used ONLY when `_lineage_minted` says this process
    # stamped them -- see `_crew_log_lineage` for why a restored `_created_by`
    # must never be promoted to gateway-authored lineage.
    # The one await on this path, and it comes FIRST: everything from the class read
    # to the emit is recorded as one moment, so nothing in that span may yield. A
    # yield after the class read would let a channel binding commit unseen; one
    # after the predecessor take would lose the predecessor on a cancel.
    _creator_key, _creator_sid = _crew_log_lineage(slot)
    if not _creator_key:
        await _crew_log_seed_tree(slot)
    # Read HERE rather than at mint, because these are facts about the session
    # this log is being opened for and they are recorded as of this moment. A class
    # the session acquires LATER reaches the log through its own record: every
    # surface that commits a change routes through ``note_crew_log_class``, so this
    # entry states the opening class and the moves are stated as they happen. What
    # this read still owes is the BEGINNING -- the value a later move is a move
    # from.
    _class_memory, _class_app, _class_channel = _crew_log_class(state, slot)
    # Read-and-clear, ONE handover: the sid and the reason it may be empty are one
    # statement about this slot's predecessor, so they cannot be taken apart.
    #
    # The replay marker is asked HERE and not at the latch, because only here is
    # there a session to ask: the latch runs before allocation, and a missing
    # session reads as "no replay owed", which is exactly the cold start where the
    # mapping is most likely to be holding the older generation. One dict lookup,
    # no disk. It bears only on an id the mapping supplied.
    _crew_log_replay_owed = state.sessions.provider_switch_replay_pending(session_key) is True
    _crew_log_edge = slot.take_crew_log_previous(
        now_writing=_crew_log_sid, replay_pending=_crew_log_replay_owed
    )
    if not _creator_key:
        # After the take, because the walk starts from the log this one supersedes:
        # the predecessor named here is exactly the one this entry will cite.
        _creator_key = _crew_log_inherited_parent(slot, _crew_log_edge.sid)
    crew_log_emit.on_session_opened(
        _crew_log_sid,
        agent=slot.agent or "",
        slot=slot.key,
        model=_crew_log_model(slot),
        # What the allocation that produced this live session ASKED for.
        # A pre-warmed session can survive a config change before its first
        # turn, so the turn's freshly resolved request is not evidence about
        # that allocation. Unknown allocation provenance stays absent.
        model_requested=slot._session_requested_model or "",
        cwd=_turn_project,
        resumed=bool(resumed),
        parent_slot=_creator_key,
        parent_sid=_creator_sid,
        # Resolved from the SAME creator this entry cites, after both sources
        # for it are settled, so the depth and the `parent` cannot describe
        # two different edges. One in-memory read of the tree the fold already
        # holds, never a disk read, and it answers `None` -- no key written --
        # whenever the chain above this session cannot be established.
        depth=_crew_log_tree_depth(slot, _creator_key),
        memory=_class_memory,
        app=_class_app,
        channel=_class_channel,
        workspace=_crew_log_workspace(slot),
        # Read-and-clear: the latch is owed to exactly one `session/opened`,
        # and the emitter alone decides whether it becomes an edge -- it
        # writes one only on a CREATE naming a different store, so handing
        # the value over on a re-attach costs nothing and leaving it behind
        # would make the slot's next store cite this store's predecessor
        # instead of this store. `now_writing` records which store the slot
        # is on while THIS process lives, which is what the slot's next
        # allocation names before the store can answer: the create below is
        # queued to the writer thread, so until that job runs there is no
        # unit for a reader to find.
        previous_sid=_crew_log_edge.sid,
        previous_undecided=_crew_log_edge.undecided,
    )
    agent_label = kiro_agent or slot.agent or "default"
    # The label states what the session RUNS on, so a withheld pin reports the
    # effective model rather than `slot.model` — the pin is kept, so reading it
    # here would print the withheld model on the activity line directly beside
    # the notice card explaining that it is not what is running.
    # An inheriting slot keeps `slot.model` empty, so it names the model the
    # session serves, which differs from the inherited default when startup
    # withheld that default; the default is the fallback when nothing was read.
    model_label = (
        "auto" if withheld_pin else (slot.model or slot.served_model or default_model or "auto")
    )
    # `spawned` marks the frames where a session was actually (re)started, so
    # consumers can act on a real session boundary. The frame itself is also
    # emitted on warm turns, where nothing was spawned and the advertised
    # model list cannot have changed.
    spawned = bool(is_new or resumed)
    if resumed:
        state.broadcast_ws(
            "activity_event",
            {
                "slot": slot.key,
                "kind": "session",
                "spawned": spawned,
                "text": f"Session resumed · {agent_label} · {model_label}",
            },
        )
    else:
        state.broadcast_ws(
            "activity_event",
            {
                "slot": slot.key,
                "kind": "session",
                "spawned": spawned,
                "text": f"Session created · {agent_label} · {model_label}",
            },
        )

    # Propagate trust/YOLO to session so subagents inherit auto-approve.
    # A scoped grant is excluded on purpose — see _persistable_session_policy.
    # Assigned unconditionally (not only when granting) so a turn that starts
    # after a grant went away clears any policy an earlier turn stored.
    state.sessions.set_approval_policy(
        session_key, _persistable_session_policy(slot, state.is_yolo_active())
    )

    # Drain MCP OAuth requests captured during session init. kiro-cli
    # buffers `_kiro.dev/mcp/oauth_request` notifications during MCP
    # server bring-up; the AcpClient collected them into
    # `pending_oauth_requests`. Every one is emitted; the ones a
    # Connections card owns are tagged so the render layer can avoid
    # repeating a prompt that card already shows.
    try:
        await _drain_session_init_oauth_requests(state, slot, client)
    except Exception:  # pragma: no cover — never let UI surfacing kill chat init
        logger.warning("Failed to surface pending MCP OAuth requests", exc_info=True)

    # Publish what this session's servers actually reported while starting.
    # Same duck-typed reach as the OAuth drain above; a report is never worth
    # a failed session init, so it is best-effort.
    try:
        _publish_session_mcp_report(state, slot, client)
    except Exception:  # pragma: no cover
        logger.warning("Failed to publish the session MCP report", exc_info=True)

    # Publish this turn's session identity so managed MCP tools resolve
    # X-Session-Key; one shared writer lives in messaging.identity.
    await publish_turn_identity(state.sessions, session_key)

    # ── @prompt expansion: resolve @name to SOP/prompt content ──
    # Captured BEFORE any expansion: `@prompt` replaces `message` and
    # `$skill` appends to it, so len(message) at classification time no
    # longer reflects what the user actually typed. See
    # attributable_user_chars. Transform-side corrections (marker
    # neutralization, the multibyte fold, a rewriting hook) are NOT applied
    # here — build_message maps these bounds to their final position itself.
    user_typed_message = message
    turn.user_typed_message = user_typed_message
    user_typed_len = len(user_typed_message)
    turn.user_typed_len = user_typed_len
    # The crew log turn ordinal is resolved HERE, above the @prompt expansion
    # gate, rather than only at the emit block in _turn_dispatch_gates. A blocked or oversized
    # expansion returns before that block, so computing it there left a refused
    # expansion with no crew log entry at all -- neither the accepted input nor
    # the refusal. Resolving it now lets the refusing branch record the same
    # pair the dispatch gates already do: the ``message/received`` for the
    # accepted input and a ``turn/refused`` naming the reason. The accepted
    # (``ok``) path can append one or more durable ``"system"`` rows after this
    # point -- the ``_surface_prompt_chip`` chip and the ``_expand_dollar_skills``
    # skill-load row, both of which ``durable_row_count`` counts -- so a fresh
    # count at the emit block could read higher by an amount this site cannot
    # predict. That is exactly why the ordinal is resolved ONCE here and both
    # branches reuse this single pre-expansion value rather than recomputing it;
    # the value is NOT a fixed offset from the emit-block count. Turn ordinals
    # still cannot collide, because each turn first appends its own durable
    # ``user`` row (the ``message/received``) before the runner is re-entered,
    # so every turn starts from a strictly higher base.
    # `durable_row_count` is the SHARED counting rule every
    # site that sets or advances that base uses, so the ordinal cannot disagree
    # with the base about which rows are durable.
    _crew_log_turn_no = int(getattr(slot, "_disk_older_durable_count", 0) or 0) + durable_row_count(
        slot.messages
    )
    turn._crew_log_turn_no = _crew_log_turn_no
    # A quick prompt is a REPLACING expansion, the same class as @prompt: the
    # instruction the model receives is injected content, not the user's typing,
    # so none of it is attributable to them. This flag drives the FALLBACK
    # attribution path (used when the authoritative span cannot be re-derived
    # after post-assembly prefixes). It must NOT also shorten the span handed to
    # build_message -- that span is how the matcher FINDS the token, and zeroing
    # it stops the expansion entirely. `user_text_span` keeps the two apart.
    _is_quick_prompt = first_word.lower() in QUICK_PROMPTS
    turn._is_quick_prompt = _is_quick_prompt
    prompt_expanded = _is_quick_prompt
    turn.prompt_expanded = prompt_expanded
    if message.startswith("@") and not is_slash and _prompt_depth < 1:
        original = message
        # Off the loop, for the same reason the `$skill` expansion in _turn_dispatch_prep is:
        # this resolves and READS files — the project's prompt directory is
        # not the gateway's own and may be network-backed, and its local half
        # is uncacheable, so it cannot be amortized away. `await` keeps the
        # ordering identical to the inline call: nothing after this line runs
        # until the expansion (and the chip it appends) is complete.
        message, _status = await _expand_prompt_mention_off_loop(
            message, state, slot, project=_turn_project
        )
        turn._status = _status
        turn.message = message
        if _status == "ok":
            prompt_expanded = True
            turn.prompt_expanded = prompt_expanded
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name="prompt_expansion",
                tool_kind="prompt",
                outcome="ok",
                metadata={"mention": original.split()[0], "slot": slot.key},
            )
        elif _status in ("blocked", "too_large"):
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name="prompt_expansion",
                tool_kind="prompt",
                outcome=_status,
                metadata={"mention": original.split()[0], "slot": slot.key},
            )
            label = (
                "sensitive path"
                if _status == "blocked"
                else f"size limit ({MAX_PROMPT_BYTES // 1000}KB)"
            )
            slot.append("system", f"🔒 Prompt blocked — {label}.", "msg msg-info")
            state.push_slots_update()
            # Append-only the session's log (flag-gated, fail-soft). This gate
            # returns before the normal emit block in _turn_dispatch_gates, so without
            # these two calls a refused expansion would leave the accepted turn with no entry at
            # all. Record the accepted input and then a ``turn/refused`` naming
            # the reason -- the same pair every dispatch gate in _turn_dispatch_gates writes for
            # ``not_authorized`` and its siblings. The composed request does not
            # exist yet here, so ``request/configured`` and ``context/composed``
            # are deliberately NOT written: the contract on a refusal is the
            # accepted input plus the refusal, not facts derived from a request
            # that was never assembled.
            crew_log_emit.on_message_received(
                _crew_log_sid,
                _crew_log_turn_no,
                role="user",
                text=user_typed_message,
                source=telemetry_channel_of(session_key),
                attachments=_attachments,
            )
            crew_log_emit.on_turn_refused(
                _crew_log_sid,
                _crew_log_turn_no,
                _status,
                turn._crew_log_actor,
                depth=_prompt_depth,
            )
            return _Flow.END
        elif _status == "not_found":
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name="prompt_expansion",
                tool_kind="prompt",
                outcome="not_found",
                metadata={"mention": original.split()[0], "slot": slot.key},
            )

    _request_prefix_context = ""
    turn._request_prefix_context = _request_prefix_context
    return None
