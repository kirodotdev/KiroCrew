"""Chat with a HEADLESS remote crew, through the gateway rather than the browser.

A remote Kiro Crew GATEWAY is reached by embedding its own dashboard: the hub
opens a tunnel, mints a dashboard token, and the iframe does the rest. A headless
crew has no dashboard to embed. It serves one turn route -- the OpenAI-shaped
``POST /v1/chat/completions`` of
``apps/builtins/aws_control/crew/runtime/container/front`` -- and nothing else,
so the hub has to be the client.

That is what this module is: the hub's side of a conversation with a crew that
cannot draw its own.

WHY THE GATEWAY AND NOT THE BROWSER
-----------------------------------
The turn route authenticates every caller with the deployment's per-crew control
secret. Three reasons that secret never leaves this process:

* it lives in the owner's Secrets Manager and is read with the owner's AWS
  credentials, which the browser does not have and must not be given;
* it is the SAME secret that gates the crew's control surface, so a copy in a
  browser tab is a copy in every page that tab ever loads;
* the crew is reachable only on a loopback port of THIS machine (the far end of
  an SSM port-forward), which a browser on another machine cannot dial anyway.

So the browser talks to this gateway same-origin, with its own dashboard token,
and this gateway talks to the crew. One hop, two credentials, neither crossing.

ONE LANE
--------
Only the MicroVM lane is served (:data:`TURN_LANE`). The route needs a connected
instance whose far end is a headless crew front and whose control secret and
crew name the launch recorded; a Fargate crew is declined for the reason
:data:`TURN_LANE` gives.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Optional

import aiohttp
from aiohttp import web

from kiro_crew.cloud.connect import FARGATE_TURN_PATH
from kiro_crew.dashboard.handlers_instances import (
    ProxyReplyUnredactable,
    _audit,
    _redact_peer_payload,
    _redact_sse_event_async,
)
from kiro_crew.instances.constants import PROXY_REDACT_BUFFER_MAX_BYTES
from kiro_crew.instances.registry import HEADLESS_CREW_PROVISIONERS as _REGISTRY_HEADLESS
from kiro_crew.platform.defaults import FARGATE_PROVISIONER_ID, MICROVM_PROVISIONER_ID

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: The header the crew's front process reads its control secret from. One
#: definition, in the container's ``common``; named here as a constant so the
#: value cannot drift, and asserted against the container's own constant by a
#: test rather than copied by eye.
CONTROL_SECRET_HEADER = "X-SMC-Control-Secret"

#: The shape a per-crew control secret's name has, as the lane writes it: the
#: path the lane's own launch spec builds (``MicroVmLaunchSpec.control_secret_name``)
#: from the crew's launch tag, so the hub derives the name itself instead of
#: being told a path by the caller. Used only to DERIVE an id when the lane
#: recorded none; the recorded reference is always preferred, because it is what
#: the launch actually minted.
_SECRET_PATH = "{prefix}/{crew}/CONTROL_SECRET"


#: The one lane this route serves.
#:
#: A Fargate crew is a headless crew too, and serving it here would need the crew
#: name and the control secret THIS task ran with. Both are resolvable only from
#: the operator's configuration, which is a live document naming whichever crew is
#: configured now -- so a second crew configured in the same account and region
#: would make a turn to the first one present the second one's credential, to a
#: task that must not see it. Resolving it correctly means recording the
#: deployment on the crew's row at launch, which is a change to that lane's launch
#: path and belongs with that lane.
#:
#: So this route declines a Fargate crew instead, and declining is the whole
#: mechanism: there is no branch to get wrong, and no reader of the live
#: configuration on this path at all.
TURN_LANE = MICROVM_PROVISIONER_ID


def control_secret_id(inst: Any) -> str:
    """This crew's control secret, by NAME, or ``""``.

    Read from the record this instance IS -- found by ``_record_for`` on the node
    id the launch registered -- and never from the instance's display name. The
    display name is a label and the row carrying it is agent-writable, so a label
    cannot be allowed to choose a secret: see ``_record_for`` for the relabelling
    it would otherwise permit. The record holds the reference the LAUNCH minted,
    which is also the only value guaranteed to name the right secret, since the
    Fargate lane's own label (``Kiro Crew Cloud (<tag>)``) is not a valid secret
    name at all.

    Derives the name from the operator's configured prefix -- not the hardcoded
    default, so a moved prefix is honoured -- only when the record was already
    matched and simply carries no reference, where the tag used is the RECORD's
    own. A row that owns no record gets ``""``, which the caller reports as a
    crew it cannot authenticate to rather than guessing a name for it.
    """
    try:
        from kiro_crew.cloud.config import CloudConfig
    except Exception:  # noqa: BLE001 - no lane, no reference
        return ""
    record = _record_for(inst)
    if record is None:
        return ""
    if record.control_secret_ref:
        return record.control_secret_ref
    # No reference stored, but the row IS this record's: the tag is the record's
    # own, not a label the caller supplied, so deriving a path from it names this
    # crew's secret and no other.
    config = CloudConfig.load().microvm_config()
    prefix = config.secret_path_prefix if config else "kirocrew/crew"
    return _SECRET_PATH.format(prefix=prefix, crew=record.tag)


def _record_for(inst: Any) -> Any:
    """The crew record this instance IS, or ``None``.

    Matched on ``ssm_target`` -- the ``mi-`` node id the launch registered -- and
    not on the row's tag, display name or registry id, because ``instances.json``
    is not write-protected and a row is therefore agent-writable while the crew
    record beside it is not.

    That gap is the whole reason this function exists. A tag match binds the
    credential to a LABEL while the tunnel stays bound to the NODE, and the two
    can be made to disagree: relabel a connected hostile peer's row with a real
    crew's tag, lane and AWS coordinates, and the owner's next turn reads the
    REAL crew's control secret and sends it down the HOSTILE peer's already-open
    tunnel.

    ``ssm_target`` is the field that closes it, because it is the field the
    tunnel is built from: the forward dials ``validate_ssm_target(inst.
    ssm_target)``, so a row whose ``ssm_target`` owns no record gets no
    credential, and a row that carries a real crew's ``ssm_target`` IS that crew
    as far as the tunnel is concerned. The registry id cannot stand in for it.
    ``connect.register_instance`` calls ``reg.add(name=tag, ssm_target=<mi-id>)``
    and ``add`` derives the id from the NAME, so the id of every row this lane
    writes is a slug of the launch tag -- a label, and one no record carries.

    The account and region are checked too, not for the tunnel but for the READ:
    the caller passes the ROW's ``aws_profile`` and ``aws_region`` to
    Secrets Manager, so a row that keeps the node and moves the account points
    the read at an account the writer controls, where that secret name can be
    made to exist and hold a value they chose.
    """
    from kiro_crew.cloud.microvm.record import CrewStore

    # ``ssm_target`` decides where the tunnel goes only on an ``ssm`` row. An
    # ``ssh`` row keeps whatever ``ssm_target`` it was given and dials its own
    # host, so a row that copied a real crew's node, profile and region onto an
    # SSH peer would otherwise match the record here while its tunnel reached a
    # different machine.
    if str(getattr(inst, "connection_method", "") or "") != "ssm":
        return None
    node_id = str(getattr(inst, "ssm_target", "") or "")
    if not node_id:
        return None
    for record in CrewStore().iter_records():
        if not record.mi_id or record.mi_id != node_id:
            continue
        if str(getattr(inst, "aws_profile", "") or "") != record.profile:
            return None
        if str(getattr(inst, "aws_region", "") or "") != record.region:
            return None
        return record
    return None


def _tunnel_binds_record(status: dict, record: Any) -> bool:
    """True when the LIVE tunnel was dialled on *record*'s own node and account.

    ``_record_for`` matches the registry row, and the row is agent-writable after
    the forward is built -- so matching it is not enough to prove the open tunnel
    reaches the crew the row now names. The forward dials
    ``validate_ssm_target(ssm_target)`` with ``aws_profile``/``aws_region`` and
    then does NOT rebuild when the row changes, so the tunnel's own frozen dial
    identity (carried on its status) is what actually decides where the port
    goes. Bind the credential to THAT, not to the row.

    Requires an ``ssm`` transport whose ``ssm_target`` is the record's ``mi_id``
    and whose ``aws_profile``/``aws_region`` are the record's. A row relabelled
    onto an already-open SSH peer, or onto an SSM peer dialled on another node or
    in another account, fails here even though ``_record_for`` matched it, so the
    secret is never read for it and never sent down that tunnel.
    """
    if str(status.get("transport") or "") != "ssm":
        return False
    if str(status.get("ssm_target") or "") != str(getattr(record, "mi_id", "") or ""):
        return False
    if str(status.get("aws_profile") or "") != str(getattr(record, "profile", "") or ""):
        return False
    if str(status.get("aws_region") or "") != str(getattr(record, "region", "") or ""):
        return False
    return True


def _forward_unchanged(before: dict, now: dict, record: Any, lane: str, target: str) -> bool:
    """True when *now* is still the forward the turn was resolved against.

    The handler awaits between resolving the target and posting to it (the
    secret read, the crew-name read, and on the streamed path the response
    prepare). If the tunnel drops in that window, another forward can take the
    freed local port, and a URL resolved earlier would then reach a different
    peer. So the post goes out only when the tunnel is still connected on the
    same port, from the same connection (``connected_at``), to the same target,
    and still dialled on the record's node and account.
    """
    if now.get("state") != "connected":
        return False
    if now.get("local_port") != before.get("local_port"):
        return False
    if now.get("connected_at") != before.get("connected_at"):
        return False
    if _turn_target(now, lane) != target:
        return False
    return _tunnel_binds_record(now, record)


def served_crew_name(inst: Any) -> str:
    """The crew name this instance's deployment SERVES, or ``""``.

    The value the turn must send as ``model``, and it is not the instance's
    display name. Both lanes set the container's ``SMC_CREW_NAME`` from the crew
    the operator named -- Fargate from its secrets' own binding, MicroVM from the
    bundle manifest the image was built from -- and the guest's front compares
    ``model`` against that name and answers 404 ``crew_not_served_here`` for
    anything else. The display name is a label: this lane's is the launch tag
    (``kc-22d27f``) and Fargate's is ``Kiro Crew Cloud (<tag>)``, so on both lanes
    the label addresses nobody.

    Resolved from the record the LAUNCH wrote, the same way
    :func:`control_secret_id` resolves the secret and for the same reason: the
    launch is the only party that held the crew's name, and nothing recoverable
    from an instance row stands in for it. Deliberately NO fallback to the tag --
    a tag that happens to equal the crew name would make this look like it works
    and leave every other crew answering 404.
    """
    provisioner = str(getattr(inst, "provisioner_id", "") or "")
    if provisioner != TURN_LANE:
        return ""
    try:
        record = _record_for(inst)
    except Exception:  # noqa: BLE001 - no lane, no record
        return ""
    if record is None:
        return ""
    return str(record.crew_name or "")


#: This module deliberately holds no tag-from-display-name parser.
#:
#: ``instances.json`` is not write-protected, so an instance's display name is
#: agent-supplied. Recovering a launch tag from ``Kiro Crew Cloud (<tag>)`` and
#: resolving a crew by it therefore lets a relabelled row name whichever crew's
#: secret its label claims, while the credential travels down that row's own
#: tunnel. ``_record_for`` matches on the node the row's tunnel dials, and a row
#: whose node owns no record is refused rather than having a secret name guessed
#: for it. A helper that turns a label back into a crew id is the shape of that
#: bug, so this module keeps none.

#: Longest message the hub will forward. The crew's own front caps the body it
#: accepts; this is the hub's cap, so an oversized prompt is refused here rather
#: than after a tunnel round trip.
_MAX_MESSAGE_CHARS = 32_000

#: A turn can legitimately run for minutes -- it is a model call that may use
#: tools. Only the CONNECT is short, so a dead tunnel fails fast instead of
#: holding the pane for the whole read budget.
_TURN_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=600)

#: The SEL audit operation every outcome of this route is recorded under. Same
#: name the shared gate already audits its denials with, so a reader sees one
#: operation per turn rather than two.
_AUDIT_OP = "crew_turn"

#: What the crew's front answers a non-streamed turn with, and a streamed one.
#: Checked rather than assumed: a reply in any other type is refused instead of
#: forwarded, so a crew that has been made to serve active content cannot have it
#: land on the authenticated hub origin. The same pair the peer proxy allows,
#: split per path here because this route knows which of the two it asked for.
#:
#: The gate covers the replies this route FORWARDS. A crew's 4xx/5xx is not
#: forwarded at all -- both paths answer their own ``crew_refused`` carrying a
#: bounded, redacted slice of it -- so the type of a refusal body is not checked
#: and not sent on.
_TURN_JSON_CONTENT_TYPE = "application/json"
_TURN_SSE_CONTENT_TYPE = "text/event-stream"

#: Longest slice of a crew's refusal body either path carries: the streamed
#: path's error frame and the non-streamed path's ``crew_refused`` body. The
#: body is redacted WHOLE before it is cut -- see ``_refusal_detail`` -- so this
#: bounds what is reported rather than the redactor's input.
_REFUSAL_DETAIL_CHARS = 400


def _refuse(code: str, detail: str, status: int = 400) -> web.Response:
    return web.json_response({"code": code, "detail": detail}, status=status)


def _content_type(resp: Any) -> str:
    """The upstream reply's media type, lowercased and without its parameters."""
    raw = str((getattr(resp, "headers", None) or {}).get("Content-Type") or "")
    return raw.split(";")[0].strip().lower()


