"""Always allow one MCP tool: the approval card's per-tool grant and its store.

A person clicks "Always allow this tool" on an MCP tool's approval card. The grant
is the entry ``@<server>/<tool>`` in ``hooks.auto_approve_tool_identities`` (its
own list: ``hooks.auto_approve_tools`` is the operator's, and these surfaces never
write it), which the gate's ``operator-grants`` tier (``hook_runtime/gate_tiers``)
matches against the call's VERIFIED MCP identity only, never a title. It is written to ``config.json``
through the locked, endorsed config writer and applied by the config watcher, so
the next call is answered by it without a restart, and Settings -> Security lists
and removes it (``handlers/tool_grants``).

The grant never skips Kiro Crew's own gate. It is answered by the gate's grant
tier, which runs only after every deny tier and governance (``POLICY ∩ PROFILE``)
have judged the call with its real arguments; ``hooks.confirm_tools``, the
card-turn and low-fidelity downgrades and the name-grant checks still apply on
every call. That is why the offer does not ask ``governance.may_skip_gate_now``:
that predicate decides whether a STATIC ``allowedTools`` entry may bypass the gate,
and it is false for exactly the servers a ceiling has an opinion about, which are
the servers whose calls reach this card. KAS's own ``allow_always`` persistence and
``permissions.yaml`` are never written: those stop the call reaching the gate.

Authority comes only from server-derived facts: :func:`card_fields` stamps the
verified ``@server/tool`` and a grantable flag on the pending card, and the approve
route grants exactly what that card carries, never text the client sent.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The decision the approval route takes for this grant.
ACTION = "always_allow_tool"

#: The ``hooks`` list these grants live in.
GRANT_KEY = "auto_approve_tool_identities"

#: Card fields (all strings, as every pending-card field is).
FIELD_GRANTABLE = "always_allow_tool_grantable"
FIELD_REF = "always_allow_tool_ref"
FIELD_REFUSAL = "always_allow_tool_refusal"
CARD_FIELDS = (FIELD_GRANTABLE, FIELD_REF, FIELD_REFUSAL)

#: Why a tool's grant is not offered, shown beside the control as a short reason.
#: ``asks_every_time``: an operator ``hooks.confirm_tools`` pattern covers it.
#: ``set_elsewhere``: ``config.local.json`` sets the list, so a ``config.json``
#: write would not take effect.
#: ``crew_owned``: a Kiro Crew server, an app's own server or a Connection bridge,
#: which never take a standing grant from here.
REFUSAL_ASKS_EVERY_TIME = "asks_every_time"
REFUSAL_SET_ELSEWHERE = "set_elsewhere"
REFUSAL_CREW_OWNED = "crew_owned"

#: Characters ``fnmatch`` reads as a pattern: a name holding one would be a
#: wildcard grant, never exactly this tool.
_PATTERN_CHARS = "*?["
#: Kiro Crew's own MCP servers. Their tools act on Kiro Crew itself (spawning,
#: scheduling, messaging), so they are never given a standing grant.
_CREW_SERVER_PREFIX = "kirocrew-"


def tool_ref(server: str, tool: str) -> str:
    """``@server/tool`` when both names can be granted exactly, else ``""``."""
    if not server or not tool:
        return ""
    for name in (server, tool):
        if "/" in name or any(ch in name for ch in _PATTERN_CHARS):
            return ""
        if name != name.strip() or any(ord(ch) < 32 for ch in name):
            return ""
    return f"@{server}/{tool}"


def is_tool_grant(entry: object) -> bool:
    """Whether a grant-list entry is one exact ``@server/tool`` grant."""
    if not isinstance(entry, str) or not entry.startswith("@"):
        return False
    server, sep, tool = entry[1:].partition("/")
    return bool(sep) and tool_ref(server, tool) == entry


def server_is_crew_owned(server: str) -> bool:
    """Whether *server* is one that never takes a standing per-tool grant.

    Kiro Crew's own servers (``kirocrew-*``), a server a shipped app manifest
    declares, and a server a Crew-owned Connection bridges (whose harness is
    answered once and whose gateway proxy keeps its own approvals).
    shortcut: a Connection is known only while a session holds its bridge, so a
    grant for its server can be written from the MCP tab while none does. The
    gate matches such a grant only against a verified identity, never a title,
    so it approves only a verified call of that tool;
    the card refuses it per call (:func:`event_tool_ref`), and the Connection's
    gateway proxy still asks on its own. Upgrade when Connections are known
    without a live bridge.
    """
    from kiro_crew.connections.crew_oauth_proxy import serves_connection
    from kiro_crew.hooks import _is_declared_builtin_mcp_server

    return (
        server.casefold().startswith(_CREW_SERVER_PREFIX)
        or _is_declared_builtin_mcp_server(server)
        or serves_connection(server)
    )


def event_tool_ref(event: Any) -> str:
    """The grantable ``@server/tool`` of a permission request, or ``""``.

    Only an identity the client proved (``mcp_identity_trusted``, both names from
    the harness's own frames), for an MCP call that is not a shell, whose identity
    was readable, and -- for a backend sub-agent whose arguments are missing --
    whose identity alone is verified (``child_mcp_identity_trusted``, the same
    exception the low-fidelity downgrade keeps for identity-keyed grants). Never a
    Crew-owned Connection's call (``connection_mcp_identity``) or a server
    :func:`server_is_crew_owned` names.
    """
    if not getattr(event, "mcp_identity_trusted", False):
        return ""
    if getattr(event, "is_shell", False) or getattr(event, "mcp_identity_unreadable", False):
        return ""
    if getattr(event, "connection_mcp_identity", None):
        return ""
    if getattr(event, "child_low_fidelity", False) and not getattr(
        event, "child_mcp_identity_trusted", False
    ):
        return ""
    server = str(getattr(event, "mcp_server_name", "") or "")
    if server_is_crew_owned(server):
        return ""
    return tool_ref(server, str(getattr(event, "tool_name", "") or ""))


def server_refusal(server: str) -> str:
    """Why no tool of *server* can be granted now, or ``""`` (blocking I/O).

    Per server, because every refusal is: ``confirm_tools`` is judged for a server
    whose tool names are not known (``governance._operator_requires_confirmation``).
    """
    from kiro_crew.config.loader import overlay_pins
    from kiro_crew.platform.governance import _operator_requires_confirmation

    if server_is_crew_owned(server):
        return REFUSAL_CREW_OWNED
    if _operator_requires_confirmation(f"@{server}/"):
        return REFUSAL_ASKS_EVERY_TIME
    if overlay_pins("hooks", GRANT_KEY):
        return REFUSAL_SET_ELSEWHERE
    return ""


def refusal_for(ref: str) -> str:
    """Why the exact grant *ref* cannot be written now, or ``""`` (blocking I/O)."""
    from kiro_crew.platform.governance import _operator_requires_confirmation

    server = ref[1:].partition("/")[0]
    refusal = server_refusal(server)
    if refusal:
        return refusal
    if _operator_requires_confirmation(ref):
        return REFUSAL_ASKS_EVERY_TIME
    return ""


def card_fields(event: Any) -> dict[str, str]:
    """The pending card's grant fields for *event* (blocking I/O: run off the loop).

    Empty when the call has no grantable identity, so the option is not shown.
    """
    ref = event_tool_ref(event)
    if not ref:
        return {}
    refusal = refusal_for(ref)
    if refusal:
        return {FIELD_REFUSAL: refusal}
    return {FIELD_GRANTABLE: "1", FIELD_REF: ref}


class GrantRefused(Exception):
    """The grant cannot be written; ``code`` is the machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _grant_list(data: dict) -> list:
    hooks = data.get("hooks")
    if hooks is None:
        hooks = data["hooks"] = {}
    if not isinstance(hooks, dict):
        raise GrantRefused("config_unreadable", "the hooks section of config.json is not an object")
    entries = hooks.setdefault(GRANT_KEY, [])
    if not isinstance(entries, list):
        raise GrantRefused("config_unreadable", f"hooks.{GRANT_KEY} is not a list")
    return entries


def write_grant(ref: str, *, add: bool) -> None:
    """Add or remove the exact entry *ref* in ``config.json`` (blocking I/O).

    Under the config file's lock, through ``update_config_locked``, which endorses
    the bytes it publishes; a file with a pending outside edit is refused by that
    writer (``PendingEditBlocksSave``). Run under the dashboard's config lock.
    """
    if not is_tool_grant(ref):
        raise GrantRefused("invalid_ref", "not a single MCP tool")
    from kiro_crew.config.loader import config_path, update_config_locked

    def _mutate(data: dict) -> dict | None:
        entries = _grant_list(data)
        if add:
            if any(isinstance(e, str) and e.lower() == ref.lower() for e in entries):
                return None
            entries.append(ref)
        else:
            kept = [e for e in entries if not (isinstance(e, str) and e.lower() == ref.lower())]
            if len(kept) == len(entries):
                return None
            entries[:] = kept
        return data

    update_config_locked(config_path(), mutate=_mutate)


def list_grants() -> list[str]:
    """The exact ``@server/tool`` entries of ``hooks.auto_approve_tool_identities`` in force."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = live.snapshot() or KiroCrewConfig.load()
    hooks = cfg.hooks if isinstance(cfg.hooks, dict) else {}
    entries = hooks.get(GRANT_KEY)
    if not isinstance(entries, list):
        return []
    return list(dict.fromkeys(e for e in entries if is_tool_grant(e)))


def _audit(
    request: Any, operation: str, outcome: str, *, resources: str = "", error: str = ""
) -> None:
    """One SEL record for a grant write or its refusal. Best-effort: never changes the answer."""
    from kiro_crew.sel import sel

    try:
        sel().log_api_access(
            caller=str(request.get("user") or "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
            error=error,
        )
    except Exception:
        logger.warning("SEL audit failed for %s", operation, exc_info=True)


def refuse(
    request: Any, operation: str, code: str, message: str, status: int, *, ref: str = ""
) -> Any:
    """Audit a refused grant change (``denied``, ``error=code``), then answer it."""
    from aiohttp import web

    _audit(request, operation, "denied", resources=ref, error=code)
    return web.json_response({"error": message, "code": code}, status=status)


async def save_grant(request: Any, ref: str, *, add: bool, operation: str) -> Any:
    """Write or drop *ref*, apply it to the running gate, and audit it.

    Returns ``None`` on success, else the refusal response. Shared by the card's
    decision and the Settings list so both write the same way.
    """
    import asyncio

    from aiohttp import web

    from kiro_crew.config.loader import ConfigReadError, PendingEditBlocksSave
    from kiro_crew.dashboard.handlers.agents import _get_config_lock
    from kiro_crew.dashboard.handlers.core import _hot_apply_after_write

    async with _get_config_lock():
        try:
            await asyncio.to_thread(write_grant, ref, add=add)
        except PendingEditBlocksSave as exc:
            return refuse(request, operation, exc.code, str(exc), 409, ref=ref)
        except GrantRefused as exc:
            return refuse(request, operation, exc.code, str(exc), 409, ref=ref)
        except (ConfigReadError, OSError):
            _audit(request, operation, "error", resources=ref, error="write_failed")
            return web.json_response(
                {"error": "failed to write config file", "code": "write_failed"}, status=500
            )
    # The gate reads its patterns from the watcher's reload; wait for that cycle
    # so the next call is judged under the new list.
    await _hot_apply_after_write()
    _audit(request, operation, "success", resources=ref)
    return None


async def grant_from_card(
    request: Any,
    *,
    fut: Any,
    card: Callable[[str], str],
    body: dict,
    from_app: bool,
    delegated: bool,
    strict_native: bool,
) -> Any:
    """The card's "Always allow this tool" decision, before the call is approved.

    Only the dashboard owner's own browser, deciding this slot's own native card
    (never an app token, a Crew Mode conductor's relay, or an agent caller), and
    only for the ``@server/tool`` the pending card itself carries (*card* reads one
    field of that card). The client's
    ``pattern`` is proof the card it clicked names the same tool; it never
    supplies the grant. Returns ``None`` when the grant is written, else the
    refusal, and nothing has changed.
    """
    op = "tool_approval:" + ACTION
    if from_app or delegated or not strict_native:
        return refuse(
            request,
            op,
            "always_allow_not_here",
            "this approval cannot record a standing grant",
            403,
        )
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, op)
    if denied is not None:
        return denied
    if not fut or fut.done():
        return refuse(request, op, "approval_not_pending", "no pending approval", 404)
    grantable, card_ref = card(FIELD_GRANTABLE), card(FIELD_REF)
    if grantable != "1" or not is_tool_grant(card_ref):
        return refuse(request, op, "pattern_underivable", "this tool cannot be always allowed", 400)
    if body.get("pattern") != card_ref:
        return refuse(
            request, op, "approval_superseded", "the approval card changed", 409, ref=card_ref
        )
    import asyncio

    refusal = await asyncio.to_thread(refusal_for, card_ref)
    if refusal:
        return refuse(
            request, op, refusal, "this tool cannot be always allowed now", 409, ref=card_ref
        )
    return await save_grant(request, card_ref, add=True, operation="security.tool_grants.add")
