"""Security event log rows: ``ctx.audit`` for code that holds no context."""

from __future__ import annotations

import os

from kiro_crew.apps.audit_sdk import AuditSDK

#: The variable a spawned backend's app name arrives in (api-reference.md,
#: "Backend Environment Variables").
APP_NAME_ENV = "KIROCREW_APP_NAME"


def audit_log(app_name: str | None = None) -> AuditSDK:
    """The audit writer for *app_name*, or for this spawned backend's own app.

    The same writer ``ctx.audit`` is: ``record(operation, outcome, *, resources="",
    error="")`` appends one row to the gateway's append-only security event log,
    attributed ``app:<name>``, with the operation prefixed ``<name>.``, every field
    redacted downstream, and never raises. Without *app_name* the name is the
    ``KIROCREW_APP_NAME`` the gateway set when it spawned this process. Attribution
    is cooperative, as it is for ``ctx.audit``: it says which app wrote the row,
    not proof of it.

    Raises ``LookupError`` when no name is given and the variable is unset or
    blank, so a writer that would attribute rows to nobody is refused when it is
    built rather than dropping every row silently.
    """
    name = app_name if app_name is not None else os.environ.get(APP_NAME_ENV, "")
    name = name.strip()
    if not name:
        raise LookupError(
            f"no app name: pass app_name, or call this from a backend the gateway "
            f"spawned (it sets {APP_NAME_ENV})"
        )
    return AuditSDK(name)
