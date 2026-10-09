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
  condition, not destructive), and its steps come ONLY from that plan: a
  version-2 plan lists, per placement (viewport), step ids. The claiming tab
  names the placement it walks and the gateway records that placement's step
  ids (:func:`claim_placement`); progress may name only those. It only points:
  every step is a ``ui`` step and nothing is clicked or saved. The record also
  carries the index's ``build_digest``; a tab whose bundle carries another
  refuses the guide (``build_mismatch``) instead of walking it. An AUTO
  location (``auto:<page>:<label key>``) is accepted only when the build-time
  auto tier the dashboard bundle ships (:func:`ui_auto_tier`) hangs off this
  very index and gave it ``guide_policy: point`` and a single-step plan; its
  record carries that plan and the auto tier's own digest, which only a bundle
  stamped by the same build carries. Without the file, ``unknown_location``.

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
from typing import Any

logger = logging.getLogger(__name__)

ACTION_SETTINGS_SHOW = "settings.show"
ACTION_CREWMATE_CREATE = "crewmate.create"
ACTION_MCP_OPEN_ADD = "mcp.open_add"
ACTION_UI_SHOW = "ui.show"

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
_UI_SHOW_MAX_STEPS = 6
#: The ``ui.show`` plan format this gateway walks (the generator's ``GUIDE_PLAN_VERSION``).
UI_SHOW_PLAN_VERSION = 2

_REGISTRY_PATH = Path(__file__).resolve().parent / "docs" / "settings-registry.generated.json"
_UI_INDEX_PATH = Path(__file__).resolve().parent / "docs" / "ui-index.generated.json"
#: The build-time auto tier, shipped in the dashboard bundle and never committed
#: (the same file :data:`kiro_crew.ui_index.AUTO_INDEX_PATH` names).
_UI_AUTO_INDEX_PATH = Path(__file__).resolve().parent / "static" / "dist" / "ui-index.auto.json"
#: Ceiling on the auto tier file this module reads (the find_ui loader's own).
_UI_AUTO_MAX_BYTES = 8 * 1024 * 1024

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


#: Caps on the agent's own words in a guide: the guide-level ``intro`` (offer
#: card and first step) and one action's ``note`` (shown under its final step).
MAX_INTRO_CHARS = 200
MAX_NOTE_CHARS = 160

#: Anything a renderer could read as a link or as markup. The browser renders the
#: text as React text either way; refusing these keeps an agent from presenting a
#: link or formatting the dashboard did not draw.
_GUIDE_TEXT_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)")
_GUIDE_TEXT_MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
_GUIDE_TEXT_MARKUP = ("<", ">", "`")
#: Bidirectional embedding/override/isolate controls: they can make text read
#: differently from what it is. Other format characters (e.g. the joiner inside
#: an emoji) are ordinary text.
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def clean_guide_text(raw: object, field_name: str, max_chars: int) -> str:
    """Validate one piece of agent-authored guide text into what is stored.

    Plain text only: line breaks and tabs collapse to single spaces, any other
    control character is refused, as are links (``http(s)://``, ``www.``) and
    markup (``<``, ``>``, backticks, ``[text](target)``). Over the cap is
    REFUSED with the length, never truncated, so the agent can shorten it.
    ``None`` or blank means absent and returns ``""``.
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
    if any(ch in text for ch in _GUIDE_TEXT_MARKUP) or _GUIDE_TEXT_MD_LINK_RE.search(text):
        raise GuideCatalogError(
            "invalid_text", f"{field_name}: plain text only, no markup, HTML or backticks"
        )
    if needs_redaction(text):
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
                }
            },
            "required": ["location_id"],
            "additionalProperties": False,
        },
        mutates=False,
        steps_per_record=True,
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
_ID_PART_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")


def _step_is_well_formed(st: dict[str, Any]) -> bool:
    """A pointing step names a location; a ``select`` step its picker and
    selection; a ``gate`` step its gate (and maybe the setting that turns it on)
    and no location."""
    kind = st.get("kind")
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


@functools.lru_cache(maxsize=1)
def ui_show_plans() -> dict[str, dict[str, Any]]:
    """Packaged location id -> its ``guide_plan``, for every guidable location.

    Read once from the index this build ships; a location the generator gave no
    plan (a runtime condition, a destructive control, a deny-listed id) is simply
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


@dataclass(frozen=True)
class UiAutoTier:
    """The pointable part of the build-time auto tier (see :func:`ui_auto_tier`).

    ``build_digest`` is the auto tier's own (empty: no usable tier), ``plans``
    each ``point`` location's single-step plan, ``sites`` its one render-site
    id (what the bundle stamped as ``data-ui-auto``).
    """

    build_digest: str
    plans: dict[str, dict[str, Any]]
    sites: dict[str, str]


def _auto_plan_site(lid: str, plan: object) -> str | None:
    """The site id of a well-formed auto plan: one placement, one pointing step at one site."""
    if not _AUTO_LOCATION_RE.match(lid) or not _plan_is_well_formed(plan):
        return None
    assert isinstance(plan, dict)
    placements = plan["placements"]
    if len(placements) != 1 or len(placements[0]["steps"]) != 1:
        return None
    site = placements[0]["steps"][0].get("location")
    return site if isinstance(site, str) and _AUTO_SITE_RE.match(site) else None


@functools.lru_cache(maxsize=1)
def ui_auto_tier() -> UiAutoTier:
    """The auto tier's ``point`` plans, read once from the dashboard bundle.

    Refused whole (an empty tier, so every auto id is ``unknown_location``)
    unless the artifact names THIS committed index twice over: its
    ``base_input_digest`` is the index's ``input_digest`` and its
    ``base_build_digest`` the index's ``build_digest``. Only ``tier: auto``
    locations with ``guide_policy: point`` and a well-formed single-step plan
    are kept; ``search-only`` and ``deny`` ones are never guidable.
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
        if not isinstance(lid, str) or loc.get("guide_policy") != "point":
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


@functools.lru_cache(maxsize=1)
def ui_build_manifest() -> UiBuildManifest:
    """The packaged index's build manifest, read once (see :class:`UiBuildManifest`)."""
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
    _exact_keys(params, {"location_id"}, ACTION_UI_SHOW)
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
    # Derived per record: every placement's step ids (version 2), and the build
    # the plan came from, which a tab built from another one refuses
    # (``build_mismatch``). Which placement is walked is the claiming tab's
    # choice (its viewport); ``claim`` records it with its step ids, step
    # count and kinds (every step a UI step: the guide only points).
    return {"location_id": lid}, {
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


_VALIDATORS = {
    ACTION_SETTINGS_SHOW: _validate_settings_show,
    ACTION_CREWMATE_CREATE: _validate_crewmate_create,
    ACTION_MCP_OPEN_ADD: _validate_mcp_open_add,
    ACTION_UI_SHOW: _validate_ui_show,
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
