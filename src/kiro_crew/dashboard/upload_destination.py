"""``file_send``'s destination oracle -- the one place that answers "where does
this file go, and may this caller send it there?" for BOTH delivery legs.

The two legs of ``file_send`` share one ADMISSION gate
(:func:`~kiro_crew.dashboard.handlers.files._gate_upload_file`: containment, the
descriptor-safe read, the binary MIME allowlist, the content credential scans).
This module is the destination half: keeping the two resolvers per-leg and inline
in each endpoint is what produces "two paths, two behaviours". Here they sit side
by side in one file, each endpoint keeps only its own delivery verb and response
shape, and every rung one leg runs and the other does not is written down below
instead of being discoverable only by reading two handlers 200 lines apart.

This is deliberately NOT a single ``resolve(leg, ...)`` dispatcher. Each endpoint
knows its leg statically, so a union-typed dispatcher would buy nothing and force
both call sites to unwrap a wider result. What makes this one oracle is that both
resolvers live here, share one refusal/skip vocabulary, and are audited through
one shape (``_audit_file_send`` in the handlers module, which owns the
test-patchable ``_sel``).

Rung by rung. Both legs run the same two ceilings -- the ``channels``-scope
governance vet and the restricted-session ceiling -- but the Slack leg calls them
DIRECTLY (:func:`_slack_egress_permitted`) rather than joining
``channel_transports``, because its dedicated client genuinely cannot ride the
shared ladder. The rows
that still differ differ for a reason named in the row:

======================================  =============================  ===========================================
rung                                    channel leg                    Slack leg
======================================  =============================  ===========================================
destination source                      session-map mirror link only    request-named ``channel``, else the
                                                                        session-map Slack link, else the owner DM
recipient authorization                 ``transport.may_send_to``       ``is_tracked_channel``; a session-map
                                        (fail-closed)                   channel passes on a ``D`` prefix, on
                                                                        tracking, OR as the live code channel
                                                                        bound to the caller, whose upload must
                                                                        then come from the session's own work
                                                                        dir (all fail-closed)
``channels``-scope governance           yes, inside                     yes, direct call keyed on ``slack``
(``vet_and_audit``, fail-closed, SEL)   ``_resolve_channel_target``     (``_slack_egress_permitted``)
restricted-session ceiling              yes                             yes, same shared predicate
(``upload_gate.uploads_restricted``)
transport registration +                yes                             n/a -- Slack's dedicated client is
``supports_proactive_send``                                             deliberately absent from
                                                                        ``channel_transports``
caller identity on the wire             STRICT session key, pinned by   lenient ``_resolve_session_key()``; the
                                        the MCP tool                    tool's three-state classifier refuses an
                                                                        unresolved caller before the leg runs
resolution off the event loop           yes (``asyncio.to_thread``)     partly -- ``open_dm`` is a coroutine, so
                                                                        the ladder stays on the loop with its
                                                                        config and tracking reads inline; the two
                                                                        ceilings above are offloaded, since they
                                                                        read the profile directory
======================================  =============================  ===========================================

Ambient lookups are PARAMETERS, not imports (``tracked_probe``,
``code_channel_probe``, ``persisted_probe``) -- the same contract
:func:`kiro_crew.messaging.upload_gate.uploads_restricted` uses for its probe.
The caller keeps one place to bind (and one place for a test to patch), and this
module stays free of both the Slack handler's tracked-channel config dependency
and the ``handlers`` package it is called from.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.messaging import upload_gate
from kiro_crew.messaging.link import SLACK_NAMESPACE, is_channel_session_key
from kiro_crew.platform.context import PlatformCompositionError
from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY, vet_and_audit
from kiro_crew.validation import FILE_SEND_SCHEMA, ValidationError, validate_tool_args

logger = logging.getLogger(__name__)

#: ``channel_id -> is a tracked Slack channel``. Blocking (it reads the Slack
#: config), and a raising probe DENIES: an authorization check that errored has
#: not authorized anybody.
TrackedProbe = Callable[[str], bool]

#: ``(state, channel_id, anchor_ts, session_key) -> work dir``: the uploading
#: session's working directory when *channel_id* is the live, unarchived code
#: channel bound to exactly that session through *anchor_ts*, else ``""``. A
#: raising probe DENIES, as :data:`TrackedProbe` does.
CodeChannelProbe = Callable[[Any, str, str, str], str]

#: Channels whose transport carries the name-preserving ``send_document`` verb,
#: as distinct from the extraction upload whose filename sanitizer maps any
#: non-raster mime to ``.bin``. A channel joins this list only once it has that
#: verb; every other channel is a skip.
DOCUMENT_CHANNELS = ("telegram", "discord", "wecom")

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
    does not. *downstream* is set only where the audit record names a service.
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
    error. Skips carry no ``downstream_service`` in the audit trail on either
    leg.
    """

    reason: str


