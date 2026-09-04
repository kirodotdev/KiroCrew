"""Connections handlers for browser-to-gateway OAuth callback recovery."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from aiohttp import web

from kiro_crew.connections import get_provider
from kiro_crew.connections.ownership import remove_provider_entry
from kiro_crew.connections.registry import Provider
from kiro_crew.dashboard.handlers.mcp import _is_valid_mcp_name
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_RETURN_ADDRESS_BYTES = 8192
_MAX_REQUEST_TARGET_BYTES = 6144
# RFC 3986 scheme followed by "://". Deliberately requires the "//": a bare
# "host:port/..." (which urlsplit would misread as scheme + opaque path) must
# NOT count as having a scheme, so it gets the http:// default (#7406).
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SERVER_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ALLOWED_CALLBACK_QUERY_KEYS = {
    "authuser",
    "code",
    "error",
    "error_description",
    "iss",
    "prompt",
    "scope",
    "state",
}


@dataclass(frozen=True)
class _LoopbackCallback:
    """A validated callback reduced to the fields needed for a fixed-host GET."""

    port: int
    request_target: str
    ipv6: bool = False


def _validated_loopback_return_address(value: object) -> _LoopbackCallback | None:
    """Parse a browser return address into a constrained loopback callback.

    The user controls only an unprivileged loopback port and an ASCII HTTP
    request-target containing a single OAuth code.  The network host is selected
    later from fixed literals, so request data can never choose a remote host.

    A paste with no scheme is normalized to ``http://`` first (#7406): mobile
    browsers — iOS Safari in particular — copy address-bar URLs without the
    scheme, so the documented paste-back flow otherwise fails on exactly the
    text the browser gave the user. Prepending a scheme is safe here because
    every containment constraint below (loopback host literals, port floor,
    query allowlist) applies to the normalized value; a scheme cannot turn a
    non-loopback host into a loopback one. The regex also catches the
    ``urlsplit`` gotcha where ``localhost:8976/...`` parses ``localhost`` as
    the scheme rather than the host.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate.encode("utf-8")) > _MAX_RETURN_ADDRESS_BYTES:
        return None
    if not _URL_SCHEME_RE.match(candidate):
        candidate = f"http://{candidate}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or host not in {"127.0.0.1", "::1", "localhost"}
        or port is None
        or port < 1024
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None

    query = parse_qs(parsed.query, keep_blank_values=True)
    codes = query.get("code", [])
    contains_control = any(
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        for values in query.values()
        for value in values
    )
    if (
        len(codes) != 1
        or not codes[0]
        or not set(query).issubset(_ALLOWED_CALLBACK_QUERY_KEYS)
        or contains_control
    ):
        return None

    path = parsed.path or "/"
    request_target = path + (f"?{parsed.query}" if parsed.query else "")
    if (
        not request_target.isascii()
        or any(character in request_target for character in "\r\n ")
        or len(request_target.encode("ascii")) > _MAX_REQUEST_TARGET_BYTES
    ):
        return None
    return _LoopbackCallback(
        port=port,
        request_target=request_target,
        ipv6=host == "::1",
    )


class _NoListener(Exception):
    """Nothing is bound to the loopback port a return address names."""


async def _relay_loopback_callback(callback: _LoopbackCallback) -> int:
    """Send one GET to a fixed loopback host and return its HTTP status."""
    host = "::1" if callback.ipv6 else "127.0.0.1"
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, callback.port),
            timeout=3,
        )
    except ConnectionRefusedError as refused:
        # The kernel answered for this port and said nothing is bound to it. That
        # is the only signal proving the listener is ABSENT rather than merely
        # slow, saturated or unroutable, so it is the only one raised distinctly:
        # every other dial failure stays an ordinary delivery failure. Once the
        # connection is established the listener demonstrably exists, so nothing
        # after this point may reach here either.
        raise _NoListener(str(refused)) from refused
    try:
        host_header = f"[{host}]" if callback.ipv6 else host
        request = (
            f"GET {callback.request_target} HTTP/1.1\r\n"
            f"Host: {host_header}:{callback.port}\r\n"
            "Connection: close\r\n"
            "Accept: text/plain\r\n\r\n"
        ).encode("ascii")
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=3)
        status_line = await asyncio.wait_for(reader.readline(), timeout=5)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})[^\r\n]*\r?\n", status_line)
    if match is None:
        raise OSError("OAuth callback returned an invalid HTTP status line")
    return int(match.group(1))


