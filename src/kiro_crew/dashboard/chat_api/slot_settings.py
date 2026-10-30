"""The per-slot settings that need no relaunch: the auto-compact threshold, the turn
timeout and the tab colour.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        AUTOCOMPACT_PCT_MAX,
        AUTOCOMPACT_PCT_MIN,
        CHAT_TURN_TIMEOUT_MAX,
        CHAT_TURN_TIMEOUT_MIN,
        COLOR_HEX_RE,
        MAX_COLOR_INDEX,
        AutocompactPctError,
        DashboardState,
        KiroCrewConfig,
        _autocompact_txn_locks,
        _reauthorize_after_await,
        chat_turn_timeout_secs,
        crew_autocompact_pct,
        deny_app_slot_session_access,
        effective_session_key,
        logger,
        parse_autocompact_pct,
        published_autocompact_pct,
        read_bounded_json,
        redact_credentials,
        redact_exfiltration_urls,
        resolve_agent_bindings,
        resolve_session_agent_bindings,
        save_slot_off_loop,
        slot_history_key,
        slot_not_found,
    )
    from kiro_crew.dashboard.slot_color import write_slot_color


def _autocompact_txn_lock(history_key: str) -> asyncio.Lock:
    lock = _autocompact_txn_locks.get(history_key)
    if lock is None:
        lock = asyncio.Lock()
        _autocompact_txn_locks[history_key] = lock
    return lock


async def api_chat_slot_autocompact(request: web.Request) -> web.Response:
    """GET/POST /api/chat/slots/{slot}/autocompact — per-session compact threshold.

    GET returns the slot's override (``pct``, null when it follows a default),
    the current global (``global_pct``), the default the session's crew
    declares (``agent_pct``) with that crew's name (``agent``), both null when
    it declares none -- the session then follows the global -- and the valid
    range. POST takes
    ``{"pct": <number|null>}``: a number sets this session's override (rejected
    outside the documented range, matching the global knob's PATCH validation),
    null clears it back to the agent's default, else the global. The value
    applies to the live session
    immediately via the SessionManager override map and persists with the slot
    metadata, so it survives gateway restarts.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    # Session-aware ownership gate, not the slot-only check: the POST writes the
    # override keyed by effective_session_key(slot), so a linked app-owned slot
    # (channel stem) would let an app modify a foreign session's threshold and
    # metadata. deny_app_slot_session_access authorizes the key the write actually
    # lands on, same as /context and /note.
    request_app = request.get("app", "")
    denied = deny_app_slot_session_access(request_app, slot, name, "slot_autocompact")
    if denied is not None:
        return denied
    if request.method == "GET":
        return web.json_response(
            {
                "pct": slot.autocompact_pct,
                "global_pct": published_autocompact_pct(),
                **await _slot_agent_autocompact_fields(state, slot),
                "harness_managed": await _slot_harness_managed_backend(state, slot),
                "min": AUTOCOMPACT_PCT_MIN,
                "max": AUTOCOMPACT_PCT_MAX,
            }
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    if "pct" not in body:
        return web.json_response(
            {"error": "pct is required (number or null)", "code": "pct_required"}, status=400
        )
    pct = body["pct"]
    if pct is not None:
        # bool is an int subclass; True would otherwise read as 1.0 and be
        # rejected by range, but reject it explicitly for a clear error.
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            return web.json_response(
                {"error": "pct must be a number or null", "code": "pct_not_a_number"}, status=400
            )
        try:
            pct = parse_autocompact_pct(pct)
        except AutocompactPctError as exc:
            return web.json_response({"error": str(exc), "code": exc.code}, status=400)
    # Persist via the same forced-save mechanism every other slot-metadata
    # route uses (tags / folders / pin: save_slot_off_loop(force=True) -- the
    # empty-window merge for message-less slots, the full save otherwise;
    # both read the slot fields INSIDE the transcript's cross-process lock
    # and run the delete-won guard, so a permanent delete racing this write
    # is refused by main's own tested path). A never-saved tab's override
    # lives only in memory until the tab first persists, exactly like its
    # model: the empty-window merge lands only into an existing line.
    #
    # Transactional shape (mirrors the tag-vocabulary delete): mutate the
    # slot field, confirm the durable write with best_effort=False, and on
    # any failure roll the field back and return a coded error -- the
    # SessionManager override map (the live gate) is only touched after the
    # persist verdict, so a failed request leaves live behavior unchanged.
    # Serialize the whole transaction per slot: with awaits inside the write
    # span, a second concurrent POST would otherwise capture this one's value
    # as its rollback snapshot, and value-based rollback cannot tell "my
    # write survived" from "someone else wrote the same number". Under the
    # lock exactly one request is inside the span, so a rollback can only
    # undo its own write. The client's per-slot promise chain orders writes
    # from ONE client; this lock is the cross-client half. Keyed by the
    # TRANSCRIPT so two alias slots resolving onto one file serialize too.
    locked_history_key = slot_history_key(slot)
    async with _autocompact_txn_lock(locked_history_key):
        stale = _reauthorize_after_await(state, slot, name, request_app, "slot_autocompact")
        if stale is not None:
            return stale
        # Pin the write to the transcript this authorization decision covered:
        # the persist await below is a rebind window, and the save derives its
        # target from live routing at write time. expected_history_key makes the
        # save refuse (False, nothing written) if the routing moved, so the
        # durable write can never land on a transcript this request was not
        # authorized against. No await between the reauth above and this read.
        authorized_history_key = slot_history_key(slot)
        if authorized_history_key != locked_history_key:
            # The slot was rebound between the lock-key read and acquisition:
            # this request holds the OLD transcript's lock while the write
            # would target the new one, so the serialization guarantee does
            # not cover it. Same disposition as the mid-persist rebind below.
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        prior_pct = slot.autocompact_pct
        slot.autocompact_pct = pct
        if state.conversation_log:
            try:
                applied = await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    best_effort=False,
                    expected_history_key=authorized_history_key,
                )
            except Exception:
                # Roll back this request's write and mark dirty so the
                # periodic flush reconverges the durable record to the live
                # field (a non-endpoint save may have durably written this
                # rejected value before the failure).
                slot.autocompact_pct = prior_pct
                slot._dirty = True
                logger.exception("Slot %s autocompact_pct persist failed", name)
                return web.json_response(
                    {"error": "could not persist threshold", "code": "persist_failed"}, status=500
                )
            if not applied:
                # The save refused without writing: either the delete-won guard
                # (session permanently deleted while the save awaited the lock)
                # or the routing-moved pin (slot rebound to another transcript
                # mid-request). Do not resurrect, do not write elsewhere, do not
                # mutate live state.
                slot.autocompact_pct = prior_pct
                return web.json_response(
                    {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
                )
            # Mirror the COMMITTED value to every live slot whose current
            # transcript key is the one this write landed on — not just the
            # requesting slot: channel-linked aliases resolve distinct slot
            # names onto one file, and a sibling left holding the old value
            # would persist it back over this acknowledged commit on its next
            # flush (its ordinary save writes ``autocompact_pct``
            # unconditionally from its own field). Membership is re-derived
            # here, NOT assumed: a slot rebound during the persist does not
            # write this file, and mirroring it would apply a live change its
            # reauthorization is about to deny. Snapshot values() — the event
            # loop may mutate the dict between iterations. Priors are recorded
            # so the confirm-save failure paths below can undo the mirror.
            mirrored: list = []
            for other in list(state._slots.values()):
                if other is not slot and slot_history_key(other) == authorized_history_key:
                    mirrored.append((other, other.autocompact_pct))
                    other.autocompact_pct = pct
            # The mirror runs on the event loop AFTER the persist returned, but
            # a sibling's already-queued flush can acquire the transcript's
            # file lock in the executor BEFORE the loop resumes here and write
            # its then-stale field over the acknowledged commit (executor
            # threads do not wait for the event loop). The mirror above fixes
            # every live field; this second confirmed save re-orders the
            # durable record after any such interleaved stale write — the file
            # lock serializes it behind the sibling's write, and every field it
            # can read is now the committed value. Same pin, same dispositions.
            try:
                confirmed = await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    best_effort=False,
                    expected_history_key=authorized_history_key,
                )
            except Exception:
                for other, other_prior in mirrored:
                    if slot_history_key(other) == authorized_history_key:
                        other.autocompact_pct = other_prior
                        other._dirty = True
                slot.autocompact_pct = prior_pct
                slot._dirty = True
                logger.exception("Slot %s autocompact_pct confirm-persist failed", name)
                return web.json_response(
                    {"error": "could not persist threshold", "code": "persist_failed"}, status=500
                )
            if not confirmed:
                for other, other_prior in mirrored:
                    if slot_history_key(other) == authorized_history_key:
                        other.autocompact_pct = other_prior
                        other._dirty = True
                slot.autocompact_pct = prior_pct
                return web.json_response(
                    {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
                )
        # INVARIANT for this handler: every write is immediately preceded by an
        # authorization decision with NO await between them. The persist await is
        # a rebind window (same mechanism as the body read), so re-decide before
        # the live override mutation; the slot-field change above rolls back for
        # a rebound slot, whose successor re-derives its state on restore.
        stale = _reauthorize_after_await(state, slot, name, request_app, "slot_autocompact")
        if stale is not None:
            slot.autocompact_pct = prior_pct
            return stale
        # Reauthorization can PASS after a rebind the pin never saw: a rebind
        # landing after the save's internal routing read leaves the durable
        # write correctly on the authorized transcript while the slot now
        # resolves to a different session the caller may also own. Seeding the
        # live map from effective_session_key(slot) would then apply the
        # threshold to a session whose transcript never received it. Refuse:
        # the committed transcript's siblings were mirrored above and its live
        # override re-seeds on hydration; this slot's successor re-derives.
        if slot_history_key(slot) != authorized_history_key:
            slot.autocompact_pct = prior_pct
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
            )
        live_pct = slot.autocompact_pct
        state.sessions.set_autocompact_pct(effective_session_key(slot), live_pct)
        logger.info("Slot %s autocompact_pct set to %r", name, live_pct)
        return web.json_response(
            {
                "ok": True,
                "pct": live_pct,
                "global_pct": published_autocompact_pct(),
                **await _slot_agent_autocompact_fields(state, slot),
            }
        )


