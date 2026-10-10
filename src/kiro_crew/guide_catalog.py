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
* ``ui.show`` points at one indexed UI location (``chat.older-sessions``). The id
  must carry a ``guide_plan`` in the packaged ``ui-index.generated.json``; the
  generator emits one only for an eligible location (curated, no runtime
  condition the guide cannot walk, not under the agent's own ceiling; a
  destructive one carries ``caution: true`` on its last step), and its steps
  come ONLY from that plan: a
  version-2 plan lists, per placement (viewport), step ids. The claiming tab
  names the placement it walks and the gateway records that placement's step
  ids (:func:`claim_placement`); progress may name only those. It only points:
  every step is a ``ui`` step and nothing is clicked or saved. The record also
  carries the index's ``build_digest``; a tab whose bundle carries another
  refuses the guide (``build_mismatch``) instead of walking it. An AUTO
  location (``auto:<page>:<label key>``) is accepted only when the build-time
  auto tier the dashboard bundle ships (:func:`ui_auto_tier`) hangs off this
  very index and gave it a guidable ``guide_policy`` (``point``, or
  ``caution`` for a destructive control) and a single-step plan (one placement,
  or one per page for a control several pages share, every one pointing at
  the same site); its record carries that plan and the auto tier's own digest,
  which only a bundle stamped by the same build carries. Without the file,
  ``unknown_location``. A plan's last step may carry ``caution: true``: the page
  then shows the destructive-control warning and never finishes that step on a
  press of the control alone.
* ``ui.find`` points at a control by its on-screen NAME on one page: ``route``
  (a page this build's index knows, never a trust-root page), ``label`` (the
  name in the language the dashboard shows), an optional ``role`` and an
  optional ``container`` hint. The tab searches its visible controls for that
  accessible name and, when none matches, may open side-effect-free
  containers (menus, popovers, disclosures, unselected tabs) to look inside,
  restoring each before the next. It has two fixed steps: ``open`` (point at
  the container that holds the control; passed at once when the control is
  already visible) and ``show`` (point at the control). The tab reports only
  what it found (:func:`clean_find_report`: found / ambiguous / none, a
  count, a role, a registered id or label key); page text never leaves it.

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
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Generic, Iterable, TypeVar
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

ACTION_SETTINGS_SHOW = "settings.show"
ACTION_CREWMATE_CREATE = "crewmate.create"
ACTION_MCP_OPEN_ADD = "mcp.open_add"
ACTION_UI_SHOW = "ui.show"
ACTION_UI_FIND = "ui.find"

#: A guide carries between one and this many ordered actions.
MAX_ACTIONS = 8

#: Byte ceiling on the serialized ``actions`` list one start may carry.
MAX_ACTIONS_BYTES = 64 * 1024

STEP_UI = "ui"
STEP_COMMIT = "commit"

#: Caps on a guide's pre-filled name and goal: a proposal short enough to read
#: at a glance in the New crewmate card's Name and "What it looks after"
#: fields, which accept at least this much (they set no shorter limit).
_GOAL_MAX_CHARS = 200
_NAME_MAX_CHARS = 24
_SETTING_ID_MAX_CHARS = 200
_LOCATION_ID_MAX_CHARS = 120
#: Ceiling on one ``ui.show`` plan's steps; the generator's ``GUIDE_MAX_STEPS``.
_UI_SHOW_MAX_STEPS = 7
#: The ``ui.show`` plan format this gateway walks (the generator's ``GUIDE_PLAN_VERSION``).
UI_SHOW_PLAN_VERSION = 2
#: Caps on a ``ui.find`` action's page and the names it searches for.
_FIND_ROUTE_MAX_CHARS = 200
_FIND_LABEL_MAX_CHARS = 80
#: The roles a ``ui.find`` may narrow its search to (``FIND_ROLES`` in
#: ``website/src/guide/findByName.ts``).
FIND_ROLES = ("button", "link", "menuitem", "tab", "switch", "checkbox", "textbox", "option")
#: Pages of the agent's own ceiling: a ``ui.find`` never opens or searches
#: them (the same tabs ``settings.show`` refuses and the auto tier denies).
TRUST_ROOT_ROUTES = ("/settings/security", "/settings/computer-use", "/settings/secrets")
#: An English name that deletes or removes something: a ``ui.find`` of it
#: carries ``caution`` (the panel's warning, and the press alone never ends
#: the step), whatever the agent passed. ``REMOVAL_LABEL_RE`` in
#: ``website/scripts/lib/ui-index.mjs`` is the same rule for the auto tier.
REMOVAL_LABEL_RE = re.compile(
    r"\b(?:delete|remove|uninstall|erase|reset|wipe|purge|destroy)\b"
    r"|\bclear\s+(?:all|data|everything|history|cache|memory)\b",
    re.IGNORECASE,
)
#: An English name that widens what the agent may do (approve, grant, trust,
#: allow): a ``ui.find`` of it is refused, as the auto tier keeps such
#: controls search-only (``CEILING_LABEL_RE`` in the generator).
CEILING_LABEL_RE = re.compile(r"\b(?:approve|grant|trust|allow|autopilot|yolo)\b", re.IGNORECASE)
#: What a tab may report about a ``ui.find`` search (:func:`clean_find_report`).
FIND_RESULTS = ("found", "ambiguous", "none")
_FIND_MAX_COUNT = 50
_LABEL_KEY_RE = re.compile(r"^[a-z][A-Za-z0-9_.]{0,160}$")
_FIND_LOCATION_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,159}$")
#: A ``container`` that numbers a match (``#2``, ``2``): refused, see _validate_ui_find.
_NUMBERED_CONTAINER_RE = re.compile(r"^\s*#?\s*\d{1,2}\s*$")
#: Locations of the agent's own ceiling (``TRUST_ROOT_LOCATION_RE`` in the
#: dashboard's ``guide/findTargetPolicy.ts``): never a ``ui.find`` target.
_TRUST_ROOT_LOCATION_RE = re.compile(
    r"^(?:settings\.tab\.(?:security|computer-use|secrets)$"
    r"|settings\.sub\.(?:security|computer-use|secrets)\.)"
)

_REGISTRY_PATH = Path(__file__).resolve().parent / "docs" / "settings-registry.generated.json"
_UI_INDEX_PATH = Path(__file__).resolve().parent / "docs" / "ui-index.generated.json"
#: The build-time auto tier, shipped in the dashboard bundle and never committed
#: (the same file :data:`kiro_crew.ui_index.AUTO_INDEX_PATH` names).
_UI_AUTO_INDEX_PATH = Path(__file__).resolve().parent / "static" / "dist" / "ui-index.auto.json"
#: Ceiling on the auto tier file this module reads (the find_ui loader's own).
_UI_AUTO_MAX_BYTES = 8 * 1024 * 1024
_T = TypeVar("_T")


