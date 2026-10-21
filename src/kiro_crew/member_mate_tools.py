"""The crewmate's own-record tools (``kirocrew-mate``) on a crewmate's spec.

Two questions about that one server, answered in one place:

* :func:`spec_has_mate_tools` / :func:`binding_has_mate_tools` -- whether a
  crewmate's sessions load the server, so the text Crew writes for it (its
  identity, its first welcome, the goal nudges) names the tools only when they
  are there, and points the user at the Goals tab when they are not.
* :func:`grant_mate_tools_to_new_member` -- the creation-time outcome for the
  server. A crewmate on its own file gets ``kirocrew-mate`` from the gateway
  (its conductor and its worker base both declare it), so the grant writes
  nothing to that file and answers ``platform_provided``. A platform server is
  never written into a crewmate's file by creation; an entry that names one
  with no transport is dropped wherever the file is written or read
  (:func:`kiro_crew.mate_worker.drop_partial_platform_servers`).

Blocking IO throughout; callers run it off the event loop.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MATE_SERVER = "kirocrew-mate"
_MATE_REF = f"@{MATE_SERVER}"
# A spec file stem: no separator, no leading dot, so it cannot leave its directory.
_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")

# Outcomes of :func:`grant_mate_tools_to_new_member`; none of them writes anything.
GRANTED = "granted"
TEMPLATE_HAS_IT = "template_has_it"
TEMPLATE_MUTES_IT = "template_mutes_it"
DEFAULT_TEMPLATE = "default_template"
NOT_A_MEMBER = "not_a_member"
ALREADY_ENROLLED = "already_enrolled"
POLICY_WITHHOLDS = "policy_withholds"
SERVER_UNAVAILABLE = "server_unavailable"
BACKEND_UNSUPPORTED = "backend_unsupported"
MATE_FILE_MISSING = "mate_file_missing"
PLATFORM_PROVIDED = "platform_provided"
FAILED = "failed"


def _digest(value: Any) -> str:
    """Stable digest of a spec, to tell a no-op edit from a real one."""
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _referenced(tools: Any) -> bool:
    """Whether a ``tools`` list mounts the server: its ref, one of its tools, or ``*``."""
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(t, str) and (t in ("*", _MATE_REF) or t.startswith(_MATE_REF + "/"))
        for t in tools
    )


def _server_entry(spec: Any) -> Any:
    servers = spec.get("mcpServers") if isinstance(spec, dict) else None
    return servers.get(MATE_SERVER) if isinstance(servers, dict) else None


def spec_has_mate_tools(spec: Any) -> bool:
    """Whether *spec* launches ``kirocrew-mate`` and mounts its tools."""
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted

    entry = _server_entry(spec)
    if not isinstance(entry, dict) or mcp_entry_is_muted(entry):
        return False
    return _referenced(spec.get("tools"))


def _read_template(name: str, workspace: str) -> dict | None:
    """The spec file named *name* a session in *workspace* runs, project scope first.

    Only the exact ``<name>.json`` / ``<name>.md`` candidates are read, never a
    scan for a declared ``name``: this answers a wording question and must not
    resolve a different template than the session's own reader would. A
    candidate that exists but cannot be read answers ``None`` rather than
    falling through to another scope.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.config.loader import default_project_dir
    from kiro_crew.config.paths import project_agents_dir

    if not _SAFE_NAME.fullmatch(name):
        return None
    project = default_project_dir(workspace)
    roots = [project_agents_dir(project)] if project else []
    roots.append(agent_mod.kiro_agents_dir_path())
    for root in roots:
        for suffix in (".json", ".md"):
            path = root / f"{name}{suffix}"
            if path.is_file() or path.is_symlink():
                if not agent_mod._spec_path_is_safe(path, root):
                    return None
                return agent_mod._read_spec_capped(path)
    return None


def binding_has_mate_tools(binding: Any) -> bool:
    """Whether the crewmate *binding* (a ``config.agents`` row) has the mate tools.

    Read from the spec its sessions run (its private copy once enrolled). A spec
    that cannot be read answers True, keeping the wording every crewmate had
    before this check existed: the question only chooses text.
    """
    name = getattr(binding, "kiro_agent", "")
    if not isinstance(name, str) or not name:
        return True
    try:
        spec = _read_template(name, getattr(binding, "workspace", "") or "")
    except Exception:
        logger.debug("spec for %r unreadable; keeping the mate wording", name, exc_info=True)
        return True
    return True if spec is None else spec_has_mate_tools(spec)


def member_has_mate_tools(member: str) -> bool:
    """:func:`binding_has_mate_tools` for the configured crewmate *member*."""
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        binding = KiroCrewConfig.load().agents.get(member)
    except Exception:
        logger.debug("config unreadable; keeping the mate wording", exc_info=True)
        return True
    return True if binding is None else binding_has_mate_tools(binding)


def _row_ids(view: dict, section: str) -> dict[str, dict]:
    return {
        row["id"]: row
        for row in view.get("rows", [])
        if row.get("section") == section and row.get("present")
    }


def _covered(grant: str, allowed: dict[str, dict]) -> bool:
    return any(
        ref == grant or fnmatch.fnmatchcase(grant, ref) or grant.startswith(ref + "/")
        for ref in allowed
    )