def _bad_request(error: str, code: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=400)


def _bad_gateway(error: str, code: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=502)


def _approval_superseded(error: str, code: str) -> web.Response:
    return web.json_response({"error": error, "code": code}, status=409)


async def api_mcp_oauth_relay(request: web.Request) -> web.Response:
    """POST /api/mcp/oauth/relay — deliver a failed browser redirect locally."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "mcp_oauth_relay")
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        return _bad_request("invalid JSON", "invalid_json")
    if not isinstance(body, dict):
        return _bad_request("request body must be an object", "invalid_request_body")

    # The relay only DELIVERS an already-minted authorization code to the
    # loopback listener that minted it; it never mints one. That listener and its
    # PKCE verifier belong to a specific pending kiro-cli OAuth flow regardless of
    # whether the server is a curated Connections provider or a user-added /
    # self-hosted one (issue #4491, the #4008 population). So relay membership is
    # NOT gated on the Connections registry — every safety property here is
    # provider-independent: the return address must target the gateway's own
    # loopback listener (_validated_loopback_return_address), and a port nothing is
    # bound to is reported as a spent approval (_NoListener). The name is
    # validated with the SAME shape user-added servers pass at add time
    # (_is_valid_mcp_name: bounded length, safe charset, traversal rejected) so a
    # server the add path accepted — `myServer`, `@org/tools` — can also relay,
    # while staying a safe, bounded SEL audit label rather than
    # attacker-controlled log content. The registry-slug shape stays on the MINT
    # path only (_requested_provider). This is deliberately distinct from
    # generalising the MINT to uncurated URLs, which is parked decision #4286 and
    # untouched here.
    server = body.get("server")
    if not isinstance(server, str) or not _is_valid_mcp_name(server):
        return _bad_request("invalid server", "invalid_server")
    callback = _validated_loopback_return_address(body.get("redirect_url"))
    if callback is None:
        return _bad_request(
            "invalid loopback return address",
            "invalid_loopback_return_address",
        )

    try:
        callback_status = await _relay_loopback_callback(callback)
    except _NoListener:
        # Nothing is bound to that port. The listener and the PKCE verifier are
        # created by the process that minted the authorize URL and die with it, so
        # its absence proves the code can no longer be redeemed BY ANYONE -- a
        # fresh listener on the same port never saw the verifier. Answering with
        # the delivery-failure message below would blame the paste for an
        # approval that is simply spent.
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_oauth_callback_relay",
            outcome="denied",
            resources=server,
        )
        return _approval_superseded(
            "the approval this return address belongs to is no longer live",
            "approval_superseded",
        )
    except (asyncio.TimeoutError, OSError, ValueError):
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_oauth_callback_relay",
            outcome="failed",
            resources=server,
        )
        return _bad_gateway(
            "the local OAuth callback did not accept the return address",
            "oauth_callback_unreachable",
        )

    if callback_status >= 400:
        sel().log_api_access(
            caller="dashboard",
            operation="mcp_oauth_callback_relay",
            outcome="failed",
            resources=server,
        )
        return _bad_gateway(
            "the local OAuth callback rejected the return address",
            "oauth_callback_rejected",
        )

    sel().log_api_access(
        caller="dashboard",
        operation="mcp_oauth_callback_relay",
        outcome="completed",
        resources=server,
    )
    return web.json_response({"ok": True})


# ── On-demand approval-URL mint ──
#
# Connect asks for a URL instead of waiting for one. The engine lives in
# kiro_crew.connections.mint; these two handlers are its HTTP surface, and the
# GET is the card's authoritative feed for a card-initiated mint.

# Fire-and-forget mint tasks, held so the loop cannot collect one mid-flight.
_mint_tasks: set[asyncio.Task] = set()

# The same keepalive for the premint activation, kept separate so a page open cannot
# be mistaken for a card-initiated mint when either set is inspected.
_premint_tasks: set[asyncio.Task] = set()

#: SEL read-id for the grant observation the premint endpoint acts on. Distinct from
#: the mint engine's and the status module's ids so the trail says which surface
#: looked; registered in ``hooks._AUDIT_ONLY_READ_IDS``, which fail-closes on an
#: unregistered id and would record nothing.
_GRANT_PRESENCE_READ_ID = "connections_premint.oauth_grant_presence"


def _requested_provider(slug: str) -> Provider | None:
    """The registry provider ``slug`` names, or None."""
    if not slug or len(slug) > 64 or not _SERVER_SLUG_RE.match(slug):
        return None
    provider = get_provider(slug)
    if provider is None or not provider.get("mcp_url"):
        return None
    return provider


async def _mint_request(
    request: web.Request,
) -> tuple[dict, Provider] | web.Response:
    """The JSON body and its registry provider, or the error response to return.

    Registry membership is the bound on what a caller can make the gateway act on:
    a mint starts a kiro-cli process and a disconnect deletes stored grant
    artifacts, so the slug has to resolve to a provider we ship rather than to
    arbitrary caller-supplied text. Shared by every provider-scoped endpoint so
    that bound is enforced in one place.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a client error, not a fault
        return _bad_request("body must be JSON", "invalid_body")
    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object", "invalid_body")
    slug = str(body.get("slug") or "").strip().lower()
    provider = _requested_provider(slug)
    if provider is None:
        return _bad_request("unknown provider", "unknown_provider")
    return body, provider


