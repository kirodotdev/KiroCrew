"""Gemini CLI / Antigravity adapter: ``$GEMINI_HOME`` / ``$ANTIGRAVITY_HOME`` (default ``~/.gemini``).

Google's terminal-agent lineage shares one home, so one adapter reads both the
retired Gemini CLI's layout and Antigravity's. Its config paths are probed in
precedence order, its per-project files under ``config/projects/`` supply the
workspaces, and its MCP entries are normalized onto the canonical shape before
the MCP projection judges them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import url2pathname

from kiro_crew.onboarding_plan import (
    _MAX_WORKSPACE_COMPONENT_CHARS,
    _MAX_WORKSPACE_PATH_CHARS,
    _MAX_WORKSPACES,
    _add_instruction_files,
    _add_mcp_configs,
    _add_skills,
    _collect_project_paths,
    _diagnose_unsupported_config,
    _merge_missing,
    _settings_from,
    _workspace_item,
)
from kiro_crew.onboarding_scan import _is_dir_safe, _parse_configs, _read_json, _Scan, _walk_files

# Google's terminal-agent lineage shares ONE home directory. Antigravity CLI
# (``agy``) has replaced Gemini CLI for individual accounts, reusing
# ``~/.gemini`` rather than claiming a directory of its own
# -- so a single source id covers both, and a user who was moved off Gemini CLI
# still has an importable config on disk. Antigravity is closed-source and its
# config subpath has shifted between releases, so probe every known layout and
# let ``_parse_configs`` skip whichever are absent. ORDER IS PRECEDENCE:
# ``_add_mcp_configs`` keeps the FIRST definition of a server name and
# ``_merge_missing`` keeps the first settings value, so the live tool's current
# layout must come first — a user who migrated keeps a stale ``settings.json``
# next to the Antigravity config, and legacy-first order would import the dead
# tool's definition over the live one.
_GEMINI_CONFIG_RELATIVE_PATHS = (
    "config/mcp_config.json",  # Antigravity CLI, current layout
    "antigravity/mcp_config.json",  # Antigravity CLI, earlier layout
    "antigravity-cli/settings.json",  # Antigravity CLI, settings scope
    "settings.json",  # Gemini CLI (retired), user scope
)


# Per-workspace config, resolved against each configured project root. Same
# precedence rule: Antigravity before the retired Gemini CLI.
_GEMINI_WORKSPACE_RELATIVE_PATHS = (
    ".agents/mcp_config.json",  # Antigravity, workspace MCP
    ".gemini/settings.json",  # Gemini CLI, project scope
)


# Hierarchical context file both tools read as instructional memory.
_GEMINI_CONTEXT_FILENAME = "GEMINI.md"


# Remote-endpoint field names seen across the lineage. Gemini CLI documents
# ``httpUrl``; a real Antigravity ``config/mcp_config.json`` writes
# ``serverUrl``. Both mean the same thing as the canonical ``url`` that
# ``_sanitize_mcp_spec`` accepts.
_GEMINI_URL_FIELDS = ("httpUrl", "serverUrl")


# Antigravity serializes its MCP block from protobuf, so every stdio entry
# carries a ``$typeName`` discriminator
# (``exa.cascade_plugins_pb.CascadePluginCommandTemplate``). It is inert
# serializer metadata with no runtime meaning, and dropping it is lossless --
# but leaving it in place trips the unknown-field arm of ``_sanitize_mcp_spec``
# and refuses EVERY stdio server the tool ever wrote.
_GEMINI_TYPE_MARKER_FIELD = "$typeName"


# Antigravity writes ``env: {}`` on stdio entries that need no environment. The
# key NAME matches ``_SECRET_KEY_RE``, so an empty map is still scored as a
# credential and refuses the server. An empty map carries no secret and no
# behavior, so dropping it is lossless. A NON-empty ``env`` is deliberately left
# untouched: silently stripping it would both change how the server runs and
# hide that secrets were present, so those stay refused.
_GEMINI_ENV_FIELD = "env"


# Antigravity records each project as its own file under ``config/projects/``,
# with the folder as a percent-encoded ``file://`` URI rather than a plain path.
_GEMINI_PROJECTS_DIRNAME = "projects"


_GEMINI_PROJECT_RESOURCES_KEY = "projectResources"


_GEMINI_PROJECT_RESOURCE_LIST_KEY = "resources"


_GEMINI_PROJECT_FOLDER_KEY = "folderUri"


_GEMINI_FILE_URI_SCHEME = "file://"


# Directories that exist under the shared home but have no Kiro Crew
# destination. Antigravity kept Skills, Hooks, Subagents and plugins; only the
# skills path can land here, and only when it is a SKILL.md package.
_GEMINI_UNSUPPORTED_DIRS = (
    ("plugins", "extensions"),
    ("hooks", "hooks"),
    ("subagents", "agents"),
)


def _bounded_workspaces(scan: _Scan, workspaces: set[str]) -> list[str]:
    """Sort, filter and cap a workspace list that a foreign config declared.

    The list is attacker-influenced — it is whatever the source config named —
    and every surviving entry costs filesystem work: two candidate config stats
    here, then a strict ``resolve`` plus a sensitivity check in
    ``_workspace_item``. So bound the count with ``_MAX_WORKSPACES`` (the same
    ceiling the other declared-workspace readers use) and drop relative paths up
    front, since ``_workspace_item`` refuses them anyway and resolving one would
    otherwise aim a candidate read at the gateway's own working directory.
    """
    absolute: list[str] = []
    for workspace in workspaces:
        candidate = workspace.strip()
        if not candidate or "\x00" in candidate:
            continue
        if candidate.startswith(("//", "\\\\")):
            # CLASS-LEVEL chokepoint for network paths: every declared-
            # workspace entry funnels through here regardless of which config
            # shape named it (a ``projects`` key, a workspace map, or a
            # decoded project-file URI). A ``\\server\share`` UNC path IS
            # absolute on Windows (and ``//server/share`` is absolute on
            # POSIX too), so it would pass the not-absolute refusal below and
            # reach the config probes — and merely probing a UNC path makes
            # Windows open an SMB connection (and authenticate) to a host
            # named by a FOREIGN config file.
            scan.diagnostic("workspaces", "network_workspace_excluded")
            continue
        expanded = Path(os.path.expanduser(candidate))
        if not expanded.is_absolute():
            scan.diagnostic("workspaces", "workspace_not_absolute")
            continue
        # Mirror _workspace_item's length ceiling BEFORE the path becomes a
        # candidate read, and check each COMPONENT as well as the total: the
        # filesystem limit that bites first is NAME_MAX (255), not PATH_MAX, so a
        # 301-char path passes a total-length test and still fails to stat.
        # _exists_safe now absorbs that, but rejecting here is what gives the
        # user a reason in the "Not imported" list instead of a silent skip.
        if len(candidate) > _MAX_WORKSPACE_PATH_CHARS or any(
            len(part) > _MAX_WORKSPACE_COMPONENT_CHARS for part in expanded.parts
        ):
            scan.diagnostic("workspaces", "workspace_path_too_long")
            continue
        absolute.append(candidate)
    ordered = sorted(absolute)
    if len(ordered) > _MAX_WORKSPACES:
        scan.diagnostic("workspaces", "item_count_limit", count=len(ordered))
        return ordered[:_MAX_WORKSPACES]
    return ordered


def _normalized_gemini_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Project one Gemini/Antigravity MCP entry onto the canonical shape.

    Three lossless rewrites, verified against a real Antigravity
    ``~/.gemini/config/mcp_config.json``:

    * ``httpUrl`` / ``serverUrl`` -> ``url`` (remote endpoints)
    * drop the inert ``$typeName`` protobuf discriminator
    * drop ``env`` only when it is empty

    Everything else is left exactly as written, so ``_sanitize_mcp_spec`` still
    has the final say. That keeps the credential boundary intact: a server with
    a populated ``env``, or with an unsupported field such as
    ``authProviderType``, is still refused with its own diagnostic rather than
    being reshaped into something Kiro Crew cannot actually run.
    """
    rewritten: dict[str, Any] = {}
    has_url = "url" in spec
    for key, value in spec.items():
        if key == _GEMINI_TYPE_MARKER_FIELD:
            continue
        if key == _GEMINI_ENV_FIELD and not value:
            continue
        if key in _GEMINI_URL_FIELDS and not has_url:
            rewritten["url"] = value
            continue
        rewritten[key] = value
    return rewritten