async def _slot_agent_autocompact_fields(state: DashboardState, slot: Any) -> dict[str, Any]:
    """``agent_pct`` and ``agent``: the default *slot*'s session takes from its crew.

    Both are null when the session follows the global because its crew
    declares no default. A live session answers for the crew it started
    under. Without one (a new slot, an idle expiry, a restart before the first
    turn) the answer is for the crew the slot's next session starts under, so
    the popover shows the number the next context reading compacts at.
    """
    key = effective_session_key(slot)
    sessions = state.sessions
    if sessions is not None and sessions.has_session(key):
        found = sessions.agent_autocompact_default(key)
    else:
        found = await asyncio.to_thread(_slot_next_agent_autocompact_default, key, slot)
    if found is None:
        return {"agent_pct": None, "agent": None}
    # The name is display text for the popover, never an identity this route
    # acts on. Crew names are operator- and agent-writable and the name grammar
    # admits token-shaped strings, so it is scrubbed like any other label.
    label, _ = redact_exfiltration_urls(found[0])
    label, _ = redact_credentials(label)
    return {"agent_pct": found[1], "agent": label}


async def _slot_harness_managed_backend(state: DashboardState, slot: Any) -> str | None:
    """Backend id when *slot*'s session compacts on its own, else ``None``.

    A backend in ``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION`` (KAS) runs its own
    summarization, and the turn-end gate declines Crew's ``/compact`` for it at
    any threshold, so the popover says the slider does not apply there. The
    live session's provider answers when one exists; otherwise the answer is
    for the backend the slot's next session starts on, chosen by the same
    member-aware selector a new session uses.
    """
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_HARNESS_MANAGED_COMPACTION
    from kiro_crew.members import select_provider_backend

    key = effective_session_key(slot)
    sessions = state.sessions
    get_provider = getattr(sessions, "get_provider", None)
    provider = get_provider(key) if callable(get_provider) else None
    if provider is not None:
        backend = getattr(provider, "harness_managed_compaction_backend", None)
        return backend if isinstance(backend, str) and backend else None
    try:
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        agent_cfg = cfg.agent
        # A crewmate pinned to a harness that cannot start raises here and falls
        # to the debug path: its next session is refused, so no hint applies.
        backend = select_provider_backend(
            key,
            getattr(agent_cfg, "member_acp_backend", ""),
            getattr(agent_cfg, "acp_backend", ""),
            crew_backend=crewmate_backend_pin(cfg, key),
        )
    except Exception:
        logger.debug("autocompact: could not resolve slot %s's backend", key, exc_info=True)
        return None
    if isinstance(backend, str) and backend in ACP_BACKENDS_HARNESS_MANAGED_COMPACTION:
        return backend
    return None


