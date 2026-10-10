"""The run views: ``GET /api/spawn/{id}`` with its result view, and the ``GET
/api/spawn`` listing that the Subagents panel and ``spawn_list`` read.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.messaging import (
        _SPAWN_STATUS_MAX_GREP_LEN,
        _SPAWN_STATUS_MAX_LINES,
        PERSISTED_SUBAGENT_REPLAY_KEEP,
        PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
        DashboardState,
        PanelRecords,
        QueuedReadUnavailable,
        QueuedRunListing,
        _agent_dir,
        _audit_allow,
        _audit_deny,
        _queue_unreadable,
        _queued_lookup,
        _queued_run_payload,
        _queued_runs,
        _run_belongs_to_caller,
        classify_persisted_ending,
        internal_memory_scope,
        is_sensitive_path,
        logger,
        persisted_replay_denial_reason,
        persisted_snapshot_denial_reason,
        read_panel_records,
        read_state,
        read_tombstone,
        redact_credentials,
        redact_exfiltration_urls,
        slot_owner_snapshot,
        subagent_event_slot,
    )


def _redact(text: str) -> str:
    """Two-pass redaction for LLM-derived content on external surfaces."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _spawn_result_view(text: str, offset: int, limit: int, grep: str) -> tuple[str, dict]:
    """Apply optional grep (regex line filter) then offset/limit line slicing.

    Line-oriented, like reading code: *offset* is a 0-based start line and *limit*
    caps returned lines (0 = to end, hard-capped at ``_SPAWN_STATUS_MAX_LINES``).
    When *grep* is set, lines are filtered by a case-insensitive regex first, then
    offset/limit apply to the matches. Returns ``(view_text, meta)``; on a bad
    regex ``meta['grep_error']`` is set and *view_text* is empty. Pure CPU — run
    via ``asyncio.to_thread`` so a pathological regex never stalls the loop.
    """
    lines = text.splitlines()
    total = len(lines)
    if grep:
        try:
            pat = re.compile(grep[:_SPAWN_STATUS_MAX_GREP_LEN], re.IGNORECASE)
        except re.error as exc:
            return "", {"grep_error": f"invalid grep regex: {exc}"}
        lines = [ln for ln in lines if pat.search(ln)]
    meta: dict = {"total_lines": total}
    if grep:
        meta["matched_lines"] = len(lines)
    start = min(max(0, offset), len(lines))
    span = _SPAWN_STATUS_MAX_LINES if limit <= 0 else min(limit, _SPAWN_STATUS_MAX_LINES)
    end = min(len(lines), start + span)
    meta["offset"] = start
    meta["returned_lines"] = end - start
    meta["has_more"] = end < len(lines)
    return "\n".join(lines[start:end]), meta


async def _apply_result_view(request: web.Request, text: str) -> tuple[str, dict]:
    """Read offset/limit/grep query params and apply :func:`_spawn_result_view`.

    Returns ``(text, {})`` unchanged when no paging/filter params are present, so
    the default ``spawn_status`` contract (full transcript) is preserved. Only a
    paged/filtered request pays the split+regex cost, offloaded to a thread.
    """

    def _q_int(name: str) -> int:
        try:
            return max(0, int(request.query.get(name, 0)))
        except (TypeError, ValueError):
            return 0

    offset = _q_int("offset")
    limit = _q_int("limit")
    grep = (request.query.get("grep") or "").strip()[:_SPAWN_STATUS_MAX_GREP_LEN]
    if not (grep or offset > 0 or limit > 0):
        return text, {}
    return await asyncio.to_thread(_spawn_result_view, text, offset, limit, grep)


