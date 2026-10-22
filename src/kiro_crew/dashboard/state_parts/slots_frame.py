"""What a slots frame contains: serialised rows and views, links and lineage parents.

A slot's channel and source links as the sidebar shows them, the lineage parents
(with the one-shot seed request and its failure latch, rebound only here), the
frame and note shape and its size-paced interval, and member slot transitions.

Instance fields of ``DashboardState`` this module reads and writes:

Writes: ``_member_driven_slots_seen``.
Reads only: ``_member_slots_retry``, ``_member_slots_unconfirmed``,
``_pinned_order_rev``, ``_slots``, ``_slots_under_construction``, ``sessions``,
``subagents``.

A function whose first parameter is ``self`` is a ``DashboardState`` method: the
class binds it under the same name (``state_parts/dashboard_state.py``), so it reads
and writes the state's own fields and calls its siblings through ``self``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from kiro_crew.dashboard.state_parts.slot_directory import _is_genuine_slack_link
from kiro_crew.messaging.link import (
    SLACK_NAMESPACE,
    ChannelLink,
    binding_token,
    channel_namespace_of,
    split_namespaced_channel_id,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.dashboard._types import SubagentManager
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot
    from kiro_crew.dashboard.state_parts.dashboard_state import DashboardState

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
    slots_partial: bool = False,
    slots_held_keys: object = (),
    slots_hold_channels: bool = False,
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
    if slots_partial:
        frame["slotsPartial"] = True
    if slots_held_keys:
        frame["slotsHeldKeys"] = slots_held_keys
    if slots_hold_channels:
        frame["slotsHoldChannels"] = True
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
# slack/gateway_runtime/subagent_wiring.py hardcodes the same value independently;
# the two are not shared.
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


def _channel_link_is_live(self: DashboardState, link: ChannelLink) -> bool:
    """Is a proactive-capable transport registered for this channel?

    Deliberately an IN-MEMORY check only. This runs per linked slot inside
    ``serialize_slots``, which sits on the ``push_slots_update`` websocket
    broadcast path, so it must not touch the filesystem: the full governed
    ladder (``chat_runner._resolve_channel_target``) calls
    ``governance_permits``, which walks the profile directory (``iterdir`` +
    ``stat``, with a possible reload) — a slow filesystem there would block
    the event loop on every push and can drive watchdog restarts.

    Governance stays enforced at the async SEND boundary (
    ``_resolve_mirror_target`` in the turn path and in the mirror-link
    reminder handler). A link may therefore read ``live: true`` here and
    still be refused at send time; that asymmetry is deliberate and safe —
    the menu affordance is optimistic, the side effect is gated.
    """
    if link.channel_type == SLACK_NAMESPACE or not link.channel_id:
        return False
    transport = self.get_channel_transport(link.channel_type)
    if transport is None:
        return False
    return bool(
        getattr(
            getattr(transport, "capabilities", None),
            "supports_proactive_send",
            False,
        )
    )


def _slot_links(
    self: DashboardState, slot: _ChatSlot
) -> tuple[list[dict[str, Any]], bool, str, str]:
    """Build the redacted channel-neutral link projection for one slot."""
    # circular import: chat imports state at module scope.
    from kiro_crew.dashboard.chat_utils import (
        effective_session_key,
        mirror_is_paused,
        slack_mirror_is_paused,
    )

    session_key = effective_session_key(slot)
    # Resolved once per slot rather than per row: all three are storage reads,
    # and a session holds at most one Slack thread, one born-in conversation
    # and one mirror binding.
    slack_paused = slack_mirror_is_paused(self, session_key)
    mirror_paused = mirror_is_paused(self, session_key)
    origin_paused = mirror_is_paused(self, session_key, origin=True)
    mirror: ChannelLink | None = None
    persisted_ts: str | None = None
    persisted_channel: str | None = None
    try:
        candidate = self.sessions.get_mirror_link(session_key)
        if isinstance(candidate, ChannelLink):
            mirror = candidate
    except Exception:
        pass
    try:
        raw_ts, raw_channel = self.sessions.get_slack_link(session_key)
        persisted_ts = raw_ts if isinstance(raw_ts, str) else None
        persisted_channel = raw_channel if isinstance(raw_channel, str) else None
    except Exception:
        pass

    # Prefer persisted values, but retain explicit in-memory Slack links in
    # tests and during the short interval before persistence is observable.
    slack_ts = persisted_ts or slot._slack_thread_ts
    slack_channel = persisted_channel or slot._slack_channel
    namespaced_origin = split_namespaced_channel_id(persisted_channel)
    genuine_slack = _is_genuine_slack_link(slack_ts, slack_channel)
    # A Slack-BORN session's ``slack_thread_ts`` names the thread it LIVES
    # in, not a mirror target somewhere else: the Slack inbound handler
    # writes it every turn as the thread registry that routes replies back.
    # That makes it a self-reference, and the sidebar already draws an origin
    # glyph from the slot key -- so surfacing it as an outbound mirror badges
    # one conversation twice and offers a session its own origin thread as a
    # releasable mirror. A Slack-born session that genuinely mirrors to a
    # DIFFERENT thread still carries a different ts, so it is unaffected.
    slack_origin_self_link = (
        channel_namespace_of(session_key) == SLACK_NAMESPACE
        and bool(slack_ts)
        and session_key.endswith(slack_ts)
    )
    links: list[dict[str, Any]] = []
    # The per-binding nonces, read the way the bindings themselves are, so
    # the row's token and the map's compare-and-clear digest the same
    # material. Only a string counts: a session double that predates the
    # accessors (or answers them with a mock) reads as no nonce.
    mirror_nonce = _mirror_link_nonce(self, session_key)
    slack_nonce = _slack_link_nonce(self, session_key)

    def append_link(
        link: ChannelLink, direction: str, nonce: str = "", *, drives_session: bool
    ) -> None:
        """Append one row. *drives_session*: messages sent there land in THIS session.

        The inbound-routing fact is the server's to state, per row, because
        it is not readable from the row's other fields: a Slack thread is
        marked ``out`` (its inbound routing is Slack's own thread index, not
        the mirror's inbound marker) yet a reply in it resumes this session;
        a ``both`` mirror routes inbound by that marker; an ``out`` mirror
        only receives replies; and the conversation a session was born in
        is where its turns come from. Judged client-side from ``direction``
        plus the channel name, a paused Slack row reads as a one-way link --
        so the client reads this bit and special-cases nothing.
        """
        channel_type = (link.channel_type or "").lower()
        if not channel_type:
            return
        channel_id = link.channel_id or ""
        nested = split_namespaced_channel_id(channel_id)
        if nested and nested[0] == channel_type:
            channel_id = nested[1]
        normalized = ChannelLink(channel_type, channel_id, link.thread_id)
        # Real on EVERY row, origin included: the conversation a session was
        # born in can be disconnected too, so it stops syndicating there and
        # the session carries on in the dashboard. `direction` still records
        # the provenance the sidebar mark needs; it does not decide whether
        # the row has a control.
        #
        # Keyed to the row's SOURCE, not just its channel: a session born in
        # Discord that also mirrors to Telegram draws two non-Slack rows, and
        # if both read one value, muting either silently mutes the other.
        if channel_type == SLACK_NAMESPACE:
            paused = slack_paused
        elif direction == "origin":
            paused = origin_paused
        else:
            paused = mirror_paused
        links.append(
            {
                "channel": channel_type,
                "label": _link_label(channel_type),
                "target": _redacted_link_target(channel_id),
                # The row's identity for an unlink: the redacted `target`
                # above is display only and drops the thread, so a Slack
                # thread and its same-channel replacement would read alike;
                # the binding's own nonce keeps a same-target replacement
                # from reading alike too.
                "binding": binding_token(normalized, nonce),
                "direction": direction,
                "drives_session": drives_session,
                "live": self._channel_link_is_live(normalized),
                "paused": paused,
            }
        )

    # Non-Slack transports currently leak their home conversation through
    # slack_channel_id. Surface that as a read-only origin, never a Slack
    # mirror. This prefix sniff is intentionally defensive for unknown
    # future channel types too.
    if namespaced_origin and namespaced_origin[0] != SLACK_NAMESPACE:
        # The conversation the session was born in: the channel dispatcher
        # routes its messages here on every inbound turn.
        append_link(
            ChannelLink(namespaced_origin[0], namespaced_origin[1]),
            "origin",
            drives_session=True,
        )

    if mirror is not None:
        if mirror.channel_type == SLACK_NAMESPACE:
            # get_mirror_link synthesizes Slack for the legacy fields. If
            # those fields actually hold a namespaced non-Slack origin, the
            # origin above is the only truthful representation.
            if not namespaced_origin and genuine_slack and not slack_origin_self_link:
                append_link(
                    ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                    "out",
                    slack_nonce,
                    drives_session=True,
                )
        else:
            # A resume binding (set by an in-channel `!sessions` pick) routes
            # BOTH ways: this session's replies go to that channel AND
            # messages from it are delivered back here. That is a materially
            # different thing for the user to see and release than an
            # outbound-only `!link` mirror, so it gets its own direction
            # rather than being flattened into "out". Slack is excluded by
            # the branch above — it carries inbound on its own thread index
            # and never sets the marker.
            inbound = False
            try:
                inbound = bool(self.sessions.mirror_accepts_inbound(session_key))
            except Exception:
                # Older/stubbed SessionManagers may not expose the accessor;
                # degrade to the outbound reading rather than dropping the link.
                inbound = False
            append_link(mirror, "both" if inbound else "out", mirror_nonce, drives_session=inbound)
    elif genuine_slack and not slack_origin_self_link:
        # Defensive fallback for SessionManager test doubles or older
        # implementations that expose get_slack_link but not get_mirror_link.
        append_link(
            ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
            "out",
            slack_nonce,
            drives_session=True,
        )

    if genuine_slack and slack_origin_self_link:
        # The conversation a session was BORN in gets a row too. Suppressing
        # it was the last place a channel appeared with no control at all:
        # you can stop a Slack-born session syndicating to its thread and
        # carry on in the dashboard, and a human reply in that thread brings
        # it back. It stays `origin` so the sidebar keeps showing where the
        # conversation came from — provenance is history and survives a
        # disconnect; only the delivery indicator reflects the mute.
        append_link(
            ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
            "origin",
            slack_nonce,
            drives_session=True,
        )

    if genuine_slack and not slack_origin_self_link:
        slack_namespace = split_namespaced_channel_id(slack_channel)
        visible_slack_channel = slack_namespace[1] if slack_namespace else (slack_channel or "")
        # A Slack ROW accompanies `slack_linked=True` unconditionally. The
        # dashboard's channel control is built from `links` alone — it no
        # longer synthesizes a Slack row from this boolean, because a
        # synthesized row cannot know `paused` and so rendered a muted thread
        # as connected. That makes a True here with no row worse than a
        # cosmetic gap: the session IS linked and the menu would offer to
        # connect it. Guaranteed here rather than left to hold incidentally
        # across the branches above.
        if not any(
            row["channel"] == SLACK_NAMESPACE and row["direction"] != "origin" for row in links
        ):
            append_link(
                ChannelLink(SLACK_NAMESPACE, slack_channel, slack_ts),
                "out",
                slack_nonce,
                drives_session=True,
            )
        return links, True, visible_slack_channel, slack_ts or ""
    return links, False, "", ""


def serialize_slot(
    self: DashboardState,
    slot: _ChatSlot,
    *,
    include_check_status: bool = False,
    dashboard_user: bool = False,
) -> dict[str, Any]:
    """Serialize one slot with state-backed channel-link metadata."""
    payload = slot.to_dict(include_check_status=include_check_status, dashboard_user=dashboard_user)
    if payload.get("created_by"):
        # A worker tab says whether its process is held or was released by the
        # idle sweep (the conversation resumes on the next send either way).
        from kiro_crew.dashboard.session_control_parts.session_status import process_state

        payload["process"] = process_state(self, slot)
    links, slack_linked, slack_channel, slack_thread_ts = self._slot_links(slot)
    manual = self.manual_session_order
    payload.update(
        {
            "pin_rank": self.pin_rank(slot),
            "pin_rev": self._pinned_order_rev,
            "manual_rank": manual.rank(slot.key),
            "manual_rev": manual.rev,
            "links": links,
            "slack_linked": slack_linked,
            "slack_channel": slack_channel,
            "slack_thread_ts": slack_thread_ts,
        }
    )
    return payload


def serialize_slots(
    self: DashboardState, *, include_check_status: bool = False, dashboard_user: bool = False
) -> list:
    """Serialize slots, optionally including owner-only provider status.

    ``subagents_running`` and ``subagents_queued`` remain available to every
    authenticated caller.
    Credential-backed ``ci`` and ``state`` fields are omitted unless an
    authenticated owner boundary explicitly opts in — EXCEPT a link whose
    repository is known public, which any authenticated dashboard user
    (``dashboard_user=True``) may see because that lifecycle is already
    world-visible. Private/unknown repos and app tokens stay owner-only.
    """
    out = []
    subs = getattr(self, "subagents", None)
    # A slot that is registered but still under construction is not yet a
    # session: its transcript is mid-hydration and, for an import, its Layer B
    # join is not written. Omit it from the payload so the creation-time
    # broadcast never advertises a tab that resolves to nothing (a click would
    # cold-start a fresh context the pending join can never attach to). It
    # stays REGISTERED in ``_slots`` throughout, so a concurrent same-key
    # resume still resolves it and the idempotency guard holds; it simply is
    # not shown until the builder ends construction and pushes. Belt-and-
    # suspenders on ``__new__``-built states that never ran __init__:
    # treat a missing set as empty rather than AttributeError-ing this hot
    # path.
    # circular import: chat_utils imports this module at load time.
    from kiro_crew.dashboard.chat_utils import effective_session_key

    under_construction = getattr(self, "_slots_under_construction", None) or ()
    queued_by_slot = _published_queued_by_slot(subs)
    for s in self._slots.values():
        if s.key in under_construction:
            continue
        self._drop_orphaned_mcp_report(s)
        d = self.serialize_slot(
            s,
            include_check_status=include_check_status,
            dashboard_user=dashboard_user,
        )
        d["subagents_running"] = bool(subs and subs.running_agents_for(effective_session_key(s)))
        d["subagents_queued"] = queued_by_slot.get(s.key, 0)
        out.append(d)
    # The slot-key/session-key correspondence the lineage join needs, read the same
    # way ``/api/sessions/memory`` reads it for the Sessions table. Handed over
    # UNCALLED: resolving it is pure dict work over the live registry, but it belongs
    # inside that function's failure boundary, because a fault in it is a fault in
    # the nesting and must cost the nesting rather than the whole sidebar.
    # ``getattr`` because a stub state in the suite may not carry the method at all.
    _attach_slot_parents(out, getattr(self, "spend_slot_by_session", None))
    return out


def serialize_slot_views(
    self: DashboardState, *, owner: bool
) -> tuple[list[dict], list[dict], list[dict] | None]:
    """One serialization pass, three audience views of the slot list.

    Returns ``(bare, dashboard_user, owner)``; ``owner`` is ``None`` when
    ``owner`` is False (no owner socket to build it for).

    The three views the broadcast ships differ ONLY in each slot's
    ``source_links`` field -- the audience gates are read nowhere else in the
    summary (see ``_ChatSlot.source_links_view``). Serializing the list three
    times therefore ran the per-slot projection body (last-message markdown
    strip, credential redaction, options parse) and the source-link
    budgeting three times for identical output, synchronously on the
    event loop: measured at ~200 ms per pass on a sidebar of ~80 tabs, so a
    broadcast stalled the loop for ~600 ms. Those stalls read as host pressure
    to the adaptive concurrency controller (``loop_lag >= 250 ms`` is a
    sufficient-alone decrease signal) and pinned the subagent cap at its floor
    on an idle machine. Serialize once, then re-project only the one field.

    The derived dicts are shallow copies: every other value is shared with
    the bare list. Nothing mutates a slot payload after serialization (the
    bare list is ``json.dumps``-ed for SSE and the WS frames are built from
    the lists as-is), so sharing is safe and avoids a deep copy per audience.
    A payload whose key has no live slot (a patched ``serialize_slots`` in
    tests, or a slot closed between the two loops) is carried over unchanged.
    """
    bare = self.serialize_slots()
    return (
        bare,
        self._reproject_slots(bare, dashboard_user=True),
        self._reproject_slots(bare, include_check_status=True) if owner else None,
    )


def _reproject_slots(
    self: DashboardState,
    slots_data: list[dict],
    *,
    include_check_status: bool = False,
    dashboard_user: bool = False,
) -> list[dict]:
    out: list[dict] = []
    for payload in slots_data:
        slot = self._slots.get(payload.get("key", ""))
        if slot is None or "source_links" not in payload:
            out.append(payload)
            continue
        view = dict(payload)
        view["source_links"] = slot.source_links_view(
            include_check_status=include_check_status, dashboard_user=dashboard_user
        )
        out.append(view)
    return out


def _drop_orphaned_mcp_report(self: DashboardState, slot: "_ChatSlot") -> None:
    """Drop a slot's MCP report unless it describes the slot's CURRENT session.

    A report describes exactly ONE session. Clearing it at each teardown was
    the wrong shape — review found path after path that skipped it (the reset
    funnel, the reload and reset-conversation routes, the queued discard, a
    channel handler, the cron reaper, the task runner, a project change) — and
    a LIVENESS check ("does a session exist?") still missed the last of them,
    because a reset RECREATES a session under the same key: the slot looked
    alive while the report described the session that had gone.

    So the question asked here is identity, not liveness — is the live session
    the one this payload was taken under? A mismatch is dropped by
    construction, which closes every one of those paths at once, including any
    future one, and demotes the remaining ``clear_mcp_report()`` calls to a
    courtesy that pushes the delta early rather than the thing correctness
    rests on.

    Validating HERE cannot be bypassed: this is the single projector both
    ``/api/chat/slots`` and the WebSocket snapshot go through.

    The key derivation mirrors ``chat_utils.effective_session_key`` — a
    channel-born slot's turns run on the channel's own session — and is
    inlined because ``chat_utils`` imports this module. One refinement over
    that mirror: an in-flight turn's OWN key wins over the slot's routing.
    The two diverge when the routing is reassigned on a live slot — a cron
    injection binds ``linked_session_key`` to ``cron:<id>`` with no
    ``running`` gate — and the report describes the session the turn is
    actually running on, not the one a FUTURE turn would route to.
    Resolving the routing there reads a different (or absent) provider,
    mismatches, and clears a live session's report mid-turn. Same rule as
    turn cancellation: address the turn, not the routing.
    """
    if slot.mcp_report_payload() is None:
        return
    session_key = (
        getattr(slot, "_active_turn_session_key", "")
        or getattr(slot, "linked_session_key", "")
        or f"dashboard:{slot.key}"
    )
    provider = self.sessions.get_provider(session_key)
    live_id = getattr(provider, "session_id", "") if provider is not None else ""
    if live_id != slot._mcp_report_session_id:
        slot.clear_mcp_report()


def _emit_member_slot_transitions(self: DashboardState) -> None:
    """Log slot/opened and slot/closed for member-driven slots.

    Runs after every slots broadcast and after :meth:`push_slot_removed`,
    which is how a close reaches the log when no consumer needed the full
    list. It diffs against the last-seen set, so a second call for the same
    registry state emits nothing.
    """
    # Best-effort per-member event log: emit slot/opened and slot/closed
    # for slots DRIVEN by a member (created_by is a member NAME), diffed
    # against the last-seen set on this state object. Additive; never
    # affects the broadcast above.
    try:
        from kiro_crew import eventlog_hooks

        # The known-agent check must not read config.json here: this runs on
        # the gateway serving loop, so it reads the off-loop alias snapshot
        # (refreshed by every successful config load) instead of stat/read/
        # parsing config synchronously and stalling every task.
        from kiro_crew.eventlog.types import SLOT_CLOSED, SLOT_OPENED
        from kiro_crew.members import member_slug, slug_from_dm_slot_key

        # `_created_by` is `session_control`'s attribution and it holds the
        # creator's SLOT KEY, never an agent name -- so comparing it against the
        # alias snapshot could not match for ANY member-created slot, and every
        # member slot event was dropped on the normal path.
        #
        # A member drives two kinds of slot and both are resolved, because
        # covering only one leaves the other silently dropped:
        #   (a) its pinned DM slot, keyed `member-<slug>`, from which the slug
        #       is pure string work -- `slug_from_dm_slot_key` is the members
        #       module's single spelling of that strip, including the
        #       `.memory-<store>` suffix a reader must drop;
        #   (b) an ordinary chat slot bound to the member's private store, whose
        #       owner lives in the CONFIG. That read is deferred to the worker
        #       below for the same reason the log id already is: this function
        #       runs on the gateway serving loop and must not parse config here.
        # So the loop collects identity MATERIAL and the worker resolves it.
        current: dict[str, tuple[str, str]] = {}
        for _sk, _slot in list(self._slots.items()):
            _cb = getattr(_slot, "_created_by", "")
            if not _cb:
                continue
            _slug = slug_from_dm_slot_key(_cb)
            if _slug:
                current[_sk] = (_slug, "")
                continue
            _creator = self._slots.get(_cb)
            _store = getattr(_creator, "memory_store", "") if _creator else ""
            if _store and _store != "default":
                current[_sk] = ("", _store)
        prev = self._member_driven_slots_seen
        if self._member_slots_unconfirmed:
            # An append the worker could not complete is not recorded, whatever
            # the checkpoint says. Applying the corrections here is what makes
            # the comparison below recompute exactly those transitions and
            # nothing else. Drained rather than read, so a retry that fails
            # again queues a fresh correction instead of looping on a stale one.
            # Drained IN PLACE, and by repeated POP rather than copy-then-clear.
            # The worker closure captures this dictionary by reference when it is
            # created, so rebinding the attribute to a fresh one would leave an
            # in-flight worker writing its failures into an object nothing reads.
            # And a copy followed by clear() leaves a window: a failure the worker
            # records between the two is wiped without ever being applied, so the
            # comparison below recomputes nothing for it and the checkpoint then
            # advances past that transition, dropping it for good. Each pop either
            # returns an entry, which is therefore applied, or finds none and
            # leaves later ones for the next pass -- no entry is discarded unread.
            _lost: dict[str, tuple[str, str] | None] = {}
            while True:
                try:
                    _lkey, _lval = self._member_slots_unconfirmed.popitem()
                except KeyError:
                    break
                _lost[_lkey] = _lval
            prev = dict(prev)
            for _lk, _lwho in _lost.items():
                if _lwho is None:
                    prev.pop(_lk, None)
                else:
                    prev[_lk] = _lwho
            self._member_driven_slots_seen = prev
        # Drained IN PLACE and BEFORE the comparison, for two reasons. In place,
        # because an in-flight worker holds this list by reference exactly as it
        # holds the map. Before, because a retry must not depend on the slots
        # having changed: a slot whose open failed and which is still open makes
        # `current == prev`, so anything inside the comparison below would never
        # run and the open would wait for an unrelated slot to move.
        _retry: list[tuple[str, tuple[str, str], str, dict]] = []
        while self._member_slots_retry:
            _retry.append(self._member_slots_retry.pop(0))
        if current != prev or _retry:
            # emit fsyncs, so collect the (member, type, data) tuples and
            # offload the writes: _do_slots_broadcast runs on the gateway
            # loop and a synchronous durability barrier per slot transition
            # would stall every concurrent session. The member's LOG ID is
            # resolved inside that worker, not here: a member may carry an
            # explicit `member_id` and only member_slug honours it, but it
            # reads the config, which this path must not do on the loop.
            # Retries FIRST: a re-emitted open has to reach the log ahead of the
            # close computed for the same key below, or the ledger records a close
            # with no open before it.
            _emits: list[tuple[str, tuple[str, str], str, dict]] = list(_retry)
            for _sk, _who in current.items():
                if _sk not in prev:
                    _emits.append((_sk, _who, SLOT_OPENED, {"slot_key": _sk}))
            for _sk, _who in prev.items():
                if _sk not in current:
                    _emits.append(
                        (
                            _sk,
                            _who,
                            SLOT_CLOSED,
                            {"slot_key": _sk, "reason": "closed"},
                        )
                    )

            def _emit_slots(
                events: list[tuple[str, tuple[str, str], str, dict]] = _emits,
                unconfirmed: dict[str, tuple[str, str] | None] = (self._member_slots_unconfirmed),
                retry: list[tuple[str, tuple[str, str], str, dict]] = (self._member_slots_retry),
            ) -> None:
                def _report(
                    _key: str, _slug_in: str, _store_in: str, _etype: str, _data: dict
                ) -> None:
                    # A close goes through the checkpoint: putting the key BACK is
                    # what makes the next comparison recompute it.
                    if _etype == SLOT_CLOSED:
                        unconfirmed[_key] = (_slug_in, _store_in)
                        return
                    # An open is retried VERBATIM and the checkpoint is left
                    # alone. Popping the key instead only recomputes the open
                    # while the slot is still open; if it closed in this window
                    # the comparison computes nothing and the episode is lost for
                    # the life of the process. Leaving the checkpoint claiming the
                    # open means a later close is still computed normally, and
                    # this entry is re-emitted ahead of it.
                    retry.append((_key, (_slug_in, _store_in), _etype, _data))

                for _key, (_slug_in, _store_in), _etype, _data in events:
                    try:
                        _slug = _slug_in
                        if not _slug and _store_in:
                            # Case (b): the config read this path may not do on
                            # the loop is safe here, in the worker.
                            from kiro_crew.config.loader import KiroCrewConfig

                            _rec = KiroCrewConfig.load().memory_stores.get(_store_in)
                            _owner = getattr(_rec, "owner_member", "") if _rec else ""
                            if _owner:
                                _slug = member_slug(_owner)
                        # `name` is left empty on purpose: `emit` passes
                        # `name or slug` to `ensure`, whose _resolved_name looks
                        # the exact name up in the roster, so the name is
                        # resolved once per member rather than per event.
                        if not eventlog_hooks.emit(_slug, "", _etype, _data):
                            # Reported, not swallowed. The checkpoint already
                            # counts this transition as handed over, so without
                            # this the loss is permanent for the life of the
                            # process: the next broadcast compares against a
                            # checkpoint that claims the event was written.
                            _report(_key, _slug_in, _store_in, _etype, _data)
                    except Exception:
                        _report(_key, _slug_in, _store_in, _etype, _data)
                        logger.debug("slot event-log emit failed", exc_info=True)

            # The checkpoint advances only once the transitions are HANDED
            # OVER. `submit` is bounded and can refuse, and this checkpoint is
            # the only record of what is still unwritten: advancing it first
            # turns a refusal into permanent staleness, because the next
            # broadcast compares against `current` and computes no transitions
            # to retry. Leaving it at `prev` instead means the next broadcast
            # recomputes the same set and hands it over again.
            if _emits:
                # No running-loop check: `submit` queues on the ordered
                # executor whether or not a loop is running, and queuing
                # BOTH paths is what keeps them in one order. Running a
                # no-loop caller inline instead would let it reach the log
                # ahead of an append already queued by a loop caller.
                if eventlog_hooks.submit(_emit_slots):
                    self._member_driven_slots_seen = current
            else:
                # A change with no open or close (a slot's store changed under
                # the same key) has nothing to hand over, so holding the
                # checkpoint back would recompute an empty set forever.
                self._member_driven_slots_seen = current
    except Exception:
        logger.debug("slot open/close event-log hook failed", exc_info=True)


def _lineage_rows(self: DashboardState) -> list[dict[str, Any]]:
    """One row per live slot carrying exactly what the lineage join reads.

    The key, plus the two mint-witness fields ``serialize_slot`` writes for the
    full frame. A child dispatched and not yet run has no node in the fold, and
    the join nests it from ``created_by`` only when ``lineage_minted`` vouches for
    it, so a row with the key alone would answer ``parent: None`` for that child
    and a patch would detach a session the full frame nests.
    """
    under_construction = getattr(self, "_slots_under_construction", None) or ()
    rows: list[dict[str, Any]] = []
    for k, slot in list(self._slots.items()):
        if k in under_construction:
            continue
        rows.append(
            {
                "key": k,
                "created_by": getattr(slot, "_created_by", ""),
                "lineage_minted": bool(getattr(slot, "_lineage_minted", False)),
            }
        )
    return rows