def _sources_stamp() -> tuple[tuple[str, int | None, int | None], ...]:
    """Path, mtime and size of every packaged file this module reads."""
    out: list[tuple[str, int | None, int | None]] = []
    for path in (_REGISTRY_PATH, _UI_INDEX_PATH, _UI_AUTO_INDEX_PATH):
        try:
            st = path.stat()
        except OSError:
            out.append((str(path), None, None))
        else:
            out.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(out)


class _CachedOnSources(Generic[_T]):
    """A no-argument reader of the packaged files, cached on those files.

    The value is reused while every source keeps its path, mtime and size, so
    a reader never answers from files other than the ones the module names
    now: a cache keyed on nothing kept the first answer it ever computed, for
    the whole process, even after the paths pointed somewhere else.
    """

    def __init__(self, fn: Callable[[], _T]) -> None:
        self._fn = fn
        self._hit: tuple[object, _T] | None = None
        functools.update_wrapper(self, fn)

    def __call__(self) -> _T:
        key, hit = _sources_stamp(), self._hit
        if hit is not None and hit[0] == key:
            return hit[1]
        value = self._fn()
        self._hit = (key, value)
        return value

    def cache_clear(self) -> None:
        self._hit = None


#: Settings tabs whose every control is a credential or a security ceiling. The
#: guide never points at them: a guide is an agent steering the human's attention,
#: and these are exactly the controls where an agent-chosen nudge is the risk.
_EXCLUDED_SETTING_TABS = frozenset({"security", "secrets", "connections", "computer-use"})

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
#: (who may reach the agent, auto-approval) anywhere else. Remote Crew
#: (Instances) is the user's own setup and is guidable. Matched
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
    }
)

_SEGMENT_SPLIT = re.compile(r"[-._%:]+")


class GuideCatalogError(ValueError):
    """A guide request that names an unknown action or carries bad parameters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: Caps on the agent's own words in a guide: the guide-level ``intro`` (offer
#: card and first step) and one action's ``note`` (shown under its final step).
MAX_INTRO_CHARS = 200
MAX_NOTE_CHARS = 160

#: Anything a renderer could read as a link or as markup. The browser renders the
#: text as React text either way; refusing these keeps an agent from presenting a
#: link or formatting the dashboard did not draw.
_GUIDE_TEXT_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)")


def _has_markdown_link(text: str) -> bool:
    """Recognize the prohibited link shape in one pass over delimiters."""
    label_open = False
    target_open = False
    for i, char in enumerate(text):
        if char == ")" and target_open:
            return True
        if char == "[":
            label_open = True
        elif char == "]":
            if label_open and text[i + 1 : i + 2] == "(":
                target_open = True
            label_open = False
    return False


_GUIDE_TEXT_MARKUP = ("<", ">", "`")
#: Bidirectional embedding/override/isolate controls: they can make text read
#: differently from what it is. Other format characters (e.g. the joiner inside
#: an emoji) are ordinary text.
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def clean_guide_text(
    raw: object, field_name: str, max_chars: int, *, check_redaction: bool = True
) -> str:
    """Validate one piece of agent-authored guide text into what is stored.

    Plain text only: line breaks and tabs collapse to single spaces, any other
    control character is refused, as are links (``http(s)://``, ``www.``) and
    markup (``<``, ``>``, backticks, ``[text](target)``). Over the cap is
    REFUSED with the length, never truncated, so the agent can shorten it.
    ``None`` or blank means absent and returns ``""``. ``check_redaction=False``
    skips the redactor pass, for text read from this build's own packaged
    catalogs rather than written by an agent.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise GuideCatalogError("invalid_text", f"{field_name} must be a string")
    text = re.sub(r"[\r\n\t]+", " ", raw)
    if any(unicodedata.category(ch) == "Cc" or ch in _BIDI_CONTROLS for ch in text):
        raise GuideCatalogError(
            "invalid_text", f"{field_name}: remove control or invisible formatting characters"
        )
    text = re.sub(r" {2,}", " ", text).strip()
    if len(text) > max_chars:
        raise GuideCatalogError(
            "invalid_text",
            f"{field_name} is {len(text)} characters; shorten it to at most {max_chars}",
        )
    if _GUIDE_TEXT_URL_RE.search(text):
        raise GuideCatalogError(
            "invalid_text", f"{field_name}: no links; the guide points at the dashboard itself"
        )
    if any(ch in text for ch in _GUIDE_TEXT_MARKUP) or _has_markdown_link(text):
        raise GuideCatalogError(
            "invalid_text", f"{field_name}: plain text only, no markup, HTML or backticks"
        )
    if check_redaction and needs_redaction(text):
        raise GuideCatalogError(
            "invalid_text",
            f"{field_name}: remove the credential or link; the guide shows it as written",
        )
    return text


def needs_redaction(text: str) -> bool:
    """Whether the output redactors would change *text*.

    Both passes agent output gets on its way to the page -- the exfiltration-URL
    scrubber, then the credential redactor (through the platform context, so a
    companion's extra patterns apply) -- run over the whole text. Agent-authored
    display text that is stored and shown as written (a guide's intro or note, a
    card's reason) is REFUSED when either would change it, rather than stored
    redacted: a rewritten sentence would read as the agent's own words.
    """
    if not text:
        return False
    from kiro_crew.platform import redact_via_context
    from kiro_crew.security import redact_exfiltration_urls

    scrubbed, _ = redact_exfiltration_urls(text)
    return scrubbed != text or redact_via_context(scrubbed) != scrubbed


def validate_intro(raw: object) -> str:
    """The guide-level ``intro``: ``""`` when absent. See :func:`clean_guide_text`."""
    return clean_guide_text(raw, "intro", MAX_INTRO_CHARS)


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
    #: The steps come from each start's parameters (``ui.show``), so the catalog
    #: lists no fixed count; each stored record carries its own.
    steps_per_record: bool = False


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
            "Open the New crewmate card with an optional name and goal "
            "pre-filled, then point at its Create button. Nothing is created "
            "until the user clicks Create; completion reports the new "
            "crewmate's id."
        ),
        steps=(StepDef("create", STEP_COMMIT),),
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
            "Take the user to the existing MCP servers tab, point at its Add "
            "Custom button, then at the form's server JSON box, where the "
            "command, args and env go. Nothing is pre-filled or saved: the user "
            "fills in and saves the existing form themselves. Completion means the "
            "add form was reached, never that a server was installed."
        ),
        steps=(StepDef("servers-tab", STEP_UI), StepDef("add", STEP_UI), StepDef("form", STEP_UI)),
        params_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        mutates=False,
    ),
    ACTION_UI_SHOW: ActionDef(
        id=ACTION_UI_SHOW,
        title="Show a place in the dashboard",
        description=(
            "Open the page an indexed UI location is on and point at it, walking "
            "through the controls that reveal it (a collapsed sidebar, a menu). "
            "Only ids find_ui returns with a ui.show guide_ref are accepted. It "
            "only points: the user clicks; nothing is clicked or saved for them."
        ),
        # Placeholder: the real steps come from the location's packaged plan and
        # are stored per record (``step_count`` / ``step_kinds``).
        steps=(StepDef("show", STEP_UI),),
        params_schema={
            "type": "object",
            "properties": {
                "location_id": {
                    "type": "string",
                    "maxLength": _LOCATION_ID_MAX_CHARS,
                    "description": "A find_ui location id with a ui.show guide_ref, e.g. 'chat.older-sessions'.",
                },
                "pick": {
                    "type": "string",
                    "maxLength": _FIND_LABEL_MAX_CHARS,
                    "description": (
                        "Optional: the name of the one item the user named (crewmate "
                        "'Helper', job 'Morning summary') when the guide first has them "
                        "choose one; the tab outlines only that item."
                    ),
                },
            },
            "required": ["location_id"],
            "additionalProperties": False,
        },
        mutates=False,
        steps_per_record=True,
    ),
    ACTION_UI_FIND: ActionDef(
        id=ACTION_UI_FIND,
        title="Point at a control by its name",
        description=(
            "Open one page and point at the control whose on-screen name is "
            "`label` (the dashboard's language, as find_ui returns it). Use the "
            "find_ref find_ui gives, or a page and label from search_docs, when "
            "there is no ui.show guide_ref. The user's tab looks for the name "
            "itself and, if it is not visible, inside the dashboard's shared "
            "menus, popovers and local tabs, then points at what to open "
            "first. guide_status says found, ambiguous (several controls share "
            "the name: the panel lists them and the user picks one there; a "
            "container names a menu or section, never a number) or not_found (the user is "
            "asked to open the menu or section it is in; else describe the "
            "path in words). It never points at the agent's own security "
            "controls. It only points: the user clicks."
        ),
        steps=(StepDef("open", STEP_UI), StepDef("show", STEP_UI)),
        params_schema={
            "type": "object",
            "properties": {
                "route": {
                    "type": "string",
                    "maxLength": _FIND_ROUTE_MAX_CHARS,
                    "description": (
                        "The page, as find_ui returns it (e.g. '/schedule' or "
                        "'/capabilities?tab=mcp'). Omit for a control on every page."
                    ),
                },
                "label": {
                    "type": "string",
                    "maxLength": _FIND_LABEL_MAX_CHARS,
                    "description": "The control's on-screen name, in the dashboard's language.",
                },
                "role": {"type": "string", "enum": list(FIND_ROLES)},
                "container": {
                    "type": "string",
                    "maxLength": _FIND_LABEL_MAX_CHARS,
                    "description": (
                        "Optional: the name of the menu, section or tab it is in, "
                        "never a number: the user picks among several matches in the panel."
                    ),
                },
                "caution": {
                    "type": "boolean",
                    "description": "The control deletes or removes something (find_ref says so).",
                },
                "location_id": {
                    "type": "string",
                    "maxLength": 160,
                    "description": (
                        "Optional, from a find_ref only: the registered control's id, "
                        "so the tab finds it whatever its label reads right now."
                    ),
                },
                "opener": {
                    "type": "string",
                    "maxLength": 160,
                    "description": (
                        "Optional, from a find_ref only: the registered control that "
                        "opens the menu it is in; the guide points there first."
                    ),
                },
            },
            "required": ["label"],
            "additionalProperties": False,
        },
        mutates=False,
    ),
}


