"""Probing a remote (HTTP) MCP server.

``_probe_remote`` SSRF-vets the configured url through
``link_unfurl.vet_unfurl_url`` and pins the connection to the vetted address, then
sends the ``initialize`` and ``tools/list`` handshake over HTTP with the server's
headers expanded, tells a server that needs authorization from one that failed,
and reads the bearer challenge and the runtime grant that decide which. The vet is
why a server url naming a link-local or cloud metadata address is refused before
the handshake instead of having the gateway POST to it and reflect the reply onto
the dashboard row; a public, loopback or private server is probed as before.

:mod:`kiro_crew.mcp_discovery` stays the import path and the patch surface for
every name here; that module's comment lists the owners.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping

import aiohttp

from kiro_crew.json_line import parse_json_object_line
from kiro_crew.mcp_discovery_probe_state import (
    MCP_CLIENT_PROTOCOL_VERSION,
    _cache_probe,
    _get_probe_timeout,
    _sanitize_probe_error,
    downgrade_protocol_version,
    negotiated_protocol_version,
)
from kiro_crew.mcp_discovery_server_info import McpServerInfo
from kiro_crew.mcp_gateway.secret_uri import remote_header_secret_ref_error
from kiro_crew.mcp_grant import grant_observed

logger = logging.getLogger("kiro_crew.mcp_discovery")


async def _read_jsonrpc_response(resp: aiohttp.ClientResponse) -> dict:
    """Parse a JSON-RPC response from either JSON or SSE content-type.

    MCP Streamable HTTP servers may respond with ``application/json`` (single
    object) or ``text/event-stream`` (SSE with ``data:`` lines containing JSON).
    """
    ct = resp.content_type or ""
    if "text/event-stream" in ct:
        body = await resp.text()
        last: dict = {}
        for line in body.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload:
                    parsed = parse_json_object_line(payload)
                    if parsed is not None and "id" in parsed:
                        last = parsed
        return last
    return await resp.json()


def _expand_header_placeholders(
    headers: Mapping[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Resolve ``${VAR}`` / ``${env:VAR}`` references in remote-probe header values.

    A static header whose value carries a runtime reference is a documented
    form (docs/reference/kiro-cli/mcp/configuration.md) that kiro-cli expands
    at session runtime. Sending the reference as literal text gets the server's
    correct rejection reported as a failing row — with advice to delete a
    header that works in every session.

    Delegates to the mcp_gateway rewriter's declared-env expander — same regex,
    same credential-filtered source view (``is_secret_env_key`` /
    ``is_credential_env_key`` / ``scrub_agent_denied_env`` names are misses),
    same "unresolved stays literal" kiro-cli parity — rather than duplicating
    the policy here: two copies of a credential filter is the drift
    ``is_secret_env_key``'s own docstring warns against. The import is
    function-local because ``mcp_gateway.preflight`` / ``evaluate`` import this
    module at module scope; keeping the rewriter off this module's import graph
    avoids handing every consumer that never probes the rewriter's imports.

    Returns ``(expanded_headers, resolved_values)``. The second element is
    every placeholder value that actually resolved, INDIVIDUALLY: a partially
    expanded value like ``${TOKEN}${MISSING}`` sends ``<resolved>${MISSING}``,
    so a scrub set keyed on whole sent values would miss a server echoing only
    the resolved fragment. Each resolved value therefore joins the
    ``redact_mcp_error`` scrub set on its own (see its ``extra_values``).
    """
    from kiro_crew.mcp_gateway.rewriter import (
        _ENV_VAR_PLACEHOLDER,
        _expand_env_placeholders,
        _placeholder_source_env,
    )

    source = _placeholder_source_env()
    expanded: dict[str, str] = {}
    resolved: list[str] = []
    for name, value in headers.items():
        if isinstance(value, str):
            for match in _ENV_VAR_PLACEHOLDER.finditer(value):
                # The same lookup _expand_env_placeholders performs against the
                # same source view; a miss stays literal and contributes no
                # scrub entry (the literal is not a secret).
                hit = source.get(match.group(1))
                if hit is not None:
                    resolved.append(hit)
            expanded[name] = _expand_env_placeholders(value, source=source)
        else:
            expanded[name] = value
    return expanded, resolved


