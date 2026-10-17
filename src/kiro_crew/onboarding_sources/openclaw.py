"""OpenClaw adapter: ``$OPENCLAW_STATE_DIR``, else ``$OPENCLAW_HOME`` / profile, else ``~/.openclaw``.

OpenClaw's root depends on a profile and a state-dir override, and its content
lives under one or more agent workspaces (``workspace/``, ``workspace-main/``,
``workspace-<agentId>/``, or wherever ``openclaw.json`` points). ``SOUL.md``'s
directive text and ``AGENTS.md`` become instructions; ``MEMORY.md`` and
``memory/`` become memories. OpenClaw is a live foreign agent, so its own MCP
servers are excluded from import but never purged.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# The module, not its names: the patch seams read from it here (``_is_link_like`` and
# ``_MAX_FILES``) live only in that module, so each is read off it at call time and a
# patch on ``onboarding_import.<name>`` reaches this module too.
from kiro_crew import onboarding_scan
from kiro_crew.onboarding_plan import (
    _add_instruction_files,
    _add_json_schedules,
    _add_mcp_configs,
    _add_memories,
    _add_memory_files,
    _add_skills,
    _collect_project_paths,
    _diagnose_unsupported_config,
    _merge_missing,
    _settings_from,
    _workspace_item,
)
from kiro_crew.onboarding_scan import (
    _expand_root,
    _parse_configs,
    _Scan,
    _sqlite_database_is_safe,
)
from kiro_crew.security import is_sensitive_path

_OPENCLAW_LEGACY_ROOTS = (".clawdbot",)


_OPENCLAW_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _openclaw_profile(env: Mapping[str, str]) -> str:
    profile = env.get("OPENCLAW_PROFILE", "").strip().casefold()
    if profile == "default" or not _OPENCLAW_PROFILE_RE.fullmatch(profile):
        return ""
    return profile


def _openclaw_root(env: Mapping[str, str], home: Path) -> Path:
    """OpenClaw's state root, for ``_source_roots``.

    ``OPENCLAW_STATE_DIR`` wins outright; ``OPENCLAW_HOME`` holds the state dir;
    otherwise the profile's directory under *home*, or -- with no profile --
    the first of ``.openclaw`` and the legacy roots that exists.
    """
    state_override = env.get("OPENCLAW_STATE_DIR", "").strip()
    openclaw_home = env.get("OPENCLAW_HOME", "").strip()
    profile = _openclaw_profile(env)
    state_name = f".openclaw-{profile}" if profile else ".openclaw"
    if state_override:
        return _expand_root(state_override, home)
    if openclaw_home:
        return _expand_root(openclaw_home, home) / state_name
    candidates = [home / state_name]
    if not profile:
        candidates.extend(home / name for name in _OPENCLAW_LEGACY_ROOTS)
    return next(
        (candidate for candidate in candidates if candidate.exists()),
        candidates[0],
    )


def _openclaw_context(
    root: Path,
    home: Path,
    env: Mapping[str, str],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    config_candidates: list[Path] = []
    explicit_config = env.get("OPENCLAW_CONFIG_PATH", "").strip()
    if explicit_config:
        config_candidates.append(_expand_root(explicit_config, home))
    config_candidates.append(root / "openclaw.json")
    if root == home / ".clawdbot":
        config_candidates.append(root / "clawdbot.json")
    config_paths: list[Path] = []
    seen: set[str] = set()
    for path in config_candidates:
        marker = os.path.normcase(os.path.abspath(str(path)))
        if marker not in seen:
            seen.add(marker)
            config_paths.append(path)

    workspace_paths: list[Path] = []
    workspace_override = env.get("OPENCLAW_WORKSPACE_DIR", "").strip()
    if workspace_override:
        workspace_paths.append(_expand_root(workspace_override, home))
    profile = _openclaw_profile(env)
    if profile:
        workspace_paths.append(home / ".openclaw" / f"workspace-{profile}")
    if (root / "workspace").is_dir():
        workspace_paths.append(root / "workspace")
    if (root / "workspace-main").is_dir():
        workspace_paths.append(root / "workspace-main")
    return tuple(config_paths), tuple(workspace_paths)


def _openclaw_agent_entries(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return {}
    entries = agents.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {
        agent_id: entry
        for agent_id, entry in entries.items()
        if isinstance(agent_id, str)
        and agent_id
        and "/" not in agent_id
        and "\\" not in agent_id
        and isinstance(entry, dict)
    }


def _openclaw_workspace_values(config: dict[str, Any]) -> set[str]:
    values = _collect_project_paths(config)
    agents = config.get("agents")
    if isinstance(agents, dict):
        defaults = agents.get("defaults")
        default_workspace = (
            defaults.get("workspace")
            if isinstance(defaults, dict) and isinstance(defaults.get("workspace"), str)
            else ""
        )
        entries = _openclaw_agent_entries(config)
        if entries:
            for agent_id, entry in entries.items():
                workspace = entry.get("workspace")
                if isinstance(workspace, str):
                    values.add(workspace)
                elif default_workspace:
                    values.add(str(Path(default_workspace) / agent_id))
        elif default_workspace:
            values.add(default_workspace)
        configured_agents = agents.get("list")
        if isinstance(configured_agents, list):
            for agent in configured_agents:
                if isinstance(agent, dict) and isinstance(agent.get("workspace"), str):
                    values.add(agent["workspace"])
    profiles = config.get("profiles")
    profile_values: Iterable[Any]
    if isinstance(profiles, dict):
        profile_values = profiles.values()
    elif isinstance(profiles, (list, tuple)):
        profile_values = profiles
    else:
        profile_values = ()
    for profile in profile_values:
        if isinstance(profile, dict) and isinstance(profile.get("workspace"), str):
            values.add(profile["workspace"])
    return values


def _openclaw_agent_dirs(scan: _Scan) -> list[Path]:
    agents_root = scan.root / "agents"
    if not agents_root.is_dir() or onboarding_scan._is_link_like(agents_root):
        if onboarding_scan._is_link_like(agents_root):
            scan.diagnostic("workspaces", "symlink_rejected")
        return []
    children: list[Path] = []
    truncated = False
    try:
        for index, child in enumerate(agents_root.iterdir()):
            if index >= onboarding_scan._MAX_FILES:
                truncated = True
                break
            children.append(child)
    except OSError:
        return []
    if truncated:
        scan.diagnostic("workspaces", "agent_count_limit", count=1)
    agent_dirs: list[Path] = []
    for child in sorted(children, key=lambda path: path.name.casefold()):
        if onboarding_scan._is_link_like(child):
            scan.diagnostic("workspaces", "symlink_rejected")
            continue
        if child.is_dir():
            agent_dirs.append(child)
    return agent_dirs


def _openclaw_workspace_source(scan: _Scan, raw_path: str | Path) -> Path | None:
    raw_value = str(raw_path)
    path = _expand_root(raw_value, scan.user_home)
    if not path.is_absolute():
        scan.diagnostic("workspaces", "workspace_not_absolute")
        return None
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        scan.diagnostic("workspaces", "workspace_unavailable")
        return None
    if not resolved.is_dir() or is_sensitive_path(str(resolved)):
        return None
    try:
        source_root = scan.root.resolve(strict=True)
    except (OSError, RuntimeError):
        source_root = scan.root.resolve()
    if resolved != source_root and source_root not in resolved.parents:
        canonical = _workspace_item(scan, str(resolved))
        if canonical is None:
            return None
    return resolved


def _diagnose_openclaw_database(
    scan: _Scan,
    path: Path,
    category: str,
    reason: str,
) -> None:
    if not os.path.lexists(path):
        return
    if _sqlite_database_is_safe(path, scan.root, scan, category):
        scan.diagnostic(category, reason, unsupported=True)


def _scan_openclaw(scan: _Scan) -> None:
    root = scan.root
    agent_dirs = _openclaw_agent_dirs(scan)
    _diagnose_openclaw_database(
        scan,
        root / "openclaw.sqlite",
        "schedules",
        "unsupported_schedule_database",
    )
    configs = _parse_configs(
        scan,
        [
            (
                path,
                path.parent,
                "json5",
            )
            for path in scan.config_paths
        ],
    )
    _diagnose_unsupported_config(scan, configs)
    workspace_roots: set[Path] = set()
    for workspace_path in scan.workspace_paths:
        resolved = _openclaw_workspace_source(scan, workspace_path)
        if resolved is not None:
            workspace_roots.add(resolved)
    agent_ids = {"main"}
    agent_ids.update(agent_dir.name for agent_dir in agent_dirs)
    for config in configs:
        agent_ids.update(_openclaw_agent_entries(config))
        for configured_workspace in _openclaw_workspace_values(config):
            resolved = _openclaw_workspace_source(scan, configured_workspace)
            if resolved is not None:
                workspace_roots.add(resolved)
    for agent_id in agent_ids:
        default_workspace = root / f"workspace-{agent_id}"
        if not os.path.lexists(default_workspace):
            continue
        resolved = _openclaw_workspace_source(scan, default_workspace)
        if resolved is not None:
            workspace_roots.add(resolved)
    _add_mcp_configs(scan, configs)
    ordered_workspaces = sorted(workspace_roots)
    _add_skills(scan, [workspace / "skills" for workspace in ordered_workspaces])
    _add_memories(scan, [workspace / "memory" for workspace in ordered_workspaces])
    _add_memory_files(
        scan,
        [(workspace / "MEMORY.md", workspace) for workspace in ordered_workspaces],
    )
    # SOUL.md's DIRECTIVE text becomes lessons; its persona ROLE is not imported
    # (see _add_instruction_files). AGENTS.md is a plain instruction document.
    _add_instruction_files(
        scan,
        [
            (workspace / filename, workspace)
            for workspace in ordered_workspaces
            for filename in ("SOUL.md", "AGENTS.md")
        ],
    )
    if (root / "agents").exists():
        scan.diagnostic("agents", "unsupported_category", unsupported=True)
    schedule_paths = [path for path in (root / "cron" / "jobs.json",) if path.is_file()]
    _add_json_schedules(scan, schedule_paths, root)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "openclaw"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)