def step_count(action_id: str) -> int:
    """The static step count of a fixed action (``ui.show`` stores its own per record)."""
    return len(ACTIONS[action_id].steps)


def step_kind(action_id: str, step_index: int) -> str:
    return ACTIONS[action_id].steps[step_index].kind


def record_step_kind(record: dict[str, Any], step_index: int) -> str:
    """The kind of step *step_index* of one stored action record.

    A record whose steps were derived at start (``ui.show``) carries its own
    ``step_kinds``; a fixed action reads the static catalog.
    """
    kinds = record.get("step_kinds")
    if isinstance(kinds, list):
        return str(kinds[step_index])
    return step_kind(str(record["id"]), step_index)


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
            # ``None``: the count depends on the location (see each record).
            "step_count": None if a.steps_per_record else len(a.steps),
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


@_CachedOnSources
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


# ── ui.show plans ──


_PLACEMENT_ID_RE = re.compile(r"^[a-z]{1,16}$")
_STEP_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


def _plan_is_well_formed(plan: object) -> bool:
    """A version-2 plan: per-placement step lists, each step with a unique id.

    Version 1 (one ``step_count`` for every placement) is not accepted: the
    generator, the packaged index and the bundle moved to version 2 together,
    so a version-1 plan can only be a stale index, which is left out.
    """
    if not isinstance(plan, dict) or plan.get("version") != UI_SHOW_PLAN_VERSION:
        return False
    placements = plan.get("placements")
    if not isinstance(placements, list) or not placements:
        return False
    placement_ids: set[str] = set()
    step_ids: set[str] = set()
    for p in placements:
        if not isinstance(p, dict):
            return False
        pid = p.get("id")
        if not isinstance(pid, str) or not _PLACEMENT_ID_RE.match(pid) or pid in placement_ids:
            return False
        placement_ids.add(pid)
        steps = p.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= _UI_SHOW_MAX_STEPS:
            return False
        route = p.get("route")
        if route is not None and not (isinstance(route, str) and route.startswith("/")):
            return False
        for st in steps:
            if not isinstance(st, dict) or not _step_is_well_formed(st):
                return False
            sid = st.get("id")
            if not isinstance(sid, str) or not _STEP_ID_RE.match(sid) or sid in step_ids:
                return False
            step_ids.add(sid)
            req = st.get("requires", [])
            if not isinstance(req, list) or not all(isinstance(x, str) for x in req):
                return False
        # The last step is the location itself, which a guide only points at.
        if steps[-1].get("kind") is not None:
            return False
    return True


