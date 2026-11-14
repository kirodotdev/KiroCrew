"""What an app is told when it lacks the user-session control grant.

An app token reaches tool approvals, approval modes and question cards on the
owner's chat sessions only with ``permissions.sessionApproval`` declared, the
owner's consent, and the app enabled. Without one of them the route answers
``403 session_approval_not_granted``. The ``cause`` in the body says which one is
missing, and ``error`` says who clears it and how:

- ``not_declared``: the developer adds the flag to ``app.json`` and updates the
  app; the update turns the app off until the owner turns it back on and allows
  chat control on the app's page.
- ``consent_pending``: the owner allows chat control on the app's page.
- ``disabled``: the owner turns the app on.
- ``unavailable``: nothing to change; a retry clears it.

The cause is read again when the refusal is built, off the event loop, so it
words the refusal only and never decides it. A grant that reads as held by then
moved under the two reads and is worded ``unavailable``.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

from kiro_crew.apps import permissions as app_permissions

# The app detail page's button for each cause the owner clears
# (``pages.appDetailPage.enable_and_allow_chat_control`` / ``.enable``).
_ALLOW_BUTTON = "Enable and allow chat control"
_ENABLE_BUTTON = "Enable"

_WHAT = "approve tool requests, answer question cards or change approval modes in chat sessions"


def session_approval_refusal_text(app_name: str, cause: str) -> str:
    """The ``error`` for *app_name* refused for *cause*: the cause, then the fix."""
    page = f"/apps/detail/{app_name}"
    if cause == app_permissions.SESSION_APPROVAL_NOT_DECLARED:
        return (
            f'App "{app_name}" cannot {_WHAT}: its installed app.json does not declare '
            'permissions.sessionApproval. To fix this, add "sessionApproval": true to '
            "permissions in the app's app.json and install that version with kirocrew "
            f"app update {app_name}. An app that registers itself registers its new "
            "manifest again instead. The new version is turned off until the dashboard "
            f'owner opens the app\'s page ({page}) and clicks "{_ALLOW_BUTTON}".'
        )
    if cause == app_permissions.SESSION_APPROVAL_CONSENT_PENDING:
        # A record from before the consent marker can still read enabled, and the
        # page offers the allow button only on a disabled app. A self-registered
        # app's page shows no Disable, but its Library menu does.
        return (
            f'App "{app_name}" cannot {_WHAT} yet: its installed version asks for '
            "permissions.sessionApproval, which the dashboard owner has not allowed. "
            f"To fix this, the dashboard owner opens the app's page ({page}) and clicks "
            f'"{_ALLOW_BUTTON}". If the app is still on, turn it off first, on the page '
            "or from its menu in the Library."
        )
    if cause == app_permissions.SESSION_APPROVAL_DISABLED:
        return (
            f'App "{app_name}" cannot {_WHAT} while it is turned off. To fix this, the '
            f'dashboard owner opens the app\'s page ({page}) and clicks "{_ENABLE_BUTTON}".'
        )
    return (
        f'App "{app_name}" cannot {_WHAT} right now: its permissions could not be read, '
        "or an update of the app is in progress. Retry in a moment. If this keeps "
        f"happening, check the app with: kirocrew app info {app_name}"
    )


async def session_approval_refusal(request_app: str) -> web.Response:
    """The ``403 session_approval_not_granted`` answer for *request_app*."""
    cause = await asyncio.to_thread(app_permissions.session_approval_denial_cause, request_app)
    if not cause:
        cause = app_permissions.SESSION_APPROVAL_UNAVAILABLE
    return web.json_response(
        {
            "error": session_approval_refusal_text(request_app, cause),
            "code": "session_approval_not_granted",
            "cause": cause,
        },
        status=403,
    )
