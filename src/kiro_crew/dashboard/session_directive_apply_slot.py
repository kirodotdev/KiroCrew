"""The directives that act on the turn's slot, and the quiet end of a turn.

Setting the slot's project, resetting its conversation, applying a chat tag, suggesting
a follow-up, and the nothing-to-do outcome, with the push that tells the browser. A new
slot-targeted directive goes here. Its function-local imports are deliberate;
the facade's module docstring says why.

Served through :mod:`kiro_crew.dashboard.session_directive_apply`, which callers import
and tests patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to
this module, and this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from kiro_crew.dashboard.session_directive_apply_monitor import _binding
from kiro_crew.dashboard.session_directive_apply_outcome import (
    QUIET_END_OUTCOME_PREFIX,
    DirectiveOutcome,
    _DirectiveDenied,
)

logger = logging.getLogger("kiro_crew.dashboard.session_directive_apply")


# ── slot-targeted effects (the dashboard-only pair + set_project) ────────────


async def _set_project(state: Any, slot: Any, args: dict[str, Any]) -> str:
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.sandbox import voice_runtime_workspace_conflict
    from kiro_crew.security import is_unverifiable_path_refusal, sensitive_path_refusal

    clear = bool(args.get("clear"))
    project = str(args.get("project") or "").strip()
    old_project = getattr(slot, "project", "") or ""
    if clear or not project:
        slot.project = ""
        if old_project:
            slot._pending_reset_history_key = effective_session_key(slot)
        _push(state)
        return "Project cleared. The next message cold-starts with no project scope."
    expanded = os.path.expanduser(project)

    def _validate() -> tuple[str, str | None, bool]:
        """Resolve + classify the path on a worker thread.

        `realpath`/`isdir` touch the filesystem, so a network-mounted project
        path would stall chat, heartbeat and liveness if resolved on the event
        loop (no-blocking-call-on-event-loop). Returns
        (realpath, refusal, is_dir); the sensitive check runs on BOTH the
        pre-resolution and resolved forms — the pre-check keeps a sensitive
        path from being probed at all, the post-check catches symlink/".."
        evasion.
        """
        if reason := sensitive_path_refusal(expanded):
            return "", reason, False
        rp_ = os.path.realpath(expanded)
        if reason := sensitive_path_refusal(rp_):
            return rp_, reason, False
        return rp_, None, os.path.isdir(rp_)

    rp, refusal, is_dir = await asyncio.to_thread(_validate)
    if refusal:
        # Permission decision — raise so the wrapper audits it as denied.
        if is_unverifiable_path_refusal(refusal):
            raise _DirectiveDenied(f"Error: {refusal}")
        raise _DirectiveDenied("Error: access denied (sensitive path).")
    if not is_dir:
        return f"Error: not a directory: {rp}"
    # Pre-flight, mirrored from the HTTP project endpoint: this directive
    # is the OTHER user/agent-driven moment of choice that sets slot.project
    # (set_project MCP routes here in-process, never through the endpoint), so
    # without this check the overlap refusal would still land at spawn time,
    # after the bad folder was committed. Same helper, same message; off the
    # loop because it stats the runtime paths.
    overlap = await asyncio.to_thread(voice_runtime_workspace_conflict, rp)
    if overlap is not None:
        return f"Error: {overlap}"
    slot.project = rp
    if rp != old_project:
        slot._pending_reset_history_key = effective_session_key(slot)
        try:
            from kiro_crew.dashboard.chat_handlers import _save_recent_project

            # Offload the recent-projects file IO (mkdir + read + atomic write)
            # off the event loop — the HTTP endpoint this replaced did the same.
            await asyncio.to_thread(_save_recent_project, rp)
        except Exception:
            logger.debug("save recent project failed", exc_info=True)
    _push(state)
    return (
        f"Project set to {rp}. The session cold-starts with the new CWD and "
        "project-level .kiro/steering on the next message."
    )


def _slot_has_pending_interaction(state: Any, slot: Any) -> bool:
    """Whether *slot* waits on a person: a question card or a tool approval.

    Reads the same registries the dashboard's waiting lanes do: the slot's
    question records (blocking and non-blocking), its own approval futures, and
    the gateway coordinator's approvals owned by the slot.
    """
    if getattr(slot, "_question_pending", None):
        return True
    futures = getattr(slot, "_approval_futures", None) or {}
    if any(not fut.done() for fut in futures.values()):
        return True
    pending_for = getattr(state, "pending_coordinator_approvals", None)
    return bool(callable(pending_for) and pending_for(getattr(slot, "key", "")))


async def _refuse_unvouched_wake_reset(
    state: Any, slot: Any, session_key: str, wake_loop_id: str
) -> None:
    """Refuse a patrol wake's ``reset_conversation`` unless the gateway can vouch for it.

    Admitted only when the slot runs a conductor agent, the wake comes from this
    slot's CURRENT, active loop, the
    keystone-gated self-arm record says that loop was armed by this slot's own
    turn (so never a cron, a sub-agent, a person's dashboard arm or the
    bind-time default patrol), and nothing in the slot waits on a person -- a
    reset would drop the context the answer is meant for.
    """
    from kiro_crew.autonudge import get_instance
    from kiro_crew.autonudge_selfarm import WAKE_RESET_AGENTS, is_recorded_self_arm

    refused = "Conversation NOT reset: "
    if str(getattr(slot, "agent", "") or "") not in WAKE_RESET_AGENTS:
        raise _DirectiveDenied(
            refused + "only a conductor's patrol wake may drop its own conversation."
        )

    def _live_loop() -> Any:
        svc = get_instance()
        binding = _binding(session_key)
        current = svc.get_by_slot(binding) if svc is not None and binding else None
        if (
            not wake_loop_id
            or current is None
            or getattr(current, "id", "") != wake_loop_id
            or not getattr(current, "active", False)
        ):
            raise _DirectiveDenied(
                refused + "the loop that delivered this wake is not this session's live monitor."
            )
        return current

    current = _live_loop()
    vouched = await asyncio.to_thread(
        is_recorded_self_arm, wake_loop_id, str(getattr(current, "slot_key", ""))
    )
    if not vouched:
        raise _DirectiveDenied(
            refused + "the loop that delivered this wake was not armed by this session's "
            "own turn, so its wake may not drop the conversation."
        )
    # Re-read after the await: a Pause, Stop or replacement can land while the
    # trust read is off-loop. From here to the queued flag nothing yields.
    _live_loop()
    if _slot_has_pending_interaction(state, slot):
        raise _DirectiveDenied(
            refused + "a question card or a tool approval is still pending in this session."
        )


async def _reset_conversation(
    slot: Any, session_key: str, args: dict[str, Any], *, from_wake: bool = False
) -> str:
    """Queue a conversation discard for this slot's next turn boundary.

    Deferred rather than applied here because the caller is mid-turn: a discard
    is a full provider teardown, and the immediate route
    (``POST /api/chat/slots/{slot}/reset-conversation``) refuses a busy slot for
    exactly that reason. Queuing is what makes the effect reachable from inside
    the turn that wants it — the flag is consumed at a later turn boundary.

    Queues the *session_key* THIS TURN runs on, captured by the caller, rather
    than re-resolving it from the slot. A slot's ``linked_session_key`` is
    mutable: a cron or workflow injection can rebind the live slot between the
    turn that asked for the reset and the consume that applies it, so a
    slot-resolved key would discard whatever conversation the slot points at by
    then and leave the one the caller meant untouched. The key is the caller's,
    not the slot's.

    Only the model's memory is dropped. The slot stays open, the session-map
    entry keeps its channel linkage, and the transcript is untouched on disk and
    in the tab: the record is the user's, the context was the conversation's.
    """
    slot._pending_discard_conversation_key = session_key
    # A wake's discard re-checks, where it lands, that nobody is waiting on an
    # answer (``chat_runner``); a person's own reset does not.
    slot._pending_discard_from_wake = from_wake
    return (
        "Conversation reset queued. It lands at a turn boundary — normally the "
        "end of this turn, later if a turn is still in flight on the session or "
        "sub-agents are running, queued, or delivering a result. The next "
        "message after it lands starts with no memory of this conversation. The "
        "transcript is untouched — earlier messages stay visible in the tab and "
        "on disk."
    )


async def _apply_chat_tag(state: Any, slot: Any, session_key: str, args: dict[str, Any]) -> str:
    """Apply a ``chat_tag`` directive to THIS turn's slot.

    Mirrors the ``PUT /api/chat/slots/{slot}/tags`` write sequence
    (chat_tags.api_chat_slot_tags): hold the tags write lock across
    resolve→validate→assign→persist, read ``slot.tags`` FRESH inside the lock
    (a concurrent folder/board edit landing mid-apply is the stale-read bug
    class), and push a slots update after persisting.

    Enforces the per-tag agent policy (chat_tags.agent_tag_policy). Named
    refusals surface as the directive's result string: ``tag_policy_denied:<id>``
    (not agent-writable for the requested op), ``tag_grants_unavailable:<id>``
    (the grants store is unreadable, gone, or was quarantined this boot and the
    tag has no row -- a store condition, not a human's reservation) and
    ``unknown_tag:<id>``. A no-op
    (the session already carries exactly the requested state/labels) is audited
    as ``no_op`` but answers with the current tag list ("No change. ...") — the
    documented READ path.
    On success the result includes the session's RESULTING tag names — this is
    also the agent's tag READ path.
    """
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_tag_grants import has_grant_row, refresh_cache, store_degraded
    from kiro_crew.dashboard.chat_tags import (
        _bump_slot_tags_revision,
        agent_tag_grant,
        agent_tag_policy,
        tags_write_lock,
        validate_folder_tag_ids,
    )

    # Identity FIRST, before any suspension point: this directive was
    # authorized against the conversation that produced it, and the grant
    # refresh below is an await — a concurrent rebind landing inside it must
    # not let the capture bind the REBOUND transcript, or the in-lock recheck
    # compares the moved key against itself and passes.
    from kiro_crew.dashboard.chat_utils import slot_history_key

    authorized_history_key = slot_history_key(slot)

    # Pull the grants store read+parse off the event loop ONCE; the sync
    # resolutions below then serve from the installed in-memory snapshot
    # (the resolver is cache-only and touches no filesystem per call). The
    # full json read must not run on the gateway loop.
    await asyncio.to_thread(refresh_cache)

    # A refusal the store cannot vouch for is not a human's decision. When the
    # snapshot is degraded (unreadable, gone, or quarantined this boot) and the
    # tag has NO row, every store-derived refusal (policy, statusness,
    # identity) names the store instead: the agent
    # would otherwise report "human-reserved" for a tag nobody reserved, and
    # the operator would debug a broken feature from gateway logs.
    degraded = store_degraded()

    def _store_refusal(code: str, tag_id: str) -> str:
        if degraded and not has_grant_row(tag_id):
            return f"Error: tag_grants_unavailable:{tag_id}"
        return f"Error: {code}:{tag_id}"

    def _policy_refusal(tag_id: str) -> str:
        return _store_refusal("tag_policy_denied", tag_id)

    set_state = str(args.get("set_state") or "").strip()
    add_ids = [str(t) for t in (args.get("add") or [])]
    remove_ids = [str(t) for t in (args.get("remove") or [])]

    def _sel_self_tag(outcome: str, resources: str = "") -> None:
        try:
            from kiro_crew.sel import sel

            sel().log_api_access(
                caller="mcp-directive",
                operation="chat.self_tag",
                outcome=outcome,
                source="mcp-directive",
                resources=resources,
            )
        except Exception:
            logger.debug("chat_tag SEL api_access audit failed", exc_info=True)

    # Capture the transcript identity of the TURN's slot BEFORE any awaited
    # work: this directive was authorized against the conversation that
    # produced it, and a rebind landing while we wait on the tags lock (or
    # during the persist) must not let the mutation follow the slot to a
    # different transcript. Mirrors the locked_history_key discipline in the
    # chat_handlers metadata endpoints. ``authorized_history_key`` was
    # captured at FUNCTION ENTRY, before the grant-refresh await — capturing
    # it here would already be past that suspension point.

    async with tags_write_lock(state):
        # The slot may have been rebound while we awaited the lock: the write
        # below would target the NEW transcript while this directive's
        # authorization names the old one. Refuse rather than follow.
        if slot_history_key(slot) != authorized_history_key:
            _sel_self_tag("denied", "session_rebound")
            return "Error: session_rebound"
        # Live vocabulary, resolved INSIDE the lock. Map lowercased id AND
        # lowercased display name -> tag dict so requests resolve
        # case-insensitively by either handle (maintainer audit ask from
        # Closure guarantee: a user-created status tag has a uuid id, so
        # name resolution is what keeps it reachable). Names are indexed
        # first and ids second, so an id always wins a collision — ids are
        # the authoritative handle.
        vocab_by_lower: dict[str, dict[str, Any]] = {}
        for t in state._tags:
            tname = t.get("name")
            if isinstance(tname, str) and tname.strip():
                vocab_by_lower.setdefault(tname.lower(), t)
        for t in state._tags:
            tid = t.get("id")
            if isinstance(tid, str):
                vocab_by_lower[tid.lower()] = t

        def _resolve(requested: str) -> dict[str, Any] | None:
            return vocab_by_lower.get(requested.lower())

        def _available() -> str:
            names = [str(t.get("name") or t.get("id")) for t in state._tags if isinstance(t, dict)]
            return ", ".join(n for n in names if n)

        # Validate every requested id exists BEFORE any mutation, so a bad id in
        # a multi-tag call changes nothing.
        for requested in ([set_state] if set_state else []) + add_ids + remove_ids:
            if _resolve(requested) is None:
                _sel_self_tag("denied", requested)
                return (
                    f"Error: unknown_tag:{requested}. No tag named '{requested}' "
                    f"found (case-insensitive, by id or name). "
                    f"Available: {_available()}"
                )

        # Workflow-state tags are mutually exclusive, and `set_state` is the only
        # verb carrying the peer-strip that upholds that invariant. A state id
        # smuggled through `add` would append WITHOUT stripping peers, persisting
        # two exclusive states — refuse and teach the boundary instead. Status-
        # ness here (and at every authorization decision below) is the GRANT
        # STORE's recorded bit, not the tag dict's own field: tags.json is
        # agent-writable, so a forged ``status`` must not re-route which verbs
        # apply or which peers get stripped.
        for requested in add_ids:
            if agent_tag_grant(_resolve(requested))[1]:  # type: ignore[arg-type]
                _sel_self_tag("denied", requested)
                return f"Error: status_tag_requires_set_state:{requested}"

        # Policy: `add` needs add-only or add-remove; `remove` and the implicit
        # removal inside `set_state` need add-remove.
        for requested in add_ids:
            policy = agent_tag_policy(_resolve(requested))  # type: ignore[arg-type]
            if policy not in ("add-only", "add-remove"):
                _sel_self_tag("denied", requested)
                return _policy_refusal(str(requested))
        for requested in remove_ids:
            policy = agent_tag_policy(_resolve(requested))  # type: ignore[arg-type]
            if policy != "add-remove":
                _sel_self_tag("denied", requested)
                return _policy_refusal(str(requested))
        if set_state:
            state_tag = _resolve(set_state)
            # Pre-validation above guarantees every requested id resolves;
            # narrow explicitly for the type checker.
            assert state_tag is not None
            # `set_state` is the workflow-state verb: the requested tag must BE
            # a workflow state, or the peer-strip below would strip real states
            # in exchange for a plain label. One store read answers both the
            # status question and the policy question so the two cannot be
            # satisfied by different sources.
            state_policy, state_is_status = agent_tag_grant(state_tag)
            if not state_is_status:
                _sel_self_tag("denied", set_state)
                return _store_refusal("not_a_status_tag", str(set_state))
            if state_policy != "add-remove":
                _sel_self_tag("denied", set_state)
                return _policy_refusal(str(set_state))
            state_canonical_id = str(state_tag["id"])
            # `set_state=X, remove=[X]` in one call would add X then remove it,
            # leaving the session with NO workflow state — the exact outcome
            # set_state exists to prevent. Refuse the contradictory call.
            for requested in remove_ids:
                if _resolve(requested)["id"] == state_canonical_id:  # type: ignore[index]
                    _sel_self_tag("denied", requested)
                    return f"Error: set_state_conflicts_with_remove:{state_canonical_id}"

        # FRESH read of the slot's current tags inside the lock.
        current: list[str] = list(getattr(slot, "tags", None) or [])
        new_tags: list[str] = list(current)

        def _add(canonical_id: str) -> None:
            if canonical_id not in new_tags:
                new_tags.append(canonical_id)

        def _remove(canonical_id: str) -> None:
            if canonical_id in new_tags:
                new_tags.remove(canonical_id)

        if set_state:
            state_id = _resolve(set_state)["id"]  # type: ignore[index]
            # Mutual exclusivity: strip every OTHER workflow-state tag (any tag
            # carrying status: True), keyed on the LIVE vocabulary rather than a
            # hardcoded id list, then add the requested one. A removed peer that
            # is human-only must NOT be silently stripped — refuse instead.
            for existing in list(new_tags):
                et = _resolve(existing)
                if et is None:
                    continue
                et_policy, et_is_status = agent_tag_grant(et)
                if (
                    not et_is_status
                    and et.get("status") is True
                    and not has_grant_row(str(et["id"]))
                ):
                    # The vocabulary calls this tag a workflow state but the
                    # protected store holds NO row for it (an upgraded install's
                    # custom status tag, or a tag caught between a revoke and
                    # its re-mint). Its identity is UNKNOWN, not "non-status":
                    # treating it as a plain label would leave two exclusive
                    # states on the session. The vocabulary bit is agent-writable
                    # and grants nothing here — it is only ever a reason to
                    # REFUSE. Recovery is owner adoption
                    # (``POST /api/chat/tags/{id}/adopt``); a status PATCH on a
                    # rowless tag answers ``tag_id_not_grantable``.
                    _sel_self_tag("denied", et["id"])
                    return _store_refusal("status_identity_unprotected", str(et["id"]))
                if et_is_status and et["id"] != state_id:
                    if et_policy != "add-remove":
                        _sel_self_tag("denied", et["id"])
                        return _policy_refusal(str(et["id"]))
                    _remove(et["id"])
            _add(state_id)

        for requested in add_ids:
            _add(_resolve(requested)["id"])  # type: ignore[index]
        for requested in remove_ids:
            _remove(_resolve(requested)["id"])  # type: ignore[index]

        if new_tags == current:
            # The documented READ path: a no-op change is how a caller asks
            # for its current tags, so answer with them instead of a bare
            # error (the doc promises this and the code once
            # returned "Error: no_op" without the list). Still audited as
            # no mutation.
            _sel_self_tag("denied", "no_op")
            # String ids only: a malformed (e.g. list-valued) ``id`` loaded
            # from tags.json is unhashable and would raise here; such entries resolve via the ``tid`` fallback instead.
            name_by_id = {
                t.get("id"): (t.get("name") or t.get("id"))
                for t in state._tags
                if isinstance(t.get("id"), str)
            }
            names = [str(name_by_id.get(tid, tid)) for tid in current]
            shown = ", ".join(names) if names else "(none)"
            return f"No change. This session currently carries: {shown}."

        # Pin the persist to the transcript captured at TURN ENTRY (before the
        # lock wait): a slot rebind landing during the awaited save would
        # otherwise deliver this agent's tag mutation to a conversation it
        # never touched. On a refused save, roll the in-memory slot back and
        # mark it dirty so the periodic flush reconverges the durable record
        # to the (restored) live state.
        prior_tags = list(current)
        applied_tags = validate_folder_tag_ids(new_tags, state)
        slot.tags = applied_tags
        # Rotate the revision WITH the mutation: the board's PUT is a
        # compare-and-swap on ``tags_revision``, so a human editing the same
        # session from base R must see this change as a conflict, not
        # overwrite it. Same helper, same discipline as the human path.
        written_tags_revision = _bump_slot_tags_revision(slot)
        applied = await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        )
        if not applied:
            # A refused pin-save means the slot REBOUND (that is the only
            # refusal condition), so nothing was committed and the original
            # transcript on disk is untouched — no reconvergence is owed.
            # Roll memory back, but do NOT mark the rebound slot dirty: a
            # dirty flush would persist this memory onto the DIFFERENT
            # transcript the slot now points at — the same
            # rebound-slot-marked-dirty leak this module closes elsewhere.
            # Only while the slot still holds THIS mutation's value: the save
            # awaited, and a newer concurrent write must not be erased.
            if slot.tags == applied_tags and slot.tags_revision == written_tags_revision:
                slot.tags = prior_tags
                # A fresh revision, not the prior one: a client that adopted
                # the provisional revision from a broadcast treats the prior
                # as a known predecessor and would keep the rejected tags.
                _bump_slot_tags_revision(slot)
            _sel_self_tag("denied", "session_rebound")
            return "Error: session_rebound"

        # Live-alias overwrite hazard: a SECOND live slot bound to
        # the same transcript still holds the pre-update tags in memory, and
        # its next dirty flush would persist those stale tags over the update
        # we just committed. Mirror the applied tags onto every live alias
        # inside this same lock, so any later flush of an alias writes the
        # same (current) state instead of losing it.
        #
        # Convergence-by-flusher: each mirrored alias is ALSO marked dirty, and so is the
        # requester. Durable reconvergence rides the periodic flusher acting
        # on correct-memory slots instead of a synchronous re-save racing to
        # be the last writer — a synchronous re-save adds an await for the
        # next interleaving to exploit. The
        # requester's dirty mark covers the single-slot case (no aliases to
        # reconverge from); a rebound alias cannot leak these tags to a
        # foreign transcript because every flush save is pinned by the slot's
        # own live history key. Residual, stated: a queued stale flush that
        # lands after the pinned commit leaves the DISK stale until the next
        # periodic flush — bounded, self-healing, and the committed state was
        # already durably written once by the pin-save above.
        for other in state._slots.values():
            if other is slot:
                continue
            try:
                if slot_history_key(other) == authorized_history_key:
                    other.tags = list(slot.tags)
                    # The revision travels with the tags: a board edit on the
                    # alias compares against the same base the requester now
                    # carries, and a later alias flush persists a matching pair.
                    other.tags_revision = slot.tags_revision
                    other._dirty = True
            except Exception:
                logger.warning("chat_tag alias tag mirror failed", exc_info=True)
        try:
            slot._dirty = True
        except Exception:
            logger.debug("chat_tag requester dirty-mark failed", exc_info=True)

    _push(state)
    _sel_self_tag("allowed", ",".join(slot.tags))

    # Resulting tag NAMES for the model (the READ path). Fall back to ids for
    # any tag whose vocabulary entry lacks a name. String ids only: a
    # malformed (e.g. list-valued) ``id`` loaded from tags.json is unhashable
    # and would raise here AFTER the mutation committed, reporting failure on
    # a persisted change.
    name_by_id = {
        t.get("id"): (t.get("name") or t.get("id"))
        for t in state._tags
        if isinstance(t.get("id"), str)
    }
    names = [str(name_by_id.get(tid, tid)) for tid in slot.tags]
    shown = ", ".join(names) if names else "(none)"
    return f"Board tags updated. This session now carries: {shown}."


async def _suggest_followup(state: Any, slot: Any, args: dict[str, Any]) -> str:
    from kiro_crew.dashboard.chat_handlers import _redact_followup_item

    items = [_redact_followup_item(i) for i in (args.get("items") or [])]
    if not items:
        return "No follow-up items to show."
    deliver = getattr(state, "deliver_ws_owners", None)
    if deliver is None:
        return "Follow-up card could not be delivered (no owner channel)."
    clients = int(
        await deliver("followup_card", {"slot": slot.key, "items": items, "ts": time.time()})
    )
    if clients == 0:
        return (
            "Follow-up card prepared, but no dashboard client is attached — "
            "restate the follow-ups in your reply text so they are not lost."
        )
    if not getattr(slot, "project", ""):
        # With no project directory the card has no repository to branch, so
        # its first button is "Start in new chat" (FollowUpCard.tsx gates on
        # projectDir): a plain new chat with no worktree. This confirmation is
        # the model's only window into that, so it can describe the card the
        # user actually sees — Research Lab worker slots, for one, are created
        # unscoped (auto_research/handlers.py).
        return (
            "Follow-up card shown below the composer. Note: this session has no "
            "project directory, so the card offers 'Start in new chat' (a new "
            "chat with no worktree) instead of 'Start in new worktree', beside "
            "'Add to this session'. For isolated git work, suggest the user "
            "scope a git repository first (the composer's Project chip)."
        )
    return "Follow-up card shown below the composer."


QUIET_END_REFUSED_USER_TURN = (
    "Error: nothing_to_do was not applied — a person opened this turn, so it "
    "owes them a reply. Answer in text (even one line) instead."
)


def _nothing_to_do(
    args: dict[str, Any],
    *,
    producer_is_user_facing: bool,
    producer_is_self_wake: bool,
    producer_is_channel: bool,
) -> DirectiveOutcome:
    """Record the deliberate quiet end of this turn.

    THE ONE GATE IS WHO OPENED THE TURN. A quiet end is for a turn nobody is
    waiting on: a monitor wake (``producer_is_self_wake``) or a headless
    producer (a cron, a crew runtime, an app or task-runner injection — neither
    user-facing nor a channel). A turn a PERSON opened owes that person a reply,
    and the contract says so; enforcing it here rather than in prompt text is
    what keeps a model misfire a VISIBLE failure: the refusal leaves
    ``ends_turn`` False, so the runner's empty-response ladder, the Resume
    control and the channel's empty-turn notice all run exactly as they did
    before this directive existed. A channel turn is refused too, because the
    channel driver cannot yet tell a human's message from a loop's wake — the
    conservative answer until it carries that provenance; the patrol then gets
    the pre-existing "ended without a closing reply" notice, not silence.

    No slot or surface gate otherwise: the quiet end mutates nothing, so there
    is no effect a wrong identity could misdirect. The outcome text is what the
    transcript's tool step shows — the low-key, inspectable record that the turn
    ended on purpose — and the ``note`` the model supplied rides on it. It is
    not a chat message: the consumer renders no assistant bubble, no notice
    card and no continuation for a turn that ends here.
    """
    opened_by_person = producer_is_user_facing or producer_is_channel
    if opened_by_person and not producer_is_self_wake:
        raise _DirectiveDenied(QUIET_END_REFUSED_USER_TURN)
    note = str(args.get("note") or "").strip()
    text = QUIET_END_OUTCOME_PREFIX if not note else f"{QUIET_END_OUTCOME_PREFIX} {note}"
    return DirectiveOutcome(text, ends_turn=True)


def _push(state: Any) -> None:
    push = getattr(state, "push_slots_update", None)
    if push is not None:
        try:
            push()
        except Exception:
            logger.debug("push_slots_update failed", exc_info=True)