#: Step kinds a plan may carry besides pointing (``kind`` absent).
STEP_KIND_SELECT = "select"
STEP_KIND_GATE = "gate"
#: Pickers whose rows carry their entity's name (``data-guide-pick``), so a
#: ``ui.show`` ``pick`` can be bound to the one entity the user named. A plan
#: choosing from any other picker takes no ``pick``: nothing there could tell
#: the named entity from another. Mirrors ``GUIDE_PICKABLE_PICKERS`` in the
#: dashboard's ``guideActions.ts``.
UI_SHOW_PICKABLE_PICKERS = frozenset(
    {
        "agents.crew-list",
        "members.roster-list",
        "schedule.job-list",
        "apps.library.app-list",
        "artifacts.list",
    }
)
_ID_PART_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def _step_is_well_formed(st: dict[str, Any]) -> bool:
    """A pointing step names a location; a ``select`` step its picker and
    selection; a ``gate`` step its gate (and maybe the setting that turns it on)
    and no location."""
    kind = st.get("kind")
    if "caution" in st and (st["caution"] is not True or kind is not None):
        # Only a pointing step can be a destructive control's.
        return False
    if "caution_key" in st and (
        st.get("caution") is not True or not isinstance(st["caution_key"], str)
    ):
        # The page's own words for what it removes ride on a caution step only.
        return False
    if kind == STEP_KIND_GATE:
        setting = st.get("setting_id")
        return (
            isinstance(st.get("gate"), str)
            and bool(_ID_PART_RE.match(st["gate"]))
            and "location" not in st
            and (setting is None or (isinstance(setting, str) and bool(_ID_PART_RE.match(setting))))
        )
    if not isinstance(st.get("location"), str):
        return False
    if kind is None:
        return True
    return (
        kind == STEP_KIND_SELECT
        and isinstance(st.get("selection"), str)
        and bool(_ID_PART_RE.match(st["selection"]))
    )


