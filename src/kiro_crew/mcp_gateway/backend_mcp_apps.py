"""MCP Apps on a shared ``Backend``.

The feature gate, the ``ui`` extension it advertises at ``initialize``, the model-visibility
filter and ``ui://`` declaration harvest on every ``tools/list`` a stub is answered, and
the out-of-band fetch, spool and delayed delivery of a ``tools/call`` result that names a
``ui://`` resource.

Each function here whose first parameter is ``self`` is a ``Backend`` method: the class
binds it by name, so it reads and writes the backend's own state on the instance, and a
patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_apps_declared_uris``, ``_apps_tasks``,
``_pending_requests``, ``_served_tool_surfaces``.

Instance fields read: ``pool_key``, ``stdin``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.mcp_gateway.apps import (
    WithheldTools,
    append_marker,
    extract_declared_ui_uris,
    extract_ui_resource_uri,
    strip_model_hidden_tools,
    write_spool,
)
from kiro_crew.mcp_gateway.backend_wire import (
    INTERNAL_STUB_PREFIXES,
    _PendingRequest,
    _write_json_line,
)
from kiro_crew.mcp_gateway.tool_surface import project_tool_surface
from kiro_crew.sel import SecurityEventLog

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.backend import Backend

#: The facade's logger: log capture and filters select this code's records by that name.
logger = logging.getLogger("kiro_crew.mcp_gateway.backend")


# MCP Apps (SEP-1865): extension key + MIME profile the gateway advertises to
# backends when the feature flag is on. kiro-cli sends empty client
# capabilities, so UI-enabled tools would otherwise degrade to text-only.
# The gateway is the actual Apps host (it fetches/renders the ui:// resource
# out-of-band), so it — not kiro-cli — advertises the capability.
MCP_APPS_EXTENSION_KEY = "io.modelcontextprotocol/ui"
MCP_APPS_MIME_TYPE = "text/html;profile=mcp-app"
MCP_APPS_ENV_FLAG = "KIROCREW_MCP_APPS"
#: Tokens the env flag recognises. Module constants rather than literals inline
#: in the gate, because the dashboard's write path has to recognise the SAME set
#: to refuse a config write the env would override — two copies would drift.
MCP_APPS_ENV_TRUE = ("1", "true", "yes")
MCP_APPS_ENV_FALSE = ("0", "false", "no", "off")

# Sentinel ``stub_uuid`` for a gateway-originated ``resources/read`` issued to
# fetch a ui:// app resource. Its response is routed to the parked future in
# :meth:`Backend._read_ui_resource` instead of any stub — mirrors the
# ``"__init__"`` sentinel used for the gateway-driven initialize handshake.
_APPS_STUB_SENTINEL = "__apps__"


# Deadline for the out-of-band ``resources/read`` round-trip. On timeout the
# original tools/call response is delivered unmodified — the app render is
# best-effort and MUST NOT wedge or drop the tool result.
_APPS_RESOURCE_READ_TIMEOUT_SECS = 10.0


def mcp_apps_env_override() -> bool | None:
    """The env flag's verdict, or ``None`` when it does not pin the feature.

    Public because the dashboard's write path needs the same answer: with the env
    pinning this, a config write is inert, and reporting success for an inert
    write is precisely the false-success the switch exists to prevent.

    Reads the CURRENT process's environment. The dashboard and gatewayd normally
    agree — ``env_target_resolver`` hands the backend a copy of the gateway's own
    env and this flag is not a credential, so it is inherited — but an operator
    who exported it into only one of the two would defeat the check. It is a
    best-effort guard against the common case, not a proof.
    """
    raw = os.environ.get(MCP_APPS_ENV_FLAG, "").strip().lower()
    if raw in MCP_APPS_ENV_FALSE:
        return False
    if raw in MCP_APPS_ENV_TRUE:
        return True
    return None


def _mcp_apps_enabled() -> bool:
    """Feature gate for MCP Apps. **Tightest-wins**: any explicit off disables.

    Capability follows the STUB, not a new preference. This function only ever
    runs inside a backend, and a backend only exists because a stub reached the
    broker for a server the operator stubbed, so the opt-in has already happened
    by the time control is here. That is why there is no *forward-facing* apps
    switch any more: a preference could not grant the feature (with no stub there
    is no render or callback path to grant).

    What survives is the two ways an operator can still say **no**:

    1. ``KIROCREW_MCP_APPS`` off -> disabled. Absolute kill switch, for tests,
       the e2e harness, and an operator who wants a stubbed server's backend
       shared without its server-authored UI.
    2. A stored ``mcp_gateway.apps_enabled = false`` -> disabled, EVEN with the
       env flag on. This key is retired going forward — nothing writes it, the
       MCP Management page does not surface it, and the docs do not teach it —
       but a released version honoured it as a trustworthy opt-out, so a config
       that already carries ``false`` keeps its opt-out. Dropping it here would
       silently start executing server-authored UI for the one operator who took
       the trouble to turn it off. It defaults True when absent, so this fires
       only on a value someone actually wrote: "not configured" is not an opt-out.

    The released gate had a third leg — it also required ``mcp_gateway.enabled``,
    because back then the broker existed only when sharing was on. That leg is
    deliberately gone, and it costs no released behaviour: under the current
    migration ``enabled: false`` resolves to an EMPTY stub set, so no stub, no
    backend, and this gate never runs for such an install.

    Fails CLOSED: if config cannot be read, the feature is disabled. An
    unreadable config in gatewayd is an abnormal state, and silently disabling an
    optional rendering feature is the low-harm outcome versus rendering against
    an operator preference we could not confirm.

    Read per-call (``KiroCrewConfig.load`` is fingerprint-cached, so this is a
    dict lookup in the common case) so the gateway reflects a config change
    without a daemon restart — including a daemon this gateway merely adopted and
    therefore cannot restart.
    """
    override = mcp_apps_env_override()
    if override is False:
        return False
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        gw = KiroCrewConfig.load().mcp_gateway
    except Exception:  # pragma: no cover - defensive; fail closed
        logger.debug("mcp-apps: config unreadable; treating feature as disabled", exc_info=True)
        return False
    return bool(gw.apps_enabled)


def _inject_client_extensions(msg: dict[str, Any]) -> dict[str, Any]:
    """Return ``msg`` with ``capabilities.extensions["io.modelcontextprotocol/ui"]``
    deep-merged into an ``initialize`` frame — or ``msg`` unchanged when the
    MCP Apps flag is off or the frame is not a well-formed initialize.

    Copy discipline mirrors :func:`_strip_caller_meta`: every dict on the
    mutated path (``params`` → ``capabilities`` → ``extensions``) is shallow-
    copied before mutation so the stub's captured frame is never aliased.
    Pre-existing extension entries are preserved; an existing ui-extension
    entry is left untouched (the caller declared it deliberately).
    """
    if not _mcp_apps_enabled():
        return msg
    params = msg.get("params")
    if not isinstance(params, dict):
        return msg
    out = dict(msg)
    params = dict(params)
    caps_raw = params.get("capabilities")
    caps = dict(caps_raw) if isinstance(caps_raw, dict) else {}
    ext_raw = caps.get("extensions")
    extensions = dict(ext_raw) if isinstance(ext_raw, dict) else {}
    if MCP_APPS_EXTENSION_KEY not in extensions:
        extensions[MCP_APPS_EXTENSION_KEY] = {"mimeTypes": [MCP_APPS_MIME_TYPE]}
    caps["extensions"] = extensions
    params["capabilities"] = caps
    out["params"] = params
    return out


async def _maybe_intercept_ui_result(
    self: Backend, pending: _PendingRequest, msg: dict[str, Any]
) -> bool:
    """Decide whether ``msg`` (a completed tools/call response) carries an
    MCP Apps ui:// resource that must be fetched + spooled before delivery.

    Returns ``True`` when interception was *initiated* — a background task
    now owns delivering the (marked) response to the stub, so the caller
    must NOT deliver it. Returns ``False`` (the common/off path) when the
    caller should deliver the response normally right now. Never raises:
    any classification hiccup falls back to normal delivery.

    The tools/list visibility filter runs even when the feature gate is
    OFF — see below for why — so this is not a pure no-op in that state.
    """
    # Gateway-internal requests must NEVER be re-intercepted:
    #
    # * an app-originated callback (the app-call relay forwards a tools/call
    #   on a ``__app_call__*`` stub) whose tool declares a ui:// resource
    #   would have its real result replaced by an internal marker string and
    #   mint a stray spool record. The render/spool path is only for
    #   MODEL-originated tool results; app callbacks return verbatim to the
    #   requesting app.
    # * a tool-surface probe (``__tool_surface__*``) asks for the server's
    #   OWN declaration so it can be compared with one already served. The
    #   filter would hide part of that declaration, and its SEL audit would
    #   record a withhold against a synthetic stub nobody asked for.
    #
    # Checked ahead of the feature gate because it also exempts a listing
    # from the visibility filter, which runs gate-independently.
    if pending.stub_uuid.startswith(INTERNAL_STUB_PREFIXES):
        return False
    if pending.method == "tools/list":
        result = msg.get("result")
        if isinstance(result, dict):
            # What THIS stub is being told its tools are. Projected from the
            # listing BEFORE the strip below mutates it, so this one call
            # site does not depend on the strip having run: the projection
            # applies the SAME model-visibility predicate itself, so it sees
            # the set the client ends up holding either way, and the
            # ordering cannot silently change what is recorded.
            #
            # An unprojectable listing CLEARS this stub's entry rather than
            # leaving the previous one: the client just received a listing
            # this host cannot read, so any earlier readable claim does not
            # describe what the session holds, and comparing against it
            # would be comparing against a superseded answer.
            #
            # A PARTIAL listing clears it for the same reason and is the
            # commoner case: ``tools/list`` is paginated, so one response can
            # be a page rather than the whole tool set — the request carrying
            # a ``cursor`` says this is a continuation, and a ``nextCursor``
            # in the result says more follows. Either way it cannot stand for
            # what the session holds, and recording it would compare a page
            # against the probe's first page and refuse a replacement that
            # never changed. A paginating server therefore keeps no anchor at
            # all, which is the pre-existing behaviour: no validation, and no
            # false refusal either.
            #
            # Not an authorization input and never served to anybody; see
            # :mod:`kiro_crew.mcp_gateway.tool_surface`.
            partial = pending.list_paginated or result.get("nextCursor") is not None
            projected = None if partial else project_tool_surface(result)
            if projected is None:
                self._served_tool_surfaces.pop(pending.stub_uuid, None)
            else:
                self._served_tool_surfaces[pending.stub_uuid] = projected
            # SEP-1865 MUST: a tool whose visibility omits "model" is not
            # the agent's to see. Mutates the response in place before the
            # caller delivers it. Reachable ONLY for model-facing listings —
            # the internal-stub guard above returns first, so an app's
            # authorization snapshot keeps its app-only tools and the
            # visibility gate in app_call still sees them.
            #
            # DELIBERATELY OUTSIDE the feature gate: visibility is the
            # SERVER's statement about who may call a tool, not a property
            # of our renderer. With apps disabled there is no app to call
            # an app-only tool, so filtering makes it unreachable — which is
            # the server's own consequence and strictly better than handing
            # the model a tool the server withheld from it.
            hidden = strip_model_hidden_tools(result)
            if hidden.declared:
                logger.info(
                    "mcp-apps: withheld %d app-only tool(s) from the agent's "
                    "listing for server=%s: %s",
                    len(hidden.declared),
                    self.pool_key.server_name,
                    ", ".join(hidden.declared),
                )
            if hidden.unreadable:
                # WARNING, not INFO: the server DID declare a visibility and
                # this host could not parse it, so a tool disappeared on our
                # judgement rather than the server's instruction. That is the
                # one drop an operator needs to see — it is the failure mode
                # where a real server's shape trips the parser.
                logger.warning(
                    "mcp-apps: withheld %d tool(s) from the agent's listing "
                    "for server=%s because their _meta.ui.visibility could "
                    "not be read: %s",
                    len(hidden.unreadable),
                    self.pool_key.server_name,
                    ", ".join(hidden.unreadable),
                )
            if hidden:
                self._audit_visibility_withhold(pending, hidden)
            # Passive harvest: tool declarations are the SEP-1865 PRIMARY
            # place a server associates a tool with its ui:// resource (the
            # real pdf-server and Excalidraw declare it ONLY here, not on
            # results). Every tools/list response updates the map.
            #
            # Runs AFTER the strip, so a withheld tool's ui:// never enters
            # the map. That ordering is load-bearing, not incidental: it
            # means a model-originated call naming an app-only tool cannot
            # find a declared resource to render. Keep the strip first.
            #
            # Also runs REGARDLESS of the feature gate, so the map always
            # reflects the server's CURRENT declarations. Gating it would
            # let a listing that arrives while apps are disabled leave a
            # WITHDRAWN tool→ui association cached, which a later re-enable
            # would then render from.
            try:
                declared = extract_declared_ui_uris(result)
            except Exception:  # pragma: no cover — defensive; extract is total
                declared = {}
            # REPLACE (never merge): each tools/list is the server's
            # complete current declaration set — merging would keep a
            # WITHDRAWN tool→ui association alive until backend restart,
            # so later successful calls would still render the withdrawn
            # app resource.
            self._apps_declared_uris = declared
        return False
    # Everything below is the RENDER path, which the feature gate owns.
    if not _mcp_apps_enabled():
        return False
    if pending.method != "tools/call":
        return False
    result = msg.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("isError"):
        # A FAILED tool call must never spawn an app render (nor mint a
        # live spool capability) — checked before EITHER association form
        # (result-side _meta.ui or the tools/list declaration) is read.
        return False
    try:
        resource_uri = extract_ui_resource_uri(result)
    except Exception:  # pragma: no cover — defensive; extract is total
        logger.debug("mcp-apps: extract_ui_resource_uri raised", exc_info=True)
        return False
    if resource_uri is None:
        # Fall back to the uri the tool DECLARED in tools/list (the
        # SEP-1865 primary form real servers use).
        resource_uri = self._apps_declared_uris.get(pending.tool_name)
    if resource_uri is None:
        return False
    task = asyncio.create_task(self._fetch_and_deliver_ui(pending, msg, resource_uri))
    self._apps_tasks.add(task)
    task.add_done_callback(self._apps_tasks.discard)
    return True


def _audit_visibility_withhold(
    self: Backend, pending: _PendingRequest, hidden: WithheldTools
) -> None:
    """SEL-audit a tools/list visibility withhold.

    Removing a tool from the agent's listing is an authorization decision
    this gateway makes, and the sibling direction (an app calling a tool,
    in :mod:`kiro_crew.mcp_gateway.app_call`) audits every outcome — so the
    direction that silently takes capability AWAY from the model must not
    be the unaudited one. Log lines rotate; the SEL chain is the durable
    record of what was hidden and why.

    ONE event per listing that actually withheld something, not one per
    tool and not one per tools/list — a server with a permanent app-only
    tool would otherwise mint an event on every listing forever.
    """
    try:
        SecurityEventLog().log_api_access(
            caller=pending.session_key or "unknown",
            operation="mcp-gateway.tools-list-visibility",
            outcome="denied",
            source="gateway",
            resources=(
                f"server={self.pool_key.server_name} "
                f"declared={','.join(hidden.declared) or '-'} "
                f"unreadable={','.join(hidden.unreadable) or '-'}"
            ),
        )
    except Exception:  # pragma: no cover — audit must never break delivery
        logger.debug("SEL audit for tools/list visibility withhold failed", exc_info=True)


async def _fetch_and_deliver_ui(
    self: Backend, pending: _PendingRequest, msg: dict[str, Any], resource_uri: str
) -> None:
    """Out-of-band fetch of a ui:// resource, spool it, mark the tools/call
    response, and deliver the (possibly-marked) response to the stub.

    On ANY failure/timeout the ORIGINAL response is delivered unmodified
    and a warning logged — the app render is best-effort and must never
    wedge or drop the tool result. Runs as a background task so the shared
    stdout pump is never blocked awaiting the resources/read round-trip.
    """
    response = dict(msg)
    response["id"] = pending.original_id
    try:
        contents = await self._read_ui_resource(resource_uri)
        html, csp, permissions = self._parse_ui_contents(contents)
        result = msg.get("result")
        structured = result.get("structuredContent") if isinstance(result, dict) else None
        content = result.get("content") if isinstance(result, dict) else None
        spool_id = await asyncio.to_thread(
            write_spool,
            {
                "server": self.pool_key.server_name,
                "tool": pending.tool_name,
                "session_key": pending.session_key,
                # GOVERNANCE IDENTITY of the callbacks this render may make, and
                # the reason it is stamped HERE: this is gatewayd, writing an
                # owner-only record no session can reach, and the value is the
                # agent the producing stub declared on its Register frame over
                # the uid socket. Both properties the app-call ceiling needs at
                # once — per call, so a backend shared by several agents
                # attributes each one correctly, and not writable by the agent
                # being governed. ``""`` when the stub declared none, which
                # ``app_call._agent_for_call`` refuses rather than defaults.
                "agent": self.agent_for_stub(pending.stub_uuid),
                # Exact-identity binding for the app→gateway callback: the
                # callback resolves its backend EXCLUSIVELY by this digest, so
                # an app can only ever call back into the same pool partition
                # (same server, command, env, work dir and OS user) that
                # produced it — never a co-pooled tenant's backend for the
                # same server.
                "pool_digest": self.storage_digest,
                "html": html,
                "csp": csp,
                "permissions": permissions,
                "structured_content": structured,
                # Originating tools/call inputs + full result content, so the
                # app initializes from its REAL state (SEP-1865 tool-input /
                # tool-result notifications) instead of empty placeholders.
                "tool_input": pending.tool_arguments,
                "result_content": content if isinstance(content, list) else None,
            },
        )
        if isinstance(result, dict):
            response["result"] = append_marker(result, spool_id)
        logger.info(
            "mcp-apps: spooled ui resource %s for tool=%s server=%s id=%s",
            resource_uri,
            pending.tool_name or "?",
            self.pool_key.server_name,
            spool_id,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; deliver original
        logger.warning(
            "mcp-apps: ui fetch/spool failed for %s (tool=%s); delivering "
            "original response unmodified: %s",
            resource_uri,
            pending.tool_name or "?",
            exc,
        )
        response = dict(msg)
        response["id"] = pending.original_id
    await self._deliver_to_stub(pending.stub_uuid, response)


async def _read_ui_resource(self: Backend, resource_uri: str) -> list[Any]:
    """Issue a gateway-originated ``resources/read`` for ``resource_uri`` to
    this backend and return its ``result.contents`` list.

    Parks a future under a ``_APPS_STUB_SENTINEL`` pending entry (same
    gateway-id mechanism the initialize handshake uses) so the stdout pump
    resolves it. Bounded by :data:`_APPS_RESOURCE_READ_TIMEOUT_SECS`.
    """
    fid = self._next_forward_id()
    fut: "asyncio.Future[dict[str, Any]]" = asyncio.get_running_loop().create_future()
    self._pending_requests[fid] = _PendingRequest(
        stub_uuid=_APPS_STUB_SENTINEL,
        original_id=None,
        method="resources/read",
        t_start_ms=time.monotonic() * 1000.0,
        apps_future=fut,
    )
    request = {
        "jsonrpc": "2.0",
        "id": fid,
        "method": "resources/read",
        "params": {"uri": resource_uri},
    }
    try:
        await _write_json_line(self.stdin, request)
        response = await asyncio.wait_for(fut, timeout=_APPS_RESOURCE_READ_TIMEOUT_SECS)
    finally:
        # Resolved path already popped it in _route_backend_line; this
        # covers the timeout/write-failure path so no stale entry lingers.
        self._pending_requests.pop(fid, None)
    if "error" in response:
        raise RuntimeError(f"resources/read error: {response['error']}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"resources/read malformed result: {response!r}")
    contents = result.get("contents")
    if not isinstance(contents, list) or not contents:
        raise RuntimeError("resources/read returned no contents")
    return contents


def _parse_ui_contents(self: Backend, contents: list[Any]) -> tuple[str, Any, Any]:
    """Extract ``(html, csp, permissions)`` from a resources/read
    ``contents`` list. Requires ``contents[0].mimeType`` to equal
    :data:`MCP_APPS_MIME_TYPE`; reads inline ``text`` or base64 ``blob``;
    pulls ``csp``/``permissions`` from ``contents[0]._meta.ui``."""
    first = contents[0]
    if not isinstance(first, dict):
        raise RuntimeError("resources/read contents[0] is not an object")
    mime = first.get("mimeType")
    if mime != MCP_APPS_MIME_TYPE:
        raise RuntimeError(f"unexpected mimeType {mime!r} (want {MCP_APPS_MIME_TYPE!r})")
    text = first.get("text")
    if isinstance(text, str):
        html = text
    elif isinstance(first.get("blob"), str):
        try:
            html = base64.b64decode(first["blob"], validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"invalid base64 blob: {exc}") from exc
    else:
        raise RuntimeError("resources/read contents[0] has neither text nor blob")
    meta = first.get("_meta")
    ui = meta.get("ui") if isinstance(meta, dict) else None
    if not isinstance(ui, dict):
        ui = {}
    return html, ui.get("csp"), ui.get("permissions")