async def api_connections_mint(request: web.Request) -> web.Response:
    """POST /api/connections/mint — start minting a provider's approval URL.

    Returns as soon as the mint is scheduled. The URL is not ready yet: the
    caller polls :func:`api_connections_mint_state` for it.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "connections_mint")
    if owner_denied is not None:
        return owner_denied
    parsed = await _mint_request(request)
    if isinstance(parsed, web.Response):
        return parsed
    _body, provider = parsed
    slug = str(provider["slug"])

    # Function-local by DESIGN, not for a cycle: this handlers package is imported
    # on the gateway boot path, and the mint engine drags in the ACP client, the
    # credential predicate and the PID registry -- the warm engine adds the ACP
    # runtime and the MCP inventory on top. Keeping both here is what stops a
    # gateway start paying for a subsystem most requests never touch, and
    # test_the_handlers_package_does_not_import_the_mint_engine (and its warm twin)
    # enforce it in a subprocess -- hoisting either to module scope turns them red.
    from kiro_crew.connections.mint import _dispose_mint, reserve_mint_row, start_oauth_mint
    from kiro_crew.connections.warm import adopt_shared_mint

    # ADOPTION FIRST, because the alternative is throwing the answer away. The premint
    # sweep may already hold this provider's approval URL, and ``reserve_mint_row``
    # below pops whatever row is at the slug -- so reserving first disposed the very
    # URL this click existed to serve and then paid a ~7.5s cold spawn to re-mint it.
    # A refusal (nothing warmed, a dead holder, another tab got there first) falls
    # through to that cold path, which stays correct and stays the only path for a
    # provider warming never covered.
    adopted = await adopt_shared_mint(slug, str(provider["mcp_url"]))
    if adopted is not None:
        # ONE event, outcome ``ok``: unlike the cold path below, this request both
        # starts and finishes here, so a ``started`` with no completion would leave the
        # audit trail showing a mint that never ended.
        await asyncio.to_thread(
            lambda: sel().log_api_access(
                caller="dashboard",
                operation="connections_mint",
                outcome="ok",
                resources=f"provider:{slug} reason=adopted_warm_mint",
            )
        )
        # ``waiting`` rather than ``minting``: the URL exists already. The card polls
        # the mint state either way, and that poll now finds it on the first read.
        return web.json_response({"ok": True, "slug": slug, "state": "waiting", "token": adopted})

    # Reserved BEFORE responding: the response names a row this tab polls
    # immediately, so the row has to be visible first. Allocating only a token here
    # would leave the previous (possibly terminal) row answering that poll, and the
    # card would read it as the verdict on this attempt.
    token, prior = await reserve_mint_row(slug)
    try:
        task = asyncio.create_task(start_oauth_mint(slug, str(provider["mcp_url"]), token, prior))
    except BaseException:
        # The flow owns the displaced row once it starts; if it never starts,
        # nothing else will ever release that row's process and spec.
        if prior is not None:
            await _dispose_mint(prior)
        raise
    _mint_tasks.add(task)
    task.add_done_callback(_mint_tasks.discard)

    # Off the loop: only the append is queued to SEL's writer thread. The FIRST
    # sel() of a process CONSTRUCTS the log -- trust-dir creation, key validation,
    # and on Windows the owner-only DACL on the key file -- and this handler runs
    # BEFORE the audit
    # middleware's own call (that one logs the response), so on a fresh gateway
    # whose first state-changing request is a Connect click it would land here and
    # stall every other request. Same reasoning as server._audit_denied.
    await asyncio.to_thread(
        lambda: sel().log_api_access(
            caller="dashboard",
            operation="connections_mint",
            outcome="started",
            resources=f"provider:{slug}",
        )
    )
    return web.json_response({"ok": True, "slug": slug, "state": "minting", "token": token})


async def api_connections_mint_state(request: web.Request) -> web.Response:
    """GET /api/connections/mint?slug=… — this provider's mint state and URL.

    ``idle`` means no mint exists for the provider, which is distinct from a mint
    that ran and produced nothing: the card treats it as "nothing pending" rather
    than as a failure.
    """
    slug = str(request.query.get("slug") or "").strip().lower()
    if _requested_provider(slug) is None:
        return _bad_request("unknown provider", "unknown_provider")

    # Function-local for the same reason as the POST above: the boot path must not
    # carry the mint engine, and the subprocess guard test enforces it.
    from kiro_crew.connections.mint import expire_dead_holder, pending_mint_for

    # Commit the dead-holder verdict before reporting it, so the row the abandon
    # fence sees matches the state this response hands the card.
    await expire_dead_holder(slug)
    view = pending_mint_for(slug)
    if view is None:
        return web.json_response({"slug": slug, "state": "idle"})
    payload: dict[str, object] = {"slug": slug, "state": view.get("state", "minting")}
    if view.get("token"):
        payload["token"] = view["token"]
    if view.get("oauth_url"):
        payload["oauth_url"] = view["oauth_url"]
    if view.get("reason"):
        payload["reason"] = view["reason"]
    return web.json_response(payload)


async def api_connections_status(request: web.Request) -> web.Response:
    """GET /api/connections/status — authorization verdict per visible provider.

    Reports whether kiro-cli holds a grant (``grantPresent``) and the persisted
    first-connect time (``connectedSince``) for each visible provider. It does
    NOT probe endpoint reachability -- that stays with ``/api/mcp`` -- and it
    never owns approval-URL minting, which remains the mint endpoints' job. It is
    the authorization axis the card is otherwise blind to: a provider authorized
    outside the dashboard, and one never authorized, both answer the reachability
    probe with the same 401, and only a grant presence check tells them apart.
    """
    # Function-local for the same reason as the mint handlers below: the gateway
    # imports this package at boot, and status collection reaches the mint engine
    # for grant presence -- test_the_handlers_package_does_not_import_the_mint_engine
    # keeps that engine off the boot path.
    from kiro_crew.connections.status import _STATUS_SCHEMA_VERSION, collect_connection_statuses
    from kiro_crew.connections.warm import expire_dead_mints

    # Withdraw shared rows whose minting process is gone BEFORE the statuses are
    # read, so a card cannot be served an approval URL nothing can redeem. Keyed on
    # the fact rather than a cause, which is what covers a process that went away by
    # a route no expiry path anticipated; cheap enough to run per request because
    # liveness is a returncode read, not I/O.
    await expire_dead_mints()
    statuses = await collect_connection_statuses()
    return web.json_response({"schema_version": _STATUS_SCHEMA_VERSION, "connections": statuses})


async def api_connections_test(request: web.Request) -> web.Response:
    """POST /api/connections/test — enumerate this provider through kiro-cli.

    The dedicated ACP session is promptless: kiro-cli authenticates the remote
    server, performs its MCP ``tools/list``, and reports the final agent-exposed
    tools through native structured commands. The endpoint never receives token
    material and never invokes a provider tool.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "connections_test")
    if owner_denied is not None:
        return owner_denied
    parsed = await _mint_request(request)
    if isinstance(parsed, web.Response):
        return parsed
    _body, provider = parsed

    # Function-local by design: the handlers package is imported at gateway
    # boot, while this path imports the ACP client and should be paid only when
    # the owner explicitly clicks Test.
    from kiro_crew.connections.tool_test import test_connection_tools

    return web.json_response(await test_connection_tools(provider))


