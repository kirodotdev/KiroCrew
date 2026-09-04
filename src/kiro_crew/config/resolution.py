"""Raw config overlay, preservation, and degraded-input resolution.

The loader imports and re-exports this module's names as its compatibility
facade.  Keep this module one-way: it must not import the loader, schema, or
validation modules.
"""

from __future__ import annotations

import logging
from dataclasses import fields, is_dataclass
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

# Section keys that to_dict() emits from somewhere OTHER than the section's own
# dataclass fields. They are recognized (never captured as unknown) for two
# distinct reasons, both of which make capture wrong:
#
#   * ``channels`` / ``dm_activation`` / ``trusted_bot_ids`` are emitted
#     CONDITIONALLY and dropped when empty, so their absence from the emitted
#     document is a deliberate deletion. Restoring them from a load-time capture
#     would resurrect a channel or an allow-list the caller just cleared.
#   * ``observe_max_messages`` / ``observe_ttl_hours`` are stored on the
#     TOP-LEVEL config object, not on SlackConfig, and to_dict() writes them into
#     the slack section unconditionally. Capturing them would be pure noise.
#
# Drift is caught by test_default_emitted_section_keys_are_all_recognized: every
# key a default config emits per section must be recognized here or be a field.
_SECTION_KEYS_EMITTED_ELSEWHERE: dict = {
    "slack": frozenset(
        {
            "channels",
            "dm_activation",
            "trusted_bot_ids",
            "observe_max_messages",
            "observe_ttl_hours",
        }
    ),
}

# Section keys this build deliberately does NOT round-trip. Capture must skip
# them, because preserving them defeats the decision that removed them.
#
# Two reasons land here, and the distinction matters when adding an entry:
#   * RENAMED — the reader still accepts the old spelling but save() settles on
#     the canonical one. Pinning the old spelling would make the migration never
#     finish.
#   * RETIRED — the feature behind the key is gone. Re-persisting the key would
#     keep offering a setting with nothing behind it (see
#     TestSttRemovedFieldsAreInert).
#
# The failure direction of THIS map is deliberate: forgetting an entry PRESERVES
# a key that could have been dropped, which is cosmetic. The reverse — dropping a
# key that should have been preserved — is the data loss this whole mechanism
# exists to stop, so preservation is the default and every exception is written
# down here. (_SECTION_KEYS_EMITTED_ELSEWHERE above does NOT share this safety:
# forgetting a conditionally-emitted key there resurrects a cleared value, and
# the default-config drift guard cannot see a key that is empty by default.)
_SECTION_KEYS_DELIBERATELY_DROPPED: dict = {
    # RENAMED: agent.dangerously_skip_permissions is also read as the camelCase
    # `dangerouslySkipPermissions` (the spelling other agent tools use) and the
    # legacy `yolo`; save() writes only the canonical snake_case. Preserving the
    # older spellings would leave two of them in the file at once, so a user who
    # turns the canonical grant OFF could still have an old `yolo: true` sitting
    # beside it — a contradiction in the one key that controls standing,
    # unattended auto-approval. See sections._read_dangerously_skip_permissions.
    "agent": frozenset({"dangerouslySkipPermissions", "yolo"}),
    # RENAMED: knowledge.auto_add_documents was auto_ingest_doc_links;
    # sections._read_auto_add_documents still reads the old spelling.
    "knowledge": frozenset({"auto_ingest_doc_links"}),
    # RETIRED: the out-of-band installs the removed local STT providers needed.
    "stt": frozenset({"whisper_path", "mlx_model", "parakeet_model", "device"}),
}


#: Sections that are MAPS OF NAMED RECORDS, each record a dataclass: the user
#: picks the record names, so the names themselves are never "unknown", but a
#: key INSIDE a record is parsed field-by-field and re-emitted with ``asdict``
#: exactly like a section field — so an unmodelled ``agents.<name>.<key>`` was
#: dropped on save just as ``agent.<key>`` was. Captured one level deeper, as
#: ``{section: {record_name: {key: value}}}``. ``hooks`` is NOT here: it is
#: emitted raw (``"hooks": self.hooks``) and round-trips whole.
_RECORD_MAP_SECTIONS: frozenset = frozenset({"agents", "workspaces", "memory_stores"})


def _unknown_keys_of(raw: dict, obj: object, recognized_extra: frozenset) -> dict:
    recognized = {f.name for f in fields(obj)} | recognized_extra  # type: ignore[arg-type]
    return {k: v for k, v in raw.items() if k not in recognized and not k.startswith("_")}