def _normalized_gemini_configs(configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply ``_normalized_gemini_spec`` to every MCP entry, on a copy."""
    normalized: list[dict[str, Any]] = []
    for config in configs:
        servers = config.get("mcpServers")
        if not isinstance(servers, dict):
            normalized.append(config)
            continue
        rebuilt: dict[str, Any] = {
            name: (_normalized_gemini_spec(spec) if isinstance(spec, dict) else spec)
            for name, spec in servers.items()
        }
        normalized.append({**config, "mcpServers": rebuilt})
    return normalized


def _gemini_project_workspaces(scan: _Scan, root: Path) -> set[str]:
    """Collect workspace paths from Antigravity's ``config/projects/*.json``.

    Antigravity does not keep a ``projects`` map inside its config the way Codex
    and Claude Code do — it writes one file per project, and records the folder
    as a percent-encoded ``file://`` URI. Decode those back into plain absolute
    paths so ``_workspace_item`` can validate them like any other source's.
    """
    found: set[str] = set()
    projects_dir = root / _GEMINI_PROJECTS_DIRNAME
    if not projects_dir.is_dir():
        return found
    for path in _walk_files(projects_dir, scan, "workspaces", suffixes=(".json",)):
        data = _read_json(path, projects_dir, scan, "workspaces")
        if not isinstance(data, dict):
            continue
        resources = data.get(_GEMINI_PROJECT_RESOURCES_KEY)
        entries = (
            resources.get(_GEMINI_PROJECT_RESOURCE_LIST_KEY)
            if isinstance(resources, dict)
            else None
        )
        if not isinstance(entries, list):
            continue
        for entry in entries[:_MAX_WORKSPACES]:
            if not isinstance(entry, dict):
                continue
            uri = entry.get(_GEMINI_PROJECT_FOLDER_KEY)
            if not isinstance(uri, str) or not uri.startswith(_GEMINI_FILE_URI_SCHEME):
                continue
            # ``url2pathname`` rather than a bare ``unquote``: on Windows the URI
            # is ``file:///C:/Users/...``, and stripping the scheme leaves
            # ``/C:/Users/...`` — a path with no drive, which pathlib reports as
            # NOT absolute, so every workspace would be refused as
            # ``workspace_not_absolute`` there. url2pathname is the stdlib's
            # platform-correct inverse (it drops the leading slash and rebuilds
            # ``C:\Users\...`` on Windows, and is plain unquoting on POSIX).
            # ``url2pathname`` re-raises OSError on Windows when the path
            # component cannot map to a drive path (e.g. ``file:///::/x``) —
            # the same class of foreign-input failure the ``_exists_safe``
            # helpers absorb. Refuse the one bad URI with its own skip reason
            # instead of letting the whole scan escape as an HTTP 500.
            try:
                decoded = url2pathname(uri[len(_GEMINI_FILE_URI_SCHEME) :])
            except (OSError, ValueError):
                scan.diagnostic("workspaces", "workspace_uri_invalid")
                continue
            if decoded.startswith(("//", "\\\\")):
                # ``file:////server/share`` decodes to a UNC path, which IS
                # absolute on Windows, so it would sail past the
                # not-absolute refusal in ``_bounded_workspaces`` and reach
                # the workspace/config probes — and merely probing a UNC
                # path makes Windows open an SMB connection (and
                # authenticate) to a host named by a FOREIGN config file.
                # Refuse network workspaces outright.
                scan.diagnostic("workspaces", "network_workspace_excluded")
                continue
            if decoded:
                found.add(decoded)
    return found


def _scan_gemini(scan: _Scan) -> None:
    root = scan.root
    # Root configs are parsed first so the workspace list is known before each
    # project's own config is read — the same two-pass shape as ``_scan_claude``.
    root_configs = _parse_configs(
        scan,
        [(root / relative, root, "json") for relative in _GEMINI_CONFIG_RELATIVE_PATHS],
    )
    declared: set[str] = set()
    for config in root_configs:
        declared.update(_collect_project_paths(config))
    # Antigravity keeps projects as one file each under config/projects/, not as
    # a map inside the config, so they need their own pass.
    declared.update(_gemini_project_workspaces(scan, root / "config"))
    workspaces = _bounded_workspaces(scan, declared)
    project_configs: list[tuple[Path, Path, str]] = []
    for workspace_value in workspaces:
        workspace_path = Path(os.path.expanduser(workspace_value))
        project_configs.extend(
            (workspace_path / relative, workspace_path, "json")
            for relative in _GEMINI_WORKSPACE_RELATIVE_PATHS
        )
    configs = root_configs + _parse_configs(scan, project_configs)
    # Diagnose the NORMALIZED view so both passes agree on what is excluded:
    # on the raw view an EMPTY ``env`` scored as a credential field (the key
    # matches ``_SECRET_KEY_RE``) even though normalization drops it and
    # nothing is actually withheld from the import. Normalization only
    # rewrites ``mcpServers`` entries losslessly — a populated ``env`` is kept
    # verbatim — so no real secret is hidden from the diagnostic.
    normalized_configs = _normalized_gemini_configs(configs)
    _diagnose_unsupported_config(scan, normalized_configs)
    # A project config may name further workspaces; fold them in and re-bound,
    # so the second pass cannot exceed the ceiling either.
    for config in configs:
        declared.update(_collect_project_paths(config))
    workspaces = _bounded_workspaces(scan, declared)
    for configured_workspace in workspaces:
        _workspace_item(scan, configured_workspace)
    _add_mcp_configs(scan, normalized_configs)
    # Antigravity "Agent Skills" land only if they are SKILL.md packages. When the
    # directory exists but yields nothing, say so: a silent no-op would drop the
    # user's skills from the import story with no count AND no skip reason, so
    # they would simply vanish from the Review step's "Not imported" list.
    skills_before = len(scan.items["skills"])
    _add_skills(scan, [root / "skills"])
    if _is_dir_safe(root / "skills") and len(scan.items["skills"]) == skills_before:
        scan.diagnostic("skills", "unsupported_category", unsupported=True)
    instruction_paths: list[tuple[Path, Path]] = [(root / _GEMINI_CONTEXT_FILENAME, root)]
    instruction_paths += [
        (workspace_root / _GEMINI_CONTEXT_FILENAME, workspace_root)
        for workspace_root in (Path(os.path.expanduser(workspace)) for workspace in workspaces)
    ]
    _add_instruction_files(scan, instruction_paths)
    for relative, category in _GEMINI_UNSUPPORTED_DIRS:
        if (root / relative).exists():
            scan.diagnostic(category, "unsupported_category", unsupported=True)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "gemini"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)
