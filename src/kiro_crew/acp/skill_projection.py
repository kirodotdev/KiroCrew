"""Native Kiro launch views whose skill directory is supplied by Crew.

The authored resource mapping remains the authority for Crew search/list/read.
Native aliases preserve the other spec fields but carry no skill:// resources:
Kiro 2.21.2 progressively loads bodies, yet enumerates all their metadata before
the first prompt. Bounding only the Crew prompt cannot bound that native cost.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew.agent_discovery import SCOPE_PROJECT, _read_agent_spec, list_agents
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import kiro_agents_dir, kiro_home, project_agents_dir
from kiro_crew.hooks import safe_read_file_bytes

_MANAGED_SETTING = "kirocrew.skillDiscovery.inheritFiles"
_INHERIT_SETTING = "chat.disableInheritingDefaultResources"
_INHERIT_SOURCE = "kirocrew.skillDiscovery.inheritSource"
_PREVIOUS_INHERITANCE = "kirocrew.skillDiscovery.previousInheritance"
_SEARCH_TOOL = "@kirocrew-core/skill_search"


@dataclass
class NativeSkillProjection:
    """Translate transport identities while Crew keeps the authored agent name."""

    aliases: dict[str, str]
    specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    search_agents: set[str] = field(default_factory=set)

    def agent(self, name: str) -> str:
        if name not in self.aliases:
            if name in self.errors:
                raise ValueError(f"Agent {name!r}: {self.errors[name]}")
            raise ValueError(f"Agent {name!r} has no prepared skill discovery view")
        return self.aliases[name]

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session/set_mode":
            return {**params, "modeId": self.agent(str(params.get("modeId", "")))}
        if method == "_kiro.dev/commands/execute":
            command = params.get("command", "")
            if isinstance(command, dict):
                name = str(command.get("command", "")).lstrip("/")
                args = command.get("args") or {}
                value = str(args.get("value", "")) if isinstance(args, dict) else ""
            else:
                words = str(command).strip().lstrip("/").split(None, 1)
                name = words[0] if words else ""
                value = words[1] if len(words) > 1 else ""
            if name == "agent" and value.strip() not in {"list", "schema"}:
                raise ValueError(
                    "Use Crew's agent selector to change agents so its skill scope stays in sync."
                )
        return params

    def frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        reverse = {alias: name for name, alias in self.aliases.items()}

        def visit(value: Any, field: str = "") -> Any:
            if isinstance(value, dict):
                return {key: visit(item, key) for key, item in value.items()}
            if isinstance(value, list):
                if field == "availableModes":
                    value = [
                        item
                        for item in value
                        if isinstance(item, dict) and item.get("id") in reverse
                    ]
                return [visit(item) for item in value]
            if field in {"id", "name", "agentName", "modeId", "currentModeId"} and isinstance(
                value, str
            ):
                return reverse.get(value, value)
            return value

        return visit(frame)


def _settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = safe_read_file_bytes(str(path))
    if raw is None:
        raise ValueError(f"Cannot read Kiro settings at {path}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"Kiro settings must be an object: {path}")
    return data


def _restore_inheritance(path: Path, local: dict[str, Any]) -> None:
    """Undo only our overlay; a changed or removed native setting wins."""
    inherited = local.get(_MANAGED_SETTING)
    source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or source not in ("local", "global"):
        return
    previous = local.get(_PREVIOUS_INHERITANCE)
    if previous is None:
        # Views prepared before rollback support recorded source and a boolean.
        previous = {"present": source == "local", "value": not inherited}
    if (
        not isinstance(previous, dict)
        or not isinstance(previous.get("present"), bool)
        or (previous["present"] and "value" not in previous)
    ):
        raise ValueError(f"Cannot restore Crew's inheritance overlay at {path}")
    if local.get(_INHERIT_SETTING) is True:
        if previous["present"]:
            local[_INHERIT_SETTING] = previous["value"]
        else:
            local.pop(_INHERIT_SETTING, None)
    for key in (_MANAGED_SETTING, _INHERIT_SOURCE, _PREVIOUS_INHERITANCE):
        local.pop(key, None)
    atomic_write(path, json.dumps(local, indent=2))


def prepare_native_skill_projection(
    work_dir: Path, *, enabled: bool | None = None
) -> NativeSkillProjection | None:
    """Prepare native views after spec freshness admission, before spawning.

    Uses the existing workspace CLI settings channel. No home, identity store,
    session store or authored agent file is relocated or rewritten.
    """
    directory = kiro_agents_dir()
    workspace_settings = work_dir / ".kiro" / "settings" / "cli.json"
    local = _settings(workspace_settings)
    if enabled is None:
        enabled = os.environ.get("KIROCREW_NATIVE_SKILL_PROJECTION", "1") != "0"
    if not enabled:
        _restore_inheritance(workspace_settings, local)
        return None
    global_settings = _settings(kiro_home() / "settings" / "cli.json")
    inherited = local.get(_MANAGED_SETTING)
    preference_source = local.get(_INHERIT_SOURCE)
    if not isinstance(inherited, bool) or local.get(_INHERIT_SETTING) is not True:
        local[_PREVIOUS_INHERITANCE] = {
            "present": _INHERIT_SETTING in local,
            "value": local.get(_INHERIT_SETTING),
        }
        preference_source = "local" if _INHERIT_SETTING in local else "global"
        inherited = local.get(_INHERIT_SETTING, global_settings.get(_INHERIT_SETTING)) is not True
    elif preference_source == "global":
        inherited = global_settings.get(_INHERIT_SETTING) is not True
    aliases: dict[str, str] = {}
    specs: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    search_agents: set[str] = set()
    for agent in list_agents(project_dir=str(work_dir)):
        if not agent.filename:
            continue
        source_dir = (
            project_agents_dir(str(work_dir)) if agent.scope == SCOPE_PROJECT else directory
        )
        source = source_dir / agent.filename
        spec = _read_agent_spec(source, operation="native_skill_projection", source="acp")
        if spec is None:
            continue
        identity = f"{work_dir.absolute()}\n{agent.name}"
        alias = NATIVE_SKILL_ALIAS_PREFIX + hashlib.sha256(identity.encode()).hexdigest()[:24]
        view = copy.deepcopy(spec)
        view["name"] = alias
        resources = view.get("resources", [])
        resources = resources if isinstance(resources, list) else []
        view["resources"] = [
            r for r in resources if not (isinstance(r, str) and r.startswith("skill://"))
        ]
        needs_search = agent.name == "kirocrew" or any(
            isinstance(r, str) and r.startswith("skill://") for r in resources
        )
        if needs_search:
            excluded = view.get("excludedTools", [])
            if isinstance(excluded, list) and any(
                isinstance(t, str)
                and (t == "@kirocrew-core" or fnmatch.fnmatchcase(_SEARCH_TOOL, t))
                for t in excluded
            ):
                errors[agent.name] = (
                    "skill_search is explicitly excluded; bounded skill discovery requires it"
                )
                continue
            # The bounded directory must have a loading path even for a custom spec
            # whose authored resources rely on native skill activation. Expose
            # only the read/search capability; do not grant server-wide tools or
            # change the author's approval policy.
            from kiro_crew.agent import managed_mcp_spec_entry

            servers = view.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                errors[agent.name] = "mcpServers must be an object"
                continue
            original_core = servers.get("kirocrew-core", {})
            if not isinstance(original_core, dict):
                errors[agent.name] = "kirocrew-core must be a server object"
                continue
            disabled = original_core.get("disabled", False)
            disabled_tools = original_core.get("disabledTools", [])
            if not isinstance(disabled, bool):
                errors[agent.name] = "kirocrew-core.disabled must be a boolean"
                continue
            if not isinstance(disabled_tools, list) or any(
                not isinstance(tool, str) for tool in disabled_tools
            ):
                errors[agent.name] = "kirocrew-core.disabledTools must be a list of strings"
                continue
            if disabled or "skill_search" in disabled_tools:
                errors[agent.name] = "skill_search is disabled; bounded skill discovery requires it"
                continue
            entry = managed_mcp_spec_entry("kirocrew-core")
            if entry is None:
                errors[agent.name] = "Crew's managed skill search server is unavailable"
                continue
            for key in ("autoApprove", "disabledTools", "timeout"):
                if key in original_core:
                    entry[key] = original_core[key]
            servers["kirocrew-core"] = entry
            tools = view.get("tools", [])
            if tools != "*" and isinstance(tools, list):
                if not any(t in tools for t in ("*", "@kirocrew-core", _SEARCH_TOOL)):
                    view["tools"] = [*tools, _SEARCH_TOOL]
            search_agents.add(agent.name)
        if inherited:
            for resource in (
                f"file://{kiro_home().as_posix()}/steering/**/*.md",
                "file://.kiro/steering/**/*.md",
                "file://AGENTS.md",
            ):
                if resource not in view["resources"]:
                    view["resources"].append(resource)
        prompt = view.get("prompt")
        if isinstance(prompt, str) and prompt.startswith("file://"):
            path = Path(prompt[7:]).expanduser()
            if not path.is_absolute():
                view["prompt"] = "file://" + (source.parent / path).absolute().as_posix()
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write(
            directory / f"{alias}.json",
            json.dumps(view, ensure_ascii=False),
            restrict_to_owner=True,
        )
        aliases[agent.name] = alias
        specs[agent.name] = view
    local[_MANAGED_SETTING] = inherited
    local[_INHERIT_SOURCE] = preference_source
    local[_INHERIT_SETTING] = True
    workspace_settings.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(workspace_settings, json.dumps(local, indent=2))
    return NativeSkillProjection(aliases, specs, errors, search_agents)
