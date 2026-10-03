"""The registered-action catalog behind the ``kirocrew-guide`` UI guide.

An agent asks the dashboard to walk the human through ONE of a small, fixed set
of actions. It names the action and fills in its parameters; it never supplies a
route, a selector, markup or code. Everything the browser does with a guide is
derived here, on the gateway, from data this build ships:

* ``settings.show`` points at one registered setting. The id must be in the
  packaged ``settings-registry.generated.json`` and the route comes ONLY from that
  registry entry. Credential and security-ceiling controls are not in the guidance
  catalog at all, so an agent cannot even point at them.
* ``crewmate.create`` pre-fills the create-a-crewmate flow. The crewmate is created
  only by the human's own click on the existing owner-only ``POST /api/agents``.
* ``mcp.open_add`` points at the existing MCP servers tab and its Add Custom
  button. It pre-fills nothing and saves nothing: it completes when the user
  reaches the existing add form, never when a server is installed.

Each action is an ordered list of steps. A ``ui`` step may be advanced by the
owning browser tab reporting that it observed the target; a ``commit`` step is
advanced ONLY by the gateway itself, after the real mutation route returned
success (see :mod:`kiro_crew.dashboard.guide_runs`). A client never reports the
success of a mutation.

This module is pure: validation only, no I/O except reading the packaged registry
once.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ACTION_SETTINGS_SHOW = "settings.show"
ACTION_CREWMATE_CREATE = "crewmate.create"
ACTION_MCP_OPEN_ADD = "mcp.open_add"

#: A guide carries between one and this many ordered actions.
MAX_ACTIONS = 8

#: Longest guide id the gateway issues or accepts back.
GUIDE_ID_MAX_LEN = 64

#: Byte ceiling on the serialized ``actions`` list one start may carry.
MAX_ACTIONS_BYTES = 64 * 1024

STEP_UI = "ui"
STEP_COMMIT = "commit"

#: The create-a-crewmate form's own field caps (``MeetCrewmatesFlow.tsx``
#: ``JOB_MAX`` / ``NAME_MAX``). A pre-fill longer than the field it lands in is a
#: draft the user can see but not reproduce or edit back to, so a guide never
#: proposes one.
_GOAL_MAX_CHARS = 200
_NAME_MAX_CHARS = 24
_SETTING_ID_MAX_CHARS = 200

_REGISTRY_PATH = Path(__file__).resolve().parent / "docs" / "settings-registry.generated.json"

#: Settings tabs whose every control is a credential or a security ceiling. The
#: guide never points at them: a guide is an agent steering the human's attention,
#: and these are exactly the controls where an agent-chosen nudge is the risk.
_EXCLUDED_SETTING_TABS = frozenset(
    {"security", "secrets", "connections", "computer-use", "instances"}
)

#: Individual controls the dashboard's own guide registry (``guideActions.ts``
#: ``SENSITIVE_IDS``) refuses and the segment rule below does not reach. Kept a
#: superset of the browser's list, so a guide the gateway accepts is never one the
#: page then refuses to show.
_EXCLUDED_SETTING_IDS = frozenset(
    {
        "developer.remote-crew-sessions",
        "skills.require-approval-before-generated-skills-go-live",
    }
)

#: Id segments that mark a credential field or an access-control / trust ceiling
#: (who may reach the agent, auto-approval, remote reach) anywhere else. Matched
#: against whole ``-``/``.``/``_`` separated segments of the id and config key, so
#: this is a catalog exclusion over packaged data, not a secret detector.
_EXCLUDED_SETTING_SEGMENTS = frozenset(
    {
        "token",
        "secret",
        "password",
        "key",
        "credential",
        "credentials",
        "client",
        "allowed",
        "who",
        "owner",
        "autopilot",
        "approve",
        "yolo",
        "trust",
        "remote",
    }
)

_SEGMENT_SPLIT = re.compile(r"[-._%:]+")


class GuideCatalogError(ValueError):
    """A guide request that names an unknown action or carries bad parameters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class StepDef:
    key: str
    kind: str


@dataclass(frozen=True)
class ActionDef:
    id: str
    title: str
    description: str
    steps: tuple[StepDef, ...]
    params_schema: dict[str, Any]
    mutates: bool


