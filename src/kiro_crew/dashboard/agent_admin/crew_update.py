"""``PUT /api/agents/{name}``: the crew record update, including its binding change and avatar promotion."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        TEMPLATE_NAME_RE,
        CapabilityError,
        KiroCrewAgentConfig,
        KiroCrewConfig,
        _avatar_stem,
        _carries_mask,
        _carry_motions_through_motionless_save,
        _carry_pack_through_faceless_save,
        _commit_promoted_avatar,
        _crew_effort_rejected,
        _crew_memory_store_rejected,
        _discard_pending_avatar,
        _drained_to_thread,
        _foreign_private_copy_owner,
        _ForeignPrivateCopy,
        _get_config_lock,
        _is_ghost_shaped,
        _live_avatar_file,
        _model_pin_rejected,
        _pin_entitlement_backend,
        _promote_pending_avatar,
        _rebind_crew_locked,
        _refresh_session_defaults,
        _remove_avatar_files,
        _require_owner,
        _revalidate_crew_pin,
        _rollback_promoted_avatar,
        _safe_avatar,
        _safe_color,
        _sel,
        _StaleBinding,
        _UnverifiableLineage,
        coerce_effort,
        logger,
        normalize_agent_model,
        persist_member_config,
        require_unmanaged_template,
    )


def _effort_inputs(crew: KiroCrewAgentConfig | None) -> tuple[str, str] | None:
    """The crew fields `resolve_session_effort` reads, or ``None`` for no crew.

    Derived from the resolver's inputs rather than enumerated per call site. The
    chain reads two things off the record -- the pin itself, and the bound
    ``kiro_agent`` the role default keys on -- and the factory answers from the
    config it captured, so ANY change to either (including a crew appearing or
    disappearing) must invalidate that capture. Three rounds of review found the
    per-condition version incomplete one case at a time (an unpinned crew whose
    binding makes it a background worker; a re-bound `kiro_agent`); comparing this
    tuple before and after a write is the invariant those cases are instances of,
    and a future field the chain starts reading is added here once instead of at
    every handler.
    """
    if crew is None:
        return None
    return (crew.kiro_agent, coerce_effort(crew.reasoning_effort))


async def _apply_member_approval_to_live_thread(request: web.Request, name: str, mode: str) -> None:
    """Put crewmate ``name``'s live DM slot on ``mode`` through ``/api/chat/mode``'s path."""
    from kiro_crew import members as members_mod
    from kiro_crew.dashboard.chat_handlers import apply_approval_mode

    state = request.app.get("state")
    if state is None:
        return
    slot = next(
        (
            s
            for s in list(state._slots.values())
            if s.mode == members_mod.DM_SLOT_MODE and s.agent == name
        ),
        None,
    )
    if slot is None:
        # No live thread: the next thread open seeds from the record.
        return
    await apply_approval_mode(
        state,
        mode=mode,
        slot=slot,
        slot_key=slot.key,
        request_app="",
        cron_creator="",
        audit_caller=lambda label: "dashboard:member_profile",
    )
    # The thread now holds the record's choice; the open-time seed must not redo it.
    slot._member_approval_seeded = True


async def api_kirocrew_agent_update(request: web.Request) -> web.Response:
    """PUT /api/agents/{name} — update a Kiro Crew agent."""

    denied = await _require_owner(request, "agent.update")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be an object", "code": "body_not_object"}, status=400
        )
    # WRITE-SIDE HALF of the roster mask, and it runs FIRST -- immediately after
    # the body-object check, before any validation. `GET /api/agents` replaces a
    # value it cannot show verbatim with `_SENSITIVE_MASK` (`_roster_mask`), and
    # a client that echoes the record back -- the agents page sends every field
    # on every save, so that `""` can clear a pin -- would otherwise persist the
    # mask over the stored original. A field carrying the mask therefore means
    # "unchanged" and is dropped here, which is the remedy
    # `_masked_config_dict`'s docstring prescribes verbatim: "MUST treat
    # `_SENSITIVE_MASK` as 'unchanged' and keep the stored value".
    #
    # Ordering is load-bearing, not cosmetic: `model` and `reasoning_effort` are
    # validated below and would REJECT an echoed mask with a 400, failing an edit
    # to some unrelated field. Dropping the masked entries before those checks
    # means a mask can never be validated as if it were content.
    #
    # It can run this early only because the predicate matches a FIXED sentinel
    # and needs no access to the stored record -- a rule that recognised the view
    # by recomputing the redaction of `agent` would have to wait for the config
    # load inside the lock, and would therefore sit after these validations.
    body = {key: val for key, val in body.items() if not _carries_mask(val)}
    # Binding-only fast path (the template pane's saved-as-you-go switch). The
    # generic path below writes a full ``cfg.save()`` snapshot, which races
    # every other config writer (CLI, settings PUTs) and silently reverts
    # their concurrent changes — so a payload that moves ONLY the binding goes
    # through the locked binding-delta writer instead, stale-checked against
    # the caller's expected prior binding when it supplies one.
    if "kiro_agent" in body and set(body) <= {"kiro_agent", "expected_kiro_agent"}:
        new_target = body["kiro_agent"]
        expected_raw = body.get("expected_kiro_agent")
        if expected_raw is not None and not isinstance(expected_raw, str):
            return web.json_response(
                {
                    "error": "expected_kiro_agent must be a string",
                    "code": "invalid_expected_kiro_agent",
                },
                status=400,
            )
        current = await asyncio.to_thread(KiroCrewConfig.load)
        if name not in current.agents:
            return web.json_response(
                {"error": f"Agent '{name}' not found", "code": "agent_not_found"}, status=404
            )
        stored_target = current.agents[name].kiro_agent
        if new_target != stored_target and (
            not isinstance(new_target, str) or not TEMPLATE_NAME_RE.fullmatch(new_target)
        ):
            return web.json_response(
                {"error": "invalid kiro_agent name", "code": "invalid_kiro_agent_name"},
                status=400,
            )
        # The new target itself stays acceptable so a repeated switch to the
        # same template is idempotent rather than a spurious conflict.
        expected: tuple[str, ...] | None = (
            None if expected_raw is None else (expected_raw, new_target)
        )
        if expected is None and new_target == stored_target:
            expected = (stored_target,)
        # Under the handler-level config lock, like fork/publish/reset: the
        # cross-process advisory lock inside ``_rebind_crew_locked`` guards the
        # file write, but it cannot stop the generic path below from saving a
        # full snapshot it loaded BEFORE this rebind (it holds this lock across
        # its load->save span and awaits in between). Without taking the same
        # lock here, that stale snapshot lands after the delta and silently
        # reverts the switch while both requests report success.
        try:
            async with _get_config_lock():
                await asyncio.to_thread(_rebind_crew_locked, name, expected, new_target)
        except CapabilityError as exc:
            return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)
        except _StaleBinding:
            return web.json_response(
                {
                    "error": "The crew's template changed underneath this switch; reload and retry.",
                    "code": "stale_binding",
                },
                status=409,
            )
        except _ForeignPrivateCopy as exc:
            return web.json_response(
                {
                    "error": f"Template '{new_target}' is crew '{exc.owner}'s private copy; "
                    "it cannot be bound to another crew.",
                    "code": "foreign_private_copy",
                },
                status=409,
            )
        except _UnverifiableLineage:
            return web.json_response(
                {
                    "error": f"Cannot verify whether '{new_target}' is a private copy; retry.",
                    "code": "lineage_unverifiable",
                },
                status=409,
            )
        # A binding change moves what the effort chain resolves, same as the
        # generic path.
        await _refresh_session_defaults(request, name)
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="agent.update",
            outcome="success",
            source="dashboard",
            resources=name,
        )
        return web.json_response({"ok": True, "name": name})
    if "model" in body:
        pending_model = normalize_agent_model(body["model"])
    # Rejected before the config is even loaded: the check is pure, and every
    # validation must land before the first field assignment below so a bad value
    # cannot leave the in-memory record half-updated.
    if "reasoning_effort" in body:
        effort_reason = _crew_effort_rejected(body["reasoning_effort"])
        if effort_reason:
            return web.json_response(
                {"error": effort_reason, "code": "invalid_reasoning_effort"}, status=400
            )
    # Same placement rule as reasoning_effort: validated up here, before the
    # lock and before any field or avatar-file mutation. A body that pairs a
    # bad `starred` with an avatar promotion would otherwise move the staged
    # picture and then 400 without rolling it back. Strictly a bool: a string
    # "false" from a hand-typed request must not read as truthy and star the crew.
    # Local: ``compose`` rebinds this module's functions onto the agents
    # handler namespace, so a module-level import here is not in scope.
    from kiro_crew.config.sections import MEMBER_APPROVAL_MODES

    if "approval_mode" in body and body["approval_mode"] not in MEMBER_APPROVAL_MODES:
        return web.json_response(
            {
                "error": "approval_mode must be one of " + ", ".join(MEMBER_APPROVAL_MODES),
                "code": "invalid_approval_mode",
            },
            status=400,
        )
    if "expected_approval_mode" in body and not isinstance(body["expected_approval_mode"], str):
        return web.json_response(
            {
                "error": "expected_approval_mode must be a string",
                "code": "invalid_expected_approval_mode",
            },
            status=400,
        )
    if "starred" in body and not isinstance(body["starred"], bool):
        return web.json_response(
            {"error": "starred must be a boolean", "code": "invalid_starred"}, status=400
        )
    if "memory_store" in body:
        memory_store_reason = _crew_memory_store_rejected(body["memory_store"])
        if memory_store_reason:
            return web.json_response(
                {"error": memory_store_reason, "code": "invalid_memory_store"}, status=400
            )
    if "provision_memory" in body:
        return web.json_response(
            {
                "error": "Memory is initialized only when creating a new member. Restore missing member memory from backup.",
                "code": "member_memory_creation_only",
            },
            status=400,
        )
    if "model" in body:
        pending_reason = await _revalidate_crew_pin(pending_model, request)
        if pending_reason:
            return web.json_response({"error": pending_reason, "code": "invalid_model"}, status=400)
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name not in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' not found"}, status=404)
        if "model" in body:
            # Validated before the write, reusing the config loaded just above so
            # this costs no extra read.
            model_reason = _model_pin_rejected(
                pending_model,
                request,
                cfg.agent.provider,
                backend=_pin_entitlement_backend(cfg),
            )
            if model_reason:
                return web.json_response(
                    {"error": model_reason, "code": "invalid_model"}, status=400
                )
        agent = cfg.agents[name]
        if "kiro_agent" in body and body["kiro_agent"] != agent.kiro_agent:
            new_target = body["kiro_agent"]
            if not isinstance(new_target, str) or not TEMPLATE_NAME_RE.fullmatch(new_target):
                return web.json_response(
                    {"error": "invalid kiro_agent name", "code": "invalid_kiro_agent_name"},
                    status=400,
                )
            try:
                await asyncio.to_thread(require_unmanaged_template, agent.kiro_agent)
            except CapabilityError as exc:
                return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)
        prior_memory_store = agent.memory_store
        # A binding is immutable in both directions -- a private store is never
        # shared or rebound, and a live V1 binding is kept until the owner opts in
        # -- with one exception: a V1 binding whose name no resolver composes
        # (``unusable_legacy_binding``) may move to the global store. Nothing is
        # protected on it: the name resolves no directory, so the member cannot
        # run a turn on it, and the same name is what refused every repair.
        if "memory_store" in body and body["memory_store"] != prior_memory_store:
            return web.json_response(
                {
                    "error": "A member's memory identity cannot be rebound",
                    "code": "member_memory_immutable",
                },
                status=409,
            )
        # Captured BEFORE any mutation: what the effort chain reads today.
        effort_inputs_before = _effort_inputs(agent)
        # Best-effort per-member event log: snapshot the config-derived roster
        # fields before mutation so member/config can report which changed.
        _ev_before = {
            "kiro_agent": agent.kiro_agent,
            "workspace": agent.workspace,
            "memory_store": agent.memory_store,
            "model": agent.model,
            "source": agent.source,
            "starred": bool(agent.starred),
            "avatar": agent.avatar,
            "display_name": agent.display_name,
        }
        changed: list[str] = []
        if "kiro_agent" in body:
            try:
                owner = await asyncio.to_thread(
                    _foreign_private_copy_owner, name, body["kiro_agent"]
                )
            except _UnverifiableLineage:
                return web.json_response(
                    {
                        "error": f"Cannot verify whether '{body['kiro_agent']}' is a "
                        "private copy; retry.",
                        "code": "lineage_unverifiable",
                    },
                    status=409,
                )
            if owner:
                return web.json_response(
                    {
                        "error": f"Template '{body['kiro_agent']}' is crew '{owner}'s "
                        "private copy; it cannot be bound to another crew.",
                        "code": "foreign_private_copy",
                    },
                    status=409,
                )
            agent.kiro_agent = body["kiro_agent"]
            changed.append("kiro_agent")
        if "workspace" in body:
            agent.workspace = body["workspace"]
            changed.append("workspace")
        if "model" in body:
            # "auto"/"" both mean inherit; store the single "" spelling so the
            # agent keeps deferring to the kiro pin / global fallback. Raw, not
            # str()-coerced — see the create path for why.
            agent.model = normalize_agent_model(body["model"])
            changed.append("model")
        if "reasoning_effort" in body:
            # Already validated above; "" is the inherit sentinel and clears a pin.
            agent.reasoning_effort = body["reasoning_effort"].strip()
            changed.append("reasoning_effort")
        if "approval_mode" in body:
            # Compare-and-set: the profile sends the value it last read, and a
            # write made from a stale read is refused with the current value,
            # so the server, not the browser, decides which pick wins. Checked
            # under the config lock against the record loaded inside it.
            expected_mode = body.get("expected_approval_mode")
            if expected_mode is not None and expected_mode != agent.approval_mode:
                return web.json_response(
                    {
                        "error": "the permission changed since it was read",
                        "code": "approval_mode_conflict",
                        "approval_mode": agent.approval_mode,
                    },
                    status=409,
                )
            # Validated above. Only ever a real choice: the profile never sends
            # "" back, so a pinned mode cannot fall back to the trust default.
            agent.approval_mode = body["approval_mode"]
            changed.append("approval_mode")
        if "description" in body:
            agent.description = body["description"]
            changed.append("description")
        if "display_name" in body:
            # Presentation only, but strictly a string: a non-string here would
            # be stored verbatim and then rendered by every roster surface.
            # "" is a real value — it clears the label back to the name.
            if not isinstance(body["display_name"], str):
                return web.json_response(
                    {"error": "display_name must be a string", "code": "invalid_display_name"},
                    status=400,
                )
            agent.display_name = body["display_name"].strip()
            changed.append("display_name")
        if "triggers" in body:
            agent.triggers = body["triggers"]
            changed.append("triggers")
        if "session_color" in body:
            _sc = body["session_color"]
            _norm = _safe_color(_sc)
            if _sc not in ("", None) and not _norm:
                return web.json_response(
                    {
                        "error": "session_color must be #rrggbb or empty",
                        "code": "invalid_color_hex",
                    },
                    status=400,
                )
            agent.session_color = _norm
            changed.append("session_color")
        _avatar_promoted = False
        _avatar_pin = ""
        _prior_pin: object = None
        _remove_files_after_save = False
        if "avatar" in body:
            _raw_av = body["avatar"]
            _av = _safe_avatar(_raw_av)
            # Same 400 convention as session_color: junk that coerces to "no
            # override" is refused rather than silently clearing the face.
            # None/{} are the explicit "reset to name-derived" spellings, and
            # a well-formed ghost override that collapses all-empty is the
            # validator's own reset rule, not caller junk.
            if _raw_av not in (None, {}) and not _av and not _is_ghost_shaped(_raw_av):
                return web.json_response(
                    {
                        "error": "avatar must be {'kind': 'ghost', 'traits'/'motions'/'sounds': {...}}, {'kind': 'image'}, {'kind': 'pack', 'id': ...}, or empty",
                        "code": "invalid_avatar",
                    },
                    status=400,
                )
            _av = _carry_pack_through_faceless_save(agent.avatar, _raw_av, _av)
            _av = _carry_motions_through_motionless_save(agent.avatar, _raw_av, _av)
            if _av.get("kind") == "image":
                # THE commit point for pictures, under this same config lock.
                # `promote` is a wire-only directive (never persisted — the
                # validator drops it): the client sets it exactly when THIS
                # save staged a fresh upload. Without it, a leftover staging
                # from an earlier failed or abandoned save must NOT ride along
                # into an unrelated edit — it is discarded instead, and the
                # crew keeps wearing its current picture.
                _tok = _raw_av.get("token") if isinstance(_raw_av, dict) else None
                _wants_promote = isinstance(_raw_av, dict) and _raw_av.get("promote") is True
                _prior_pin = agent.avatar.get("file")
                if _wants_promote and not isinstance(_tok, str):
                    # `promote` without its staging token must not slide into
                    # the picture-keeping branch: that would discard the
                    # staged replacement and report success for a save that
                    # installed nothing.
                    return web.json_response(
                        {
                            "error": "promote requires the staging token from the upload",
                            "code": "avatar_file_missing",
                        },
                        status=400,
                    )
                if _wants_promote and isinstance(_tok, str):
                    _promoted = await _drained_to_thread(_promote_pending_avatar, name, _tok)
                    _avatar_promoted = _promoted is not None
                    if _promoted is None:
                        # The bytes THIS save staged are gone (a newer save
                        # re-staged the slot, or staging was cleaned up).
                        # Falling back to the current live picture would
                        # report success while silently dropping the user's
                        # selected replacement — fail the commit instead.
                        return web.json_response(
                            {
                                "error": "staged avatar no longer matches this save"
                                " — upload the picture again",
                                "code": "avatar_file_missing",
                            },
                            status=400,
                        )
                    stamp, _avatar_pin = _promoted
                else:
                    await _drained_to_thread(_discard_pending_avatar, name)
                    # A picture-keeping edit (no fresh upload): stamp from the
                    # file the config's pin already selects.
                    _live = await asyncio.to_thread(_live_avatar_file, name, _prior_pin)
                    stamp = None
                    if _live is not None:
                        _avatar_pin = _live.name[len(_avatar_stem(name)) + 1 :]
                        try:
                            stamp = int((await asyncio.to_thread(_live.stat)).st_mtime_ns)
                        except OSError:
                            stamp = None
                if stamp is None:
                    return web.json_response(
                        {
                            "error": "no uploaded avatar file to commit — POST the picture first",
                            "code": "avatar_file_missing",
                        },
                        status=400,
                    )
                # Rebuilt, not mutated, so the record carries exactly the
                # committed stamp and pin. The cue is validated INPUT rather than
                # commit output, so it has to be carried across explicitly --
                # otherwise saving a sound on a crew that wears a picture reports
                # success and stores nothing. `motions` is not carried: the
                # validator drops it on this tier, so there is never one here.
                _av = {
                    "kind": "image",
                    "v": stamp,
                    "file": _avatar_pin,
                    **{k: v for k, v in _av.items() if k in ("expressions", "sounds")},
                }
            elif agent.avatar.get("kind") == "image":
                # Leaving the picture tier: the stored file must not linger
                # as a silently-retrievable orphan — but only once the config
                # write that stops selecting it has actually succeeded.
                _remove_files_after_save = True
            agent.avatar = _av
            changed.append("avatar")
        if "source" in body:
            agent.source = body["source"]
            changed.append("source")
        if "starred" in body:
            # Already validated above, before any mutation.
            agent.starred = body["starred"]
            changed.append("starred")
        effort_inputs_after = _effort_inputs(agent)
        # Avatar rollback applies only to ordinary failure: cancellation may
        # arrive after the drained worker published the new avatar pin. Store
        # cleanup independently checks the current locked config, so a landed
        # memory binding survives even when the request was cancelled.
        from kiro_crew.memory_stores import MemberApprovalConflict

        expected_on_disk = body.get("expected_approval_mode") if "approval_mode" in body else None
        try:
            await _drained_to_thread(
                lambda: persist_member_config(
                    cfg,
                    name,
                    expected_store=prior_memory_store,
                    changed_fields=set(changed),
                    expected_approval_mode=expected_on_disk,
                )
            )
        except MemberApprovalConflict as conflict:
            # Lost to a write from another process: nothing was saved and the
            # live thread is not touched.
            if _avatar_promoted:
                await _drained_to_thread(_rollback_promoted_avatar, name, _avatar_pin, _prior_pin)
            return web.json_response(
                {
                    "error": "the permission changed since it was read",
                    "code": "approval_mode_conflict",
                    "approval_mode": conflict.current,
                },
                status=409,
            )
        except BaseException as exc:
            if isinstance(exc, Exception) and _avatar_promoted:
                await _drained_to_thread(_rollback_promoted_avatar, name, _avatar_pin, _prior_pin)
            raise
        if _avatar_promoted:
            await _drained_to_thread(_commit_promoted_avatar, name, _avatar_pin)
        if _remove_files_after_save:
            await _drained_to_thread(_remove_avatar_files, name)
        if "approval_mode" in changed:
            # The server owns both writes: the crewmate's live DM thread takes
            # the saved permission here, under the same config lock as the
            # compare-and-set above, so no browser write can land out of order.
            await _apply_member_approval_to_live_thread(request, name, agent.approval_mode)
        # Best-effort per-member event log: the save succeeded, so emit a
        # config snapshot with the list of fields that actually changed.
        try:
            from kiro_crew import eventlog_hooks
            from kiro_crew.dashboard.handlers.members import normalize_member_source
            from kiro_crew.eventlog.types import MEMBER_CONFIG
            from kiro_crew.members import member_slug

            _ev_after = {
                "kiro_agent": agent.kiro_agent,
                "workspace": agent.workspace,
                "memory_store": agent.memory_store,
                "model": agent.model,
                # Bounded to the roster vocabulary, matching the roster row and
                # ``_config_snapshot_for_agent`` — ``source`` is agent-writable
                # free text, so a credential- or URL-shaped value must not reach
                # the durable projection (which the drawer and WS ship) raw.
                "source": normalize_member_source(agent.source),
                "starred": bool(agent.starred),
                "avatar": agent.avatar,
                # Presentation label; ships raw here like its config peers —
                # the projection delivery path redacts every string before the
                # browser (`_redact_projection_value`), and the HTTP roster row
                # masks it independently (`_roster_mask`).
                "display_name": agent.display_name,
            }
            _ev_changed = [k for k, v in _ev_after.items() if v != _ev_before.get(k)]
            # A save that touched none of the roster fields is not a fact worth
            # recording: the projection would fold to the same value and emit
            # nothing, leaving only a no-op line in the log.
            if _ev_changed:
                # Off the event loop: ``emit`` opens the member log and does a
                # synchronous ``os.fsync`` append, which would otherwise stall
                # every gateway task on this async handler.
                await asyncio.to_thread(
                    eventlog_hooks.emit,
                    # member_slug, not the bare fold: a member may carry an explicit
                    # `member_id`, and the roster keys their log by it. Folding the
                    # name here would write this event to a DIFFERENT log than the
                    # roster reads, so the change would never appear. `cfg` is the
                    # config this handler already loaded, so the resolve costs no
                    # I/O on the loop -- member_slug would otherwise load it here.
                    member_slug(name, cfg),
                    name,
                    MEMBER_CONFIG,
                    {**_ev_after, "changed": _ev_changed},
                )
        except Exception:
            logger.debug("member/config event-log hook failed", exc_info=True)
    # Compared, not merely "the body carried the field": the crew form sends
    # reasoning_effort on every save (that is what makes clearing a pin possible)
    # and refresh_defaults drains the warm pool, so refreshing on presence would
    # cost a cold start on every unrelated crew edit. A re-bound kiro_agent counts
    # too -- the role default reads it. Outside the config lock, because the
    # refresh takes the session locks.
    if effort_inputs_after != effort_inputs_before:
        await _refresh_session_defaults(request, name)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.update",
        outcome="success",
        source="dashboard",
        resources=f"{name} ({','.join(changed)})",
    )
    result = {"ok": True, "name": name, "memory_store": agent.memory_store}
    return web.json_response(result)
