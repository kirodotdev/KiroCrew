"""Session identity for builtin app MCP servers on the KAS backend.

A builtin app server whose shipped manifest runs the host CLI (``mochi:mochi``)
is Kiro Crew's own code: its ``tools/call`` reads the session's tool policy
behind the same attestation as the managed ``kirocrew-*`` servers. On kiro-cli
:func:`~kiro_crew.acp.session_mcp.kiro_control_plane_servers` re-declares such a
server per session with the signed session token. KAS never reaches that path,
and :func:`~kiro_crew.acp.kas_agents.hoist_servers_for_session` tokens only the
managed names, so on KAS the app server launched with no token and refused every
call as ``identity_unattestable``.

This module supplies the KAS half. :func:`resolve_app_identity_launches` picks,
off the event loop, the app servers the active agent declares that may get the
element: the launch is re-derived from the IMMUTABLE shipped manifest
(:func:`~kiro_crew.acp.session_mcp.builtin_app_identity_launches`), and a server
a settings file restricts keeps its native declaration, the verdict the kiro-cli
mount reaches through the same predicate. :func:`tokened_app_element` then builds
the session-level element the hoist sends, so the token only ever reaches the
gateway's own interpreter running the manifest's args, and travels in the
session's wire array, never in a spec on disk.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Env keys a tokened hoisted element (managed or app) keeps from its
#: projection. Both are set from the gateway's own values, never from the spec.
TOKENED_ENV_KEYS_KEPT = ("KIROCREW_PORT", "KIROCREW_SESSION_KEY")

#: Entry keys whose listed value restricts nothing, so dropping them before the
#: hoist's key check changes no behaviour.
_NOOP_RESTRICTIONS: Mapping[str, Any] = {"disabled": False, "disabledTools": []}


def gateway_bound_port() -> str:
    """The gateway's live listener port, or ``""`` when it is not a valid port.

    Native MCP children on KAS do not inherit the gateway's environment, so the
    port is taken from the gateway itself, never from an editable spec.
    """
    # Deferred: launch imports the transport stack this module must not load.
    from kiro_crew.acp.launch import _well_formed_port

    port = os.environ.get("KIROCREW_BOUND_PORT", "")
    return port if _well_formed_port(port) else ""


def without_noop_restrictions(entry: Any) -> Any:
    """*entry* without keys that restrict nothing (``disabled: false``, ``disabledTools: []``).

    A shipped app manifest declares ``disabled: false``, which the hoist's key
    check would otherwise read as a restriction and leave the server untokened.
    Only an exact no-op value is dropped; any other value stays and keeps the
    entry out of the hoist. A non-dict is returned as is.
    """
    if not isinstance(entry, dict):
        return entry
    return {
        k: v
        for k, v in entry.items()
        if not (k in _NOOP_RESTRICTIONS and v == _NOOP_RESTRICTIONS[k])
    }


def resolve_app_identity_launches(
    custom_agents: list[dict[str, Any]] | None,
    agent_id: str,
    work_dir: str | Path | None,
    managed_names: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """The shipped launch for each builtin app server the ACTIVE agent declares.

    Blocking (manifest and settings reads): call it off the event loop and hand
    the result to the hoist. A name a settings file restricts is left out, and so
    is every name when a settings file cannot be read, because a restriction that
    cannot be read must stay authoritative. Left out means the server keeps its
    native declaration, exactly as before this element existed.
    """
    declared: Mapping[str, Any] = {}
    for descriptor in custom_agents or ():
        if isinstance(descriptor, dict) and descriptor.get("id") == agent_id:
            raw = descriptor.get("mcpServers")
            declared = raw if isinstance(raw, dict) else {}
            break
    names = [name for name in declared if name not in managed_names]
    if not names:
        return {}
    from kiro_crew.acp.session_mcp import (
        NativeSettingsUnreadable,
        builtin_app_identity_launches,
        native_mount_withholding,
        native_settings_sources,
    )

    launches = builtin_app_identity_launches()
    wanted = {name: launches[name] for name in names if name in launches}
    if not wanted:
        return {}
    try:
        settings = native_settings_sources(work_dir)
    except NativeSettingsUnreadable:
        logger.warning(
            "KAS: MCP settings unreadable; builtin app servers %s get no session"
            " identity element, so their calls will refuse identity_unattestable",
            sorted(wanted),
        )
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, launch in wanted.items():
        withheld = native_mount_withholding(name, declared[name], settings)
        if withheld is not None:
            logger.info("KAS: %s", withheld.explain("its session identity"))
            continue
        out[name] = launch
    return out


def tokened_app_element(
    element: dict[str, Any],
    launch: Mapping[str, Any],
    session_token: str,
    attach: Callable[[list[dict[str, Any]], str], list[dict[str, Any]]],
) -> dict[str, Any]:
    """*element* relaunched as the shipped app launch and stamped with the token.

    The command and args come from *launch* alone, so a spec that renames
    another program ``mochi:mochi`` cannot receive the token. The env is the
    gateway's port and session key, then the pinned launch env (``PYTHONPATH``,
    ``KIROCREW_HOME``); nothing else from the projection survives.
    """
    raw_env = element.get("env")
    pairs: list[Any] = raw_env if isinstance(raw_env, list) else []
    env = {
        str(pair["name"]): str(pair.get("value", ""))
        for pair in pairs
        if isinstance(pair, dict) and pair.get("name") in TOKENED_ENV_KEYS_KEPT
    }
    port = gateway_bound_port()
    if port:
        env.setdefault("KIROCREW_PORT", port)
    env.update({str(k): str(v) for k, v in (launch.get("env") or {}).items()})
    relaunched = {
        "name": element["name"],
        "command": str(launch["command"]),
        "args": [str(a) for a in launch.get("args") or []],
        "env": [{"name": k, "value": v} for k, v in env.items()],
    }
    (stamped,) = attach([relaunched], session_token)
    return stamped
