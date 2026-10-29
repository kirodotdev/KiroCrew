"""Built-in apps the gateway skipped at boot because their files could not be read.

``dashboard.routes.system.register`` imports each built-in app and skips one whose
import raises :class:`OSError` (an endpoint security agent denying a path, a damaged
install). It records the skip here, on the aiohttp application, so ``GET /api/apps``
and ``GET /api/apps/{name}`` can tell the dashboard the app is unavailable and why,
instead of listing it as a working app whose page then fails with no reason.

The record lives on the application, not in a module global: it describes one
gateway's boot, and a test that builds a second application starts with none.
"""

from __future__ import annotations

from typing import Any

from aiohttp import web

from kiro_crew.security import redact

#: Installed-app name of a skipped built-in -> the reason it was skipped, as ``"<ExceptionType>: <message>"``.
UNAVAILABLE_BUILTINS_KEY: web.AppKey[dict[str, str]] = web.AppKey("unavailable_builtin_apps", dict)


def builtin_app_name(module_name: str) -> str:
    """The installed-app name of the built-in shipped as package *module_name*.

    A built-in's package is its manifest name with ``-`` spelled ``_``
    (``code_review_sage`` ships ``code-review-sage``). Derived, not read from its
    ``app.json``: the skip this feeds is for an app whose files could not be read.
    ``test_app_store_unavailable_builtin`` pins the rule for every shipped built-in.
    """
    return module_name.replace("_", "-")


def record_unavailable_builtin(app: web.Application, module_name: str, exc: BaseException) -> None:
    """Remember that the built-in in package *module_name* was skipped at boot.

    Keyed by the installed-app name, which is what ``GET /api/apps`` records carry.
    """
    skipped = app.get(UNAVAILABLE_BUILTINS_KEY)
    if skipped is None:
        skipped = {}
        app[UNAVAILABLE_BUILTINS_KEY] = skipped
    skipped[builtin_app_name(module_name)] = f"{type(exc).__name__}: {exc}"


def unavailable_builtins(app: web.Application) -> dict[str, str]:
    """The built-in apps skipped at boot on *app*, by name, with their reasons."""
    return dict(app.get(UNAVAILABLE_BUILTINS_KEY) or {})


def mark_unavailable(record: dict[str, Any], skipped: dict[str, str]) -> dict[str, Any]:
    """Stamp ``unavailable`` on an installed-app *record* that was skipped at boot.

    Only a built-in record is stamped: the skip list names built-in apps, and an
    external app that happens to share a name is a different app. The reason is
    redacted, as every other warning this endpoint returns is. A record that loaded
    is returned unchanged, so a working app's payload is the same as before.
    """
    reason = skipped.get(str(record.get("name", "")))
    if reason is not None and record.get("origin") == "builtin":
        record["unavailable"] = {"reason": redact(reason)}
    return record
