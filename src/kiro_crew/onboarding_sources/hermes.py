"""Hermes Agent adapter: ``$HERMES_HOME`` (and aliases), else ``%LOCALAPPDATA%/hermes``, else ``~/.hermes``.

Reads the root and up to 50 ``profiles/`` beside it: ``config.yaml`` for MCP
servers, settings and the default timezone, ``memories/MEMORY.md`` +
``USER.md``, ``SOUL.md``'s directive text, ``skills/`` (minus Hermes's managed
and re-imported trees) and ``cron/jobs.json``. ``memory_store.db`` is reported
as unsupported, never parsed.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any

# The module, not its names: the patch seams read from it here (``_is_link_like`` and
# ``_sqlite_columns``) live only in that module, so each is read off it at call time
# and a patch on ``onboarding_import.<name>`` reaches this module too.
from kiro_crew import onboarding_scan
from kiro_crew.onboarding_plan import (
    _MAX_SCHEDULES,
    _MAX_WORKSPACES,
    _add_instruction_files,
    _add_mcp_configs,
    _add_memory_files,
    _add_skills,
    _diagnose_unsupported_config,
    _merge_missing,
    _safe_skill_name,
    _schedule_from_record,
    _settings_from,
    _workspace_item,
)
from kiro_crew.onboarding_scan import (
    _SQLITE_TABLE_NAMES_QUERY,
    _open_snapshot_db,
    _parse_configs,
    _read_json,
    _read_text,
    _Scan,
)

# Directory names a foreign agent's OWN importer uses for skills it pulled in
# from a third agent (Hermes: ``hermes import-agent`` / ``hermes claw migrate``).
_FOREIGN_REIMPORT_SKILL_DIRS = (
    "claude-code-imports",
    "codex-imports",
    "openclaw-imports",
)


_HERMES_SKILL_EXCLUDED_PARTS = frozenset(
    {
        ".archive",
        ".hub",
        "dependency",
        "dependencies",
        "cache",
        ".cache",
        # Hermes ships its own importer, which writes FOREIGN skills into these
        # dirs. Importing them from Hermes would duplicate what the original
        # source already contributes, and neither dedupe layer can catch it: the
        # fingerprint is source-scoped (``hermes`` != ``claude_code``) and the
        # destination differs too (``skills/imported/hermes/`` vs
        # ``skills/imported/claude_code/``), so not even a conflict is reported.
        # The originals are still on disk, so excluding these loses nothing.
        *_FOREIGN_REIMPORT_SKILL_DIRS,
    }
)


_HERMES_SCHEDULE_RUNTIME_FIELDS = frozenset(
    {
        "id",
        "enabled",
        "created_at",
        "updated_at",
        "last_run_at",
        "next_run_at",
        "last_error",
        "last_result",
        "last_status",
        "last_delivery_error",
        "status",
        "run_count",
        "schedule_display",
        "state",
        "paused_at",
        "paused_reason",
    }
)


_HERMES_INERT_SCHEDULE_FIELDS = frozenset(
    {
        "skills",
        "skill",
        "model",
        "provider",
        "provider_snapshot",
        "model_snapshot",
        "base_url",
        "script",
        "context_from",
        "enabled_toolsets",
        "workdir",
    }
)


_HERMES_SCHEDULE_FIELDS = (
    frozenset(
        {
            "name",
            "prompt",
            "schedule",
            "timezone",
            "repeat",
            "origin",
            "deliver",
            "no_agent",
        }
    )
    | _HERMES_SCHEDULE_RUNTIME_FIELDS
    | _HERMES_INERT_SCHEDULE_FIELDS
)


def _hermes_windows_root(env: Mapping[str, str]) -> Path | None:
    """``%LOCALAPPDATA%/hermes`` when it exists, for ``_source_roots``.

    Consulted only when no ``HERMES_*`` override is set, and before the
    ``~/.hermes`` default: it is where Hermes lives on Windows.
    """
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    windows_root = Path(local_app_data) / "hermes" if local_app_data else None
    if windows_root is not None and windows_root.exists():
        return windows_root
    return None


def _hermes_schedule_has_unsupported_semantics(record: dict[str, Any]) -> bool:
    fields = set(record)
    if fields - _HERMES_SCHEDULE_FIELDS:
        return True
    if any(key.casefold().replace("_", "").startswith(("claim", "execution")) for key in fields):
        return True
    if any(
        record.get(key) not in (None, "", [], {}) for key in fields & _HERMES_INERT_SCHEDULE_FIELDS
    ):
        return True
    if "no_agent" in record and record["no_agent"] is not False:
        return True
    repeat = record.get("repeat")
    if repeat is not None:
        schedule = record.get("schedule")
        raw_kind = schedule.get("kind", "") if isinstance(schedule, dict) else ""
        kind = raw_kind.casefold() if isinstance(raw_kind, str) else ""
        expected_times = 1 if kind == "once" else None
        if repeat != {"times": expected_times, "completed": 0}:
            return True
    origin = record.get("origin")
    if origin not in (None, ""):
        return True
    deliver = record.get("deliver")
    if isinstance(deliver, str):
        if deliver.casefold() not in ("", "local"):
            return True
    elif isinstance(deliver, dict):
        if set(deliver) - {"mode"} or str(deliver.get("mode", "")).casefold() != "local":
            return True
    elif deliver is not None:
        return True
    return False


def _hermes_schedule_from_record(
    record: Any,
    scan: _Scan,
    *,
    default_timezone: str = "",
) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
        return None
    if _hermes_schedule_has_unsupported_semantics(record):
        scan.diagnostic("schedules", "unsupported_schedule_semantics", unsupported=True)
        return None
    name = record.get("name")
    prompt = record.get("prompt")
    schedule = record.get("schedule")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(prompt, str)
        or not isinstance(schedule, dict)
    ):
        scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
        return None
    kind = schedule.get("kind")
    if not isinstance(kind, str):
        scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
        return None
    kind = kind.casefold()
    allowed_schedule_fields = {
        "cron": {"kind", "expr", "timezone", "display"},
        "interval": {"kind", "minutes", "display"},
        "once": {"kind", "run_at", "timezone", "display"},
    }.get(kind)
    if allowed_schedule_fields is None or set(schedule) - allowed_schedule_fields:
        scan.diagnostic("schedules", "unsupported_schedule_semantics", unsupported=True)
        return None

    timezone_value = schedule.get("timezone", record.get("timezone", default_timezone))
    if kind == "cron" and not timezone_value:
        scan.diagnostic("schedules", "timezone_required", unsupported=True)
        return None
    if kind == "once":
        run_at = schedule.get("run_at")
        if isinstance(run_at, str):
            try:
                parsed = datetime.fromisoformat(run_at.strip().replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is None and not timezone_value:
                scan.diagnostic("schedules", "timezone_required", unsupported=True)
                return None

    projected_schedule = {key: value for key, value in schedule.items() if key != "display"}
    if timezone_value and "timezone" not in projected_schedule:
        projected_schedule["timezone"] = timezone_value
    projected = {
        "name": name,
        "prompt": prompt,
        "schedule": projected_schedule,
    }
    return _schedule_from_record(projected, scan)


def _add_hermes_json_schedules(
    scan: _Scan,
    paths: list[Path],
    anchor: Path,
    *,
    default_timezone: str = "",
) -> None:
    for path in paths:
        data = _read_json(path, anchor, scan, "schedules")
        records: Any = data.get("jobs", []) if isinstance(data, dict) else data
        if not isinstance(records, list):
            scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
            continue
        for record in records[:_MAX_SCHEDULES]:
            payload = _hermes_schedule_from_record(
                record,
                scan,
                default_timezone=default_timezone,
            )
            if payload is not None:
                scan.add("schedules", json.dumps(payload, sort_keys=True), payload)


def _sqlite_workspace_values(
    connection: sqlite3.Connection,
    table: str,
    columns: set[str],
    candidates: tuple[str, ...],
    scan: _Scan,
) -> None:
    selected = [name for name in candidates if name in columns]
    for column in selected:
        rows = connection.execute(
            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL LIMIT ?',
            (_MAX_WORKSPACES,),
        ).fetchall()
        for (workspace,) in rows:
            if isinstance(workspace, str):
                _workspace_item(scan, workspace)


def _scan_hermes_projects_db(scan: _Scan, root: Path) -> None:
    path = root / "projects.db"
    if not path.is_file():
        return
    with _open_snapshot_db(path, root, scan, "workspaces") as connection:
        if connection is None:
            return
        try:
            tables = {
                str(row[0]) for row in connection.execute(_SQLITE_TABLE_NAMES_QUERY).fetchall()
            }
            if "projects" in tables:
                _sqlite_workspace_values(
                    connection,
                    "projects",
                    onboarding_scan._sqlite_columns(connection, "projects"),
                    ("primary_path", "path", "cwd", "root"),
                    scan,
                )
            if "project_folders" in tables:
                _sqlite_workspace_values(
                    connection,
                    "project_folders",
                    onboarding_scan._sqlite_columns(connection, "project_folders"),
                    ("path",),
                    scan,
                )
        except sqlite3.Error:
            scan.diagnostic("workspaces", "unsupported_database_schema", unsupported=True)


def _hermes_roots(scan: _Scan) -> list[Path]:
    roots = [scan.root]
    profiles = scan.root / "profiles"
    if profiles.is_dir() and not onboarding_scan._is_link_like(profiles):
        try:
            children = list(islice(profiles.iterdir(), 51))
        except OSError:
            scan.diagnostic("profiles", "read_failed")
            return roots
        if len(children) > 50:
            scan.diagnostic("profiles", "profile_count_limit", count=1)
        for child in sorted(children[:50], key=lambda path: path.name.casefold()):
            if child.is_dir() and not onboarding_scan._is_link_like(child):
                roots.append(child)
    return roots


def _hermes_skill_lock_names(data: Any, skills_root: Path) -> set[str]:
    if not isinstance(data, dict):
        return set()
    containers: list[dict[Any, Any] | list[Any]] = []
    for key in ("skills", "installed"):
        container_value = data.get(key)
        if isinstance(container_value, dict):
            containers.append(container_value)
        elif isinstance(container_value, list):
            containers.append(container_value)
    names: set[str] = set()
    for container in containers:
        entries: Iterable[tuple[Any, Any]]
        if isinstance(container, dict):
            entries = container.items()
        else:
            entries = ((None, item) for item in container)
        for raw_name, value in entries:
            candidates = [raw_name]
            if isinstance(value, dict):
                candidates.extend((value.get("name"), value.get("install_path")))
            for candidate in candidates:
                if not isinstance(candidate, str) or not candidate.strip():
                    continue
                path = Path(candidate)
                if path.is_absolute():
                    try:
                        path = path.relative_to(skills_root)
                    except ValueError:
                        continue
                if path.parts and path.parts[0].casefold() == "skills":
                    path = Path(*path.parts[1:])
                name = _safe_skill_name(path)
                if name:
                    names.add(name.casefold())
    return names


def _hermes_managed_skill_names(scan: _Scan, root: Path) -> frozenset[str]:
    skills_root = root / "skills"
    names: set[str] = set()
    manifest = skills_root / ".bundled_manifest"
    if manifest.is_file():
        text = _read_text(manifest, root, scan, "skills")
        if text is not None:
            for line in text.splitlines():
                raw_name = line.strip().split(":", 1)[0]
                name = _safe_skill_name(Path(raw_name))
                if name:
                    names.add(name.casefold())
    lock_path = skills_root / ".hub" / "lock.json"
    if lock_path.is_file():
        names.update(
            _hermes_skill_lock_names(
                _read_json(lock_path, root, scan, "skills"),
                skills_root,
            )
        )
    return frozenset(names)


def _scan_hermes(scan: _Scan) -> None:
    roots = _hermes_roots(scan)
    _add_memory_files(
        scan,
        [
            (root / "memories" / filename, root)
            for root in roots
            for filename in ("MEMORY.md", "USER.md")
        ],
    )
    _add_instruction_files(scan, [(root / "SOUL.md", root) for root in roots])
    unsupported_memory_databases = sum(
        int(os.path.lexists(root / "memory_store.db")) for root in roots
    )
    if unsupported_memory_databases:
        scan.diagnostic(
            "memories",
            "unsupported_memory_database",
            unsupported=True,
            count=unsupported_memory_databases,
        )
    configs = _parse_configs(
        scan,
        [
            config
            for root in roots
            for config in (
                (root / "config.yaml", root, "yaml"),
                (root / "config.yml", root, "yaml"),
            )
        ],
    )
    _diagnose_unsupported_config(scan, configs)
    _add_mcp_configs(scan, configs)
    for root in roots:
        _add_skills(
            scan,
            [root / "skills"],
            excluded_parts=_HERMES_SKILL_EXCLUDED_PARTS,
            excluded_names=_hermes_managed_skill_names(scan, root),
        )
    schedule_paths = [
        path for root in roots for path in (root / "cron" / "jobs.json",) if path.is_file()
    ]
    default_timezone = ""
    for config in configs:
        timezone_value = config.get("timezone")
        if isinstance(timezone_value, str) and timezone_value:
            default_timezone = timezone_value
            break
    _add_hermes_json_schedules(
        scan,
        schedule_paths,
        scan.root,
        default_timezone=default_timezone,
    )
    settings: dict[str, Any] = {}
    for config in configs:
        _merge_missing(settings, _settings_from(config, "hermes"))
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)