def plan_step_meta(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Step id -> what a ``select`` or ``gate`` step waits on, for a well-formed plan.

    Only ids from the packaged plan (a selection scope, a gate, a settings
    registry id); a pointing step has no entry. A guide in ``gate_off`` or
    ``needs_selection`` names its current step's entry as its ``blocker``.
    """
    out: dict[str, dict[str, Any]] = {}
    for p in plan["placements"]:
        for st in p["steps"]:
            kind = st.get("kind")
            if kind == STEP_KIND_GATE:
                out[str(st["id"])] = {
                    "kind": STEP_KIND_GATE,
                    "gate": st["gate"],
                    "setting_id": st.get("setting_id"),
                }
            elif kind == STEP_KIND_SELECT:
                out[str(st["id"])] = {"kind": STEP_KIND_SELECT, "selection": st["selection"]}
    return out


def step_key(step_id: str) -> str:
    """A step id without its placement prefix: the same step in every placement."""
    return step_id.split(":", 1)[1] if ":" in step_id else step_id


def plan_placements(plan: dict[str, Any]) -> dict[str, list[str]]:
    """Placement id -> its step ids, in order, for a well-formed plan."""
    return {str(p["id"]): [str(st["id"]) for st in p["steps"]] for p in plan["placements"]}


def record_needs_placement(record: dict[str, Any]) -> bool:
    """Whether *record* is a version-2 ``ui.show`` record that walks one placement."""
    return record.get("plan_version") == UI_SHOW_PLAN_VERSION and isinstance(
        record.get("placements"), dict
    )


def claim_placement(record: dict[str, Any], placement_id: object) -> dict[str, Any]:
    """The fields a claim with *placement_id* records on a ``ui.show`` record.

    ``placement`` and its ``step_ids`` (from the record's own ``placements``,
    which came from the packaged index at start, never from the tab), their
    ``step_count`` and ``step_kinds``. Raises :class:`GuideCatalogError` for a
    placement the plan does not have.
    """
    placements = record["placements"]
    if not isinstance(placement_id, str) or placement_id not in placements:
        raise GuideCatalogError("unknown_placement", "that placement is not one of this plan's")
    ids = [str(x) for x in placements[placement_id]]
    return {
        "placement": placement_id,
        "step_ids": ids,
        "step_count": len(ids),
        "step_kinds": [STEP_UI] * len(ids),
    }


def replan_placement(
    record: dict[str, Any], placement_id: object, step_index: int
) -> dict[str, Any]:
    """The fields a mid-guide re-plan to *placement_id* records, at *step_index*.

    Allowed only at a step boundary both placements share: the steps already
    walked (the first *step_index*) are the same steps, by key, in the new
    placement, which also has a step at *step_index* to continue with. Raises
    :class:`GuideCatalogError` (``unknown_placement``, ``replan_not_at_boundary``).
    """
    update = claim_placement(record, placement_id)
    held = record.get("step_ids")
    if not isinstance(held, list):
        raise GuideCatalogError("replan_not_at_boundary", "this action was never claimed")
    new = update["step_ids"]
    walked = [step_key(str(x)) for x in held[:step_index]]
    if step_index >= len(new) or [step_key(x) for x in new[:step_index]] != walked:
        raise GuideCatalogError(
            "replan_not_at_boundary", "the steps walked so far differ in that placement"
        )
    return update


@_CachedOnSources
def ui_show_plans() -> dict[str, dict[str, Any]]:
    """Packaged location id -> its ``guide_plan``, for every guidable location.

    Read once from the index this build ships; a location the generator gave no
    plan (a runtime condition, a control of the agent's own ceiling) is simply
    absent, so ``ui.show`` refuses it as unknown. An unreadable index yields an
    empty catalog rather than a guess, and a malformed plan is left out.
    """
    try:
        payload = json.loads(_UI_INDEX_PATH.read_text(encoding="utf-8"))
        locations = payload["locations"]
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("ui index unreadable; ui.show is unavailable")
        return {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(locations, list):
        return out
    for loc in locations:
        if not isinstance(loc, dict) or "guide_plan" not in loc:
            continue
        lid, plan = loc.get("id"), loc.get("guide_plan")
        if not isinstance(lid, str) or loc.get("tier", "curated") != "curated":
            continue
        if isinstance(plan, dict) and _plan_is_well_formed(plan):
            out[lid] = plan
    return out


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_AUTO_LOCATION_RE = re.compile(r"^auto:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$")
_AUTO_SITE_RE = re.compile(r"^auto:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+(?::[0-9]+)?$")
#: The parent of an auto control several pages share (``SHARED_PARENT`` in the generator).
_AUTO_SHARED_PARENT = "shared"
#: The ``guide_policy`` values a guide may walk.
_GUIDABLE_POLICIES = frozenset({"point", "caution"})


@dataclass(frozen=True)
class UiAutoTier:
    """The pointable part of the build-time auto tier (see :func:`ui_auto_tier`).

    ``build_digest`` is the auto tier's own (empty: no usable tier), ``plans``
    each guidable location's single-step plan, ``sites`` its one render-site
    id (what the bundle stamped as ``data-ui-auto``).
    """

    build_digest: str
    plans: dict[str, dict[str, Any]]
    sites: dict[str, str]


#: Most placements a shared auto plan may list (one per page that draws it).
_AUTO_MAX_PLACEMENTS = 32


def _auto_plan_site(lid: str, plan: object) -> str | None:
    """The site id of a well-formed auto plan, or ``None``.

    One placement (``any``), or one per page for a shared control (``auto:shared:``
    ids only), each a single pointing step at the SAME site.
    """
    if not _AUTO_LOCATION_RE.match(lid) or not _plan_is_well_formed(plan):
        return None
    assert isinstance(plan, dict)
    placements = plan["placements"]
    if not 1 <= len(placements) <= _AUTO_MAX_PLACEMENTS:
        return None
    if len(placements) > 1 and not lid.startswith(f"auto:{_AUTO_SHARED_PARENT}:"):
        return None
    if any(len(p["steps"]) != 1 or p["steps"][0].get("kind") is not None for p in placements):
        return None
    sites = {p["steps"][0].get("location") for p in placements}
    if len(sites) != 1:
        return None
    (site,) = sites
    return site if isinstance(site, str) and _AUTO_SITE_RE.match(site) else None


@_CachedOnSources
def ui_auto_tier() -> UiAutoTier:
    """The auto tier's guidable plans, read from the dashboard bundle once per file version.

    Refused whole (an empty tier, so every auto id is ``unknown_location``)
    unless the artifact names THIS committed index twice over: its
    ``base_input_digest`` is the index's ``input_digest`` and its
    ``base_build_digest`` the index's ``build_digest``. Only ``tier: auto``
    locations with a guidable ``guide_policy`` (``point`` or ``caution``) and a
    well-formed single-step plan are kept; ``search-only`` and ``deny`` ones are
    never guidable.
    """
    empty = UiAutoTier("", {}, {})
    try:
        if _UI_AUTO_INDEX_PATH.stat().st_size > _UI_AUTO_MAX_BYTES:
            return empty
        raw = json.loads(_UI_AUTO_INDEX_PATH.read_text(encoding="utf-8"))
        base = json.loads(_UI_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict) or not isinstance(base, dict) or raw.get("artifact") != "auto":
        return empty
    digest = raw.get("build_digest")
    if (
        not isinstance(digest, str)
        or not _DIGEST_RE.match(digest)
        or raw.get("base_input_digest") != base.get("input_digest")
        or raw.get("base_build_digest") != base.get("build_digest")
        or not isinstance(raw.get("locations"), list)
    ):
        return empty
    plans: dict[str, dict[str, Any]] = {}
    sites: dict[str, str] = {}
    for loc in raw["locations"]:
        if not isinstance(loc, dict) or loc.get("tier") != "auto":
            continue
        lid, plan = loc.get("id"), loc.get("guide_plan")
        if not isinstance(lid, str) or loc.get("guide_policy") not in _GUIDABLE_POLICIES:
            continue
        site = _auto_plan_site(lid, plan)
        if site is not None and isinstance(plan, dict):
            plans[lid] = plan
            sites[lid] = site
    return UiAutoTier(digest, plans, sites)


@dataclass(frozen=True)
class UiBuildManifest:
    """What a live guide relies on the packaged index and the bundle agreeing on.

    ``build_digest`` is the index's own (the generator writes the same value
    into the browser bundle); empty when the index is unreadable or carries
    none, which no tab's bundle ever matches. ``observable`` is the curated
    location ids a tab may be asked to observe, ``scopes`` the reveal scope ids,
    ``plan_scopes`` the scopes each ``ui.show`` plan's reveal steps name, and
    ``plan_predicates`` the runtime predicates its steps carry (the vocabulary
    is ``predicates``).
    """

    build_digest: str
    observable: frozenset[str]
    scopes: frozenset[str]
    plan_scopes: dict[str, tuple[str, ...]]
    plan_predicates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    predicates: frozenset[str] = frozenset()
    #: Selection scope ids (a ``select`` step's fact; also live predicates).
    selections: frozenset[str] = frozenset()
    #: Gate id -> the settings registry id that turns it on, or ``None``.
    gates: dict[str, str | None] = field(default_factory=dict)
    #: Auto ``point`` location id -> its render-site id: the auto locations a
    #: tab may be asked to observe (it is asked for the site id).
    auto_sites: dict[str, str] = field(default_factory=dict)


@_CachedOnSources
def ui_build_manifest() -> UiBuildManifest:
    """The packaged index's build manifest, read once per file version (:class:`UiBuildManifest`)."""
    empty = UiBuildManifest("", frozenset(), frozenset(), {}, {}, frozenset())
    try:
        payload = json.loads(_UI_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(payload, dict):
        return empty
    digest = payload.get("build_digest")
    locations = payload.get("locations")
    scopes = payload.get("reveal_scopes")
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        return empty
    if not isinstance(locations, list) or not isinstance(scopes, dict):
        return empty
    raw_predicates = payload.get("runtime_predicates", [])
    raw_selections = payload.get("guide_selections", {})
    raw_gates = payload.get("guide_gates", {})
    selections = frozenset(
        s
        for s in (raw_selections if isinstance(raw_selections, dict) else {})
        if isinstance(s, str)
    )
    gates: dict[str, str | None] = {}
    for gid, g in (raw_gates if isinstance(raw_gates, dict) else {}).items():
        if isinstance(gid, str) and isinstance(g, dict):
            sid = g.get("setting_id")
            gates[gid] = sid if isinstance(sid, str) else None
    # Selections and gates are live predicates too: a tab reports each one's
    # met / unmet / unknown, never what was picked.
    predicates = (
        frozenset(
            p
            for p in (raw_predicates if isinstance(raw_predicates, list) else [])
            if isinstance(p, str)
        )
        | selections
        | frozenset(gates)
    )
    observable: set[str] = set()
    plan_scopes: dict[str, tuple[str, ...]] = {}
    plan_predicates: dict[str, tuple[str, ...]] = {}
    for loc in locations:
        if not isinstance(loc, dict) or loc.get("tier") != "curated":
            continue
        lid = loc.get("id")
        if not isinstance(lid, str):
            continue
        observable.add(lid)
        plan = loc.get("guide_plan")
        if isinstance(plan, dict) and _plan_is_well_formed(plan):
            named = {
                st["scope"]
                for p in plan["placements"]
                for st in p["steps"]
                if isinstance(st.get("scope"), str) and st["scope"] in scopes
            }
            if named:
                plan_scopes[lid] = tuple(sorted(named))
            needs = {
                x
                for p in plan["placements"]
                for st in p["steps"]
                for x in [*st.get("requires", []), st.get("selection"), st.get("gate")]
                if isinstance(x, str) and x in predicates
            }
            if needs:
                plan_predicates[lid] = tuple(sorted(needs))
    return UiBuildManifest(
        digest,
        frozenset(observable),
        frozenset(s for s in scopes if isinstance(s, str)),
        plan_scopes,
        plan_predicates,
        predicates,
        selections,
        gates,
        dict(ui_auto_tier().sites),
    )


# ── parameter validation ──


def _route_pathname(route: str) -> str:
    return route.split("?", 1)[0].split("#", 1)[0]


# Query parameters that choose which page a path shows (``/capabilities?tab=mcp``).
_PAGE_IDENTITY_PARAMS = ("tab",)


def _route_compatible(asked: str, other: str) -> bool:
    """Whether *other* is the page the route *asked* names.

    The paths match (trailing slashes aside) and every page-identity query
    parameter *asked* sets has the same value in *other*: ``/capabilities``
    with ``?tab=crews`` is another page than with ``?tab=mcp``. A parameter
    *asked* leaves out does not tell pages apart.
    """
    if _route_pathname(asked).rstrip("/") != _route_pathname(other).rstrip("/"):
        return False
    want = parse_qs(asked.split("#", 1)[0].partition("?")[2])
    have = parse_qs(other.split("#", 1)[0].partition("?")[2])
    return all(want[k] == have.get(k) for k in _PAGE_IDENTITY_PARAMS if k in want)


def _is_trust_root_route(route: str) -> bool:
    path = _route_pathname(route).rstrip("/").lower()
    return any(path == r or path.startswith(r + "/") for r in TRUST_ROOT_ROUTES)


def is_trust_root(location_id: str, routes: Iterable[str] = ()) -> bool:
    """A location of the agent's own ceiling: a trust-root tab or section, a
    setting on an excluded tab, or any of *routes* on a trust-root page."""
    if _TRUST_ROOT_LOCATION_RE.match(location_id):
        return True
    if location_id.startswith("setting:"):
        tab = location_id[len("setting:") :].split(".", 1)[0]
        if tab in _EXCLUDED_SETTING_TABS:
            return True
    return any(isinstance(r, str) and _is_trust_root_route(r) for r in routes)


@_CachedOnSources
def ui_find_routes() -> tuple[frozenset[str], frozenset[str]]:
    """``(routes, pathnames)`` a ``ui.find`` may open: every page this build's
    index (and its auto tier, when shipped) places a control on, minus the
    trust-root pages. A route is accepted as written, or as a bare pathname of
    one of them (no query)."""
    routes: set[str] = set()
    sources = [_UI_INDEX_PATH]
    try:
        if _UI_AUTO_INDEX_PATH.stat().st_size <= _UI_AUTO_MAX_BYTES:
            sources.append(_UI_AUTO_INDEX_PATH)
    except OSError:
        pass
    for path in sources:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        locations = payload.get("locations") if isinstance(payload, dict) else None
        for loc in locations if isinstance(locations, list) else []:
            for p in loc.get("placements", []) if isinstance(loc, dict) else []:
                route = p.get("route") if isinstance(p, dict) else None
                if isinstance(route, str) and route.startswith("/") and "//" not in route:
                    routes.add(route)
    allowed = frozenset(r for r in routes if not _is_trust_root_route(r))
    return allowed, frozenset(_route_pathname(r) for r in allowed)


def ui_find_location_ids() -> frozenset[str]:
    """The registered locations a ``ui.find`` may name by id: every control
    of this build's committed index (curated and generated, not the auto tier),
    minus pages, lists, settings and the agent's own ceiling. The tab finds a
    control carrying the id whatever its label reads in the page's state."""
    ids: frozenset[str] = _ui_find_index_facts()["ids"]
    return ids


def ui_find_opener(location_id: str) -> str | None:
    """The registered control that opens the menu *location_id* is drawn in,
    from the index (its first placement's innermost parent), or None.

    Only a control the guide may point at as an opener: one ``ui.find`` may
    name, never a trust-root control and never one that deletes or removes
    something. A ``ui.find``'s opener is always this value; the caller never
    chooses it."""
    opener: str | None = _ui_find_index_facts()["openers"].get(location_id)
    return opener


def ui_find_cautions() -> frozenset[str]:
    """Indexed controls that delete or remove something: a ``caution`` guide
    policy, or an English label :data:`REMOVAL_LABEL_RE` matches."""
    cautions: frozenset[str] = _ui_find_index_facts()["cautions"]
    return cautions


def _ui_find_index_facts() -> dict[str, Any]:
    empty: dict[str, Any] = {
        "ids": frozenset(),
        "openers": {},
        "cautions": frozenset(),
        "pages": {},
    }
    try:
        st = _UI_INDEX_PATH.stat()
    except OSError:
        return empty
    stamp = (str(_UI_INDEX_PATH), st.st_mtime_ns, st.st_size)
    if _location_ids_cache.get("stamp") == stamp:
        facts: dict[str, Any] = _location_ids_cache["facts"]
        return facts
    try:
        payload = json.loads(_UI_INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    locations = payload.get("locations") if isinstance(payload, dict) else None
    labels = payload.get("labels") if isinstance(payload, dict) else None
    english = labels.get("en") if isinstance(labels, dict) else None
    english = english if isinstance(english, dict) else {}
    out: set[str] = set()
    cautions: set[str] = set()
    parent_of: dict[str, str] = {}
    pages: dict[str, set[str]] = {}
    for loc in locations if isinstance(locations, list) else []:
        if not isinstance(loc, dict):
            continue
        lid = loc.get("id")
        if not isinstance(lid, str):
            continue
        if loc.get("kind") == "page":
            _note_page_labels(loc, labels, pages)
        label = english.get(str(loc.get("label_key")), "")
        if loc.get("guide_policy") == "caution" or (
            isinstance(label, str) and REMOVAL_LABEL_RE.search(label)
        ):
            cautions.add(lid)
        if (
            _FIND_LOCATION_RE.match(lid)
            and loc.get("kind") not in ("page", "list", "setting")
            and not loc.get("setting_id")
            and loc.get("guide_policy") != "deny"
            and not _TRUST_ROOT_LOCATION_RE.match(lid)
        ):
            out.add(lid)
        placements = loc.get("placements")
        first = placements[0] if isinstance(placements, list) and placements else None
        parents = first.get("parent_ids") if isinstance(first, dict) else None
        if (
            isinstance(first, dict)
            and first.get("entry_kind") == "menu"
            and isinstance(parents, list)
            and parents
            and isinstance(parents[-1], str)
        ):
            parent_of[lid] = parents[-1]
    openers = {
        lid: parent
        for lid, parent in parent_of.items()
        if lid in out
        and parent != lid
        and parent in out
        and parent not in cautions
        and not _TRUST_ROOT_LOCATION_RE.match(parent)
    }
    facts = {
        "ids": frozenset(out),
        "openers": openers,
        "cautions": frozenset(cautions),
        "pages": {route: frozenset(names) for route, names in pages.items()},
    }
    _location_ids_cache.update(stamp=stamp, facts=facts)
    return facts


_location_ids_cache: dict[str, Any] = {}


def _page_name(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _note_page_labels(loc: dict[str, Any], labels: object, pages: dict[str, set[str]]) -> None:
    """Record a page's name, in every shipped language, under the path a guide opens it by.

    Only a page reached by its address (a direct link or a tab), not one the
    navigation rail draws an entry for on every page: that one is pointed at
    by its rail entry instead.
    """
    keys = [str(loc.get("label_key") or "")] + [str(k) for k in loc.get("alias_keys") or []]
    for p in loc.get("placements") or []:
        if not isinstance(p, dict) or p.get("entry_kind") == "rail":
            continue
        route = p.get("route")
        if not isinstance(route, str) or not route.startswith("/") or _is_trust_root_route(route):
            continue
        names = pages.setdefault(_route_pathname(route), set())
        for table in labels.values() if isinstance(labels, dict) else []:
            for key in keys:
                text = table.get(key) if isinstance(table, dict) else None
                if isinstance(text, str) and text.strip():
                    names.add(_page_name(text))


def ui_find_is_page(route: object, label: object) -> bool:
    """Whether a ``ui.find`` names the page it opens: *label* is that page's
    own name at *route*. Such a find is done when the person is on the page;
    there is no control named like the page to point at there."""
    if not isinstance(route, str) or not isinstance(label, str):
        return False
    pages: dict[str, frozenset[str]] = _ui_find_index_facts().get("pages", {})
    return _page_name(label) in pages.get(_route_pathname(route), frozenset())


def _validate_ui_find(
    params: dict[str, Any], *, packaged: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    _exact_keys(
        params,
        {"route", "label", "role", "container", "caution", "location_id", "opener", "page"},
        ACTION_UI_FIND,
    )
    out: dict[str, Any] = {}
    route = params.get("route")
    if route not in (None, ""):
        if not isinstance(route, str) or len(route) > _FIND_ROUTE_MAX_CHARS:
            raise GuideCatalogError("invalid_params", "ui.find: route must be a dashboard path")
        if _has_control_chars(route) or "\\" in route or not route.startswith("/"):
            raise GuideCatalogError("invalid_params", "ui.find: route must be a dashboard path")
        if _is_trust_root_route(route):
            raise GuideCatalogError(
                "sensitive_page",
                "ui.find: that page holds the agent's own security settings; describe it in words",
            )
        routes, pathnames = ui_find_routes()
        if route not in routes and not ("?" not in route and route in pathnames):
            raise GuideCatalogError(
                "unknown_route",
                "ui.find: that page is not in this build's index; use a find_ui route",
            )
        out["route"] = route
    label = clean_guide_text(
        params.get("label"), "ui.find label", _FIND_LABEL_MAX_CHARS, check_redaction=not packaged
    )
    if not label:
        raise GuideCatalogError("invalid_params", "ui.find: label is required")
    if CEILING_LABEL_RE.search(label):
        raise GuideCatalogError(
            "sensitive_label",
            "ui.find: a control that widens what the agent may do is not pointed at; "
            "describe where it is in words",
        )
    out["label"] = label
    role = params.get("role")
    if role is not None:
        if role not in FIND_ROLES:
            raise GuideCatalogError("invalid_params", "ui.find: unknown role")
        out["role"] = role
    container = clean_guide_text(
        params.get("container"),
        "ui.find container",
        _FIND_LABEL_MAX_CHARS,
        check_redaction=not packaged,
    )
    if container and _NUMBERED_CONTAINER_RE.match(container):
        # A number names a match only in the list the person saw; the page can
        # reorder or remount before a new guide runs, so the person picks in
        # the panel, where the pick is bound to the control itself.
        raise GuideCatalogError(
            "invalid_params",
            "ui.find: container names a menu or section, never a number; "
            "the user picks among several matches in the guide panel",
        )
    if container:
        out["container"] = container
    location_id = params.get("location_id")
    if location_id is not None:
        if not isinstance(location_id, str) or not _FIND_LOCATION_RE.match(location_id):
            raise GuideCatalogError("invalid_params", "ui.find: location_id must be a location id")
        if location_id not in ui_find_location_ids():
            raise GuideCatalogError(
                "unknown_location",
                "ui.find: that location is not in this build's index; use a find_ui find_ref",
            )
        out["location_id"] = location_id
    # The opener is a fact of the index (the control that opens the menu the
    # target is drawn in), never the caller's choice: a caller value must be
    # exactly the target's indexed opener, and that one is kept even when
    # the caller left it out. An indexed opener that removes something or
    # sits under the agent's own ceiling is never handed out.
    opener = params.get("opener")
    if opener is not None:
        if not isinstance(opener, str) or not _FIND_LOCATION_RE.match(opener):
            raise GuideCatalogError("invalid_params", "ui.find: opener must be a location id")
        if (
            opener == location_id
            or opener not in ui_find_location_ids()
            or opener in ui_find_cautions()
            or _TRUST_ROOT_LOCATION_RE.match(opener)
            or not isinstance(location_id, str)
            or ui_find_opener(location_id) != opener
        ):
            raise GuideCatalogError(
                "unknown_location",
                "ui.find: that opener is not the control that opens this one's menu; "
                "use a find_ui find_ref",
            )
    indexed_opener = ui_find_opener(location_id) if isinstance(location_id, str) else None
    if indexed_opener is not None:
        out["opener"] = indexed_opener
    # ``page`` is a fact of the index, like the opener: the find names a page
    # by its route and name (Logs at /logs). Such a page has no entry in the
    # rail (a rail page's find names its entry, with no route), so there is
    # nothing on screen to point at, and the guide never opens a page itself:
    # it is refused, and its path is given in words. A caller's own ``page``
    # value is ignored either way, and so is any role or container: naming
    # how the entry would be drawn does not make one exist.
    if location_id is None and "route" in out and ui_find_is_page(out["route"], label):
        raise GuideCatalogError(
            "no_entry",
            "ui.find: that page has no menu entry to point at; "
            "give its path in words from find_ui",
        )
    caution = params.get("caution")
    if caution is not None and not isinstance(caution, bool):
        raise GuideCatalogError("invalid_params", "ui.find: caution must be a boolean")
    if caution or REMOVAL_LABEL_RE.search(label):
        out["caution"] = True
    return out, {}


def ui_find_params_ok(params: dict[str, Any]) -> bool:
    """Whether a ``ui.find`` built from this build's packaged index would be
    accepted (``find_ui``'s ``find_ref``): every rule :func:`validate_actions`
    applies, except the redactor pass, which packaged labels never need."""
    try:
        _validate_ui_find(params, packaged=True)
    except GuideCatalogError:
        return False
    return True


def clean_find_report(raw: object) -> dict[str, Any]:
    """Validate what a tab says it found for a ``ui.find`` into what is stored.

    ``{result, count, role?, location_id?, label_key?}``: ``result`` one of
    :data:`FIND_RESULTS`, ``count`` how many visible controls carry the name
    (0-50), ``role`` one of :data:`FIND_ROLES`, and, when the match is a
    registered control, its location id and label key. Nothing else: the
    names the page shows (the match's, the containers', the alternatives'
    context) stay in the tab. Raises :class:`GuideCatalogError` on anything else.
    """
    if not isinstance(raw, dict):
        raise GuideCatalogError("invalid_find", "find must be an object")
    unknown = sorted(set(raw) - {"result", "count", "role", "location_id", "label_key"})
    if unknown:
        raise GuideCatalogError("invalid_find", f"find: unknown field '{unknown[0][:32]}'")
    result, count = raw.get("result"), raw.get("count")
    if result not in FIND_RESULTS:
        raise GuideCatalogError("invalid_find", "find.result must be found, ambiguous or none")
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= _FIND_MAX_COUNT:
        raise GuideCatalogError("invalid_find", "find.count must be an integer from 0 to 50")
    if (result == "none") != (count == 0) or (result == "found" and count != 1):
        raise GuideCatalogError("invalid_find", "find.count does not fit find.result")
    out: dict[str, Any] = {"result": result, "count": count}
    role = raw.get("role")
    if role is not None:
        if role not in FIND_ROLES:
            raise GuideCatalogError("invalid_find", "find.role is not a known role")
        out["role"] = role
    lid = raw.get("location_id")
    if lid is not None:
        if not isinstance(lid, str) or not _AUTO_SITE_OR_LOCATION_RE.match(lid):
            raise GuideCatalogError("invalid_find", "find.location_id is not a location id")
        out["location_id"] = lid
    key = raw.get("label_key")
    if key is not None:
        if not isinstance(key, str) or not _LABEL_KEY_RE.match(key):
            raise GuideCatalogError("invalid_find", "find.label_key is not a catalog key")
        out["label_key"] = key
    return out


_AUTO_SITE_OR_LOCATION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


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


def _validate_ui_show(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    _exact_keys(params, {"location_id", "pick"}, ACTION_UI_SHOW)
    lid = params.get("location_id")
    if not isinstance(lid, str) or not lid or len(lid) > _LOCATION_ID_MAX_CHARS:
        raise GuideCatalogError("invalid_params", "ui.show: location_id is required")
    if lid.startswith("auto:"):
        return _validate_ui_show_auto(lid)
    plan = ui_show_plans().get(lid)
    if plan is None:
        raise GuideCatalogError(
            "unknown_location",
            "ui.show: that location id has no guide plan; use an id find_ui returns "
            "with a ui.show guide_ref",
        )
    out: dict[str, Any] = {"location_id": lid}
    # ``pick``: the name of the one item the user meant (crewmate "Helper",
    # job "Morning summary"), for a plan that has the user choose one first.
    # The tab outlines only the item with that name, never deciding for them:
    # the selection is still the user's own press.
    pick = clean_guide_text(params.get("pick"), "ui.show pick", _FIND_LABEL_MAX_CHARS)
    if pick:
        pickers = {
            st.get("location")
            for p in plan["placements"]
            for st in p["steps"]
            if st.get("kind") == STEP_KIND_SELECT
        }
        if not pickers:
            raise GuideCatalogError(
                "invalid_params", "ui.show: pick names an item only for a plan with a choose step"
            )
        if not pickers <= UI_SHOW_PICKABLE_PICKERS:
            raise GuideCatalogError(
                "invalid_params",
                "ui.show: this plan's list cannot single out a named item; omit pick",
            )
        out["pick"] = pick
    # Derived per record: every placement's step ids (version 2), and the build
    # the plan came from, which a tab built from another one refuses
    # (``build_mismatch``). Which placement is walked is the claiming tab's
    # choice (its viewport); ``claim`` records it with its step ids, step
    # count and kinds (every step a UI step: the guide only points).
    return out, {
        "plan_version": UI_SHOW_PLAN_VERSION,
        "placements": plan_placements(plan),
        "step_meta": plan_step_meta(plan),
        "step_count": None,
        "build_digest": ui_build_manifest().build_digest,
    }


def _validate_ui_show_auto(lid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """``ui.show`` of an auto location: its plan from the auto tier, or ``unknown_location``.

    The record carries the plan itself (``auto_plan``): the bundle has no
    generated copy of auto plans, and it walks this one only when the record's
    ``build_digest`` is its own auto digest, i.e. the stamps came from the same
    generator run as this plan.
    """
    tier = ui_auto_tier()
    plan = tier.plans.get(lid)
    if plan is None or not tier.build_digest:
        raise GuideCatalogError(
            "unknown_location",
            "ui.show: that auto location is not pointable in this build; use an id find_ui "
            "returns with a ui.show guide_ref",
        )
    return {"location_id": lid}, {
        "plan_version": UI_SHOW_PLAN_VERSION,
        "placements": plan_placements(plan),
        "step_meta": {},
        "step_count": None,
        "build_digest": tier.build_digest,
        "auto_plan": json.loads(json.dumps(plan)),
    }


_VALIDATORS: dict[str, Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]]] = {
    ACTION_SETTINGS_SHOW: _validate_settings_show,
    ACTION_CREWMATE_CREATE: _validate_crewmate_create,
    ACTION_MCP_OPEN_ADD: _validate_mcp_open_add,
    ACTION_UI_SHOW: _validate_ui_show,
    ACTION_UI_FIND: _validate_ui_find,
}


def validate_actions(raw: object) -> list[dict[str, Any]]:
    """Validate a start request's ``actions`` list into the stored action records.

    Each record is ``{id, params, step_count}`` plus the server-derived fields an
    action carries (``route``/``label`` for ``settings.show``; ``ui.show``
    carries its plan's ``placements`` and a ``step_count`` of ``None`` until a
    tab claims it, see :func:`claim_placement`), and ``note`` when the agent gave
    one (:func:`clean_guide_text`). Raises
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
        extra = sorted(set(item) - {"id", "params", "note"})
        if extra:
            raise GuideCatalogError(
                "invalid_actions", f"action {index}: unknown field '{extra[0][:64]}'"
            )
        action_id = item.get("id")
        if not isinstance(action_id, str) or action_id not in ACTIONS:
            raise GuideCatalogError("unknown_action", f"action {index}: unknown action id")
        # One note per action, shown under its FINAL step: every action has at
        # least one step, and the final one is the target in every ui.show
        # placement, so a note can never outnumber or misalign with the steps.
        note = clean_guide_text(item.get("note"), f"action {index} note", MAX_NOTE_CHARS)
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
        if note:
            record["note"] = note
        out.append(record)
    return out


def warm_catalogs() -> None:
    """Read every packaged file this module caches, so later calls are cache hits.

    Blocking IO: parsing the UI index alone is hundreds of kilobytes. Async
    callers run this in a worker thread (``asyncio.to_thread``) before they
    touch the catalog on the event loop, where each cached reader then only
    stats its sources.
    """
    guidable_settings()
    ui_show_plans()
    ui_auto_tier()
    ui_build_manifest()
    ui_find_routes()
    _ui_find_index_facts()
