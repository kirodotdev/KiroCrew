"""Lineage adapter: an install of Kiro Crew's OWN layout that an edition registers.

A predecessor, a rename or a fork of this product writes ``config.json``,
``mcp.json``, ``recent_projects.json``, a ``workspace/`` tree, ``crons.json``
and ``memory.db`` exactly where this product does, so a registered source needs
a root and nothing else: every registered source is read by
:func:`_scan_lineage_install`. Its ``memory.db`` is the one foreign store the
engine parses, row by row, through the decoded-value screen.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

# The module, not its names: the patch seams read from it here (``_sqlite_columns``)
# live only in that module, so each is read off it at call time and a patch on
# ``onboarding_import.<name>`` reaches this module too.
from kiro_crew import onboarding_scan
from kiro_crew.onboarding_plan import (
    _MAX_WORKSPACES,
    _add_db_directive,
    _add_instruction_files,
    _add_json_schedules,
    _add_mcp_configs,
    _add_memories,
    _add_skills,
    _collect_project_paths,
    _diagnose_unsupported_config,
    _merge_missing,
    _settings_from,
    _workspace_item,
)
from kiro_crew.onboarding_scan import (
    _SQLITE_TABLE_NAMES_QUERY,
    _count_secret_fields,
    _decoded_value_is_unsafe,
    _open_snapshot_db,
    _parse_configs,
    _read_json,
    _read_text,
    _sanitize_text,
    _Scan,
)
from kiro_crew.security import contains_injection

_MAX_DB_ROWS = 10_000


_SEMANTIC_KEY_RE = re.compile(r"^[a-z][a-z0-9_.]*[a-z0-9]$")


_SEMANTIC_PREFIXES = ("pref.", "project.", "user.", "lesson.")


# Foreign memory stores carry a workspace/scope column even for single-workspace
# installs, where it holds a SENTINEL rather than a real workspace identity.
# Treating a sentinel as "scoped" drops every row: an install that stamps
# ``default`` on all of them reads as entirely workspace-scoped and imports nothing.
_UNSCOPED_WORKSPACE_IDS = frozenset({"", "default", "global", "main", "none", "null"})


def _scan_lineage_install(scan: _Scan) -> None:
    """Scan an agent that shares Kiro Crew's OWN on-disk layout.

    A predecessor, a rename, or a fork of this product writes the same files in
    the same places — ``config.json``, ``mcp.json``, ``recent_projects.json``,
    a ``workspace/`` tree, ``crons.json``, ``memory.db`` — so reading one needs
    no format knowledge, only a root. The engine reads it: every registered
    (non-core) source is bound to this reader by the engine itself, so a source
    descriptor carries no layout field and never restates the layout.
    """
    root = scan.root
    workspaces: set[str] = set()
    configs = _parse_configs(
        scan,
        [
            (root / "config.json", root, "json"),
            (root / "mcp.json", root, "json"),
        ],
    )
    _diagnose_unsupported_config(scan, configs)
    recent = root / "recent_projects.json"
    if recent.is_file():
        data = _read_json(recent, root, scan, "workspaces")
        if isinstance(data, list):
            for recent_workspace in data[:_MAX_WORKSPACES]:
                if isinstance(recent_workspace, str):
                    canonical = _workspace_item(scan, recent_workspace)
                    if canonical:
                        workspaces.add(canonical)
    for pointer_name in ("workspace_dir", "project_dir"):
        workspace_file = root / pointer_name
        if workspace_file.is_file():
            workspace_value = _read_text(workspace_file, root, scan, "workspaces")
            if workspace_value:
                canonical = _workspace_item(scan, workspace_value.strip())
                if canonical:
                    workspaces.add(canonical)
    for config in configs:
        for configured_workspace in _collect_project_paths(config):
            canonical = _workspace_item(scan, configured_workspace)
            if canonical:
                workspaces.add(canonical)
    _add_mcp_configs(scan, configs)
    skill_roots = [root / "workspace" / "skills"]
    skill_roots.extend(Path(workspace) / "skills" for workspace in sorted(workspaces))
    _add_skills(scan, skill_roots)
    # The workspace tree holds arbitrary user documents, so only the canonical
    # instruction filenames are read — never a blind sweep of every .md there.
    _add_instruction_files(
        scan,
        [
            (base / filename, base)
            for base in (root / "workspace", *(Path(w) for w in sorted(workspaces)))
            for filename in ("AGENTS.md", "CLAUDE.md")
        ],
    )
    has_memory_db = _scan_lineage_memory_db(scan)
    _add_memories(scan, [root / "workspace" / "memory"])
    if not has_memory_db:
        _add_memories(scan, [root / "memory"])
    schedule_paths = [
        path for path in (root / "crons.json", root / "cron" / "jobs.json") if path.is_file()
    ]
    _add_json_schedules(scan, schedule_paths, root)
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, scan.source_id))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)


def _row_is_workspace_scoped(value: Any) -> bool:
    """Return whether a memory row belongs to ONE foreign workspace.

    Kiro Crew's own memory tables have no workspace column, so a genuinely
    workspace-scoped row has no faithful destination and is reported unsupported.
    A SENTINEL value is not scoping, though: a single-workspace install stamps
    every row with the same placeholder, so reading that as scoped discarded 100%
    of the store.
    """
    if value is None:
        return False
    return str(value).strip().casefold() not in _UNSCOPED_WORKSPACE_IDS


def _scan_lineage_memory_db(scan: _Scan) -> bool:
    path = scan.root / "memory.db"
    if not path.is_file():
        return False
    with _open_snapshot_db(path, scan.root, scan, "memories") as connection:
        if connection is None:
            return True
        try:
            tables = {
                str(row[0]) for row in connection.execute(_SQLITE_TABLE_NAMES_QUERY).fetchall()
            }
            required_columns = {
                "semantic_memory": {"key", "value_json", "confidence", "is_deleted"},
                "episodic_memories": {"id", "text", "importance", "is_deleted"},
            }
            table_columns = {
                table: onboarding_scan._sqlite_columns(connection, table)
                for table in required_columns
                if table in tables
            }
            active_rows = 0
            for table, required in required_columns.items():
                if required <= table_columns.get(table, set()):
                    remaining = _MAX_DB_ROWS - active_rows
                    rows = connection.execute(
                        f'SELECT 1 FROM "{table}" WHERE "is_deleted" = 0 LIMIT ?',
                        (remaining + 1,),
                    ).fetchall()
                    active_rows += len(rows)
                    if active_rows > _MAX_DB_ROWS:
                        scan.diagnostic("memories", "row_count_limit")
                        return True
            supported = False
            if "semantic_memory" in tables:
                columns = table_columns["semantic_memory"]
                if {"key", "value_json", "confidence", "is_deleted"} <= columns:
                    supported = True
                    extra_columns = [name for name in ("workspace_id", "kind") if name in columns]
                    selected_columns = ["key", "value_json", "confidence", *extra_columns]
                    rows = connection.execute(
                        "SELECT "
                        + ", ".join(f'"{name}"' for name in selected_columns)
                        + ' FROM "semantic_memory" WHERE "is_deleted" = 0 LIMIT ?',
                        (_MAX_DB_ROWS,),
                    ).fetchall()
                    for row in rows:
                        values = dict(zip(selected_columns, row))
                        key = values["key"]
                        value_json = values["value_json"]
                        confidence = values["confidence"]
                        if _row_is_workspace_scoped(values.get("workspace_id")):
                            scan.diagnostic(
                                "memories",
                                "scoped_memory_unsupported",
                                unsupported=True,
                            )
                            continue
                        # A directive is a RULE, not a fact, so semantic memory is
                        # the wrong tier -- but dropping it would discard exactly
                        # the least replaceable rows (a lineage store keeps every
                        # learned lesson this way). Route it to the instruction
                        # tier, which is where an imported rule belongs, instead.
                        is_directive = str(values.get("kind", "")).casefold() == "directive"
                        if (
                            not isinstance(key, str)
                            or len(key) > 100
                            or not _SEMANTIC_KEY_RE.fullmatch(key)
                            or not key.startswith(_SEMANTIC_PREFIXES)
                            or not isinstance(value_json, str)
                        ):
                            scan.diagnostic("memories", "unsupported_semantic_memory")
                            continue
                        cleaned = _sanitize_text(value_json, scan)
                        if cleaned != value_json.strip():
                            scan.diagnostic("memories", "credential_bearing_memory")
                            continue
                        if contains_injection(cleaned):
                            scan.diagnostic("memories", "injection_memory_excluded")
                            continue
                        try:
                            value = json.loads(value_json)
                        except (json.JSONDecodeError, RecursionError):
                            scan.diagnostic("memories", "invalid_memory_record")
                            continue
                        if _count_secret_fields(value):
                            scan.diagnostic("memories", "secret_fields_omitted")
                            continue
                        # Re-screen the DECODED value: the screens above ran on the
                        # raw JSON text, which hides both patterns behind escapes.
                        unsafe = _decoded_value_is_unsafe(value, scan)
                        if unsafe:
                            scan.diagnostic("memories", unsafe)
                            continue
                        numeric_confidence = (
                            float(confidence)
                            if isinstance(confidence, (int, float))
                            and not isinstance(confidence, bool)
                            else 0.9
                        )
                        if is_directive:
                            _add_db_directive(scan, key, value)
                            continue
                        payload = {
                            "kind": "semantic",
                            "key": key,
                            "value": value,
                            "confidence": max(0.8, min(1.0, numeric_confidence)),
                        }
                        scan.add("memories", f"sqlite\0semantic\0{key}", payload)
                else:
                    scan.diagnostic(
                        "memories",
                        "unsupported_memory_database_schema",
                        unsupported=True,
                    )
            if "episodic_memories" in tables:
                columns = table_columns["episodic_memories"]
                if {"id", "text", "importance", "is_deleted"} <= columns:
                    supported = True
                    extra_columns = [name for name in ("workspace_id", "kind") if name in columns]
                    selected_columns = ["id", "text", "importance", *extra_columns]
                    rows = connection.execute(
                        "SELECT "
                        + ", ".join(f'"{name}"' for name in selected_columns)
                        + ' FROM "episodic_memories" WHERE "is_deleted" = 0 LIMIT ?',
                        (_MAX_DB_ROWS,),
                    ).fetchall()
                    for row in rows:
                        values = dict(zip(selected_columns, row))
                        memory_id = values["id"]
                        text = values["text"]
                        importance = values["importance"]
                        if _row_is_workspace_scoped(values.get("workspace_id")):
                            scan.diagnostic(
                                "memories",
                                "scoped_memory_unsupported",
                                unsupported=True,
                            )
                            continue
                        is_directive = str(values.get("kind", "")).casefold() == "directive"
                        if not isinstance(text, str):
                            scan.diagnostic("memories", "invalid_memory_record")
                            continue
                        cleaned = _sanitize_text(text, scan)
                        if cleaned != text.strip():
                            scan.diagnostic("memories", "credential_bearing_memory")
                            continue
                        if contains_injection(cleaned):
                            scan.diagnostic("memories", "injection_memory_excluded")
                            continue
                        # A directive stored as an episode is still a rule: route it
                        # to the lesson tier rather than dropping it (see
                        # _add_db_directive). Checked before the episodic length
                        # bound so a directive is measured against the instruction
                        # limits, not the episodic ones.
                        if is_directive:
                            _add_db_directive(scan, str(memory_id), cleaned)
                            continue
                        if not 10 <= len(cleaned) <= 2000:
                            scan.diagnostic("memories", "unsupported_memory_length")
                            continue
                        numeric_importance = (
                            float(importance)
                            if isinstance(importance, (int, float))
                            and not isinstance(importance, bool)
                            else 0.5
                        )
                        payload = {
                            "kind": "episodic",
                            "text": cleaned,
                            "importance": max(0.0, min(1.0, numeric_importance)),
                        }
                        scan.add("memories", f"sqlite\0episodic\0{memory_id}", payload)
                else:
                    scan.diagnostic(
                        "memories",
                        "unsupported_memory_database_schema",
                        unsupported=True,
                    )
            if not supported:
                scan.diagnostic(
                    "memories",
                    "unsupported_memory_database_schema",
                    unsupported=True,
                )
        except sqlite3.Error:
            scan.diagnostic(
                "memories",
                "unsupported_memory_database_schema",
                unsupported=True,
            )
    return True
