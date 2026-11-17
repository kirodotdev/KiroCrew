"""The tool gate's own-server trust for a former built-in adopted from its pin.

The PreToolUse gate's ``app-own-server`` grant auto-approves an app's call to its
OWN declared MCP server only when the app is in the gate's first-party set and the
``<app>:<server>`` is in its declared-server set. Boot warms both from
``execution.builtin_app_names`` and ``execution.builtin_app_mcp_servers``, which keep
only BUILTIN-OWNED records, so a built-in adopted onto its own repository would lose
the grant and its calls would start asking where they did not before.
:func:`adopted_app_own_mcp_servers` names the servers that keep it; each refresh
adds them, and their apps, to the same two sets, so the gate's rule, its tier and
``POLICY ∩ PROFILE`` ahead of it are unchanged.

An ``<app>:<server>`` is named only when every input agrees:

- the wheel pins *app* (``adoption-pins.json``, immutable), so an app no pin
  names, every third-party app included, is never named;
- the active record is adopted from the pin's repository
  (``builtin_adoption.record_is_adopted_from``), so a record installed from any
  other source, under the same name or not, is not;
- the operator's execution grant for *app* is bound to that same repository and
  covers the installed code (``execution.trusted_app_repository`` and
  ``execution.repository_bound_grant_denied``);
- the wheel built-in's shipped manifest declares the server, and the installed
  manifest declares it as a stdio server started from the adopted tree within that
  declaration (``builtin_adoption_mcp.carried_server``, the check adoption itself
  ran), so a renamed server, a ``-m`` module, a PATH binary or a widened entry is
  not.

The first three are :func:`adopted_under_pin_trust`, which ``app_agent_tools`` also
asks before it lists an adopted record's declared agent tools under the
auto-approved ``kirocrew-core`` server, with the wheel copy's ``agentTools`` as that
listing's ceiling the way the wheel's ``mcpServers`` is this grant's.

A built-in's grant rests on code the wheel ships. An adopted record's tree lives in
the data home instead, so its grant rests on the repository trust above: the
operator's grant to the repository the wheel pins, the same grant under which the
record's own code is admitted to run at all.

Filesystem and config reads, so :func:`publish_gate_app_trust` and
:func:`refresh_gate_app_trust` run this off the event loop, like the builtin sets,
at boot and after every app lifecycle or trust change. A failure drops the app it
concerns (fail-closed) and never raises.
Nothing here names an app. Owning spec: app-kit-platform.md § Former built-in
adoption.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def adopted_app_own_mcp_servers() -> frozenset[str]:
    """``<app>:<server>`` for every server an adopted former built-in keeps the grant on."""
    try:
        from kiro_crew.apps.builtin_adoption_pins import adoption_pin_names
        from kiro_crew.apps.execution import _shipped_builtin_apps

        pinned = adoption_pin_names()
        if not pinned:
            return frozenset()
        wheel_roots: dict[str, Path] = {}
        for root, name in _shipped_builtin_apps():
            if name in pinned:
                wheel_roots.setdefault(name, root)
    except Exception:  # noqa: BLE001 - unknown provenance trusts no adopted app
        logger.warning("adopted app own-server trust unreadable", exc_info=True)
        return frozenset()
    servers: set[str] = set()
    for name in sorted(wheel_roots):
        try:
            servers.update(_kept_servers(name, wheel_roots[name]))
        except Exception:  # noqa: BLE001 - one unreadable app only costs its own prompts
            logger.warning("app %s: adopted own-server trust unreadable", name, exc_info=True)
    return frozenset(servers)


def adopted_under_pin_trust(name: str) -> bool:
    """Whether *name*'s active record is the former built-in adopted from its pin.

    True only when the wheel pins *name*, the active record is adopted from the pin's
    repository, and the operator's execution grant for *name* is bound to that same
    repository and covers the installed code: the first three inputs of the module
    docstring's list. Every other trust that follows an adopted former built-in
    (this module's own-server grant, ``app_agent_tools``' listing) is this
    predicate plus its own ceiling read from the wheel copy. Reads config and
    ``installed.json``; never raises (an unreadable input is False).
    """
    try:
        from kiro_crew.apps.builtin_adoption import _same_target, record_is_adopted_from
        from kiro_crew.apps.builtin_adoption_pins import adoption_pin
        from kiro_crew.apps.execution import (
            repository_bound_grant_denied,
            trusted_app_repository,
        )
        from kiro_crew.apps.manager import _read_installed

        pin = adoption_pin(name)
        if pin is None or not record_is_adopted_from(_read_installed(name), pin.repository):
            return False
        if not _same_target(trusted_app_repository(name), pin.repository):
            return False
        return repository_bound_grant_denied(name) is None
    except Exception:  # noqa: BLE001 - unknown provenance trusts no adopted app
        logger.warning("app %s: adopted-record trust unreadable", name, exc_info=True)
        return False


def _kept_servers(name: str, wheel_root: Path) -> list[str]:
    from kiro_crew.apps.builtin_adoption_mcp import carried_server
    from kiro_crew.apps.manager import app_dir
    from kiro_crew.apps.manifest import AppManifest

    if not adopted_under_pin_trust(name):
        return []
    shipped = AppManifest.from_json_file(wheel_root / "app.json")
    tree_root = app_dir(name)
    installed = AppManifest.from_json_file(tree_root / "app.json")
    if shipped.name != name or installed.name != name:
        return []
    return [
        f"{name}:{server}"
        for server, cfg in installed.mcpServers.items()
        if isinstance(server, str)
        and server
        and carried_server(server, cfg, tree_root, shipped.mcpServers)
    ]


# One refresh publishes at a time, so a slow one that read the disk earlier can
# never overwrite the sets a later one published.
_publish_lock = threading.Lock()
# Set by the boot warm. Only the full gateway warms, and only it runs the hook
# reconciler whose tick re-reads changes made by other processes; a process that
# never warmed (the ``--slack-only`` gateway) keeps its sets empty, so own-server
# calls ask there instead of holding a grant nothing would re-read.
_warmed = False


def publish_gate_app_trust() -> None:
    """Fill the gate's three ``app-own-server`` sets at boot; later refreshes follow.

    Boot runs this once. The gateway then runs :func:`refresh_gate_app_trust` after
    every app lifecycle or trust change (``hidden_sessions.request_hiding_refresh``'s
    fan-in) and on the hook reconciler's tick, so after an install, uninstall,
    replacement or adoption the grant follows the records on disk.
    """
    global _warmed
    _warmed = True
    _publish()


def refresh_gate_app_trust() -> None:
    """Re-read the sets after a change, in a process whose boot warm filled them.

    shortcut: a change made by another process (the CLI, a hand edit of a tree) is
    seen on the next reconciler tick (``hook_reconcile.POLL_INTERVAL_SECS`` plus the
    reconcile pass before it, about 75s at worst), not at once.
    """
    if _warmed:
        _publish()


def _publish() -> None:
    """Recompute the three sets from disk and install them.

    The names go in first: the grant needs the app in that set, so a revoke takes
    effect before the other two change. Blocking filesystem and config reads: call
    it off the event loop. A failure publishes empty sets (fail-closed: own-server
    calls ask), never the old ones.
    """
    from kiro_crew.hooks import (
        set_builtin_app_agents,
        set_builtin_app_mcp_servers,
        set_builtin_app_names,
    )

    with _publish_lock:
        try:
            from kiro_crew.apps.execution import (
                builtin_app_agents,
                builtin_app_mcp_servers,
                builtin_app_names,
            )

            adopted = adopted_app_own_mcp_servers()
            # An app name holds no ":", so the key's first segment is its app.
            adopted_apps = frozenset(key.partition(":")[0] for key in adopted)
            names = builtin_app_names() | adopted_apps
            servers = builtin_app_mcp_servers() | adopted
            # Agent -> owning app, so a builtin whose UI is not an app iframe
            # (empty Slot._app) can still auto-approve calls to its OWN server.
            agents = builtin_app_agents(adopted_apps)
        except Exception:  # noqa: BLE001 - a failed refresh only costs prompts
            logger.warning("Failed to refresh the gate's builtin app sets", exc_info=True)
            names, servers, agents = frozenset(), frozenset(), {}
        set_builtin_app_names(names)
        set_builtin_app_mcp_servers(servers)
        set_builtin_app_agents(agents)
