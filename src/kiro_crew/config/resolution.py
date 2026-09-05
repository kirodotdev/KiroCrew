"""Raw config overlay, preservation, and degraded-input resolution.

The loader imports and re-exports this module's names as its compatibility
facade.  Keep this module one-way: it must not import the loader, schema, or
validation modules.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("kiro_crew.config.loader")


# Top-level config.json keys that save() stamps itself rather than modelling as
# a section. They are neither parsed into a field nor round-tripped through
# to_dict(), so every consumer that classifies top-level keys — the
# _extra_sections capture below and validation.py's unrecognized-key warning —
# must exclude them, or Kiro Crew warns the user about a key it wrote itself.
CONFIG_RESERVED_TOP_KEYS: frozenset = frozenset({"meta"})

# Top-level config.json sections this core models AND round-trips through
# to_dict(). Any other top-level key found at load() is captured into
# KiroCrewConfig._extra_sections and re-emitted by to_dict() so an
# edition-contributed section (written by a companion) survives the save()/PATCH
# round-trip instead of being silently dropped.
#
# INVARIANT: this set must equal the top-level keys to_dict() emits (guarded by
# test_config_extra_sections_roundtrip's parity test). It is the *emitted* set,
# not merely the *parsed* set: a section this core parses into a field must ALSO
# be emitted by to_dict() to be listed here — otherwise it would be excluded
# from _extra_sections capture yet dropped by to_dict(), losing it on save().
_KNOWN_CONFIG_SECTIONS: frozenset = frozenset(
    {
        "agent",
        "session",
        "memory",
        "slack",
        "publish",
        "telegram",
        "discord",
        "webex",
        "wakatime",
        "wecom",
        "weixin",
        "whatsapp",
        "feishu",
        "teams",
        "imessage",
        "dashboard",
        "tunnel",
        "hooks",
        "agents",
        "default_agent",
        "workspaces",
        "default_workspace",
        "memory_stores",
        "default_memory_store",
        "stt",
        "computer_use",
        "instances",
        "mcp_gateway",
        "mcp",
        "taskrunner",
        "orchestrator",
        "watchdog",
        "resource_limits",
        "messaging",
        "cron_history",
        "knowledge",
        "heartbeat",
        "skills",
        "session_summary",
        "telemetry",
        "snapshot_dir",
        "timezone",
        "auto_update",
        "registries",
        "connections_ui",
    }
)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into *base*, returning a new dict.

    - Dict values are merged recursively
    - All other types in overlay replace base values
    - Keys in overlay not in base are added
    """
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _subtract_overlay(merged: dict, overlay: dict) -> dict:
    """Remove leaf values from *merged* that are owned by the overlay.

    For nested dicts, recurse. For leaf keys present in both overlay and
    merged with the same value, remove from the result so they only live
    in config.local.json.
    """
    result = dict(merged)
    for key, ov_value in overlay.items():
        if key not in result:
            continue
        if isinstance(ov_value, dict) and isinstance(result[key], dict):
            cleaned = _subtract_overlay(result[key], ov_value)
            if cleaned:
                result[key] = cleaned
            else:
                del result[key]
        elif result[key] == ov_value:
            del result[key]
    return result


#: Marker used in :attr:`KiroCrewConfig.degraded_sections` for "a whole config
#: FILE could not be read" (unparseable, or a top level that is not a JSON
#: object), as opposed to one named section being malformed. A gate that reads
#: any security value must treat it exactly like its own section being
#: degraded: the operator's settings are unknown either way.
DEGRADED_WHOLE_CONFIG = "*"

#: ``degraded_sections`` key for "the operator wrote a tailnet identity policy
#: this load could not read". Its own key rather than reusing ``"dashboard"``
#: so the tailnet gate denies on exactly the narrowing it enforces, and an
#: unrelated malformed ``dashboard`` value does not.
DEGRADED_TAILSCALE = "dashboard.tailscale"


def tailnet_identity_unknown(sections: frozenset[str]) -> bool:
    """Whether *sections* means the tailnet login allowlist could not be read.

    Three shapes lose it, at three depths: an unreadable config FILE, an
    unreadable ``dashboard`` section, and an unreadable ``dashboard.tailscale``
    value. All three resolve ``allowed_logins`` to the empty list, which the
    loader turns into ``trust_identity = False`` — i.e. no login restriction —
    so the gate has to treat them alike.

    Lives here, beside the keys it names, because BOTH server startup surfaces
    ask it. Asking twice in two places is how the tailnet feature shipped a
    drift bug before (see ``governed_tailnet_trust``).
    """
    return bool(sections & {DEGRADED_WHOLE_CONFIG, "dashboard", DEGRADED_TAILSCALE})