async def _read_capped(resp: Any) -> Optional[bytes]:
    """The upstream body whole, or ``None`` when it runs past the redaction cap.

    ``resp.text()`` reads an unbounded body into this process, and the body here
    is produced by the far end: a crew that answers with gigabytes is a crew that
    exhausts the gateway's memory, on the sole event loop that also serves chat,
    websockets and the heartbeat.

    Buffered rather than streamed because the redactor has to see a WHOLE
    document -- a credential written as a JSON escape is only visible once the
    document is decoded -- but buffered only to the cap the peer proxy buffers
    to, and a reply past it is refused rather than held. The same fail-closed arm
    the streamed path already had for an oversized event.
    """
    raw = bytearray()
    async for chunk in resp.content.iter_any():
        raw += chunk
        if len(raw) > PROXY_REDACT_BUFFER_MAX_BYTES:
            return None
    return bytes(raw)


async def _redacted_body(raw: bytes) -> str:
    """*raw* with its credentials redacted, off the event loop.

    ``_redact_peer_payload`` walks and re-serializes a whole JSON document and
    runs the redaction patterns over every decoded string in it. On a megabyte
    reply that is CPU work, and on the loop it stalls every other connection the
    gateway is serving for the duration. ``to_thread`` is where the peer proxy
    does the same work, for the same reason.

    Raises :class:`ProxyReplyUnredactable` when the payload cannot be checked.
    """
    return await asyncio.to_thread(_redact_peer_payload, raw.decode("utf-8", "replace"))