def _header_reference_unresolved(value: object) -> bool:
    """True when a header value still carries a ``${VAR}`` reference.

    After :func:`_expand_header_placeholders` this means the variable was
    missing or credential-filtered — either way nothing was dereferenced, so
    the value is not a credential anything supplied.
    """
    from kiro_crew.mcp_gateway.rewriter import _ENV_VAR_PLACEHOLDER

    return isinstance(value, str) and _ENV_VAR_PLACEHOLDER.search(value) is not None


def _needs_authorization(
    status_code: int, resp_headers: Mapping[str, str], sent_headers: Mapping[str, str]
) -> bool:
    """True when a remote probe response means "authenticate", not "broken".

    The runtime completes OAuth and holds the token; the probe does not. So a
    tokenless probe of an OAuth server gets 401 (or 403 with a
    ``WWW-Authenticate`` challenge). Treat that as ``needs_auth``.

    A static ``Authorization`` header in the config is a different case: the
    caller supplied a credential and it was rejected, which is a real error.
    That premise requires a credential to actually have been supplied — an
    Authorization value still carrying an unresolved ``${VAR}`` reference
    (a missing or credential-filtered variable, sent as literal text) supplied
    nothing, so it does not suppress ``needs_auth``.
    """
    if any(
        k.lower() == "authorization" and not _header_reference_unresolved(v)
        for k, v in sent_headers.items()
    ):
        return False
    if status_code == 401:
        return True
    if status_code == 403 and any(k.lower() == "www-authenticate" for k in resp_headers):
        return True
    return False


# A challenge comes from an endpoint that has not authenticated anything yet, so
# every bound here is on untrusted input.
_MAX_CHALLENGE_LEN = 2048
_CHALLENGE_PARAM_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*"([^"]*)"')


def _is_bearer_challenge(header_value: str) -> bool:
    """Whether a ``WWW-Authenticate`` value is a recognisable OAuth challenge.

    A predicate rather than the parsed parts, because recognising the challenge is
    the whole job: the scope list and the metadata URL are the evidence that this
    IS one, and nothing downstream renders either. Deliberately not a general RFC
    9110 auth-param parser — a challenge naming another scheme, or one whose params
    are unquoted, reads as "not a challenge" and the caller falls back to the
    status code alone.

    ``resource_metadata`` counts only when it is https. The value is the server's
    own claim about itself (RFC 9728 §5.1), and an http or javascript URL arriving
    from an unauthenticated endpoint is not evidence of anything.

    Total by construction: a probe must never fail because of the shape of a
    header, so anything that is not a string is simply not a challenge.
    """
    if not isinstance(header_value, str) or not header_value:
        return False
    if len(header_value) > _MAX_CHALLENGE_LEN:
        return False
    challenge_parts = header_value.lstrip().split(None, 1)
    if len(challenge_parts) != 2 or challenge_parts[0].lower() != "bearer":
        return False
    params = {k.lower(): v for k, v in _CHALLENGE_PARAM_RE.findall(header_value)}
    if params.get("resource_metadata", "").lower().startswith("https://"):
        return True
    return bool(params.get("scope", "").split())