def tailnet_effective_allowed_logins(
    sections: frozenset[str], allowed_logins: list[str] | tuple[str, ...]
) -> tuple[str, ...]:
    """The parsed allowlist reduced to what is safe to ENFORCE from *sections*.

    Degradation comes in two severities and only one of them invalidates the
    value that was parsed:

    * A whole config FILE could not be read (:data:`DEGRADED_WHOLE_CONFIG`).
      ``config.local.json`` is deep-merged OVER ``config.json``, and an overlay
      may exist precisely to NARROW the base -- so a lost overlay leaves the
      base's wider list standing and every login the operator removed is
      admitted again. The parsed list is not the effective policy, it is a
      stale one, so nothing may be enforced from it: the allowlist is empty and
      every peer is denied, matching how the publish gate treats an unreadable
      file.
    * A section or field inside a file that WAS read (``dashboard``,
      :data:`DEGRADED_TAILSCALE`). Whatever parsed is literally what the
      operator wrote in a file this load could see, so it is kept: access
      narrows to the entries that survived, and the administrator whose own
      login parsed fine is not locked out mid-repair.

    Both cases still enforce -- see :func:`tailnet_identity_unknown`. This
    decides only WHOSE logins may satisfy that enforcement.
    """
    if DEGRADED_WHOLE_CONFIG in sections:
        return ()
    return tuple(allowed_logins)


#: Sections observed malformed by ANY read in this process, remembered for its
#: lifetime.
#:
#: Stickiness is the point, not an optimization. ``load()`` runs a migration
#: that REWRITES ``config.json`` in normalized form, so the very first load
#: repairs the file: a second load — including the one a security gate makes
#: moments later in the same request — sees a clean file with the malformed
#: section silently gone, and an empty allowlist that reads as "operator
#: configured nothing". Remembering keeps the answer truthful for as long as the
#: process could still act on that value.
#:
#: The operator's fix is to correct the file and restart the gateway, which is
#: the same ceremony every other boot-time config decision already requires.
_OBSERVED_DEGRADED_SECTIONS: set[str] = set()


def reset_degraded_observations() -> None:
    """Forget every degradation this process has observed.

    The observations are deliberately sticky for the life of a gateway (see
    :data:`_OBSERVED_DEGRADED_SECTIONS`), so the ONLY legitimate callers are
    tests, which share one interpreter and would otherwise let one case's
    malformed config deny in the next. Production clears it by restarting,
    which is the same ceremony every other boot-time config decision requires.
    """
    _OBSERVED_DEGRADED_SECTIONS.clear()


def _mark_file_degraded(path: Path) -> None:
    """Record that a whole config FILE could not be read as a JSON object.

    Adds both the generic marker (so a gate can ask one question) and the file's
    name (so the refusal can tell the operator which file to go and fix).
    """
    _OBSERVED_DEGRADED_SECTIONS.add(DEGRADED_WHOLE_CONFIG)
    _OBSERVED_DEGRADED_SECTIONS.add(f"{DEGRADED_WHOLE_CONFIG}{path.name}")


def degraded_config_files(sections: frozenset[str]) -> list[str]:
    """The config file names inside a ``degraded_sections`` set."""
    return sorted(
        s[len(DEGRADED_WHOLE_CONFIG) :]
        for s in sections
        if s.startswith(DEGRADED_WHOLE_CONFIG) and s != DEGRADED_WHOLE_CONFIG
    )


def _coerced_section(data: dict, key: str, degraded: set[str]) -> dict:
    """Return ``data[key]`` as a dict, RECORDING the coercion when it is not one.

    The loader must keep degrading — a malformed section cannot be allowed to
    take the whole process down — but it must stop doing so SILENTLY. Every
    section read goes through here so the "was this value real, or invented by
    the parser" question has one answer for every consumer, instead of each
    security gate growing its own shadow parser beside the loader (#4057).

    An ABSENT section is not degraded: that is the genuine unconfigured state.
    """
    if key not in data:
        return {}
    value = data[key]
    if isinstance(value, dict):
        return value
    degraded.add(key)
    _OBSERVED_DEGRADED_SECTIONS.add(key)
    logger.warning(
        "config: '%s' section is not a JSON object (got %s) — using defaults; "
        "any setting it carried is NOT in effect",
        key,
        type(value).__name__,
    )
    return {}


def _fail_closed_project_skills_config(
    data: dict, *, config_source_unreadable: bool = False
) -> None:
    """Preserve the project-skills off-switch's fail-closed semantics.

    Optional JSON Schema validation removes invalid fields before dataclass
    construction. Normalizing this security switch first keeps an invalid
    value distinct from an absent value, whose documented default is enabled.
    """
    if config_source_unreadable:
        skills = data.get("skills")
        if not isinstance(skills, dict):
            skills = {}
            data["skills"] = skills
        skills["project_skills_enabled"] = False
        return

    if "skills" not in data:
        return

    skills = data["skills"]
    if not isinstance(skills, dict):
        data["skills"] = {"project_skills_enabled": False}
        return

    if "project_skills_enabled" in skills and not isinstance(
        skills["project_skills_enabled"], bool
    ):
        skills["project_skills_enabled"] = False
