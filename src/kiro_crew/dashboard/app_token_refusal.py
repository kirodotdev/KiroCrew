"""The refusal an app token gets for a request no app token may make.

Some requests are refused to every app token, whatever its manifest declares: the
owner's question cards, a mobile sign-in link, dismissing a run the gateway no
longer tracks, stopping all of a chat's subagents. A refusal that says only "app
token not allowed" reads like the API scope refusal, which a ``permissions.api``
entry clears, so this one says that no permission lets an app token do it, and
who can do it instead. It speaks of the action, not the route: a route can admit
an app token for another action, as ``DELETE /api/spawn/{agent_id}`` cancels a
live run.

The code stays ``app_token_forbidden``, and ``error`` carries the cause and the
next step.
"""

from __future__ import annotations

from aiohttp import web


def app_token_refusal(action: str, remedy: str) -> web.Response:
    """The ``403 app_token_forbidden`` answer: app tokens cannot *action*, *remedy* instead."""
    error = (
        f"App tokens cannot {action}, and no permission in app.json lets an app "
        f"token do this. {remedy}"
    )
    return web.json_response({"error": error, "code": "app_token_forbidden"}, status=403)
