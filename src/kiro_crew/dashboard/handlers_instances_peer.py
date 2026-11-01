"""The Instances routes that read a connected peer, and the proxy to it.

The proxy path policy, the peer's capabilities, approval state and versions, its live
session list, the redaction of a peer's replies, and ``api_instances_proxy``.

Owner of :mod:`kiro_crew.dashboard.handlers_instances`, which is its only import path:
callers and tests reach every name here as ``handlers_instances.<name>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import unquote

from aiohttp import web

import kiro_crew
from kiro_crew.apps.version import versions_compatible
from kiro_crew.crew_mode import VIEW_ALL as CREW_VIEW_ALL
from kiro_crew.dashboard.chat_persistence import (
    cap_effort_capability_levels,
    register_reasoning_effort_values,
)
from kiro_crew.dashboard.handlers._shared import _owner_denial_response, read_capped_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.dashboard.handlers_instances_gate import (
    _PEER_FIELD_MAX_CHARS,
    MANAGER_UNAVAILABLE_CODE,
    MANAGER_UNAVAILABLE_MESSAGE,
    _audit,
    _guard,
    _manager_unavailable_response,
    _registry,
)
from kiro_crew.dashboard.peer_redaction import redact_peer_text
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS
from kiro_crew.instances.constants import (
    PEER_SLOTS_REPLY_MAX_BYTES,
    PROXY_PATH_MAX_DECODE_PASSES,
    PROXY_REDACT_BUFFER_MAX_BYTES,
    PROXY_REQUEST_BODY_MAX_BYTES,
)
from kiro_crew.instances.registry import is_headless_provisioner
from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError, TunnelState
from kiro_crew.validation import sanitize_string

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger("kiro_crew.dashboard.handlers_instances")


# ── Generic chat proxy ────────────────────────────────────────────────────────

# Response headers forwarded from the peer to the browser — an explicit
# ALLOWLIST, not a hop-by-hop skip-list. Everything else (Set-Cookie, CSP,
# CORS, hop-by-hop per RFC 9110 §7.6.1) is dropped: the peer's cookie belongs
# to the peer's origin, and a compromised peer must not be able to plant
# headers (or a credential) on the hub origin.
_PROXY_RESP_ALLOW_HEADERS = frozenset(
    {
        "content-type",
        "cache-control",
        "x-accel-buffering",  # SSE: the peer disables proxy buffering; keep it
    }
)


# Response content types forwarded from the peer. The chat surface speaks JSON
# and SSE only; anything else — above all text/html — is refused so a
# compromised peer can never serve active content that executes on the
# authenticated hub origin.
_PROXY_RESP_ALLOW_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "text/event-stream",
    }
)


_PROXY_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


# The separator in a URL path, which is `/` on every platform (RFC 3986) —
# NOT the filesystem separator. Named rather than inlined because the two are
# genuinely different things: `pathlib`/`os.path.join` would be the WRONG tool
# here (on Windows they would emit `\`, which is not a URL separator and would
# corrupt every proxied request), and the repo's portability scan rightly asks
# any bare `"/"` split to say which of the two it means.
_URL_PATH_SEP = "/"


# One path segment the proxy will forward: unreserved characters plus the
# sub-delims a real endpoint or id uses. Deliberately an ALLOWLIST — it is what
# makes the rebuilt path safe to send verbatim, because no character in it can
# introduce a separator, a query, or another encoding layer. `.` is admitted
# (file-ish ids, version suffixes); a segment that is ONLY dots is rejected
# separately, since that is traversal rather than a name.
_PROXY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~:@!$&'()*+,;=-]+$")


# The peer surface the proxy will forward, as canonical segment prefixes — a
# positive ALLOWLIST, one named prefix per row. The proxy carries the
# remote-crew chat view and nothing else, so only the peer's `api/chat`
# subtree and its `api/stream` event feed are reachable (each a prefix grant:
# every route under it, including mutating ones — that breadth is the chat
# feature's own wire surface).
# Everything outside the named prefixes is refused — including the peer's own
# `api/instances` control plane (no chaining a hub through a peer into a
# third machine) and the peer's token-minting routes, whose JSON replies
# would otherwise carry a minted peer credential back through the hub
# in-band.
#
# `api/stream` is the peer's own SSE broadcast endpoint (its `api_stream`
# handler), the out-of-turn half of the chat view: the per-turn reply streams
# back from `api/chat`, while session-list and slot-state changes arrive here.
# It is deliberately SSE and not the sibling `api/ws`: a WebSocket row would
# need a `101 Switching Protocols` to cross this proxy, and the reply
# content-type gate below exists precisely to stop a peer serving anything but
# JSON/SSE onto the authenticated hub origin — an upgrade would tunnel straight
# through it. A GET returning `text/event-stream` needs no such exception.
#
# Note what this row admits: that feed is per-CLIENT but not per-slot, so a hub
# holding it receives the peer's whole notification/slot broadcast, not only the
# session on screen. That is peer content crossing to a hub user who is already
# the peer's owner (this route is owner-only), so it widens VOLUME, not
# privilege — but it is the reason this is a named row rather than a blanket
# `api/` grant.
#
# A new prefix is added HERE explicitly, never by widening the policy back to
# deny-only. The constant's exact value and row shape are pinned by tests.
_PROXY_ALLOWED_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("api", "chat"),
    ("api", "stream"),
)


# Derived from the allowlist so the refusal (and its SEL audit line) stays
# honest as rows are added.
_PROXY_PATH_DENIED_REASON = "path is outside the proxied peer surface (%s)" % ", ".join(
    _URL_PATH_SEP.join(prefix) for prefix in _PROXY_ALLOWED_PREFIXES
)


def _proxy_canonical_path(raw: str) -> tuple[str, str]:
    """Canonicalize *raw* into a forwardable path, or return a denial reason.

    Returns ``(path, "")`` on success and ``("", reason)`` on refusal.

    Built as a CONSTRUCTION, not a series of pattern checks: the earlier
    denylist shape (reject ``..``, reject an ``api/instances`` prefix) inspected
    a half-decoded string while the peer resolved the fully-decoded one, so any
    extra encoding layer — ``api/%252e%252e/api/instances/...``, which the
    router hands over as ``api/%2e%2e/...`` — passed every check and then
    normalized back into the control plane the checks existed to protect.

    So: decode to a fixed point FIRST, admit only plainly-named segments, and
    rebuild the outbound path from exactly the segments that were vetted. The
    proxy exists for the remote-crew chat surface, not as a general tunnel-HTTP
    escape hatch, so the vetted shape is narrow on purpose — only the prefixes
    in `_PROXY_ALLOWED_PREFIXES` are forwarded. Allowing the needed surface
    (rather than denying known-bad prefixes) is what keeps every peer route the
    feature never asked for — the `api/instances` control plane, the peer's
    token-minting routes, anything added to the peer later — unreachable by
    default instead of proxied silently.
    """
    path = raw
    for _ in range(PROXY_PATH_MAX_DECODE_PASSES):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    else:
        return "", "path encoding is too deeply nested"
    # After the fixed point a `%` can only be a malformed escape (a well-formed
    # one would have decoded); refusing it keeps "decoded" honest.
    if "%" in path:
        return "", "malformed percent-encoding in path"
    segments = path.strip(_URL_PATH_SEP).split(_URL_PATH_SEP)
    for seg in segments:
        if not seg:
            return "", "empty path segment"
        if not seg.strip("."):
            return "", "path traversal"
        if not _PROXY_SEGMENT_RE.match(seg):
            return "", "illegal character in path segment"
    for prefix in _PROXY_ALLOWED_PREFIXES:
        # A malformed row must fail CLOSED: an empty row would prefix-match
        # everything and a one-segment ("api",) row would restore the whole
        # peer /api/ surface. Rows shallower than two segments are ignored
        # here (and refused by the constant's shape test).
        if len(prefix) >= 2 and tuple(segments[: len(prefix)]) == prefix:
            return _URL_PATH_SEP.join(segments), ""
    return "", _PROXY_PATH_DENIED_REASON


#: Caps applied to a peer's capability reply before it reaches the browser. The
#: peer is the user's own machine but its reply is still untrusted input crossing
#: a trust boundary, and these lists feed pickers — an unbounded roster would
#: render an unusable menu and an unbounded string would break the layout.
_CAP_MAX_ROWS = 500


_CAP_MAX_STR = 512


_CAP_MAX_VERSION_STR = 64


def _cap_str(value: object, limit: int = _CAP_MAX_STR) -> str:
    """A peer-supplied string, redacted and clamped, or "" for anything else.

    Every one of these lands in a dashboard picker, so the peer's text is an
    output boundary: it runs the peer-text credential + exfiltration-URL chain
    BEFORE the clamp, so a credential cannot survive by sitting past the limit.
    """
    if not isinstance(value, str):
        return ""
    return redact_peer_text(sanitize_string(value))[:limit]


def _remote_is_behind(local: str, remote: str) -> bool:
    """Whether a peer runs a strictly OLDER build than this gateway.

    Exact-version, so a patch or pre-release skew counts even though
    :func:`versions_compatible` still lets the two crews exchange frames. Uses
    the update checker's own ordering (``0.5.0rc3`` < ``0.5.0``), so "behind"
    here means what "an update is available" means everywhere else. An empty or
    unparseable version is never behind: the badge states a fact or says
    nothing.
    """
    if not remote or not local or remote == local:
        return False
    from kiro_crew.dashboard.handlers.updates import _is_newer

    return _is_newer(local, remote) is True


def _can_run_remote_commands(inst: object) -> bool:
    """Whether this dashboard can run a ``kirocrew`` verb on *inst*'s host.

    The display side of ``SshTunnelManager._remote_command_target``'s refusals,
    so the dashboard offers Update only where the gateway would accept it: a
    chained crew (no shell from here) and a fargate or headless crew (no
    gateway) are refused there, and offered nothing here. The manager remains
    the authority; this only decides what is shown.
    """
    if getattr(inst, "via_instance_id", ""):
        return False
    if getattr(inst, "connection_method", "") == "fargate":
        return False
    return not is_headless_provisioner(str(getattr(inst, "provisioner_id", "") or ""))


def _cap_list(payload: object, key: str) -> object:
    """The row list out of a peer reply that is either bare or wrapped in *key*.

    The roster endpoints do not agree on a shape: ``/api/models`` and
    ``/api/effort-levels`` answer a bare list, while ``/api/agents`` and
    ``/api/workspaces`` answer ``{"<key>": [...]}`` next to a sibling default.
    Normalizing here is what keeps a wrapped reply from reaching ``_cap_rows``,
    which accepts only a list — a dict would come back as an empty picker rather
    than an error, so the failure would look like "this crew has no agents".
    """
    return payload.get(key) if isinstance(payload, dict) else payload


def _cap_rows(payload: object, fields: dict[str, int]) -> list[dict[str, object]]:
    """Re-shape a peer's list reply to *fields*, dropping everything unlisted.

    Allowlist, not passthrough: the browser gets the keys this gateway knows how
    to render and nothing else, so a peer on a build with extra fields cannot
    inject content into a picker. ``context_window`` is the one numeric field and
    is coerced rather than clamped — a bogus value reads as "unknown", which the
    frontend already handles by falling back to the reference window.
    """
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, object]] = []
    for entry in payload[:_CAP_MAX_ROWS]:
        if not isinstance(entry, dict):
            continue
        row: dict[str, object] = {}
        for field, limit in fields.items():
            raw = entry.get(field)
            if field == "context_window":
                row[field] = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else 0
            else:
                row[field] = _cap_str(raw, limit)
        rows.append(row)
    return rows


async def api_instances_capabilities(request: web.Request) -> web.Response:
    """GET /api/instances/{id}/capabilities — what a connected peer can do.

    A frontend building a chat that runs on a connected peer must offer the PEER's agents,
    models, effort levels and workspaces in its header — showing this machine's
    would let the user pick a crew or model that does not exist over there. Those
    rosters are gateway-wide reads, and every existing frontend control fetches
    them same-origin from the local gateway, so this route is the per-instance
    counterpart those frontend controls fetch for a peer instance.

    Deliberately NOT served through ``/api/instances/{id}/proxy/*``: that route
    forwards a caller-supplied path and is fenced to the ``api/chat`` /
    ``api/stream`` prefixes. Widening it to reach ``api/agents`` would have
    granted the peer's mutating ``PUT /api/agents/{name}`` in the same stroke,
    because the fence matches prefixes and cannot distinguish verbs. So the reads
    go through ``SshTunnelManager.peer_capability``, whose target is chosen from a
    closed set in the backend.

    One peer read failing does not fail the request: each result is independent
    and a missing one is reported in ``unavailable`` so the frontend can disable
    exactly that control instead of showing an empty menu that looks like the peer
    has no models.
    """
    denied = _guard(request, "capabilities")
    if denied is not None:
        return denied
    # Owner-only, same bar as the proxy and the federated search: the reads run
    # on a peer with the OWNER's manager-held credential, so a Slack-minted
    # `!dashboard` subject (an authenticated non-owner) must not reach them.
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        _audit("capabilities", "denied", error="non-owner identity rejected")
        return _owner_denial_response(request, "remote-crew capabilities are owner-only")
    state: DashboardState = request.app["state"]
    instance_id = request.match_info.get("id", "")
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("capabilities", "denied", error="instances manager unavailable")
        return _manager_unavailable_response()

    paths = ("/api/version", "/api/agents", "/api/models", "/api/effort-levels", "/api/workspaces")
    results = await asyncio.gather(
        *(mgr.peer_capability(instance_id, path) for path in paths),
        return_exceptions=True,
    )
    raw: dict[str, object] = {}
    unavailable: dict[str, str] = {}
    for path, outcome in zip(paths, results):
        field = path.rsplit("/", 1)[-1].replace("-", "_")
        if isinstance(outcome, BaseException):
            logger.info(
                "Peer capability %s on %s raised (%s)",
                path,
                instance_id,
                type(outcome).__name__,
            )
            unavailable[field] = "capability_unreachable"
            continue
        ok, payload = outcome
        if ok:
            raw[field] = payload
        else:
            unavailable[field] = (
                _cap_str(payload.get("code"), 64)
                if isinstance(payload, dict)
                else "capability_error"
            )

    peer_version = ""
    version_payload = raw.get("version")
    if isinstance(version_payload, dict):
        peer_version = _cap_str(version_payload.get("version"), _CAP_MAX_VERSION_STR)
    agents_payload = raw.get("agents")
    workspaces_payload = raw.get("workspaces")
    workspaces = _cap_rows(
        _cap_list(workspaces_payload, "workspaces"), {"name": 128, "path": _CAP_MAX_STR}
    )
    effort_payload = _cap_list(raw.get("effort_levels"), "effort_levels")
    effort_levels = (
        cap_effort_capability_levels(effort_payload, source="peer pre-session")
        if isinstance(effort_payload, list)
        else []
    )
    version_match = versions_compatible(kiro_crew.__version__, peer_version)
    behind = _remote_is_behind(kiro_crew.__version__, peer_version)
    # The remote pre-session picker can offer these levels before a live slot
    # reports its config. Keep the hub's POST allowlist in sync with that offer.
    if version_match and effort_levels:
        effort_levels = register_reasoning_effort_values(effort_levels)

    _audit("capabilities", "success", request_id=instance_id)
    return web.json_response(
        {
            "instance_id": instance_id,
            "version": peer_version,
            "local_version": kiro_crew.__version__,
            # The major.minor version gate, surfaced so the UI
            # can explain a refusal BEFORE the user types a message rather than
            # after their first send fails.
            "version_match": version_match,
            # Exact-version skew, independent of the compatibility gate above:
            # a patch-behind peer still chats, but the header says it is behind.
            "behind": behind,
            "agents": _cap_rows(
                _cap_list(agents_payload, "agents"),
                {"name": 128, "description": _CAP_MAX_STR, "scope": 32, "model": 128},
            ),
            # What answers when the bound session has picked nothing. A local
            # session falls back to THIS machine's default agent, which for a
            # peer-bound session would name a crew the peer may not have — so the
            # shelf needs the peer's own default to render honestly before the
            # first pick.
            "default_agent": (
                _cap_str(agents_payload.get("default_agent"), 128)
                if isinstance(agents_payload, dict)
                else ""
            ),
            "models": _cap_rows(
                _cap_list(raw.get("models"), "models"),
                {
                    "model_name": 128,
                    "display_name": 128,
                    "description": _CAP_MAX_STR,
                    "context_window": 0,
                },
            ),
            "effort_levels": effort_levels,
            "workspaces": workspaces,
            "default_workspace": (
                _cap_str(workspaces_payload.get("default"), 128)
                if isinstance(workspaces_payload, dict)
                else ""
            ),
            "unavailable": unavailable,
        }
    )


async def api_instances_approval_state(request: web.Request) -> web.Response:
    """GET /api/instances/{id}/approval-state — a connected peer's machine-wide YOLO flag.

    The crew window's approval-mode picker must show the mode the peer is really
    in, and a peer's YOLO is not on its slot rows. So this reads the peer's
    ``/api/status`` through the closed capability carrier and forwards one field
    and nothing else: ``yolo``, ``None`` when the read failed or carried no
    boolean, so the window can tell "off" from "unknown". A route of its own, not
    a field of ``/capabilities``: the window polls it, and the roster reads must
    not ride every poll. Owner-only, the same bar as the capability read.
    """
    denied = _guard(request, "approval_state")
    if denied is not None:
        return denied
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        _audit("approval_state", "denied", error="non-owner identity rejected")
        return _owner_denial_response(request, "remote-crew approval state is owner-only")
    state: DashboardState = request.app["state"]
    instance_id = request.match_info.get("id", "")
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("approval_state", "denied", error="instances manager unavailable")
        return _manager_unavailable_response()
    peer_yolo: bool | None = None
    try:
        ok, payload = await mgr.peer_capability(instance_id, "/api/status")
    except Exception as e:  # a dead tunnel reports "unknown", never a 500
        logger.info("Peer status on %s raised (%s)", instance_id, type(e).__name__)
        ok, payload = False, {"code": "capability_unreachable"}
    if ok and isinstance(payload, dict):
        yolo = payload.get("yolo")
        # Only a real boolean is an answer: a missing or odd value is unknown,
        # never "off", which would offer a Normal pick that ends a live YOLO.
        peer_yolo = yolo if isinstance(yolo, bool) else None
    _audit("approval_state", "success", request_id=instance_id)
    return web.json_response({"yolo": peer_yolo})


async def api_instances_versions(request: web.Request) -> web.Response:
    """GET /api/instances/versions — every CONNECTED crew's version beside ours.

    One read for the whole crew list, so the Remote Crew rows and the
    post-update offer can show which remotes run an older build without opening
    a capabilities read per row. Each version comes from the peer's own
    ``/api/version`` through :meth:`SshTunnelManager.peer_capability`, the same
    closed-path carrier capabilities uses. A peer that does not answer reports
    ``version: ""`` and is never ``behind``. ``behind`` is the exact-version
    compare of :func:`_remote_is_behind`.

    Owner-only on top of ``_guard``: the reads run on each peer with the owner's
    manager-held credential.
    """
    denied = _guard(request, "versions")
    if denied is not None:
        return denied
    if not is_owner_dashboard_request(request):
        _audit("versions", "denied", error="non-owner identity rejected")
        return _owner_denial_response(request, "remote-crew versions are owner-only")
    state: DashboardState = request.app["state"]
    local = kiro_crew.__version__
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("versions", "success", request_id="no-manager")
        return web.json_response({"local_version": local, "instances": []})
    connected = [
        iid
        for iid, st in mgr.status_all().items()
        if getattr(st, "state", None) is TunnelState.CONNECTED
    ]
    results = await asyncio.gather(
        *(mgr.peer_capability(iid, "/api/version") for iid in connected),
        return_exceptions=True,
    )
    names: dict[str, str] = {}
    updatable: dict[str, bool] = {}
    if connected:
        try:
            for inst in await asyncio.to_thread(_registry(state).list):
                names[inst.id] = inst.name
                updatable[inst.id] = _can_run_remote_commands(inst)
        except Exception:
            names, updatable = {}, {}
    rows: list[dict[str, object]] = []
    for iid, outcome in zip(connected, results):
        version = ""
        # Why this crew's version is unknown, as the carrier's own code, so a
        # failed read is reported rather than looking like a current crew.
        read_error = ""
        if isinstance(outcome, BaseException):
            logger.info("Peer version read on %s raised (%s)", iid, type(outcome).__name__)
            read_error = "capability_unreachable"
        else:
            ok, payload = outcome
            if ok and isinstance(payload, dict):
                version = _cap_str(payload.get("version"), _CAP_MAX_VERSION_STR)
            elif not ok:
                read_error = (
                    _cap_str(payload.get("code"), 64) if isinstance(payload, dict) else ""
                ) or "capability_error"
        rows.append(
            {
                "id": iid,
                "name": _cap_str(names.get(iid) or iid, 128),
                "version": version,
                "behind": _remote_is_behind(local, version),
                "updatable": updatable.get(iid, False),
                "error": read_error,
            }
        )
    _audit("versions", "success")
    return web.json_response({"local_version": local, "instances": rows})


#: Every field ``useInstanceSessions.ts`` reads off a peer slot, with the clamp
#: each one gets. An ALLOWLIST rather than a passthrough, for the same reason as
#: ``_cap_rows``: the peer's ``/api/chat/slots`` projection carries far more than
#: this — a message preview, the pending tool's input and kind, the option
#: labels, source links, todo and MCP payloads — none of which this list renders.
#: Forwarding them would hand the browser peer-authored text no local code path
#: has a use for, and a peer on a build with extra fields could put content into
#: a row through a key this gateway has never heard of. Timestamps are clamped
#: short because they are PARSED as ISO-8601 instants, never shown as prose.
_PEER_SLOT_STR_FIELDS: dict[str, int] = {
    "key": _PEER_FIELD_MAX_CHARS,
    "title": _CAP_MAX_STR,
    "agent": 128,
    "last_turn_ts": 64,
    "last_ts": 64,
    "created": 64,
    # The PEER's sidebar folder the row is filed in, an id in the peer's own folder
    # space. The crew group nests the row under that folder, whose name the browser
    # reads from the peer's ``api/chat/folders`` through the proxy. Clamped like
    # ``key``: an opaque id, matched, never shown.
    "folder_id": _PEER_FIELD_MAX_CHARS,
    # The peer's Crew Mode stamp on a worker: its root's key in the PEER's key
    # space, so the person's list on this dashboard leaves the peer's workers out
    # the way it leaves local ones out. An opaque id, matched, never shown.
    "crew_root": _PEER_FIELD_MAX_CHARS,
}


#: The booleans the sidebar reads, coerced with ``is True`` rather than
#: truth-tested: a peer answering ``"running": "no"`` must not raise a spinner, an
#: approval badge or a question it never claimed. The hook normalizes these too —
#: doing it here as well is what makes the WIRE honest, so this route answers the
#: same way for any reader, not only the one frontend that happens to re-check.
_PEER_SLOT_BOOL_FIELDS = ("running", "pending_approval", "needs_input")


#: The two keys ALLOWLISTED from a peer row's ``parent`` citation (``{slot, key}``,
#: the shape ``lineage_parents`` puts on every local row), each clamped like
#: ``key``. The two answer different questions in the sidebar. ``key`` is a bare
#: key in the PEER's key space and is what the conductor lane nests a
#: peer-to-peer citation on, resolved against rows of the same origin only.
#: ``slot`` is the child's own record of who opened it -- the "opened by" glyph
#: on a row placed under nothing (``orphanCitation``, ``citesParent``) and the baseline the lane diffs
#: to tell a re-parented row from a new one (``citedCreatorRef``).
#: ``lineage_parents`` leaves ``key`` null when the creator is gone and keeps
#: ``slot``, so a citation with only ``slot`` is the orphan case, not a malformed
#: one. Dropped from the wire, every session a peer's conductor opened rendered
#: at the top level of this dashboard as a stray -- the tree existed on the peer
#: and was stripped one hop from the reader.
_PEER_SLOT_PARENT_FIELDS: dict[str, int] = {
    "slot": _PEER_FIELD_MAX_CHARS,
    "key": _PEER_FIELD_MAX_CHARS,
}


def _clean_peer_parent(value: object) -> dict[str, str] | None:
    """Shape a peer row's ``parent`` citation, or ``None`` when it carries none.

    A citation is a dict with a string ``key`` or a string ``slot``; one that has
    neither nests nothing and names nobody, so it is treated as no citation at all
    rather than forwarded as an empty object. Anything else -- ``None``, a string,
    a list -- is not a citation.

    """
    if not isinstance(value, dict):
        return None
    out: dict[str, str] = {}
    for field, limit in _PEER_SLOT_PARENT_FIELDS.items():
        shaped = _cap_str(value.get(field), limit)
        if shaped:
            out[field] = shaped
    return out or None


def _clean_peer_slot(row: object) -> dict[str, object] | None:
    """Re-shape one untrusted peer slot: allowlist keys, redact, clamp, coerce.

    What ``_clean`` does for a peer's SEARCH row, applied to a peer's LIVE row.
    Strings go through ``_cap_str``, the sink registered for peer text on this
    boundary, which redacts BEFORE clamping so a credential cannot survive by
    sitting past the limit — safe to run on whole fields here because the reply
    was already bounded to ``PEER_SLOTS_REPLY_MAX_BYTES`` before it was decoded.
    The peer redacts its own copy through this same chain, which makes the pass
    idempotent in the healthy case and is exactly why it is cheap enough to not
    depend on the peer having run it.

    Empty results are OMITTED rather than sent as ``""``: an absent title must
    stay absent so the row falls back to its placeholder instead of rendering a
    blank label.

    Returns ``None`` only for a non-dict, which is not a row at all. A row whose
    ``key`` is missing or unusable is still returned, shaped — the hook drops it
    on its own ``typeof s.key !== 'string'`` guard.
    """
    if not isinstance(row, dict):
        return None
    out: dict[str, object] = {}
    for field, limit in _PEER_SLOT_STR_FIELDS.items():
        value = _cap_str(row.get(field), limit)
        if value:
            out[field] = value
    for field in _PEER_SLOT_BOOL_FIELDS:
        out[field] = row.get(field) is True
    # Both OMITTED when absent, the distinction the local payload keeps: a row with
    # no citation carries no ``parent`` (the lane reads absence and ``None`` alike),
    # and ``lineage_pending`` appears only on the frame whose citations are still
    # provisional -- the lane skips such a frame when it records what the user has
    # opened, and a ``False`` here would look like a settled frame to a reader that
    # tests presence.
    parent = _clean_peer_parent(row.get("parent"))
    if parent is not None:
        out["parent"] = parent
    if row.get("lineage_pending") is True:
        out["lineage_pending"] = True
    # ``True`` on a peer's Crew Mode root; omitted otherwise, like the local payload
    # reads it. Only a real ``True`` counts: a string or number is dropped.
    if row.get("crew_mode_root") is True:
        out["crew_mode_root"] = True
    return out


class PeerSlotsUnavailable(Exception):
    """A peer's live slot list could not be read.

    Carries the answer BOTH readers of :func:`read_peer_slots` need: the wire
    ``code``/``message``/``status`` the chat-slots route returns verbatim, and the
    short ``audit_detail`` each caller logs under its own operation name. The
    adopt path translates every one of these to its own single code instead
    (``remote_bind_failed``), because from the caller's side "the crew could not
    be asked" is one outcome however the read failed.
    """

    def __init__(
        self, code: str, message: str, status: int, audit_detail: str, outcome: str = "failure"
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.audit_detail = audit_detail
        #: The SEL outcome the chat-slots route logs. A missing instances manager
        #: is a REFUSAL of the request (the feature is not there), every other
        #: case is a failure of a read that was attempted — the distinction the
        #: route drew before this read was extracted, kept here so extracting it
        #: did not quietly relabel one audit line.
        self.outcome = outcome


class PeerSlots(NamedTuple):
    """The outcome of one peer slot-list read.

    ``rows`` are the peer's OWN rows, exactly as it sent them — unshaped, because
    the two readers need different fields off them: the chat-slots route shapes
    them through :func:`_clean_peer_slot` for the browser, while the adopt path
    reads fields that allowlist deliberately omits (``memory_mode``, the privacy
    boundary an adopted session must inherit rather than default). ``over_cap``
    is the audit count the route reports.
    """

    rows: list[dict[str, object]]
    over_cap: int


async def read_peer_slots(
    state: "DashboardState",
    instance_id: str,
) -> PeerSlots:
    """Read *instance_id*'s live slot list.

    The read behind :func:`api_instances_chat_slots`. The returned rows are
    capped at ``MAX_LIVE_SLOTS`` (read in the body, so a test patching the module
    constant still moves it) to bound the per-row redaction the route then runs.
    Memory stays bounded by the BYTE cap below, which is applied before anything
    is decoded.

    Raises :class:`PeerSlotsUnavailable` for every failure; returns
    :class:`PeerSlots` otherwise.
    """
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        raise PeerSlotsUnavailable(
            MANAGER_UNAVAILABLE_CODE,
            MANAGER_UNAVAILABLE_MESSAGE,
            503,
            "instances manager unavailable",
            outcome="denied",
        )

    try:
        # ``crew=all``: the hub reads with the peer token, which the peer treats
        # as a person's read, and a person's default view leaves Crew Mode
        # workers out (``crew_mode.list_view``). The merged list keeps them.
        async with mgr.proxy_request(
            instance_id, "GET", "api/chat/slots", params={"crew": CREW_VIEW_ALL}
        ) as upstream:
            if not 200 <= upstream.status < 300:
                raise PeerSlotsUnavailable(
                    "peer_slots_refused",
                    "the crew refused to list its sessions",
                    502,
                    f"peer HTTP {upstream.status}",
                )
            # Shared drain-to-EOF primitive. It preserves the established
            # ``len(raw) > cap`` sentinel while stopping at ``cap + 1`` bytes;
            # this caller still owns the peer-specific interrupted-body outcome.
            try:
                raw = await read_capped_response(upstream, PEER_SLOTS_REPLY_MAX_BYTES)
            except Exception as e:
                # An INTERRUPTED body is its own failure, and without this it was
                # a 500. The carrier only wraps the phase BEFORE the response —
                # once it has yielded, a tunnel that drops mid-body
                # (``ClientConnectionError``), a chunked reply that ends short of
                # its declared length (``ClientPayloadError``) and the read-idle
                # timeout expiring all raise straight through this handler. An
                # unhandled one is a peer-side fault reported as a hub bug, with
                # no code for the sidebar's banner to name, so it gets one beside
                # the refused / oversized / malformed replies.
                #
                # REFUSED rather than decoding what did arrive, even though a
                # prefix that happens to parse is possible: a short list and a
                # complete one are indistinguishable here, so accepting it would
                # silently drop sessions from the merged list — the same
                # symptom, and the same wrong diagnosis, as a truncating read.
                #
                # Only the exception TYPE is logged or returned, following the
                # federated-search read this is modelled on: never the partial
                # body (untrusted peer bytes) and never the credential.
                # ``asyncio.CancelledError`` is a BaseException, so it is
                # deliberately NOT absorbed here — swallowing a cancel would
                # defeat cooperative shutdown.
                logger.info(
                    "chat-slots read from %s was interrupted (%s)",
                    instance_id,
                    type(e).__name__,
                )
                raise PeerSlotsUnavailable(
                    "peer_slots_interrupted",
                    "the crew stopped answering partway through its session list",
                    502,
                    f"interrupted reply ({type(e).__name__})",
                ) from None
    except ProxyRequestError as e:
        # Literal statuses for the same reason as the proxy route: the carrier
        # only ever suggests 503 (not connected / no credential) or 502.
        raise PeerSlotsUnavailable(
            e.code, e.message, 503 if e.http_status == 503 else 502, e.code
        ) from None

    if len(raw) > PEER_SLOTS_REPLY_MAX_BYTES:
        raise PeerSlotsUnavailable(
            "peer_slots_too_large",
            "the crew returned an oversized session list",
            502,
            "oversized reply",
        )
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        # `RecursionError` is NOT a `ValueError`, and this input is a peer's.
        # `json.loads` recurses per nesting level, so a deeply nested document --
        # 10,000 open brackets is a few KB, far under the byte cap above -- raises
        # it instead of a parse error, and an uncaught one leaves this route as an
        # HTTP 500 rather than the 502 every other unreadable reply produces.
        # Both mean the same thing here: the reply is not something we can read.
        payload = None
    if not isinstance(payload, list):
        raise PeerSlotsUnavailable(
            "peer_slots_malformed",
            "the crew returned a malformed session list",
            502,
            "malformed reply",
        )

    rows = payload

    # A ROW cap as well as the byte cap, because the two bound different costs.
    # The byte cap bounds what is BUFFERED; this bounds what is then PROCESSED,
    # and the per-row work is not free — every string field of every surviving
    # row runs the peer-text redaction chain in ``_cap_str``. ``{"key":"x"},`` is
    # twelve bytes, so a hostile or broken peer fits hundreds of thousands of
    # rows under 4 MiB and spends the hub's CPU in the redactor rather than its
    # memory. A byte cap is not a row cap, which is why the capability reader
    # above carries both.
    #
    # ``MAX_LIVE_SLOTS`` rather than ``_CAP_MAX_ROWS`` even though both are 500
    # today, because only one of them is DERIVED: it is the ceiling a Kiro Crew
    # gateway enforces on its own live slots at every path that allocates one,
    # so a peer running this software cannot honestly answer with more rows than
    # that, and a raised ceiling should widen this read along with it. The
    # capability cap is a judgement about how long a PICKER may usefully be and
    # has no reason to move when the slot ceiling does.
    #
    # TRUNCATED rather than refused, unlike the byte cap: bytes past that cap are
    # only ever garbage, but a peer on a newer build with a higher ceiling is
    # honest and a refusal would cost it every row. Audited so a short list is
    # attributable to this bound instead of looking like sessions the peer never
    # reported.
    over_cap = max(0, len(rows) - MAX_LIVE_SLOTS)
    if over_cap:
        rows = rows[:MAX_LIVE_SLOTS]
    return PeerSlots(rows=rows, over_cap=over_cap)


async def api_instances_chat_slots(request: web.Request) -> web.Response:
    """GET /api/instances/{id}/chat-slots — a peer's LIVE sessions.

    Backs the merged-sessions sidebar preview, where a connected peer's open
    sessions appear as ordinary rows in this machine's Sessions list. Read-only:
    a peer-owned session offers no rename, close or pin, because those are
    local-slot operations that cannot reach a session on another machine.

    Rows are re-shaped by ``_clean_peer_slot`` rather than
    forwarded as the peer sent them. A slot title is MODEL-AUTHORED text from
    another machine, and ``slot_projection.py`` redacts a LOCAL slot's title
    (``redact(slot.display_title)``) before the browser ever sees it — so an
    unshaped peer row would have been the one row in that merged list whose text
    never met a redactor. Runtime TYPE validation still lives in the sidebar
    hook, which is the only place that knows what it renders; what happens here
    is field selection and redaction, neither of which the browser can do for
    itself once the bytes have already arrived.

    The reply is bounded twice, in BYTES before decoding and in ROWS before
    shaping, because the two bound different costs — see each bound's own note
    below. An interrupted read is refused with its own code rather than decoded
    from whatever arrived: a short session list is indistinguishable from a
    complete one here, so accepting one would silently lose sessions.

    This route is GET-only, which is narrower than the ``("api", "chat")`` proxy
    row it replaces for this read — that row admits the peer's mutating verbs
    too, and nothing here needs them.
    """
    denied = _guard(request, "chat_slots")
    if denied is not None:
        return denied
    # Owner-only, the same bar as the proxy and the capability read: this executes
    # on a peer with the OWNER's manager-held credential and discloses every one
    # of that peer's open session titles, so a Slack-minted `!dashboard` subject
    # (an authenticated NON-owner) must not reach it.
    if not is_owner_dashboard_request(request):
        _audit("chat_slots", "denied", error="non-owner identity rejected")
        return _owner_denial_response(request, "remote-crew session list is owner-only")
    state: DashboardState = request.app["state"]
    instance_id = request.match_info.get("id", "")
    try:
        peer = await read_peer_slots(state, instance_id)
    except PeerSlotsUnavailable as e:
        _audit("chat_slots", e.outcome, request_id=instance_id, error=e.audit_detail)
        return web.json_response({"error": e.message, "code": e.code}, status=e.status)

    shaped = [
        cleaned for cleaned in (_clean_peer_slot(row) for row in peer.rows) if cleaned is not None
    ]
    # Stamp the row identity HERE, so the server is the only author of it.
    # After the allowlist, so a peer's own `row_identity` cannot reach the browser:
    # the value is composed from the instance id this route was called with plus
    # the row's allowlisted `key`.
    for row_out in shaped:
        key = row_out.get("key")
        if isinstance(key, str) and key:
            row_out["row_identity"] = f"{instance_id}:{key}"
    detail = f"{len(shaped)} rows"
    if peer.over_cap:
        detail += f", {peer.over_cap} past the row cap"
    _audit("chat_slots", "success", request_id=f"{instance_id} ({detail})")
    return web.json_response(shaped)


class ProxyReplyUnredactable(Exception):
    """A peer reply the redactor cannot walk (nested past the recursion limit)."""


def _redact_peer_value(value: object) -> object:
    """Redact every string in a decoded peer JSON value, keys included."""
    if isinstance(value, str):
        return redact_peer_text(value)
    if isinstance(value, list):
        return [_redact_peer_value(v) for v in value]
    if isinstance(value, dict):
        return {redact_peer_text(str(k)): _redact_peer_value(v) for k, v in value.items()}
    return value


def _redact_peer_payload(text: str) -> str:
    """Redact one peer JSON document, or the raw text when it is not JSON.

    Redacting the decoded strings rather than the serialized text keeps a
    credential split by a JSON escape (``\\u0041KIA…``) visible to the
    redactor, and keeps the output valid JSON.
    """
    # A leading BOM: the browser's JSON parser skips it, Python's refuses it,
    # and a raw-text fallback would miss an escaped credential behind it.
    text = text.removeprefix("\ufeff")
    try:
        decoded = json.loads(text)
        return json.dumps(_redact_peer_value(decoded))
    except RecursionError:
        # Too deeply nested to walk: refuse rather than forward it unredacted.
        raise ProxyReplyUnredactable() from None
    except json.JSONDecodeError:
        # Not JSON at all (a plain SSE data line): redact it as text.
        return redact_peer_text(text)
    except ValueError:
        # Valid JSON Python will not decode (an integer past the digit limit):
        # the browser parses it, so a raw-text pass could miss an escaped
        # credential. Refuse it.
        raise ProxyReplyUnredactable() from None


def _redact_sse_event(event: bytes) -> bytes:
    """Redact one SSE event block (no trailing blank line, ``\\n`` line ends).

    The browser joins an event's ``data:`` lines into ONE payload before it
    parses it, so the payload is joined and redacted the same way here: a
    credential split across two lines, or behind a JSON escape, is caught. It
    is re-emitted as one ``data:`` line (a JSON re-serialization has no raw
    newline). Every other line (``event:``, ``id:``, comments) is peer text
    as well and runs the same chain as plain text.
    """
    out: list[str] = []
    data: list[str] = []
    for line in event.decode("utf-8", "replace").split("\n"):
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        else:
            out.append(redact_peer_text(line))
    if data:
        clean = _redact_peer_payload("\n".join(data))
        out.extend("data: " + part for part in clean.split("\n"))
    return "\n".join(out).encode()


async def _redact_sse_event_async(event: bytes) -> bytes:
    """`_redact_sse_event`, off the loop for a large event (a peer `slots`
    broadcast can run to megabytes)."""
    if len(event) > 65536:
        return await asyncio.to_thread(_redact_sse_event, event)
    return _redact_sse_event(event)


async def api_instances_proxy(request: web.Request) -> web.StreamResponse:
    """ANY /api/instances/{id}/proxy/{path} — forward to a connected peer.

    The carrier for the remote-crew chat view (design: remote-crew-chat): the
    browser talks same-origin to the hub, the hub forwards over the already-open
    tunnel using the manager-held credential, and the reply — including a
    minutes-long SSE chat stream — is pumped back chunk-by-chunk. The peer's
    token never reaches the browser, no browser Origin or cookies are forwarded
    to the peer (the hub presents as a same-origin loopback client), and the
    peer's Set-Cookie never reaches the hub origin.

    Every reply is redacted with the peer-text chain before the
    browser sees it, because the window renders peer text directly: a JSON
    body as one document, an SSE stream one event at a time.
    """
    denied = _guard(request, "proxy")
    if denied is not None:
        return denied
    # Owner-only, strictly: `_guard` verifies an authenticated dashboard subject,
    # but a Slack-invited user who minted a `!dashboard` link is such a subject
    # too (app == "" with a non-owner identity). The proxy executes on a peer
    # with the OWNER's manager-held credential, so it requires the positively
    # identified owner — the same bar api_instances_search_sessions sets, for
    # the same reason.
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        # Domain audit stays here for the same reason as
        # ``api_instances_search_sessions`` in ``handlers_instances`` (the facade):
        # ``_audit``, defined in ``handlers_instances_gate``, is the ``instances_*``
        # SEL stream. Only the denial tail is shared.
        _audit("proxy", "denied", error="non-owner identity rejected")
        # Deny decision made above; only the response label changes for a signed
        # pre-owner bootstrap subject (see stale_owner_session_response).
        return _owner_denial_response(request, "remote-crew proxy is owner-only")
    state: DashboardState = request.app["state"]
    instance_id = request.match_info["id"]
    path = request.match_info.get("path", "")
    if request.method.upper() not in _PROXY_METHODS:
        _audit("proxy", "denied", request_id=instance_id, error="method not allowed")
        return web.json_response(
            {"error": "method not allowed", "code": "proxy_method_not_allowed"}, status=405
        )
    # The forwarded path is the CANONICAL one this returns, never the raw
    # match_info: vetting one string and sending another is the gap that let a
    # double-encoded traversal through.
    path, reason = _proxy_canonical_path(path)
    if reason:
        _audit("proxy", "denied", request_id=instance_id, error=reason)
        return web.json_response({"error": reason, "code": "proxy_path_denied"}, status=400)
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        _audit("proxy", "failure", request_id=instance_id, error="manager unavailable")
        return _manager_unavailable_response()

    # Forward the query WITHOUT the hub's own credential: the browser may
    # authenticate this request with ?token=<hub token>, and forwarding it
    # verbatim would hand the peer a replayable credential for THIS gateway.
    # The peer-side credential is the manager-held cookie; nothing from the
    # browser's auth material may cross the tunnel.
    params = {k: v for k, v in request.query.items() if k != "token"}

    # Bound the inbound body BEFORE buffering (mirrors the federated-search
    # reply cap): the hub must not hold unbounded bytes for either side.
    body = b""
    if request.body_exists:
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.content.iter_chunked(65536):
            received += len(chunk)
            if received > PROXY_REQUEST_BODY_MAX_BYTES:
                _audit("proxy", "denied", request_id=instance_id, error="body too large")
                return web.json_response(
                    {"error": "request body too large", "code": "proxy_body_too_large"},
                    status=413,
                )
            chunks.append(chunk)
        body = b"".join(chunks)

    try:
        async with mgr.proxy_request(
            instance_id,
            request.method.upper(),
            path,
            params=params,
            data=body or None,
            content_type=request.headers.get("Content-Type", ""),
        ) as upstream:
            # Content-type gate: JSON and SSE only. A compromised peer must
            # not serve HTML (or anything active) that would execute on the
            # authenticated hub origin.
            upstream_ct = (upstream.headers.get("Content-Type") or "").split(";")[0].strip()
            if upstream_ct.lower() not in _PROXY_RESP_ALLOW_CONTENT_TYPES:
                _audit(
                    "proxy",
                    "denied",
                    request_id=instance_id,
                    error=f"peer content type refused: {upstream_ct or '(none)'}",
                )
                return web.json_response(
                    {
                        "error": "peer returned a content type the proxy does not forward",
                        "code": "proxy_content_type_refused",
                    },
                    status=502,
                )
            if upstream_ct.lower() == "application/json":
                # Buffered whole: a JSON body redacts as one document, and it
                # must not reach the browser before it is redacted.
                raw = bytearray()
                async for chunk in upstream.content.iter_any():
                    raw += chunk
                    if len(raw) > PROXY_REDACT_BUFFER_MAX_BYTES:
                        _audit("proxy", "denied", request_id=instance_id, error="reply too large")
                        return web.json_response(
                            {"error": "peer reply too large", "code": "proxy_reply_too_large"},
                            status=502,
                        )
                try:
                    text = await asyncio.to_thread(
                        _redact_peer_payload, raw.decode("utf-8", "replace")
                    )
                except ProxyReplyUnredactable:
                    _audit("proxy", "denied", request_id=instance_id, error="reply unredactable")
                    return web.json_response(
                        {
                            "error": "peer reply could not be redacted",
                            "code": "proxy_reply_unredactable",
                        },
                        status=502,
                    )
                _audit("proxy", "success", request_id=instance_id)
                return web.Response(
                    status=upstream.status,
                    body=text.encode(),
                    content_type="application/json",
                    headers={"X-Content-Type-Options": "nosniff"},
                )
            resp = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() in _PROXY_RESP_ALLOW_HEADERS:
                    resp.headers[key] = value
            resp.headers["X-Content-Type-Options"] = "nosniff"
            await resp.prepare(request)
            try:
                pending = b""
                first = True
                async for chunk in upstream.content.iter_any():
                    # SSE allows CR and CRLF line ends; the browser honours
                    # them, so framing is normalised before events are cut.
                    # A CR at a chunk edge waits one chunk for its LF.
                    pending += chunk
                    if first:
                        # The stream's BOM, likewise, before fields are read;
                        # a BOM split across chunks waits for its last byte.
                        if len(pending) < 3 and b"\xef\xbb\xbf".startswith(pending):
                            continue
                        pending = pending.removeprefix(b"\xef\xbb\xbf")
                        first = False
                    hold = pending.endswith(b"\r")
                    if hold:
                        pending = pending[:-1]
                    pending = pending.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    *events, pending = pending.split(b"\n\n")
                    if hold:
                        pending += b"\r"
                    for event in events:
                        if len(event) > PROXY_REDACT_BUFFER_MAX_BYTES:
                            pending = event
                            break
                        try:
                            clean = await _redact_sse_event_async(event)
                        except ProxyReplyUnredactable:
                            _audit(
                                "proxy",
                                "partial",
                                request_id=instance_id,
                                error="event unredactable",
                            )
                            return resp
                        await resp.write(clean + b"\n\n")
                    if len(pending) > PROXY_REDACT_BUFFER_MAX_BYTES:
                        # Fail closed: an event too large to redact is dropped
                        # with the rest of the stream, never forwarded raw.
                        _audit("proxy", "partial", request_id=instance_id, error="event too large")
                        return resp
                if pending:
                    tail = pending.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    try:
                        await resp.write(await _redact_sse_event_async(tail))
                    except ProxyReplyUnredactable:
                        _audit(
                            "proxy", "partial", request_id=instance_id, error="event unredactable"
                        )
                        return resp
            except ConnectionResetError:
                # Browser went away mid-stream; the peer finishes its turn on
                # its own (its transcript is authoritative — see design doc).
                # Deliberately NOT catching asyncio.CancelledError alongside it:
                # absorbing a cancel would defeat cooperative shutdown.
                #
                # RETURN rather than fall through: write_eof() on the transport
                # that just refused a write raises again, and that second
                # exception escapes the handler. There is also nothing to audit
                # as a success — the response never completed.
                _audit("proxy", "partial", request_id=instance_id, error="client disconnected")
                return resp
            await resp.write_eof()
            _audit("proxy", "success", request_id=instance_id)
            return resp
    except ProxyRequestError as e:
        _audit("proxy", "failure", request_id=instance_id, error=e.code)
        # Literal statuses on purpose: the error-code contract ratchets
        # ``status=<expression>`` sites, and the carrier only ever suggests
        # 503 (not connected / no credential) or 502 (peer-side failure).
        if e.http_status == 503:
            return web.json_response({"error": e.message, "code": e.code}, status=503)
        return web.json_response({"error": e.message, "code": e.code}, status=502)


# Each exception class is named by the module callers import it from: a
# raised class renders as ``module.qualname`` in a traceback and an error chain.
PeerSlotsUnavailable.__module__ = "kiro_crew.dashboard.handlers_instances"
ProxyReplyUnredactable.__module__ = "kiro_crew.dashboard.handlers_instances"