@dataclass(frozen=True)
class SlackTarget:
    """A resolved, authorized Slack destination.

    *workdir* is set only for the caller's own code channel: the upload is
    authorized there only once :func:`code_channel_source_refusal` has found
    the file's bytes inside that directory.
    """

    channel: str
    thread_ts: str
    workdir: str = ""
    #: The code channel's anchor, so the caller can ask the probe again right
    #: before it uploads.
    anchor_ts: str = ""


@dataclass(frozen=True)
class ChannelTarget:
    """A resolved, authorized non-Slack destination plus its delivery verb."""

    link: Any
    transport: Any
    deliver: Callable[..., Any]


def _slack_egress_permitted(session_key: str) -> bool:
    """Vet a Slack file upload against the ``channels`` governance scope.

    The channel leg inherits this from the shared send ladder. Slack is
    deliberately absent from ``channel_transports`` and so never reaches that
    ladder, which would leave the broadest-audience leg -- the only one whose
    destination a REQUEST can name -- as the one unvetted, unaudited file egress.
    Same shape as the other Slack egress that cannot reach the ladder
    (``chat_compaction_notice._channel_egress_permitted``): a direct call to the
    audited seam rather than a registry change, so the registry keeps meaning
    "transports the shared ladder can drive".

    An empty key is a caller with no session of its own -- a cron, the heartbeat,
    an out-of-band host action -- reaching this leg through the owner-DM
    fallback. It is vetted under ``HOST_SESSION_KEY`` for the same reason
    ``handlers.messaging`` uses that sentinel on its channel legs: an empty key
    classifies as ``unknown`` and matches no profile at all, so vetting under it
    would make host-side governance inert here, while ``_host`` is the stable
    bind target operators attach it to. A legitimate owner-DM send therefore
    stays permitted on an ungoverned host, and is deniable by an explicit host
    binding rather than by a fall-through.

    Fail-closed: a degraded evaluation denies. ``PlatformCompositionError``
    propagates -- an unusable governance ceiling is a host misconfiguration, not
    a routine authorization answer, and must not be reported as one.

    Synchronous by design -- the caller runs it through ``asyncio.to_thread``
    because the gate reads the profile directory.
    """
    try:
        decision = vet_and_audit(
            "channels",
            SLACK_NAMESPACE,
            session_key=session_key or HOST_SESSION_KEY,
            tool_name="file_send",
            fail_closed=True,
        )
    except PlatformCompositionError:
        raise
    except Exception:
        logger.debug(
            "slack upload: governance check failed for %s; denying (fail-closed)",
            session_key,
            exc_info=True,
        )
        return False
    # Default False: a Decision without ``permitted`` is an unusable answer from
    # a gate and must not read as permission.
    return bool(getattr(decision, "permitted", False))


