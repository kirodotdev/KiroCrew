"""Every route the gateway registers has a label, or a line in the shrink-only baseline.

A label is an entry in ``kiro_crew.dashboard.route_access.ROUTES``. The baseline,
``.github/route-access-baseline.txt``, lists the routes that had none when the table
arrived. A route added without a label is in neither, and fails here.

The live router is the one ``test_dashboard_route_table.py`` reads. A route an installed
app registers at run time is not in it.

The routes named in the unit tests below are invented: no real route is labelled.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard import route_access
from kiro_crew.dashboard.route_access import STABLE, UNSTABLE, RouteAccess

BASELINE = Path(__file__).resolve().parent.parent / ".github" / "route-access-baseline.txt"

Route = tuple[str, str]


def _read_baseline() -> list[Route]:
    """The baseline's rows in file order, as (METHOD, path)."""
    rows: list[Route] = []
    for line in BASELINE.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        method, _, path = line.partition(" ")
        rows.append((method, path))
    return rows


def _order(route: Route) -> tuple[str, str]:
    """The baseline's sort key: by path, then by method."""
    return (route[1], route[0])


def _without_implied_head(routes: set[Route]) -> set[Route]:
    """Drop each HEAD route that sits beside a GET route of the same path.

    Registering a GET route registers that HEAD route too, and it takes the GET
    route's label, so it needs no entry and no row of its own.
    """
    return {r for r in routes if r[0] != "HEAD" or ("GET", r[1]) not in routes}


def _unlabelled(
    routes: set[Route], baseline: set[Route], labels: Mapping[Route, RouteAccess]
) -> list[Route]:
    """The routes with neither a label nor a baseline row."""
    return sorted((r for r in routes if r not in labels and r not in baseline), key=_order)


def _malformed(labels: Mapping[Route, RouteAccess]) -> list[Route]:
    """The entries whose action name or tier is not a valid one."""
    return sorted(
        (
            key
            for key, entry in labels.items()
            if not route_access.ACTION_NAME_RE.fullmatch(entry.action)
            or entry.tier not in route_access.TIERS
        ),
        key=_order,
    )


@pytest.mark.asyncio
async def test_every_registered_route_is_labelled_or_baselined(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Checked against one live router: ``_dashboard`` runs a full startup per use."""
    from test_dashboard_server_startup_coverage import _dashboard

    # Development mode registers a route a default startup does not.
    monkeypatch.delenv("KIROCREW_DEV_MODE", raising=False)
    async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
        registered = {
            (route.method, route.resource.canonical)
            for route in runner.app.router.routes()
            if route.resource is not None
        }
    live = _without_implied_head(registered)

    missing = _unlabelled(live, set(_read_baseline()), route_access.ROUTES)
    assert missing == [], (
        "routes with no entry in kiro_crew.dashboard.route_access.ROUTES "
        "(add one; the baseline only shrinks, see "
        "docs/system-specs/modules/route-access.md):\n"
        + "\n".join(f"{method} {path}" for method, path in missing)
    )

    # A label whose key matches no route labels nothing: a typo, or a route that moved.
    orphans = sorted(set(route_access.ROUTES) - live, key=_order)
    assert orphans == [], "ROUTES entries that name no registered route:\n" + "\n".join(
        f"{method} {path}" for method, path in orphans
    )


def test_a_new_route_without_a_label_or_a_baseline_row_is_reported() -> None:
    new = ("POST", "/api/widgets/{widget_id}/reset")
    labelled = ("GET", "/api/widgets/{widget_id}")
    baselined = ("GET", "/api/widgets")
    labels = {labelled: RouteAccess("widgets.read", UNSTABLE)}

    assert _unlabelled({new, labelled, baselined}, {baselined}, labels) == [new]


def test_a_head_route_beside_its_get_route_needs_no_row() -> None:
    get, head = ("GET", "/api/widgets"), ("HEAD", "/api/widgets")
    lone_head = ("HEAD", "/api/ping")

    assert _without_implied_head({get, head, lone_head}) == {get, lone_head}


def test_baseline_rows_are_well_formed_sorted_and_unique() -> None:
    rows = _read_baseline()
    assert rows, "the baseline is empty: delete the file and this test with it"
    malformed = [r for r in rows if not r[0].isupper() and r[0] != "*" or not r[1].startswith("/")]
    assert malformed == []
    assert rows == sorted(rows, key=_order), "baseline rows are sorted by path, then method"
    assert len(rows) == len(set(rows)), "a baseline row is repeated"


def test_a_labelled_route_has_no_baseline_row() -> None:
    """A route that gains a label leaves the baseline in the same change."""
    stale = [r for r in _read_baseline() if r in route_access.ROUTES]
    assert stale == [], "delete these baseline rows, the routes are labelled:\n" + "\n".join(
        f"{method} {path}" for method, path in stale
    )


def test_every_label_is_a_well_formed_action_name_and_a_known_tier() -> None:
    assert _malformed(route_access.ROUTES) == []


def test_a_malformed_action_name_or_an_unknown_tier_is_reported() -> None:
    good = ("GET", "/api/widgets")
    one_part = ("POST", "/api/widgets")
    upper_case = ("PUT", "/api/widgets")
    bad_tier = ("DELETE", "/api/widgets")
    labels = {
        good: RouteAccess("widgets.read", STABLE),
        one_part: RouteAccess("widgets", UNSTABLE),
        upper_case: RouteAccess("Widgets.Write", UNSTABLE),
        bad_tier: RouteAccess("widgets.delete", "permanent"),
    }

    assert _malformed(labels) == sorted([one_part, upper_case, bad_tier], key=_order)
