"""The Crew API spec's availability marks against the routes the gateway serves.

An operation in ``crew-public.yaml`` or ``crew-owner.yaml`` carries two marks when the
gateway serves it:

* ``x-crew-status: available``. An operation without it is planned.
* ``x-crew-available-to``: the callers the gateway serves it to, ``owner`` alone or the
  list ``owner``, ``app``.

These tests hold both marks equal to the gateway: the routes a default startup
registers under the Crew API's contract (``crew_api_errors.is_crew_api_path``), and for
each route the callers ``crew_api_auth``'s own lists name. Nothing here lists a route
by hand, so a route added, removed or opened to apps fails here until the spec says so.

Adding an endpoint: ``docs/system-specs/modules/route-access.md`` ("A Crew API route").

The live router is the one ``test_route_access_labels.py`` reads. The routes named in
the unit tests below are invented.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from kiro_crew.dashboard import crew_api_auth
from kiro_crew.dashboard.crew_api_errors import is_crew_api_path, is_owner_path

SPEC_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "request-for-change" / "assets" / "crew-api"
)
SPEC_FILES = ("crew-public.yaml", "crew-owner.yaml")
AVAILABLE = "available"
STATUSES = frozenset({AVAILABLE, "planned"})
OWNER = frozenset({"owner"})
OWNER_AND_APP = frozenset({"owner", "app"})
_METHODS = frozenset({"get", "put", "post", "delete", "patch", "head", "options", "trace"})

Route = tuple[str, str]


def _operations(paths: Mapping[str, Any]) -> dict[Route, Mapping[str, Any]]:
    """Each operation under an OpenAPI ``paths`` object, keyed by (METHOD, path)."""
    return {
        (method.upper(), path): operation
        for path, item in paths.items()
        for method, operation in item.items()
        if method in _METHODS
    }


def _spec_operations() -> dict[Route, Mapping[str, Any]]:
    found: dict[Route, Mapping[str, Any]] = {}
    for name in SPEC_FILES:
        spec = yaml.safe_load((SPEC_DIR / name).read_text(encoding="utf-8"))
        found.update(_operations(spec["paths"]))
    return found


def _marked_callers(operation: Mapping[str, Any]) -> frozenset[str]:
    """An operation's ``x-crew-available-to``, one word or a list of them, as a set."""
    value = operation.get("x-crew-available-to", ())
    return frozenset([value] if isinstance(value, str) else value)


def _callers_served(
    route: Route, owner_only: Collection[str], app_served: Collection[Route]
) -> frozenset[str] | None:
    """The callers the gateway serves *route* to, read from its own lists.

    *owner_only* holds the Public path patterns behind the owner gate and
    *app_served* the routes an app credential reaches. An Owner route serves the
    owner alone. ``None`` when no list names the route.
    """
    if route in app_served:
        return OWNER_AND_APP
    if is_owner_path(route[1]) or route[1] in owner_only:
        return OWNER
    return None


def _gateway_callers(route: Route) -> frozenset[str] | None:
    return _callers_served(
        route, crew_api_auth.OWNER_ONLY_PUBLIC_ROUTES, crew_api_auth.APP_SERVED_ROUTES
    )


def _order(route: Route) -> tuple[str, str]:
    return (route[1], route[0])


def _drift(
    served: Mapping[Route, frozenset[str] | None], operations: Mapping[Route, Mapping[str, Any]]
) -> dict[str, list[Route]]:
    """What disagrees between the served routes and the spec's marks, by kind.

    *served* maps each route the gateway serves to the callers it serves it to.
    """
    marked = {key for key, op in operations.items() if op.get("x-crew-status") == AVAILABLE}
    described = served.keys() & operations.keys()
    kinds = {
        "served, with no operation in the spec": served.keys() - operations.keys(),
        "served, and not marked x-crew-status: available": described - marked,
        "marked x-crew-status: available, and not served": marked - served.keys(),
        "served, and no list in crew_api_auth says to which callers": {
            key for key, callers in served.items() if callers is None
        },
        "x-crew-available-to is not the callers the gateway serves": {
            key
            for key in described
            if served[key] is not None and _marked_callers(operations[key]) != served[key]
        },
        "x-crew-available-to on an operation the gateway does not serve": {
            key
            for key, op in operations.items()
            if "x-crew-available-to" in op and key not in served
        },
    }
    return {kind: sorted(routes, key=_order) for kind, routes in kinds.items()}


