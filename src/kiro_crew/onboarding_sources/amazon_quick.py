"""Amazon Quick adapter: ``~/.quickwork`` (``%USERPROFILE%\\.quickwork`` on Windows).

The Amazon Quick desktop app keeps its local configuration under that root and
keeps conversations, memory and the knowledge graph in the user's Quick account,
so there is no local memory to read. It keeps one directory per signed-in profile
under ``profiles/``; ``profiles.json`` names the profile the app opens with
(``last_active``) and where each profile lives (``data_path``, relative to the
root). Older installs kept a single profile's files at the root itself, so the
root is read as one more profile, last.

From each profile directory, the profile the app opens with first:

- ``mcp_config.json``: MCP servers, in the standard ``mcpServers`` map with the
  app's own per-entry metadata under ``_quick``, or in an older ``servers`` list.
  Only the app's bookkeeping is dropped; every other field reaches the shared
  MCP projection, so a server carrying a tool filter, a working directory, a
  credential or a field the projection does not know is refused exactly as it
  is for any other source. Each server keeps its on/off state, so it runs in
  Kiro Crew exactly when it ran in Quick.
- ``skills/<name>/SKILL.md``: skill packages.
- ``user_preferences.json``: ``custom_prompt``, the preferences the user asked
  Quick to apply to every conversation, becomes instructions; ``user_timezone``
  becomes the timezone setting and the zone of a scheduled task that names none.
- ``agents.json``: scheduled tasks. Only a prompt on a schedule is imported.
- ``acp_agents.json``: coding-agent connections have no destination and are
  reported.

``profiles.json`` also holds sign-in metadata; only ``last_active`` and each
entry's ``id`` and ``data_path`` are read from it.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from heapq import nsmallest
from pathlib import Path, PureWindowsPath
from typing import Any

# The module, not its names: ``_is_link_like`` is a patch seam that lives only in
# that module, so it is read off it at call time and a patch on
# ``onboarding_import._is_link_like`` reaches this module too.
from kiro_crew import onboarding_scan
from kiro_crew.onboarding_plan import (
    _MAX_SCHEDULES,
    _add_instruction_text,
    _add_mcp_configs,
    _add_skills,
    _merge_missing,
    _schedule_from_record,
    _settings_from,
)
from kiro_crew.onboarding_scan import _is_dir_safe, _is_file_safe, _read_json, _Scan

#: Profile directories read under ``profiles/`` besides the active one; past this
#: the rest are reported.
_MAX_PROFILES = 50

#: What the ``mcpServers`` map keeps beside each server's connection: a
#: description, and under ``_quick`` the app's own bookkeeping for the entry,
#: such as its display name, where a copied server came from, startup and
#: loading preferences, and a marker on a server the app switched off after it
#: failed to start. None of it limits what the server can do, so it is dropped.
#: The app reads on/off from ``disabled`` alone and writes that marker only
#: beside ``disabled: true``, so dropping it cannot switch a server on.
_MAP_APP_FIELDS = frozenset({"_quick", "description"})

#: ``transport`` is checked separately and dropped only when it names what the
#: command or URL already implies. The remaining fields are the same bookkeeping
#: the map keeps under ``_quick``; none changes how a server is launched or reached.
#: The list's on/off switch is ``enabled``.
_LIST_APP_FIELDS = frozenset(
    {
        "id",
        "name",
        "description",
        "transport",
        "source",
        "upstream_id",
        "startupTimeout",
        "host",
        "defer",
        "instructions",
        "trigger",
        "patterns",
        "autoDisabled",
        "managed_by",
    }
)

#: ``HH:MM``, the wall-clock form of a daily or weekly task.
_CLOCK_RE = re.compile(r"^(?P<hour>[01]?[0-9]|2[0-3]):(?P<minute>[0-5][0-9])$")

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

#: Instruction items are keyed by this origin rather than by profile, so the same
#: preferences saved in two profiles are offered once.
_PREFERENCES_ORIGIN = "user_preferences.json#custom_prompt"

#: Task fields whose value binds a run to something a Kiro Crew schedule cannot
#: carry: conditions that gate each run, a conversation or chat agent the run
#: continues, preferred connector accounts, and skills resolved ahead of the run.
#: A task that sets any of them is reported, never narrowed into a plain prompt.
_BOUND_TASK_FIELDS = (
    "triggers",
    "conversation_id",
    "chat_agent_id",
    "connector_preferences",
    "pre_resolved_skills",
)


def _scan_amazon_quick(scan: _Scan) -> None:
    profiles = _profile_roots(scan)
    _add_mcp_configs(
        scan,
        [config for root in profiles if (config := _mcp_config(scan, root)) is not None],
        keep_enabled_state=True,
    )
    _add_skills(scan, [root / "skills" for root in profiles])
    settings: dict[str, Any] = {}
    tasks_past_limit = 0
    for root in profiles:
        preferences = _read_object(scan, root / "user_preferences.json", "settings")
        custom_prompt = preferences.get("custom_prompt")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            _add_instruction_text(scan, custom_prompt, _PREFERENCES_ORIGIN)
        timezone_value = preferences.get("user_timezone")
        profile_settings = (
            _settings_from({"timezone": timezone_value}, scan.source_id)
            if isinstance(timezone_value, str)
            else {}
        )
        _merge_missing(settings, profile_settings)
        tasks_past_limit += _add_tasks(
            scan, root, default_timezone=profile_settings.get("timezone", "")
        )
    if tasks_past_limit:
        # Said once for the whole scan, with every profile's overflow counted.
        scan.diagnostic("schedules", "item_count_limit", count=tasks_past_limit)
    if any(_has_coding_agents(scan, root) for root in profiles):
        scan.diagnostic("agents", "unsupported_category", unsupported=True)
    if settings:
        scan.add("settings", json.dumps(settings, sort_keys=True), settings)


def _profile_roots(scan: _Scan) -> list[Path]:
    """Every profile directory to read: the active one, the rest by name, the root."""
    roots: list[Path] = []
    seen: set[str] = set()

    def safe(candidate: Path) -> bool:
        return _is_dir_safe(candidate) and not _reached_through_link(scan.root, candidate)

    def keep(candidate: Path | None) -> None:
        if candidate is None or not safe(candidate):
            return
        marker = os.path.normcase(os.path.abspath(str(candidate)))
        if marker not in seen:
            seen.add(marker)
            roots.append(candidate)

    keep(_active_profile_root(scan))
    profiles = scan.root / "profiles"
    if _is_dir_safe(profiles) and not onboarding_scan._is_link_like(profiles):
        unread_profiles = 0

        def candidates() -> Iterator[Path]:
            """Safe profile directories not already kept, so each one takes a slot of the cap."""
            nonlocal unread_profiles
            for child in profiles.iterdir():
                if safe(child):
                    marker = os.path.normcase(os.path.abspath(str(child)))
                    if marker not in seen:
                        unread_profiles += 1
                        yield child

        try:
            children = nsmallest(
                _MAX_PROFILES, candidates(), key=lambda path: (path.name.casefold(), path.name)
            )
        except OSError:
            scan.diagnostic("profiles", "read_failed")
        else:
            for child in children:
                keep(child)
            omitted = unread_profiles - len(children)
            if omitted:
                scan.diagnostic("profiles", "profile_count_limit", count=omitted)
    keep(scan.root)
    return roots


def _active_profile_root(scan: _Scan) -> Path | None:
    """The directory of the profile ``profiles.json`` marks as last active."""
    manifest = _read_object(scan, scan.root / "profiles.json", "settings")
    active = manifest.get("last_active")
    entries = manifest.get("entries")
    if not isinstance(active, str) or not active or not isinstance(entries, list):
        return None
    # The manifest is byte-capped by the reader; only one matching entry is retained, with no tail.
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("id") != active:
            continue
        data_path = entry.get("data_path")
        if data_path is None or data_path == "":
            # A profile with no data path of its own lives at the root.
            return scan.root
        if not isinstance(data_path, str):
            return None
        return _contained_path(scan.root, data_path)
    return None


def _contained_path(root: Path, relative: str) -> Path | None:
    """*relative* joined under *root*, or None when it could name a path outside it.

    The manifest may have been written on either platform, so *relative* is read
    with Windows path rules, which accept both separators and recognize a rooted
    path or a network share; a POSIX relative path parses the same way. A colon,
    which only a drive or a stream name carries, and a part made only of dots
    are refused too.
    """
    if "\x00" in relative or ":" in relative:
        return None
    parsed = PureWindowsPath(relative)
    if parsed.anchor or not parsed.parts:
        return None
    if any(not part.strip(". ") for part in parsed.parts):
        return None
    return root.joinpath(*parsed.parts)


def _reached_through_link(root: Path, candidate: Path) -> bool:
    """True when *candidate* is not under *root*, or a directory on the way is a link."""
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if onboarding_scan._is_link_like(current):
            return True
    return False


def _read_object(scan: _Scan, path: Path, category: str) -> dict[str, Any]:
    """A JSON object read through the bounded, symlink-safe reader, or ``{}``."""
    if not _is_file_safe(path):
        return {}
    data = _read_json(path, scan.root, scan, category)
    return data if isinstance(data, dict) else {}


def _mcp_config(scan: _Scan, root: Path) -> dict[str, Any] | None:
    """One profile's connection fields, keyed by each server's own Quick id.

    File order is preserved. A repeated id in the older ``servers`` list keeps
    its first entry, matching the engine's first-wins order across profiles.
    """
    raw = _read_object(scan, root / "mcp_config.json", "mcp_servers")
    servers = raw.get("mcpServers")
    entries: list[tuple[Any, Any, Any]]
    if isinstance(servers, dict):
        app_fields = _MAP_APP_FIELDS
        entries = [
            (server_id, entry, entry.get("_quick") if isinstance(entry, dict) else None)
            for server_id, entry in servers.items()
        ]
    elif isinstance(raw.get("servers"), list):
        # The older list form keeps the app's metadata beside the connection fields.
        app_fields = _LIST_APP_FIELDS
        entries = [
            (entry.get("id") if isinstance(entry, dict) else None, entry, entry)
            for entry in raw["servers"]
        ]
    else:
        return None
    projected: dict[str, Any] = {}
    for server_id, entry, metadata in entries:
        if not isinstance(server_id, str) or not server_id:
            scan.diagnostic("mcp_servers", "invalid_server_name")
            continue
        if not isinstance(entry, dict):
            # Passed through for the shared projection to report.
            projected.setdefault(server_id, entry)
            continue
        metadata = metadata if isinstance(metadata, dict) else {}
        if metadata.get("managed_by"):
            # An entry the app manages itself, not one the user added.
            scan.diagnostic("mcp_servers", "managed_server_excluded")
            continue
        if app_fields is _LIST_APP_FIELDS and "transport" in entry:
            transport = entry["transport"]
            normalized = (
                re.sub(r"[-_ ]", "", transport.casefold()) if isinstance(transport, str) else ""
            )
            has_url = bool(entry.get("url") or entry.get("serverUrl"))
            if normalized not in (("http", "streamablehttp") if has_url else ("stdio",)):
                scan.diagnostic("mcp_servers", "unsupported_mcp_schema", unsupported=True)
                continue
        projected.setdefault(server_id, _connection(entry, app_fields))
    return {"mcpServers": projected}


def _connection(entry: dict[str, Any], app_fields: frozenset[str]) -> dict[str, Any]:
    """*entry* without the app's bookkeeping (*app_fields*), for the shared projection.

    Every other field passes through, so the projection decides what a server
    may carry and refuses one with a tool filter, a working directory or a
    field it does not know, the same as for any other source. The on/off switch
    travels as written: ``disabled`` in the ``mcpServers`` map, ``enabled`` in
    the older list.

    The entry is read the way the app reads it. With a URL it is a remote
    server, so a command and arguments beside it are dropped, and ``serverUrl``,
    the spelling some tools write, counts as ``url``; with no URL it runs its
    command, so an empty ``url`` is dropped. An empty ``env`` or ``headers`` map
    is dropped too, since the field name alone scores as a credential. A
    populated one stays, so the projection refuses the server and counts what
    it held rather than importing it without values it needs.
    """
    connection = {key: value for key, value in entry.items() if key not in app_fields}
    url = connection.pop("url", None)
    server_url = connection.pop("serverUrl", None)
    if url or server_url:
        connection["url"] = url or server_url
        connection.pop("command", None)
        connection.pop("args", None)
    for key in ("env", "headers"):
        if key in connection and connection[key] in (None, {}, []):
            del connection[key]
    return connection


def _add_tasks(scan: _Scan, root: Path, *, default_timezone: str) -> int:
    """Offer one profile's scheduled tasks; return how many lay past the limit.

    Only the first ``_MAX_SCHEDULES`` records are read. The caller reports the
    rest, so a cut-off list does not read like one that ended there. The count
    is of records the limit cut off: an unread record may be one of the app's
    hidden tasks or a copy of another profile's, and nothing here can tell.
    """
    records = _read_object(scan, root / "agents.json", "schedules").get("agents")
    if records is None:
        return 0
    if not isinstance(records, list):
        scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
        return 0
    for record in records[:_MAX_SCHEDULES]:
        payload = _task_schedule(record, scan, default_timezone=default_timezone)
        if payload is not None:
            scan.add("schedules", json.dumps(payload, sort_keys=True), payload)
    return max(0, len(records) - _MAX_SCHEDULES)


def _task_schedule(record: Any, scan: _Scan, *, default_timezone: str) -> dict[str, Any] | None:
    """The engine schedule for one scheduled task, or None when it is not portable."""
    if not isinstance(record, dict):
        scan.diagnostic("schedules", "unsupported_schedule_schema", unsupported=True)
        return None
    if record.get("visible") is False:
        # The app's own background tasks are hidden from the user's task list;
        # they are not something the user made.
        return None
    # Only ``monitor`` tasks are imported; any other type is reported.
    if record.get("type", "monitor") != "monitor" or any(
        record.get(field) not in (None, "", [], {}) for field in _BOUND_TASK_FIELDS
    ):
        scan.diagnostic("schedules", "unsupported_schedule_semantics", unsupported=True)
        return None
    prompt = record.get("prompt_override")
    if not isinstance(prompt, str) or not prompt.strip():
        # The task runs a prompt that ships inside the app, not one in the task.
        scan.diagnostic("schedules", "unsupported_schedule_semantics", unsupported=True)
        return None
    schedule, reason = _schedule_spec(record.get("schedule"), default_timezone)
    if schedule is None:
        scan.diagnostic("schedules", reason, unsupported=True)
        return None
    projected: dict[str, Any] = {"prompt": prompt, "schedule": schedule}
    name = record.get("name")
    if isinstance(name, str) and name.strip():
        projected["name"] = name
    return _schedule_from_record(projected, scan)


def _schedule_spec(raw: Any, default_timezone: str) -> tuple[dict[str, Any] | None, str]:
    """A task's schedule in the engine's spec vocabulary, else None and the reason.

    A wall-clock time carries no zone of its own: the app reads it in the task's
    ``timezone`` and, without one, in the user's timezone setting. With neither,
    the import refuses to guess.
    """
    if not isinstance(raw, dict):
        return None, "unsupported_schedule_schema"
    kind = raw.get("type", "interval")
    zone = raw.get("timezone") or default_timezone
    if kind == "interval":
        minutes = raw.get("minutes")
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
            return None, "unsupported_schedule_schema"
        return {"kind": "interval", "minutes": minutes}, ""
    if kind in ("time_of_day", "cron_like"):
        clock_value = raw.get("time")
        clock = _CLOCK_RE.match(clock_value) if isinstance(clock_value, str) else None
        if clock is None:
            return None, "unsupported_schedule_schema"
        days = "*"
        if kind == "cron_like":
            raw_days = raw.get("days")
            if (
                not isinstance(raw_days, list)
                or not raw_days
                or not all(isinstance(day, str) and day.casefold() in _WEEKDAYS for day in raw_days)
            ):
                return None, "unsupported_schedule_schema"
            wanted = {day.casefold() for day in raw_days}
            days = ",".join(day for day in _WEEKDAYS if day in wanted)
        if not zone:
            return None, "timezone_required"
        expr = f"{int(clock.group('minute'))} {int(clock.group('hour'))} * * {days}"
        return {"kind": "cron", "expr": expr, "timezone": zone}, ""
    if kind == "run_once":
        at = raw.get("at")
        if not isinstance(at, str) or not at.strip():
            return None, "unsupported_schedule_schema"
        if not zone:
            return None, "timezone_required"
        return {"kind": "once", "run_at": at.strip(), "timezone": zone}, ""
    return None, "unsupported_schedule_semantics"


def _has_coding_agents(scan: _Scan, root: Path) -> bool:
    agents = _read_object(scan, root / "acp_agents.json", "agents").get("agents")
    return isinstance(agents, list) and bool(agents)
