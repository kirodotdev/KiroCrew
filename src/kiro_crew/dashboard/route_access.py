"""The label an HTTP route of the gateway can carry: an action name and a tier.

A :class:`RouteAccess` says what a route does (for example ``widgets.reset``) and how long its
request and response shape is promised. :data:`ROUTES` records the labels, keyed by
method and path pattern.

The table is separate from route registration on purpose. One handler can serve two
routes, so a label on the handler would be ambiguous, and registration runs on the
gateway boot path, where a label must cost nothing.

No request reads this module: it decides nothing about who may call a route.
``test/test_route_access_labels.py`` holds the registered routes against it.
Rules for picking a name and a tier: ``docs/system-specs/modules/route-access.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ACTION_NAME_RE",
    "PREVIEW",
    "ROUTES",
    "STABLE",
    "TIERS",
    "UNSTABLE",
    "RouteAccess",
]

# Request and response shapes only gain fields.
STABLE: Final = "stable"
# The shape is nearly settled; small changes are possible.
PREVIEW: Final = "preview"
# No promise: the shape changes whenever the dashboard needs it to.
UNSTABLE: Final = "unstable"

TIERS: Final = frozenset({STABLE, PREVIEW, UNSTABLE})

# ``<area>.<verb>``: lower-case parts joined by dots, at least two of them.
ACTION_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")


@dataclass(frozen=True)
class RouteAccess:
    """One route's action name and stability tier."""

    action: str
    tier: str


