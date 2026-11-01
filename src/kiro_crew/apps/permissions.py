"""App permissions — validate and enforce declared permissions.

Apps declare permissions in their manifest. KiroCrew validates them at
install time and enforces them at runtime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from kiro_crew.apps.manager import _read_installed, get_app_manifest, is_app_enabled
from kiro_crew.apps.manifest import AppManifest

logger = logging.getLogger(__name__)


@dataclass
class PermissionCheck:
    """Result of a permission validation check."""

    allowed: bool = True
    warnings: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"allowed": self.allowed}
        if self.warnings:
            d["warnings"] = self.warnings
        if self.denied:
            d["denied"] = self.denied
        return d


def validate_permissions(manifest: AppManifest) -> PermissionCheck:
    """Validate an app's declared permissions.

    Returns a PermissionCheck with warnings for broad permissions
    and denied items for unsafe requests.
    """
    result = PermissionCheck()
    perms = manifest.permissions

    # Check for broad MCP tool access
    if len(perms.mcpTools) > 10:
        result.warnings.append(
            f"App requests access to {len(perms.mcpTools)} MCP tools — review carefully"
        )

    # Network access warning
    if perms.network:
        result.warnings.append("App requests network access — it can make outbound HTTP calls")

    # Shared memory warning
    if perms.memory == "shared":
        result.warnings.append(
            "App requests shared memory access — it can read/write KiroCrew's memory stores"
        )

    # Path traversal in any resource paths
    for path_list_name in ("agents", "skills", "sops"):
        for p in getattr(manifest, path_list_name, []):
            if ".." in str(p):
                result.denied.append(f"path traversal in {path_list_name}: {p}")
                result.allowed = False

    return result


def check_tool_permission(app_name: str, tool_name: str, manifest: AppManifest) -> bool:
    """Check if an app is allowed to use a specific MCP tool.

    Returns True if the tool is in the app's declared mcpTools list,
    or if the app declares no tool restrictions (empty list = unrestricted).
    """
    if not manifest.permissions.mcpTools:
        return True  # no restrictions declared
    return tool_name in manifest.permissions.mcpTools


#: Managed tools the framework wires into every app agent's own view, so a
#: declared ``mcpTools`` list never derives them into the exclusion.
_FRAMEWORK_REQUIRED_MANAGED_TOOLS: frozenset[str] = frozenset({"skill_search"})


class ManagedToolExclusion(NamedTuple):
    """What an app agent's spec withholds from it, per :func:`managed_tool_exclusion`.

    ``tools`` are bare managed tool names for ``managedToolPolicy.exclude``;
    ``servers`` are whole managed servers for ``managedToolPolicy.excludeServers``.
    """

    tools: list[str]
    servers: list[str]


def managed_tools_to_exclude(manifest: AppManifest) -> list[str]:
    """The managed tool names :func:`managed_tool_exclusion` withholds (``tools`` only)."""
    return managed_tool_exclusion(manifest).tools


def managed_tool_exclusion(manifest: AppManifest) -> ManagedToolExclusion:
    """Host-managed tools and servers an app agent may NOT reach, given its ``mcpTools``.

    ``permissions.mcpTools`` is the app author's allow-list of the tools the app's
    own agent and cron sessions use. The gateway mounts its OWN managed MCP servers
    (``kirocrew-core`` / ``kirocrew-cron`` / ``kirocrew-dashboard`` / … -- every name
    in ``mcp_discovery._MANAGED_SERVER_TOOL_MODULES``) into every agent regardless of
    that agent's config, so an app that declared a NARROW list still had the full
    managed surface at runtime -- the allow-list scoped nothing. This turns the
    declaration into a deny-list the framework can enforce through the mechanism that
    already gates managed tools per agent (``managedToolPolicy``, read per session by
    the managed servers): every managed tool the app did NOT declare.

    Returns an empty exclusion -- no restriction -- when ``mcpTools`` is empty (an app
    that declares nothing stays unrestricted, matching :func:`check_tool_permission`).
    Otherwise ``tools`` is the sorted managed tools that fail that same predicate.
    Only host-managed tool names are considered: an app's own server tools
    (``@<app>:...``) and an agent's base built-ins (``read`` / ``glob`` / ``grep``) are
    never in the managed universe, so this never strips the tools the app ships or
    needs to read files -- it withholds only the host capabilities the author did not
    ask for.

    The universe is the AUTHORITATIVE managed-tool set -- each managed server's own
    tool declaration read in-process -- not the display-title table, which tables
    only three of the ten managed servers.

    FAIL-CLOSED on an unreadable server: when a managed server's tool declaration
    cannot be read, its tool names are unknown, so no name-based exclusion can cover
    them. That server is returned in ``servers`` instead, and the agent's spec
    withholds it WHOLE (``managedToolPolicy.excludeServers``) -- every one of its
    tools, a declared one included -- until a later materialization reads it. The
    agent loses that server rather than gaining every tool on it.

    Managed tools the FRAMEWORK itself relies on for every app agent are never
    derived into ``tools`` (:data:`_FRAMEWORK_REQUIRED_MANAGED_TOOLS`): an app
    agent's skills are served through ``skill_search``, so excluding it would cut
    the agent off from its own mapped skills. An author can still exclude one of
    these explicitly in the agent spec. A withheld server is withheld whole, these
    included: the framework cannot tell which tools that server would have served.
    """
    if not manifest.permissions.mcpTools:
        return ManagedToolExclusion([], [])
    app_name = manifest.name
    universe, unreadable = _read_managed_tool_universe()
    tools = sorted(
        tool
        for tool in universe - _FRAMEWORK_REQUIRED_MANAGED_TOOLS
        if not check_tool_permission(app_name, tool, manifest)
    )
    return ManagedToolExclusion(tools, sorted(unreadable))


def _managed_tool_universe() -> set[str]:
    """Every tool name the READABLE host-managed MCP servers expose.

    See :func:`_read_managed_tool_universe`, which also names the servers that
    could not be read.
    """
    return _read_managed_tool_universe()[0]


def _read_managed_tool_universe() -> tuple[set[str], list[str]]:
    """``(tool names, unreadable servers)`` over Kiro Crew's own managed MCP servers.

    The union of each managed server's declared tools, read in-process from the
    server module's own declaration (via
    ``mcp_discovery_managed.managed_tool_names_for_policy``) -- the SAME names a
    spawned ``tools/list`` would return, over every server in
    ``_MANAGED_SERVER_TOOL_MODULES``. A pure, cheap read: no subprocess.

    UNCONDITIONAL on purpose, unlike discovery's "what is live now" read: a server
    that HIDES its tools behind a runtime enable must still contribute its tools
    here, or the deny-list would omit them and enabling the feature later -- which
    does not re-materialize this app agent -- would let a scoped app call tools it
    never declared. The policy reader returns the full ``_tool_definitions()`` for
    such a server.

    A server whose declaration cannot be read (``None``) contributes no names and
    is returned in the second element, so the caller withholds it whole rather
    than leaving its tools reachable; it is logged at warning. The import is
    deferred because ``permissions`` is loaded early in the apps boot path.
    """
    from kiro_crew.mcp_discovery import _MANAGED_SERVER_TOOL_MODULES
    from kiro_crew.mcp_discovery_managed import managed_tool_names_for_policy

    universe: set[str] = set()
    unreadable: list[str] = []
    for server in _MANAGED_SERVER_TOOL_MODULES:
        tools = managed_tool_names_for_policy(server)
        if tools is None:
            logger.warning(
                "mcpTools enforcement: could not read managed tools for %s in-process; "
                "withholding the whole server from scoped app agents until a later "
                "materialization reads it",
                server,
            )
            unreadable.append(server)
            continue
        universe.update(tools)
    return universe, unreadable


def app_can_manage_session_approvals(app_name: str) -> bool:
    """Return whether an enabled app holds the user-session control grant.

    Read the live manifest on every decision. App tokens can outlive an enable
    cycle, so a cached grant must not survive a disable or manifest edit.

    Manifest FIRST, then ``enabled``. ``installed.json`` lives inside the app
    directory and is retired together with the old tree, so while an update
    swaps trees there is no metadata at all and ``enabled`` reads False; reading
    it last means whichever manifest was seen, the enabled state that follows is
    at least as new and cannot pair a widened grant with the pre-update state.
    """
    if not app_name:
        return False
    try:
        manifest = get_app_manifest(app_name)
        if not (manifest and manifest.permissions.sessionApproval):
            return False
        # One record read, judged against the manifest read above, decides
        # consent and enablement together. A grant still waiting for its
        # consent moment is not held: a record written before the consent
        # marker existed reads as pending (``_session_approval_consent_unrecorded``).
        # An unreadable record is not evidence of consent either. The later
        # enablement read can only refuse, never grant.
        meta = _read_installed(app_name, manifest=manifest)
        if meta is None or meta.sessionApprovalConsentPending or not meta.enabled:
            return False
        return is_app_enabled(app_name)
    except Exception:
        logger.warning(
            "Could not resolve user-session control permission for app %s",
            app_name,
            exc_info=True,
        )
        return False


def format_permissions_summary(manifest: AppManifest) -> str:
    """Format a human-readable permissions summary for display during install."""
    perms = manifest.permissions
    lines: list[str] = []

    if perms.mcpTools:
        lines.append(f"  MCP Tools: {', '.join(perms.mcpTools[:5])}")
        if len(perms.mcpTools) > 5:
            lines.append(f"             ... and {len(perms.mcpTools) - 5} more")
    if perms.storage:
        lines.append("  Storage:   app-scoped data directory")
    if perms.network:
        lines.append("  Network:   outbound HTTP access")
    if perms.memory:
        lines.append(f"  Memory:    {perms.memory}")
    if perms.cron:
        lines.append("  Cron:      scheduled agent jobs")

    if not lines:
        return "  No special permissions required"
    return "\n".join(lines)
