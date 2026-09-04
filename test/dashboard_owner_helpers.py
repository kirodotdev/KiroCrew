"""Owner identity for the fixtures that exercise owner-gated dashboard routes.

Routes such as ``PUT /api/dashboard/config``, ``POST /api/connections/mint``,
``POST /api/connections/cancel`` and ``POST /api/mcp/oauth/relay`` are owner-gated
(``handlers._shared.require_owner_dashboard_request``), and the gate reads
``request.app["state"]`` plus the authenticated claims the token-auth middleware
normally populates. A fixture that registers the handler on
a bare ``web.Application()`` therefore answers 500 (no ``state`` key) or 403 (no
owner claim) before the body-validation branch under test is ever reached -- the
test would still "pass or fail", but on the gate rather than on what it names.

``as_owner`` installs the minimum the gate needs and nothing else:

* an ``owner_id`` of ``""`` -- the standalone-local shape, where the predicate
  accepts the signed local bootstrap subject. Installed ONLY when the app has no
  ``state`` of its own, so a fixture carrying a real ``DashboardState`` keeps it.
* a stand-in for the token middleware, so the caller defaults to ``local-app``.

``NoConfiguredOwner`` is exported for the tests that hand-roll a request stub
instead of going through a ``TestClient``.

A test that wants a NON-owner sends ``X-Test-User``; one that wants an app token
sends ``X-Test-App``. The gate's own denial behaviour is covered directly in
``test_dashboard_files_coverage.TestDashboardConfigPutOwnerGate`` and in
``test_agent_config_owner_gate_invariant`` -- this helper exists so the OTHER
tests keep testing their own subject.
"""

from __future__ import annotations

from aiohttp import web


class NoConfiguredOwner:
    """The standalone-local shape: no owner configured yet."""

    owner_id = ""


@web.middleware
async def _identity(request: web.Request, handler):
    request["user"] = request.headers.get("X-Test-User", "local-app")
    request["app"] = request.headers.get("X-Test-App", "")
    return await handler(request)


def as_owner(app: web.Application) -> web.Application:
    """Make requests to *app* read as the dashboard owner. Returns *app*."""
    if "state" not in app:
        app["state"] = NoConfiguredOwner()
    app.middlewares.append(_identity)
    return app
