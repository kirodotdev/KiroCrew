"""The dashboard route table's ordering contract.

aiohttp matches routes in registration order and never warns when a pattern shadows a
literal (``/api/steering/search`` before ``/api/steering/{key}``). The guard derives
every literal/pattern pair from the live router, so new routes are covered too.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard import routes as routes_pkg


def test_registrar_tuple_is_explicit_and_not_alphabetical() -> None:
    """The tuple's order IS the table's order, so it must not be sorted.

    This pins the declared order only; the live-router test below catches a change in
    how ``register_all`` iterates.
    """
    assert routes_pkg.REGISTRAR_NAMES == (
        "realtime",
        "memory",
        "messaging",
        "skills",
        "themes",
        "agent_config",
        "chat",
        "agents",
        "sessions",
        "taskrunner",
        "connections",
        "system",
        "browser_live",
        "shared_crew",
    )
    assert list(routes_pkg.REGISTRAR_NAMES) != sorted(
        routes_pkg.REGISTRAR_NAMES
    ), "the registrar order must stay the table's original order, not alphabetical"


def _slice_routes(slice_name: str) -> list[tuple[str, str]]:
    """Every (method, path) a slice module registers with a literal path.

    Read from source so the probe needs no app instance.
    """
    import ast

    path = Path(routes_pkg.__file__).parent / f"{slice_name}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("add_")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            out.append((node.func.attr.removeprefix("add_").upper(), node.args[0].value))
    return out


def _anchor_index(slice_name: str, ordered: list[tuple[str, str]]) -> int:
    """Earliest router position of a route registered ONLY by this slice.

    ``_register_mcp_routes`` registers some paths earlier (``/api/notifications``), so a
    plain ``index()`` would find that registration instead.
    """
    counts: dict[tuple[str, str], int] = {}
    for pair in ordered:
        counts[pair] = counts.get(pair, 0) + 1
    unique = [p for p in _slice_routes(slice_name) if counts.get(p) == 1]
    assert unique, f"{slice_name} has no uniquely-registered route to anchor on"
    return min(ordered.index(p) for p in unique)


@pytest.mark.asyncio
async def test_route_table_ordering_invariants(tmp_path: Path, monkeypatch: Any) -> None:
    """The three ordering properties, checked against ONE live router.

    One test because ``_dashboard`` runs a full startup per use. It checks: effective
    order matches the declared order; no literal is shadowed by an earlier pattern; no
    pattern is shadowed by an earlier pattern with a different handler.

    Only paths the table registers are checked. Builtin apps register theirs later, and
    there ``/api/apps/{name}/config`` shadows some apps' literal ``/config`` routes; that
    fix belongs with ``apps/routes.py``.
    """
    from test_dashboard_server_startup_coverage import _dashboard

    owned = _table_owned_paths()
    async with _dashboard(tmp_path, monkeypatch) as (runner, _state, _spies):
        ordered = [
            (
                route.method,
                route.resource.canonical,
                _resource_pattern(route.resource),
                getattr(route.handler, "__qualname__", repr(route.handler)),
            )
            for route in runner.app.router.routes()
            if route.resource is not None
        ]

    pairs = [(m, p) for m, p, _rx, _h in ordered]

    # 1. effective slice order
    positions = [(name, _anchor_index(name, pairs)) for name in routes_pkg.REGISTRAR_NAMES]
    ascending = [idx for _n, idx in positions]
    assert ascending == sorted(
        ascending
    ), "slices are registered out of declared order: " + ", ".join(f"{n}@{i}" for n, i in positions)

    # 2. literal shadowed by an earlier pattern
    literal_hits: list[str] = []
    for idx, (method, path, rx, _h) in enumerate(ordered):
        if rx is not None or (method, path) not in owned:
            continue
        for pidx, (pmethod, ppath, prx, _ph) in enumerate(ordered):
            if prx is None or pidx >= idx or pmethod != method:
                continue
            # fullmatch, not match: aiohttp's compiled patterns are not all
            # anchored at the end, and a prefix match would report
            # `/api/channels/{id}` as shadowing
            # `/api/channels/weixin/qr/status` even though `{id}` cannot span `/`.
            if prx.fullmatch(path):
                literal_hits.append(f"{method} {path} (#{idx}) shadowed by {ppath} (#{pidx})")
    assert literal_hits == [], "literals registered after a matching pattern:\n" + "\n".join(
        literal_hits
    )

    # 3. pattern shadowed by a different earlier pattern
    pattern_hits: list[str] = []
    for idx, (method, path, rx, handler) in enumerate(ordered):
        if rx is None or (method, path) not in owned:
            continue
        for pidx, (pmethod, ppath, prx, phandler) in enumerate(ordered):
            if prx is None or pidx >= idx or pmethod != method:
                continue
            if ppath == path or phandler == handler:
                continue  # same route, or same handler by design
            if prx.fullmatch(path):
                pattern_hits.append(
                    f"{method} {path} -> {handler} (#{idx}) shadowed by "
                    f"{ppath} -> {phandler} (#{pidx})"
                )
    assert (
        pattern_hits == []
    ), "patterns registered after a different matching pattern:\n" + "\n".join(pattern_hits)


def test_shared_crew_slice_matches_each_lane_registrar() -> None:
    """The slice's literal Shared Crew routes are exactly what each module registers."""
    from aiohttp import web

    from kiro_crew.dashboard.routes import shared_crew
    from kiro_crew.shared_crew import (
        routes_access,
        routes_admission,
        routes_capabilities,
        routes_collab,
        routes_identity,
        routes_invites,
        routes_people,
        routes_preview,
    )

    def table(*registrars: Any) -> list[tuple[str, str, Any]]:
        app = web.Application()
        for register in registrars:
            register(app)
        # By name: the slice registers the invite handlers through a lazy
        # wrapper so the invite module stays off the boot path.
        return [
            (route.method, route.resource.canonical, route.handler.__name__)
            for route in app.router.routes()
            if route.resource is not None
        ]

    assert table(shared_crew.register) == table(
        routes_identity.register,
        routes_admission.register,
        routes_collab.register,
        routes_capabilities.register,
        routes_preview.register,
        routes_invites.register,
        routes_access.register,
        routes_people.register,
    )