async def _refusal_detail(resp: Any, instance_id: str) -> str:
    """A bounded, redacted slice of the crew's refusal body for the refusal.

    Capped, redacted, THEN cut, in that order, and every other order is a leak.
    Reading first without a cap hands the far end this process's memory; cutting
    before the redaction leaves a credential that straddles the cut as two halves
    the redactor does not recognise, and forwards the leading one; and cutting
    raw text loses the JSON decode that is what sees a credential written as a
    ``\\uXXXX`` escape.

    A body past the cap, or one the redactor refuses, becomes a sentence saying
    so rather than an empty detail, because this detail is all the pane gets of
    the refusal and "no detail" and "detail withheld" are different things to a
    reader.

    Used by both paths: the streamed path's ``crew_refused`` frame and the
    non-streamed path's ``crew_refused`` body.
    """
    raw = await _read_capped(resp)
    if raw is None:
        logger.info("crew refusal body from %s was too large to check", instance_id)
        return "(the crew's refusal was too large for this gateway to check)"
    try:
        clean = await _redacted_body(raw)
    except ProxyReplyUnredactable:
        logger.info("crew refusal body from %s was unredactable", instance_id)
        return "(the crew's refusal could not be checked for credentials)"
    return clean[:_REFUSAL_DETAIL_CHARS]


