"""The session MCP report and the session-init OAuth drain."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        _ChatSlot,
        _emit_mcp_oauth_request,
        _session_mcp_report,
        get_visible_providers,
        kirocrew_managed_names,
        logger,
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
        _emit_mcp_oauth_request(
            state,
            slot,
            server_name,
            req.get("oauthUrl") or "",
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