def test_the_typing_lease_takeover_sits_beside_its_read() -> None:
    """The takeover write is a literal Shared Crew route, registered right after the lease read."""
    routes = _slice_routes("shared_crew")
    read = routes.index(("GET", "/api/shared-crew/typing-lease"))
    assert routes[read + 1] == ("POST", "/api/shared-crew/typing-lease/takeover")


def test_every_slice_module_exposes_register() -> None:
    """A slice that loses ``register`` would drop its whole section silently."""
    import importlib

    for name in routes_pkg.REGISTRAR_NAMES:
        mod = importlib.import_module(f"kiro_crew.dashboard.routes.{name}")
        assert callable(getattr(mod, "register", None)), f"{name} has no register()"


def test_slice_files_and_registrars_agree() -> None:
    """A new slice file that nobody added to _REGISTRARS registers nothing."""
    pkg_dir = Path(routes_pkg.__file__).parent
    on_disk = {p.stem for p in pkg_dir.glob("*.py") if not p.stem.startswith("_")}
    assert on_disk == set(routes_pkg.REGISTRAR_NAMES)


def _resource_pattern(resource: Any) -> re.Pattern[str] | None:
    """The resource's own compiled pattern, or None for a plain literal path.

    Read from ``get_info()``: ``canonical`` drops the inner regex, turning a four-file
    allowlist like ``/{name:manifest\\.json|sw\\.js}`` into a catch-all ``/{name}``.
    """
    info = resource.get_info()
    pattern = info.get("pattern")
    return pattern if pattern is not None else None


def _table_owned_paths() -> set[tuple[str, str]]:
    """The (method, path) pairs the route table itself registers.

    Owned means a slice module registers it, not the handler's ``__module__``: the
    handlers for ``/api/suggestions`` and ``/api/tips/*`` live outside the table.
    """
    owned: set[tuple[str, str]] = set()
    for name in routes_pkg.REGISTRAR_NAMES:
        owned |= set(_slice_routes(name))
    return owned