async def _read_control_secret(secret_id: str, profile: str, region: str) -> Optional[str]:
    """The crew's control secret, from the owner's own Secrets Manager.

    Through the gateway's one ``aws`` CLI chokepoint, so this read is subject to
    the same agent-session allowlist as every other AWS call the product makes:
    an agent driving the dashboard cannot turn this route into a way to print a
    secret, because the chokepoint refuses the pair.

    Returns ``None`` rather than raising when the secret is absent or unreadable.
    The caller turns that into one refusal, because the two cases are the same
    thing to a user -- this crew cannot be chatted with until its deploy is
    complete -- and distinguishing them in the reply would report whether a
    named secret exists.
    """
    from kiro_crew.cloud.aws import AWSError, checked_json

    def _read() -> Optional[str]:
        try:
            data = checked_json(
                [
                    "secretsmanager",
                    "get-secret-value",
                    "--secret-id",
                    secret_id,
                ],
                profile,
                region,
                action="secretsmanager:GetSecretValue",
                timeout=30,
            )
        except (AWSError, Exception) as exc:  # noqa: BLE001 - one refusal either way
            # The VALUE is never logged. This records the crew's NAME and the
            # exception's TYPE, which is what a reader needs to tell a secret that
            # does not exist from a Secrets Manager that could not be reached. The
            # message text names the thing that could not be read, which is not
            # the same as printing it.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.info("crew control secret %s unreadable: %s", secret_id, type(exc).__name__)
            return None
        value = data.get("SecretString") if isinstance(data, dict) else None
        return str(value) if value else None

    return await asyncio.to_thread(_read)


