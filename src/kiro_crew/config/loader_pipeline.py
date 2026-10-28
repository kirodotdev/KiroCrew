"""The load pipeline and the one-call loaders built on ``KiroCrewConfig``.

``read_config_document``, ``build_config`` and ``persist_write_back``, and
``workspace_dir_for``, ``default_project_dir``, ``load_config_with_content_stamp`` and
``load_loop_stall_exit_after``. An owner of the :mod:`kiro_crew.config.loader` facade;
it never imports the facade.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import kiro_crew.config.resolution as _resolution
from kiro_crew.atomic_write import atomic_write
from kiro_crew.aws_project_profiles import coerce_project_profiles
from kiro_crew.config import migration as _migration
from kiro_crew.config import sections as _sections
from kiro_crew.config.fields import _coerce_bool, _safe_str
from kiro_crew.config.integration_sections import AppSettingsConfig, AwsConfig
from kiro_crew.config.loader_agents import _crew_autocompact_pct
from kiro_crew.config.loader_builders import (
    _build_agent_config,
    _build_dashboard_config,
    _build_session_config,
    _build_telemetry_config,
    _clamp_security_bounds,
    _default_memory_mode_from,
    _read_backend_choice,
    _registry_entry,
    resolve_loop_stall_exit_after,
)
from kiro_crew.config.loader_config import KiroCrewConfig
from kiro_crew.config.loader_document import (
    _SIDECAR_BASE_SHADOW,
    _SIDECAR_BASE_UNREADABLE,
    _SIDECAR_OVERLAY_UNREADABLE,
    _config_fingerprint,
    _content_digest_of,
    _invalidate_config_cache,
    _shadowed_base_sections,
    _store_validated_data,
)
from kiro_crew.config.loader_locations import (
    config_local_path,
    config_path,
    read_config_text,
    workspace_dir_from_entry,
)
from kiro_crew.config.loader_snapshots import (
    default_template,
    dispatch_kiro_agent,
    next_config_load_ticket,
)
from kiro_crew.config.loader_writeback import _persist_config_migration
from kiro_crew.config.migration import (
    CONNECTIONS_UI_MIGRATION_MARKER,
    MIGRATE_AGENTS,
    MIGRATE_CONNECTIONS_UI,
    MIGRATE_DEFAULT_AGENT,
    MIGRATE_STAR_DEFAULT_TEMPLATE,
    MIGRATE_SUPERSEDED_DEFAULTS,
    MIGRATE_WORKSPACES,
    _adopt_in_memory,
    _overlay_supplies,
    _report_superseded_defaults,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.config.resolution import (
    _KNOWN_CONFIG_SECTIONS,
    _OBSERVED_DEGRADED_SECTIONS,
    CONFIG_RESERVED_TOP_KEYS,
    DEGRADED_WHOLE_CONFIG,
    _coerced_section,
    _deep_merge,
    _fail_closed_project_skills_config,
    _mark_file_degraded,
)
from kiro_crew.config.section_builders import (
    _build_computer_use_config,
    _build_cron_history_config,
    _build_discord_config,
    _build_feishu_config,
    _build_imessage_config,
    _build_instances_config,
    _build_knowledge_config,
    _build_mcp_config,
    _build_mcp_gateway_config,
    _build_memory_config,
    _build_messaging_config,
    _build_monitoring_config,
    _build_power_config,
    _build_publish_config,
    _build_session_summary_config,
    _build_skills_config,
    _build_slack_config,
    _build_stt_config,
    _build_taskrunner_config,
    _build_teams_config,
    _build_telegram_config,
    _build_tunnel_config,
    _build_wakatime_config,
    _build_watchdog_config,
    _build_webex_config,
    _build_wecom_config,
    _build_weixin_config,
    _build_whatsapp_config,
    _build_work_ledger_config,
)
from kiro_crew.config.sections import (
    AgentConfig,
    ChannelConfig,
    DecisionsConfig,
    HeartbeatConfig,
    InstancesConfig,
    KiroCrewAgentConfig,
    MemoryStoreConfig,
    MonitoringConfig,
    PublishConfig,
    ResourceLimitsConfig,
    SkillsConfig,
    WorkspaceConfig,
    _migrate_workspaces,
    _safe_bool,
    _safe_color,
    _safe_float,
    _safe_int,
    _validate_activation,
    coerce_effort,
)
from kiro_crew.config.superseded_defaults import SupersededDefault, auto_adoptable
from kiro_crew.config.validation import _CONFIG_CACHE
from kiro_crew.config.validation import validate_config_data as _validate_config_data
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, memory_store_name_defect

logger = logging.getLogger("kiro_crew.config.loader")


def workspace_dir_for(workspace: str | None = None) -> Path:
    """Resolve a named workspace to its directory path.

    Reads the LOADED config's ``workspaces`` table, not the raw bytes on disk, so
    this and ``resolve_agent_bindings`` cannot answer the same question two ways:
    the loaded table is the one the validator has repaired and the write-back
    migration has normalized, and it is what every other consumer already reads.

    Values starting with ``/`` or ``~`` are treated as absolute paths.
    Otherwise the value is relative to ``config_dir()`` (``~/.kiro/crew/``).

    An unmapped name falls back to the ``WorkspaceConfig`` default directory —
    NOT to ``default_workspace``'s directory, which could be an absolute path
    outside the data home — and the fall-through is LOGGED. Two DISTINCT names
    both resolving to the same ``<home>/workspace`` is how a workspace split
    turns into a shared tree nobody notices, so that case warns. An install that
    simply has no ``workspaces`` section yet is the ordinary fresh state and logs
    at debug — it is one workspace, not a collision.

    Never raises. ``default_project_dir`` and the workspace-identity block are
    built on this, so a raise here would break a fresh install and take both
    with it; a config that cannot be loaded resolves to the default directory.
    """
    mapping: dict[str, WorkspaceConfig] = {}
    ws = workspace or ""
    configured_default = ""
    try:
        cfg = KiroCrewConfig.load()
        mapping = cfg.workspaces
        configured_default = cfg.default_workspace
        ws = workspace or configured_default
    except Exception:
        logger.warning(
            "could not load config to resolve workspace %r; using the default directory",
            workspace,
            exc_info=True,
        )

    entry = mapping.get(ws)
    if entry is None:
        # An unmapped name resolves to the BASE workspace directory, never to the
        # configured default's directory. That distinction is the boundary: this
        # answer becomes a filesystem root, and one caller
        # (``dashboard/handlers/files.py``'s ``?workspace=``) takes the name from a
        # request without the ``is_sensitive_path`` check its ``?project=`` sibling
        # applies. Hopping to ``default_workspace`` would let an unrecognized name
        # reach whatever absolute ``dir`` that workspace declares, so an unmapped
        # name stays inside the data home by construction.
        #
        # It is still a fail-OPEN worth naming: two DISTINCT declared-looking names
        # both landing here share one tree, which is how a workspace split becomes a
        # shared tree nobody notices. An install with no ``workspaces`` section is
        # the ordinary fresh state and is one workspace, not a collision.
        report = logger.debug if ws == configured_default else logger.warning
        report(
            "workspace %r is not declared in workspaces; falling back to the base "
            "workspace directory",
            ws,
        )
    return workspace_dir_from_entry(entry)


def default_project_dir(workspace: str | None = None) -> str:
    """Resolve the default project directory for a workspace.

    Returns the realpath of ``workspace_dir_for(workspace)`` if it exists and
    is not a sensitive path, otherwise returns ``""``.

    Used by chat_handlers (slot.project fallback) and session.py (pool cwd)
    to avoid duplicating the same resolution + validation logic.
    """
    from kiro_crew.security import is_sensitive_path  # circular import

    try:
        ws_dir = os.path.realpath(str(workspace_dir_for(workspace)))
        if os.path.isdir(ws_dir) and not is_sensitive_path(ws_dir):
            return ws_dir
    except Exception:
        pass
    return ""


def load_config_with_content_stamp() -> tuple[KiroCrewConfig, str | None]:
    """Load the config together with the digest of the bytes it was parsed from.

    The digest comes OUT of the load rather than being read around it, which is the
    only way it can describe this object: reading the files on both sides of the call
    proves the bytes did not move during it, but the load answers from a cache keyed
    on stat metadata, so a replacement presenting the same fingerprint returns an
    earlier object while those reads hash the new bytes. The cache entry carries the
    digest of the bytes it was parsed from, so a hit reports its own provenance and a
    miss reports what it just read.

    ``None`` means no digest can be bound -- the files could not be read whole, or the
    document was unusable and defaults were substituted. A caller must treat that as
    unknown, never as a match.

    For a caller that holds the result across other I/O and later writes something
    derived from it, comparing this against :func:`config_content_stamp` is what tells
    "my copy is still current" from "a save landed while I was working".
    """
    cfg = KiroCrewConfig.load()
    return cfg, getattr(cfg, "_content_digest", None)


def load_loop_stall_exit_after(
    environ: Mapping[str, str] | None = None,
) -> int:
    """Load the effective watchdog budget through the canonical config loader.

    The dataclass keeps an absent/null value as ``None`` rather than
    materializing a launch-specific number, so an unrelated ``save()`` cannot
    turn the managed 90-second default into an explicit desktop 25 seconds (or
    leak 90 seconds into a later desktop launch). The normal validated,
    overlay-aware loader remains the single config reader.
    """
    configured = KiroCrewConfig.load().dashboard.loop_stall_exit_after_secs
    dashboard_data = {} if configured is None else {"loop_stall_exit_after_secs": configured}
    return resolve_loop_stall_exit_after(dashboard_data, environ)


@dataclass
class ConfigDocument:
    """What one read of ``config.json`` and ``config.local.json`` observed.

    :func:`read_config_document` produces it; :func:`build_config` turns it into a
    :class:`KiroCrewConfig` without touching the filesystem; :func:`persist_write_back`
    runs the write-back migration it makes due. Besides this document, the build
    reads (and extends) one process-wide input: the sticky set of degraded-section
    observations (``_OBSERVED_DEGRADED_SECTIONS``).

    ``data`` is the read stage's OUTPUT, not raw file content: the overlay is merged,
    the read-stage repairs are applied (a quoted ``"false"`` on an agent off-switch
    reads as ``false``; an unreadable source leaves ``skills.project_skills_enabled``
    off), and the document is validated and clamped. The build applies none of those
    repairs itself, so a document assembled in memory must already hold them.
    Single-use: building normalizes ``data`` in place, as the load always has.
    """

    #: Load-order ticket drawn before any read (see :func:`next_config_load_ticket`).
    ticket: int
    #: The ``config.json`` path this read resolved.
    path: Path
    #: The repaired, validated, overlay-merged document. When neither file loaded it
    #: is ``{}``, or carries only the project-skills off-switch an unreadable file
    #: leaves behind.
    data: dict
    #: False when neither file loaded: the build starts from the field defaults.
    loaded: bool
    #: Digest of the bytes ``data`` was parsed from, or ``None`` when none can be named.
    content_digest: str | None
    #: ``config.json`` was present but unreadable or not a JSON object.
    base_unreadable: bool = False
    #: ``config.local.json`` was present but unreadable or not a JSON object.
    overlay_unreadable: bool = False
    #: The base document's copy of each top-level section the overlay touched.
    base_shadow: dict = field(default_factory=dict)
    #: ``config.local.json`` as this read parsed it; ``{}`` on a cache hit.
    overlay: dict = field(default_factory=dict)
    #: Superseded defaults this read found adoptable; only a disk read finds any.
    adoptable: list[SupersededDefault] = field(default_factory=list)
    #: The 0.6.x-or-older writer stamp when the legacy ``skills.lazy_load`` rewrite is due.
    legacy_lazy_stamp: str | None = None
    #: True when this read parsed the files, False on a cache hit or an in-memory
    #: document. The build's advisory warnings fire only on a disk read, as
    #: validation's do, so a cached load does not repeat them.
    disk_read: bool = False


def read_config_document() -> ConfigDocument:
    """Read ``config.json`` and ``config.local.json`` into a :class:`ConfigDocument`.

    The stage that reads the two files and the validated-data cache to build the
    document (the write-back re-reads ``config.json`` under its lock). It draws the
    load-order ticket, takes ONE fingerprint stat pass of both files, and either serves
    the cache entry that fingerprint names or reads both files, merges the overlay,
    applies the fail-closed normalizations, validates and clamps the result and caches
    it. A
    missing, unreadable or malformed file never raises: it degrades to defaults and is
    recorded (``_mark_file_degraded``). Writes no config file: it fills the cache and,
    when a security clamp fires, records a SEL event. The superseded defaults it
    reports are logged, and only a disk read decides an adoption.
    """
    # Drawn BEFORE any read below, so it records when this load began
    # observing the files rather than when it finished. See
    # next_config_load_ticket and publish_autocompact_pct.
    ticket = next_config_load_ticket()
    path = config_path()

    # Hot-path cache: reuse the validated, merged dict when neither config
    # file has changed since the last load. Skips read + json.loads +
    # _deep_merge + the full jsonschema.validate. A deep copy is returned so
    # in-place mutation by callers (and by persist_write_back) can never corrupt
    # the cached original.
    #
    # ONE stat pass serves both consumers of it below: the cache lookup and
    # the pre-read TOCTOU fingerprint. load() runs on the event loop, so a
    # second pass would be filesystem I/O there for information already in
    # hand.
    fp = _config_fingerprint()
    # Data and sidecar come from ONE lock hold: fetched separately, a save()
    # on another thread could clear the cache between the two and hand this
    # load a merged document with an EMPTY base shadow — which would then be
    # captured from as if no overlay existed (see _shadowed_base_sections).
    cached = _CONFIG_CACHE.get_with_sidecar(fp)
    if cached is not None:
        # A cache HIT read no base document this load, and a load that did not look
        # at the file must not adopt from it, so the document carries no adoptable
        # superseded default and no legacy ``skills.lazy_load`` stamp. Empty is the
        # correct answer on the hot path, not a missing one -- the load that
        # populated the cache already decided both.
        data, sidecar, content_digest = cached
        return ConfigDocument(
            ticket=ticket,
            path=path,
            data=data,
            loaded=True,
            content_digest=content_digest,
            base_unreadable=bool(sidecar.get(_SIDECAR_BASE_UNREADABLE, False)),
            overlay_unreadable=bool(sidecar.get(_SIDECAR_OVERLAY_UNREADABLE, False)),
            base_shadow=sidecar.get(_SIDECAR_BASE_SHADOW, {}),
        )

    # Pre-overlay base copy of the sections the overlay touched. Filled below.
    # Empty when there is no overlay, in which case the merged document IS the base.
    base_shadow: dict = {}
    # The writer stamp of 0.6.x or older the base document carried when the legacy
    # ``skills.lazy_load`` rewrite is due, or None.
    legacy_lazy_stamp: str | None = None
    base_unreadable = False
    overlay_unreadable = False
    # Capture the invalidation generation BEFORE disk I/O. A successful
    # write advances it, so this read cannot repopulate pre-write data
    # after the writer clears the cache even if the filesystem's coarse
    # timestamp and unchanged size leave the fingerprint identical.
    read_generation = _CONFIG_CACHE.generation()
    # fp was captured BEFORE reading, so a write landing during the read
    # is detected: the fingerprint normally changes, and the generation
    # fence covers filesystems where a same-size replacement does not.
    # _store_validated_data documents this contract.
    pre_read_fp = fp
    data = {}
    loaded_base = False
    config_source_unreadable = False
    # The bytes, kept so the digest names exactly what was parsed rather
    # than whatever a later read would find.
    read_parts: list[bytes | None] = [None, None]
    digestible = True
    if path.exists():
        try:
            base_text = read_config_text(path)
            read_parts[0] = base_text.encode("utf-8")
            raw = json.loads(base_text)
            if isinstance(raw, dict):
                data = raw
                loaded_base = True
            else:
                config_source_unreadable = True
                base_unreadable = True
                logger.warning("Config is not a JSON object, using defaults")
                _mark_file_degraded(path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            config_source_unreadable = True
            base_unreadable = True
            # A digest names the BYTES, and a document that read whole but would
            # not parse still has bytes to name -- what it does not have is
            # faithful CONTENT, which ``degraded_sections`` is what reports. Only
            # a read that never completed leaves nothing to name.
            if read_parts[0] is None:
                digestible = False
            logger.warning("Failed to load config from %s: %s", path, e)
            _mark_file_degraded(path)

    # Report -- and, for the entries that opt in, ADOPT -- a stored BASE
    # value that still holds a superseded default, before the overlay merge
    # below: the overlay is the operator's live choice and says nothing about
    # what the base materialized.
    #
    # The two halves differ in what they may touch. Reporting is read-only
    # and covers every drifted key, because a key with a documented escape
    # hatch cannot be corrected automatically -- a stale default and a
    # deliberate opt-out are the same bytes. Adoption covers only the
    # entries whose ``auto_adopt`` says the value has no second meaning, and
    # happens at most once per key per install; ``auto_adoptable`` owns both
    # filters plus the acknowledgment one.
    #
    # Skipped when no base file loaded -- nothing is stored to adopt or
    # report on.
    adoptable = []
    if loaded_base:
        adoptable = auto_adoptable(data)
        # Decided here, on the base as the previous build left it: once this
        # load writes, or the gateway's meta refresh runs, the stamp names
        # the running build and the proof is gone.
        legacy_lazy_stamp = _migration.legacy_lazy_load_rewrite_due(
            data, connections_marker=config_dir() / CONNECTIONS_UI_MIGRATION_MARKER
        )
        report_skip = {e.dotted_key for e in adoptable}
        if legacy_lazy_stamp is not None:
            # The line must not send the operator after a key this same
            # load removes (it applies once the registry has a row for it).
            report_skip.add(_migration.LAZY_LOAD_KEY)
        _report_superseded_defaults(data, skip=report_skip)

    # Deep-merge config.local.json overlay (user-owned, never touched by setup)
    local_data: dict = {}
    local_path = config_local_path()
    if local_path.is_file():
        try:
            st_mode = local_path.stat().st_mode
            if st_mode & 0o002:
                logger.warning(
                    "config.local.json is world-writable (%o); consider running: chmod 600 %s",
                    st_mode & 0o777,
                    local_path,
                )
            local_text = read_config_text(local_path)
            read_parts[1] = local_text.encode("utf-8")
            raw_local = json.loads(local_text)
            if isinstance(raw_local, dict):
                local_data = raw_local
            else:
                config_source_unreadable = True
                overlay_unreadable = True
                logger.warning("config.local.json is not a JSON object, ignoring")
                _mark_file_degraded(local_path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            config_source_unreadable = True
            overlay_unreadable = True
            # Same rule as the base file: bytes that read whole can be named
            # whether or not they parsed.
            if read_parts[1] is None:
                digestible = False
            logger.warning("Failed to load config.local.json: %s", e)
            _mark_file_degraded(local_path)

    if local_data:
        # Remember the base copy of what the overlay is about to shadow —
        # the last moment both documents exist. See _shadowed_base_sections.
        base_shadow = _shadowed_base_sections(data, local_data)
        data = _deep_merge(data, local_data)

    # A present source that cannot be read or parsed may contain the
    # operator's hard-off switch. Preserve that unknown as disabled
    # before either the defaults return or schema normalization can
    # turn it into the enabled-by-default missing-field case.
    _fail_closed_project_skills_config(data, config_source_unreadable=config_source_unreadable)

    # Neither file loaded: hand back an unloaded document, which build_config turns
    # into field defaults and the write-back skips. Not cached — there's no file to
    # invalidate against, and the path is already cheap (existence checks only, no
    # read/parse/validate).
    if not loaded_base and not local_data:
        # Both files absent is a VALID state with bytes to name -- the digest of
        # "neither file exists" -- and naming it is what lets a caller holding
        # this config tell "still absent" from "someone just created one". Only
        # a read that never completed reaches here with nothing to name.
        return ConfigDocument(
            ticket=ticket,
            path=path,
            data=data,
            loaded=False,
            content_digest=_content_digest_of(read_parts) if digestible else None,
            base_unreadable=base_unreadable,
            overlay_unreadable=overlay_unreadable,
        )

    # Preserve fail-closed security semantics before advisory schema
    # validation can replace malformed input with a missing-field default.
    # Normalize resource_limits FIRST, for exactly that reason. Its
    # fields are declared ``int | None``, so jsonschema reads a
    # hand-edited ``512.5`` as a type violation and
    # ``_apply_field_default`` POPS the key -- deleting a ceiling the
    # parse rule would have accepted, since it truncates. That deletion
    # is not neutral: the rlimit path's fallback for a missing value is
    # ``0``, which means "leave inherited", so a 512 MB ceiling becomes
    # NO ceiling, and ``to_dict`` then persists ``null`` over what the
    # operator wrote. Normalizing here means validation sees the same
    # integers ``from_raw`` would produce; it is idempotent, so
    # build_config agrees by construction.
    if isinstance(data.get("resource_limits"), dict):
        data["resource_limits"] = asdict(ResourceLimitsConfig.from_raw(data["resource_limits"]))
    # Same fail-closed-before-validation reason for the three agent
    # switches whose safe direction is FALSE.
    # `agent.session_control` is the operator's single withdrawal of
    # cross-session control, `agent.member_dispatch` gates whether
    # a crew member bypasses that withdrawal, and `agent.crew_panel`
    # gates the member's own webview. Schema validation pops a
    # present-but-malformed value and the missing-field default is TRUE
    # for all three, so a quoted `"false"` -- a routine operator quoting
    # mistake -- would silently ride that default back to the
    # capability staying enabled. Coerce a present non-bool to False
    # HERE, so validation sees a valid bool and keeps it; a genuinely
    # absent key is left absent and still defaults to true (today's
    # behaviour). One loop, so no switch can keep the guard while
    # another loses it.
    #
    # Say so out loud. The coercion resolves a malformed value one way,
    # and an operator who meant the other way has no other signal:
    # validation sees the repaired bool and stays quiet. The line names
    # the key, the type it found and the JSON it wanted, so the fix is
    # the next thing the operator does rather than a capability they
    # find missing later.
    _agent_section = data.get("agent")
    if isinstance(_agent_section, dict):
        for _fail_closed_key in ("session_control", "member_dispatch", "crew_panel"):
            if _fail_closed_key in _agent_section and not isinstance(
                _agent_section[_fail_closed_key], bool
            ):
                logger.warning(
                    "agent.%s is %s, not a boolean; reading it as the safe "
                    "value false (the capability is OFF). Write true or false "
                    "without quotes to choose.",
                    _fail_closed_key,
                    type(_agent_section[_fail_closed_key]).__name__,
                )
                _agent_section[_fail_closed_key] = False
    # Keep a genuinely absent default backward-compatible with older
    # configs, but normalize a PRESENT malformed value before advisory
    # schema validation can delete it and turn corruption into the
    # missing-field Persistent default.
    _dashboard_section = data.get("dashboard")
    if isinstance(_dashboard_section, dict) and "default_memory_mode" in _dashboard_section:
        _dashboard_section["default_memory_mode"] = _default_memory_mode_from(
            _dashboard_section["default_memory_mode"]
        )
    # Validate against JSON Schema (advisory — never fatal)
    _validate_config_data(data)
    # Clamp security-relevant resource-limit knobs to their API ceilings
    # BEFORE caching, so a hand-edited/prompt-injected config.json that
    # exceeds a ceiling cannot drive resource exhaustion (DoS). Runs only
    # on the disk-read path; a cache hit, served above, already holds clamped values.
    _clamp_security_bounds(data)
    # Cache under the PRE-read fingerprint and generation. A mid-read
    # write either changes the fingerprint or advances the generation;
    # both paths force the next load to re-read. The base shadow rides
    # along so a hit can capture unknown keys from the base document
    # exactly as this disk read did.
    # Named from the bytes THIS read parsed, so a later hit on this entry can
    # say which content it represents. Withheld only when a file existed and
    # could not be READ whole: a document that read but would not parse still
    # has bytes to name, and that it is not faithful is what
    # ``degraded_sections`` reports instead.
    content_digest = _content_digest_of(read_parts) if digestible else None
    # Cached only when every present file read WHOLE. A transient read
    # failure (a sharing violation, an EIO) on config.json with an overlay
    # present leaves an overlay-only document here, and caching it under
    # the unchanged stat fingerprint served the base settings at their
    # defaults on every later load until a file happened to change. A file
    # whose bytes read fine but would not parse is a stable fact about
    # those bytes and still caches.
    if digestible:
        _store_validated_data(
            data,
            pre_read_fp,
            {
                _SIDECAR_BASE_SHADOW: base_shadow,
                _SIDECAR_BASE_UNREADABLE: base_unreadable,
                _SIDECAR_OVERLAY_UNREADABLE: overlay_unreadable,
            },
            expected_generation=read_generation,
            content_digest=content_digest,
        )

    return ConfigDocument(
        ticket=ticket,
        path=path,
        data=data,
        loaded=True,
        content_digest=content_digest,
        base_unreadable=base_unreadable,
        overlay_unreadable=overlay_unreadable,
        base_shadow=base_shadow,
        overlay=local_data,
        adoptable=adoptable,
        legacy_lazy_stamp=legacy_lazy_stamp,
        disk_read=True,
    )


#: The class whose field defaults an omitted top-level key reads, bound at import so a
#: test that rebinds the module's ``KiroCrewConfig`` cannot steer -- or break -- a
#: real load, which never read that global.
_TOP_LEVEL_DEFAULTS: type[KiroCrewConfig] = KiroCrewConfig


def _parse_app_settings(raw: Any) -> dict[str, AppSettingsConfig]:
    """The ``apps`` table: one :class:`AppSettingsConfig` per app name.

    A section that is not an object, a non-string name, or an entry that is not an
    object reads as absent, which means every app inherits. A ``model`` that is not
    a string reads as the empty INHERIT value. Names are kept as written: the
    resolver looks an app up by its exact manifest name, so an entry for an app
    that is not installed simply matches nothing.
    """
    if not isinstance(raw, dict):
        logger.warning(
            "Config 'apps' is not a JSON object (got %s); every app inherits its model",
            type(raw).__name__,
        )
        return {}
    apps: dict[str, AppSettingsConfig] = {}
    for name, entry in raw.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        model = _sections.SectionReader(AppSettingsConfig, entry).get("model")
        apps[name] = AppSettingsConfig(model=model if isinstance(model, str) else "")
    return apps


def build_config(
    doc: ConfigDocument, config_cls: type[KiroCrewConfig] = KiroCrewConfig
) -> KiroCrewConfig:
    """Build the :class:`KiroCrewConfig` a :class:`ConfigDocument` describes.

    No filesystem I/O. Reads *doc* and the process-wide sticky degraded set, and
    extends that set. Every section is extracted through ``_coerced_section``, so a
    present section that is not an object is recorded -- in the returned config's
    ``degraded_sections`` and in the sticky set -- and logged, then built from its
    field defaults; a degraded dashboard section (or an observed whole-file
    degradation) forces Temporary memory mode, and a malformed publish narrowing
    denies publishing. Unknown top-level sections and section keys are captured from
    the BASE view (``data`` with ``base_shadow`` over it) for the round-trip. The
    read stage's agent off-switch and project-skills repairs, validation and security
    clamp are not repeated here (``ResourceLimitsConfig.from_raw`` and the memory-mode
    parse do run again, idempotently).
    An unloaded document (neither file read) yields the field defaults plus the
    seeded default crew, with the fail-closed overrides a degraded or unreadable read
    leaves: the project-skills off-switch ``data`` carries, and Temporary memory mode
    once the dashboard section or the whole file was observed degraded. The
    write-back migration is :func:`persist_write_back`'s, so a loaded document missing
    its default agent or ``default_agent`` comes back that way.

    *config_cls* is the class to build; ``KiroCrewConfig._load_resolved`` passes its
    own ``cls``, so a load never builds whatever class a rebound module global names.
    """
    data = doc.data
    base_shadow = doc.base_shadow
    if not doc.loaded:
        # Seed the default "kirocrew" agent in-memory (matching the on-disk
        # migration in persist_write_back) so a never-setup home still lists the
        # default agent — but do NOT persist: a plain read (e.g. `agent list`) must
        # not create config files as a side effect, so the write-back skips an
        # unloaded document.
        #
        # An UNREADABLE file reaches this same "no config" branch as a
        # genuinely absent one, and the two are opposite claims for a
        # security gate: "the operator configured nothing" versus "we
        # could not read what they configured". Carry the observation
        # through so the caller can tell them apart.
        cfg = config_cls(_degraded_sections=frozenset(_OBSERVED_DEGRADED_SECTIONS))
        # An absent key, so an edition's ``default_stt()`` applies here too.
        cfg.stt.provider = _sections.default_stt_provider()
        agent_defaults = _sections.SectionReader(AgentConfig, {})
        cfg.agent.acp_backend = _read_backend_choice(agent_defaults, {}, "acp_backend")
        cfg.agent.member_acp_backend = _read_backend_choice(
            agent_defaults, {}, "member_acp_backend"
        )
        cfg._base_unreadable = doc.base_unreadable
        cfg._overlay_unreadable = doc.overlay_unreadable
        if (
            DEGRADED_WHOLE_CONFIG in _OBSERVED_DEGRADED_SECTIONS
            or "dashboard" in _OBSERVED_DEGRADED_SECTIONS
        ):
            cfg.dashboard.default_memory_mode = "temporary"
        cfg.skills.project_skills_enabled = (
            _sections.SectionReader(SkillsConfig, data.get("skills", {})).get(
                "project_skills_enabled"
            )
            is True
        )
        kiro = default_template(cfg)
        cfg.agents["default"] = KiroCrewAgentConfig(
            kiro_agent=kiro,
            workspace="default",
            memory_store="default",
        )
        cfg.default_agent = "default"
        return cfg

    # Collected during the parse that discards them — the only moment the
    # evidence exists, since the write-back rewrites config.json in
    # normalized form (see KiroCrewConfig.degraded_sections).
    _degraded: set[str] = set()
    agent_data = _coerced_section(data, "agent", _degraded)
    session_data = _coerced_section(data, "session", _degraded)
    taskrunner_data = _coerced_section(data, "taskrunner", _degraded)
    cron_history_data = _coerced_section(data, "cron_history", _degraded)
    memory_data = _coerced_section(data, "memory", _degraded)
    hooks_data = _coerced_section(data, "hooks", _degraded)
    knowledge_data = _coerced_section(data, "knowledge", _degraded)
    telegram_data = _coerced_section(data, "telegram", _degraded)
    weixin_data = _coerced_section(data, "weixin", _degraded)
    whatsapp_data = _coerced_section(data, "whatsapp", _degraded)
    feishu_data = _coerced_section(data, "feishu", _degraded)
    discord_data = _coerced_section(data, "discord", _degraded)
    webex_data = _coerced_section(data, "webex", _degraded)
    wakatime_data = _coerced_section(data, "wakatime", _degraded)
    teams_data = _coerced_section(data, "teams", _degraded)
    imessage_data = _coerced_section(data, "imessage", _degraded)
    slack_data = _coerced_section(data, "slack", _degraded)
    publish_data = _coerced_section(data, "publish", _degraded)
    # A malformed allowed_destinations is the same class as a malformed
    # section one level down, in two shapes. A non-LIST value:
    # iterating it either crashes load() with a TypeError (a scalar — a
    # config typo must not abort gateway startup) or yields garbage (a
    # dict iterates as its keys, a string as its characters). A list with
    # non-string/empty ENTRIES: the parse filter drops them, so an
    # all-invalid narrowing like [1, 2] parses to [] — indistinguishable
    # from "no restriction configured", the exact silent widening this fix
    # exists to stop. Both shapes record the degradation so the publish
    # gate denies, and parse from what safely remains. Validation cannot
    # repair these values (publish.allowed_destinations is fail-closed
    # there — repairing an OPEN default silently widens), so the loader
    # must be the layer that survives them.
    _dests_raw = _sections.SectionReader(PublishConfig, publish_data).get("allowed_destinations")
    if not isinstance(_dests_raw, list):
        _degraded.add("publish")
        _OBSERVED_DEGRADED_SECTIONS.add("publish")
        logger.warning(
            "config: 'publish.allowed_destinations' is not a list (got %s) "
            "— treating the publish section as degraded; publishing is "
            "denied until the file is fixed and the gateway restarted",
            type(_dests_raw).__name__,
        )
        _dests_raw = []
    elif any(not (isinstance(_d, str) and _d) for _d in _dests_raw):
        _degraded.add("publish")
        _OBSERVED_DEGRADED_SECTIONS.add("publish")
        logger.warning(
            "config: 'publish.allowed_destinations' carries entr(y/ies) "
            "that are not non-empty strings — treating the publish section "
            "as degraded; publishing is denied until the file is fixed and "
            "the gateway restarted",
        )
        _dests_raw = []
    # Back-compat: this channel's config section was renamed
    # "wechat" -> "wecom". Fall back to the legacy key so existing
    # installs keep their WeCom settings on upgrade (read-only alias;
    # no broader migration machinery).
    # Alias-aware: record under whichever key the operator actually used, so
    # the warning names the section they can go and fix.
    _wecom_key = "wecom" if "wecom" in data else "wechat"
    wecom_data = _coerced_section(data, _wecom_key, _degraded)
    dashboard_data = _coerced_section(data, "dashboard", _degraded)
    # Persistent is the compatibility default only when a readable config
    # genuinely omits this field. If the dashboard section or either config
    # file was unreadable, the missing value may have been a privacy choice
    # we could not recover, so new chats must fail closed to Temporary until
    # the operator fixes the file and restarts the gateway.
    if (
        "dashboard" in _degraded
        or "dashboard" in _OBSERVED_DEGRADED_SECTIONS
        or DEGRADED_WHOLE_CONFIG in _OBSERVED_DEGRADED_SECTIONS
    ):
        dashboard_data["default_memory_mode"] = "temporary"
    stt_data = _coerced_section(data, "stt", _degraded)
    computer_use_data = _coerced_section(data, "computer_use", _degraded)
    instances_data = _coerced_section(data, "instances", _degraded)
    instances = _sections.SectionReader(InstancesConfig, instances_data)
    connect_timeout_raw = instances.get("connect_timeout_secs")
    mint_timeout_raw = instances.get("mint_timeout_secs")
    mcp_gateway_data = _coerced_section(data, "mcp_gateway", _degraded)
    mcp_data = _coerced_section(data, "mcp", _degraded)
    heartbeat = _sections.SectionReader(
        HeartbeatConfig, _coerced_section(data, "heartbeat", _degraded)
    )
    heartbeat_default_deliver = str(heartbeat.get("default_deliver")).strip().lower()
    if heartbeat_default_deliver not in ("slack", "dashboard"):
        heartbeat_default_deliver = heartbeat.default("default_deliver")
    # A stored document written before this key existed has no "monitoring"
    # object at all, and that is the case that must keep working: the miss
    # resolves to the dataclass default, which is the off position. So an
    # already-installed gateway needs nothing written to be correct here --
    # only a gateway that wants the key ON writes it, and Settings does
    # that. (The hazard this avoids belongs to a SHIPPED DEFAULT that
    # CHANGES: config.json materializes every key, so the stored value
    # outranks the new default forever. Adding a key has no stored value to
    # outrank it.)
    monitoring_data = _coerced_section(data, "monitoring", _degraded)
    monitoring_prefer_structured_arming = _sections.SectionReader(
        MonitoringConfig, monitoring_data
    ).read("prefer_structured_arming", _safe_bool)
    tunnel_data = _coerced_section(data, "tunnel", _degraded)
    skills_data = _coerced_section(data, "skills", _degraded)
    session_summary_data = _coerced_section(data, "session_summary", _degraded)
    session_environments_data = _coerced_section(data, "session_environments", _degraded)
    messaging_data = _coerced_section(data, "messaging", _degraded)
    telemetry_data = _coerced_section(data, "telemetry", _degraded)
    watchdog_data = _coerced_section(data, "watchdog", _degraded)
    work_ledger_data = _coerced_section(data, "work_ledger", _degraded)
    power_data = _coerced_section(data, "power", _degraded)
    decisions_data = _coerced_section(data, "decisions", _degraded)
    resource_limits_data = _coerced_section(data, "resource_limits", _degraded)

    # Parse agents section into dict[str, KiroCrewAgentConfig]
    raw_agents = data.get("agents", {})
    agents: dict[str, KiroCrewAgentConfig] = {}
    if isinstance(raw_agents, dict):
        for name, entry in raw_agents.items():
            if isinstance(entry, dict):
                crew = _sections.SectionReader(KiroCrewAgentConfig, entry)
                # config.json is hand-editable (and agent-writable), so a
                # non-string model (e.g. `model: 123`) must not survive the
                # load — it would reach normalize_agent_model().strip() and
                # raise AttributeError from the resolver instead of simply
                # being ignored.
                raw_model = crew.get("model")
                # Same guard as model: a non-string triggers (e.g. `1`) must
                # not survive load — select_crew's roster calls .strip() on it.
                raw_triggers = crew.get("triggers")
                # Same guard family: the label is rendered verbatim by every
                # roster surface, so a non-string collapses to "" (show the
                # name) rather than reaching the wire.
                raw_display_name = crew.get("display_name")
                # Every other declared-``str`` field except the two identity
                # fields joins the same guard family through ``_safe_str``: a
                # null, number, list or object stored under a string key reads
                # as the field's declared default, so a wrong-typed key behaves
                # like an absent one. ``kirocrew agent list`` width-formats
                # ``kiro_agent`` / ``workspace`` / ``source``, and a stored
                # null or container reaching that f-string raises TypeError
                # from ``__format__``. The roster and config views still mask
                # a non-string on their own side as defence in depth.
                #
                # ``memory_store`` and ``member_id`` are deliberately NOT
                # pinned. The ``memory_store`` default names the shared store,
                # so collapsing a malformed binding would silently attach this
                # member to it. The ``member_id`` default ``""`` reads as "no
                # identity yet", which would switch off two fail-closed checks
                # in ``memory_stores.py`` (``require_member_memory_store`` and
                # the legacy upgrade) and let the upgrade mint a new identity.
                # Both stay as written and the resolvers refuse them;
                # ``agent list`` renders a malformed ``memory_store``.
                raw_worker_model = crew.get("worker_model")
                agents[name] = KiroCrewAgentConfig(
                    member_id=crew.get("member_id"),
                    kiro_agent=crew.read("kiro_agent", _safe_str),
                    workspace=crew.read("workspace", _safe_str),
                    memory_store=crew.get("memory_store"),
                    model=raw_model if isinstance(raw_model, str) else crew.default("model"),
                    # Same hand-editable-config guard: an unknown level must
                    # collapse to "" (inherit) rather than travel to the
                    # provider, where kiro-cli rejects the whole overlay.
                    reasoning_effort=coerce_effort(crew.get("reasoning_effort")),
                    # The dispatched-worker pair: the model keeps its raw
                    # spelling because "auto" is a REAL pin here (see the
                    # field), so only the non-string guard applies; the
                    # effort takes the same coercion as the crew's own.
                    worker_model=(
                        raw_worker_model.strip()
                        if isinstance(raw_worker_model, str)
                        else crew.default("worker_model")
                    ),
                    worker_reasoning_effort=coerce_effort(crew.get("worker_reasoning_effort")),
                    # Kept as written, never coerced to a selectable value: the
                    # selection gate refuses a pin this build cannot start, and a
                    # coerced one would run the crewmate on another harness.
                    acp_backend=crew.read("acp_backend", _safe_str).strip(),
                    approval_mode=_sections.coerce_member_approval_mode(crew.get("approval_mode")),
                    display_name=(
                        raw_display_name
                        if isinstance(raw_display_name, str)
                        else crew.default("display_name")
                    ),
                    description=crew.read("description", _safe_str),
                    triggers=(
                        raw_triggers if isinstance(raw_triggers, str) else crew.default("triggers")
                    ),
                    source=crew.read("source", _safe_str),
                    # Hand-editable config: a quoted "true" or a stray int
                    # must not become a truthy star, so only a real bool
                    # is honoured and anything else reads as un-starred.
                    starred=crew.read("starred", _safe_bool),
                    # Strictly a bool, like ``starred``: a stray string reads as off.
                    dynamic_capabilities=crew.read("dynamic_capabilities", _safe_bool),
                    # Same guard family as model/triggers: config.json is
                    # hand-editable, so a junk value must collapse to 0
                    # (inherit the global window), never crash the load.
                    # lo=0 keeps a negative override from arming an
                    # instant-cancel window.
                    watchdog_tool_stall_suspect_secs=crew.read(
                        "watchdog_tool_stall_suspect_secs", _safe_float, lo=0.0
                    ),
                    watchdog_tool_stall_hard_cap_secs=crew.read(
                        "watchdog_tool_stall_hard_cap_secs", _safe_float, lo=0.0
                    ),
                    autocompact_pct=_crew_autocompact_pct(crew),
                    telegram_account=crew.read("telegram_account", _safe_str),
                    session_color=_safe_color(crew.get("session_color")),
                    # Module-qualified on purpose: the facade's `from
                    # sections import` list is a frozen pre-split snapshot
                    # (test_config_module_boundaries), and post-split
                    # internals are reached through the module, not
                    # re-exported from here.
                    avatar=_sections._safe_avatar(crew.get("avatar")),
                    imported=_sections.safe_imported(crew.get("imported")),
                )

    # Migrate workspaces from flat or structured format
    raw_workspaces = data.get("workspaces", {})
    if not isinstance(raw_workspaces, dict):
        # Reported, not just replaced: a gate that fences the memory
        # workspaces (folder steering's silo fence) reads this table to
        # learn WHERE the workspaces are, and an operator's absolute
        # workspace directory that this load could not read is a directory
        # the fence would otherwise not know to cover. Same posture as the
        # ``dashboard.tailscale`` key: the consumer decides to fail closed.
        logger.warning(
            "Config 'workspaces' is not a JSON object (got %s); the workspace "
            "table is unavailable for this load",
            type(raw_workspaces).__name__,
        )
        _degraded.add(_resolution.DEGRADED_WORKSPACES)
        raw_workspaces = {}
    workspaces = _migrate_workspaces(raw_workspaces)

    # Parse memory_stores; synthesize default if missing.
    #
    # A store NAME becomes a single path segment under
    # ``memory_stores.MEMORY_STORES_DIR_NAME``, so the shape rule is applied
    # here too — at BOOT, where the operator can see it — rather than only at
    # the first memory write. Reported, never REPAIRED: the entry is kept
    # verbatim so a ``to_dict()``/``save()`` round-trip cannot erase the
    # operator's own declaration, and no name is rewritten into a usable one,
    # because sanitizing ``../work`` or ``Work`` into ``work`` is the one
    # thing that would merge two crews' memory into one directory.
    #
    # Kept, but not usable: ``memory_stores.usable_store_names`` drops a
    # malformed name from the resolvable set, so a crew bound to it degrades
    # at ``resolve_agent_bindings`` instead of carrying the name to a
    # resolver that would raise on it.
    raw_stores = data.get("memory_stores", {})
    memory_stores: dict[str, MemoryStoreConfig] = {}
    if isinstance(raw_stores, dict) and raw_stores:
        for name, entry in raw_stores.items():
            if not isinstance(entry, dict):
                continue
            defect = memory_store_name_defect(name)
            if defect is not None:
                logger.warning(
                    "memory_stores: store name %r is unusable (%s); any crew "
                    "bound to it cannot resolve a memory directory",
                    name,
                    defect,
                )
            store = _sections.SectionReader(MemoryStoreConfig, entry)
            memory_stores[name] = MemoryStoreConfig(
                owner_member_id=store.get("owner_member_id"),
                description=store.get("description"),
                embedding_provider=store.get("embedding_provider"),
                owner_member=store.get("owner_member"),
                memory_version=store.get("memory_version"),
            )
    if not memory_stores:
        memory_stores[DEFAULT_MEMORY_STORE] = MemoryStoreConfig()

    apps = _parse_app_settings(data.get("apps", {}))

    # The top-level keys read against KiroCrewConfig's own fields, and the slack
    # keys that land on top-level fields under the same names. Against the base
    # config, not *config_cls*: an omitted key reads KiroCrewConfig's default even
    # when a subclass is built.
    top = _sections.SectionReader(_TOP_LEVEL_DEFAULTS, data)
    slack_top = _sections.SectionReader(_TOP_LEVEL_DEFAULTS, slack_data)

    # Parse top-level default_agent and default_memory_store
    default_agent_val = top.get("default_agent")
    if not isinstance(default_agent_val, str):
        default_agent_val = top.default("default_agent")
    default_memory_store_val = top.get("default_memory_store")
    if not isinstance(default_memory_store_val, str):
        default_memory_store_val = top.default("default_memory_store")
    # Reported, not repaired, for the same reason as the store names above.
    # Legacy default_memory_store is retained for V1 compatibility. Member
    # member resolution never uses it as a fallback or a filesystem path.
    elif memory_store_name_defect(default_memory_store_val) is not None:
        logger.warning(
            "default_memory_store %r is not a usable store name (%s); preserved "
            "for V1 compatibility but not used for member memory resolution",
            default_memory_store_val,
            memory_store_name_defect(default_memory_store_val),
        )

    # Capture unknown top-level sections verbatim so a section this core does
    # not model (e.g. an edition-contributed section written by a companion)
    # survives the load()->to_dict()->save() round-trip instead of being
    # silently dropped. ``meta`` is stamped by save() itself, so it is never
    # treated as an unknown section to preserve.
    #
    # Captured from the BASE view, not the merged one: for any section the
    # overlay touched, ``base_shadow`` holds what config.json itself said.
    # Capturing the merged value would make save() emit the overlay's leaf,
    # which _subtract_overlay then removes — deleting the base file's own
    # value. See _shadowed_base_sections. Sections the overlay did not touch
    # are identical in both views.
    capture_view = {**data, **base_shadow}
    extra_sections = {
        k: v
        for k, v in capture_view.items()
        if k not in _KNOWN_CONFIG_SECTIONS and k not in CONFIG_RESERVED_TOP_KEYS
    }

    cfg = config_cls(
        agent=_build_agent_config(agent_data),
        session=_build_session_config(session_data),
        taskrunner=_build_taskrunner_config(taskrunner_data),
        cron_history=_build_cron_history_config(cron_history_data),
        messaging=_build_messaging_config(messaging_data),
        # watchdog is advertised in config-baseline.json, served by
        # /api/config/schema, and read by acp/session_handle_watchdog.py, so load()
        # passes this kwarg — without it config.json values would be
        # silently ignored and the dataclass defaults would always win.
        watchdog=_build_watchdog_config(watchdog_data),
        work_ledger=_build_work_ledger_config(work_ledger_data),
        power=_build_power_config(power_data),
        resource_limits=ResourceLimitsConfig.from_raw(resource_limits_data),
        telemetry=_build_telemetry_config(telemetry_data),
        memory=_build_memory_config(memory_data),
        knowledge=_build_knowledge_config(knowledge_data),
        telegram=_build_telegram_config(telegram_data),
        weixin=_build_weixin_config(weixin_data),
        whatsapp=_build_whatsapp_config(whatsapp_data),
        discord=_build_discord_config(discord_data),
        webex=_build_webex_config(webex_data),
        wakatime=_build_wakatime_config(wakatime_data),
        imessage=_build_imessage_config(imessage_data),
        teams=_build_teams_config(teams_data),
        slack=_build_slack_config(slack_data),
        publish=_build_publish_config(_dests_raw, publish_data),
        wecom=_build_wecom_config(wecom_data),
        feishu=_build_feishu_config(feishu_data),
        dashboard=_build_dashboard_config(_degraded, dashboard_data),
        tunnel=_build_tunnel_config(tunnel_data),
        hooks=hooks_data,
        agents=agents,
        default_agent=default_agent_val,
        workspaces=workspaces,
        default_workspace=top.get("default_workspace"),
        memory_stores=memory_stores,
        default_memory_store=default_memory_store_val,
        apps=apps,
        stt=_build_stt_config(stt_data),
        # Every numeric knob is clamped to the same ceiling the MCP tool
        # schemas enforce, so a hand-edited config.json cannot ask for an
        # unbounded accessibility walk or a full-resolution screenshot.
        # There is deliberately NO ``enabled`` key read here — see
        # ComputerUseConfig's docstring and computer_use_state_path().
        computer_use=_build_computer_use_config(computer_use_data),
        # A bad entry costs that one binding: the project keeps the default
        # credential chain (aws_project_profiles.coerce_project_profiles).
        aws=AwsConfig(
            project_profiles=coerce_project_profiles(
                _coerced_section(data, "aws", _degraded).get("project_profiles")
            )
        ),
        # ``_coerce_bool``, not ``_safe_bool``: this one key decides whether
        # an unattended installer runs, and the two wrong answers are not
        # symmetric. A hand-edited ``"auto_update": "false"`` is truthy to
        # the loop, and ``_safe_bool`` would fold it (and ``0``, and
        # ``null``) to this field's True default — installing on a host whose
        # owner wrote the opposite. So a recognized spelling is honoured, and
        # anything else unreadable falls back to OFF: an update not applied
        # is a notification, while one applied against the owner's wish is a
        # restart they did not ask for. An ABSENT key still defaults ON.
        auto_update=(
            top.default("auto_update")
            if "auto_update" not in data
            else _coerce_bool(data.get("auto_update"), False)
        ),
        connections_ui=top.read("connections_ui", _safe_bool),
        _degraded_sections=frozenset(_degraded | _OBSERVED_DEGRADED_SECTIONS),
        timezone=top.get("timezone"),
        snapshot_dir=top.get("snapshot_dir"),
        registries=[
            _registry_entry(r)
            for r in (data.get("registries") or [])
            if isinstance(r, dict) and r.get("repo")
        ],
        mcp_gateway=_build_mcp_gateway_config(mcp_gateway_data),
        mcp=_build_mcp_config(mcp_data),
        instances=_build_instances_config(connect_timeout_raw, instances_data, mint_timeout_raw),
        heartbeat=HeartbeatConfig(default_deliver=heartbeat_default_deliver),
        monitoring=_build_monitoring_config(monitoring_data, monitoring_prefer_structured_arming),
        decisions=DecisionsConfig.from_raw(decisions_data),
        skills=_build_skills_config(skills_data),
        session_summary=_build_session_summary_config(session_summary_data),
        session_environments=_sections.SessionEnvironmentsConfig.from_raw(
            session_environments_data
        ),
        slack_channels={
            ch_id: ChannelConfig.from_dict(ch_data)
            for ch_id, ch_data in (
                slack_data.get("channels", {})
                if isinstance(slack_data.get("channels"), dict)
                else {}
            ).items()
            if isinstance(ch_data, dict)
        },
        slack_dm_activation=_validate_activation(
            slack_data.get("dm_activation", top.default("slack_dm_activation"))
        ),
        observe_max_messages=max(1, slack_top.read("observe_max_messages", _safe_int)),
        observe_ttl_hours=max(0.0, slack_top.read("observe_ttl_hours", _safe_float)),
        _extra_sections=extra_sections,
    )

    # Unknown keys nested inside a modelled section. Captured AFTER
    # construction because deciding what is unknown needs the built
    # dataclasses' own field sets, not a second hand-maintained list that
    # could drift from them. Same base-not-merged view as _extra_sections
    # above, for the same reason.
    cfg._extra_keys = _resolution.capture_extra_section_keys(capture_view, cfg)
    if doc.disk_read:
        # The MERGED view, unlike the capture: a misspelling in either file has
        # no effect, so either one is worth naming.
        _resolution.warn_misspelled_section_keys(data, cfg)

    cfg._base_unreadable = doc.base_unreadable
    cfg._overlay_unreadable = doc.overlay_unreadable
    return cfg


def persist_write_back(cfg: KiroCrewConfig, doc: ConfigDocument) -> None:
    """Run the write-back migration a LOADED document makes due, in memory and on disk.

    Decides the pending migrations from *cfg* and *doc* -- flat workspace strings, a
    missing default crew or ``default_agent`` (both seeded into *cfg* here), the
    one-shot ``connections_ui`` strip, the superseded defaults *doc* may adopt and the
    legacy ``skills.lazy_load`` rewrite -- then applies them to ``config.json`` as a
    delta under the write lock (:func:`_persist_config_migration`) and moves *cfg* to
    each adopted value only once the write confirmed it. Skips the disk write for a
    load that degraded a section, so the malformed bytes stay as evidence. Never
    raises: a failure is logged and the migration retries on a later load. Drops the
    validated-data cache when an adoption did not reach disk. An unloaded document
    has nothing to migrate and returns at once: a plain read of a never-set-up home
    must not create config files.
    """
    if not doc.loaded:
        return
    path = doc.path
    data = doc.data
    adoptable = doc.adoptable
    legacy_lazy_stamp = doc.legacy_lazy_stamp
    local_data = doc.overlay
    # The workspace table as build_config read it: a value that is not an object
    # was reported there and holds nothing to migrate.
    raw_workspaces = data.get("workspaces", {})
    if not isinstance(raw_workspaces, dict):
        raw_workspaces = {}

    # Write-back migration: if the on-disk config has legacy format
    # (flat workspace strings, missing sections), back up the original
    # and save the migrated version.  One-shot — subsequent loads see
    # the canonical format and skip.
    #
    # The in-memory half below mutates `cfg` and RECORDS which migrations it
    # decided on; the on-disk half re-reads config.json inside the write lock
    # and applies exactly those as a delta (see _persist_config_migration),
    # rather than `cfg.save()`, which would re-serialize this load's whole
    # snapshot and drop any config write that landed after this load's read.
    #
    # Bound BEFORE the try: the ``finally`` at the end reads them, and an
    # exception raised before their assignments would turn a logged write-back
    # failure into a NameError out of load().
    adopt_keys: set[str] = set()
    adoption_landed = False
    confirmed_adoptions: list[str] = []

    try:
        pending: set[str] = set()
        # Flat workspace strings → need migration to {"dir": ...}
        for v in raw_workspaces.values():
            if isinstance(v, str):
                pending.add(MIGRATE_WORKSPACES)
                break

        # One-time migration: create default agent when none exists
        if not cfg.agents:
            kiro = default_template(cfg)
            cfg.agents["default"] = KiroCrewAgentConfig(
                kiro_agent=kiro,
                workspace="default",
                memory_store="default",
            )
            pending.add(MIGRATE_AGENTS)
        if not cfg.default_agent or cfg.default_agent not in cfg.agents:
            # Prefer "default" if it exists, otherwise use first available agent
            if "default" in cfg.agents:
                cfg.default_agent = "default"
            elif cfg.agents:
                cfg.default_agent = next(iter(cfg.agents))
            else:
                cfg.default_agent = "default"
            pending.add(MIGRATE_DEFAULT_AGENT)

        # The template plain sessions ran before they were template sessions:
        # the roster default row's (see MIGRATE_STAR_DEFAULT_TEMPLATE), seeded
        # above or stored -- a seeded roster default is the row a plain session
        # read, and once stored it is indistinguishable from a pick, so a later
        # load would carry it anyway. Decided on the MERGED view and applied in
        # memory at once, so this very load's plain sessions keep running it;
        # skipped where the overlay supplies the field, since that value is the
        # operator's own and already wins.
        if not _overlay_supplies(local_data, "agent.default_agent"):
            carried = _migration.star_default_template_due(
                cfg.agent.default_agent,
                cfg.default_agent,
                {name: asdict(row) for name, row in cfg.agents.items()},
            )
            if carried is not None:
                cfg.agent.default_agent = dispatch_kiro_agent(carried)
                pending.add(MIGRATE_STAR_DEFAULT_TEMPLATE)

        # One-shot launch migration for ``connections_ui`` (see the marker
        # constant's docstring). Decided on the BASE document, not the
        # merged view: the overlay is user-owned prose we never rewrite,
        # and the stale materialization only ever landed in config.json.
        connections_marker = config_dir() / CONNECTIONS_UI_MIGRATION_MARKER
        connections_migrating = False
        if not connections_marker.exists():
            if data.get("connections_ui") is False:
                # In-memory half: this very load must already serve the
                # launch default — the strip below is the on-disk echo.
                cfg.connections_ui = True
                pending.add(MIGRATE_CONNECTIONS_UI)
            connections_migrating = True

        # Adopt the auto-adopting superseded defaults. The in-memory half is
        # applied BELOW, after the write is confirmed -- never here. Applying it
        # eagerly would let a failed write leave the running config holding a
        # value the stored document does not agree with, invisibly: the operator
        # reads 1800 in config.json while the gateway runs 10800.
        #
        # In memory still matters as much as the file, which is why it happens at
        # all: the gateway reads these budgets once at startup, so a disk-only
        # fix would leave the very run that performed it still on the old value,
        # and "upgraded, restarted, nothing changed" is the complaint.
        adopt_keys = {e.dotted_key for e in adoptable}
        if adopt_keys:
            pending.add(MIGRATE_SUPERSEDED_DEFAULTS)
        # The legacy lazy_load rewrite rides the same write, ledger and
        # confirmed-only in-memory half as an adoption (see its id's docstring).
        if legacy_lazy_stamp is not None:
            pending.add(_migration.MIGRATE_SKILLS_LAZY_LOAD)

        needs_migration = bool(pending)

        persisted = True
        # Tracked SEPARATELY from ``persisted``, which starts True so the
        # connections_ui marker still lands on a load that needed no migration
        # at all. This one starts False and is set only where the adoption
        # actually reached disk, so every OTHER way out -- a contended-lock
        # deferral, the degraded-sections branch below, an exception caught by
        # the handler at the end -- leaves it False and drops the cache in the
        # ``finally``.
        if needs_migration and not cfg._degraded_sections:
            persisted = _persist_config_migration(
                path,
                frozenset(pending),
                default_kiro_agent=default_template(cfg),
                adopt_keys=frozenset(adopt_keys),
                confirmed_adoptions=confirmed_adoptions,
            )
            adoption_landed = persisted
            # In memory only for keys the migration CONFIRMED it removed, and
            # only where the overlay does not supply the field: there the stale
            # base bytes are cleared like any other, but the effective value
            # belongs to ``config.local.json`` and is the operator's live choice.
            by_key = {e.dotted_key: e for e in adoptable}
            for key in confirmed_adoptions:
                entry = by_key.get(key)
                if entry is not None:
                    logger.warning(
                        "config: adopted current default for %s; removed stored value %r. "
                        "To restore it: kirocrew config set %s %s",
                        key,
                        entry.old_default,
                        key,
                        entry.old_default,
                    )
                if entry is not None and not _overlay_supplies(local_data, key):
                    _adopt_in_memory(cfg, key, entry.old_default)
            if _migration.LAZY_LOAD_KEY in confirmed_adoptions:
                overlay_sets_it = _overlay_supplies(local_data, _migration.LAZY_LOAD_KEY)
                logger.warning(
                    "config: removed skills.lazy_load=false from config.json: Kiro "
                    "Crew %s stored it when false was the default full skills "
                    "listing, and false now selects the short skill entry. %s To "
                    "choose the short entry: kirocrew config set skills.lazy_load false",
                    legacy_lazy_stamp,
                    (
                        "config.local.json still sets the key, and its value applies."
                        if overlay_sets_it
                        else "The current default (the ranked skill index) applies."
                    ),
                )
                if not overlay_sets_it:
                    _adopt_in_memory(cfg, _migration.LAZY_LOAD_KEY, False)
        elif needs_migration:
            # This load DISCARDED something (a malformed section, an
            # unreadable file). The write-back serializes only the parsed
            # fields, so writing back here would replace the operator's
            # malformed narrowing with clean defaults — erasing the only
            # on-disk evidence and turning the denial into silent
            # allow-all at the next restart. Keep the malformed
            # bytes; every future process re-observes and re-denies until
            # the operator actually fixes the file. Migration re-runs on
            # the first clean load.
            logger.warning(
                "config: skipping write-back migration — this load "
                "degraded section(s) %s and writing back would erase the "
                "evidence; fix the file to clear",
                sorted(cfg._degraded_sections),
            )

        # Record the connections_ui boundary only after a pass that was
        # allowed to act on it AND whose write-back actually landed. A
        # degraded load skips both the strip and the marker; a contended
        # lock makes _persist_config_migration return False with nothing
        # written, and the marker MUST defer with the strip — marker
        # without strip would freeze the stale false as a deliberate
        # opt-out forever. (The already-migrated-by-another-writer path
        # also returns False; the marker then lands on the next load,
        # which finds nothing to strip. One extra boot, same endpoint.)
        # Failure to write the marker itself is logged and retried next
        # load — the strip's own condition makes the retry converge.
        if connections_migrating and persisted and not cfg._degraded_sections:
            atomic_write(
                connections_marker,
                json.dumps(
                    {
                        "migrated_at": datetime.now(timezone.utc).isoformat(),
                        "stripped_stale_false": MIGRATE_CONNECTIONS_UI in pending,
                    },
                    indent=2,
                )
                + "\n",
            )
    except Exception as e:
        # Migration write-back is best-effort; never block startup.
        logger.warning("Config write-back failed: %s", e)
    finally:
        # An adoption that did NOT reach disk must not be frozen behind the
        # validated-data cache. Only a load that READS the base document can
        # decide an adoption (``adoptable`` is empty on a cache hit, by design),
        # so a read-and-skip that left its document cached would have every
        # later load serve the stale value and never retry -- the ceiling the
        # operator upgraded to fix would come back and stay. In a ``finally``
        # rather than beside the write, because the write is skipped by more
        # than one path (a contended lock, an exception) and each leaves the
        # same stale cache entry.
        #
        # The degraded-sections branch is the exception, and it is excluded on
        # purpose. Its retry condition is not "the next load" but "the operator
        # fixes the file and restarts the gateway" -- degradation observations
        # are sticky for the life of a process (``_OBSERVED_DEGRADED_SECTIONS``),
        # so until then the write is refused every time, and dropping the cache
        # buys nothing except a full re-read and re-parse of config.json on
        # EVERY load for as long as the two conditions coexist. After the
        # restart the fixed file's fingerprint misses the (empty) cache and the
        # adoption retries on that first load -- no invalidation needed.
        if (
            (adopt_keys or legacy_lazy_stamp is not None)
            and not adoption_landed
            and not cfg._degraded_sections
        ):
            _invalidate_config_cache()