def _slot_next_agent_autocompact_default(session_key: str, slot: Any) -> tuple[str, float] | None:
    """The crew a fresh session of *slot* would run, and the default it declares.

    Resolves the slot's selection the way a dashboard turn does before it
    starts a session: the crew is the resolved alias, and a template
    selection runs under no crew. Config and binding reads touch files, so
    callers run this off the event loop.
    """
    try:
        cfg = KiroCrewConfig.load()
        bindings = resolve_session_agent_bindings(
            resolve_agent_bindings,
            cfg,
            session_key,
            slot.agent or None,
            slot.project or None,
        )
    except Exception:
        logger.debug("autocompact: could not resolve slot %s's agent", session_key, exc_info=True)
        return None
    if bindings.selection_kind == "template":
        return None
    crew = bindings.resolved_alias or ""
    pct = crew_autocompact_pct(cfg, crew)
    return None if pct is None else (crew, pct)


def _turn_timeout_body_value(body: dict[str, Any]) -> tuple[int | None, web.Response | None]:
    """The ``secs`` a turn-timeout POST asks for, or the 400 that refuses it.

    Held to the same range and the same whole-seconds unit as the global
    ``agent.chat_turn_timeout_secs`` write gate, so a session can be given any
    limit Settings could save and nothing Settings would refuse. ``null`` asks
    to follow the global limit again.
    """

    def _refuse(error: str, code: str) -> tuple[None, web.Response]:
        return None, web.json_response({"error": error, "code": code}, status=400)

    if "secs" not in body:
        return _refuse("secs is required (integer or null)", "secs_required")
    secs = body["secs"]
    if secs is None:
        return None, None
    # bool is an int subclass; refuse it by name rather than as 0 or 1 seconds.
    if isinstance(secs, bool) or not isinstance(secs, int):
        return _refuse("secs must be a whole number of seconds or null", "secs_not_an_integer")
    if not (CHAT_TURN_TIMEOUT_MIN <= secs <= CHAT_TURN_TIMEOUT_MAX):
        return _refuse(
            f"secs must be between {CHAT_TURN_TIMEOUT_MIN} and {CHAT_TURN_TIMEOUT_MAX}",
            "secs_out_of_range",
        )
    return secs, None


