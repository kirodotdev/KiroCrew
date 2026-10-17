"""The session MCP report, the session-init OAuth drain, the MCP OAuth banner lifecycle and
the scrub of the ACP strings it shows."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        SessionMcpReport,
        _ChatSlot,
        _is_safe_oauth_url,
        _redact_meta_for_role,
        get_visible_providers,
        is_loopback,
        kirocrew_managed_names,
        logger,
        oauth_endpoints_path,
        oauth_url_contains_credential,
        redact_credentials,
        redact_exfiltration_urls,
        sanitized_oauth_endpoint,
        sanitized_oauth_endpoint_display,
    )


def _mcp_server_name_is_ambiguous(server_name: str, safe_name: str) -> bool:
    """Whether *safe_name* could stand for a DIFFERENT server than *server_name*.

    ``server_name`` is stored REDACTED, because it is ACP-controlled and reaches
    chat content and the WS broadcast. :func:`_redact_acp_string` maps EVERY
    credential-shaped name onto one sentinel (``[REDACTED: credential]``), so once
    redaction has fired the stored name cannot identify a server: two
    unrelated servers can share it.

    Redaction firing at all is the exact test. A name that came through untouched
    is stored verbatim and still identifies its server; a name that was rewritten
    has been collapsed toward a shared value, and nothing recoverable from the
    stored row can separate it from another name that collapsed the same way.

    Deliberately NOT solved by persisting a digest of the raw name. That would put
    an unsalted, cheap, offline-testable verifier for a credential onto the very
    two surfaces the redaction exists to keep it off -- trading a stale-link bug
    for a weaker secret, which is the worse end of the trade.
    """
    return safe_name != server_name


def _connections_managed_mcp_names() -> frozenset[str]:
    """Servers whose OAuth consent a rendered Connections card owns end to end.

    Membership is an ownership FACT, not a decision about what the user sees: it
    only tells the caller that a card surface drives this server's consent flow,
    so a request for it can be tagged ``card_owned`` and the render layer given
    something to act on. Nothing here suppresses anything.

    Two conditions, both required, each consumed from the facility that already
    decides it rather than re-derived here:

    * :func:`kirocrew_managed_names` -- our own MCP store wrote the entry. This is
      the single ownership discriminator, shared with the agent-spec emit path and
      the config-sync gate, so ownership means one thing everywhere.
    * :func:`get_visible_providers` -- the name is a Connections provider with a
      rendered card. Connect keys the store by provider slug and the card reads it
      back by slug, so the slug is the join between the two.

    Ownership ALONE is not enough. The dashboard's add-custom-server API writes to
    the same store, so a hand-added remote is every bit as "ours" while having no
    card anywhere. A provider whose launch gate is closed has no card either.
    Requiring a card keeps the annotation on servers that genuinely have a second
    surface, so the render layer never has to second-guess it.

    Registry slugs are slash-free, so ``mcp_server_alias`` is the identity on this
    set and kiro-cli's ``serverName`` is the slug verbatim -- no alias widening is
    needed. What remains open is an exact-slug collision: a server hand-added
    under a real slug is annotated, though it still renders on that provider's
    card, so a surface survives.

    Does blocking file I/O (store read + registry read) -- callers on the event
    loop must hand it to a worker thread.

    FAILS OPEN to the empty set on any error: nothing is annotated and every
    surface renders every banner, which is exactly today's behavior.
    """
    try:
        managed = kirocrew_managed_names()
        carded = {provider["slug"] for provider in get_visible_providers()}
    except Exception:
        logger.warning("Cannot resolve Connections-owned MCP names", exc_info=True)
        return frozenset()
    return frozenset(managed & carded)


async def _drain_session_init_oauth_requests(
    state: "DashboardState", slot: "_ChatSlot", client: Any
) -> None:
    """Surface the MCP OAuth requests kiro-cli buffered during session init.

    kiro-cli emits ``_kiro.dev/mcp/oauth_request`` while bringing MCP servers up;
    ``AcpClient`` collects them into ``pending_oauth_requests``. EVERY one is
    emitted as an ``mcp_oauth`` message, with no exceptions — that message is not
    just a banner, it is the state feed the Connections card reads its approval
    URL out of, so dropping one costs the user their only way to authorize.

    Requests for a server a Connections card owns are tagged ``card_owned`` (see
    :func:`_connections_managed_mcp_names`) purely so the render layer can decide
    whether chat needs to repeat a prompt the card already shows. That is a
    presentation question and it is answered where the flag that governs the card
    is known — not here.

    Async because resolving ownership reads files; the lookup runs in a worker
    thread and only when there is something to tag.
    """
    acp_client = getattr(client, "client", None)
    pop_pending = getattr(acp_client, "pop_pending_oauth_requests", None)
    if not callable(pop_pending):
        return
    pending = pop_pending() or []
    if not pending:
        # Resolve ownership only when there is something to tag — this runs on
        # every session init and the common case is zero requests.
        return
    managed = await asyncio.to_thread(_connections_managed_mcp_names)
    # The requests were buffered by the child `client` fronts, so that child's
    # process instance is the identity every one of these banners is stamped
    # with — the flow's loopback listener and verifier live in it.
    minted_by = str(getattr(client, "process_instance", "") or "")
    for req in pending:
        if not isinstance(req, dict):
            continue
        server_name = req.get("serverName") or ""
        # Raw (unredacted) name on purpose: store keys are raw and this is a
        # set-membership test, so an untrusted value can only miss. Redaction
        # happens inside _emit_mcp_oauth_request.
        oauth_url = req.get("oauthUrl") or ""
        await _approve_discovered_mcp_oauth_endpoint(server_name, oauth_url)
        _emit_mcp_oauth_request(
            state,
            slot,
            server_name,
            oauth_url,
            card_owned=bool(server_name) and server_name in managed,
            minted_by=minted_by,
        )


def _publish_session_mcp_report(state: "DashboardState", slot: "_ChatSlot", provider: Any) -> None:
    """Store this session's MCP report on the slot and push the delta.

    What the session's backend actually reported about its servers, which is a
    different fact from the agent spec on disk or the gateway's own probe. It is
    published so a reader can tell "configured" from "started here" instead of
    having to infer one from the other.

    Takes the PROVIDER, not its inner client: the shared runtime's provider has
    no ``.client``, so reaching through one dropped the report on that transport
    entirely.
    """
    report = _session_mcp_report(provider)
    if report is None:
        return
    # Stamp the payload with the session it describes. This copy outlives its
    # owner — the report itself lives on the transport and is inherently that
    # session's — so without the id a reader cannot tell a current answer from a
    # replaced session's, which is the leak every teardown patch chased.
    session_id = str(getattr(provider, "session_id", "") or "")
    payload = report.payload()
    if slot.set_mcp_report(payload, session_id):
        state.broadcast_ws(
            "mcp_report_update",
            {"slot": slot.key, "mcp_report": payload},
        )


def _record_session_mcp_event(
    state: "DashboardState",
    slot: "_ChatSlot",
    provider: Any,
    kind: str,
    server_name: str,
    error: str = "",
    *,
    fanout_no_owner: bool = False,
) -> None:
    """Fold a mid-turn MCP registration event into the slot's report.

    The init drain has already consumed the frames it saw, so a server that
    finishes init after an OAuth callback (or fails later) shows up only here.
    Without this the report would freeze at its init-time answer and keep
    showing a server as unreported after it came up.

    ``fanout_no_owner`` comes from ``AcpEvent.runtime_global``: the banner is
    still surfaced for an ownerless event, only the report mutation is gated —
    the same split the compaction path already makes.
    """
    report = _session_mcp_report(provider)
    if report is None or not report.record_event(
        kind, server_name, error, fanout_no_owner=fanout_no_owner
    ):
        return
    _publish_session_mcp_report(state, slot, provider)


def _redact_acp_string(s: str) -> str:
    """Scrub credentials + exfil URLs from an ACP-controlled string.

    server_name and error fields come from kiro-cli (and ultimately from the
    MCP server's own metadata).  Treat them as untrusted: they end up in chat
    content and in the live WS broadcast, both of which are external surfaces.
    """
    if not s:
        return s
    s, _ = redact_credentials(s)
    s, _ = redact_exfiltration_urls(s)
    return s


async def _approve_discovered_mcp_oauth_endpoint(server_name: str, oauth_url: str) -> None:
    """Let the banner gate accept a configured server's own consent endpoint.

    Awaited just before :func:`_emit_mcp_oauth_request`. A consent URL whose
    endpoint is outside the builtin and operator sets is otherwise refused for
    its PKCE entropy, which left every hand-added remote MCP server with its own
    authorization server unconnectable. ``mcp_oauth_discovery`` verifies the
    endpoint against the discovery metadata of the server URL the operator
    configured and, on an exact match, approves it process-wide, so this banner
    and every later render of it (``chat_utils._redact_meta_for_role``) pass the
    same gate. It does nothing (and makes no request) for a URL the gate already
    accepts or would refuse for any other reason, and it never raises.
    """
    if not server_name or not oauth_url:
        return
    from kiro_crew.mcp_oauth_discovery import approve_discovered_oauth_endpoint

    await approve_discovered_oauth_endpoint(server_name, oauth_url)


def _emit_mcp_oauth_request(
    state: "DashboardState",
    slot: "_ChatSlot",
    server_name: str,
    oauth_url: str,
    card_owned: bool = False,
    minted_by: str = "",
) -> None:
    """Append an mcp_oauth banner so the user can authorize an MCP server.

    If the URL is unsafe (non-http(s) scheme) or carries a credential / exfil
    pattern, surface a *rejected* banner explaining why instead of silently
    dropping.  Otherwise the user has no idea their MCP server failed to
    authenticate, and they can't escalate to whoever owns that server.

    ``minted_by`` is the process-instance identity of the ACP child asking for
    authorization — the process whose loopback listener and PKCE verifier the
    URL is redeemable against. It is stamped into the banner meta so the
    read-time gate (`_expire_dead_child_oauth_meta`) can withdraw the link the
    moment that child ceases to be the live one serving the slot, HOWEVER it
    ended: gateway restart, session reset, idle sweep, RSS recycle, or the
    child exiting on its own. An empty value stamps nothing, and an unstamped
    banner is judged dead on first read — the fail direction that removes a
    dead button rather than preserving it.

    ``card_owned`` records that some other surface owns this request's consent
    flow end to end — see :func:`_connections_managed_mcp_names`. It is an
    annotation, not a filter: the message is appended either way, because it is
    also the data feed that surface reads its approval URL out of. Only the
    render layer may act on it. Left off, the meta key is absent and the message
    is byte-identical to an unannotated one.

    Deliberately annotates the authorize banner only. A rejected URL is a
    security notice rather than a consent prompt — no card can act on it — so it
    stays unconditionally visible wherever banners render.
    """
    safe_name = _redact_acp_string(server_name)
    ambiguous = _mcp_server_name_is_ambiguous(server_name, safe_name)
    label = safe_name or "MCP server"

    if not _is_safe_oauth_url(oauth_url):
        logger.warning("ACP: refusing unsafe MCP OAuth URL for %s", server_name or "(unknown)")
        slot.append(
            "mcp_oauth",
            f"🚫 {label} sent an unsafe authentication URL (scheme rejected).",
            "msg msg-warn",
            meta={
                "server_name": safe_name,
                "failed": True,
                "rejected_url": True,
                "error": "unsafe URL scheme",
            },
        )
        return
    if oauth_url_contains_credential(oauth_url):
        # Two distinct causes reach this branch and the user cannot tell them
        # apart from the banner alone:
        #   1. A genuinely bogus URL — legitimate OAuth consent URLs carry
        #      state/code_challenge/client_id, never AKIA*/Bearer/etc.
        #   2. A legitimate consent URL at an endpoint outside
        #      ``_OAUTH_AUTHORIZATION_ENDPOINTS``. The PKCE entropy carve-out
        #      applies only at an approved (host, path), so an unlisted
        #      self-hosted IdP has its ``code_challenge`` scanned as a bare
        #      secret and fails closed.
        # Case 2 has a remedy (the ``oauth_endpoints.json`` operator keystone,
        # see security._load_operator_oauth_endpoints) but it is agent-fenced
        # with no dashboard writer, so naming it here is the only way the user
        # learns it exists. Without this the failure reads as unfixable.
        # Only the server's name is logged; the rejected URL never is.
        logger.warning(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            "ACP: rejecting MCP OAuth URL with credential/exfil pattern for %s",
            server_name or "(unknown)",
        )
        # Name the endpoint so case 2 is actionable: without the host+path the
        # user cannot know what to write into oauth_endpoints.json. The helper
        # returns host and path ONLY (query/PKCE material is never echoed) and
        # self-redacts a credential-bearing path, so surfacing it does not
        # weaken the rejection.
        endpoint = sanitized_oauth_endpoint(oauth_url)
        rejected_meta: dict[str, Any] = {
            "server_name": safe_name,
            "failed": True,
            "rejected_url": True,
            "error": "URL contained credential or exfiltration pattern",
            "remedy": "oauth_endpoints.json",
        }
        if endpoint is not None and is_loopback(endpoint[0]):
            # An authorize URL on this machine. The oauth_endpoints.json remedy
            # cannot apply here: the file refuses loopback hosts, and the gate
            # never relaxes http or an explicit port. In practice this is the
            # MCP client's own OAuth fallback after a local, no-auth server
            # failed one connect, so the advice is a retry. The guard verdict
            # above is unchanged; only the advice differs. The failed banner
            # renders meta["error"], so the advice must ride there.
            rejected_host, rejected_path = endpoint
            rejected_meta.pop("remedy")
            loopback_advice = (
                f"sign-in URL on this machine ({rejected_host}{rejected_path}) was "
                "rejected. A local MCP server that needs no sign-in usually lands "
                "here when its first connection attempt failed and the MCP client "
                "fell back to OAuth. Check the server is running, then start a new "
                "chat to reconnect."
            )
            rejected_meta["error"] = loopback_advice[0].upper() + loopback_advice[1:]
            slot.append(
                "mcp_oauth",
                f"🚫 {label}: {loopback_advice}",
                "msg msg-warn",
                meta=rejected_meta,
            )
            return
        endpoint_detail = ""
        if endpoint is not None:
            rejected_host, rejected_path = endpoint
            # The dashboard's failed-banner renderer (McpOAuthBanner) displays
            # meta["error"], not the content string — the endpoint must ride in
            # the error field to actually reach the user's screen. The banner
            # content below additionally spells the oauth_endpoints.json entry
            # shape, so text + error together carry the whole remedy; no extra
            # meta keys are emitted because no surface reads them.
            rejected_meta["error"] = (
                "URL contained credential or exfiltration pattern "
                f"(endpoint: {rejected_host}{rejected_path})"
            )
            # The failed banner renders ONLY meta["error"], so the remedy has to
            # ride there too or it never reaches the screen. It is
            # added only when writing the entry would actually clear this
            # rejection: sanitized_oauth_endpoint_display re-runs the gate as if
            # the endpoint were approved and answers None for a fixed
            # credential, http, an explicit port, a loopback/IP host, a redacted
            # or capped component, or a shape the extension loader refuses.
            # Those keep the bare error rather than advertise a fix that fails.
            # Naming the remedy changes no verdict: the URL is still rejected
            # and the file stays operator-written, out-of-band.
            if sanitized_oauth_endpoint_display(oauth_url) is not None:
                entry = json.dumps({"host": rejected_host, "path": rejected_path})
                rejected_meta["error"] += (
                    ". If you trust this identity provider, add "
                    f'{entry} to "additional_authorization_endpoints" in '
                    f"{oauth_endpoints_path()} and retry"
                )
            endpoint_detail = (
                f" The rejected authorization endpoint was "
                f"{rejected_host}{rejected_path} (query values withheld)."
            )
        slot.append(
            "mcp_oauth",
            f"🚫 {label} sent an authentication URL containing a credential "
            f"pattern (rejected).{endpoint_detail} If this is a self-hosted "
            "or otherwise unlisted identity provider, its authorization "
            "endpoint may need adding to oauth_endpoints.json in the Kiro "
            'Crew data home, shaped {"additional_authorization_endpoints": '
            '[{"host": ..., "path": ...}]}; otherwise ask the server owner '
            "to fix the URL.",
            "msg msg-warn",
            meta=rejected_meta,
        )
        return
    # A new authorize request for this server means kiro-cli started a FRESH
    # flow, and the loopback listener plus the PKCE verifier live in that flow —
    # so every still-open banner for the same server now points at a callback
    # port that cannot redeem anything. Retire them before appending, or
    # the older button stays live-looking forever and sends the browser to a
    # dead port that answers with a bare `/?code=…` page.
    #
    # Deliberately NOT done on the two rejected-URL branches above: those append
    # a banner with no `oauth_url`, so superseding an older one there would take
    # away the only authorize affordance the user has and hand back nothing.
    _supersede_open_mcp_oauth_banners(state, slot, safe_name, ambiguous)
    content = f"🔐 {label} requires authentication."
    meta: dict[str, Any] = {
        "server_name": safe_name,
        "oauth_url": oauth_url,
        # The process instance that owns the listener and the PKCE verifier this
        # URL is redeemable against. Read back by `_expire_dead_child_oauth_meta`,
        # which withdraws the link once this stops being the live child serving
        # the slot — covering every way the flow can die (hard restart, session
        # reset, idle sweep, RSS recycle, self-exit) with one comparison, because
        # no code of ours has to run at the moment of death. Stamped ONLY on this
        # branch: the rejected-URL branches carry no `oauth_url`, so they have no
        # link whose liveness this could describe.
        "child": minted_by,
    }
    if card_owned:
        meta["card_owned"] = True
    slot.append(
        "mcp_oauth",
        content,
        "msg msg-info",
        meta=meta,
    )


def _supersede_open_mcp_oauth_banners(
    state: "DashboardState", slot: "_ChatSlot", safe_name: str, ambiguous: bool
) -> None:
    """Retire every still-open mcp_oauth banner for ``safe_name``.

    An authorize link is only redeemable while the kiro-cli flow that minted it
    is alive: that process owns the loopback listener the provider redirects to
    and the PKCE verifier the code is exchanged with. A newer request replaces
    both, so an older open banner is unredeemable by anyone — clicking it walks
    the user through a full provider login and lands them on a bare
    ``http://127.0.0.1:<dead-port>/?code=…`` page that looks like success and
    consumes nothing.

    ``oauth_url`` is POPPED rather than merely flagged around, so a client that
    does not know the ``superseded`` flag still cannot render the dead link —
    the render layers all gate on having a URL. This mirrors the vocabulary the
    relay already answers a dead callback port with (``approval_superseded``).

    Walks the whole history rather than stopping at the first match: banners
    accumulate one per re-announce (every session init re-emits pending
    requests), and leaving any of them open is the defect.

    Retires NOTHING when *ambiguous* -- see
    :func:`_mcp_server_name_is_ambiguous`. Once redaction has collapsed the
    stored name, a row bearing it may belong to a different server, and this
    function's whole effect is to POP a URL: acting on a guess would take away a
    link that is still live and still the user's only way in. That is strictly
    worse than the stale link this exists to withdraw, so the ambiguous case
    keeps the same posture the rejected-URL branches take -- leave the banner
    alone rather than remove an affordance we cannot prove is dead. The cost is a
    known limitation: a server whose NAME looks like a credential keeps its stale
    banner, exactly as it does on main today.
    """
    if ambiguous:
        return
    for message in slot.messages:
        if message.get("role") != "mcp_oauth":
            continue
        meta = message.get("meta") or {}
        if meta.get("server_name") != safe_name:
            continue
        if (
            meta.get("completed")
            or meta.get("failed")
            or meta.get("superseded")
            or meta.get("expired")
        ):
            continue
        # Redact the RESTORED payload before it is re-emitted, for the same
        # reason _mark_mcp_oauth_completed does: this copies a stored dict into
        # both slot.messages and a broadcast that bypasses _prepare_messages.
        # Safe here despite that gate being stricter than the emit-path one
        # (see the long note in _mark_mcp_oauth_completed) because `oauth_url`
        # is dead data on this path — it is dropped outright below and the
        # superseded branch renders no link.
        new_meta = _redact_meta_for_role("mcp_oauth", dict(meta))
        new_meta.pop("oauth_url", None)
        new_meta["superseded"] = True
        label = safe_name or "MCP server"
        new_content = f"↻ {label} sign-in is no longer active — a newer request replaced it."
        row_mid = new_meta.get("mid")
        # Resolve by `mid`, this row's server-minted identity, NOT by `ts`. That
        # column is not an identity: `_ChatSlot.append` preserves an explicitly
        # supplied `ts` verbatim for a row replayed from a channel transcript, and
        # a coarse OS clock stamps two same-tick rows identically. A ts lookup
        # resolves the FIRST match, so on a collision this loop would rewrite one
        # row once per duplicate and leave the later banner open, still offering
        # the dead link this function exists to withdraw.
        if (
            slot.update_message(
                message.get("ts", ""),
                content=new_content,
                meta=new_meta,
                mid=row_mid if isinstance(row_mid, str) else None,
            )
            is None
        ):
            continue
        # The wire payload differs from what is PERSISTED, in two deliberate ways.
        #
        # `mid` travels so the CLIENT can resolve the same row the same way -- its
        # patch reducer prefers it over `ts` for exactly the reason above.
        #
        # `oauth_url` is sent back as an empty string even though it is ABSENT from
        # the persisted meta, because the client MERGES an incoming meta over the
        # row's existing one rather than replacing it. Omitting the key would leave
        # the live URL in place on the client row -- harmless for a client that
        # knows `superseded`, but a tab still running pre-upgrade JS would keep
        # rendering the dead link. An empty string fails that client's own
        # isSafeOAuthUrl check, so the banner withdraws instead.
        state.broadcast_ws(
            "chat_message_update",
            {
                "slot": slot.key,
                "ts": message.get("ts", ""),
                "mid": row_mid or "",
                "meta": {**new_meta, "oauth_url": ""},
                "content": new_content,
            },
        )


def _session_mcp_report(provider: Any) -> "SessionMcpReport | None":
    """The provider's per-session MCP report, or None when it keeps none.

    Reached through the ``LLMProvider`` contract, NOT by probing an attribute.
    The probe this replaces asked the provider's inner ``.client`` — which the
    shared runtime's provider does not have, so it silently answered None there
    and the whole report went missing on that transport while looking fine on the
    dedicated one. A declared method with a safe default cannot fail that way.
    """
    if provider is None:
        return None
    try:
        report = provider.mcp_session_report()
    except Exception:  # pragma: no cover — a report is never worth a failed turn
        logger.debug("Failed to read the session MCP report", exc_info=True)
        return None
    return report if isinstance(report, SessionMcpReport) else None


def _mark_mcp_oauth_completed(
    state: "DashboardState", slot: "_ChatSlot", server_name: str, success: bool, error: str = ""
) -> None:
    """Patch the most recent open mcp_oauth banner for ``server_name`` to a terminal state."""
    safe_name = _redact_acp_string(server_name)
    target: dict | None = None
    for m in reversed(slot.messages):
        if m.get("role") != "mcp_oauth":
            continue
        meta = m.get("meta") or {}
        # Compare against the redacted form already stored on the banner.
        if meta.get("server_name") != safe_name:
            continue
        if (
            meta.get("completed")
            or meta.get("failed")
            or meta.get("superseded")
            or meta.get("expired")
        ):
            continue
        target = m
        break
    if target is None:
        return
    # Redact the RESTORED payload before it is re-emitted. This function copies the
    # whole stored dict into both slot.messages and the `chat_message_update`
    # broadcast below, and that broadcast bypasses _prepare_messages — a genuine
    # egress point.
    #
    # Scope of the exposure, stated precisely: the SAVE path already redacts meta
    # (`_build_message_entry`), and `ConversationLog.append` has no `meta` parameter
    # at all, so meta this version wrote to disk comes back already clean. What this
    # guards is history lines this version did not write — legacy lines, a tampered
    # session file, or the verbatim-preserved foreign byte ranges. That is the same
    # threat model the sibling gates are written against, so it is defence in depth
    # rather than a live hole.
    #
    # The matching loop above reads only control fields (`server_name`,
    # `completed`, `failed`), which is why this reader looked safe on a first pass.
    # What decides safety is not which fields a reader INSPECTS but whether it
    # re-emits the dict. This one does.
    #
    # CAREFUL — `_redact_meta_for_role` is STRICTER than the emit-path gate and does
    # NOT preserve realistic `oauth_url`s: it calls `redact_exfiltration_urls`,
    # whose query-length (>=200) and base64-blob heuristics blank a real Google OIDC
    # or GitHub PKCE consent URL. (Measured: those two are blanked; only a short URL
    # survives.) The emit-path gate `security.oauth_url_contains_credential`
    # deliberately exempts OAuth params from exactly those heuristics — it is
    # "the sole path allowed to exempt standard OAuth entropy from the generic
    # URL heuristics" (its docstring).
    #
    # That is harmless HERE only because `oauth_url` is dead data by this point:
    # every path through this function sets `completed` or `failed`, and
    # McpOAuthBanner.tsx returns on the `failed` (line 50) and `completed` (line 61)
    # branches BEFORE the link-rendering branch (line 73). Do NOT reuse this gate on
    # a path where the authorize link is still rendered — there it would break the
    # user's ability to authorize an MCP server.
    new_meta = _redact_meta_for_role("mcp_oauth", dict(target.get("meta") or {}))
    if success:
        new_meta["completed"] = True
        new_meta.pop("failed", None)
        new_meta.pop("error", None)
    else:
        new_meta["failed"] = True
        safe_err = _redact_acp_string(error)
        if safe_err:
            new_meta["error"] = safe_err
    label = safe_name or "MCP server"
    new_content = f"🔓 {label} authenticated." if success else f"🚫 {label} authentication failed."
    # Resolve and broadcast by `mid`, the row's server-minted identity, for the
    # same reason the supersede path does: two rows can carry one `ts`, and a ts
    # lookup resolves the first match, so a completion could land on the wrong
    # banner. `ts` stays in the payload and as the resolver's fallback for a legacy
    # row written before the id existed.
    target_mid = new_meta.get("mid")
    updated = slot.update_message(
        target.get("ts", ""),
        content=new_content,
        meta=new_meta,
        mid=target_mid if isinstance(target_mid, str) else None,
    )
    if updated is None:
        return
    state.broadcast_ws(
        "chat_message_update",
        {
            "slot": slot.key,
            "ts": target.get("ts", ""),
            "mid": target_mid or "",
            "meta": new_meta,
            "content": new_content,
        },
    )
