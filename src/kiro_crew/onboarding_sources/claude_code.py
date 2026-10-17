"""Claude Code adapter: ``$CLAUDE_CONFIG_DIR`` / ``$CLAUDE_HOME`` (default ``~/.claude``).

Reads ``settings.json`` / ``settings.local.json`` and the sibling
``~/.claude.json`` for workspaces, MCP servers and settings, then each declared
workspace's own ``.claude/`` configs, skills and ``CLAUDE.md``. ``CLAUDE.md``
and ``rules/*.md`` are instructions; ``memory/`` and the per-project memory
dirs under ``projects/`` are memories. Workspaces come from explicit
configuration only -- session transcripts are never read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kiro_crew.onboarding_plan import (
    _add_instruction_files,
    _add_mcp_configs,
    _add_memories,
    _add_skills,
    _collect_project_paths,
    _diagnose_unsupported_config,
    _merge_missing,
    _settings_from,
    _workspace_item,
)
from kiro_crew.onboarding_scan import _named_descendant_dirs, _parse_configs, _Scan, _walk_files


def _scan_claude(scan: _Scan) -> None:
    root = scan.root
    # Workspaces come from explicit configuration ONLY. Session transcripts are
    # not imported (see docs/system-specs/modules/onboarding-import.md), so the
    # root configs are parsed FIRST to learn the workspaces, then each
    # workspace's own config files are parsed in a second pass.
    root_configs = _parse_configs(
        scan,
        [
            (root / "settings.local.json", root, "json"),
            (root / "settings.json", root, "json"),
            (root / ".claude.json", root, "json"),
            (root.parent / ".claude.json", root.parent, "json"),
        ],
    )
    workspaces: set[str] = set()
    for config in root_configs:
        workspaces.update(_collect_project_paths(config))
    project_configs: list[tuple[Path, Path, str]] = []
    for workspace_value in sorted(workspaces):
        workspace_path = Path(workspace_value)
        project_configs.extend(
            [
                (workspace_path / ".claude" / "settings.local.json", workspace_path, "json"),
                (workspace_path / ".claude" / "settings.json", workspace_path, "json"),
                (workspace_path / ".mcp.json", workspace_path, "json"),
            ]
        )
    configs = root_configs + _parse_configs(scan, project_configs)
    _diagnose_unsupported_config(scan, configs)
    for config in configs:
        for configured_workspace in _collect_project_paths(config):
            _workspace_item(scan, configured_workspace)
    _add_mcp_configs(scan, configs)
    skill_roots = [root / "skills"]
    skill_roots.extend(Path(workspace) / ".claude" / "skills" for workspace in workspaces)
    _add_skills(scan, skill_roots)
    memory_roots = [root / "memory"]
    memory_roots += _named_descendant_dirs(
        root / "projects",
        scan,
        "memories",
        frozenset({"memory", "memories"}),
    )
    _add_memories(scan, memory_roots)
    if (root / "tasks").exists():
        scan.diagnostic("runtime", "runtime_state_excluded")
    instruction_paths: list[tuple[Path, Path]] = [(root / "CLAUDE.md", root)]
    instruction_paths += [
        (path, root)
        for path in _walk_files(
            root / "rules",
            scan,
            "instructions",
            suffixes=(".md", ".markdown"),
        )
    ]
    instruction_paths += [
        (Path(workspace) / "CLAUDE.md", Path(workspace)) for workspace in sorted(workspaces)
    ]
    _add_instruction_files(scan, instruction_paths)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "claude_code"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)
