"""Which enabled app turns on a core route that exists for an app's page.

Some host routes serve only what an app's page does: the folder scaffold's scan and
create are such a pair. A route like that must not answer on a host where no app
that uses it is turned on, and core must not decide that by naming the app, which
can leave this repository and is not the only app that may ever call the route. So
the route asks :func:`core_route_grant` with its own path and namespace, and the
answer is read from what the installed apps declare.

The route is on while an enabled app holds an approved ``permissions.api`` entry
that admits its path, and the owner's enable of it is recorded. That is the grant the
app-token check honours (:mod:`kiro_crew.dashboard.token_auth_app_scope`): the live
manifest's entries, less the ones an update added and the owner has not approved yet
(:func:`~kiro_crew.apps.manager.staged_app_grants`), matched with the same matcher,
and honoured only while the sealed owner-enable marker stands
(:func:`~kiro_crew.apps.host_exec_grants.host_exec_granted`).

The marker, not the record, is the authority. ``apps/<name>/installed.json`` is
writable from the sandbox, and an install stores the app's approved entries while it
leaves the app off, so a record that reads enabled without the marker holds nothing.
Only the owner's enable or registration writes the marker, and every disable and
uninstall removes it. So declaring the entry reaches nothing on its own: an install is
approved at the owner's enable, and an entry an update adds waits on the app's page for
the owner, as every other new ``permissions.api`` entry does.

One rule is narrower than the token check: only an entry at or under the route's
own namespace counts. A broad entry (``/api``, ``/api/*``, ``*``) admits the path for
that app's own token, but it names no feature, so it does not turn one on for the
person.

Every call reads the records, the markers, the manifests and the approved sets fresh,
so a disable, an uninstall or an edit takes effect on the next request. Blocking disk
I/O: callers on the event loop offload it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kiro_crew.apps.host_exec_grants import host_exec_granted
from kiro_crew.apps.manager import (
    _recorded_app_names,
    apps_dir,
    get_app_manifest,
    is_app_enabled,
    staged_app_grants,
)
from kiro_crew.dashboard.token_auth_app_scope import EXACT_API_ENTRY_MARK, _api_pattern_matches

logger = logging.getLogger(__name__)

#: The grant family the route reads: the app's ``permissions.api`` entries.
GRANT_FAMILY = "api"

_HOLDS = "holds"
_DISABLED = "disabled"
_UNRECORDED = "unrecorded"


@dataclass(frozen=True)
class RouteGrant:
    """What turns a core route on, or which apps would.

    ``holder`` is the enabled app whose approved entry admits the path, ``""`` when
    there is none. ``disabled_declarers`` names, in app-name order, the installed apps
    that declare such an entry but are turned off, and ``unrecorded_declarers`` the ones
    whose record reads enabled and whose entry is approved but whose owner-enable marker
    is missing. Both only word a refusal.
    """

    holder: str = ""
    disabled_declarers: tuple[str, ...] = ()
    unrecorded_declarers: tuple[str, ...] = ()


def _within(path: str, namespace: str) -> bool:
    return path == namespace or path.startswith(namespace + "/")


def _entry_base(entry: str) -> str:
    """The literal path an entry starts from, without its exact mark or wildcard."""
    if entry.startswith(EXACT_API_ENTRY_MARK):
        return entry[len(EXACT_API_ENTRY_MARK) :]
    if entry.endswith("/*"):
        return entry[:-2]
    if entry.endswith("*"):
        return entry[:-1]
    return entry


def entry_grants_route(entry: str, path: str, namespace: str) -> bool:
    """Whether a ``permissions.api`` *entry* turns on the route at *path*.

    The entry must start at or under *namespace* and admit *path* by the token
    check's own matcher.
    """
    entry = entry.strip()
    return (
        bool(entry) and _within(_entry_base(entry), namespace) and _api_pattern_matches(entry, path)
    )


def _app_answer(name: str, path: str, namespace: str) -> str:
    """``_HOLDS``, ``_DISABLED``, ``_UNRECORDED`` or ``""`` for one installed app.

    The manifest is read before ``enabled``, as
    :func:`~kiro_crew.apps.permissions.session_approval_denial_cause` documents, so
    the enabled state judged is at least as new as the manifest it pairs with. The
    owner-enable marker is read next and the approved set last, through the same read
    the token check makes; an approved entry without the marker only words the
    refusal.
    """
    manifest = get_app_manifest(name)
    if manifest is None:
        return ""
    declared = [e for e in manifest.permissions.api if entry_grants_route(e, path, namespace)]
    if not declared:
        return ""
    if not is_app_enabled(name):
        return _DISABLED
    recorded = host_exec_granted(name)
    if not staged_app_grants(name, GRANT_FAMILY, lambda: declared):
        return ""
    return _HOLDS if recorded else _UNRECORDED


def core_route_grant(path: str, namespace: str) -> RouteGrant:
    """Who turns on the core route at *path*, inside its own *namespace*.

    Raises:
        ValueError: if *path* is not *namespace* or a path under it.
    """
    if not namespace.startswith("/") or not _within(path, namespace):
        raise ValueError(f"route {path!r} is not inside its namespace {namespace!r}")
    try:
        names = _recorded_app_names(apps_dir())
    except OSError:
        logger.warning("Could not list the installed apps; %s stays off", path, exc_info=True)
        return RouteGrant()
    disabled: list[str] = []
    unrecorded: list[str] = []
    for name in names:
        try:
            answer = _app_answer(name, path, namespace)
        except Exception:  # noqa: BLE001 - an unreadable app grants nothing
            logger.warning("App %s: could not read its grant for %s", name, path, exc_info=True)
            continue
        if answer == _HOLDS:
            return RouteGrant(holder=name)
        if answer == _DISABLED:
            disabled.append(name)
        elif answer == _UNRECORDED:
            unrecorded.append(name)
    return RouteGrant(disabled_declarers=tuple(disabled), unrecorded_declarers=tuple(unrecorded))
