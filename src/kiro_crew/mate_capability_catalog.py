"""What a crewmate's worker has, and what it could be given -- read for the conductor.

A crewmate's conductor dispatches and its worker does the work
(:mod:`kiro_crew.mate_worker`). When a piece of work needs a tool or a skill the
worker lacks, the conductor reads this catalog (``kirocrew-mate``
``find_capabilities``) and asks the user for the addition on a card
(``request_capabilities``). The catalog answers in a fixed order:

1. what the worker already has, as :func:`kiro_crew.mate_worker.resolve_worker_spec`
   projects it for this crewmate (the worker base plus the crewmate file's
   additions, governed and restricted exactly as a worker session gets it);
2. the MCP servers and skills this gateway has loaded that the worker lacks;
3. (in the HTTP handler) MCP registry search results, through the same search
   the dashboard's MCP page runs.

It also turns the conductor's named additions into the capability operations a
``crewmate.capabilities`` card applies to the crewmate's own file
(:func:`addition_operations`). Output is data only: server transports are never
returned, because their ``env`` and headers can carry credentials.

Pure functions over specs plus blocking reads; nothing here holds state.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

# The worker spec this catalog reads; one name here, so a test can stand in for it.
from kiro_crew.mate_worker import member_worker_spec as worker_spec  # noqa: F401

logger = logging.getLogger(__name__)

#: Server-entry keys a crewmate file's capability edit does not take on a
#: transport (``mate_capabilities._apply_operations`` refuses ``autoApprove``;
#: the rest are per-scope switches the gateway's own copy carries).
_TRANSPORT_DROP = frozenset({"autoApprove", "disabled", "disabledTools"})

#: Where an addition comes from.
SOURCE_GATEWAY = "gateway"
SOURCE_REGISTRY = "registry"


class AdditionError(ValueError):
    """An addition the conductor named that cannot become a card."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _servers(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, dict)}


def worker_has(spec: dict[str, Any], catalog: dict[str, str]) -> dict[str, Any]:
    """What *spec* gives the worker: servers, what runs without asking, skills."""
    allowed = _strings(spec.get("allowedTools"))
    excluded = _strings(spec.get("excludedTools"))
    servers = []
    for name, entry in sorted(_servers(spec.get("mcpServers")).items()):
        whole = any(ref in (f"@{name}", f"@{name}/", f"@{name}/*") for ref in allowed)
        tools = sorted(
            {ref.split("/", 1)[1] for ref in allowed if ref.startswith(f"@{name}/")}
            | set(_strings(entry.get("autoApprove")))
        )
        row: dict[str, Any] = {"name": name, "runs_without_asking": "all" if whole else tools}
        off = sorted(ref.split("/", 1)[1] for ref in excluded if ref.startswith(f"@{name}/"))
        if off:
            row["switched_off"] = off
        servers.append(row)
    return {
        "servers": servers,
        "skills": sorted(_worker_skills(spec, catalog)),
        "builtin_tools_left_out": sorted(t for t in excluded if not t.startswith("@")),
    }


def _worker_skills(spec: dict[str, Any], catalog: dict[str, str]) -> set[str]:
    uris = set(_strings(spec.get("resources"))) | set(_strings(spec.get("skills")))
    return {skill for skill, uri in catalog.items() if uri in uris or skill in uris}


def gateway_lacks(spec: dict[str, Any], catalog: dict[str, str]) -> dict[str, Any]:
    """The servers and skills this gateway has loaded that *spec* lacks. Blocking."""
    have = set(_servers(spec.get("mcpServers")))
    servers = []
    try:
        from kiro_crew.mcp_discovery import list_servers

        rows = list_servers()
    except Exception:  # noqa: BLE001 - an unreadable config lists no server
        logger.warning("MCP config unreadable; the catalog lists no gateway server", exc_info=True)
        rows = []
    for row in rows:
        name = getattr(row, "name", "")
        if not isinstance(name, str) or not name or name in have:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "switched_on": getattr(row, "status", "") != "disabled",
        }
        tools = [t for t in getattr(row, "tools", None) or [] if isinstance(t, str)]
        if tools:
            entry["tools"] = sorted(tools)
        servers.append(entry)
    skills = sorted(set(catalog) - _worker_skills(spec, catalog))
    return {"servers": sorted(servers, key=lambda s: s["name"]), "skills": skills}


#: Bounds of the dispatch summary: names listed per line, characters per name.
_SUMMARY_NAMES = 12
_SUMMARY_NAME_CHARS = 48
_SUMMARY_NAME_STRIP = re.compile(r"[^A-Za-z0-9_.@/ -]+")


def _summary_names(names: list[str]) -> str:
    shown = []
    for name in names[:_SUMMARY_NAMES]:
        clean = _SUMMARY_NAME_STRIP.sub("", name).strip()[:_SUMMARY_NAME_CHARS]
        if clean:
            shown.append(clean)
    more = len(names) - _SUMMARY_NAMES
    text = ", ".join(shown) or "none"
    return f"{text} and {more} more" if more > 0 else text


