"""Codex CLI adapter: ``$CODEX_HOME`` (default ``~/.codex``).

``config.toml`` supplies MCP servers, project workspaces and settings;
``AGENTS.md`` is the instruction document; ``skills/`` holds packages (its
``.system`` tree is Codex's own). The memory SQLite stores and the automations
database are reported as unsupported, never parsed as memory.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

# The module, not its names: the patch seams read from it here (``_sqlite_columns``)
# live only in that module, so each is read off it at call time and a patch on
# ``onboarding_import.<name>`` reaches this module too.
from kiro_crew import onboarding_scan
from kiro_crew.onboarding_plan import (
    _add_instruction_files,
    _add_mcp_configs,
    _add_skills,
    _collect_project_paths,
    _diagnose_unsupported_config,
    _merge_missing,
    _settings_from,
    _workspace_item,
)
from kiro_crew.onboarding_scan import (
    _SQLITE_TABLE_NAMES_QUERY,
    _open_snapshot_db,
    _parse_configs,
    _Scan,
)


def _scan_codex_automations(scan: _Scan) -> None:
    path = scan.root / "sqlite" / "codex-dev.db"
    if not path.is_file():
        return
    with _open_snapshot_db(path, scan.root, scan, "schedules") as connection:
        if connection is None:
            return
        try:
            tables = {
                str(row[0]) for row in connection.execute(_SQLITE_TABLE_NAMES_QUERY).fetchall()
            }
            if "automations" not in tables:
                return
            columns = onboarding_scan._sqlite_columns(connection, "automations")
            if "rrule" not in columns:
                scan.diagnostic(
                    "schedules",
                    "unsupported_schedule_database",
                    unsupported=True,
                )
                return
            count = connection.execute(
                'SELECT COUNT(*) FROM "automations" '
                'WHERE "rrule" IS NOT NULL AND TRIM("rrule") <> ""'
            ).fetchone()[0]
            if isinstance(count, int) and count:
                scan.diagnostic(
                    "schedules",
                    "unsupported_schedule_semantics",
                    unsupported=True,
                    count=count,
                )
        except sqlite3.Error:
            scan.diagnostic(
                "schedules",
                "unsupported_schedule_database",
                unsupported=True,
            )


def _scan_codex(scan: _Scan) -> None:
    root = scan.root
    configs = _parse_configs(scan, [(root / "config.toml", root, "toml")])
    _diagnose_unsupported_config(scan, configs)
    for config in configs:
        for workspace in _collect_project_paths(config):
            _workspace_item(scan, workspace)
    _add_mcp_configs(scan, configs)
    _add_skills(
        scan,
        [root / "skills"],
        excluded_parts=frozenset({".system"}),
    )
    if any(root.glob("memories*.sqlite*")):
        scan.diagnostic("memories", "unstable_memory_store", unsupported=True)
    if (root / "memories_extensions" / "chronicle").exists():
        scan.diagnostic("memories", "unstable_memory_store", unsupported=True)
    if (root / "hooks.json").exists():
        scan.diagnostic("hooks", "unsupported_category", unsupported=True)
    if (root / "agents").exists():
        scan.diagnostic("agents", "unsupported_category", unsupported=True)
    _add_instruction_files(scan, [(root / "AGENTS.md", root)])
    _scan_codex_automations(scan)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "codex"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)
