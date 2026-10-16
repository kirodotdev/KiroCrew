"""The crewmate's own-record tools (``kirocrew-mate``) on a crewmate's spec.

Two questions about that one server, answered in one place:

* :func:`spec_has_mate_tools` / :func:`binding_has_mate_tools` -- whether a
  crewmate's sessions load the server, so the text Crew writes for it (its
  identity, its first welcome, the goal nudges) names the tools only when they
  are there, and points the user at the Goals tab when they are not.
* :func:`grant_mate_tools_to_new_member` -- at creation only, a crewmate whose
  template does not carry the server is enrolled through the capability editor
  (:class:`kiro_crew.agent_capabilities.CapabilityService`), the same preview and
  save the owner's Capabilities pane runs. The enrollment sets the server, its
  ``tools`` reference and :data:`kiro_crew.agent._MATE_AUTO_GRANTS` on the
  member's private copy and leaves every other row inheriting, so the copy keeps
  following its template. An existing crewmate is never touched.

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

# Outcomes of :func:`grant_mate_tools_to_new_member`; only GRANTED wrote anything.
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


def _mutate_spec_for_mate_tools(spec: dict) -> bool:
    """Add the mate server + tools ref + auto-grants to *spec* IN PLACE.

    The mate-owned-config form of the grant: instead of driving the capability
    editor's fork/enroll (which mints a ``crew-`` draft and a ``private_to``
    lineage the RFC's severed copy explicitly does not want), the mate's own
    config file is edited directly. Idempotent -- a spec that already launches the
    server, mounts its tools and holds the auto-grants is left untouched.

    Returns True iff anything changed. Mirrors :func:`_operations`' intent:

    * launch ``kirocrew-mate`` (``mcpServers`` entry, un-muted) if absent;
    * reference it in ``tools`` if not already mounted;
    * add every :data:`kiro_crew.agent._MATE_AUTO_GRANTS` not already covered by
      an ``allowedTools`` entry.
    """
    from kiro_crew import agent as agent_mod

    changed = False
    servers = spec.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers is not an object")
    entry = servers.get(MATE_SERVER)
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted

    if entry is None:
        servers[MATE_SERVER] = {"disabled": False}
        changed = True
    elif isinstance(entry, dict) and mcp_entry_is_muted(entry):
        # The template's author muted it; honour that, mirroring TEMPLATE_MUTES_IT.
        return False
    tools = spec.get("tools")
    if not isinstance(tools, list):
        tools = []
        spec["tools"] = tools
    if not _referenced(tools):
        tools.append(_MATE_REF)
        changed = True
    allowed = spec.get("allowedTools")
    if not isinstance(allowed, list):
        allowed = []
        spec["allowedTools"] = allowed
    allowed_map: dict[str, dict] = {ref: {} for ref in allowed if isinstance(ref, str)}
    for grant in agent_mod._MATE_AUTO_GRANTS:
        if not _covered(grant, allowed_map):
            allowed.append(grant)
            allowed_map[grant] = {}
            changed = True
    return changed


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
    """Give the just-created crewmate *member* the mate tools its template lacks.

    Call it once, right after the create published the member, and only then:
    an existing crewmate is never enrolled by this. Returns one of the module's
    outcome codes and never raises, because a create that landed must not be
    reported as failed over a missing optional tool set. Skipped (nothing
    written) when:

    * the template already mounts the server, or mutes it (the template's
      author decided);
    * the template is the default spec ``kirocrew``, which receives the mate set
      from Crew itself and keeps a removal the user made there;
    * POLICY ∩ PROFILE withholds the server, or a fresh build would not emit it;
    * a backend the member's chats start on cannot load an enrolled member's
      saved spec (``agent_capabilities.member_backend_warning`` on the previewed
      copy), since enrolling would then refuse every chat start.
    """
    try:
        return _grant(member, catalog or _default_catalog)
    except Exception:
        logger.warning("mate tools were not added to new crewmate %r", member, exc_info=True)
        return FAILED


def _grant(member: str, catalog: Callable[[str], dict[str, str]]) -> str:
    """Edit the crewmate's OWNED config file in place to add the mate tools.

    The mate-owned-config mechanism (RFC): the crewmate already owns a config
    file at ``mates/agents/<member>.json`` (the eager copy ran first, repointing
    the binding to the mate id), so the grant is a direct read-modify-write of
    that severed file -- no capability-editor fork, no ``crew-`` draft, no
    ``private_to`` lineage. ``catalog`` is unused now (the enroll path needed it
    to classify skills; a direct edit does not), kept in the signature so callers
    and tests are unchanged.
    """
    from kiro_crew.acp.mate_config import has_mate_config, mate_config_path
    from kiro_crew.agent_files import AGENT_FILENAME
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
    from kiro_crew.config.paths import mate_agents_dir

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
    if not _policy_permits(member):
        return POLICY_WITHHOLDS
    from kiro_crew.agent_capabilities import _member_platform_transport

    if _member_platform_transport(MATE_SERVER) is None:
        return SERVER_UNAVAILABLE

    from kiro_crew.agent import _read_spec_capped, _spec_path_is_safe
    from kiro_crew.atomic_write import atomic_write
    from kiro_crew.platform.governance import sanitize_agent_config_governance

    root = mate_agents_dir()
    path = mate_config_path(member)
    if not _spec_path_is_safe(path, root):
        return FAILED
    spec = _read_spec_capped(path)
    if not isinstance(spec, dict):
        return FAILED
    before = _digest(spec)
    try:
        changed = _mutate_spec_for_mate_tools(spec)
    except ValueError:
        logger.info("mate tools not added to %r: malformed owned spec", member)
        return FAILED
    if not changed:
        # Already carries the set (TEMPLATE_HAS_IT equivalent for an owned file).
        return TEMPLATE_HAS_IT
    spec["name"] = member
    sanitize_agent_config_governance(spec)
    if _digest(spec) == before:
        # Governance stripped exactly what we added -- the ceiling withholds it.
        return POLICY_WITHHOLDS
    try:
        atomic_write(path, json.dumps(spec, indent=2) + "\n", restrict_to_owner=True)
    except OSError:
        logger.warning("mate tools: could not write owned spec for %r", member, exc_info=True)
        return FAILED
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