def dispatch_summary(spec: dict[str, Any], catalog: dict[str, str]) -> str:
    """Two short lines for a conductor's hand-off result: what its worker can use and what it lacks.

    The same data as ``find_capabilities`` steps 1 and 2 (:func:`worker_has`,
    :func:`gateway_lacks`), reduced to server names: what the worker's MCP
    servers are, and which servers this gateway has loaded that the worker
    lacks and the user has not switched off. The registry stays on demand. Names are cut to a plain character set
    and a bounded count, so a server name cannot carry text into the result.
    Blocking (the gateway list reads the MCP config).
    """
    has = [row["name"] for row in worker_has(spec, catalog)["servers"]]
    # A server the user switched off cannot be added (``additions_to_draft``
    # refuses it), so it is not offered.
    lacks = [
        row["name"]
        for row in gateway_lacks(spec, catalog)["servers"]
        if row.get("switched_on", True)
    ]
    from kiro_crew import crewmate_prompts

    lines = [crewmate_prompts.fill("dispatch.worker_has", names=_summary_names(has))]
    if lacks:
        lines.append(crewmate_prompts.fill("dispatch.gateway_has", names=_summary_names(lacks)))
    return " ".join(lines)


def transport_for_file(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """*spec* (a gateway's server entry) as a crewmate file's capability edit takes it.

    Drops the per-scope switches a file's edit does not take (``autoApprove``,
    ``disabled``, ``disabledTools``): approvals travel as ``allowedTools``
    operations, and the user's per-tool switches reach the worker on their own
    (:func:`kiro_crew.mate_worker.globally_disabled_refs`).
    """
    if not isinstance(spec, dict):
        return None
    return {k: v for k, v in spec.items() if k not in _TRANSPORT_DROP}


def addition_operations(
    name: str,
    transport: dict[str, Any] | None,
    auto_approve: list[str],
) -> list[dict[str, Any]]:
    """The capability operations that add server *name* to a crewmate's file.

    The server's entry (``mcpServers``), its mount (``tools``) and one
    ``allowedTools`` grant per tool in *auto_approve*: exactly the tools the card
    lists as running without asking, before the governance ceiling. With no
    *transport* the entry is left as the file has it.
    """
    ops: list[dict[str, Any]] = []
    if transport is not None:
        ops.append({"section": "mcpServers", "id": name, "action": "set", "value": transport})
    ops.append({"section": "tools", "id": f"@{name}", "action": "set", "value": True})
    for tool in dict.fromkeys(auto_approve):
        ops.append(
            {"section": "allowedTools", "id": f"@{name}/{tool}", "action": "set", "value": True}
        )
    return ops


def skill_operation(skill: str) -> dict[str, Any]:
    """The capability operation that adds catalog skill *skill* to a crewmate's file."""
    return {"section": "skills", "id": skill, "action": "set", "value": True}


#: The most additions one request may name.
MAX_ADDITIONS = 8
_ORIGIN_ID_MAX = 200


def clean_origins(value: Any) -> dict[str, dict[str, str]]:
    """A ``crewmate.capabilities`` card's ``origins``, validated.

    ``{server: {"source": "gateway"}}`` or ``{server: {"source": "registry",
    "registry_id": id}}``. The gateway writes it (the handler resolving a
    conductor's additions); a card shows it only where the gateway can still
    vouch for it (:func:`origin_rows`). Raises ``ValueError`` on any other shape.
    """
    if not isinstance(value, dict) or len(value) > MAX_ADDITIONS:
        raise ValueError("'origins' maps each added server to where it comes from")
    out: dict[str, dict[str, str]] = {}
    for name, origin in value.items():
        if not isinstance(name, str) or not name or not isinstance(origin, dict):
            raise ValueError("'origins' maps each added server to where it comes from")
        source = origin.get("source")
        if source == SOURCE_GATEWAY and set(origin) == {"source"}:
            out[name] = {"source": SOURCE_GATEWAY}
            continue
        rid = origin.get("registry_id")
        if (
            source == SOURCE_REGISTRY
            and set(origin) == {"source", "registry_id"}
            and isinstance(rid, str)
            and 0 < len(rid) <= _ORIGIN_ID_MAX
        ):
            out[name] = {"source": SOURCE_REGISTRY, "registry_id": rid}
            continue
        raise ValueError("an origin is {source: gateway} or {source: registry, registry_id}")
    return out


def _granted(impact: list[dict[str, Any]]) -> tuple[dict[str, list[str]], set[str]]:
    """``(tools per server, servers granted whole)`` the preview's *impact* adds.

    A ``@server/tool`` grant names one tool; a bare ``@server`` grant is every
    tool that server has, and is kept apart so the card can say so.
    """
    granted: dict[str, list[str]] = {}
    whole: set[str] = set()
    for item in impact:
        ref = str(item.get("id") or "")
        if (
            item.get("section") in ("allowedTools", "autoApprove")
            and item.get("change") == "added"
            and ref.startswith("@")
        ):
            server, slash, tool = ref[1:].partition("/")
            if not slash:
                whole.add(server)
            elif tool:
                granted.setdefault(server, []).append(tool)
    return granted, whole


def origin_rows(
    draft: dict[str, Any],
    origins: dict[str, dict[str, str]],
    gateway_matches: set[str],
    impact: list[dict[str, Any]],
    display: Callable[[str], str],
) -> list[dict[str, Any]]:
    """The card rows that say where each added server comes from and what runs unasked.

    *gateway_matches* is the servers whose entry in the draft is the one this
    gateway runs now (re-read when the card is built). Only servers *origins*
    names get rows, so a card the gateway did not resolve keeps its own rows. A
    server is "this gateway's" only when it is in that set; a registry origin is shown for
    any other; a server with neither is defined by the card itself (its launch
    line is shown in full by the card's own spec rows). The "runs without asking"
    row lists exactly the approvals the preview kept after the governance
    ceiling (*impact*), so a grant the ceiling withholds is not on it. The
    registry id is external text, shown through the card's own *display* pass.
    """
    if not origins:
        # Not an addition the gateway resolved: the card keeps its own rows.
        return []
    added = [
        str(op.get("id") or "")
        for op in draft.get("operations") or []
        if op.get("section") == "mcpServers" and op.get("action") == "set"
    ]
    granted, whole = _granted(impact)
    rows: list[dict[str, Any]] = []
    for name in added:
        origin = origins.get(name) or {}
        if name not in origins:
            continue
        if name in gateway_matches:
            where = "this gateway's MCP servers (already loaded here)"
        elif origin.get("source") == SOURCE_REGISTRY:
            where = (
                "the MCP registry ("
                + display(origin["registry_id"])
                + "), installed into this crewmate only"
            )
        else:
            where = "defined on this card, for this crewmate only"
        rows.append({"label": f"{name}: comes from", "after": where})
        tools = granted.get(name) or []
        if name in whole:
            rows.append({"label": f"{name}: runs without asking", "after": "every tool"})
        elif tools:
            rows.append({"label": f"{name}: runs without asking", "add": sorted(tools)})
        else:
            rows.append(
                # No new per-tool grant. Not "every call asks": the crewmate's own
                # approval mode (Trust, say) still decides, so no per-call promise.
                {
                    "label": f"{name}: runs without asking",
                    "after": "no new per-tool grant; current approval settings apply",
                }
            )
    return rows


def addition_summary(
    draft: dict[str, Any],
    origins: dict[str, dict[str, str]],
    gateway_matches: set[str],
    impact: list[dict[str, Any]],
    display: Callable[[str], str],
) -> list[dict[str, Any]]:
    """One plain row per addition, for the card's main view; ``[]`` for other cards.

    Each row carries an ``addition`` the dashboard words in the reader's
    language: ``{"kind": "server", "name", "origin", "auto_approve"}`` (plus
    ``registry_id`` for a registry install, and ``auto_approve_all`` when the
    draft grants the whole server) or ``{"kind": "skill", "name"}``.
    ``origin`` follows :func:`origin_rows`: ``gateway`` only for a server this
    gateway runs as the draft has it, ``registry`` for a registry install,
    ``card`` otherwise. ``auto_approve`` is what the preview kept after the
    governance ceiling. Only a card the gateway resolved from named additions
    (*origins* non-empty) gets these rows; the raw rows then go behind Details.
    """
    if not origins:
        return []
    granted, whole = _granted(impact)
    rows: list[dict[str, Any]] = []
    for op in draft.get("operations") or []:
        section, name = op.get("section"), str(op.get("id") or "")
        if op.get("action") != "set" or not name:
            continue
        if section == "mcpServers":
            origin = origins.get(name) or {}
            addition: dict[str, Any] = {"kind": "server", "name": name}
            if name in gateway_matches:
                addition["origin"] = SOURCE_GATEWAY
            elif origin.get("source") == SOURCE_REGISTRY:
                addition["origin"] = SOURCE_REGISTRY
                addition["registry_id"] = display(origin["registry_id"])
            else:
                addition["origin"] = "card"
            addition["auto_approve"] = sorted(granted.get(name) or [])
            if name in whole:
                addition["auto_approve_all"] = True
            rows.append({"label": name, "addition": addition})
        elif section == "skills":
            rows.append({"label": name, "addition": {"kind": "skill", "name": name}})
    return rows


def only_gateway_servers(draft: dict[str, Any], gateway_matches: set[str]) -> bool:
    """Whether every server *draft* sets is one this gateway already runs as set."""
    names = [
        str(op.get("id") or "")
        for op in draft.get("operations") or []
        if op.get("section") == "mcpServers" and op.get("action") == "set"
    ]
    return all(name in gateway_matches for name in names)