def _operations(view: dict) -> list[dict] | str:
    """The capability operations that give the member the mate set, or an outcome."""
    from kiro_crew import agent as agent_mod

    servers = _row_ids(view, "mcpServers")
    tools = _row_ids(view, "tools")
    entry = servers.get(MATE_SERVER)
    referenced = _referenced(list(tools))
    if entry is not None:
        value = entry.get("value")
        if isinstance(value, dict) and value.get("disabled", False) is not False:
            return TEMPLATE_MUTES_IT
        if referenced:
            return TEMPLATE_HAS_IT
    ops: list[dict] = []
    if entry is None:
        ops.append(
            {
                "section": "mcpServers",
                "id": MATE_SERVER,
                "action": "set",
                "value": {"disabled": False},
            }
        )
    if not referenced:
        ops.append({"section": "tools", "id": _MATE_REF, "action": "set", "value": True})
    allowed = _row_ids(view, "allowedTools")
    ops.extend(
        {"section": "allowedTools", "id": grant, "action": "set", "value": True}
        for grant in agent_mod._MATE_AUTO_GRANTS
        if not _covered(grant, allowed)
    )
    return ops


def _policy_permits(member: str) -> bool:
    """Whether POLICY ∩ PROFILE lets the member's sessions reach the server at all.

    Asked with a sentinel tool, the way the auto-approve writers probe a server,
    so a whole-server rule and a wildcard both answer. Fails closed: a server the
    ceiling withholds is never added back by this step.
    """
    from kiro_crew.platform.governance_profiles import governance_permits

    decision = governance_permits(
        "mcp", f"{_MATE_REF}/probe", agent=member, log_warning=False, fail_closed=True
    )
    return bool(getattr(decision, "permitted", False))


def _default_catalog(project: str) -> dict[str, str]:
    # The Capabilities pane's own catalog, so the saved baseline classifies skills
    # exactly as the pane later reads them and shows no phantom Parent changes.
    from kiro_crew.dashboard.handlers.agent_capabilities import _catalog

    return _catalog(project)


def grant_mate_tools_to_new_member(
    member: str,
    *,
    catalog: Callable[[str], dict[str, str]] | None = None,
) -> str:
    """The mate-tools outcome for the just-created crewmate *member*. Never raises.

    ``platform_provided`` for a crewmate on its own file: the gateway supplies
    the server, so nothing is written. ``mate_file_missing`` when the eager copy
    did not land, ``default_template`` and ``not_a_member`` as named.
    """
    try:
        return _grant(member, catalog or _default_catalog)
    except Exception:
        logger.warning("mate tools were not added to new crewmate %r", member, exc_info=True)
        return FAILED


def _grant(member: str, catalog: Callable[[str], dict[str, str]]) -> str:
    """The outcome for crewmate *member*; writes nothing to its own file.

    ``catalog`` is unused while Crewmate 2.0 is on; off, the old enrollment
    (:func:`_grant_by_enrollment`) runs with it.
    """
    from kiro_crew import crewmate_v2

    if not crewmate_v2.enabled():
        return _grant_by_enrollment(member, catalog)
    from kiro_crew.agent_files import AGENT_FILENAME
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
    from kiro_crew.mate_config import has_mate_config

    _invalidate_config_cache()
    binding = KiroCrewConfig.load().agents.get(member)
    if binding is None or member == "default":
        return NOT_A_MEMBER
    if binding.kiro_agent == Path(AGENT_FILENAME).stem:
        return DEFAULT_TEMPLATE
    # The eager copy must have run first and repointed the binding at the mate
    # id. If it did not (its best-effort copy failed), there is no owned file to
    # edit; leave the shared pointer as-is rather than minting one here.
    if binding.kiro_agent != member or not has_mate_config(member):
        return MATE_FILE_MISSING
    # A crewmate on its own file chats on the conductor, which the gateway
    # builds with ``kirocrew-mate`` whatever the template carried, and its file
    # only extends the worker, whose base declares the server and always wins.
    # So nothing is written: an entry here could only be an override with no
    # transport, which a session that ran the file would wait on and never start.
    return PLATFORM_PROVIDED


def _grant_by_enrollment(member: str, catalog: Callable[[str], dict[str, str]]) -> str:
    """The creation-time grant while Crewmate 2.0 is off: the enrollment main runs.

    The crewmate has no file of its own then, so the server is set on its
    private copy through the capability editor, as before the switch existed.
    """
    from kiro_crew.agent_capabilities import CapabilityError, CapabilityService
    from kiro_crew.agent_files import AGENT_FILENAME
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache

    _invalidate_config_cache()
    binding = KiroCrewConfig.load().agents.get(member)
    if binding is None or member == "default":
        return NOT_A_MEMBER
    if binding.kiro_agent == Path(AGENT_FILENAME).stem:
        return DEFAULT_TEMPLATE
    service = CapabilityService(catalog)
    try:
        view = service.get(member)
    except CapabilityError as exc:
        logger.info("mate tools not added to %r: %s", member, exc.code)
        return f"refused:{exc.code}"
    if view.get("mode") != "shared":
        return ALREADY_ENROLLED
    ops = _operations(view)
    if isinstance(ops, str):
        return ops
    if not _policy_permits(member):
        return POLICY_WITHHOLDS
    if any(op["section"] == "mcpServers" for op in ops):
        from kiro_crew.agent_capabilities import _member_platform_transport

        if _member_platform_transport(MATE_SERVER) is None:
            return SERVER_UNAVAILABLE
    body = {"revision": view["revision"], "enroll": True, "operations": ops}
    try:
        preview = service.preview(member, body)
        if preview.get("backend_warning"):
            return BACKEND_UNSUPPORTED
        service.put(member, {**body, "preview_token": preview["preview_token"]})
    except CapabilityError as exc:
        logger.info("mate tools not added to %r: %s", member, exc.code)
        return f"refused:{exc.code}"
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller="system",
            operation="member_capabilities.mate_tools_granted",
            outcome="success",
            source="agent_create",
            resources=member,
        )
    except Exception:
        logger.debug("SEL audit unavailable for the mate grant", exc_info=True)
    return GRANTED
