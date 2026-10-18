"""Native Kiro launch views whose skill directory is supplied by Crew.

The authored resource mapping remains the authority for Crew search/list/read.
Native aliases preserve the other spec fields but carry no skill:// resources:
Kiro 2.21.2 progressively loads bodies, yet enumerates all their metadata before
the first prompt. Bounding only the Crew prompt cannot bound that native cost.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror, pinned_fs, platform_compat  # noqa: F401
from kiro_crew.acp import session_mcp  # noqa: F401
from kiro_crew.agent_discovery import (  # noqa: F401
    SCOPE_PROJECT,
    _read_agent_spec,
    can_inherit_skills,
    inherited_skill_globs,
    list_agents,
    skill_search_refusal,
)
from kiro_crew.agent_spec_format import (  # noqa: F401
    NATIVE_SKILL_ALIAS_PREFIX,
    SKILL_VIEW_PROJECTION_CEILING,
    volatile_env_keys,
)
from kiro_crew.atomic_write import atomic_write, on_event_loop  # noqa: F401
from kiro_crew.config.paths import (  # noqa: F401
    data_home,
    kiro_agents_dir,
    kiro_home,
    project_agents_dir,
)
from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes  # noqa: F401
from kiro_crew.security import _PATH_RESOLVE_TIMEOUT_SECS  # noqa: F401
from kiro_crew.user_json import copy_json_document  # noqa: F401
from kiro_crew.validation import is_registered_agent_name  # noqa: F401
from kiro_crew.workspace_cli_settings import workspace_cli_settings_lock  # noqa: F401

logger = logging.getLogger(__name__)


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.acp.skill_projection_parts.alias_reclaim import (  # noqa: F401
        _ALIAS_LOCK_SUFFIX,
        _CENSUS_ALIAS_MAX_BYTES,
        _CENSUS_MAX_ALIASES,
        _CHURNING_ENV_KEYS_MAX,
        _CHURNING_GROUPS_MAX,
        _CHURNING_KEYS_PER_GROUP_MAX,
        _CHURNING_LABEL_MAX_CHARS,
        _COLLECTED_ENDS,
        _COLLECTING_THREAD,
        _DRAIN_BATCH_PAUSE_SECS,
        _DRAIN_IDLE_BATCHES,
        _DRAIN_MAX_BATCHES,
        _LEGACY_RECLAIM_MIN_AGE_SECS,
        _PROJECTION_PRUNE_WORK_LIMIT,
        _PRUNE_MAX_RECLAIMS_PER_RUN,
        _PRUNE_MAX_SECONDS_PER_RUN,
        _REAPER,
        _REAPER_LOCK,
        _SIDECAR_SWEEP_MAX_PER_RUN,
        _eager_prune,
        _eager_prune_in,
        _end_projection,
        _end_projection_off_loop,
        _ensure_projection_reaper,
        _migrate_legacy_side_files,
        _move_sidecar,
        _note_collection,
        _note_env_values,
        _projection_prune_candidates,
        _prune_excluding_older_publishers,
        _prune_stale_managed_aliases,
        _prune_start_offset,
        _read_alias_spec_bounded,
        _reap_collected_projections,
        _reclaim_prune_candidate,
        _sweep_orphan_alias_locks,
        _sweep_orphan_sidecars,
        _sweep_orphan_sidecars_in,
        _unlink_unheld_lock_file,
        census_churning_env_keys,
        census_projected_aliases,
        census_projection_residue,
        drain_stale_aliases,
    )
    from kiro_crew.acp.skill_projection_parts.inheritance_overlay import (  # noqa: F401
        _ABSENT_SETTINGS,
        _INHERIT_SETTING,
        _INHERIT_SOURCE,
        _MANAGED_SETTING,
        _PREVIOUS_INHERITANCE,
        _SETTINGS_READ_RETRY_PAUSES,
        _inheritance_preference,
        _restore_inheritance,
        _settings,
        _settings_read_sleep,
        inherits_default_resources,
    )
    from kiro_crew.acp.skill_projection_parts.native_projection import (  # noqa: F401
        _ACTIVE_PROJECTIONS,
        _ACTIVE_PROJECTIONS_LOCK,
        _RECOGNISED_ALIASES_MAX,
        NativeSkillProjection,
        _active_aliases,
        _register_active_projection,
    )
    from kiro_crew.acp.skill_projection_parts.preparation import (  # noqa: F401
        _CEILING_FALLBACK_WARNED,
        _CEILING_FALLBACK_WARNED_LOCK,
        _count_projected_aliases,
        _warn_ceiling_fallback_once,
        prepare_native_skill_projection,
        project_agent_view,
    )
    from kiro_crew.acp.skill_projection_parts.side_files import (  # noqa: F401
        _IN_PROCESS_PROJECTION_LOCKS,
        _IN_PROCESS_PROJECTION_LOCKS_GUARD,
        _IN_PROCESS_PROJECTION_WAIT_SECS,
        _LAST_PER_SPAWN_PRUNE,
        _LEGACY_ALIAS_NAME_RE,
        _PER_SPAWN_PRUNE_MIN_INTERVAL_SECS,
        _PINNED_SIDE_DIR,
        _PROJECTION_LEASE_DIR_NAME,
        _PROJECTION_LEASE_HOLDER_SUFFIX,
        _PROJECTION_LEASE_MAX_ALIASES,
        _PROJECTION_LEASE_MAX_BYTES,
        _PROJECTION_LEASE_RECORD_SUFFIX,
        _PROJECTION_LEASE_SCAN_LIMIT,
        _PROJECTION_LEASE_SCAN_MAX_SECONDS,
        _PROJECTION_LEASE_STEM_RE,
        _PROJECTION_LOCK_NAME,
        _PROJECTION_LOCK_TIMEOUT_SECS,
        _PROJECTION_METADATA_DIR_NAME,
        _PROJECTION_PUBLICATION_RESERVE_SECS,
        _PROJECTION_SIDE_DIR_NAME,
        _PROJECTION_SIDE_LEASE_DIR_NAME,
        _PROJECTION_SIDE_LOCK_NAME,
        _PROJECTION_SIDE_METADATA_DIR_NAME,
        _UNLINK_WARNING_INTERVAL_SECS,
        _UNLINK_WARNING_LAST,
        _UNLINK_WARNING_LOCK,
        _UNLINK_WARNING_SUPPRESSED,
        _UNWRITABLE_ERRNOS,
        CrossProcessProjectionLockTimeout,
        InProcessProjectionWaitTimeout,
        _acquire_projection_lease,
        _ensure_projection_metadata_directory,
        _ensure_real_directory,
        _hold_legacy_publication_lock,
        _in_process_projection_lock,
        _is_projection_lease_stem,
        _LeaseIdentities,
        _legacy_lease_dir,
        _legacy_lock_path,
        _legacy_metadata_dir,
        _metadata_dirs,
        _path_exists,
        _per_spawn_prune_due,
        _probe_orphan_projection_holder,
        _probe_projection_lease,
        _projection_alias_lock,
        _projection_lease_dir,
        _projection_lock_path,
        _projection_metadata_dir,
        _projection_side_dir,
        _prune_budget_within,
        _read_lease_record,
        _scan_lease_dir,
        _scan_legacy_lease_dir,
        _scan_projection_leases,
        _unlink_alias_if_unchanged,
        _unlink_projection_lease_if_unchanged,
        _warn_unlink_refused,
    )
    from kiro_crew.acp.skill_projection_parts.view_ownership import (  # noqa: F401
        _HOME_TAG_HEX_CHARS,
        _MANAGED_AGENT,
        _MANAGED_ALIAS_SHA256,
        _MANAGED_CREW_HOME,
        _MANAGED_MARKER,
        _MANAGED_MARKER_VALUE,
        _MANAGED_SOURCE,
        _MANAGED_VIEW_DESCRIPTION_PREFIX,
        _MANAGED_VIEW_DESCRIPTION_RE,
        _MANAGED_VIEW_DESCRIPTION_TEMPLATE,
        _MANAGED_VIEW_SHA256,
        _alias_identity,
        _canonical_json,
        _env_identity,
        _home_tag,
        _is_current_publication,
        _is_legacy_projected_view,
        _managed_marker,
        _managed_metadata_for_alias,
        _managed_metadata_in,
        _managed_view_description,
        _parsed_spec,
        _same_crew_home,
        _sidecar_record,
        _sidecar_record_in,
        _view_digest,
        _view_home_tag,
    )
    from kiro_crew.acp.skill_projection_parts.view_publication import (  # noqa: F401
        _ALIAS_DESCRIPTION_MAX_CHARS,
        _ALIAS_DROPPED_DISPLAY_FIELDS,
        _DISPLAY_TEXT_TOTAL_WARN_BYTES,
        _DISPLAY_TEXT_WARN_BYTES,
        _DISPLAY_TEXT_WARNED,
        _DISPLAY_TEXT_WARNED_MAX,
        _LOGGED_NAME_MAX_CHARS,
        _SEARCH_TOOL,
        _STDOUT_FRAME_LIMIT_BYTES,
        _announce_publication,
        _display_text_bytes,
        _loggable,
        _strip_alias_display_text,
        _warn_on_display_text,
        announce_alias,
    )
    from kiro_crew.acp.skill_projection_parts.view_sources import (  # noqa: F401
        _LEDGER_EVICTION_WARNED,
        _LEDGER_PREFETCH_WINDOW,
        _VIEW_LEDGER_NAME,
        _VIEW_SOURCES,
        _VIEW_SOURCES_LOCK,
        _VIEW_SOURCES_MAX,
        RetiredSkillView,
        _admissible_source_agent,
        _import_legacy_ledger,
        _read_json_regular_file,
        _read_view_ledger,
        _record_view_ledger,
        _record_view_ledger_entries,
        _recorded_view_source,
        _remember_view_sources,
        _sidecar_answers_for,
        _ViewLedgerWrites,
        is_skill_view_name,
        remembered_view_source,
        resolve_source_agent,
        source_agent_name,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    import asyncio  # noqa: F401
    import errno  # noqa: F401
    import functools  # noqa: F401
    import gc  # noqa: F401
    import hashlib  # noqa: F401
    import json  # noqa: F401
    import os  # noqa: F401
    import queue  # noqa: F401
    import re  # noqa: F401
    import secrets  # noqa: F401
    import stat  # noqa: F401
    import threading  # noqa: F401
    import time  # noqa: F401
    import uuid  # noqa: F401
    import weakref  # noqa: F401
    from collections.abc import (  # noqa: F401
        Callable,
        Iterator,
    )
    from contextlib import (  # noqa: F401
        ExitStack,
    )
    from dataclasses import (  # noqa: F401
        dataclass,
        field,
    )
    from pathlib import (  # noqa: F401
        Path,
    )
    from typing import (  # noqa: F401
        Any,
    )


# This module is the facade of the skill-projection owners in
# ``kiro_crew.acp.skill_projection_parts``, and every name is reached through it as
# ``kiro_crew.acp.skill_projection.<name>``: a read is served by the owner that binds the
# name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner
# holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and it
# keeps no code of its own: new work goes to the owner whose responsibility it is, listed
# in :mod:`kiro_crew.acp.skill_projection_parts`.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.acp.skill_projection_parts.side_files",
    "kiro_crew.acp.skill_projection_parts.inheritance_overlay",
    "kiro_crew.acp.skill_projection_parts.view_publication",
    "kiro_crew.acp.skill_projection_parts.view_ownership",
    "kiro_crew.acp.skill_projection_parts.view_sources",
    "kiro_crew.acp.skill_projection_parts.native_projection",
    "kiro_crew.acp.skill_projection_parts.alias_reclaim",
    "kiro_crew.acp.skill_projection_parts.preparation",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name the module and its owners bind.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