async def resolve_slack(
    state: Any,
    slack: Any,
    *,
    session_key: str,
    requested_channel: str,
    thread_ts: str | None,
    tracked_probe: TrackedProbe,
    code_channel_probe: CodeChannelProbe,
    persisted_probe: upload_gate.PersistedProbe,
) -> SlackTarget | Refusal | Skip:
    """Resolve the Slack leg's destination and authorize the caller for it.

    The ladder, in order: an explicitly named channel
    wins; otherwise a linkable session's own Slack link supplies both channel
    and thread; otherwise the owner DM. A named channel must be tracked; a
    channel that came from the session map is accepted on a ``D`` prefix (the
    system created that link, and a DM is not a broadcast), on tracking, or as
    the live code channel bound to this very session (*code_channel_probe*).
    That last one posts top-level, as the channel's mirrored replies do, and
    carries the session's work dir so the caller can hold the file to it
    (:func:`code_channel_source_refusal`): a code channel may be Slack Connect,
    so it gets the session's own files, not the whole workspace. A request-named
    channel never takes the code-channel rung. Every probe fails closed on a
    raise.

    The session map is consulted only for a ``dashboard:`` or channel-native
    key, and only when the request supplied no ``thread_ts`` -- an explicit
    thread is the caller's own choice of destination. A link's thread is
    inherited ONLY when the caller named no channel or named that same channel:
    a thread ts belongs to one channel, so pairing it with a different one would
    post into an unrelated conversation. ``state.sessions`` is read after the
    linkable test, so a key that names no session never touches it.

    Both ceilings run BEFORE any of that: a caller who may not egress here never
    opens an owner DM and never reads the session map, so a denial leaks nothing
    about where the file would have gone.
    """
    # Off-loop: the gate reads the profile directory and writes a SEL record
    # either way (no-blocking-call-on-event-loop).
    if not await asyncio.to_thread(_slack_egress_permitted, session_key):
        return Refusal(
            error="slack egress denied by the active governance profile",
            code="channels_governance_denied",
            status=403,
            audit_error="channels_governance_denied",
            downstream=SLACK_NAMESPACE,
        )
    # The ceiling the channel leg and the renderers' extraction path already
    # enforce, on the same shared predicate (which SEL-audits its own denial): a
    # session the user expected to leave no trace ships no local file bytes to a
    # chat surface, and Slack -- where a tracked channel can be company-visible
    # -- must not be the one exception. A Refusal, not a Skip: the caller asked
    # for this send, so it must learn the file did not leave.
    if await upload_gate.uploads_restricted(
        state,
        session_key,
        channel_type=SLACK_NAMESPACE,
        persisted_probe=persisted_probe,
    ):
        return Refusal(
            error="restricted session: local file bytes may not leave it",
            code="restricted_session",
            status=403,
            audit_error="restricted_session",
            downstream=SLACK_NAMESPACE,
        )
    target_channel = requested_channel
    channel_from_session_map = False
    session_map_ts = ""
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
                session_map_ts = link_ts
        elif (
            not link_ts
            and link_ch
            and not target_channel
            and session_key == f"{SLACK_NAMESPACE}:{link_ch}"
        ):
            # A flat 1:1 DM (slack.dm_single_session) is keyed BY its channel and
            # deliberately claims no thread, so the branch above finds a channel
            # with no thread and would fall through to the owner's DM -- sending
            # the file to a different conversation than the one that asked for
            # it. Deliver at the root of that DM, which is where the flat
            # conversation itself lives. Narrow on purpose: only when the key IS
            # this channel's key, so a thread-scoped or dashboard session that
            # merely knows a channel keeps failing closed to the owner DM rather
            # than broadcasting at channel root.
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
        # Defense-in-depth: a session-map channel must still be a DM, tracked,
        # or the caller's own live code channel.
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
                # The caller's own code channel: its session link names the
                # channel's anchor, and the probe answers with the session's work
                # dir only while the binding is live and names this session.
                try:
                    workdir = code_channel_probe(state, target_channel, session_map_ts, session_key)
                except Exception:
                    workdir = ""  # deny-by-default extends to uncertainty
                if workdir:
                    return SlackTarget(
                        channel=target_channel,
                        thread_ts="",
                        workdir=workdir,
                        anchor_ts=session_map_ts,
                    )
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
            # propagate=False: a post-boot read must not re-seed os.environ (a
            # worker-thread setdefault could restore a credential the live
            # watcher just popped).
            creds = await asyncio.to_thread(KiroCrewConfig.load().load_credentials, propagate=False)
            owner_id = creds.get("KIROCREW_OWNER_ID", "")
            if owner_id:
                channel = await slack.open_dm(owner_id)
        except Exception:
            pass
    if not channel:
        return Skip("no_channel")
    return SlackTarget(channel=channel, thread_ts=thread_ts or "")


def code_channel_caller_attested(request: Any, session_key: str) -> bool:
    """Whether the transport attests *session_key* as the caller's own.

    The code-channel rung authorizes by the caller's session, so the declared
    ``X-Session-Key`` must be bound to the request (the Unix-socket peer check or
    the signed per-session token ``file_send`` already sends), as the other
    endpoints that act for a named session require
    (:func:`kiro_crew.member_memory_auth.session_key_is_attested`). The internal
    secret alone lets any local holder name another session. Fails closed.

    Blocking (the token branch reads a key file): run it through ``asyncio.to_thread``.
    """
    from kiro_crew.member_memory_auth import session_key_is_attested

    try:
        return bool(session_key) and session_key_is_attested(request, session_key)
    except Exception:
        logger.debug("code channel upload: attestation check failed; denying", exc_info=True)
        return False