async def _runtime_grant_present(mcp_url: str, name: str) -> bool | None:
    """Whether the kiro-cli runtime holds an OAuth grant for ``mcp_url``.

    Three-valued on purpose. ``True``/``False`` are observations; **``None`` means
    the question could not be answered**, and the caller must not let that reach
    the payload as ``False``. The distinction is load-bearing: "no grant held" is
    what makes "Sign-in required" honest, so a cache home that cannot be read at
    all — a permission error, a broken mount — degrades to ``None``, and absence
    from the payload renders as the safe "Not verified" instead.

    ``None`` does NOT cover artifact-layout drift, and it is worth being exact
    about that. If kiro-cli re-keys the paths this mirrors, the stat succeeds
    against a path that simply is not there, so ``grant_presence`` returns
    ``False`` and an already-authorized server reads "Sign-in required". That row
    does NOT recover on its own, and a maintainer must not deprioritize the drift
    on the assumption that it does: a second sign-in mints artifacts under the
    NEW key while this keeps stat-ing the old one, so the row goes on asking for
    a sign-in until this mirror is corrected. The recorded-hash tests pin the
    mirror only against itself, so such a change would not fail in-repo either —
    catching it needs an observation of an artifact kiro-cli actually wrote.

    The three-valued answer comes from :func:`mcp_grant.grant_presence`, which the
    persisted connection view resolves through as well -- one spelling of "present,
    absent, or unknowable", so the two surfaces cannot disagree about the same
    artifacts, and neither can lose the middle answer.

    The probe holds no token of its own (Kiro Crew stores no credentials), so the
    runtime's own artifacts are the only evidence available, and they are stat-ed
    for presence, never read. ``mcp_grant`` is a leaf module for exactly that
    reason: the derivation is shared with the mint rather than copied, and it
    carries none of the agent or ACP graph, so this is an ordinary module-scope
    import with nothing deferred to request time.

    ``name`` is what gets logged, never ``mcp_url``: a user-added endpoint can
    carry a credential in its userinfo or query string, and this runs for any URL
    someone typed, not just a vetted one.
    """
    # ``audit_absence``: this caller reads once and renders the answer either way,
    # so an ABSENT grant is acted on just as much as a held one -- it is what turns
    # the row into "Sign-in required". The mint's polling watcher keeps the
    # default, where only the acted-on TRUE is recorded.
    present = await grant_observed(mcp_url, audit_absence=True)
    if present is None:
        # ``name``, never ``mcp_url``: a user-added endpoint can carry a credential
        # in its userinfo or query string, and this line lands in gateway.log.
        logger.debug("MCP probe [%s]: grant presence unreadable", name)
    return present


