"""The agent switch: ``POST /api/chat/slots/{slot}/agent``, its request-free core
``switch_slot_agent``, and the model-binding reset a switch runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NamedTuple

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _TEARDOWN_INCOMPLETE_WARNING,
        SESSION_SWITCH_KIND,
        DashboardState,
        KiroCrewConfig,
        LLMProvider,
        _app_cancel_denied,
        _ChatSlot,
        _CommitToken,
        _default_template_stamp,
        _emit_agent_assignment,
        _has_conversation,
        _history_key_for,
        _record_explicit_agent_selection,
        _reset_slot_session_or_warn,
        _slot_replaced_while_queued,
        _slot_switch_session_lock,
        _subagents_attached_response,
        _switch_target_busy,
        _workspace_name_for_dir,
        cached_project_agent_names,
        default_project_dir,
        default_template,
        deny_app_slot_access,
        drained_to_thread,
        effective_session_key,
        is_registered_agent_name,
        load_window_off_loop,
        logger,
        members_mod,
        person_priority,
        pin_private_agent_store,
        read_bounded_json,
        relay_archive_refusal,
        release_prewarmed_session,
        resolve_agent_bindings,
        resolve_folder_project_dir_off_loop,
        restore_agent_selection,
        schedule_eager_spawn,
        sel,
        session_agent_selection_name,
        session_switch_notice,
        slot_not_found,
        switch_announcement,
        warm_project_agent_names,
    )


async def _append_session_switch_notice(slot: _ChatSlot, switch: str, value: str) -> None:
    """Leave the boundary row a committed, session-resetting switch owes the transcript.

    Called by the agent, model, bulk-model, reasoning-effort and workspace
    switches at their commit point, still inside the slot and session locks,
    so a same-value pick (answered earlier under the same locks) and a refused
    or rolled-back switch append nothing. Only a conversation that has started
    gets the row (:func:`_has_conversation`): a pick made on an empty chat
    before its first message is no boundary, and a notice there would be the
    chat's first row. ``append`` broadcasts the row itself.

    A restored row that was never opened still has its window on disk, and the
    bulk route is not a per-slot route that loads it first, so the window is
    loaded off the loop here before ``messages`` is read. A failed load leaves
    the window pending and omits the notice, without failing the committed switch.
    """
    try:
        await load_window_off_loop(slot)
    except Exception:
        logger.warning(
            "Skipping session-switch notice for %s: transcript window could not be loaded",
            slot.key,
            exc_info=True,
        )
        return
    if not _has_conversation(slot):
        return
    # Truncation can remove the syntax that makes a credential recognizable.
    from kiro_crew.security import redact

    value = redact(value)
    text = session_switch_notice(switch, value)
    from kiro_crew.security import redact

    text = redact(text)
    slot.append("assistant", text, "msg msg-a", meta={"kind": SESSION_SWITCH_KIND})


def clear_slot_model_binding(slot: Any) -> None:
    """Reset the fallback walk; the pin itself is kept.

    The walk belongs to the old agent's provider and cannot resume on its
    replacement. The stored pin is scoped on read and stays unchanged.

    Use a fresh identity token so rollback restores the prior walk only while
    this reset still owns it; a later reset must remain distinguishable.
    """
    slot._fallback_candidate_idx = 0
    slot._fallback_walked = []
    slot._active_fallback_model = _CommitToken("")
    slot._fallback_primary_model = ""
    slot._fallback_slot_model = ""
    slot._fallback_pick_gen = 0


class _MemberMemoryRequiresNewConversation(ValueError):
    """A member pick that would rebind an existing conversation's memory.

    Subclasses ValueError so every existing catch still treats it as the
    selection failure it is; named so the handler can answer it as the
    conversation-boundary refusal it means, instead of wrapping it in the
    store-unavailable 503 the generic except produces.
    """


class SlotAgentSwitchCaller(NamedTuple):
    """Who asked :func:`switch_slot_agent` to switch: an HTTP request, or a turn.

    The HTTP route passes its request (:meth:`for_request`); the chat runner's
    in-turn ``/agent <name>`` command passes :meth:`for_turn`. Everything the
    switch reads about its caller is here, so the two doors run ONE
    transaction instead of two copies that drift.
    """

    #: The app the caller acts for ("" = the dashboard).
    app: str
    #: Peer address, for the audit trail.
    remote: str
    #: Whether the dashboard owner asked. A turn's command never claims it: its
    #: text can come from any allowed channel user, so it gets the permitted-user
    #: rules (templates and V1 stores, never a V2 member store).
    owner: bool
    #: The HTTP request, when there is one.
    request: web.Request | None = None
    #: The switch runs inside the slot's own turn (see ``_switch_target_busy``).
    own_turn: bool = False
    #: Append the session-switch boundary row on commit. Off for a caller that
    #: writes its own ``/agent`` announcement line, so the switch is marked once.
    notice: bool = True

    @classmethod
    def for_request(cls, request: web.Request) -> SlotAgentSwitchCaller:
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        return cls(
            app=str(request.get("app", "") or ""),
            remote=request.remote or "",
            owner=is_owner_dashboard_request(request),
            request=request,
        )

    @classmethod
    def for_turn(cls, slot: _ChatSlot) -> SlotAgentSwitchCaller:
        # The slot's own app scope: an app's turn is held to what that app's
        # own request could do, never widened to the dashboard's.
        # The in-turn command appends its own announcement line, so no notice.
        return cls(app=slot._app or "", remote="", owner=False, own_turn=True, notice=False)

    @property
    def scope(self) -> web.Request | Mapping[str, Any]:
        """What the app-isolation helpers read (they take ``request.get("app")``)."""
        return self.request if self.request is not None else {"app": self.app}

    async def require_owner(self, operation: str) -> web.Response | None:
        if self.request is not None:
            from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

            return await require_owner_dashboard_request(self.request, operation)
        if self.owner:
            return None
        sel().log_api_access(
            caller=self.app or "turn",
            operation=operation,
            outcome="denied",
            source="dashboard",
            resources="non_owner_block",
        )
        return web.json_response(
            {"error": "owner authorization required", "code": "owner_only"}, status=403
        )


async def api_chat_slot_agent(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/agent — set agent for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_agent")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # `announce`: the composer's `/agent <name>` asks for the transcript line
    # the in-turn command appends; the picker sends no flag and appends none.
    announce = body.get("announce", False)
    if not isinstance(announce, bool):
        return web.json_response(
            {"error": "announce must be a boolean", "code": "invalid_announce"}, status=400
        )
    resp = await switch_slot_agent(
        state,
        slot,
        name,
        body.get("agent", ""),
        body.get("agent_kind", ""),
        # The announce line below marks the switch itself; the picker gets the
        # boundary notice instead.
        SlotAgentSwitchCaller.for_request(request)._replace(notice=not announce),
    )
    if announce:
        ok, outcome = agent_switch_outcome(resp)
        if ok:
            text = switch_announcement(
                str(outcome.get("agent") or slot.agent), str(outcome.get("warning") or "")
            )
            from kiro_crew.security import redact

            text = redact(text)
            slot.append("assistant", text, "msg msg-a")
            state.push_slots_update()
    return resp


def agent_switch_outcome(resp: web.Response) -> tuple[bool, dict[str, Any]]:
    """Whether a :func:`switch_slot_agent` response committed, and its JSON body.

    The body is ``{}`` when it is not a JSON object, so a caller reads
    ``error`` / ``agent`` / ``warning`` from it without its own guards.
    """
    try:
        body = json.loads(resp.text or "{}")
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    return resp.status == 200 and bool(body.get("ok")), body


async def switch_slot_agent(
    state: DashboardState,
    slot: _ChatSlot,
    name: str,
    agent_name: Any,
    agent_kind: Any,
    caller: SlotAgentSwitchCaller,
) -> web.Response:
    """Switch *slot* (registered as *name*) to *agent_name*: resolve, reset, commit.

    The transaction behind ``POST /api/chat/slots/{slot}/agent`` and the
    in-turn ``/agent <name>`` command. Answers the route's own responses, so a
    non-HTTP caller reads ``status`` and the JSON ``error``/``code`` body.
    """
    if slot.mode == members_mod.DM_SLOT_MODE and not members_mod.is_dispatchable_member_name(
        slot.agent
    ):
        sel().log_api_access(
            caller=caller.remote,
            operation="chat.slot_agent",
            outcome="denied",
            source="member_pin",
            resources=f"slot={slot.key}",
            error="stored member pin is not dispatchable",
        )
        return web.json_response(
            {
                "error": "this thread's crew name cannot be dispatched",
                "code": "member_pin_mismatch",
            },
            status=409,
        )
    member_pin_match = members_mod.member_pin_matches(slot.mode, slot.agent, agent_name)
    if not isinstance(agent_name, str) or (
        agent_name
        and not is_registered_agent_name(agent_name)
        and not member_pin_match
        # Same widening as the send guard: a configured free-form member (the
        # catalog lists it, the agent cycle sends its bare name) is admissible on
        # an ordinary slot; anything else off-grammar is refused.
        and not await asyncio.to_thread(members_mod.is_configured_dispatchable_member, agent_name)
    ):
        return web.json_response({"error": "invalid agent name"}, status=400)
    # Same contract as the create route: an optional namespace for the name.
    if agent_kind not in ("", "member", "template"):
        return web.json_response(
            {"error": "invalid agent kind", "code": "invalid_agent_kind"}, status=400
        )
    stamped_default = not agent_name
    if stamped_default:
        # "Use the default" is the default TEMPLATE, stamped in its namespace
        # exactly as the create route stamps an agent-less slot -- so
        # the switch records a selection, and the slot never reads as the
        # `default` crewmate.
        try:
            agent_name = await asyncio.to_thread(
                lambda: _default_template_stamp(KiroCrewConfig.load())
            )
        except Exception:
            logger.warning("Failed to load config for the default template", exc_info=True)
            agent_name = default_template(KiroCrewConfig())
        agent_kind = "template"
    if slot.mode == "member" and (agent_name != slot.agent or agent_kind == "template"):
        # Member DM threads are pinned to their crew: refuse the switch before
        # any state is touched. A same-name "switch" stays allowed — it is a
        # session reset, not a re-bind — but only in the MEMBER namespace: the
        # same name picked as a template would run the shared template and
        # detach the thread from the member's memory, which is a re-bind by
        # another spelling. Audited like every other pin denial
        # (the send path's guard emits the same event), so a probe against the
        # pin is visible in the SEL trail.
        _emit_agent_assignment(slot.key, agent_name, outcome="denied_member_pin")
        return web.json_response(
            {"error": "member thread agent is pinned", "code": "member_thread_agent_pinned"},
            status=409,
        )
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal

    # The whole resolve -> reset -> commit section runs under the slot's
    # lock: the awaits yield the event loop, and an interleaved second switch
    # could otherwise observe (or write) intermediate state. TRANSACTIONAL
    # ordering: the new values are computed into locals, the session reset
    # runs FIRST, and the slot is mutated only after the reset succeeds — a
    # failed request provably changed nothing, the invariant the frontend's
    # slotSwitch failure-recovery relies on, with no rollback machinery to
    # race against concurrent writers (e.g. the in-turn set_project directive,
    # which does not take this lock).
    # Two locks, in the order documented at _slot_switch_session_lock:
    # slot._lock, then the session lock. An ExitStack because the session
    # lock's KEY is only known after the in-lock read below, and locking on
    # any earlier read could leave this holding the wrong session lock.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above (see _slot_replaced_while_queued):
        # ``name`` can be recreated for a different app while this request
        # queued, and every read of ``slot`` below would be of the stale one.
        if _slot_replaced_while_queued(state, slot, name, caller.scope, "chat.slot_agent"):
            return slot_not_found()
        # The session the switch resets — ``effective_session_key``, never
        # ``_history_key_for`` (see api_chat_slot_model): a channel- or
        # cron-born slot runs its turns under its linked key, and the
        # dashboard-prefixed spelling names a session that never existed —
        # the reset would "succeed" against nothing while the live process
        # kept the old agent. Resolved INSIDE the lock: the binding can land
        # while this request waits on it.
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot on this same session.
        # slot._lock is created per _ChatSlot and so is DISJOINT across
        # aliases. Keyed on the value resolved just above -- the same one the
        # probe and reset below use -- so the lock provably guards them even if
        # a binding landed while this request waited on slot._lock (see
        # _slot_switch_session_lock).
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        # Second lock-acquisition await, second re-check: a same-name
        # recreate lands during this wait just as easily as during the first.
        if _slot_replaced_while_queued(state, slot, name, caller.scope, "chat.slot_agent"):
            return slot_not_found()
        # App isolation on the SESSION, not just the slot (the cancel routes'
        # policy): slot ownership does not imply ownership of a linked
        # channel session, so an app caller may not switch the agent a
        # channel thread runs on. Denied as an indistinguishable 404.
        denied = _app_cancel_denied(caller.scope, slot, "chat.slot_agent", session_key)
        if denied is not None:
            return denied
        owner_request = caller.owner
        if not owner_request:
            # Permitted chat users keep template and legacy V1 choices, but
            # cannot change a member assignment through aggregate controls. Refuse a V2 choice
            # before any slot, provider or history mutation.
            try:
                choice_cfg = await asyncio.to_thread(KiroCrewConfig.load)
                await warm_project_agent_names(
                    slot.project or None, operation="api_chat_slot_agent", source="dashboard"
                )
                choice = await asyncio.to_thread(
                    resolve_agent_bindings,
                    choice_cfg,
                    agent_name,
                    slot.project or None,
                    validate_memory_files=False,
                    selection_kind=agent_kind,
                )
            except Exception as exc:
                from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                return _store_unavailable_response(slot.memory_store, exc)
            choice_store = choice_cfg.memory_stores.get(choice.memory_store_name)
            if choice_store is not None and choice_store.memory_version == 2:
                denied = await caller.require_owner("chat.slot_agent")
                if denied is not None:
                    return denied
        # A name change, or the stamped default landing on the slot's own name:
        # the default template may carry a private member's name, and "use the
        # default" on that member's conversation is a namespace change that
        # would rebind it to Global memory, so the durable pin is checked
        # before anything is touched. An explicit same-name template pick is
        # the owner's stated choice and keeps its existing path.
        if agent_name != slot.agent or stamped_default:
            from kiro_crew.execution_context import read_session_execution

            try:
                prior_execution = await asyncio.to_thread(read_session_execution, session_key)
            except (OSError, ValueError):
                return web.json_response(
                    {
                        "error": "This conversation's memory binding could not be read. "
                        "Start a new conversation to choose a different member.",
                        "code": "member_binding_unavailable",
                    },
                    status=503,
                )
            if prior_execution is not None and prior_execution.member_id is not None:
                # Resetting the provider keeps this conversation's member identity.
                # Refuse before changing the agent, its derived fields or history.
                return web.json_response(
                    {
                        "error": "This conversation belongs to its original member. "
                        "Start a new conversation to choose a different member.",
                        "code": "member_session_pinned",
                    },
                    status=409,
                )
        try:
            prior_selection = await asyncio.to_thread(session_agent_selection_name, session_key)
        except Exception as exc:
            from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

            return _store_unavailable_response(slot.memory_store, exc)
        # Never reset under an in-flight turn (the model handler's policy,
        # and the _cancel_target subtlety): a RUNNING turn owns a captured
        # identity because ``linked_session_key`` is mutable, so the key
        # resolved above may not be the turn's — tearing it down would kill
        # the wrong session (or the streaming turn itself). The three signals
        # and why each is needed live on _switch_target_busy. A 409 is
        # retryable once the turn completes. Checked BEFORE the commit below,
        # so nothing needs rolling back.
        busy_provider = state.sessions.get_provider(session_key)
        if _switch_target_busy(state, slot, session_key, busy_provider, own_turn=caller.own_turn):
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        # Rollback baseline for the session_rebound path below. The commit is
        # otherwise deliberately not rolled back on a failing reset (see the
        # teardown_incomplete comment), so this is the ONE case that unwinds.
        prior_agent = slot.agent
        prior_agent_kind = slot.agent_kind
        # Stored verbatim — never rewritten to whatever currently answers. See
        # the same reasoning in api_chat_slot_create.
        new_workspace = slot.workspace
        new_project = slot.project
        new_memory_store = slot.memory_store
        # Compare-and-set baseline, captured BEFORE the first await in this
        # section: the resolution warm-up and the session reset both yield
        # the event loop, and the project/workspace endpoints do not take
        # this lock — a user's explicit pick landing anywhere in that window
        # must win over this switch's DERIVED values (the reverse would
        # silently erase an action that happened after the agent pick).
        pre_await_workspace = slot.workspace
        pre_await_project = slot.project
        pre_await_memory_store = slot.memory_store

        # Resolve workspace from agent bindings. The response value is seeded
        # from the slot's CURRENT workspace, not a "default" literal: if
        # resolution below fails, the response still names this value, and
        # the acting tab writes it into its store — a fabricated
        # "default" would pin the chip to a workspace the slot does not hold
        # (the websocket rebroadcast corrects it only when the socket is up,
        # which is exactly when the optimistic write is load-bearing).
        workspace = slot.workspace or "default"
        assignment_resolved = False
        new_agent_kind = ""
        committed_agent: str | None = None
        committed_agent_kind: str | None = None
        try:
            cfg = KiroCrewConfig.load()
            # Resolve by the name being STORED, which is exactly the name dispatch
            # will resolve later (`chat_runner` -> resolve_agent_bindings(
            # slot.agent)). Looking it up as an alias first and taking THAT
            # alias's workspace disagrees with dispatch whenever the two differ:
            # a name that is merely some alias's `kiro_agent` target, or a
            # materialized app agent, dispatches with the DEFAULT bindings while
            # the slot records the alias's workspace. A materialized agent
            # matches no alias at all, which leaves the slot on the PREVIOUS
            # agent's project.
            # Resolve WITH the captured project scope (warmed off-loop first) so a
            # project agent counts as resolved rather than falling back.
            await warm_project_agent_names(
                pre_await_project or None, operation="api_chat_slot_agent", source="dashboard"
            )
            if owner_request:
                bindings = await asyncio.to_thread(
                    resolve_agent_bindings,
                    cfg,
                    agent_name,
                    pre_await_project or None,
                    validate_memory_files=False,
                    selection_kind=agent_kind,
                )
            else:
                bindings = await asyncio.to_thread(
                    resolve_agent_bindings,
                    cfg,
                    agent_name,
                    pre_await_project or None,
                    selection_kind=choice.selection_kind,
                    validate_memory_files=False,
                )
                selected_store = cfg.memory_stores.get(bindings.memory_store_name)
                if selected_store is not None and selected_store.memory_version == 2:
                    # The member may have moved to V2 during resolution. No
                    # derived fields, reset or history write has committed yet.
                    denied = await caller.require_owner("chat.slot_agent")
                    if denied is not None:
                        return denied
            assignment_resolved = bindings.requested_resolved
            new_agent_kind = bindings.selection_kind if assignment_resolved else ""
            ws_name = _workspace_name_for_dir(cfg, bindings.workspace_dir)
            new_workspace = ws_name
            workspace = ws_name
            new_memory_store = bindings.memory_store_name
            # A project-scope agent exists only inside slot.project: kiro-cli
            # resolves --agent against $PWD/.kiro/agents, so resetting the
            # project here would make the very agent just selected unresolvable
            # on the next turn (slot advertises it, default answers — the
            # silent substitution this resolution exists to remove). Aliases keep the
            # reset: their project comes from their own workspace bindings.
            is_project_agent = agent_name not in cfg.agents and agent_name in (
                cached_project_agent_names(slot.project or None) or frozenset()
            )
            if not is_project_agent:
                # A slot filed into a project-linked folder keeps that
                # folder's directory rather than the new agent's workspace
                # default: the link is an explicit choice about where this
                # chat's tools run, and `api_chat_slot_create` already
                # prefers it over the workspace default — an agent pick must
                # not silently undo it. Resolved through the SAME helper as
                # the create path, which walks the parent_id chain (so a
                # project inherited from an ancestor folder counts too) and
                # RE-VALIDATES the stored path instead of trusting
                # folders.json: a directory recorded there can since have
                # been moved, or become sensitive, and this value becomes
                # the agent subprocess's cwd. Off the loop, as the helper's
                # docstring requires (realpath/isdir priming).
                folder_project = ""
                if slot.folder_id:
                    try:
                        # REVALIDATED against the id the snapshot was taken
                        # for. `read_folders` and the off-loop resolve are two
                        # awaits, and a concurrent assignment can file this
                        # slot into a folder CREATED after the snapshot -- whose
                        # id is then absent from it, resolving to nothing and
                        # committing the workspace default as this chat's
                        # directory. One retry is enough for an assignment that
                        # has already landed; a slot being reassigned faster
                        # than that has no stable answer to commit, so it keeps
                        # the documented fall-through.
                        folder_error: str | None = ""
                        for _ in range(2):
                            folder_id_at_read = slot.folder_id
                            if not folder_id_at_read:
                                break
                            folder_snapshot = await state.read_folders(
                                lambda folders: [dict(folder) for folder in folders]
                            )
                            folder_project, folder_error = (
                                await resolve_folder_project_dir_off_loop(
                                    folder_snapshot, folder_id_at_read
                                )
                            )
                            if slot.folder_id == folder_id_at_read:
                                break
                            # Reassigned mid-resolve: what came back describes a
                            # folder other than the one this slot now holds, so
                            # it is discarded rather than committed.
                            folder_project, folder_error = "", ""
                        if folder_error:
                            # Deliberately NOT the create path's 400: that
                            # validator also rejects a directory that no
                            # longer exists, so failing the request here
                            # would make the agent permanently unswitchable
                            # for any folder whose project was moved or
                            # deleted. Fall through to the workspace default.
                            logger.warning(
                                "Slot %s folder project unusable (%s); "
                                "falling back to the workspace default",
                                name,
                                folder_error,
                            )
                            folder_project = ""
                    except Exception:
                        # Same fall-through for an unreadable or corrupt
                        # folder store: letting it reach the outer handler
                        # would leave the workspace advanced with the
                        # project stale — a half-applied switch.
                        logger.warning(
                            "Failed to resolve folder project for slot %s", name, exc_info=True
                        )
                        folder_project = ""
                if folder_project:
                    new_project = folder_project
                elif ws_name not in ("default", cfg.default_workspace):
                    # Only a workspace the agent RESOLVED TO DELIBERATELY may
                    # retarget the project. Two names fail that test and both
                    # have to be excluded:
                    #
                    # * the literal "default" — `_workspace_name_for_dir`
                    #   answers it both for an agent bound to no workspace and
                    #   for one naming a workspace absent from the config;
                    # * `cfg.default_workspace` — on an install that renames
                    #   its default, the resolver falls back to that NAME, so
                    #   the same "no deliberate choice" case arrives spelled
                    #   differently and a literal-only gate lets it through.
                    #
                    # Either way the agent expressed no workspace preference,
                    # and retargeting on a fallback discards the directory the
                    # user chose and runs the next turn's tools elsewhere.
                    new_project = default_project_dir(workspace)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to resolve agent bindings for %r", agent_name, exc_info=True)

        if agent_kind and not assignment_resolved:
            # A stated namespace never falls back to whoever answers by default.
            return web.json_response(
                {
                    "error": "the selected agent choice is not available",
                    "code": "agent_choice_unavailable",
                },
                status=409,
            )

        if not assignment_resolved and prior_selection is not None:
            # A failed lookup cannot commit a name while retaining a different
            # protected selection. Leave the established conversation usable.
            from kiro_crew.dashboard.handlers.memory import _store_unavailable_response
            from kiro_crew.memory_stores import UnknownMemoryStore

            return _store_unavailable_response(
                slot.memory_store, UnknownMemoryStore("Conversation agent selection is unavailable")
            )

        # Publish the selected name and its resolved namespace together in one
        # no-await section. Resolution may yield while discovering project
        # agents, so publishing the name before it finishes lets a sender see a
        # new name beside the previous namespace. A sender during resolution
        # instead keeps using the complete old selection; the busy re-probe and
        # reset below either refuse that overlap or retire its old session.
        #
        # The two tokens also form one rollback ownership claim. Any concurrent
        # writer replaces at least the field it owns, including a same-value
        # write, so rollback never erases that later selection.
        #
        # Compare-and-set against the pre-resolution baseline BEFORE the commit,
        # the same guard the derived fields below apply to their pre_await_*
        # snapshots. The resolution awaits above yield the event loop, and the
        # unlocked `/v1/chat` openai_compat path (and the in-turn directive
        # writers) can set `slot.agent`/`slot.agent_kind` and dispatch a turn in
        # that window. Committing the tokens here would overwrite that accepted
        # selection, and because no await separates the commit from the
        # `slot.agent is not committed_agent` rebind check below, that check can
        # never see the overwrite — the busy re-probe would then roll back to
        # `prior_agent`, erasing a binding a running turn already chose. Refuse
        # instead, leaving the concurrent selection in place (the pre-move
        # behaviour, which committed before resolving and so returned 409 here).
        if slot.agent is not prior_agent or slot.agent_kind is not prior_agent_kind:
            return web.json_response(
                {"error": "slot changed during agent resolution", "code": "session_rebound"},
                status=409,
            )
        slot.agent = _CommitToken(agent_name)
        slot.agent_kind = _CommitToken(new_agent_kind)
        committed_agent = slot.agent
        committed_agent_kind = slot.agent_kind
        # The resolved namespace decides, not the request's spelling alone: a
        # name-only pick can still change member/template (the member lookup
        # answered by the shared template, or the reverse), which is a rebind
        # even though the name stood still. Both commits see the resolved kind,
        # so both the decision and the rollback below are made here.
        agent_changed = agent_name != prior_agent or (
            new_agent_kind != "" and prior_agent_kind != "" and new_agent_kind != prior_agent_kind
        )
        prior_fallback_candidate_idx = slot._fallback_candidate_idx
        prior_fallback_walked = slot._fallback_walked
        prior_active_fallback_model = slot._active_fallback_model
        prior_fallback_primary_model = slot._fallback_primary_model
        prior_fallback_slot_model = slot._fallback_slot_model
        prior_fallback_pick_gen = slot._fallback_pick_gen
        committed_active_fallback_model: str | None = None
        if agent_changed:
            # Fallback recovery is tied to the provider that serves this agent,
            # so a stale walk must not resume on the replacement session. The
            # pin is deliberately kept (see clear_slot_model_binding).
            clear_slot_model_binding(slot)
            committed_active_fallback_model = slot._active_fallback_model

        # Derived fields commit BEFORE the reset too, compare-and-set against
        # the pre-await baseline: a send landing during the reset teardown
        # cold-starts the replacement session from the slot's CURRENT
        # bindings, so the full new binding TRIPLE must already be visible or
        # the new agent's session starts in the OLD project and its tools run
        # in the wrong repository. The write-side CAS still protects a
        # concurrent explicit pick that landed during the resolution awaits
        # above; the committed values are identity tokens so the ROLLBACK can
        # prove ownership — a value compare there would erase a concurrent
        # same-value write (the in-turn set_project directive can write the
        # very project this handler derived).
        committed_workspace: str | None = None
        committed_project: str | None = None
        committed_memory_store: str | None = None
        if slot.workspace == pre_await_workspace:
            slot.workspace = _CommitToken(new_workspace)
            committed_workspace = slot.workspace
        if slot.project == pre_await_project:
            slot.project = _CommitToken(new_project)
            committed_project = slot.project
        # The store is the THIRD field of that binding, and leaving it behind
        # splits the slot in half: the turn resolves its store fresh from the new
        # agent's bindings while the consolidator writes to the store recorded at
        # birth, so a switched slot READS the new agent's memory and WRITES the old
        # agent's. No error on either side. Same commit-token CAS as the two
        # above, so the rollback below unwinds the store with the binding it
        # belongs to rather than leaving the slot half-switched.
        if slot.memory_store == pre_await_memory_store:
            slot.memory_store = _CommitToken(new_memory_store)
            committed_memory_store = slot.memory_store

        # Reset session so the next message uses the new agent.
        logger.info(
            "Slot %s agent switched to %r, resetting session", name, agent_name or "kirocrew"
        )

        def _rollback_switch() -> None:
            """Unwind this request's commit — only the values still OURS.

            EVERY field is unwound on IDENTITY of its commit token, never
            value equality: unlocked writers (the in-turn /agent and
            set_project directives in chat_runner, members, openai_compat)
            can write the SAME text during this handler's awaits — the
            in-turn set_project directive can legitimately write the very
            project this handler derived — and a value compare-and-set would
            erase that successful concurrent write. Any write replaces the
            token object, so an identity match proves the field is still
            this commit's; a field this request never committed (the
            write-side CAS lost) has a None token and is never touched.

            ``slot.model`` is NOT part of the commit at all: the pin is kept by
            design, so a rollback leaves it alone either way. The fallback walk
            the commit reset is unwound here instead, on the same ownership
            test — a refused switch that keeps the reset has changed something
            while reporting failure.
            """
            if slot.agent is committed_agent and slot.agent_kind is committed_agent_kind:
                slot.agent = prior_agent
                slot.agent_kind = prior_agent_kind
                if (
                    committed_active_fallback_model is not None
                    and slot._active_fallback_model is committed_active_fallback_model
                ):
                    slot._fallback_candidate_idx = prior_fallback_candidate_idx
                    slot._fallback_walked = prior_fallback_walked
                    slot._active_fallback_model = prior_active_fallback_model
                    slot._fallback_primary_model = prior_fallback_primary_model
                    slot._fallback_slot_model = prior_fallback_slot_model
                    slot._fallback_pick_gen = prior_fallback_pick_gen
            if committed_workspace is not None and slot.workspace is committed_workspace:
                slot.workspace = pre_await_workspace
            if committed_project is not None and slot.project is committed_project:
                slot.project = pre_await_project
            if committed_memory_store is not None and slot.memory_store is committed_memory_store:
                slot.memory_store = pre_await_memory_store
            # Re-mark unconditionally: the periodic flush writes a slot's
            # metadata line only while _dirty is set, so without this a
            # rollback that follows a persisted provisional binding leaves
            # the rejected values on disk across a restart.
            slot._dirty = True

        if (
            state._slots.get(slot.key) is not slot
            or effective_session_key(slot) != session_key
            or slot.agent is not committed_agent
        ):
            _rollback_switch()
            return web.json_response(
                {"error": "slot changed during agent resolution", "code": "session_rebound"},
                status=409,
            )

        # Children guard, shared with reload/model: the reset tears down the
        # runtime attached sub-agents run on, so a parent that is idle but
        # still has children must refuse rather than discard their work.
        children_409 = await _subagents_attached_response(state, slot, session_key, "slot_agent")
        if children_409 is not None:
            _rollback_switch()
            return children_409
        # Last-instant re-probe in a NO-AWAIT window before the teardown (the
        # model template's rule at its own reset site): the pre-commit check
        # above is separated from this point by the resolution warm-up and
        # the children probe awaits, so a turn — a channel message on the
        # linked session, or a sibling alias cold-starting on it — may have
        # started since it ran. Same predicate as the pre-commit check
        # (_switch_target_busy), so the sibling window it closes there is
        # closed here too. Message dispatch does not take slot._lock, so this
        # fast path plus the atomic skip_if_busy decline below are what keep
        # the teardown off a streaming turn.
        recheck = state.sessions.get_provider(session_key)
        if _switch_target_busy(state, slot, session_key, recheck, own_turn=caller.own_turn):
            _rollback_switch()
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        teardown_incomplete = False
        reset_ok = True
        # The switch is COMMITTED already (slot.agent above), so a POST-POP
        # teardown raise is answered as a success with a degraded-teardown
        # warning — but only once the helper has verified the raise came
        # AFTER the session pop. A PRE-POP raise means the old session
        # survives on the old binding, so the helper propagates it and this
        # request rolls the commit back and answers 500 instead of a false
        # success. The helper resets with skip_if_busy=True, which keeps this
        # handler's decline ladder
        # below: SessionManager.reset evaluates busyness atomically with the
        # session pop, so a turn that slipped into the microsecond residue
        # after the re-check above is declined (reset_ok False) instead of
        # torn down mid-stream. The helper's None verdict marks the degraded
        # post-pop teardown; a bool verdict is the reset outcome the ladder
        # reads.
        try:
            reset_verdict = await _reset_slot_session_or_warn(
                state, slot, session_key, switch_kind="agent"
            )
        except Exception:
            # The identity probe proved the pop never happened: the old
            # session is still alive on the old binding, so the committed
            # values describe a switch that did not take — and the acting tab
            # keeps its OLD store value on the 500, so leaving them would
            # split server state from every client. Roll back the commit
            # (identity-scoped, so a concurrent explicit pick that landed
            # during the raising await keeps its win) and re-push so clients
            # and persisted state land on the rolled-back truth, then let the
            # raise escape as a 500.
            _rollback_switch()
            state.push_slots_update()
            raise
        if reset_verdict is None:
            teardown_incomplete = True
        else:
            reset_ok = reset_verdict
        if not reset_ok and not teardown_incomplete:
            # Disambiguate the decline FAIL-CLOSED, the workspace handler's
            # template: a live provider mid-turn → roll back and 409; a live
            # IDLE session that declined (its turn ended before this re-read)
            # is always safe to tear down, so retry once; a second decline
            # means another turn is genuinely racing. No live provider means
            # there was nothing to tear down — the next message cold-starts
            # under the new binding, which is what the reset would arrange.
            busy_provider = state.sessions.get_provider(session_key)
            if isinstance(busy_provider, LLMProvider):
                if busy_provider.has_active_turn():
                    _rollback_switch()
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                    )
                # Retry through the same probing helper: a pre-pop raise on
                # the retry propagates (roll back + 500) exactly as the first
                # attempt, and a post-pop raise becomes the committed 200 +
                # warning. Left as a bare _reset_slot_session the retry would
                # answer a false success on a pre-pop raise, the divergence
                # the first-attempt guard prevents.
                try:
                    reset_verdict = await _reset_slot_session_or_warn(
                        state, slot, session_key, switch_kind="agent"
                    )
                except Exception:
                    _rollback_switch()
                    state.push_slots_update()
                    raise
                if reset_verdict is None:
                    teardown_incomplete = True
                else:
                    reset_ok = reset_verdict
                if (
                    not reset_ok
                    and not teardown_incomplete
                    and state.sessions.get_provider(session_key) is not None
                ):
                    _rollback_switch()
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                    )

        if effective_session_key(slot) != session_key:
            # The slot was bound to a different session while the resolution
            # warm-up or the reset awaited (a cron/workflow slot gets linked
            # when its first result is injected): the session this request
            # tore down is not the slot's any more, so the committed binding
            # would describe a session that never saw the switch. The
            # teardown itself was harmless (that session was idle and no
            # longer bound); roll back the commit and answer the same 409 the
            # model and workspace handlers use. Checked BEFORE the metadata
            # write below so a rolled-back agent is never persisted for
            # restart.
            _rollback_switch()
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )

        # Persist the new agent so the session resumes under the correct
        # agent after a gateway restart. INSIDE the lock: two racing switches
        # otherwise interleave their metadata writes, and a stalled earlier
        # write finishing last would restore the older agent on restart.
        # Deliberately ``_history_key_for``, NOT ``session_key``: this names
        # the slot's TRANSCRIPT (the .jsonl the restart scan reads), not the
        # live session the reset above addressed — the same history-vs-session
        # split ``_cancel_target`` documents.
        conversation_log = state.conversation_log if not slot.is_restricted else None
        if conversation_log:

            async def _rollback_history_selection() -> None:
                _rollback_switch()
                try:
                    await drained_to_thread(
                        conversation_log.update_metadata,  # type: ignore[union-attr]
                        _history_key_for(name),
                        {"agent": str(slot.agent), "agent_kind": slot.agent_kind},
                    )
                except Exception:
                    logger.warning(
                        "Failed to restore agent metadata for slot %s", name, exc_info=True
                    )
                finally:
                    state.push_slots_update()

            try:
                # update_metadata enters _locked (flock + os.close); those are
                # blocking-on-loop-prohibited, so offload to a worker thread rather
                # than run them on the event loop (a wedged peer must never freeze
                # chat/WS/heartbeat).
                await drained_to_thread(
                    conversation_log.update_metadata,
                    _history_key_for(name),
                    {"agent": agent_name, "agent_kind": new_agent_kind},
                )
            except asyncio.CancelledError:
                # Drain the writer before restoring history, and retain both
                # switch locks until restoration settles.
                await _rollback_history_selection()
                raise
            except Exception:
                logger.warning("Failed to persist agent for slot %s", name, exc_info=True)
                await _rollback_history_selection()
                return web.json_response(
                    {
                        "error": "Could not save the agent selection. Try again.",
                        "code": "history_unavailable",
                    },
                    status=503,
                )

        if effective_session_key(slot) != session_key:
            # A binding can land during the metadata await too — the rebound
            # guard above ran BEFORE that await, so it must be re-validated
            # after the last await inside the lock or a workflow binding
            # landing there gets a 200 while the linked session keeps the old
            # agent. Roll back the commit AND the metadata just persisted:
            # the 409 tells the caller nothing changed, so the transcript
            # metadata must agree. Restoring ``slot.agent`` (post-rollback)
            # rather than ``prior_agent`` is deliberate — if a concurrent
            # writer took ownership during the awaits, its value is the
            # truthful current one. The metadata is transcript-scoped and
            # binding-independent, so its restore needs no further re-check.
            _rollback_switch()
            if state.conversation_log and not slot.is_restricted:
                try:
                    await drained_to_thread(
                        state.conversation_log.update_metadata,
                        _history_key_for(name),
                        {"agent": str(slot.agent), "agent_kind": slot.agent_kind},
                    )
                except Exception:
                    logger.warning(
                        "Failed to restore agent metadata for slot %s", name, exc_info=True
                    )
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )

        # Every authorized choice must update both durable records. Only an
        # owner choice can also admit an unbound restored member.
        if slot.agent is committed_agent and assignment_resolved:
            selection_change = None
            selection_error = None

            async def _rollback_owner_selection() -> None:
                _rollback_switch()
                try:
                    await drained_to_thread(restore_agent_selection, session_key, selection_change)
                finally:
                    # The protected drain may re-raise cancellation. History
                    # must still settle before the slot/session locks release.
                    if state.conversation_log and not slot.is_restricted:
                        await drained_to_thread(
                            state.conversation_log.update_metadata,
                            _history_key_for(name),
                            {"agent": str(slot.agent), "agent_kind": slot.agent_kind},
                        )

            try:
                # Validate the old conversation BEFORE publishing its new
                # canonical identity; otherwise that publication would conceal
                # template history from the member-selection guard.
                selected_memory = cfg.memory_stores.get(bindings.memory_store_name)
                from kiro_crew.execution_context import read_session_execution

                initial_execution = await asyncio.to_thread(read_session_execution, session_key)
                changed_member = (
                    initial_execution is None
                    or initial_execution.member_id
                    != getattr(selected_memory, "owner_member_id", None)
                )
                if (
                    selected_memory is not None
                    and selected_memory.memory_version == 2
                    and changed_member
                ):
                    if slot.messages:
                        raise _MemberMemoryRequiresNewConversation(
                            "Open a new conversation to choose member memory."
                        )
                    await release_prewarmed_session(state, session_key, agent_name, cfg)
                    await pin_private_agent_store(
                        state,
                        session_key,
                        agent_name,
                        cfg,
                        memory_mode=slot.memory_mode,
                        validate_only=True,
                    )
                    if (
                        state._slots.get(slot.key) is not slot
                        or slot.agent is not committed_agent
                        or effective_session_key(slot) != session_key
                        or slot.messages
                    ):
                        raise ValueError("The conversation changed during member selection.")
                selection_change = await _record_explicit_agent_selection(
                    session_key,
                    agent_name,
                    bindings,
                    config=cfg,
                    memory_mode=slot.memory_mode,
                    app=slot._app or "",
                )
            except asyncio.CancelledError:
                await _rollback_owner_selection()
                raise
            except Exception as exc:
                selection_error = exc
            if selection_error is not None or (
                state._slots.get(slot.key) is not slot
                or slot.agent is not committed_agent
                or effective_session_key(slot) != session_key
            ):
                await _rollback_owner_selection()
                if selection_error is not None:
                    if isinstance(selection_error, _MemberMemoryRequiresNewConversation):
                        # Not a store fault: this conversation already has history,
                        # so its memory binding cannot move to a member's private
                        # store in place. Name the boundary the user can act on.
                        return web.json_response(
                            {
                                "error": str(selection_error),
                                "code": "member_memory_requires_new_conversation",
                            },
                            status=409,
                        )
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    return _store_unavailable_response(slot.memory_store, selection_error)
                return web.json_response(
                    {
                        "error": "Could not save the agent selection. Try again.",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            # A non-owner choice preserves the recorded session assignment.
            slot._memory_assignment_from_history = not owner_request

        owner_pick = slot.agent is committed_agent and assignment_resolved and caller.owner
        if owner_pick:
            slot._memory_assignment_from_history = False

        # Menu grants are for an EMPTY plain dashboard chat only. Channel,
        # cron and workflow alias tabs are excluded: an injector can set their
        # linked_session_key without the slot lock, and they carry native
        # context the pin helper refuses. With this gate, pin_key is the slot's
        # own transcript key, which nothing rebinds. The helper verifies the
        # transcript is empty before capturing member context; V1 picks pass
        # through without a grant.
        if (
            owner_pick
            and agent_name
            # A shared-template pick has no member memory to grant, even when
            # a member of the same name exists.
            and agent_kind != "template"
            and not slot.messages
            and not slot.linked_session_key
            and not slot.channel_origin
        ):
            pin_key = session_key

            async def _unwind_pin_failure() -> None:
                # A failed grant must restore the protected selection as well
                # as the slot and transcript, before either switch lock releases.
                try:
                    await _rollback_owner_selection()
                finally:
                    state.push_slots_update()

            if (
                state._slots.get(slot.key) is slot
                and slot.agent is committed_agent
                and effective_session_key(slot) == pin_key
                and not slot.messages
            ):
                try:
                    # Off the loop: the create path loads it the same way, and
                    # the in-handler load above is not guaranteed to have run.
                    pin_cfg = await asyncio.to_thread(KiroCrewConfig.load)
                    # The reset above tore this slot's session down but kept its
                    # resume pointer, and a new chat is pre-warmed while it is
                    # still on the default agent. Drop that pointer for a
                    # private pick, or the pin reads it as V1 context and
                    # refuses a chat with no messages in it. Re-checked after
                    # the awaits below for the same reason the pin is.
                    await release_prewarmed_session(state, pin_key, agent_name, pin_cfg)
                    if (
                        state._slots.get(slot.key) is not slot
                        or slot.agent is not committed_agent
                        or effective_session_key(slot) != pin_key
                        or slot.messages
                    ):
                        await _unwind_pin_failure()
                        return web.json_response(
                            {
                                "error": "slot changed during member assignment",
                                "code": "session_rebound",
                            },
                            status=409,
                        )
                    assigned_store = await pin_private_agent_store(
                        state, pin_key, agent_name, pin_cfg, memory_mode=slot.memory_mode
                    )
                except Exception as exc:
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    await _unwind_pin_failure()
                    return _store_unavailable_response(slot.memory_store, exc)
                # Agent committed: a raced message uses the new agent and confirms its grant.
                if assigned_store and (
                    state._slots.get(slot.key) is not slot or slot.agent is not committed_agent
                ):
                    # No unwind: the owner's grant on this slot's own key is valid and immutable.
                    return web.json_response(
                        {
                            "error": "slot changed during member assignment",
                            "code": "session_rebound",
                        },
                        status=409,
                    )
                if assigned_store and slot.memory_store != assigned_store:
                    slot.memory_store = assigned_store

        # Snapshot the response's workspace LAST, immediately before leaving
        # the lock: the metadata await above yields the event loop, so a
        # concurrent /workspace pick can land after the commit — the response
        # (which the acting tab writes into its store) must name the slot's
        # newest reality, not a pre-await snapshot.
        workspace = slot.workspace or "default"
        # The switch is committed and the session reset: mark the boundary in
        # the transcript, still inside both locks. A re-pick of the bound agent
        # changes nothing the row could name, so it appends none.
        if caller.notice and agent_changed:
            await _append_session_switch_notice(slot, "agent", agent_name)
    # The reset destroyed any eagerly created session; picking an agent is
    # itself a strong first-message intent signal (it also resets the
    # project), so re-arm the speculative spawn for the new bindings.
    schedule_eager_spawn(state, slot, start_priority=person_priority(caller.owner))
    state.push_slots_update()
    resp_body: dict = {
        "ok": True,
        "agent": agent_name,
        "agent_kind": slot.agent_kind,
        "workspace": workspace,
    }
    if teardown_incomplete:
        # Advisory only — the switch itself succeeded and the response
        # carries the committed state the acting tab writes optimistically.
        resp_body["warning"] = _TEARDOWN_INCOMPLETE_WARNING
    return web.json_response(resp_body)