def code_channel_source_refusal(
    workdir: str,
    source_path: object,
    uploaded: Path,
    uploaded_bytes: bytes,
    scratch: str = "",
) -> Refusal | None:
    """``None`` when a file bound for the caller's own code channel is the session's own.

    ``file_send`` copies the file into the shared outbox and uploads that copy, so
    the uploaded path says nothing about where the bytes came from. The tool also
    sends the original path as *source_path*; a direct caller that uploads a
    work-dir file itself may leave it out, and the uploaded file is then the
    source. *workdir* and *scratch* are the session's cwd and ``$KIROCREW_SCRATCH``.
    The source must:

    * lie under one of the roots the channel's mirrored replies may carry pictures
      from (``outbound_files.containing_root``: the cwd, the browser screenshot
      directory, the session's own scratch allocation), so ``file_send`` and the
      mirror leg share one boundary for the channel; with links followed it must
      stay in that root's canonical tree. The cwd itself is not a broad root (the
      filesystem root, the home folder, the data home) or a sensitive path;
    * not be in the outbox: a copy there may have come from anywhere;
    * have no dot-named component below its root (dotfiles, ``.git``, ``.kiro``,
      a ``.scratch`` tree) and not be under the crew's memory directory, since a
      session in the base workspace folder shares it with the crew's own state.
      ``file_send`` ships text and documents, where the mirror leg ships raster
      images only, so these hold under every root;
    * pass the shared file-path screen (``hooks.validate_file_path``: ``~``, UNC
      shares and linked ancestors, the credential and trust-root list) before
      anything resolves it, and not be a sensitive path
      (:func:`kiro_crew.security.is_sensitive_canonical_path`);
    * be read through ``hooks.safe_read_file_bytes_nolink`` pinned to that root's
      read pin, so a directory swapped for a link after these checks cannot carry
      the read out of it (that reader also refuses a hardlinked file), and hold
      exactly the bytes being uploaded, which the admission gate scanned.

    This bounds which file a caller may name. An agent that can write its work
    dir can still copy bytes into it; the admission gate's content scans are the
    floor for those.

    shortcut: Crew-internal dirs are dot-named entries plus ``memory/``; no shared
    list of the workspace's internal dirs exists yet. Use one here when it does.

    Blocking (``realpath`` and a bounded read): run it through ``asyncio.to_thread``.
    """
    from kiro_crew import platform_compat
    from kiro_crew.config import loader as config_loader
    from kiro_crew.config.loader_locations import _is_broad_root
    from kiro_crew.hooks import safe_read_file_bytes_nolink, validate_file_path
    from kiro_crew.memory import memory_dir
    from kiro_crew.messaging.outbound_files import containing_root
    from kiro_crew.security import is_sensitive_canonical_path

    def refuse(reason: str) -> Refusal:
        return Refusal(
            error="file is not in this code channel session's working directory",
            code="outside_session_workdir",
            status=403,
            audit_error=f"code_channel_source_refused: {reason}",
            downstream=SLACK_NAMESPACE,
        )

    if source_path in (None, ""):
        source = str(uploaded)
    elif isinstance(source_path, str):
        source = source_path
    else:
        return refuse("invalid_source_path")
    if not workdir:
        return refuse("no_workdir")

    def real(path: Any) -> Path:
        return Path(platform_compat.strip_extended_length_prefix(os.path.realpath(path)))

    # The shared screen first: it refuses a UNC share before any resolution could
    # reach it, expands ``~`` and returns the canonical (link-free) spelling.
    screened = validate_file_path(source, canonical_gate=True)
    if screened is None:
        return refuse("path_screen_refused")
    try:
        cwd = real(workdir)
        src = real(screened)
        outbox = real(config_loader.outbox_dir())
        memory = real(memory_dir())
    except (OSError, ValueError):
        return refuse("unresolvable_path")
    if _is_broad_root(str(cwd)) or is_sensitive_canonical_path(str(cwd)):
        return refuse("workdir_not_usable")
    # The same roots the channel's mirrored replies may carry pictures from: the
    # cwd, the browser screenshot directory and the session's own scratch
    # allocation (``outbound_files.approved_roots``), matched lexically there and
    # held to the canonical tree of the root that admitted it here.
    approved = containing_root(Path(os.path.abspath(os.path.expanduser(source))), workdir, scratch)
    if approved is None:
        return refuse("outside_workdir")
    try:
        root = real(approved.read_root)
    except (OSError, ValueError):
        return refuse("unresolvable_path")
    if src == root or not src.is_relative_to(root):
        return refuse("outside_workdir")
    if src.is_relative_to(outbox):
        return refuse("outbox_copy")
    if src.is_relative_to(memory):
        return refuse("crew_memory")
    if any(part.startswith(".") for part in src.relative_to(root).parts):
        return refuse("dot_path")
    if is_sensitive_canonical_path(str(src)):
        return refuse("sensitive_path")
    try:
        # The descriptor actually opened must still lie in the work dir.
        data = safe_read_file_bytes_nolink(
            str(src),
            within_root=approved.read_root,
            within_root_is_canonical=approved.read_root_is_canonical,
            canonical_gate=True,
        )
    except Exception:
        data = None  # too large or unreadable: nothing proves the bytes came from here
    if data is None or data != uploaded_bytes:
        return refuse("content_mismatch")
    return None


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