async def _probe_remote(
    server: McpServerInfo, *, protocol_version: str = MCP_CLIENT_PROTOCOL_VERSION
) -> McpServerInfo:
    """Probe a remote Streamable HTTP MCP server via POST."""
    server.status = "probing"
    server.probed_at = time.time()
    server.probe_mode = "handshake"
    # Each probe is the sole authority for its own authorization evidence, so it
    # starts from zero. ``list_servers`` rehydrates these from the NAME-keyed probe
    # cache before a re-probe, so a row whose url was edited would otherwise
    # inherit the previous endpoint's challenge and keep reporting "Sign-in
    # required" for a server that never asked for one.
    server.auth_challenge = False
    server.auth_grant_present = None
    refusal = remote_header_secret_ref_error(server.headers)
    if refusal:
        # Not contacted: the header would reach the server as the literal
        # reference, and the authorization error that comes back names neither
        # the header nor the cause.
        server.sent_headers, server.resolved_header_values = None, []
        server.status = "error"
        server.error = refusal
        _cache_probe(server)
        return server

    # SSRF vet before any connection. ``server.url`` comes from the configured
    # MCP entry, and a sandboxed process can append one to
    # ``~/.kiro/crew/mcp.json``; unvetted, a url naming the cloud metadata
    # endpoint would have the gateway POST this handshake there and reflect the
    # reply onto the dashboard row, past the IMDS floor that fences the agent's
    # own shell. Reuse ``link_unfurl``'s vet -- the one the link preview,
    # calendar, WeCom and OAuth-discovery fetches already share -- and dial only
    # the address it approved, so a DNS answer cannot change under the probe.
    # Two opt-ins fit it to an operator-configured MCP endpoint:
    # ``allow_any_port`` (servers run on :8443 / :8000 / :3000) and
    # ``allow_private`` (an app backend on 127.0.0.1 or a LAN dev server is a
    # normal target, one kiro-cli itself connects to). So a link-local or metadata
    # address is refused, and a public, loopback or private one is probed.
    # Imported function-locally for the same reason ``_expand_header_placeholders``
    # defers its import: to keep ``link_unfurl`` off the import graph of the many
    # consumers that never probe a remote server.
    from kiro_crew.link_unfurl import UnfurlRejected, pinned_connector, vet_unfurl_url

    try:
        probe_timeout = _get_probe_timeout()
        # Blocking getaddrinfo, so off the event loop -- the offload the
        # link-preview and OAuth-discovery fetches use for the same vet -- and
        # inside the probe timeout, as aiohttp's own lookup was: a stalled
        # resolver reports ``timeout`` instead of holding the probe's slot. The
        # vetted result carries the approved addresses the connector is pinned to
        # below, so there is no second lookup for a rebind to poison.
        vetted = await asyncio.wait_for(
            asyncio.to_thread(vet_unfurl_url, server.url, allow_any_port=True, allow_private=True),
            timeout=probe_timeout,
        )
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "kirocrew-probe", "version": "1.0.0"},
            },
        }
        # Resolve ${VAR}/${env:VAR} references in header VALUES so the probe
        # presents the same credential the session runtime presents. Stored on
        # the row because redaction must key on the values that actually left
        # the process (see McpServerInfo.redaction_headers).
        server.sent_headers, server.resolved_header_values = _expand_header_placeholders(
            server.headers
        )
        hdrs = {
            **server.sent_headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        timeout = aiohttp.ClientTimeout(total=probe_timeout)
        # Pin the connection to the vetted address(es): the Host header and TLS
        # SNI still come from ``server.url`` (a public server's cert keeps
        # verifying), but the socket can only reach what the vet approved.
        async with aiohttp.ClientSession(
            timeout=timeout, connector=pinned_connector(vetted)
        ) as session:
            # Redirects are not followed, on every POST below. aiohttp would
            # follow a 307/308 with the body, and a Location naming an IP literal
            # is dialed without asking the pinned resolver, so a vetted public
            # host could hand the handshake to an unvetted address. The L1 smoke
            # probe (``connections/l1_smoke.py``) refuses redirects for the same
            # handshake; a redirect here reports as its HTTP status.
            async with session.post(
                server.url, json=init_body, headers=hdrs, allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    # The CHALLENGE is recorded on both outcomes. A rejected
                    # static credential is still an OAuth server, and that is
                    # precisely the case where the user most needs to be told the
                    # token they pasted is the wrong kind of credential.
                    server.auth_challenge = _is_bearer_challenge(
                        resp.headers.get("WWW-Authenticate", "")
                    )
                    if _needs_authorization(resp.status, resp.headers, server.sent_headers):
                        # A remote OAuth server answers a tokenless probe with
                        # 401 (or 403 + WWW-Authenticate). That is the expected
                        # reply, not a fault: the kiro-cli runtime holds the
                        # OAuth token and calls the server fine. The probe never
                        # sees that token (Kiro Crew keeps no credentials), so
                        # report "needs_auth" instead of a misleading error.
                        server.status = "needs_auth"
                        server.error = ""
                        # The GRANT, unlike the challenge, is looked up only on
                        # this branch. Its sole reader gates on ``needs_auth``, so
                        # on an error row the stat would run -- and
                        # ``grant_observed`` could write a critical SEL event --
                        # for an observation nothing reads, against that helper's
                        # own rule that the access owing a trail is the one a
                        # caller ACTS on.
                        if server.auth_challenge:
                            server.auth_grant_present = await _runtime_grant_present(
                                server.url, server.name
                            )
                    else:
                        server.status = "error"
                        server.error = f"HTTP {resp.status}"
                    _cache_probe(server)
                    return server
                # A stateful Streamable HTTP server issues a session id on
                # initialize and requires it on every later request — without
                # it, tools/list gets a 4xx/error and a HEALTHY server renders
                # errored. Absent header = stateless server; nothing to carry.
                mcp_session_id = resp.headers.get("Mcp-Session-Id", "")
                data = await _read_jsonrpc_response(resp)
                if fallback := downgrade_protocol_version(data, protocol_version):
                    return await _probe_remote(server, protocol_version=fallback)
                if data.get("error"):
                    server.status = "error"
                    err = data["error"]
                    server.error = (
                        err.get("message", "unknown error") if isinstance(err, dict) else str(err)
                    )
                    _cache_probe(server)
                    return server

            if mcp_session_id:
                hdrs = {**hdrs, "Mcp-Session-Id": mcp_session_id}
            negotiated = negotiated_protocol_version(data, protocol_version)
            hdrs = {**hdrs, "MCP-Protocol-Version": negotiated}
            # The spec's lifecycle requires notifications/initialized between
            # initialize and the first request; a conforming stateful server
            # may reject tools/list without it. Notifications get 202/204 and
            # no body — only a hard connection failure matters, and that
            # surfaces on the tools/list call right after.
            initialized_body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            try:
                async with session.post(
                    server.url, json=initialized_body, headers=hdrs, allow_redirects=False
                ):
                    pass
            except aiohttp.ClientError:
                logger.debug(
                    "MCP probe [%s]: initialized notification failed; proceeding to tools/list",
                    server.name,
                )

            list_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            async with session.post(
                server.url, json=list_body, headers=hdrs, allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    # An initialize that succeeds and a tools/list that does
                    # not is a server no session can get a tool out of — the
                    # badge certifies "tools usable", so this is a failed probe.
                    server.status = "error"
                    server.error = f"tools/list: HTTP {resp.status}"
                    _cache_probe(server)
                    return server
                data = await _read_jsonrpc_response(resp)
                if data.get("error"):
                    err = data["error"]
                    server.status = "error"
                    server.error = "tools/list: " + (
                        err.get("message", "unknown error") if isinstance(err, dict) else str(err)
                    )
                    _cache_probe(server)
                    return server
                result = data.get("result")
                tools_data = result.get("tools") if isinstance(result, dict) else None
                if not isinstance(tools_data, list):
                    # Same malformed-response rule as the stdio path: no tools
                    # LIST is a failed probe, not an empty server.
                    server.status = "error"
                    server.error = "tools/list: malformed response (no tools list)"
                    _cache_probe(server)
                    return server
                server.tools = [
                    name for t in tools_data if isinstance(t, dict) and (name := t.get("name", ""))
                ]

        server.status = "ok"
    except UnfurlRejected as exc:
        # Refused by the SSRF vet before contact. Logged by server NAME, never
        # the url: a user-typed url can carry a credential in its userinfo or
        # query string.
        server.sent_headers, server.resolved_header_values = None, []
        server.status = "error"
        # ``blocked_url`` also covers a host that does not resolve: the vet fails
        # closed on an address it could not check.
        server.error = (
            "URL is malformed or carries credentials"
            if exc.code == "invalid_url"
            else "URL host is a link-local or metadata address, or does not resolve"
        )
        logger.warning("MCP probe refused [%s]: %s", server.name, exc.code)
    except asyncio.TimeoutError:
        server.status = "error"
        server.error = "timeout"
        logger.warning("MCP probe failed [%s]: timeout", server.name)
    except Exception as exc:
        server.status = "error"
        server.error = _sanitize_probe_error(exc)
        logger.warning("MCP probe failed [%s]: %s", server.name, server.error)

    _cache_probe(server)
    return server
