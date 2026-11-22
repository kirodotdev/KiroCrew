"""Every dashboard route carries an explicit delegated ``allow`` / ``deny`` tag.

A token minted through the ``dashboard_proxy`` grant (``via_app``) reaches only the routes
tagged ``allow``; a route with no tag is DENIED at runtime. This walk fails on any route
of the real table that no tag names, so a new route is tagged on purpose, and on a tag
that names no route, so the table cannot rot. It also pins the routes the spec names as
refused, whatever else the table says.
"""

from __future__ import annotations

import pytest
from aiohttp import web

from kiro_crew.dashboard.routes import register_all


def _walk() -> list[tuple[str, str]]:
    app = web.Application()
    register_all(app)
    pairs = []
    for route in app.router.routes():
        resource = route.resource
        assert resource is not None
        pairs.append((route.method, resource.canonical))
    return pairs


def _tag(method: str, canonical: str) -> str | None:
    from kiro_crew.dashboard.delegated_scope import route_tag

    return route_tag(method, canonical)


def test_no_route_of_the_table_is_untagged() -> None:
    untagged = sorted({f"{m} {p}" for m, p in _walk() if _tag(m, p) not in ("allow", "deny")})
    assert not untagged, "routes with no delegated allow/deny tag:\n" + "\n".join(untagged)


def test_no_tag_names_a_route_that_does_not_exist() -> None:
    from kiro_crew.dashboard.delegated_route_tags import ROUTE_TAGS

    real = {p for _m, p in _walk()}
    stale = sorted(set(ROUTE_TAGS) - real)
    assert not stale, "tags for routes that are not registered:\n" + "\n".join(stale)


def test_an_unlisted_route_has_no_tag() -> None:
    assert _tag("GET", "/api/a-route-nobody-tagged") is None
    assert _tag("POST", "/api/status") is None, "a method the table does not list"


_REFUSED = [
    ("GET", "/api/token/local"),
    ("POST", "/api/auth/mobile-link"),
    ("POST", "/api/apps/{name}/token"),
    ("POST", "/api/apps/register"),
    ("POST", "/api/webhooks/tokens"),
    ("POST", "/api/relay-viewer/token"),
    ("POST", "/api/apps/install"),
    ("POST", "/api/apps/{name}/enable"),
    ("POST", "/api/apps/{name}/disable"),
    ("POST", "/api/apps/{name}/uninstall"),
    ("POST", "/api/apps/{name}/update"),
    ("PUT", "/api/apps/{name}/bot-grants"),
    ("PUT", "/api/config/kirocrew"),
    ("POST", "/api/security/tool-grants"),
    ("POST", "/api/security/tool-grants/remove"),
    ("POST", "/v1/crew/owner/approvals/{approval_id}/decision"),
    ("POST", "/v1/crew/owner/grant-requests/{request_id}/decision"),
    ("DELETE", "/v1/crew/owner/grants/{grant_id}"),
    ("POST", "/api/chat/mode"),
    ("POST", "/api/restart"),
    ("POST", "/api/update/approve"),
    # Terminal and approvals are local-only (consistent with the owner approval routes).
    ("POST", "/api/terminal/sessions"),
    ("POST", "/api/terminal/external"),
    ("POST", "/api/terminal/complete"),
    ("POST", "/api/approvals/{id}/{action}"),
    ("POST", "/api/chat/slots/{slot}/approve"),
    ("POST", "/api/channels/{id}/agents/{aid}/approve"),
    # Agent, skill and theme code that runs on the host.
    ("PUT", "/api/agent/config"),
    ("PUT", "/api/agents/b64/{name_b64}/capabilities"),
    ("POST", "/api/agents/b64/{name_b64}/capabilities/detach"),
    ("PUT", "/api/agents/b64/{name_b64}/worker-kit"),
    ("PUT", "/api/agents/{name}/worker-kit"),
    ("PUT", "/api/agents/{name}/capabilities"),
    ("POST", "/api/agents/{name}/capabilities/detach"),
    # Agent definitions (tools, MCP servers, permissions), themes and skills: code and
    # grants that run on the host.
    ("POST", "/api/agents"),
    ("PUT", "/api/agents"),
    ("PUT", "/api/agents/{name}"),
    ("PATCH", "/api/agents/detail/{name}"),
    ("POST", "/api/themes"),
    ("PUT", "/api/themes/{slug}"),
    ("POST", "/api/skills"),
    ("PUT", "/api/skills/{name}"),
    # A terminal is a shell on the host.
    ("GET", "/api/ws/terminal/{session_id}"),
    ("DELETE", "/api/terminal/sessions/{session_id}"),
    ("POST", "/api/terminal/redact"),
    ("POST", "/api/skills/-/trust"),
    ("DELETE", "/api/skills/-/trust"),
    ("POST", "/api/skills/-/pending/{slug}/approve"),
    ("POST", "/api/skills/-/discover/install"),
    ("POST", "/api/themes/install"),
    ("POST", "/api/themes/install-zip"),
    ("PUT", "/api/chat/slots/{slot}/environment"),
    ("PUT", "/api/chat/slots/{slot}/environment/file"),
    # Linking a chat to an outside account.
    ("POST", "/api/chat/slots/{name}/slack-link"),
    ("POST", "/api/chat/slots/{slot}/slack-link"),
    ("POST", "/api/chat/slots/{name}/mirror-link"),
    ("POST", "/api/chat/slots/{slot}/mirror-link"),
    ("PUT", "/api/notifications/channels/settings"),
    ("POST", "/api/wakatime/oauth/authorize"),
    ("POST", "/api/wakatime/oauth/disconnect"),
]


