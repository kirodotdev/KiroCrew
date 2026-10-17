"""The ``.crewmate`` bundle reader (``docs/request-for-change/rfc-crewmate-share.md`` §5.2).

Pure: turns one parsed bundle an app hands ``ctx.crewmate.propose`` into the
``crewmate.import`` change-card params, failing closed on anything this version
does not read.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew import change_card_catalog as catalog

CREWMATE_FORMAT = "kirocrew.crewmate"
_BUNDLE_KEYS = {"crewmate", "agent", "rules", "goals", "crons", "avatar"}
_MANIFEST_HINTS = ("modelHint", "reasoningEffortHint", "dashboardTemplate")
_MANIFEST_KEYS = {
    *"format formatVersion name description avatarTraits skills".split(),
    *_MANIFEST_HINTS,
}
_MANIFEST_KEYS |= {"sender", "exportedAt"}
_TEMPLATE_ID_RE = re.compile(r"^(?:builtin|user):[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _bundle_part(bundle: dict[str, Any], key: str, kind: type, *, required: bool = False) -> Any:
    value = bundle.get(key)
    if value is None and not required:
        return kind()
    if not isinstance(value, kind):
        raise catalog._fail(
            "invalid_bundle", f"'{key}' must be a{'n object' if kind is dict else ' list'}"
        )
    return value


def crewmate_import_params(bundle: Any) -> dict[str, Any]:
    """The ``crewmate.import`` params for one parsed ``.crewmate`` bundle.

    Fails closed on anything this version does not read: an unknown key in the
    bundle, ``crewmate.json``, ``agent.json``, ``goals.json``, ``rules.json``, the
    sender or the avatar marker, another format or a newer version, a grant, page HTML, and, until the
    mate-owned agent file exists (RFC §5.7), any MCP server or skill. Rules are
    refused too: importing without them would give the crewmate fewer limits
    than its sender set, and a rules file outlives its crewmate, so the card's
    Undo could not take them back. Crons and key results are only counted for
    the card's "Not brought over" row; their entries are not read.
    """
    if not isinstance(bundle, dict):
        raise catalog._fail("invalid_bundle", "the bundle must be an object")
    catalog._only(bundle, _BUNDLE_KEYS, "bundle")
    m = _bundle_part(bundle, "crewmate", dict, required=True)
    catalog._only(m, _MANIFEST_KEYS, "crewmate.json")
    if m.get("format") != CREWMATE_FORMAT:
        raise catalog._fail("invalid_bundle", "this is not a .crewmate file")
    version = m.get("formatVersion")
    if type(version) is not int or version < 1:
        raise catalog._fail("invalid_bundle", "'formatVersion' must be a whole number")
    if version not in catalog.CREWMATE_FORMAT_VERSIONS:
        raise catalog._fail("newer_format", "this file needs a newer Kiro Crew")
    agent = _bundle_part(bundle, "agent", dict, required=True)
    grant = sorted(set(agent) & {"allowedTools", "autoApprove", "permissions"})
    if grant:
        raise catalog._fail("grant_in_bundle", f"a bundle never carries '{grant[0]}'")
    catalog._only(agent, {"mcpServers", "skills"}, "agent.json")
    if agent.get("mcpServers") or agent.get("skills") or m.get("skills"):
        raise catalog._fail(
            "needs_mate_config", "this version cannot import MCP servers or skills yet"
        )
    rules = _bundle_part(bundle, "rules", dict)
    catalog._only(rules, {"rules"}, "rules.json")
    if rules.get("rules"):
        raise catalog._fail("rules_unsupported", "this version cannot import rules yet")
    goals = _bundle_part(bundle, "goals", dict)
    catalog._only(goals, {"longTerm", "keyResults"}, "goals.json")
    avatar = _bundle_part(bundle, "avatar", dict)
    catalog._only(avatar, {"type", "size"}, "avatar")
    sender = _bundle_part(m, "sender", dict)
    catalog._only(sender, {"label"}, "sender")
    for key in (*_MANIFEST_HINTS, "exportedAt"):
        catalog._clean_str(m, key, required=False, max_len=72, allow_empty=True)
    template = m.get("dashboardTemplate")
    if template and not _TEMPLATE_ID_RE.fullmatch(template):
        raise catalog._fail("invalid_bundle", "'dashboardTemplate' must be a builtin: or user: id")
    carried = {
        "crons": _bundle_part(bundle, "crons", list),
        "keyResults": _bundle_part(goals, "keyResults", list),
        "avatar": avatar or _bundle_part(m, "avatarTraits", dict),
        "model": (m.get("modelHint") or "").strip() or (m.get("reasoningEffortHint") or "").strip(),
        "dashboardTemplate": (template or "").strip(),
    }
    left_out = [code for code, part in carried.items() if part]
    params = {
        "name": m.get("name"),
        "format_version": version,
        "description": m.get("description"),
        "goal": goals.get("longTerm") or None,
        "sender": sender.get("label"),
        "not_imported": left_out,
    }
    return catalog.validate_params(catalog.KIND_CREWMATE_IMPORT, params)
