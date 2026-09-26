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
  home. The routes that answer 503 on a fresh home are listed in
  ``UNAVAILABLE_ON_A_FRESH_HOME`` with what each says is unavailable; a route
  that is listed must still answer 503 with that JSON ``error``, a route that
  is not listed must answer below 500, and a 500 is never a contract.

Two small exclusion tables carry the routes the SERVES sweep cannot judge:
``HELD_OPEN`` (a long-poll or SSE response that does not end) and
``REACHES_NETWORK`` (a fetch the rootdir conftest fences, or a call to the
operator's cloud account the harness fences). Every table is exact paths
with a reason; nothing is pattern- or prefix-excluded. The GUARDED sweep
skips nothing: the held-open and network routes answer 401/403 at once
without credentials, and are asserted to.
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
#: app catalog fetch is fenced by the rootdir conftest with an AssertionError
#: the handler does not catch (it is not a network error), so the fenced answer
#: is a 500 that says nothing about the product. The cloud preflight shells the
#: real ``aws`` CLI four times against whatever ``~/.aws`` resolves; the
#: harness points the CLI at credential files that do not exist, so on a
#: developer machine it fails to resolve rather than exercising their account,
#: and the route is judged by its own tests, not by this sweep.
REACHES_NETWORK: dict[str, str] = {
    "/api/apps/registry": "fetches the official app catalog (apps.crew.kiro.dev)",
    "/api/cloud/preflight": "shells the aws CLI (sts, iam) against the operator's profile",
    "/api/deploy/profiles": "shells `aws configure list-profiles`; the CLI's start-up on a "
    "host that has it is not bounded by this sweep",
}

#: Routes that answer 503 on a fresh home, and what each names as unavailable.
#: Exact paths, like every other table here: a 503 elsewhere is a failure.
UNAVAILABLE_ON_A_FRESH_HOME: dict[str, str] = {
    "/api/capability/agents": "capability manager not available",
    "/api/capability/mcp": "capability manager not available",
    "/api/capability/mcp/registry": "capability manager not available",
    "/api/capability/plugins": "capability manager not available",
    "/api/capability/skills": "capability manager not available",
    "/api/models": "model list returned empty output",
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


def _error_text(body: str) -> str:
    try:
        doc = json.loads(body)
    except ValueError:
        return ""
    return (
        doc.get("error", "") if isinstance(doc, dict) and isinstance(doc.get("error"), str) else ""
    )


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
        # The routes the SERVES sweep cannot judge were judged here: a held-open
        # or network-reaching route that stopped refusing would be in
        # ``unguarded`` above (a timeout is not 401/403 either).
        assert set(HELD_OPEN) | set(REACHES_NETWORK) <= set(routes)


@pytest.mark.asyncio
async def test_every_parameterless_get_route_serves_on_a_fresh_home(gateway_boot) -> None:
    async with gateway_boot() as gw:
        routes = _parameterless_get_routes(gw)
        skipped = {**HELD_OPEN, **REACHES_NETWORK}
        for table in (skipped, UNAVAILABLE_ON_A_FRESH_HOME):
            assert set(table) <= set(routes), sorted(set(table) - set(routes))
        failing: list[tuple[str, int | str, str]] = []
        for path in routes:
            if path in skipped:
                continue
            status, body = await _status(gw, path, auth=True)
            if status == "timeout":
                failing.append((path, status, "held the connection open; list it in HELD_OPEN"))
            elif path in UNAVAILABLE_ON_A_FRESH_HOME:
                expected = UNAVAILABLE_ON_A_FRESH_HOME[path]
                if status != 503 or expected not in _error_text(body):
                    failing.append((path, status, f"listed as 503 {expected!r}; got {body}"))
            elif not isinstance(status, int) or status >= 500:
                failing.append((path, status, body))
        assert not failing, "\n".join(f"{p} -> {s}: {b}" for p, s, b in failing)