async def api_connections_cancel(request: web.Request) -> web.Response:
    """POST /api/connections/cancel — dispose a provider's in-flight mint.

    Body: ``{"slug": "<provider>", "token"?: "<row token>"}``. Releases the mint
    process, its loopback listener and its ephemeral spec so a Connect the user
    abandoned does not hold them until the TTL expires. It deliberately does NOT
    remove the MCP config entry: the card owns that decision, because a cancelled
    NEW connect uninstalls the entry it just created while a cancelled reconnect
    keeps the working connection. Idempotent -- cancelling a provider with no
    live mint answers ``dropped=false``.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    owner_denied = await require_owner_dashboard_request(request, "connections_cancel")
    if owner_denied is not None:
        return owner_denied
    parsed = await _mint_request(request)
    if isinstance(parsed, web.Response):
        return parsed
    body, provider = parsed
    slug = str(provider["slug"])
    raw_token = body.get("token")
    # Only an ABSENT token (or JSON null, its wire spelling) means "cancel
    # whatever row is current". A token that is present but empty or non-string
    # is a malformed request, not a privilege: coercing it to None would let a
    # caller that failed to echo its row token dispose another tab's mint.
    if raw_token is not None and (not isinstance(raw_token, str) or not raw_token):
        return _bad_request("token must be a non-empty string when provided", "invalid_token")
    token = raw_token

    # Function-local, same boot-path reason as the mint handlers.
    from kiro_crew.connections.mint import cancel_mint

    dropped = await cancel_mint(slug, token)

    # Off the loop: the FIRST sel() of a process constructs the log (trust-dir
    # creation, key validation, on Windows the owner-only DACL). Same reasoning
    # as api_connections_mint above.
    await asyncio.to_thread(
        lambda: sel().log_api_access(
            caller="dashboard",
            operation="connections_cancel",
            outcome="ok",
            source="dashboard",
            resources=f"provider:{slug} dropped={dropped}",
        )
    )
    return web.json_response({"ok": True, "slug": slug, "dropped": dropped})


def _open_project_dirs(state: Any) -> tuple[Path, ...]:
    """Every project directory an open chat slot is bound to.

    The UNION over the slot registry, deliberately WIDER than
    :func:`_shared.active_project_dir`: that resolver fails closed to ``None``
    when two slots name different projects, which is right for a settings page
    that must write somewhere defensible and exactly wrong here, where ``None``
    would read as "no project agent specs exist" -- the reading that deletes a
    live grant. A census needs every directory that could hold a sharer, and its
    own answer is already the resolver's superset.

    Pure in-memory reads (``_ChatSlot.project``, with ``project_dir`` accepted for
    slot-like objects that expose that name), so this is safe on the event loop;
    the directory scans it feeds happen on a worker thread.

    RESIDUAL: a project with no open chat slot is not enumerated, so its specs
    stay invisible. Slot state is what the dashboard actually knows about the
    checkouts kiro-cli runs in; widening to ``recent_projects.json`` would scan
    directories the user may have moved on from.
    """
    from kiro_crew.dashboard.handlers._shared import _slot_project

    found: dict[str, Path] = {}
    for slot in list((getattr(state, "_slots", None) or {}).values()):
        project = _slot_project(slot)
        if project is not None:
            found.setdefault(str(project), project)
    return tuple(found.values())


async def api_connections_disconnect(request: web.Request) -> web.Response:
    """POST /api/connections/disconnect — undo a connection on this machine.

    Body: ``{"slug": "<registry provider>"}``. Three local things: any in-flight
    mint is torn down, then -- in ONE locked transaction -- the MCP entry is
    removed from the scopes that configure this provider and the runtime's stored
    grant artifacts are unlinked.

    Deleting the artifacts is the whole point of this endpoint. Removing the config
    entry alone left a usable refresh token on disk, so a later reconnect resumed
    the old grant silently instead of asking for consent -- while the card had
    already told the user this machine's connection was gone.

    What it deliberately does NOT do is revoke at the provider. Nothing here can;
    only the provider can. So the response never claims the upstream grant is dead,
    and the card keeps sending the user to the provider's revoke page as well.

    ``grantRemoved`` and ``grantSurviving`` are separate answers on purpose: the
    artifacts are a pair, and "the token went" is not the same fact as "the grant is
    gone". The caller is told which one happened instead of inferring it from a
    delete loop's own optimism, and the audit outcome is ``partial`` when anything
    survived. ``grantCensusIncomplete`` says WHY a grant was kept when no sharer is
    named: a source the ownership decision needed could not be read.
    """
    # Owner-only, BEFORE any parse or destructive act: this endpoint deletes
    # machine-global config and OAuth artifacts, the same server-side boundary
    # every mutating agents route enforces. Non-owner dashboard subjects are
    # real (presigned links), and the token middleware only authenticates.
    # Function-local import: same boot-path reason as the mint handlers below.
    from kiro_crew.dashboard.handlers.agents import _require_owner

    denied = await _require_owner(request, "connections.disconnect")
    if denied is not None:
        return denied

    parsed = await _mint_request(request)
    if isinstance(parsed, web.Response):
        return parsed
    _body, provider = parsed
    slug = str(provider["slug"])
    mcp_url = str(provider["mcp_url"])

    # Function-local, same boot-path reason as the mint handlers.
    from kiro_crew.connections.mint import cancel_mint
    from kiro_crew.mcp_grant import surviving_grant_artifacts

    # A pending mint for this provider is now moot, and leaving it live would let
    # a grant arrive moments after the user asked for the connection to be gone.
    await cancel_mint(slug, None)
    # Read from slot state BEFORE the transaction and handed in, so the census and
    # the purge judge one snapshot of which checkouts are open rather than two.
    scope = await remove_provider_entry(slug, mcp_url, _open_project_dirs(request.app.get("state")))
    removed = scope.grant_removed
    # Asked rather than inferred from ``removed``: a survivor is what decides
    # whether this Disconnect actually held. Read outside the lock on purpose --
    # it changes nothing, and an entry appearing now does not make a pair that is
    # already gone come back.
    surviving = []
    for grant_url in scope.attempted_urls:
        for label in await asyncio.to_thread(surviving_grant_artifacts, grant_url):
            if label not in surviving:
                surviving.append(label)

    # Off the loop: the FIRST sel() of a process constructs the log. Same
    # reasoning as api_connections_cancel above.
    await asyncio.to_thread(
        lambda: sel().log_api_access(
            caller="dashboard",
            operation="connections_disconnect",
            # No `or grant_shared_with` escape any more: only ATTEMPTED pairs are
            # re-stat'd, so a survivor is always a failed unlink rather than a
            # deliberate keep that had to be excused.
            outcome="partial" if surviving else "ok",
            source="dashboard",
            resources=(
                f"provider:{slug} artifacts_removed={len(removed)} "
                f"surviving={len(surviving)} entry_removed={scope.entry_removed} "
                f"grant_shared={len(scope.grant_shared_with)} "
                f"census_incomplete={scope.census_incomplete}"
            ),
        )
    )
    return web.json_response(
        {
            "ok": True,
            "grantRemoved": bool(removed),
            "grantSurviving": surviving,
            "entryRemoved": scope.entry_removed,
            "grantSharedWith": list(scope.grant_shared_with),
            "grantCensusIncomplete": scope.census_incomplete,
            "grantCensusUnreadable": list(scope.census_unreadable),
        }
    )


async def api_connections_premint(request: web.Request) -> web.Response:
    """POST /api/connections/premint — warm every mintable provider's URL in one activation.

    The page fires this once on mount, ahead of any click, so that a Connect
    serves a URL the warm table already holds instead of paying a cold spawn.
    Takes no body: what is mintable is a fact about the user's registry and grant
    state, never a caller's choice, and the bound on what may be spawned has to
    stay on this side of the wire.

    ``preminting`` names the providers warming was STARTED for, which is why the
    response can precede any of them holding a URL. Warming one provider costs
    seconds and the whole activation is a single shared process, so awaiting it
    would stall the page's first paint for the sake of a report the card already
    gets from its own mint feed. A slug reported here can still end up without a
    URL -- the activation snapshot is the engine's to compute -- so the card's
    verdict remains the mint state, not this list.
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    # Owner-gated for the same reason as the mint POST: warming spawns a kiro-cli
    # process, so the caller has to be the owner rather than merely authenticated.
    owner_denied = await require_owner_dashboard_request(request, "connections_premint")
    if owner_denied is not None:
        return owner_denied

    # Function-local by DESIGN, not for a cycle: the handlers package is imported on
    # the gateway boot path, and the warm engine imports the cold mint at module
    # scope then adds the ACP runtime and the MCP inventory on top -- the heaviest
    # half of Connections. test_the_handlers_package_does_not_import_the_warm_engine
    # enforces it in a subprocess; hoisting this to module scope turns that red.
    from kiro_crew.connections.warm import _audited_mintable_providers, warm_mint_all

    # Off the loop: the scan reads the user's MCP config and stats kiro-cli's OAuth
    # artifact directory, either of which can sit on a network mount where a stat is
    # unbounded. warm.py routes the same call through a thread for this reason and
    # pins it with a drift guard.
    candidates, audit_recorded = await asyncio.to_thread(_audited_mintable_providers)
    slugs = [str(provider["slug"]) for provider in candidates]
    if not slugs:
        # Nothing to warm: an activation with an empty claim set would spawn a
        # process, pay the fixed activation cost and hold nothing. Nothing was acted
        # on either, so the scan owes no audit -- see below.
        return web.json_response({"ok": True, "preminting": []})

    # The credential-store observation this endpoint ACTS on: the scan above stats
    # kiro-cli's OAuth artifacts per provider, and reaching this line means the answer
    # is about to spawn a warm activation. ONE event for the whole sweep, matching
    # ``connections.status``: a single scan pass yields N answers but exactly one act
    # decision, so per-candidate events would over-count one observation, and the
    # per-URL ``mcp_grant.grant_observed`` wrapper would additionally have to break the
    # scan's synchronous shape that warm.py pins with a drift guard.
    #
    # Off the loop because the entry point marks its events critical, which drains the
    # SEL queue synchronously -- the same reason the log_api_access calls here are
    # threaded. Best-effort, NOT fail-closed: the artifacts are stat-ed and never
    # opened, so no credential material crosses this boundary, and refusing to warm on
    # an SEL outage would make every Connect pay a cold spawn instead. An unaudited
    # boolean is the lesser failure, and it leaves a warning behind.
    if not audit_recorded:
        logger.warning(
            "grant-presence audit for the premint scan could not be recorded; "
            "proceeding unaudited"
        )

    # The candidates are PASSED rather than re-derived inside the engine, so the
    # claim set and this response come from one scan. Two independent scans can
    # disagree -- a consent completing between them drops a provider -- and the
    # response would then name a slug nothing ever claimed.
    task = asyncio.create_task(warm_mint_all(candidates))
    _premint_tasks.add(task)
    task.add_done_callback(_premint_tasks.discard)

    # Off the loop for the same reason as api_connections_mint: this handler can be
    # the first state-changing request a fresh gateway serves, and the FIRST sel()
    # of a process constructs the log.
    await asyncio.to_thread(
        lambda: sel().log_api_access(
            caller="dashboard",
            operation="connections_premint",
            outcome="started",
            resources=f"providers:{len(slugs)}",
        )
    )
    return web.json_response({"ok": True, "preminting": slugs})