ACTIONS: dict[str, ActionDef] = {
    ACTION_SETTINGS_SHOW: ActionDef(
        id=ACTION_SETTINGS_SHOW,
        title="Show a setting",
        description=(
            "Open Settings at one registered setting and point at it. The user "
            "changes it themselves; you are told only that they reached it, never "
            "its value. Credential and security controls cannot be shown."
        ),
        steps=(StepDef("show", STEP_UI),),
        params_schema={
            "type": "object",
            "properties": {
                "setting_id": {
                    "type": "string",
                    "maxLength": _SETTING_ID_MAX_CHARS,
                    "description": "A registered setting id, e.g. 'chat.response-verbosity'.",
                }
            },
            "required": ["setting_id"],
            "additionalProperties": False,
        },
        mutates=False,
    ),
    ACTION_CREWMATE_CREATE: ActionDef(
        id=ACTION_CREWMATE_CREATE,
        title="Create a crewmate",
        description=(
            "Open the create-a-crewmate flow with an optional name and goal "
            "pre-filled, then point at the goal, the name and the Create button. "
            "Nothing is created until the user clicks Create; completion reports "
            "the new crewmate's id."
        ),
        steps=(
            StepDef("goal", STEP_UI),
            StepDef("name", STEP_UI),
            StepDef("create", STEP_COMMIT),
        ),
        params_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "maxLength": _NAME_MAX_CHARS},
                "goal": {"type": "string", "maxLength": _GOAL_MAX_CHARS},
            },
            "additionalProperties": False,
        },
        mutates=True,
    ),
    ACTION_MCP_OPEN_ADD: ActionDef(
        id=ACTION_MCP_OPEN_ADD,
        title="Open the add-MCP-server page",
        description=(
            "Take the user to the existing MCP servers tab and point at its Add "
            "Custom button. Nothing is pre-filled or saved: the user fills in and "
            "saves the existing form themselves. Completion means the add form was "
            "reached, never that a server was installed."
        ),
        steps=(StepDef("servers-tab", STEP_UI), StepDef("add", STEP_UI)),
        params_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        mutates=False,
    ),
}


def step_count(action_id: str) -> int:
    return len(ACTIONS[action_id].steps)


def step_kind(action_id: str, step_index: int) -> str:
    return ACTIONS[action_id].steps[step_index].kind


def commit_step_index(action_id: str) -> int | None:
    """Index of the action's final mutation step, or ``None`` for a UI-only action."""
    steps = ACTIONS[action_id].steps
    for index, step in enumerate(steps):
        if step.kind == STEP_COMMIT:
            return index
    return None


def list_actions() -> list[dict[str, Any]]:
    """The catalog as the agent sees it."""
    return [
        {
            "id": a.id,
            "title": a.title,
            "description": a.description,
            "params_schema": a.params_schema,
            "mutates": a.mutates,
            "step_count": len(a.steps),
        }
        for a in ACTIONS.values()
    ]


# ── settings registry ──


def _segments(value: str) -> set[str]:
    return {s for s in _SEGMENT_SPLIT.split(value.lower()) if s}


def _setting_is_guidable(entry: dict[str, Any]) -> bool:
    tab = str(entry.get("tab") or "")
    if tab in _EXCLUDED_SETTING_TABS or entry.get("id") in _EXCLUDED_SETTING_IDS:
        return False
    words = _segments(str(entry.get("id") or "")) | _segments(str(entry.get("configKey") or ""))
    return not (words & _EXCLUDED_SETTING_SEGMENTS)


def _route_is_internal_settings_path(route: str) -> bool:
    return (
        route.startswith("/settings/")
        and "//" not in route
        and "\\" not in route
        and not any(ord(ch) < 0x21 or ch == "\x7f" for ch in route)
    )


@functools.lru_cache(maxsize=1)
def guidable_settings() -> dict[str, dict[str, str]]:
    """Packaged setting id -> ``{id, label, tab, route}``, minus excluded controls.

    Read once from the file this build ships; it is static data, not caller state.
    An unreadable registry yields an empty catalog (every ``settings.show`` is then
    refused as unknown) rather than a guess.
    """
    try:
        payload = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
        entries = payload["settings"]
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("settings registry unreadable; settings.show is unavailable")
        return {}
    out: dict[str, dict[str, str]] = {}
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sid, route = entry.get("id"), entry.get("route")
        if not isinstance(sid, str) or not isinstance(route, str):
            continue
        if not _route_is_internal_settings_path(route) or not _setting_is_guidable(entry):
            continue
        out[sid] = {
            "id": sid,
            "label": str(entry.get("label") or ""),
            "tab": str(entry.get("tab") or ""),
            "route": route,
        }
    return out


# ── parameter validation ──


def _redacts(text: str) -> bool:
    """True when the existing credential redactor would change *text*."""
    from kiro_crew.platform import redact_via_context

    return redact_via_context(text) != text