# Keyed by (METHOD, path pattern). The pattern is the route's canonical path: the path
# as registered, with any regex dropped from a ``{name:regex}`` segment. A HEAD route
# registered beside a GET route of the same path takes the GET route's entry.
# Who may call a route is decided by its handler; the comment beside an entry only
# records it.
ROUTES: Final[dict[tuple[str, str], RouteAccess]] = {
    # Strict internal path: the ``app_store_search`` tool's read leg, called with the
    # internal secret; a loopback cookie also reaches it (apps/routes_registry.py).
    ("GET", "/api/app-store/search"): RouteAccess("app_store.search", UNSTABLE),
    # App token, owner dashboard or internal caller may read (apps/browser_sites_routes.py).
    ("GET", "/api/apps/{name}/browser-sites"): RouteAccess("apps.browser_sites.read", UNSTABLE),
    # Owner dashboard only: 403 for an app token and an internal caller.
    ("POST", "/api/apps/{name}/browser-sites"): RouteAccess("apps.browser_sites.change", UNSTABLE),
    # Owner dashboard only (apps/browser_recipes_routes.py); only narrows the approvals.
    ("POST", "/api/browser/recipe-watch-off"): RouteAccess(
        "apps.browser_recipes.watch_off", UNSTABLE
    ),
    # Internal secret plus an attested bot turn in its own thread
    # (handlers/member_memory_self.py); reads the bot's own memory, writes nothing.
    ("POST", "/api/member/self/memory/list"): RouteAccess("member.memory.list", UNSTABLE),
    # Memory owner gate (resolve_requested_memory_store in
    # handlers/memory_graph_layout_api.py). Each first refreshes the store's layout
    # file (StoreGraph.refresh: places new rows, and starts a background build when
    # the file is missing or stale), then answers from it.
    ("GET", "/api/memory/graph/overview"): RouteAccess("memory.graph.overview.refresh", UNSTABLE),
    ("GET", "/api/memory/graph/nodes"): RouteAccess("memory.graph.nodes.refresh", UNSTABLE),
    ("GET", "/api/memory/graph/search"): RouteAccess("memory.graph.search.refresh", UNSTABLE),
    ("GET", "/api/memory/graph/select"): RouteAccess("memory.graph.select.refresh", UNSTABLE),
    # Owner gate (handlers/connections_app_manifest.py); writes nothing.
    ("GET", "/api/connections/app-manifests"): RouteAccess(
        "connections.app_manifests.read", UNSTABLE
    ),
    # Owner gate (handlers_instances_gate._guard); writes nothing.
    ("GET", "/api/instances/bots"): RouteAccess("instances.bots.read", UNSTABLE),
    # Owner dashboard only (handlers/mcp_project_file.py); adds one server to a chat
    # folder's project MCP file, the Apply of an mcp.server card with target project.
    ("POST", "/api/mcp/project-file"): RouteAccess("mcp.project_file.change", UNSTABLE),
    # Owner dashboard only (chat_api/slot_effort.py); restarts the slot's idle session.
    ("POST", "/api/chat/slots/{slot}/approve-for-me"): RouteAccess(
        "chat.approve_for_me.change", UNSTABLE
    ),
    # Loopback or same-user socket plus X-Local-Secret (handlers/core.py); revokes one
    # link's credential family for `kirocrew logout --link`.
    ("POST", "/api/logout/link"): RouteAccess("auth.logout_link.revoke", UNSTABLE),
    # Dashboard user only (chat_folders._toggle_slot_user_flag): 403 for an app token
    # and an internal caller.
    ("PATCH", "/api/chat/slots/{slot}/source-links-off"): RouteAccess(
        "chat.source_links_off.change", UNSTABLE
    ),
    # Dashboard user, or the slot's owner app through the slot-ownership checkpoint
    # (chat_history_search.py); bounded transcript reads, writes nothing.
    ("GET", "/api/chat/slots/{slot}/history/search"): RouteAccess(
        "chat.history_search.read", UNSTABLE
    ),
    ("GET", "/api/chat/slots/{slot}/history/around"): RouteAccess(
        "chat.history_around.read", UNSTABLE
    ),
    ("GET", "/api/chat/slots/{slot}/history/counts"): RouteAccess(
        "chat.history_counts.read", UNSTABLE
    ),
    # Owner gate, then the machine owner alone (crew_api_grant_requests). The Crew API
    # RFC marks the Owner tier experimental. Contract test: test_crew_api_grant_requests.py.
    ("GET", "/v1/crew/owner/grants"): RouteAccess("crew_api.grants.list", PREVIEW),
    # Managed-environment lane (managed_env/routes.py): owner dashboard, 403 for a
    # teammate (teammate_access), 404 while the crew_client preview is off. The list
    # calls the service's List on every read, so it is not a `.read`.
    ("GET", "/api/managed-environments/list"): RouteAccess("managed_environments.list", UNSTABLE),
    ("GET", "/api/managed-environments/ssh-config"): RouteAccess(
        "managed_environments.ssh_config.read", UNSTABLE
    ),
    ("POST", "/api/managed-environments/ssh-config"): RouteAccess(
        "managed_environments.ssh_config.write", UNSTABLE
    ),
    ("POST", "/api/managed-environments/{sandbox_id}/ssh-session"): RouteAccess(
        "managed_environments.ssh_session.start", UNSTABLE
    ),
    # Add one environment another client made, after a fresh List for this sign-in shows it.
    ("POST", "/api/managed-environments/{sandbox_id}/adopt"): RouteAccess(
        "managed_environments.adopt", UNSTABLE
    ),
    # Stop or resume one environment this Crew made or the person added; delete one it made.
    ("POST", "/api/managed-environments/{sandbox_id}/{action}"): RouteAccess(
        "managed_environments.change", UNSTABLE
    ),
    # Owner gate on every verb (handlers/ssh_auth_sock_consent.py), verbs from its audit
    # operation names; the GET writes nothing but a refused caller's audit row.
    ("GET", "/api/ssh-agent/consent"): RouteAccess("ssh_agent.consent.read", UNSTABLE),
    ("POST", "/api/ssh-agent/consent"): RouteAccess("ssh_agent.consent.grant", UNSTABLE),
    ("DELETE", "/api/ssh-agent/consent"): RouteAccess("ssh_agent.consent.revoke", UNSTABLE),
    # Owner dashboard only (handlers/tailnet_mobile.py _guard); removes only the listed
    # phone-access keys from config.local.json.
    ("POST", "/api/tailnet/mobile/clear-overrides"): RouteAccess(
        "tailnet.mobile.clear_overrides", UNSTABLE
    ),
    # Gateway auth middleware: 403 without a token (tips.api_tips_exploratory); reads only.
    ("GET", "/api/tips/exploratory"): RouteAccess("tips.exploratory.read", UNSTABLE),
    # Owner dashboard or Shared Crew teammate only: 404 for an app token, 403 for an
    # internal caller (dashboard/handlers/member_held_limits.py).
    ("GET", "/api/members/{slug}/held-limits"): RouteAccess("members.held_limits.read", UNSTABLE),
    # Same callers as the read; ends every held limit the page showed.
    ("POST", "/api/members/{slug}/held-limits/release"): RouteAccess(
        "members.held_limits.release", UNSTABLE
    ),
    # Machine owner only (dashboard/handlers/yolo_ack.py); 403 for anyone else.
    ("GET", "/api/chat/yolo-ack"): RouteAccess("chat.yolo_ack.read", UNSTABLE),
    # Machine owner only; writes the owner's yolo_ack.json record.
    ("PUT", "/api/chat/yolo-ack"): RouteAccess("chat.yolo_ack.set", UNSTABLE),
    # Owner gate (crew_api_owner.owner_route); Crew API phase 1a, no preview marking.
    # Contract test: test_crew_api_models.py.
    ("GET", "/v1/models"): RouteAccess("crew_api.models.list", STABLE),
    # Owner gate, then the machine owner alone (crew_api_responses).
    # Contract test: test_crew_api_responses.py.
    ("POST", "/v1/responses"): RouteAccess("crew_api.responses.create", PREVIEW),
    # The same two checks (crew_api_response_reads): a turn is read back by its owner.
    # Contract test: test_crew_api_response_reads.py.
    # ``.resolve``: a read removes an expired record and moves a damaged file aside.
    ("GET", "/v1/responses/{response_id}"): RouteAccess("crew_api.responses.resolve", PREVIEW),
    ("GET", "/v1/responses/{response_id}/input_items"): RouteAccess(
        "crew_api.responses.input_items.resolve", PREVIEW
    ),
    # The same two checks (crew_api_conversations): a conversation is its owner's.
    # Contract test: test_crew_api_conversations.py.
    ("POST", "/v1/conversations"): RouteAccess("crew_api.conversations.create", PREVIEW),
    ("GET", "/v1/conversations/{conversation_id}"): RouteAccess(
        "crew_api.conversations.resolve", PREVIEW
    ),
    ("POST", "/v1/conversations/{conversation_id}"): RouteAccess(
        "crew_api.conversations.update", PREVIEW
    ),
    ("DELETE", "/v1/conversations/{conversation_id}"): RouteAccess(
        "crew_api.conversations.delete", PREVIEW
    ),
    # The two paged reads (crew_api_conversation_lists), behind the same two checks.
    # Contract test: test_crew_api_conversation_routes.py.
    ("GET", "/v1/conversations"): RouteAccess("crew_api.conversations.list.resolve", PREVIEW),
    ("GET", "/v1/conversations/{conversation_id}/items"): RouteAccess(
        "crew_api.conversations.items.resolve", PREVIEW
    ),
    # Connected computers, this computer's own side (feature preview). Owner dashboard
    # only (connected_computers/local_routes.py `_owner`); the action names are the
    # handlers' own audit operation names.
    ("GET", "/api/connected-computers/local/grants"): RouteAccess(
        "connected_computers.grants.read", UNSTABLE
    ),
    ("DELETE", "/api/connected-computers/local/grants/{grant_id}"): RouteAccess(
        "connected_computers.revoke", UNSTABLE
    ),
    ("POST", "/api/connected-computers/local/off"): RouteAccess(
        "connected_computers.off", UNSTABLE
    ),
    # A verified app credential, or the owner gate and then the machine owner alone.
    # The owner's answer reads the model catalog, an app's reads its grant; writes nothing.
    # Contract test: test_crew_api_instance.py.
    ("GET", "/v1/crew/capabilities"): RouteAccess("crew_api.capabilities.read", PREVIEW),
    # A verified app credential, or the owner gate and then the machine owner alone.
    # The first call on a data home makes and keeps the instance id
    # (relay_client.lease.crew_instance_id), so the verb is not ``read``; it changes
    # nothing after that.
    ("GET", "/v1/crew/info"): RouteAccess("crew_api.info.resolve", PREVIEW),
    # An app credential reads the grant it was issued for (crew_api_grant_self); a
    # first-party session is answered 404. Reads only.
    # Contract test: test_crew_api_grant_self.py.
    ("GET", "/v1/crew/grant"): RouteAccess("crew_api.grant.read", PREVIEW),
    # A verified app credential lists the Bots of its grant; otherwise the owner gate
    # and then the machine owner alone, who lists every Bot. Reads only.
    # Contract test: test_crew_api_bots.py.
    ("GET", "/v1/crew/bots"): RouteAccess("crew_api.bots.list", PREVIEW),
    # Owner dashboard only (handlers/project_mcp_approve.py); the mcp.project_server
    # card's Apply. Records the approval and restarts open chats.
    ("POST", "/api/mcp/project-servers/approve"): RouteAccess(
        "mcp.project_servers.approve", UNSTABLE
    ),
}