def capture_extra_section_keys(data: dict, cfg: object) -> dict:
    """Unknown keys found INSIDE a modelled section, per section.

    The top-level counterpart of this is ``_extra_sections``: a whole section
    this core does not model survives a load/save round-trip. A key nested one
    level down had no such protection, so when a build stopped modelling
    ``<section>.<key>`` — a rename, a removal, or simply an older config written
    by a newer edition — the load ignored it and the next ``save()`` (triggered
    by anything at all: a log-level change, adding a workspace, editing an
    agent) rewrote the file without it. The operator's value was gone, silently,
    with no backup on that path.

    Two shapes of section are covered. A dataclass-backed section yields
    ``{key: value}``. A map of named records (``_RECORD_MAP_SECTIONS``) yields
    ``{record_name: {key: value}}``, because each record is itself parsed
    field-by-field and would otherwise lose its unmodelled keys the same way.
    ``hooks`` is neither: it is emitted raw and round-trips whole.

    A key is recognized — and so NOT captured — when it is a field of the
    section's dataclass (including private carriers, which to_dict() drops on
    purpose), when it is listed in ``_SECTION_KEYS_EMITTED_ELSEWHERE`` or
    ``_SECTION_KEYS_DELIBERATELY_DROPPED``, or when it starts with an underscore.
    Note what this deliberately does NOT cover: a key the schema DOES model but
    validation rejected (a bad enum value from an older build) stays dropped,
    because re-emitting it would make the rejection permanent and re-warn on
    every load.

    Capture is a no-op safety net rather than an override: to_dict() restores a
    captured key only if the emitted section lacks it, so a stale copy can never
    clobber a live value.
    """
    captured: dict = {}
    for section, raw in data.items():
        if section not in _KNOWN_CONFIG_SECTIONS or not isinstance(raw, dict):
            continue
        section_obj = getattr(cfg, section, None)
        extra = _SECTION_KEYS_EMITTED_ELSEWHERE.get(
            section, frozenset()
        ) | _SECTION_KEYS_DELIBERATELY_DROPPED.get(section, frozenset())

        if section in _RECORD_MAP_SECTIONS and isinstance(section_obj, dict):
            per_record: dict = {}
            for name, entry in raw.items():
                record = section_obj.get(name)
                # A flat legacy entry (workspaces used to be plain strings) has
                # no nested keys to lose; a record the load did not build (it was
                # not a dict, or was rejected) has nothing to compare against.
                if not isinstance(entry, dict) or record is None or not is_dataclass(record):
                    continue
                unknown = _unknown_keys_of(entry, record, extra)
                if unknown:
                    per_record[name] = unknown
            if per_record:
                logger.debug(
                    "config: preserving unmodelled %s record key(s) on round-trip: %s",
                    section,
                    ", ".join(f"{n}.{k}" for n, ks in sorted(per_record.items()) for k in ks),
                )
                captured[section] = per_record
            continue

        if section_obj is None or isinstance(section_obj, type) or not is_dataclass(section_obj):
            continue
        unknown = _unknown_keys_of(raw, section_obj, extra)
        if unknown:
            logger.debug(
                "config: preserving unmodelled %s key(s) on round-trip: %s",
                section,
                ", ".join(sorted(unknown)),
            )
            captured[section] = unknown
    return captured


def restore_extra_section_keys(emitted: dict, extra_keys: dict) -> None:
    """Fill captured unknown keys back into *emitted* (the to_dict() document).

    Fill only — a key already present in the emitted document is never
    overwritten, so a captured copy can neither clobber a live value nor undo a
    deliberate deletion. Handles both capture shapes (see
    :func:`capture_extra_section_keys`). Owned here beside the capture so the
    two shapes cannot drift apart.
    """
    for section, unknown in extra_keys.items():
        node = emitted.get(section)
        if not isinstance(node, dict):
            continue
        if section in _RECORD_MAP_SECTIONS:
            for name, record_unknown in unknown.items():
                record = node.get(name)
                if not isinstance(record, dict):
                    continue  # the record was deleted in memory: stay deleted
                for k, v in record_unknown.items():
                    if k not in record:
                        record[k] = v
            continue
        for k, v in unknown.items():
            if k not in node:
                node[k] = v


def drop_extra_section_keys(document: dict, extra_keys: dict) -> None:
    """Remove captured unknown keys from *document* (a browser-facing view).

    The masked config response masks by SCHEMA path, so an unknown key is
    invisible to its sensitivity walk — a credential a previous build stored
    under a since-renamed key would ship verbatim. Preserving such a key for
    save() is the point of the capture; showing it to the browser is not. Same
    shape handling as :func:`restore_extra_section_keys`.
    """
    for section, unknown in extra_keys.items():
        node = document.get(section)
        if not isinstance(node, dict):
            continue
        if section in _RECORD_MAP_SECTIONS:
            for name, record_unknown in unknown.items():
                record = node.get(name)
                if isinstance(record, dict):
                    for k in record_unknown:
                        record.pop(k, None)
            continue
        for k in unknown:
            node.pop(k, None)


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