@pytest.mark.parametrize("method,canonical", _REFUSED)
def test_the_credential_and_control_plane_routes_are_denied(method: str, canonical: str) -> None:
    assert (method, canonical) in set(_walk()), "the route this test pins does not exist"
    assert _tag(method, canonical) == "deny"


@pytest.mark.parametrize(
    "method,canonical",
    [("GET", "/api/status"), ("GET", "/api/sessions"), ("POST", "/api/chat")],
)
def test_the_plain_dashboard_routes_are_allowed(method: str, canonical: str) -> None:
    assert _tag(method, canonical) == "allow"


def test_every_route_sharing_a_handler_and_method_shares_a_tag() -> None:
    app = web.Application()
    register_all(app)
    groups: dict[tuple[object, str], set[tuple[str, str | None]]] = {}
    for route in app.router.routes():
        resource = route.resource
        assert resource is not None
        if route.method == "*":
            continue
        tag = _tag(route.method, resource.canonical)
        groups.setdefault((route.handler, route.method), set()).add((resource.canonical, tag))
    split = sorted(
        f"{method} {sorted(pairs)}"
        for (_h, method), pairs in groups.items()
        if len({tag for _p, tag in pairs}) > 1
    )
    assert not split, "one handler, two tags (deny must win on every spelling):\n" + "\n".join(
        split
    )


def test_every_b64_route_carries_the_tag_of_its_name_twin() -> None:
    from kiro_crew.dashboard.delegated_route_tags import ROUTE_TAGS

    real = {p for _m, p in _walk()}
    checked = 0
    for canonical, tags in ROUTE_TAGS.items():
        if "/b64/{name_b64}" not in canonical:
            continue
        twin = canonical.replace("/b64/{name_b64}", "/{name}")
        if twin not in real:
            continue
        for method, tag in tags.items():
            if method in ROUTE_TAGS[twin]:
                assert ROUTE_TAGS[twin][method] == tag, f"{method} {canonical} vs {twin}"
                checked += 1
    assert checked


def test_a_star_route_is_tagged_for_every_concrete_method() -> None:
    star = sorted({p for m, p in _walk() if m == "*"})
    assert "/apps/{name}/api/{path}" in star
    for canonical in star:
        from kiro_crew.dashboard.delegated_route_tags import ROUTE_TAGS

        expected = ROUTE_TAGS[canonical]["*"]
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
            assert _tag(method, canonical) == expected, f"{method} {canonical}"