async def api_spawn_status(request: web.Request) -> web.Response:
    """GET /api/spawn/{id} — poll subagent status."""
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"error": "subagents not available"}, status=503)
    agent_id = request.match_info["agent_id"]
    info = state.subagents.get(agent_id)
    if not info:
        # Accepted but not started: the gate deferred it, or it waits for a
        # slot. It has no run folder, so the persistence fallback below would
        # answer 404 for a spawn the caller was just told is queued. The scope
        # guard already looked it up for an internal caller.
        try:
            queued = await _queued_lookup(request, state, agent_id)
        except QueuedReadUnavailable:
            return _queue_unreadable()
        if queued is not None:
            return web.json_response(_queued_run_payload(queued))
        # The pump can register the run while that lookup awaited; the
        # registry then answers, not the half-written folder.
        info = state.subagents.get(agent_id)
    if not info:
        # Fall back to persistence layer (orphaned/recovered agents)
        try:
            disk_state = read_state(agent_id)
            if disk_state:
                disk_data: dict[str, object] = {
                    "id": agent_id,
                    "task": _redact(disk_state.get("task", "")),
                    "done": True,
                    "started": disk_state.get("started"),
                }
                tombstone = await asyncio.to_thread(read_tombstone, agent_id) or {}
                # Legacy persisted records do not carry terminal usage. Keep
                # those fields absent rather than presenting invented zeros.
                for field in ("elapsed", "credits"):
                    if field in tombstone:
                        value = tombstone[field]
                        if (
                            not isinstance(value, bool)
                            and isinstance(value, (int, float))
                            and math.isfinite(value)
                            and value >= 0
                        ):
                            disk_data[field] = float(value)
                agent_dir = _agent_dir(agent_id)
                result_path = agent_dir / "result.txt"
                result = ""
                if result_path.exists() and not is_sensitive_path(str(result_path)):
                    try:
                        result = await asyncio.to_thread(
                            result_path.read_text, encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        pass
                # _redact() is defined in this module; calls both
                # redact_exfiltration_urls() and redact_credentials() per security guidelines.
                view, view_meta = await _apply_result_view(request, result)
                if view_meta:
                    disk_data["result_meta"] = view_meta
                disk_data["result"] = _redact(view) if view else "_No result._"
                # One classifier, shared with the panel list. Reading the
                # tombstone here as well let the same folder answer "completed"
                # in a list and "Orphaned: delivered" when opened, and flattened
                # a recorded user stop into a failure.
                outcome, error, stopped = await asyncio.to_thread(
                    classify_persisted_ending, agent_dir
                )
                if not outcome:
                    # Nothing recorded an ending, so this run is not known to be
                    # over: it may be registering this instant, or be an earlier
                    # process's orphan the reconcile has not reached. Not done,
                    # with what IS known and no outcome: a caller that read
                    # ``done`` here would collect a result that is still coming.
                    disk_data["done"] = False
                    disk_data["result"] = _redact(view) if view else ""
                    disk_data.pop("outcome", None)
                    disk_data["stopped"] = False
                    disk_data["error"] = ""
                    return web.json_response(disk_data)
                disk_data["outcome"] = outcome
                disk_data["stopped"] = stopped
                disk_data["error"] = _redact(error) if error else ""
                return web.json_response(disk_data)
        except Exception:
            logger.debug("Persistence fallback failed for %s", agent_id, exc_info=True)
        return web.json_response({"error": "not found"}, status=404)
    data = {"id": info.id, "task": _redact(info.task), "done": info.done}  # type: dict[str, object]
    data["started"] = info.started
    if info.done:
        data["elapsed"] = info.elapsed
        data["credits"] = info.credits
        # Read full result from disk (info.result is truncated to 3000 chars)
        result = info.result
        if info.result_path and not is_sensitive_path(info.result_path):
            try:
                result = await asyncio.to_thread(
                    Path(info.result_path).read_text,
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                pass
        view, view_meta = await _apply_result_view(request, result)
        data["result"] = _redact(view)
        if view_meta:
            data["result_meta"] = view_meta
        data["error"] = _redact(info.error) if info.error else ""
    else:
        data["turns"] = info.turns
        data["last_tool"] = _redact(info.last_tool)
        data["elapsed"] = round(time.time() - info.started)
        partial = _redact(getattr(info, "streaming_text", ""))
        view, view_meta = await _apply_result_view(request, partial)
        data["result"] = view
        if view_meta:
            data["result_meta"] = view_meta
        # Same predicate, same present-only-while-true convention as
        # api_spawn_list. This endpoint is the one a blocking `kirocrew spawn
        # run` polls every 2s (cli_commands.py), so leaving it out would keep the
        # CLI silent: the caller would sit on "waiting for result..." while the
        # answer ("a prompt is waiting for you") was only discoverable from a
        # separate `spawn list` or a log grep.
        if _awaiting_spawn_approval(info):
            data["awaiting_approval"] = True
    return web.json_response(data)


def _awaiting_spawn_approval(info: object) -> bool:
    """True only while a run is parked on the SPAWN-approval gate.

    ``_awaiting_approval`` alone is NOT sufficient: ``run.py`` sets the same
    flag for TOOL approvals raised INSIDE a running subagent (one approval
    watch serving its three approvers), so
    reading it bare would report a run at turn 5 waiting on a tool prompt as
    though it were waiting to START -- rendering "waiting for spawn approval"
    and telling a caller to approve it "to start this run" that already
    started. ``_exec_started`` is the permanent discriminator: it is stamped
    once when execution begins, so ``None`` means the run never entered
    execution, which for a registered run is only reachable via the spawn gate.

    Both read paths go through this ONE predicate rather than repeating the
    pair, because the two handlers build their payloads independently and a
    drift between them is invisible to a behavioural test.

    The manager package has its own copy, ``subagent._parked_at_spawn_approval``,
    read by ``subagent_manager/terminal.py`` (the reap message),
    ``subagent_manager/cancellation.py`` (the parent-end paths) and the kernel
    memory-pressure hold. Deliberately not imported from there: this handler
    layer does not reach into the manager's private helpers. The duplication is
    two lines and both sites name each other.

    ``getattr`` with a strict ``is True`` / ``is None``: these handlers are
    exercised with lightweight info doubles (SimpleNamespace / MagicMock) that
    carry only the fields a case cares about, so a bare attribute read raises
    and a truthy Mock would otherwise advertise a wait that isn't happening.
    """
    return (
        getattr(info, "_awaiting_approval", False) is True
        and getattr(info, "_exec_started", None) is None
    )


async def api_spawn_list(request: web.Request) -> web.Response:
    """GET /api/spawn — list all subagents."""
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"agents": []})
    # Called for its REFUSAL: it rejects an internal caller whose execution
    # identity cannot be verified. The store it resolves is deliberately not kept
    # as the ownership gate -- a verified internal caller on the default store
    # answers an empty store, which is indistinguishable from the dashboard
    # owner's absent one, so gating on it admits the very callers the bound is
    # for. ``_admit`` gates on ``internal_auth`` instead, the signal the live
    # branch below already uses on the same field.
    _store, refusal = await internal_memory_scope(request, "spawn.list")
    if refusal is not None:
        return refusal
    agents = []
    caller = request.headers.get("X-Session-Key", "")
    # An internal caller lists only the runs it may control, by the same
    # ownership rule the per-run routes apply: its own runs, or -- with no
    # identity at all -- only runs no session started. Listing is a read, but a
    # run id, its task text and its parent key are exactly what a later steer
    # needs, so the list must not hand out what the control route would refuse.
    # The dashboard owner (no ``internal_auth``) still sees everything.
    internal = request.get("internal_auth") is True
    # The queued half is opt-in (``?queued=1``): only the spawn tools act on it,
    # and the dashboard's pollers would otherwise pay a store read every few
    # seconds to throw it away. Read BEFORE the live registry: a spawn that
    # registers between the two reads is then live in the second (live wins
    # below), where the other order would show it in neither.
    queued_listing: QueuedRunListing | None = None
    if request.query.get("queued") in ("1", "true"):
        queued_listing = await _queued_runs(
            state, caller if internal else None, app=str(request.get("app") or "") or None
        )
    for info in state.subagents.all_agents:
        if internal and not _run_belongs_to_caller(caller, info.id, info.parent_session_key):
            continue
        entry: dict[str, object] = {
            "id": info.id,
            "task": _redact(info.task),
            "done": info.done,
            "parent": info.parent_session_key,
            # The tab the run's WS frames are slotted to -- the chat at the ROOT
            # of its spawn tree, named with the same mapping the gateway applies
            # -- so the dashboard compares slot to slot. A nested run's
            # ``parent`` is a ``subagent:<id>`` no tab shows, so a reconcile
            # keyed on ``parent`` would evict the nested card the frames just
            # painted; and a raw key compared to ``dashboard:<slot>`` misses
            # every cron- and channel-born tab, whose slot is not a prefix strip
            # of its key, so the reconcile would fail a live card as untracked.
            "slot": subagent_event_slot(state.subagents.root_session_key_for(info)),
            "agent": info.agent or info.crew,
            "started": info.started,
        }
        if info.done:
            entry["result"] = _redact(info.result)
            entry["error"] = _redact(info.error) if info.error else ""
            entry["stopped"] = info.user_stopped
            entry["outcome"] = info.outcome
        else:
            entry["turns"] = info.turns
            entry["last_tool"] = _redact(info.last_tool)
            entry["elapsed"] = round(time.time() - info.started)
            # Present only while the run is parked on its spawn-approval
            # prompt, so the default payload is unchanged. Without it a run
            # waiting for a human is byte-identical to one that is executing --
            # `kirocrew spawn list` would show the same hourglass for a run that
            # has no child process and is only ever waiting to be approved.
            if _awaiting_spawn_approval(info):
                entry["awaiting_approval"] = True
        # Present only when a group was actually withheld, so the default
        # (everything on) payload is unchanged.
        withheld = [
            group
            for group, on in (
                ("memory", info.include_memory),
                ("lessons", info.include_lessons),
                ("project", info.include_project),
            )
            if not on
        ]
        if withheld:
            entry["context_withheld"] = withheld
        agents.append(entry)
    # The audit identity is the APP, never the caller-supplied session key. The
    # dedup registry behind ``_audit_deny`` is keyed on it and is not evicted, so a
    # per-run session id would leave one permanent entry per subagent run. Every
    # other call site in the tree passes a bounded app id for the same reason.
    auditee = str(request.get("app") or "<owner>")

    # An app-authenticated caller and the dashboard owner BOTH reach this route
    # with `scope is None` -- `internal_memory_scope` answers that for a
    # non-internal caller and for a verified session whose execution record is
    # empty -- so scope alone cannot tell them apart. The app claim can, and it
    # is the instrument the rest of the tree uses: `derive_caller_app` states
    # that app-ownership checks gate on `request["app"]`. Publication is
    # narrowing-only -- every transport sets the claim ONLY for a positively
    # resolved app and leaves it absent for the person -- so a present non-empty
    # claim is itself the positive signal, the same one the middleware inverts
    # into `is_dashboard_user`. A transport flag is the wrong question here: it
    # answers which credential arrived, and three arms publish a validated app
    # claim without it.
    caller_app = str(request.get("app") or "")
    caller_is_app = bool(caller_app)

    # Slot ownership is read on the LOOP, twice, and never from the worker
    # thread. Once here as a snapshot, so the row cap is sized over the records
    # this caller may actually see; then again after the thread returns, which is
    # the authoritative check. A slot's owner can flip while the scan runs --
    # keys are caller-supplied and not app-namespaced, so another app can reclaim
    # one -- and a decision taken off-loop would be read from state the caller
    # does not describe. The WS replay reads the same pair for the same reason.
    owner_now = slot_owner_snapshot(state)

    # Accepted spawns with no run yet: deferred by the memory gate, waiting for
    # a slot, or claimed and not registered. They go under their own key rather
    # than into ``agents``, whose readers (the dashboard's reconcile and agent
    # strip) take a not-done entry for a run in progress. Same bounds as the
    # other halves: an internal caller sees only its own, and an app caller only
    # its app's (filtered in the store read, before its cap). Each row is a
    # permission decision, audited under its own reason.
    live_ids = {str(entry["id"]) for entry in agents}
    queued_entries: list[dict[str, object]] = []
    for queued in queued_listing.runs if queued_listing is not None else ():
        if queued.id in live_ids:
            continue
        if caller_is_app and queued.app != caller_app:
            _audit_deny(auditee, "api_spawn_list", "queued_app_mismatch")
            continue
        if internal and not _run_belongs_to_caller(caller, queued.id, queued.parent_session_key):
            _audit_deny(auditee, "api_spawn_list", "queued_scope_mismatch")
            continue
        _audit_allow(auditee, "api_spawn_list")
        queued_entry = _queued_run_payload(queued)
        queued_entry["parent"] = queued.parent_session_key
        queued_entries.append(queued_entry)
    # Durable half of the inventory: the runs this process never tracked, which
    # a memory-only listing cannot name at all. Live and queued entries win --
    # an id listed above is excluded rather than merged (a row a restart left
    # claimable beside its orphan tombstone will run again, so it is queued) --
    # and the caller's own scope gate is re-applied here on the record's parent,
    # the same field the live branch compares.
    listed = live_ids | {str(entry["id"]) for entry in queued_entries}

    def _admit(record: dict) -> bool:
        """This caller's own visibility, applied before the cap.

        Two bounds with different reach. The app bound is unconditional, because
        an app token must never read another app's run text no matter how its
        session scope resolved. The session bound is conditional on
        ``internal_auth`` -- the same condition the live branch above applies to
        the same field -- so the durable half of one listing is neither wider nor
        narrower than the live half a caller sees beside it. It is deliberately
        NOT conditional on the resolved memory store: that answers empty for a
        verified internal caller on the default store exactly as it does for the
        dashboard owner, so it would lift the bound for nearly every attested
        caller it exists to bind.

        Both read only the request's own values and the record, so they are sound
        on a worker thread. The ownership dimension is not: it answers from live
        slot state, so here it consults the loop-taken snapshot and sizes the cap
        only, and the record is decided again on the loop before it is listed.

        Every refusal is a permission decision and leaves a SEL record under the
        reason it actually had -- a lazily hydrated slot is `slot_missing`, not an
        ownership breach.
        """
        if caller_is_app and str(record["app"] or "") != caller_app:
            _audit_deny(auditee, "api_spawn_list", "persisted_app_mismatch")
            return False
        if not internal:
            return True
        parent = str(record["parent_session"])
        agent_id = str(record["id"])
        if parent != caller and caller != f"subagent:{agent_id}":
            _audit_deny(auditee, "api_spawn_list", "persisted_scope_mismatch")
            return False
        # The ownership dimension, sized off the loop snapshot. Withholding is
        # itself the permission decision, so it audits here under the reason it
        # had; the surviving records are decided again on the loop, and the two
        # sets are disjoint, so no record is audited twice.
        denial = persisted_snapshot_denial_reason(owner_now, subagent_event_slot(parent), record)
        if denial:
            _audit_deny(auditee, "api_spawn_list", denial)
            return False
        return True

    try:
        persisted = await asyncio.to_thread(
            read_panel_records,
            keep=PERSISTED_SUBAGENT_REPLAY_KEEP,
            max_age_secs=PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
            exclude_ids=listed,
            include_result=True,
            admit=_admit,
        )
    except Exception:
        logger.debug("Persisted spawn listing failed", exc_info=True)
        persisted = PanelRecords([], 0, False)
    for record in persisted.records:
        parent = str(record["parent_session"])
        agent_id = str(record["id"])
        if internal:
            # The authoritative gate, on the loop, against state as it is NOW
            # rather than as the snapshot found it. A record the snapshot
            # admitted and this rejects had its slot reclaimed mid-scan. Same
            # reused-slot-key exposure the replay guards, reached here through the
            # parent session rather than a frame.
            denial = persisted_replay_denial_reason(state, subagent_event_slot(parent), record)
            if denial:
                _audit_deny(auditee, "api_spawn_list", denial)
                continue
        # The GRANT is a permission decision too, and the one an operator needs to
        # reconstruct who was handed a persisted run's text. Recording only the
        # refusals leaves the admissions invisible, so a review of this stream can
        # show what was blocked and never what was released.
        _audit_allow(auditee, "api_spawn_list")
        error = str(record["error"])
        agents.append(
            {
                "id": agent_id,
                "task": _redact(str(record["task"])),
                "done": True,
                "parent": parent,
                "agent": _redact(str(record["agent"])),
                "started": record["started"],
                "result": _redact(str(record.get("result") or "")),
                "error": _redact(error) if error else "",
                # The tombstone records the run's own outcome, so a user stop
                # stays a stop here rather than being flattened into a failure.
                "stopped": bool(record.get("stopped")),
                "outcome": record["outcome"],
            }
        )
    payload: dict[str, object] = {"agents": agents}
    if queued_entries:
        payload["queued"] = queued_entries
    if queued_listing is not None and queued_listing.partial:
        # A page or an outage, and said so: unlike the persisted half, the
        # caller of this listing acts on it, and a cut-off or unread tail taken
        # as complete tells it accepted spawns were never accepted -- the
        # reading that gets work dispatched twice. Present only when true; the
        # bridge logs the transition once.
        payload["queued_truncated"] = True
    if persisted.overflow or persisted.overflow_is_lower_bound:
        # Said out loud once per listing, to the operator rather than the client:
        # a listing of 50 of 51 eligible runs otherwise reads exactly like a
        # listing of all 50 there were. The WARNING carries the count, and it is
        # the whole report: a truncation refuses nobody, so it is not a permission
        # decision and does not belong in the SEL deny stream beside the ownership
        # refusals an operator has to be able to see there. No client reads a
        # count it cannot act on, so it stays out of the payload too.
        # A saturated scan window reports even at a count of zero, because that
        # is the case where the count itself cannot see what was left out.
        logger.warning(
            "persisted spawn listing truncated: %s%d eligible run(s) past the %d cap%s",
            "at least " if persisted.overflow_is_lower_bound else "",
            persisted.overflow,
            PERSISTED_SUBAGENT_REPLAY_KEEP,
            (
                " (scan window saturated, older admissible runs may be uninspected)"
                if persisted.overflow_is_lower_bound
                else ""
            ),
        )
    return web.json_response(payload)
