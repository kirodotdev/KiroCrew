"""Crew records, work items and the append-only event ledger.

One repository's crews live under its repo data dir::

    repos/<owner>/<repo>/crews/settings.json      protocol constants, repo-wide
    repos/<owner>/<repo>/crews/<crew_id>.json     one crew

Those two are operator configuration and are files. The crew's WORK -- its work
items, its progress lines and its passes -- is not: it is a projection of the
crew's own crew log. ``issue_radar_crew_record`` appends one ``radar/recorded``
entry per call to the session log of the slot the crew runs on, and every read of
a work item, a progress line or the repository's shared skip index folds those
entries (the ``radar`` projection in ``kiro_crew.crew_log.projection``). See the
ledger section of ``crew_store_log`` for what that buys and what it costs.

Every file carries ``schema``. Issue Radar's usual versioning strategy — a schema
mismatch is a cache miss, refetch from the forge — does NOT transfer here: a crew
record has no upstream to refetch from, so readers coerce forward on read and a
real migration is required if the shape ever changes incompatibly.

Locking. ``store_issues.py``'s per-record lock is the model for the crew record. The
ledger takes no lock of its own: an append to a crew log is serialised by the
crew log's writer, and every invariant the old three-file transaction held under
three locks -- a phase never moves without its logged reason, an issue is never
skipped without being indexed -- is a property of ONE entry instead.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of Issue Radar's crew store, and every name is reached
# through it as ``kiro_crew.apps.builtins.issue_radar.backend.crew_store.<name>``:
# a read is served by the owner that binds the name, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners
# never import this module. New work goes to the owner of its responsibility:
#
#   ``crew_store_records`` -- the schema, number guards, paths, settings and crew records
#   ``crew_store_unit_order`` -- the order a crew's log units recorded in, behind no-follow holds
#   ``crew_store_log`` -- the ledger's entry type, write locks, landed checks, errors, units
#   ``crew_store_carry`` -- carrying the pre-projection files into the crew log
#   ``crew_store_ledger`` -- the folded work items, events and passes, and the appends
#   ``crew_store_fabric`` -- the fabric fold of every crew's work items


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.issue_radar.backend.crew_store_records",
    "kiro_crew.apps.builtins.issue_radar.backend.crew_store_unit_order",
    "kiro_crew.apps.builtins.issue_radar.backend.crew_store_log",
    "kiro_crew.apps.builtins.issue_radar.backend.crew_store_carry",
    "kiro_crew.apps.builtins.issue_radar.backend.crew_store_ledger",
    "kiro_crew.apps.builtins.issue_radar.backend.crew_store_fabric",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.apps.builtins.issue_radar.backend.crew_store_carry import (  # noqa: F401
        _CARRIED_TEXT_FIELDS,
        _CARRY_SHRINK_STEPS,
        _ITEMS_CARRIED_MARKER,
        _ITEMS_CARRY_BEGUN_MARKER,
        _MAX_CARRIED_TEXT,
        _SKIPS_CARRIED_MARKER,
        _SKIPS_CARRY_BEGUN_MARKER,
        _all_landed,
        _carried_history_complete,
        _carry_legacy_forward,
        _carry_pending,
        _legacy_carry_rows,
        _legacy_ci_state,
        _legacy_item_entries,
        _legacy_number,
        _legacy_preview,
        _legacy_skip_entry,
        _legacy_text,
        _mark_carry_begun,
        _read_legacy_json,
        _shrink_to_fit,
        _touch,
        _tried_key,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.crew_store_fabric import (  # noqa: F401
        FABRIC_SCHEMA,
        OFF_SPINE_PHASES,
        SPINE_PHASES,
        _fabric_title_hints,
        _fold_one_item,
        fold_fabric,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.crew_store_ledger import (  # noqa: F401
        _append,
        _editing_item,
        _entry_data,
        _prepare_write,
        _require_crew_log,
        _skip_precedence,
        _standing_skip,
        _units_for_write,
        commit_work_progress,
        is_skipped,
        list_work_items,
        open_slot_count,
        read_events,
        read_ledger,
        read_skips,
        read_work_item,
        recent_skips,
        record_crew_checkpoint,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.crew_store_log import (  # noqa: F401
        _APPEND_FLUSH_SECONDS,
        _ENTRY_SRC,
        FOLD_NAME,
        LEDGER_ENTRY_TYPE,
        CrewLedgerEntryTooLarge,
        CrewLedgerNotRecorded,
        CrewLedgerUnavailable,
        _crew_write_lock,
        _crew_write_locks,
        _crew_write_locks_guard,
        _fold_checkpoint,
        _landed_since,
        _projection,
        _undrained,
        _unit_last_seq,
        crew_log_units,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.crew_store_records import (  # noqa: F401
        _CREW_ID_MINT_TRIES,
        _CREW_ID_RE,
        _DEFAULT_CREW,
        _TEXT_SETTINGS,
        CLEARABLE_FIELDS,
        CREW_LEVEL_EVENT_KIND,
        CREW_SCHEMA,
        DEFAULT_SETTINGS,
        DEFAULT_SKIP_SCOPE,
        EDITING_PHASES,
        EVENT_KINDS,
        MAX_MAX_OPEN,
        MAX_SETTING_TEXT,
        MIN_MAX_OPEN,
        NAME_POOL,
        PHASES,
        SKIP_SCOPES,
        TERMINAL_PHASES,
        TTL_ACTIVE_PHASES,
        CrewStoreError,
        _app_root,
        _coerce_crew,
        _crew_id_in_use,
        _crew_lock_path,
        _crews_path,
        _finite_int,
        _hold_releases,
        _mint_crew_id,
        _mint_lock,
        _records_lock_path,
        _require_crew_id,
        _validated_crew_patch,
        _validated_max_open,
        _validated_model_id,
        _validated_text_setting,
        _validated_ttl_hours,
        coerce_skip_scope,
        create_crew,
        crew_path,
        crews_dir,
        events_path,
        hold_releases,
        is_crew_id,
        list_crews,
        logger,
        model_settings,
        read_crew,
        read_settings,
        retire_crew,
        set_crew_model_held,
        set_crew_paused,
        settings_path,
        skips_path,
        slot_key_for,
        suggest_names,
        taken_names,
        update_crew,
        work_item_path,
        write_settings,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.crew_store_unit_order import (  # noqa: F401
        _MAX_ORDER_READ_BYTES,
        _MAX_ORDERED_UNITS,
        _MAX_SUFFIX_DEPTH,
        _UNIT_ORDER_SUFFIX,
        _chain_depth_bound,
        _hold_chain_for_by_name_use,
        _hold_chain_no_follow,
        _pin_held,
        _read_unit_order_held,
        _record_unit_order,
        _recorded_unit_order,
        _recorded_unit_order_at,
        _release_held,
        _unit_order_path,
        _write_unit_order,
    )

if TYPE_CHECKING:  # names it serves from outside the owner family, from their homes
    from kiro_crew import atomic_write as atomic_write_module  # noqa: F401
    from kiro_crew import (  # noqa: F401
        platform_compat,
    )
    from kiro_crew.apps.builtins.issue_radar.backend import (  # noqa: F401
        store,
    )
    from kiro_crew.atomic_write import (  # noqa: F401
        atomic_write,
    )
    from kiro_crew.config.paths import (  # noqa: F401
        data_home,
    )
    from kiro_crew.constants import (  # noqa: F401
        env_file_display,
    )
    from kiro_crew.crew_log.entry_types import (  # noqa: F401
        RADAR_CLEARABLE_FIELDS,
        RADAR_CREW_LEVEL_EVENT_KIND,
        RADAR_DEFAULT_SKIP_SCOPE,
        RADAR_EDITING_PHASES,
        RADAR_ENTRY_TYPE,
        RADAR_EVENT_KINDS,
        RADAR_NUMBER_BOUNDS,
        RADAR_PHASES,
        RADAR_SKIP_SCOPES,
        RADAR_TERMINAL_PHASES,
        RADAR_TTL_ACTIVE_PHASES,
    )
    from kiro_crew.crew_log.store import (  # noqa: F401
        log_exception_text,
    )
    from kiro_crew.validation import (  # noqa: F401
        MODEL_PICKER_HIDDEN_MODEL_ID_RE,
    )