#: The provisioners whose crews are HEADLESS: they serve a turn route and have
#: no dashboard to embed. The set, not the connection method, is what decides
#: whether this pane applies -- two lanes already share the ``ssm`` method with
#: the EC2 lane, whose crews DO run a full gateway, so keying on the method would
#: offer a chat pane for a crew that has a dashboard and vice versa.
#: Re-exported from the registry, which OWNS this set. Two copies would drift,
#: and the two readers decide different things from the same answer: the tunnel
#: manager whether to mint a dashboard token, this route whether a chat pane may
#: be offered. A crew that is headless for one and not the other is a crew whose
#: pane is offered and whose forward holds no credential.
HEADLESS_CREW_PROVISIONERS = _REGISTRY_HEADLESS

# The registry holds the ids as LITERALS, because importing a lane's module from
# there would close a cycle. Checked against the real constants here instead, at
# import, so a renamed provisioner id is an immediate failure rather than a
# headless crew the registry fails to recognise.
assert HEADLESS_CREW_PROVISIONERS == {FARGATE_PROVISIONER_ID, MICROVM_PROVISIONER_ID}, (
    "the registry's headless set and the lanes' own provisioner ids disagree: "
    f"{sorted(HEADLESS_CREW_PROVISIONERS)} vs "
    f"{sorted({FARGATE_PROVISIONER_ID, MICROVM_PROVISIONER_ID})}"
)


def _turn_target(status: dict, provisioner_id: str = "") -> str:
    """The loopback turn URL for a connected headless crew, or ``""``.

    Two sources, in order. The tunnel manager populates ``turn_url`` for the
    ``fargate`` method, and that value is preferred because it is the far end's
    own statement about itself. Otherwise the URL is composed from the connected
    local port, but ONLY when the instance's provisioner is a headless-crew lane
    -- never from a port alone. A composed URL would also "work" against a
    forward pointing at a remote gateway, and posting a crew turn at a gateway's
    own port is a request that fails in a way nobody can read.
    """
    if status.get("state") != "connected":
        return ""
    url = str(status.get("turn_url") or "")
    if url:
        return url
    port = status.get("local_port")
    if isinstance(port, int) and port > 0 and provisioner_id in HEADLESS_CREW_PROVISIONERS:
        return f"http://127.0.0.1:{port}{FARGATE_TURN_PATH}"
    return ""


