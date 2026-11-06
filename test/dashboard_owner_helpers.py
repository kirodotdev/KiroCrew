"""Owner identity for the fixtures that exercise owner-gated dashboard routes.

The owner gate (``handlers._shared.require_owner_dashboard_request``) reads
``request.app["state"]`` and the token claims, so on a bare ``web.Application()`` a
test would fail on the gate (500/403) instead of on its subject.

``as_owner`` installs only an ``owner_id`` of ``""`` (when the app has no ``state``
of its own) and a token-middleware stand-in defaulting the caller to ``local-app``.
``owner_claims`` gives a hand-rolled request stub the same identity. A NON-owner
sends ``X-Test-User``; an app token sends ``X-Test-App``.
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


def owner_claims(request, *, user: str = "local-app", app_token: str = ""):
    """Make a hand-rolled request DOUBLE read as the dashboard owner.

    The ``as_owner`` identity for a ``MagicMock(spec=web.Request)``. One dict serves
    ``get("user")``, ``"app" in request`` and ``request["app"]``; a Mock subscript
    equals no string, so the caller would read as an app token. Returns *request*.
    """
    claims = {"user": user, "app": app_token}
    request.get = lambda key, default=None: claims.get(key, default)
    for name, impl in (
        ("__contains__", lambda key: key in claims),
        ("__getitem__", lambda key: claims[key]),
    ):
        hook = getattr(request, name, None)
        if hook is not None and hasattr(hook, "side_effect"):
            hook.side_effect = impl
    return request
