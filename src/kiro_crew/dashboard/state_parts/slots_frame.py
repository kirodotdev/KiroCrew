"""What a slots frame contains: serialised rows and views, links and lineage parents.

Channel-link labels, redaction and nonces, the lineage parents (with the one-shot
seed request and its failure latch, rebound only here), and the frame and note
shape with its size-paced interval.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.dashboard import DashboardState
    from kiro_crew.dashboard._types import SubagentManager

logger = logging.getLogger("kiro_crew.dashboard.state")


_CHANNEL_LABELS = {
    "slack": "Slack",
    "discord": "Discord DM",
    "telegram": "Telegram",
    "teams": "Microsoft Teams",
    "webex": "Webex",
    "wecom": "WeCom",
    "weixin": "WeChat",
    "imessage": "iMessage",
    "whatsapp": "WhatsApp",
    "feishu": "Feishu",
}


def _slots_serialization_note(slots_data: object, *, path: str = "slots-broadcast") -> str:
    """Name the slot and field that broke JSON serialization, for a traceback note.

    A dump failure on the slots projection means EVERY slots read path is broken
    — the coalesced broadcast, the dashboard-user WS frame (``_slots_ws_frame``),
    the WS connect snapshot, and ``GET /api/chat/slots`` all serialize the same
    shape — and the stock message — "Object of type X is not JSON serializable"
    — names neither the slot nor the field, so such a failure reads as a
    broadcast bug. All four paths route their dump failure through this note;
    ``path`` names the one that raised. Values are withheld
    by design: slot state can carry user text, and the type is enough to find
    the producer.

    Every caller passes ``serialize_slots()`` output (list-of-dicts by
    construction), so the walk assumes that shape rather than re-checking it.
    Diagnosis must never make the failure worse, so any surprise in the walk —
    including a caller breaking that assumption — degrades to the generic
    note below instead of raising.
    """
    try:
        # Narrowing, not validation: every caller passes a list by construction,
        # and a violation lands in the defensive except below (degrade, never
        # raise) exactly like any other surprise in the walk.
        assert isinstance(slots_data, list)
        for i, entry in enumerate(slots_data):
            try:
                json.dumps(entry)
                continue
            except (TypeError, ValueError):
                pass
            key = entry.get("key")
            slot_name = key if isinstance(key, str) else f"#{i}"
            for field, value in entry.items():
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    return (
                        f"[{path}] slot {slot_name!r} field {field!r} is not "
                        f"JSON-serializable: type {type(value).__name__} (value withheld)"
                    )
            # Every field dumps on its own, yet the entry does not: a non-string
            # key is the one shape that gets here.
            return (
                f"[{path}] slot {slot_name!r} fails serialization as a whole; "
                "no single offending field — check for non-string dict keys (value withheld)"
            )
        return f"[{path}] slot list fails serialization; no offending entry found"
    except Exception:  # defensive: a diagnostic must not raise
        return f"[{path}] slot projection is not JSON-serializable (offender walk failed)"


#: Guards :func:`_request_lineage_seed` so a burst of slot frames arriving before the
#: projection is seeded costs ONE seed rather than one per frame.
_lineage_seed_lock = threading.Lock()


_lineage_seed_in_flight = False


#: Latches :func:`_attach_slot_parents`'s failure WARNING to once per process. The
#: function runs on every slots frame, so the line is worth a warning the first time
#: and worth nothing the thousandth. Not reset when the store changes: the point is one
#: report per process that something is wrong, not a per-store tally.
_lineage_failure_warned = False


def _published_queued_by_slot(subs: "SubagentManager | None") -> dict[str, int]:
    """Each slot's ``subagents_queued``: the manager's last published depths, by tab.

    Routed exactly as the ``subagent_queued`` frames are (``subagent_event_slot`` of
    the frame's parent), so a row reports what the frame stream told that tab. The
    two keys differ for a cron tab: a stateless or agent-sequence run publishes
    under ``cron:<job>:<run>`` while the tab's own key is ``cron:<job>``, and
    several runs of one job sum on the job's tab.
    """
    if not subs:
        return {}
    table = subs.published_queued_depths()
    # circular import: chat_utils imports this module at load time.
    from kiro_crew.dashboard.chat_utils import subagent_event_slot

    by_slot: dict[str, int] = {}
    for parent, depth in table.items():
        if depth > 0:
            slot = subagent_event_slot(parent)
            by_slot[slot] = by_slot.get(slot, 0) + depth
    return by_slot


def _attach_slot_parents(
    rows: "list[dict]", resolve_aliases: "Callable[[], dict[str, str] | None] | None" = None
) -> None:
    """Give every slot row its ``parent`` -- ``{slot, key}`` or ``None``. IN PLACE.

    This is what lets the chat sidebar nest a session under the one that opened it
    through ``session_create``: the sidebar already receives the slots broadcast, so
    the edge rides a frame it gets anyway rather than a route it would have to poll.

    The SHAPE is byte-identical to the Sessions table's ``parent`` -- same keys, same
    meaning, same ``key: None`` for a creator that is not running under a chain with
    nothing open in it, or one that sits on a cycle -- because one moved ``nestsUnder``
    serves both views and a second shape would be a second way to nest the same
    gateway. A third key, ``ancestor``, rides the one case where ``key`` and ``slot``
    name different sessions: the creator in ``slot`` has CLOSED and ``key`` is the
    nearest session above it still open, so the row nests where the run is owned and
    still cites a creator that is gone (see
    :func:`~kiro_crew.crew_log.session_tree.parent_payload`). It is omitted, never
    ``False``, so an ordinary edge is the payload it has always been.

    What differs between the two views, necessarily, is the
    KEY SPACE: ``key`` names the creator's row IN THIS PAYLOAD, so here it is the bare
    slot key and on the memory payload it is the full ``dashboard:`` session key.
    ``nestsUnder`` resolves ``parent.key`` against its own payload's keys, so that is
    the invariant it needs; a session key here would name no row and every child would
    silently detach.

    Whole-population work, so it lives after the per-slot loop rather than inside
    :meth:`DashboardState.serialize_slot`: resolving a citation to a LIVE creator needs
    every row's key, which one slot does not have.

    NO DISK, and no blocking, because this runs on the event loop. The lineage is read
    only when the projection is ALREADY seeded for the store now configured; otherwise
    every row ships ``parent: None`` for this frame and the seed is handed to the
    maintenance pool. Seeding is a checkpoint load plus a names-only listing, or on the
    very first boot of this build one full scan -- work measured in milliseconds but
    still disk, and disk on this loop stalls every other request and the heartbeat
    behind it.

    The seed landing does not broadcast a slots frame. Every path that would --
    ``push_slots_update``, the trailing timer -- writes the coalescer's clock, and that
    is load-bearing elsewhere: a request inside ``suspend_slots_push`` needs that window
    open so its own flush broadcasts INLINE and a broadcast failure reaches its caller,
    and the create path pins exactly one coalesced frame per change.

    Instead the frame SAYS it is provisional: while the projection is not seeded for this
    store, every row carries ``lineage_pending: true``. When the seed lands, the
    projection announces it on the crew-log bus and :meth:`DashboardState.push_lineage_patch`
    sends the settled rows as a ``slot_patch`` -- a frame outside the coalescer -- so an
    IDLE gateway, where no other frame is coming, still nests without a client asking
    again. The flag is set only when a later answer would genuinely differ -- never with
    the crew log off, and never after a failure this cannot promise will clear.

    Nor is the seed started at boot, which would close that one-frame window: seeding is
    bound to one store and re-runs when the data home changes, so a process serving
    several homes would queue a full cold scan per bind onto the shared maintenance pool.

    *resolve_aliases* answers ``DashboardState.spend_slot_by_session()`` -- session key
    to slot key -- and it is the SAME correspondence the Sessions table's payload hands
    the same join. It is what carries the spellings this payload's own keys do not: a
    conductor whose turns run on a channel conversation is cited by that channel key, and
    a dashboard session can be cited by its ``dashboard:`` spelling, neither of which is a
    slot key. Without it the join answered "creator not running" for exactly those
    conductors while the Sessions table nested their workers from the same fold, and the
    two views disagreed about one gateway at one moment.

    It is a CALLABLE rather than the mapping itself so the read happens inside this
    function's own failure boundary. A fault while resolving it is a fault in the
    nesting, and the paragraph below is what the whole path owes such a fault: unnested
    rows plus one WARNING carrying the traceback. Reading it at the call site instead
    would put that one fault outside the boundary and take the entire sidebar down with
    it. ``None`` is accepted so a caller with no registry to ask still gets every row's
    key, and a value that is not a mapping is discarded the same way -- a state double's
    attribute call can answer with another mock.

    Never raises, and every row gets the key either way. A sidebar that cannot paint is
    a worse failure than a sidebar that does not nest, and a row silently MISSING the
    key would make the frontend's ``parent === undefined`` mean two different things.

    A failure IS reported, once per process at WARNING with its traceback, because it is
    the only outcome this leaves no evidence of: the payload it produces is
    byte-identical to a store that genuinely holds no lineage, so without the line a
    missing conductor lane cannot be told from a gateway with nothing to nest.
    """
    if not rows:
        return
    parents: dict = {}
    # Set ONLY when a later read would answer differently, which is now two cases: the
    # projection is not seeded for this store yet, and a seed that FAILED is due for
    # another attempt. Both have a seed asked for behind them, so the promise the flag
    # makes is one something is working to keep. With the crew log off it stays off -- a
    # client must never be told to come back for an answer that will never change.
    pending = False
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        # Checked BEFORE the storage import, the way the memory payload's ``_lineage``
        # does it. With the crew log off there are no records to read and never will
        # be, so seeding would scan a store nobody is writing -- and every row's answer
        # is the same ``None`` either way.
        if not crew_log_emit.enabled():
            for row in rows:
                row["parent"] = None
            return

        from kiro_crew.crew_log.session_tree_projection import projection
        from kiro_crew.dashboard.session_memory import lineage_parents

        # Inside the boundary on purpose: see the docstring. A mapping is required, so a
        # state double answering with another mock is discarded rather than joined on.
        aliases = resolve_aliases() if resolve_aliases is not None else None
        if not isinstance(aliases, dict):
            aliases = None

        proj = projection()
        if proj.seeded_for_current_store:
            # A child minted and not yet run has no node, and the join nests it from
            # the row's own witnessed ``created_by`` -- see ``lineage_parents``.
            parents = lineage_parents(rows, proj.nodes(), aliases)
            # A seed that FAILED leaves a readable but EMPTY state, so the check above
            # is satisfied and this path would otherwise never ask for another one --
            # the projection's own retry is reached only by a caller that seeds, and
            # the one that does is the System page's sampler. A sidebar on a gateway
            # nobody opens that page on would stay unnested for the life of the
            # process. Asking here is what makes the retry reach this payload.
            if proj.seed_retry_due:
                _request_lineage_seed()
                pending = True
        else:
            _request_lineage_seed()
            # Say that this frame's answer is PROVISIONAL, so a reader can come back for
            # the real one. Without it a cold start is indistinguishable from a store
            # with no lineage at all, and an idle sidebar -- nothing running, no frame
            # coming -- paints unnested and stays that way until the user happens to act.
            # The seed's announcement pushes the settled rows (see above).
            pending = True
    except Exception:
        # Reached only by a genuine failure. The crew log being off returns above, so
        # this is a broken read, not a configuration -- and it is the ONE outcome of
        # this function that leaves no trace a reader can find. A parent, an explicit
        # ``None`` and ``lineage_pending`` are all visible in the payload, so a
        # sidebar that never offers the conductor lane is diagnosable from the wire in
        # every case but this one, where the payload is byte-identical to a store that
        # genuinely holds no lineage. Report it at WARNING, with the traceback: the
        # alternative is what actually happened, an investigation that could not tell
        # a swallowed exception from an empty store.
        #
        # Latched to once per process because this runs on EVERY slots frame, and a
        # failure here is far more likely to be persistent (a bad store, an import
        # that cannot load) than one-off, so an unlatched WARNING would fill the log
        # with one line per broadcast. Later occurrences keep the traceback at DEBUG.
        global _lineage_failure_warned
        if not _lineage_failure_warned:
            _lineage_failure_warned = True
            logger.warning(
                "slot lineage could not be resolved; slots ship without parents and "
                "the chat sidebar will not offer its conductor lane",
                exc_info=True,
            )
        else:
            logger.debug("slot lineage could not be resolved", exc_info=True)
    for row in rows:
        key = row.get("key")
        row["parent"] = parents.get(key) if isinstance(key, str) else None
        # Omitted rather than sent as False, the same distinction `parent` keeps: the
        # ordinary frame carries no flag at all, so nothing is added to the steady state.
        if pending:
            row["lineage_pending"] = True


def _request_lineage_seed() -> None:
    """Seed the session-tree projection on the maintenance pool. Returns immediately.

    One seed in flight at a time. The guard is an in-flight flag rather than a
    once-per-process latch, because the projection legitimately needs re-seeding when
    the data home changes underneath the process -- a latch would leave the sidebar
    permanently un-nested after a pod or a relocated home, which is the bug the flag
    avoids while still collapsing a burst of frames into one seed.

    Never raises. A pool that will not take the job leaves the flag clear so the next
    caller can try, and until some seed succeeds every row simply ships no parent.

    A no-op with the crew log off: there are no records to fold, so seeding would only
    scan a store nobody is writing.
    """
    global _lineage_seed_in_flight
    try:
        from kiro_crew.crew_log import emit as crew_log_emit

        if not crew_log_emit.enabled():
            return
    except Exception:
        return
    with _lineage_seed_lock:
        if _lineage_seed_in_flight:
            return
        _lineage_seed_in_flight = True

    def _seed() -> None:
        global _lineage_seed_in_flight
        try:
            from kiro_crew.crew_log.session_tree_projection import projection

            projection().ensure_seeded()
        except Exception:
            logger.debug("session tree projection could not be seeded", exc_info=True)
        finally:
            with _lineage_seed_lock:
                _lineage_seed_in_flight = False

    try:
        from kiro_crew.executors import maintenance_executor

        maintenance_executor().submit(_seed)
    except Exception:
        with _lineage_seed_lock:
            _lineage_seed_in_flight = False
        logger.debug("lineage seed could not be scheduled", exc_info=True)


def _slots_ws_frame(
    slots: object,
    *,
    yolo: bool,
    channel_trusted: bool,
    gitlab_hosts_gen: object,
    folders: object,
    folders_gen: object,
    governance_gen: object,
) -> str:
    """Serialize the dashboard-user ``slots`` WS frame.

    ONE builder for the two sites that send this frame — the generic fan-out in
    :meth:`DashboardState._broadcast` and the owner frame in
    :meth:`DashboardState._do_slots_broadcast` — because they must carry the SAME
    keys and nothing else enforces that. ``_send_ws_all`` skips owner sockets for
    ``slots``, so the generic frame does not backstop an owner: a key present in
    one envelope and not the other silently deprives every owner window, with no
    error anywhere. Hand-building both is exactly how they drift (an owner frame
    that is a strict subset, missing ``folders`` and ``gitlabHostsGeneration``),
    so the remedy is one builder rather than a second careful copy — drift becomes
    impossible by construction instead of merely detectable.

    DELIBERATELY NOT used for the app-token frame in
    :meth:`DashboardState._serialize_for_client`. That envelope diverges on
    purpose: it omits ``folders`` (apps do not render the chat folder tree) and
    gates ``yolo`` / ``channelTrusted`` behind the token's declared scope. Routing
    it through here would widen an app's payload, so it is a third shape by
    design, not a duplicate awaiting cleanup.
    """
    frame = {
        "type": "slots",
        "data": slots,
        "yolo": yolo,
        "channelTrusted": channel_trusted,
        "gitlabHostsGeneration": gitlab_hosts_gen,
        "folders": folders,
        "foldersGeneration": folders_gen,
        # Which governance ceiling is installed. A centrally pushed policy
        # (``policy_distribution.apply_ceiling``) swaps the ceiling mid-session
        # and bumps this counter; the client invalidates its cached
        # ``dashboardConfig`` on a change, so a governance-derived field there
        # (``social_share_enabled``) follows the ceiling instead of waiting out
        # its stale window. Process-local, like the two counters above.
        "governanceGeneration": governance_gen,
    }
    # Same offender diagnostic as the coalesced broadcast: the
    # dashboard-user frame serializes the ENRICHED projection, so a value that
    # only enrichment adds raises here and nowhere else. Diagnosis only — the
    # exception propagates unchanged. A clean-slots note ("no offending entry
    # found") is itself evidence: the offender is in the envelope extras.
    try:
        return json.dumps(frame)
    except (TypeError, ValueError) as exc:
        exc.add_note(_slots_serialization_note(slots, path="ws-frame"))
        raise


def _link_label(channel_type: str) -> str:
    """Human label for a known channel; preserve unknown types verbatim."""
    return _CHANNEL_LABELS.get(channel_type, channel_type)


def _redacted_link_target(target: str | None) -> str:
    """Return a non-sensitive tail hint, never a raw conversation id."""
    if not target:
        return "…"
    safe, _ = redact_exfiltration_urls(target)
    safe, _ = redact_credentials(safe)
    if safe != target:
        return "…redacted"
    if len(safe) <= 6:
        return f"…{safe[-2:]}" if len(safe) > 2 else "…"
    return f"…{safe[-6:]}"


# The unlink body reader the two unlink endpoints (``chat_mirror.mirror-unlink``,
# ``chat_slack.slack-unlink``) share. The other two pieces of the stale-row
# guard -- mint the row's token, compare it with the binding held and clear on
# equality -- live below the dashboard: the token in ``messaging.link
# .binding_token`` (the projection mints it there too, so the row and the
# compare spell one identity), the compare-and-clear in
# ``SessionMap.clear_mirror_link_if`` / ``clear_slack_link_if``, one guarded
# step under the map's own lock. A route holds no compare of its own.


async def _expected_binding(request: web.Request) -> tuple[str, str] | None:
    """The binding an unlink body names -- ``(channel_type, binding)`` -- or None.

    Shared by the mirror and Slack unlink endpoints so both spell the guard the
    same way. Only a body naming a ``channel_type`` arms the compare; ``binding``
    is the row's opaque token as the slots projection spells it. A body that
    names the channel but no token still arms the compare, with a token nothing
    matches: the caller tried to name a row and failed, and the fail-closed
    answer is the 409, never the unconditional clear. The same posture holds one
    step earlier: only an EMPTY body reads as no body. A body that is present but
    not valid JSON, or not a JSON object, is refused with 400 ``invalid_body`` --
    reading it as "no body" would hand a caller that tried to name a row and
    garbled it the unconditional clear, the one answer the guard exists to
    withhold from a caller with a row in hand. A body whose bytes do not decode
    (invalid UTF-8, an unknown ``charset=``) is the same garbled body and takes
    the same 400: decoding raises before any read or mutation, and answering it
    with a 500 would hand the client a crash where the guard has an answer.
    """
    body: Any = None
    try:
        raw = await request.text()
    except (UnicodeDecodeError, LookupError):
        pass
    else:
        if not raw.strip():
            return None
        try:
            body = json.loads(raw)
        except ValueError:
            pass
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(
            text=json.dumps(
                {
                    "error": "the unlink body must be a JSON object naming channel_type "
                    "and binding, or empty",
                    "code": "invalid_body",
                }
            ),
            content_type="application/json",
        )
    channel_type = str(body.get("channel_type", "") or "").strip().lower()
    if not channel_type:
        return None
    binding = body.get("binding")
    return channel_type, (binding.strip() if isinstance(binding, str) else "")


def _mirror_link_nonce(state: "DashboardState", session_key: str) -> str:
    """The persisted nonce of *session_key*'s mirror binding, ``""`` when none.

    The slots projection's reader: the row's token digests this nonce, and the
    map's compare-and-clear (``SessionMap.clear_mirror_link_if``) reads the same
    stored nonce, so the row and the compare digest one value. Only a string
    counts: a session double without the accessor, or one that answers it with
    a mock, reads as no nonce, which keeps the pre-nonce token in force there.
    """
    try:
        value = state.sessions.mirror_link_nonce(session_key)
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _slack_link_nonce(state: "DashboardState", session_key: str) -> str:
    """The persisted nonce of *session_key*'s Slack thread link, ``""`` when none.

    The projection's reader for the Slack row, same contract as
    ``_mirror_link_nonce``; ``SessionMap.clear_slack_link_if`` reads the stored
    nonce itself.
    """
    try:
        value = state.sessions.slack_link_nonce(session_key)
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


# Slot-list broadcast coalescing window. The sub-agent slots debouncer in
# slack/gateway.py hardcodes the same value independently; the two are not shared.
_SLOTS_BROADCAST_INTERVAL_S: float = 0.2


# The window above is a FLOOR. Every broadcast re-sends the whole list (~1.5 KB a
# slot), so with a fixed window the bytes each dashboard socket receives grow with
# both the list size and the push rate. After each broadcast the next window is
# stretched to ``frame bytes / budget`` so a sustained burst costs each socket about
# this many bytes a second, whatever the list size. A small list stays on the floor
# (a 25-slot frame is ~38 KB, 0.15 s at this budget); the ceiling bounds how stale
# the sidebar may run at the slot cap. The leading edge after an idle window is
# still immediate, and the trailing flush still re-serializes at delivery time.
_SLOTS_BROADCAST_BYTES_PER_S: int = 256 * 1024


_SLOTS_BROADCAST_MAX_INTERVAL_S: float = 2.0


def _slots_broadcast_interval_for(frame_bytes: int) -> float:
    """Coalescing window that keeps one socket's slot-list bytes near the budget."""
    stretched = frame_bytes / _SLOTS_BROADCAST_BYTES_PER_S
    return min(_SLOTS_BROADCAST_MAX_INTERVAL_S, max(_SLOTS_BROADCAST_INTERVAL_S, stretched))
