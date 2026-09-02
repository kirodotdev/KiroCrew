"""``file_send``'s destination oracle -- the one place that answers "where does
this file go, and may this caller send it there?" for BOTH delivery legs.

PR #6044 gave the two legs of ``file_send`` one shared ADMISSION gate
(:func:`~kiro_crew.dashboard.handlers.files._gate_upload_file`: containment, the
descriptor-safe read, the binary MIME allowlist, the content credential scans).
Destination and authorization stayed per-leg, inline in each endpoint -- the last
surviving sibling of the "two paths, two behaviours" root cause that gate closed
(issue #6060). This module is the destination half of the same move: the two
resolvers now sit side by side in one file, each endpoint keeps only its own
delivery verb and response shape, and every rung one leg runs and the other does
not is written down below instead of being discoverable only by reading two
handlers 200 lines apart.

This is deliberately NOT a single ``resolve(leg, ...)`` dispatcher. Each endpoint
knows its leg statically, so a union-typed dispatcher would buy nothing and force
both call sites to unwrap a wider result. What makes this one oracle is that both
resolvers live here, share one refusal/skip vocabulary, and are audited through
one shape (``_audit_file_send`` in the handlers module, which owns the
test-patchable ``_sel``).

Rung by rung, as shipped today -- the Slack column is what #6060 asks to
converge, and the two NOT RUN rows are the parts that need the architecture
decision in that issue's step 2 (does Slack join ``channel_transports``, or does
the ladder grow a Slack adapter?), so they are named here, not silently changed:

======================================  =============================  ===========================================
rung                                    channel leg                    Slack leg
======================================  =============================  ===========================================
destination source                      session-map mirror link only    request-named ``channel``, else the
                                                                        session-map Slack link, else the owner DM
recipient authorization                 ``transport.may_send_to``       ``is_tracked_channel``; a session-map
                                        (fail-closed)                   channel passes on a ``D`` prefix OR
                                                                        tracking (both fail-closed)
``channels``-scope governance           yes, inside                     NOT RUN (#6060 step 2)
(``vet_and_audit``, fail-closed, SEL)   ``_resolve_channel_target``
restricted-session ceiling              yes                             NOT RUN (#6060 step 2)
(``upload_gate.uploads_restricted``)
transport registration +                yes                             n/a -- Slack's dedicated client is
``supports_proactive_send``                                             deliberately absent from
                                                                        ``channel_transports``
caller identity on the wire             STRICT session key, pinned by   lenient ``_resolve_session_key()``; the
                                        the MCP tool                    tool's three-state classifier refuses an
                                                                        unresolved caller before the leg runs
resolution off the event loop           yes (``asyncio.to_thread``)     no -- ``open_dm`` is a coroutine, so this
                                                                        ladder stays on the loop with its config
                                                                        and tracking reads inline, as before
======================================  =============================  ===========================================

Ambient lookups are PARAMETERS, not imports (``tracked_probe``,
``persisted_probe``) -- the same contract
:func:`kiro_crew.messaging.upload_gate.uploads_restricted` uses for its probe.
The caller keeps one place to bind (and one place for a test to patch), and this
module stays free of both the Slack handler's tracked-channel config dependency
and the ``handlers`` package it is called from.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.messaging import upload_gate
from kiro_crew.messaging.link import SLACK_NAMESPACE, is_channel_session_key
from kiro_crew.validation import FILE_SEND_SCHEMA, ValidationError, validate_tool_args

logger = logging.getLogger(__name__)

#: ``channel_id -> is a tracked Slack channel``. Blocking (it reads the Slack
#: config), and a raising probe DENIES: an authorization check that errored has
#: not authorized anybody.
TrackedProbe = Callable[[str], bool]

#: Channels whose transport carries the name-preserving ``send_document`` verb,
#: as distinct from the extraction upload whose filename sanitizer maps any
#: non-raster mime to ``.bin``. A channel joins this list only once it has that
#: verb; every other channel is a skip.
DOCUMENT_CHANNELS = ("telegram", "discord")

_DASHBOARD_PREFIX = "dashboard:"


@dataclass(frozen=True)
class Refusal:
    """The caller may not have the destination it named -- an HTTP error.

    Distinct from :class:`Skip`: a refusal means an authorization rung said no
    to something the caller asked for, so it is reported as a failure. *error*
    is the advisory prose the client sees and *code* the machine-readable
    contract it branches on (RFC 9457 3.1.3, enforced by
    ``test_error_code_contract``); *audit_error* is what the SEL record carries,
    and differs on purpose -- the audit names the refused channel, the response
    does not. *downstream* is set only where the shipped record set it.
    """

    error: str
    code: str
    status: int
    audit_error: str
    downstream: str | None = None


@dataclass(frozen=True)
class Skip:
    """No destination on this leg, and that is the normal case.

    Most sessions mirror nowhere and most callers name no Slack channel, so
    "cannot deliver here" is a skip the caller falls back from -- never an
    error. Skips carry no ``downstream_service`` in the audit trail, matching
    the shipped records on both legs.
    """

    reason: str


@dataclass(frozen=True)
class SlackTarget:
    """A resolved, authorized Slack destination."""

    channel: str
    thread_ts: str


@dataclass(frozen=True)
class ChannelTarget:
    """A resolved, authorized non-Slack destination plus its delivery verb."""

    link: Any
    transport: Any
    deliver: Callable[..., Any]


async def resolve_slack(
    state: Any,
    slack: Any,
    *,
    session_key: str,
    requested_channel: str,
    thread_ts: str | None,
    tracked_probe: TrackedProbe,
) -> SlackTarget | Refusal | Skip:
    """Resolve the Slack leg's destination and authorize the caller for it.

    The shipped ladder, in order and unchanged: an explicitly named channel
    wins; otherwise a linkable session's own Slack link supplies both channel
    and thread; otherwise the owner DM. A named channel must be tracked; a
    channel that came from the session map is accepted on a ``D`` prefix (the
    system created that link, and a DM is not a broadcast) or on tracking.
    Both tracking checks fail closed on a raising probe.

    The session map is consulted only for a ``dashboard:`` or channel-native
    key, and only when the request supplied no ``thread_ts`` -- an explicit
    thread is the caller's own choice of destination. A link's thread is
    inherited ONLY when the caller named no channel or named that same channel:
    a thread ts belongs to one channel, so pairing it with a different one would
    post into an unrelated conversation. ``state.sessions`` is read after the
    linkable test, so a key that names no session never touches it.
    """
    target_channel = requested_channel
    channel_from_session_map = False
    # A dashboard session carries its Slack link in the session map; a
    # channel-born one is linked under that same channel key by the Slack
    # handler, so both resolve their thread from the one lookup. Skipping the
    # channel case would DM the owner instead of landing the file in the thread
    # the conversation is happening in.
    linkable = session_key.startswith(_DASHBOARD_PREFIX) or is_channel_session_key(session_key)
    if not thread_ts and linkable and state.sessions:
        link_ts, link_ch = state.sessions.get_slack_link(session_key)
        if link_ts and (not target_channel or target_channel == link_ch):
            thread_ts = link_ts
            if not target_channel and link_ch:
                target_channel = link_ch
                channel_from_session_map = True
    channel = ""
    if target_channel:
        try:
            validate_tool_args({"path": "x", "channel": target_channel}, FILE_SEND_SCHEMA)
        except ValidationError:
            return Refusal(
                error="invalid channel value",
                code="invalid_channel",
                status=400,
                audit_error="channel_validation_failed",
                downstream=SLACK_NAMESPACE,
            )
        # Session-map-sourced channels are trusted (the system created the
        # link), so only a user-supplied channel must clear the tracking check.
        # Defense-in-depth: a session-map channel must still be a DM or tracked.
        if not channel_from_session_map:
            try:
                tracked = tracked_probe(target_channel)
            except Exception:
                tracked = False  # deny-by-default extends to uncertainty
            if not tracked:
                return Refusal(
                    error="channel not in tracked channels",
                    code="channel_not_tracked",
                    status=403,
                    audit_error=f"channel_not_tracked: {target_channel}",
                    downstream=SLACK_NAMESPACE,
                )
        else:
            try:
                allowed = target_channel.startswith("D") or tracked_probe(target_channel)
            except Exception:
                allowed = False  # deny-by-default extends to uncertainty
            if not allowed:
                return Refusal(
                    error="channel not authorized",
                    code="channel_not_authorized",
                    status=403,
                    audit_error=f"session_map_channel_not_authorized: {target_channel}",
                    downstream=SLACK_NAMESPACE,
                )
        channel = target_channel
    else:
        try:
            creds = KiroCrewConfig.load().load_credentials()
            owner_id = creds.get("KIROCREW_OWNER_ID", "")
            if owner_id:
                channel = await slack.open_dm(owner_id)
        except Exception:
            pass
    if not channel:
        return Skip("no_channel")
    return SlackTarget(channel=channel, thread_ts=thread_ts or "")


async def resolve_channel(
    state: Any,
    session_key: str,
    *,
    persisted_probe: upload_gate.PersistedProbe,
) -> ChannelTarget | Skip:
    """Resolve the non-Slack leg's destination through the shared send ladder.

    Every rung is the cross-surface reply mirror's own: channel-scope
    governance, transport registration, proactive-send capability, and
    ``may_send_to`` recipient re-authorization, all fail-closed and SEL-audited
    inside ``_resolve_mirror_target`` -- plus the restricted-session ceiling the
    renderers' extraction path enforces, on the same shared predicate. The
    destination comes exclusively from the caller's session map entry: a request
    cannot name a conversation, which is what keeps this leg from being a
    broadcast primitive.

    ``_resolve_mirror_target`` is imported inside the call, not at module scope:
    ``chat_runner`` imports the dashboard world, and this module is imported
    from a handler that ``chat_runner`` can reach.

    The restricted ceiling is checked BEFORE the delivery verb is probed, so a
    restricted caller learns nothing about which channels could have uploaded.
    """
    if not session_key or not getattr(state, "sessions", None):
        return Skip("no_session")
    from kiro_crew.dashboard.chat_runner import _resolve_mirror_target

    # Off-loop like the admission gate: the ladder reloads governance profiles
    # and reads the persisted session map -- synchronous filesystem work
    # (no-blocking-call-on-event-loop). The session map's reads are
    # lock-guarded, so the call is thread-safe.
    target = await asyncio.to_thread(_resolve_mirror_target, state, session_key)
    if target is None:
        # No mirror link, a Slack link (the Slack leg owns those), a missing or
        # capability-less transport, a governance denial, or a may_send_to
        # refusal -- the ladder audited the ones that matter; all mean the same
        # thing here: this caller has no non-Slack conversation to deliver to.
        return Skip("no_channel_destination")
    link, transport = target
    # An incognito/temporary session ships no local file bytes to a channel, and
    # an explicit file_send must not be the bypass. The predicate SEL-audits its
    # own denial.
    if await upload_gate.uploads_restricted(
        state,
        session_key,
        channel_type=link.channel_type,
        persisted_probe=persisted_probe,
    ):
        return Skip("restricted_session")
    deliver = (
        getattr(transport, "send_document", None)
        if link.channel_type in DOCUMENT_CHANNELS
        else None
    )
    if deliver is None:
        return Skip(f"channel_upload_unsupported:{link.channel_type}")
    return ChannelTarget(link=link, transport=transport, deliver=deliver)
