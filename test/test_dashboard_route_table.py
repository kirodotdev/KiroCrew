"""The dashboard route table's ordering contract.

The 447 registrations moved out of ``start_dashboard`` into ``dashboard/routes/``.
Splitting them made one previously-implicit property easy to break silently:
**aiohttp resolves a request against its routes in REGISTRATION order.** Several
routes in this table rely on it -- a literal path is registered before a pattern
that would otherwise swallow it, and the original inline table said so in
comments (``/api/steering/search`` before ``/api/steering/{key}``,
``/api/chat/slots/cleanup`` before ``/api/chat/slots/{slot}``, the skills browser
``/-/`` paths before ``/api/skills/{name:.+}``).

Nothing in aiohttp complains when that order is wrong. The shadowed route simply
stops being reachable: the pattern matches first and its handler answers a path
it was never meant to serve, so the failure surfaces as a wrong response body or
a 404 for a path that is still in the table.

The generic guard below is the point of this file: rather than pinning the pairs
the comments happen to mention, it derives every literal/pattern pair from the
live router and asserts the literal wins. That covers pairs nobody wrote a
comment about, and it keeps covering them as routes are added.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard import routes as routes_pkg


def test_registrar_tuple_is_explicit_and_not_alphabetical() -> None:
    """The tuple's order IS the table's order, so it must not be sorted.

    This pins the DECLARED order only. On its own it is weak -- it cannot see a
    change to how ``register_all`` iterates -- so
    ``test_effective_registration_order_matches_the_declared_order`` below pins
    what the router actually ends up with. Both are needed: this one localizes a
    reordering to the tuple, that one catches every other way the order can move.
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
    )
    assert list(routes_pkg.REGISTRAR_NAMES) != sorted(routes_pkg.REGISTRAR_NAMES), (
        "the registrar order must stay the table's original order, not alphabetical"
    )


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

    A slice's first route is not a usable anchor on its own: some paths are also
    registered earlier by ``_register_mcp_routes`` (``/api/notifications`` among
    them), so a naive ``index()`` finds the other registration and reports the
    slice as running far earlier than it does. Anchoring on a pair that occurs
    exactly once removes that ambiguity.
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

    Deliberately one test rather than three. Each property needs the real app and
    ``_dashboard`` runs a full startup and teardown per use, so three of them cost
    three startups to assert three things about a single router that is identical
    in all three cases. Sharing one build is the cheaper shape, and each assertion
    still names the property it broke.

    The properties:

    1. **Effective order matches the declared order.** The complement to the
       tuple pin above -- reversing or shuffling how ``register_all`` iterates
       leaves the tuple untouched, so only a check against the live router catches
       it. Because ordering is what stops a pattern swallowing a literal, "the
       tuple is right" is not the property that matters.
    2. **No literal is shadowed by an earlier pattern.** Derived from the router
       rather than a hand-written pair list, so it guards pairs nobody commented
       and new ones as they appear.
    3. **No pattern is shadowed by a DIFFERENT earlier pattern.** Property 2
       cannot see this, and it is the likelier future mistake: a new
       ``/api/chat/slots/{x}/...`` variant added to the wrong slice would take its
       sibling's traffic. Only flagged when the two resolve to different handlers,
       since the table legitimately points several methods of one pattern at one
       handler.

    Scope for 2 and 3 is paths the table registers. Builtin apps register their
    own routes afterwards through a separate mechanism, and that surface has a
    pre-existing literal-after-pattern overlap -- ``/api/apps/{name}/config``
    precedes the literal ``/config`` routes of the apps whose own API base is two
    segments deep, so the generic handler answers instead of theirs and skips
    their enabled-gating. This PR neither introduced nor worsened it (the
    before/after route order is byte-identical) and fixing it belongs with
    ``apps/routes.py``.
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
    assert ascending == sorted(ascending), (
        "slices are registered out of declared order: "
        + ", ".join(f"{n}@{i}" for n, i in positions)
    )

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
    assert pattern_hits == [], (
        "patterns registered after a different matching pattern:\n" + "\n".join(pattern_hits)
    )


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

    Read from ``get_info()`` rather than rebuilt from ``canonical``: canonical
    DROPS a pattern's inner regex, rendering
    ``/{name:manifest\\.json|sw\\.js|icon-\\d+\\.png|pcm-worklet\\.js}`` as bare
    ``/{name}``. Reconstructing from that turns a four-file allowlist into a
    catch-all and reports shadowing that cannot happen.
    """
    info = resource.get_info()
    pattern = info.get("pattern")
    return pattern if pattern is not None else None


def _table_owned_paths() -> set[tuple[str, str]]:
    """The (method, path) pairs the route table itself registers.

    Ownership is defined by "a slice module registers this path", not by the
    handler's ``__module__``. That distinction matters: the table registers
    ``/api/suggestions`` and ``/api/tips/*``, whose handlers live in
    ``kiro_crew.suggestions`` and ``kiro_crew.tips``, so filtering on the handler
    module would silently drop exactly the routes a shadowing check is for.
    """
    owned: set[tuple[str, str]] = set()
    for name in routes_pkg.REGISTRAR_NAMES:
        owned |= set(_slice_routes(name))
    return owned