@pytest.mark.asyncio
async def test_the_availability_marks_are_what_the_gateway_serves(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Checked against one live router: ``_dashboard`` runs a full startup per use."""
    from test_dashboard_server_startup_coverage import _dashboard

    monkeypatch.delenv("KIROCREW_DEV_MODE", raising=False)
    async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
        routes = {
            (route.method, route.resource.canonical)
            for route in runner.app.router.routes()
            if route.resource is not None
            # Registering a GET route registers its HEAD route; the spec describes the GET.
            and route.method != "HEAD" and is_crew_api_path(route.resource.canonical)
        }
    assert routes, "the startup registered no Crew API route: the route filter is wrong"
    served = {route: _gateway_callers(route) for route in routes}

    lines: list[str] = []
    for kind, found in _drift(served, _spec_operations()).items():
        if found:
            lines.append(f"{kind}:")
        for route in found:
            callers = ", ".join(sorted(served.get(route) or ()))
            lines.append(f"  {route[0]} {route[1]}" + (f" (serves: {callers})" if callers else ""))
    assert not lines, (
        "the Crew API spec and the gateway disagree. Fix the marks in "
        "docs/request-for-change/assets/crew-api/ "
        "(docs/system-specs/modules/route-access.md, 'A Crew API route'):\n" + "\n".join(lines)
    )


def test_every_status_mark_is_a_known_value() -> None:
    unknown = sorted(
        (
            key
            for key, op in _spec_operations().items()
            if "x-crew-status" in op and op["x-crew-status"] not in STATUSES
        ),
        key=_order,
    )
    assert unknown == []


def test_each_kind_of_drift_is_reported() -> None:
    right = ("GET", "/v1/widgets")
    unmarked = ("POST", "/v1/widgets")
    planned = ("DELETE", "/v1/widgets/{widget_id}")
    undescribed = ("GET", "/v1/widgets/{widget_id}")
    gone = ("PATCH", "/v1/widgets/{widget_id}")
    unlisted = ("GET", "/v1/gadgets")
    too_narrow = ("POST", "/v1/gadgets")
    too_wide = ("DELETE", "/v1/gadgets")
    mark = {"x-crew-status": AVAILABLE, "x-crew-available-to": "owner"}
    operations = {
        right: mark,
        unmarked: {},
        planned: {"x-crew-status": "planned"},
        gone: mark,
        unlisted: mark,
        too_narrow: mark,
        too_wide: {"x-crew-status": AVAILABLE, "x-crew-available-to": ["owner", "app"]},
    }
    served = {
        right: OWNER,
        unmarked: OWNER,
        undescribed: OWNER,
        unlisted: None,
        too_narrow: OWNER_AND_APP,
        too_wide: OWNER,
    }

    assert _drift(served, operations) == {
        "served, with no operation in the spec": [undescribed],
        "served, and not marked x-crew-status: available": [unmarked],
        "marked x-crew-status: available, and not served": [gone],
        "served, and no list in crew_api_auth says to which callers": [unlisted],
        "x-crew-available-to is not the callers the gateway serves": sorted(
            [unmarked, too_narrow, too_wide], key=_order
        ),
        "x-crew-available-to on an operation the gateway does not serve": [gone],
    }
    agreeing = {right: mark, planned: operations[planned]}
    assert not any(_drift({right: OWNER}, agreeing).values())


def test_the_callers_come_from_the_gateways_lists() -> None:
    owner_only = {"/v1/widgets"}
    app_served = {("GET", "/v1/widgets")}

    assert _callers_served(("GET", "/v1/widgets"), owner_only, app_served) == OWNER_AND_APP
    assert _callers_served(("POST", "/v1/widgets"), owner_only, app_served) == OWNER
    assert _callers_served(("GET", "/v1/crew/owner/widgets"), owner_only, app_served) == OWNER
    assert _callers_served(("GET", "/v1/gadgets"), owner_only, app_served) is None
    assert _callers_served(("GET", "/v1/gadgets"), (), {("GET", "/v1/gadgets")}) == OWNER_AND_APP


def test_only_operations_are_read_from_a_paths_object() -> None:
    paths = {
        "/v1/widgets": {
            "parameters": [{"name": "limit"}],
            "get": {"x-crew-status": AVAILABLE},
            "post": {},
        }
    }

    assert set(_operations(paths)) == {("GET", "/v1/widgets"), ("POST", "/v1/widgets")}
