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

LANE-NEUTRAL BY CONSTRUCTION
----------------------------
Nothing here knows about MicroVMs. What it needs is a connected instance whose
far end is a headless crew front, which is true of the Fargate lane
(``connection_method="fargate"``, an ECS task target) and of the MicroVM lane
(``connection_method="ssm"``, an ``mi-`` node) alike. The tunnel's own status is
what says so: it carries a ``turn_url`` exactly when the far end is a turn route
rather than a dashboard.
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
    _redact_peer_payload,
    _redact_sse_event_async,
)
from kiro_crew.dashboard.peer_redaction import redact_peer_text
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

#: Where a crew's control secret lives, by crew name. The same path the lane's
#: own launch spec builds (``MicroVmLaunchSpec.control_secret_name``) and the same
#: one the Fargate task definition references, so the hub reads what the deploy
#: wrote instead of being told a path by the caller.
#: The shape a per-crew control secret's name has, as the lane writes it. Used
#: only to DERIVE an id when the lane recorded none; the recorded reference is
#: always preferred, because it is what the launch actually minted.
_SECRET_PATH = "{prefix}/{crew}/CONTROL_SECRET"


#: The lanes this route serves. The MicroVM lane only.
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
TURN_LANES = frozenset({MICROVM_PROVISIONER_ID})


def control_secret_id(inst: Any) -> str:
    """This crew's control secret, by NAME, or ``""``.

    Read from the record this instance IS -- found by ``_record_for`` on the id
    the launch registered -- and never from the instance's display name. The
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

    Matched on the instance's own identity -- the ``mi-`` node id the launch
    registered -- and not on its tag or display name, because ``instances.json``
    is not write-protected and a row is therefore agent-writable while the crew
    record beside it is not.

    That gap is the whole reason this function exists. A tag match binds the
    credential to a LABEL while the tunnel stays bound to the ROW, and the two
    can be made to disagree: relabel a connected hostile peer's row with a real
    crew's tag, lane and AWS coordinates, and the owner's next turn reads the
    REAL crew's control secret and sends it down the HOSTILE peer's already-open
    tunnel. Matching on the id closes it, because the id is what decides where
    that tunnel goes.

    The account and region are checked too, not for the tunnel but for the READ:
    the caller passes the ROW's ``aws_profile`` and ``aws_region`` to
    Secrets Manager, so a row that keeps the id and moves the account points the
    read at an account the writer controls, where that secret name can be made
    to exist and hold a value they chose.
    """
    from kiro_crew.cloud.microvm.record import CrewStore

    instance_id = str(getattr(inst, "id", "") or "")
    if not instance_id:
        return None
    for record in CrewStore().iter_records():
        if not record.mi_id or record.mi_id != instance_id:
            continue
        if str(getattr(inst, "aws_profile", "") or "") != record.profile:
            return None
        if str(getattr(inst, "aws_region", "") or "") != record.region:
            return None
        return record
    return None


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
    if provisioner not in TURN_LANES:
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
#: tunnel. ``_record_for`` matches on the instance's registered id, and a row
#: that owns no record is refused rather than having a secret name guessed for
#: it. A helper that turns a label back into a crew id is the shape of that bug,
#: so this module keeps none.

#: Longest message the hub will forward. The crew's own front caps the body it
#: accepts; this is the hub's cap, so an oversized prompt is refused here rather
#: than after a tunnel round trip.
_MAX_MESSAGE_CHARS = 32_000

#: A turn can legitimately run for minutes -- it is a model call that may use
#: tools. Only the CONNECT is short, so a dead tunnel fails fast instead of
#: holding the pane for the whole read budget.
_TURN_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=600)


def _refuse(code: str, detail: str, status: int = 400) -> web.Response:
    return web.json_response({"code": code, "detail": detail}, status=status)


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
    import asyncio

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
    if lane not in TURN_LANES:
        # Declined BEFORE any secret is read or any address resolved, so a lane
        # this route does not serve reaches none of that machinery. See
        # ``TURN_LANES`` for why Fargate is not in it; the crew is reachable at
        # its own turn URL, which the Instances card shows.
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
        return _refuse(
            "crew_not_connected",
            f"this crew is not connected as a headless crew (state: {status.get('state')})",
            409,
        )

    secret_id = await asyncio.to_thread(control_secret_id, inst)
    if not secret_id:
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
        return _refuse(
            "crew_secret_unavailable",
            "this crew's control secret could not be read, so the gateway cannot "
            "authenticate to it. Check the deploy completed and that this machine's "
            "AWS credentials can read it.",
            503,
        )

    crew_name = await asyncio.to_thread(served_crew_name, inst)
    if not crew_name:
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
        try:
            async with aiohttp.ClientSession(timeout=_TURN_TIMEOUT) as session:
                async with session.post(target, json=payload, headers=headers) as resp:
                    text = await resp.text()
                    # Redacted on the way out, like every other peer reply. What comes
                    # back is model output: it can quote a credential the crew's own
                    # environment holds, or one an injected page told it to repeat, and
                    # this response lands in the owner's browser and transcript.
                    try:
                        body = _redact_peer_payload(text)
                    except ProxyReplyUnredactable:
                        logger.info("crew turn reply from %s was unredactable", instance_id)
                        return _refuse(
                            "crew_reply_unredactable",
                            "the crew answered with something this gateway could not "
                            "check for credentials, so it was not forwarded",
                            502,
                        )
                    return web.Response(
                        body=body.encode(),
                        status=resp.status,
                        content_type="application/json",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.info("crew turn to %s failed: %s", instance_id, type(exc).__name__)
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
    try:
        async with aiohttp.ClientSession(timeout=_TURN_TIMEOUT) as session:
            async with session.post(target, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    # The crew's own words, so redacted like any other reply: a
                    # refusal body can quote the request it refused.
                    detail = redact_peer_text((await resp.text())[:400])
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
                            return out
                        await out.write(clean + b"\n\n")
                    if len(pending) > PROXY_REDACT_BUFFER_MAX_BYTES:
                        logger.info("crew stream from %s had an oversized event", instance_id)
                        return out
                if pending:
                    tail = pending.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    try:
                        await out.write(await _redact_sse_event_async(tail))
                    except ProxyReplyUnredactable:
                        logger.info("crew stream tail from %s was unredactable", instance_id)
                        return out
    except Exception as exc:  # noqa: BLE001
        logger.info("crew turn stream to %s failed: %s", instance_id, type(exc).__name__)
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


def is_headless_crew(status: Any, provisioner_id: str) -> bool:
    """Whether this instance is a headless crew the pane can chat with.

    Exported for the instances list, so the frontend decides between "embed its
    dashboard" and "open the chat pane" from ONE field rather than re-deriving it
    from the connection method -- which would be wrong the moment a third lane
    reuses an existing method, and is already wrong for the EC2 lane, whose crews
    share the ``ssm`` method and do run a full gateway.

    Answers for a DISCONNECTED instance too: the pane has to be offered before
    the crew is connected, or the user has no way to reach the thing that
    connects it. So the provisioner alone decides the KIND, and the status
    decides whether a turn can be sent right now.
    """
    if provisioner_id in HEADLESS_CREW_PROVISIONERS:
        return True
    return bool(isinstance(status, dict) and status.get("turn_url"))