async def _turn_timeout_payload(secs: int | None) -> dict[str, Any]:
    """What the session menu shows: this session's limit and the global one."""
    global_secs = await asyncio.to_thread(chat_turn_timeout_secs)
    return {
        "secs": secs,
        "global_secs": int(global_secs),
        "min": CHAT_TURN_TIMEOUT_MIN,
        "max": CHAT_TURN_TIMEOUT_MAX,
    }


async def api_chat_slot_turn_timeout(request: web.Request) -> web.Response:
    """GET/POST /api/chat/slots/{slot}/turn-timeout — this session's turn time limit.

    GET returns the session's own limit (``secs``, null while it follows the
    global), the global ``agent.chat_turn_timeout_secs`` (``global_secs``) and
    the valid range. POST takes ``{"secs": <integer|null>}``: an integer gives
    this session its own limit, which its next turn runs under instead of the
    global one; null returns it to the global. Out-of-range values are refused,
    matching the global setting's write gate.

    The value lives on the slot and in the session's metadata line, so it
    survives a gateway restart; the turn dispatch reads it when each turn
    starts (``turn_dispatch.resolve_turn_limit``), so a turn already running
    keeps the limit it started with. The write holds the compaction-threshold
    route's per-transcript lock and pins the save to the authorized
    transcript, and the live field changes only once the write has committed
    (see the comment at the save).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    request_app = request.get("app", "")
    denied = deny_app_slot_session_access(request_app, slot, name, "slot_turn_timeout")
    if denied is not None:
        return denied
    if request.method == "GET":
        return web.json_response(await _turn_timeout_payload(slot.turn_timeout_secs))
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    secs, refused = _turn_timeout_body_value(body)
    if refused is not None:
        return refused

    def _gone() -> web.Response:
        return web.json_response(
            {"error": "session was deleted or rebound", "code": "session_gone"}, status=409
        )

    locked_history_key = slot_history_key(slot)
    async with _autocompact_txn_lock(locked_history_key):
        stale = _reauthorize_after_await(state, slot, name, request_app, "slot_turn_timeout")
        if stale is not None:
            return stale
        authorized_history_key = slot_history_key(slot)
        if authorized_history_key != locked_history_key:
            return _gone()
        committed = False
        if state.conversation_log:
            # Persist before publishing. The new value reaches disk as a STAGED
            # value while the live field keeps the committed one, so a turn
            # dispatched during the save still runs under the committed limit
            # and a failed save leaves nothing to undo. The live field (and the
            # live field of every alias slot on this transcript) flips in the
            # save's commit hook, which runs under the transcript lock right
            # after the write lands: an alias flush needs that lock to
            # serialize, so none can write the old value back over this one.
            # The aliases are listed inside the hook, so a slot bound to this
            # transcript while the save ran is published to as well.

            def _publish_committed() -> None:
                nonlocal committed
                committed = True
                others = [other for other in list(state._slots.values()) if other is not slot]
                for other in (slot, *others):
                    if slot_history_key(other) == authorized_history_key:  # type: ignore[arg-type]
                        other.turn_timeout_secs = secs  # type: ignore[union-attr]
                        other._turn_timeout_hydrated = True  # type: ignore[union-attr]

            try:
                saved = await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    best_effort=False,
                    expected_history_key=authorized_history_key,
                    turn_timeout_override=(secs,),
                    after_commit_under_lock=_publish_committed,
                )
            except Exception:
                # Nothing committed and nothing was published. Dirty only so an
                # unrelated flush re-converges the unchanged durable record.
                slot._dirty = True
                logger.exception("Slot %s turn_timeout_secs persist failed", name)
                return web.json_response(
                    {"error": "could not persist turn time limit", "code": "persist_failed"},
                    status=500,
                )
            if not saved:
                # Refused without writing: the session was deleted, or the slot
                # was rebound to another transcript mid-request.
                return _gone()
            # The persist await is a rebind window. A slot that now routes to
            # another transcript holds THAT transcript's limit, set by whatever
            # rebound it, so a rejection here leaves the slot's field alone.
            stale = _reauthorize_after_await(state, slot, name, request_app, "slot_turn_timeout")
            if stale is not None:
                return stale
            if slot_history_key(slot) != authorized_history_key:
                return _gone()
            if not committed:
                # A tab with no metadata line yet: the empty-window merge does
                # not materialize one, so there is no durable record to be the
                # authority. The slot is the only copy until the tab's first
                # full save, which writes this field, the same way a model
                # picked on a fresh tab persists.
                slot.turn_timeout_secs = secs
        else:
            slot.turn_timeout_secs = secs
        # The picked value is now the slot's own, read or not before.
        slot._turn_timeout_hydrated = True
        committed_secs = slot.turn_timeout_secs
        logger.info("Slot %s turn_timeout_secs set to %r", name, committed_secs)
        return web.json_response({"ok": True, **await _turn_timeout_payload(committed_secs)})


async def api_chat_slot_color(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/color — set session color.

    Accepts ``color_index`` (int 0..MAX_COLOR_INDEX or null, resolved
    client-side against the viewer's generated palette) and/or ``color_hex``
    (``#rrggbb`` or null, a theme-independent custom color). The two are
    mutually exclusive: setting a non-null value for one clears the other, so
    a slot can never carry both and clients need no precedence rule. Keys are
    ``in body``-gated so an old client sending only ``color_index`` cannot
    silently null an existing hex.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    has_ci = "color_index" in body
    has_ch = "color_hex" in body
    ci = body.get("color_index")
    if ci is not None and (
        isinstance(ci, bool) or not isinstance(ci, int) or ci < 0 or ci > MAX_COLOR_INDEX
    ):
        return web.json_response(
            {"error": f"color_index must be a non-negative integer <= {MAX_COLOR_INDEX} or null"},
            status=400,
        )
    ch = body.get("color_hex")
    if ch is not None and (not isinstance(ch, str) or not COLOR_HEX_RE.match(ch)):
        return web.json_response(
            {"error": "color_hex must be #RRGGBB or null", "code": "invalid_color_hex"},
            status=400,
        )
    new_index, new_hex = slot.color_index, slot.color_hex
    if has_ci:
        new_index = ci
        if ci is not None:
            new_hex = None
    if has_ch:
        new_hex = ch.lower() if isinstance(ch, str) else None
        if ch is not None:
            new_index = None
    write_slot_color(state, slot, color_index=new_index, color_hex=new_hex)
    return web.json_response(
        {"ok": True, "color_index": slot.color_index, "color_hex": slot.color_hex}
    )
