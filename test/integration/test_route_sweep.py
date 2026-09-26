"""Every parameter-less GET route, two contracts each, through the running gateway.

The routes are read from the live router at boot (``gw.registered_routes()``),
so a route added anywhere in the dashboard is swept the next time this runs,
and a route that stops being guarded or starts failing on a fresh home is
named by path. Two contracts:

* GUARDED -- without credentials the route answers 401 or 403. The routes that
  answer anything else unauthenticated are listed in ``UNGUARDED_GET`` with
  the status each answers and the reason; a route that is not listed and
  answers outside 401/403 is a guard regression, and a route that is listed
  and stops answering its status is a broken liveness, pre-login or asset
  contract.
* SERVES -- with credentials the route does not fail with a 5xx on a fresh
  home. A 503 whose body is a JSON ``error`` naming what is unavailable is a
  contract (a subsystem this boot does not run, said plainly); a 500 never is.

Two small exclusion tables carry the routes the SERVES sweep cannot judge:
``HELD_OPEN`` (a long-poll or SSE response that does not end) and
``REACHES_NETWORK`` (a fetch the rootdir conftest fences). Both are exact
paths with a reason; nothing is pattern-excluded.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.integration

#: Routes that answer something other than 401/403 WITHOUT credentials: the
#: status each answers, and why it may. Every GET the router serves is swept,
#: the SPA shell and its assets included; this table is the whole exception.
UNGUARDED_GET: dict[str, tuple[int, str]] = {
    "/": (200, "the SPA shell; an unauthenticated browser needs it to render the login"),
    "/favicon.ico": (200, "SPA asset served beside the shell"),
    "/logo.png": (200, "SPA asset served beside the shell"),
    "/browser-view": (
        404,
        "native browser panel route; this build serves no panel, so 404 to everyone",
    ),
    "/api/health": (200, "liveness probe for supervisors; carries only ok/app/version"),
    "/api/live": (200, "liveness probe (alias of health)"),
    "/api/ready": (200, "readiness probe for supervisors; boolean startup checks only"),
    "/api/theme/boot": (
        200,
        "pre-login theme and onboarding flags the SPA needs to render the login",
    ),
}

#: Routes whose authenticated GET holds the connection open (SSE, long-poll):
#: a timeout here is the contract, so the SERVES sweep does not judge them.
HELD_OPEN: dict[str, str] = {
    "/api/stream": "SSE event stream",
    "/api/logs": "long-poll log tail",
}

#: Routes whose authenticated GET reaches the network on a fresh home. The
#: rootdir conftest fences that URL with an AssertionError the handler does not
#: catch (it is not a network error), so the fenced answer is a 500 that says
#: nothing about the product; the route is judged by its own tests.
REACHES_NETWORK: dict[str, str] = {
    "/api/apps/registry": "fetches the official app catalog (apps.crew.kiro.dev)",
}

PER_REQUEST_SECS = 10.0


def _parameterless_get_routes(gw) -> list[str]:
    return sorted(
        canonical
        for (method, canonical) in gw.registered_routes()
        if method == "GET" and "{" not in canonical
    )


async def _status(gw, path: str, *, auth: bool) -> tuple[int | str, str]:
    try:
        resp = await gw.get(path, auth=auth, timeout=PER_REQUEST_SECS)
    except asyncio.TimeoutError:
        return "timeout", ""
    try:
        body = await resp.read()
    except asyncio.TimeoutError:
        return "timeout", ""
    finally:
        resp.release()
    return resp.status, body[:200].decode("utf-8", "replace")


def _names_the_unavailable(body: str) -> bool:
    try:
        doc = json.loads(body)
    except ValueError:
        return False
    return isinstance(doc, dict) and isinstance(doc.get("error"), str) and bool(doc["error"])


@pytest.mark.asyncio
async def test_every_parameterless_get_route_is_guarded(gateway_boot) -> None:
    async with gateway_boot() as gw:
        routes = _parameterless_get_routes(gw)
        assert len(routes) > 300, len(routes)
        unguarded: list[tuple[str, int | str]] = []
        listed_but_changed: list[tuple[str, int | str, int]] = []
        for path in routes:
            status, _ = await _status(gw, path, auth=False)
            if path in UNGUARDED_GET:
                expected = UNGUARDED_GET[path][0]
                if status != expected:
                    listed_but_changed.append((path, status, expected))
            elif status not in (401, 403):
                unguarded.append((path, status))
        assert not unguarded, f"answered without credentials: {unguarded}"
        assert not listed_but_changed, f"listed unguarded but status moved: {listed_but_changed}"
        assert set(UNGUARDED_GET) <= set(routes), sorted(set(UNGUARDED_GET) - set(routes))


@pytest.mark.asyncio
async def test_every_parameterless_get_route_serves_on_a_fresh_home(gateway_boot) -> None:
    async with gateway_boot() as gw:
        routes = _parameterless_get_routes(gw)
        skipped = {**HELD_OPEN, **REACHES_NETWORK}
        assert set(skipped) <= set(routes), sorted(set(skipped) - set(routes))
        failing: list[tuple[str, int | str, str]] = []
        for path in routes:
            if path in skipped:
                continue
            status, body = await _status(gw, path, auth=True)
            if status == "timeout":
                failing.append((path, status, "held the connection open; list it in HELD_OPEN"))
            elif status == 503 and _names_the_unavailable(body):
                continue
            elif not isinstance(status, int) or status >= 500:
                failing.append((path, status, body))
        assert not failing, "\n".join(f"{p} -> {s}: {b}" for p, s, b in failing)