def _has_control_chars(text: str, *, allow_newlines: bool = False) -> bool:
    allowed = {"\n", "\t"} if allow_newlines else set()
    return any((ord(ch) < 0x20 or ch == "\x7f") and ch not in allowed for ch in text)


def _exact_keys(params: dict[str, Any], allowed: set[str], action_id: str) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise GuideCatalogError(
            "invalid_params", f"{action_id}: unknown parameter '{unknown[0][:64]}'"
        )


def _validate_settings_show(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    _exact_keys(params, {"setting_id"}, ACTION_SETTINGS_SHOW)
    sid = params.get("setting_id")
    if not isinstance(sid, str) or not sid or len(sid) > _SETTING_ID_MAX_CHARS:
        raise GuideCatalogError("invalid_params", "settings.show: setting_id is required")
    entry = guidable_settings().get(sid)
    if entry is None:
        raise GuideCatalogError(
            "unknown_setting",
            "settings.show: that setting id is not in the guidable settings catalog",
        )
    return {"setting_id": sid}, {"route": entry["route"], "label": entry["label"]}


def _validate_crewmate_create(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from kiro_crew.members import MemberNameError, validate_member_name

    _exact_keys(params, {"name", "goal"}, ACTION_CREWMATE_CREATE)
    out: dict[str, Any] = {}
    name = params.get("name")
    if name is not None and name != "":
        if not isinstance(name, str) or len(name) > _NAME_MAX_CHARS:
            raise GuideCatalogError(
                "invalid_params",
                f"crewmate.create: name must be text of at most {_NAME_MAX_CHARS} characters",
            )
        try:
            validate_member_name(name)
        except MemberNameError as exc:
            raise GuideCatalogError("invalid_params", f"crewmate.create: {exc}") from None
        if _redacts(name):
            raise GuideCatalogError(
                "credential_shaped", "crewmate.create: name looks like a credential"
            )
        out["name"] = name
    goal = params.get("goal")
    if goal is not None and goal != "":
        if not isinstance(goal, str) or len(goal) > _GOAL_MAX_CHARS:
            raise GuideCatalogError(
                "invalid_params",
                f"crewmate.create: goal must be text of at most {_GOAL_MAX_CHARS} characters",
            )
        if _has_control_chars(goal, allow_newlines=True):
            raise GuideCatalogError(
                "invalid_params", "crewmate.create: goal has control characters"
            )
        if _redacts(goal):
            raise GuideCatalogError(
                "credential_shaped", "crewmate.create: goal contains a credential-shaped value"
            )
        out["goal"] = goal
    return out, {}


def _validate_mcp_open_add(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    _exact_keys(params, set(), ACTION_MCP_OPEN_ADD)
    return {}, {}


_VALIDATORS = {
    ACTION_SETTINGS_SHOW: _validate_settings_show,
    ACTION_CREWMATE_CREATE: _validate_crewmate_create,
    ACTION_MCP_OPEN_ADD: _validate_mcp_open_add,
}


def validate_actions(raw: object) -> list[dict[str, Any]]:
    """Validate a start request's ``actions`` list into the stored action records.

    Each record is ``{id, params, step_count}`` plus the server-derived fields an
    action carries (``route``/``label`` for ``settings.show``). Raises
    :class:`GuideCatalogError` on the first problem; nothing is partially accepted.
    """
    if not isinstance(raw, list) or not raw:
        raise GuideCatalogError("invalid_actions", "actions must be a non-empty list")
    if len(raw) > MAX_ACTIONS:
        raise GuideCatalogError("invalid_actions", f"at most {MAX_ACTIONS} actions per guide")
    try:
        size = len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise GuideCatalogError("invalid_actions", "actions must be JSON data") from None
    if size > MAX_ACTIONS_BYTES:
        raise GuideCatalogError("invalid_actions", "actions payload is too large")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GuideCatalogError("invalid_actions", f"action {index} must be an object")
        extra = sorted(set(item) - {"id", "params"})
        if extra:
            raise GuideCatalogError(
                "invalid_actions", f"action {index}: unknown field '{extra[0][:64]}'"
            )
        action_id = item.get("id")
        if not isinstance(action_id, str) or action_id not in ACTIONS:
            raise GuideCatalogError("unknown_action", f"action {index}: unknown action id")
        params = item.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise GuideCatalogError("invalid_params", f"action {index}: params must be an object")
        clean, derived = _VALIDATORS[action_id](params)
        record: dict[str, Any] = {
            "id": action_id,
            "params": clean,
            "step_count": step_count(action_id),
        }
        record.update(derived)
        out.append(record)
    return out