async def api_crew_turn(request: web.Request) -> web.StreamResponse:
    """POST /api/instances/{id}/crew-turn — one turn against a headless crew.

    Body: ``{"thread": "<id>", "message": "<text>", "stream": <bool>}``.

    ``thread`` is the crew's slot id, which is what makes a conversation a
    conversation: the crew's front serializes per slot and restores that slot's
    transcript before the turn, so the same thread id continues rather than
    starts. It is the caller's to choose and is forwarded as given, and it lives
    exactly as long as the VM that holds the transcript.

    Streams by default, as Server-Sent Events relayed from the crew's own
    OpenAI-shaped chunks. The relay is deliberately thin -- it re-frames nothing
    and interprets nothing -- so a caller reads what the crew said.
    """
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
    from kiro_crew.dashboard.handlers_instances import _guard, _registry, _status_for

    denied = _guard(request, "crew_turn")
    if denied is not None:
        return denied
    # Owner-only, the same bar as the proxy and the capability reads: this runs
    # with the owner's AWS credentials and sends the crew's control secret, so an
    # authenticated non-owner (a Slack-minted dashboard subject) must not reach it.
    if not is_owner_dashboard_request(request):
        _audit(_AUDIT_OP, "denied", error="non-owner identity rejected")
        return _owner_denial_response(request, "remote-crew chat is owner-only")

    state: "DashboardState" = request.app["state"]
    instance_id = request.match_info.get("id", "")
    reg = _registry(state)
    if reg is None:
        return _refuse("instances_unavailable", "remote crews are not available", 503)
    # OFF the loop. ``registry.get`` reads and parses the instances file, and
    # this handler runs on the gateway's sole event loop -- the one serving chat,
    # websockets, timers and the heartbeat. A synchronous read here stalls all of
    # them for the duration, and this route is hit on every turn the pane sends.
    inst = await asyncio.to_thread(reg.get, instance_id)
    if inst is None:
        return _refuse("instance_unknown", "no such remote crew", 404)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _refuse("bad_request", "request body must be JSON")
    if not isinstance(body, dict):
        return _refuse("bad_request", "request body must be a JSON object")
    message = body.get("message")
    thread = body.get("thread")
    if not isinstance(message, str) or not message.strip():
        return _refuse("bad_request", "message must be a non-empty string")
    if len(message) > _MAX_MESSAGE_CHARS:
        return _refuse("message_too_large", f"message exceeds {_MAX_MESSAGE_CHARS} characters", 413)
    if not isinstance(thread, str) or not thread.strip():
        return _refuse("bad_request", "thread must be a non-empty string")
    stream = body.get("stream", True)
    if not isinstance(stream, bool):
        return _refuse("bad_request", '"stream" must be a boolean')

    status = _status_for(state, instance_id)
    lane = str(getattr(inst, "provisioner_id", "") or "")
    if lane != TURN_LANE:
        # Declined BEFORE any secret is read or any address resolved, so a lane
        # this route does not serve reaches none of that machinery. See
        # ``TURN_LANE`` for why Fargate is not served; the crew is reachable at
        # its own turn URL, which the Instances card shows.
        _audit(_AUDIT_OP, "declined", request_id=instance_id, error=f"lane not served: {lane}")
        return _refuse(
            "crew_lane_not_served",
            "this gateway does not send turns to a crew on this lane yet. Chat with "
            "it through its own turn URL, which its card shows.",
            409,
        )

    target = _turn_target(status, lane)
    if not target:
        # Not connected, or connected to something that is not a headless crew.
        # One refusal for both, with the state named, because the pane's next
        # action is the same either way: connect the crew first.
        _audit(_AUDIT_OP, "declined", request_id=instance_id, error="not connected")
        return _refuse(
            "crew_not_connected",
            f"this crew is not connected as a headless crew (state: {status.get('state')})",
            409,
        )

    # Bind the credential to the LIVE tunnel, not to the registry row. ``inst``
    # is agent-writable and ``_record_for`` matches whatever it now claims, but
    # the open forward was dialled before any such edit and does not rebuild on
    # one. If the row was relabelled onto a peer the tunnel does not actually
    # reach -- an SSH peer, or an SSM peer on another node or account -- the
    # match points at one crew while the port reaches another, and sending that
    # crew's control secret here would hand it to the peer on the far end. Refuse
    # when the live tunnel's own dial identity does not equal the matched
    # record's node and account, BEFORE the secret is read.
    record = await asyncio.to_thread(_record_for, inst)
    if record is None or not _tunnel_binds_record(status, record):
        _audit(
            _AUDIT_OP,
            "denied",
            request_id=instance_id,
            error="tunnel identity does not match the crew record",
        )
        return _refuse(
            "crew_tunnel_mismatch",
            "this crew's open connection does not match its recorded node and "
            "account, so the gateway will not send its control secret down it. "
            "Reconnect the crew.",
            409,
        )

    secret_id = await asyncio.to_thread(control_secret_id, inst)
    if not secret_id:
        _audit(_AUDIT_OP, "failure", request_id=instance_id, error="secret reference unrecorded")
        return _refuse(
            "crew_secret_unavailable",
            "this crew's control secret reference is not recorded, so the gateway "
            "cannot authenticate to it. Relaunch the crew, or chat with it through "
            "its own turn URL.",
            503,
        )
    secret = await _read_control_secret(
        secret_id, str(inst.aws_profile or ""), str(inst.aws_region or "")
    )
    if not secret:
        _audit(_AUDIT_OP, "failure", request_id=instance_id, error="secret unreadable")
        return _refuse(
            "crew_secret_unavailable",
            "this crew's control secret could not be read, so the gateway cannot "
            "authenticate to it. Check the deploy completed and that this machine's "
            "AWS credentials can read it.",
            503,
        )

    crew_name = await asyncio.to_thread(served_crew_name, inst)
    if not crew_name:
        _audit(_AUDIT_OP, "failure", request_id=instance_id, error="crew name unrecorded")
        return _refuse(
            "crew_name_unavailable",
            "this crew's name is not recorded, so the gateway cannot address a turn "
            "to it. Relaunch the crew, or chat with it through its own turn URL.",
            503,
        )

    payload = {
        # The crew this deployment SERVES, which the guest's front compares against
        # its own ``SMC_CREW_NAME`` and 404s on a mismatch. Not the instance's
        # display name: that is the launch tag on this lane and a bracketed label
        # on Fargate, and neither addresses the crew.
        "model": crew_name,
        "id": thread,
        "stream": stream,
        "messages": [{"role": "user", "content": message}],
    }
    headers = {CONTROL_SECRET_HEADER: secret, "Content-Type": "application/json"}

    if not stream:
        if not _forward_unchanged(status, _status_for(state, instance_id), record, lane, target):
            _audit(_AUDIT_OP, "denied", request_id=instance_id, error="forward changed")
            return _refuse(
                "crew_tunnel_changed",
                "this crew's connection changed while the turn was being prepared, so "
                "the gateway did not send it. Try again.",
                409,
            )
        try:
            async with aiohttp.ClientSession(timeout=_TURN_TIMEOUT) as session:
                async with session.post(
                    target, json=payload, headers=headers, allow_redirects=False
                ) as resp:
                    # Redirects are not followed (``allow_redirects=False``
                    # above): following one would send the control-secret header
                    # and the conversation to whatever origin the crew named,
                    # outside the tunnel. A 3xx is refused here like an error and
                    # answered as a 502, since the gateway forwards no redirect.
                    if resp.status >= 300:
                        # The crew refused, so the refusal is reported as one
                        # rather than being read as an answer. Reported before
                        # the content-type gate, which applies to a reply this
                        # route would otherwise FORWARD: a refusal body is the
                        # crew's own choice of type, is never forwarded, and
                        # reaches the pane only as the bounded ``detail`` below.
                        # The crew's status is passed through, and the body is
                        # capped, redacted, then cut, in that order -- the same
                        # slice and the same order as the streamed path's
                        # ``crew_refused`` frame.
                        detail = await _refusal_detail(resp, instance_id)
                        _audit(
                            _AUDIT_OP,
                            "declined",
                            request_id=instance_id,
                            error=f"crew refused with {resp.status}",
                        )
                        return web.json_response(
                            {
                                "code": "crew_refused",
                                "status": resp.status,
                                "detail": detail,
                            },
                            status=resp.status if resp.status >= 400 else 502,
                            headers={"X-Content-Type-Options": "nosniff"},
                        )
                    # What TYPE the crew answered with, before a byte of it is
                    # buffered. A reply this gateway would not forward is not
                    # worth reading, and refusing on the header costs nothing.
                    upstream_ct = _content_type(resp)
                    if upstream_ct != _TURN_JSON_CONTENT_TYPE:
                        _audit(
                            _AUDIT_OP,
                            "denied",
                            request_id=instance_id,
                            error=f"crew content type refused: {upstream_ct or '(none)'}",
                        )
                        return _refuse(
                            "crew_content_type_refused",
                            "the crew answered with a content type this gateway does not "
                            "forward",
                            502,
                        )
                    raw = await _read_capped(resp)
                    if raw is None:
                        _audit(_AUDIT_OP, "denied", request_id=instance_id, error="reply too large")
                        return _refuse(
                            "crew_reply_too_large",
                            "the crew's answer was too large for this gateway to check for "
                            "credentials, so it was not forwarded",
                            502,
                        )
                    # Redacted on the way out, like every other peer reply. What comes
                    # back is model output: it can quote a credential the crew's own
                    # environment holds, or one an injected page told it to repeat, and
                    # this response lands in the owner's browser and transcript.
                    try:
                        reply = await _redacted_body(raw)
                    except ProxyReplyUnredactable:
                        logger.info("crew turn reply from %s was unredactable", instance_id)
                        _audit(
                            _AUDIT_OP, "denied", request_id=instance_id, error="reply unredactable"
                        )
                        return _refuse(
                            "crew_reply_unredactable",
                            "the crew answered with something this gateway could not "
                            "check for credentials, so it was not forwarded",
                            502,
                        )
                    _audit(_AUDIT_OP, "success", request_id=instance_id)
                    return web.Response(
                        body=reply.encode(),
                        status=resp.status,
                        content_type="application/json",
                        headers={"X-Content-Type-Options": "nosniff"},
                    )
        except Exception as exc:  # noqa: BLE001
            logger.info("crew turn to %s failed: %s", instance_id, type(exc).__name__)
            _audit(_AUDIT_OP, "failure", request_id=instance_id, error=type(exc).__name__)
            return _refuse("crew_unreachable", "the crew did not answer", 502)

    # Streamed: relay the crew's SSE frames straight through. The hub does not
    # buffer the turn, so a long answer appears as it is produced rather than at
    # the end -- which is the whole reason the pane streams.
    out = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            # The pane reads this with fetch + a reader, not EventSource, but a
            # proxy in between must still be told not to buffer.
            "X-Accel-Buffering": "no",
        },
    )
    await out.prepare(request)
    if not _forward_unchanged(status, _status_for(state, instance_id), record, lane, target):
        _audit(_AUDIT_OP, "denied", request_id=instance_id, error="forward changed")
        await out.write(
            b"data: "
            + json.dumps({"error": {"code": "crew_tunnel_changed", "status": 409}}).encode()
            + b"\n\n"
        )
        await out.write(b"data: [DONE]\n\n")
        return out
    try:
        async with aiohttp.ClientSession(timeout=_TURN_TIMEOUT) as session:
            async with session.post(
                target, json=payload, headers=headers, allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    # A 3xx lands here too: redirects are not followed, so the
                    # secret never leaves the tunnel for the origin a crew names.
                    #
                    # The crew's own words, so redacted like any other reply: a
                    # refusal body can quote the request it refused.
                    #
                    # Redacted WHOLE and only then cut, never the other way
                    # round. A credential that straddles the cut is two halves
                    # the redactor does not recognise, so cutting first
                    # forwards the leading half of it -- and a slice of raw text
                    # also loses the JSON decode that is what sees a credential
                    # written as a ``\uXXXX`` escape.
                    detail = await _refusal_detail(resp, instance_id)
                    _audit(
                        _AUDIT_OP,
                        "declined",
                        request_id=instance_id,
                        error=f"crew refused with {resp.status}",
                    )
                    await out.write(
                        b"data: "
                        + json.dumps(
                            {
                                "error": {
                                    "code": "crew_refused",
                                    "status": resp.status,
                                    "detail": detail,
                                }
                            }
                        ).encode()
                        + b"\n\n"
                    )
                    await out.write(b"data: [DONE]\n\n")
                    return out
                # And what TYPE the 200 is. Without this a JSON body with no
                # ``data:`` framing is one event to the reassembly below, which
                # sends it down ``_redact_sse_event``'s plain-text chain -- the
                # chain that cannot see a credential written as a JSON escape.
                upstream_ct = _content_type(resp)
                if upstream_ct != _TURN_SSE_CONTENT_TYPE:
                    _audit(
                        _AUDIT_OP,
                        "denied",
                        request_id=instance_id,
                        error=f"crew content type refused: {upstream_ct or '(none)'}",
                    )
                    # The head went out as 200 before the crew answered, so this
                    # refusal has to be a FRAME. Nothing of the body is read.
                    await out.write(
                        b"data: "
                        + json.dumps({"error": {"code": "crew_content_type_refused"}}).encode()
                        + b"\n\n"
                    )
                    await out.write(b"data: [DONE]\n\n")
                    return out
                # Redacted event by event, not chunk by chunk. ``iter_any`` splits
                # wherever the socket did, so a credential can straddle two chunks
                # and a chunk can hold half an event -- redacting chunks would both
                # miss the split secret and corrupt the frame. So complete events
                # are assembled first, exactly as ``api_instances_proxy`` does it,
                # including its two fail-closed arms: an event too large to redact
                # and one the redactor refuses both end the stream rather than
                # forwarding anything unchecked.
                pending = b""
                first = True
                async for chunk in resp.content.iter_any():
                    if not chunk:
                        continue
                    pending += chunk
                    if first:
                        # The stream's BOM, before any field is read. The browser
                        # skips it per the SSE spec, so a ``data:`` line behind one
                        # is a data line to the caller -- but not to the line match
                        # in ``_redact_sse_event``, which would send it down the
                        # plain-text chain and miss a credential written as a JSON
                        # escape. Three bytes, so a BOM the socket split waits for
                        # its last one rather than being half-tested and given up on.
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
                        try:
                            clean = await _redact_sse_event_async(event)
                        except ProxyReplyUnredactable:
                            logger.info("crew stream from %s was unredactable", instance_id)
                            _audit(
                                _AUDIT_OP,
                                "partial",
                                request_id=instance_id,
                                error="event unredactable",
                            )
                            return out
                        await out.write(clean + b"\n\n")
                    if len(pending) > PROXY_REDACT_BUFFER_MAX_BYTES:
                        logger.info("crew stream from %s had an oversized event", instance_id)
                        _audit(
                            _AUDIT_OP, "partial", request_id=instance_id, error="event too large"
                        )
                        return out
                if pending:
                    tail = pending.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    try:
                        await out.write(await _redact_sse_event_async(tail))
                    except ProxyReplyUnredactable:
                        logger.info("crew stream tail from %s was unredactable", instance_id)
                        _audit(
                            _AUDIT_OP,
                            "partial",
                            request_id=instance_id,
                            error="event unredactable",
                        )
                        return out
                # The turn completed and every frame of it was checked. Audited
                # like the peer proxy's own success, so the trail holds the turns
                # that WORKED and not only the ones that were refused -- a reader
                # asking which crew this gateway sent a control secret to, and
                # when, has no other record of it.
                _audit(_AUDIT_OP, "success", request_id=instance_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("crew turn stream to %s failed: %s", instance_id, type(exc).__name__)
        _audit(_AUDIT_OP, "failure", request_id=instance_id, error=type(exc).__name__)
        # The stream is already open, so the error has to be a FRAME: a status
        # code cannot be sent any more, and closing silently would leave the pane
        # waiting for a reply that is never coming.
        try:
            await out.write(
                b"data: " + json.dumps({"error": {"code": "crew_unreachable"}}).encode() + b"\n\n"
            )
            await out.write(b"data: [DONE]\n\n")
        except Exception:  # noqa: BLE001
            pass
    return out
