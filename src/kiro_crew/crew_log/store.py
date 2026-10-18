"""The append-only crew log store: one file per unit, the gateway the only writer.

Layout, resolved against the live data home on every call (never captured at
import, so pod isolation and test isolation both keep working)::

    <data home>/crew-log/crews/<store name>/log.jsonl
    <data home>/crew-log/sessions/<store name>/log.jsonl
    <data home>/crew-log/members/<store name>/log.jsonl

``<store name>`` is the readable-plus-digest fold of the unit id that
``session_ledger`` and ``work_ledger`` already use, and the raw id lives in the
header (see :func:`crew_log_dir` for why the id is not the directory name). Both
files carry a ``.lock`` sibling in the same directory.

One dedicated ``crew-log`` root, holding every kind, is what carries the
protection: the leaf is hidden from a sandboxed process by the OS and fenced from
the agent's own file tools, and both fences are stated per leaf, so every kind
under this root inherits them and a new kind cannot be left out by omission. A
record a conductor is meant to trust as authority must not be forgeable by
anything that can call ``open()``, and a tool gate does not answer a subprocess --
which is why the session half does NOT live under the ``sessions`` transcript
root it would otherwise have shared.

Three properties are the whole design.

**Append only.** A line, once written, is never rewritten. There is exactly ONE
mutation: on ``open``, trailing bytes that are not a complete line are dropped.
Everything else -- a damaged interior line, an unknown envelope key, a type from
a newer writer -- is handled on the READ side by skipping or ignoring, never by
repairing the file. So two readers of the same bytes always agree, and a reader
is never the thing that changes history.

**Torn is decided by termination, not by taste.** Every append writes
``line + "\\n"`` and fsyncs, so a file that does not end in a newline was
interrupted mid-write. Those trailing bytes are the crash artifact and are
truncated -- unless they happen to parse whole, in which case only the newline
was lost: the record is kept and the next append re-supplies the separator. A
line that IS newline-terminated but does not parse is damage *inside* history,
so it is skipped on read and left on disk. Nothing else can be torn, which is
why this rule needs no heuristics.

**Seq comes from the file, under the lock.** ``seq`` is read back from the tail
inside the critical section on every append rather than trusted from the
in-process cache, so two writers cannot both believe they own the same number.
The read is a bounded window at the end of the file (``_TAIL_WINDOW``), not a
scan, so it costs the same on a crew log with ten lines and one with a million.
``.last_seq`` serves the cached value for callers that only want to look.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror
from kiro_crew.crew_log.store_cuts import (  # noqa: F401
    _CUTS_FILE,
    _close_cut,
    _cuts_dir_forced,
    _cuts_path,
    _fresh_phase,
    _note_cut_if_shorter,
    _open_cut,
    _read_cuts_raw,
    _retire_cuts_inode,
    _seed_cuts,
    _sync_cuts_dir,
    _unprove_cuts,
    _write_cuts,
    read_cuts,
)
from kiro_crew.crew_log.store_handle import CrewLog  # noqa: F401
from kiro_crew.crew_log.store_paths import (  # noqa: F401
    _LOCK_FILE,
    _ROOT_DIR,
    _ROOT_LEAF,
    _SEGMENT_GLOB,
    _SEGMENT_STEM,
    _SEGMENT_SUFFIX,
    LOG_FILE,
    _checked_crew_log_root,
    _is_segment_name,
    _lock_path,
    _mkdir_private,
    _numbered_segment_seq,
    _open_lock,
    _restrict_failed,
    _segment_first_seq,
    crew_log_dir,
    crew_log_path,
    crew_log_root,
    log_exception_text,
    logger,
    newest_segment,
    now_ms,
    oldest_segment,
    segment_first_seqs,
    segment_paths,
)
from kiro_crew.crew_log.store_removal import (  # noqa: F401
    _RENAME_UNDER_LEASE,
    REMOVE_ABSENT,
    REMOVE_FAILED,
    REMOVE_LINKED,
    REMOVE_OWNED,
    REMOVE_REMOVED,
    TRASH_HOLD_FILE,
    _durable_rename,
    _eager_folder_paused,
    _is_held,
    _remove_unit_contents,
    _run_in_hold,
    _sync_down,
    _sync_hold_dir,
    _write_hold,
    crew_log_trash_root,
    hold_staged_unit,
    hold_unit,
    is_trash_held,
    release_unit_hold,
    remove_unit,
    restore_staged_unit,
    stage_unit,
)
from kiro_crew.crew_log.store_results import (  # noqa: F401
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    STATUS_CORRUPT,
    STATUS_GONE,
    STATUS_OK,
    STATUS_PRUNED,
    Page,
    ReadCursor,
    Resolution,
    ResumePoint,
    _as_ref,
    _covers_span,
)
from kiro_crew.crew_log.store_retention import (  # noqa: F401
    _LIFECYCLE_TYPES,
    _SPAWNED_NEEDLE,
    _SPAWNED_TYPE,
    _TERMINAL_CLOSE_REASONS,
    _TREE_EDGE_TYPES,
    _TYPE_SESSION_CLOSED,
    _TYPE_SESSION_OPENED,
    _expired_unit_id,
    _is_terminal_close,
    _last_entry_of_types,
    _last_lifecycle_entry,
    _scan_whole_for_types,
    _spawned_child_slot,
    _still_expired,
    find_last_tree_edge,
    find_spawned_entries,
    read_last_tree_edge,
    sweep_expired,
    tree_edge_scan_identity,
)
from kiro_crew.crew_log.store_segment_reads import (  # noqa: F401
    _TAIL_WINDOW,
    _anchor_exists,
    _Bounded,
    _full_scan_last_seq,
    _has_content,
    _iter_entries,
    _iter_entries_located,
    _parses_to_object,
    _read_first_entry_seq,
    _read_header_line,
    _scan_tail,
    _seq_of,
    _Tail,
    read_head,
)
from kiro_crew.crew_log.store_segment_writes import (  # noqa: F401
    _append_line,
    _append_lines,
    _rollback_append,
    _truncate,
    _truncate_now,
    _write_then_sync,
)
from kiro_crew.crew_log.store_tail_repair import (  # noqa: F401
    APPROVAL_DECISION_UNKNOWN,
    STOP_REASON_INTERRUPTED,
    SUBAGENT_OUTCOME_UNKNOWN,
    TOOL_STATUS_UNKNOWN,
    _classify_record,
    _close_interrupted_tail,
    _closer_entries,
    _open_tail,
    _OpenTail,
    _orphan_chunk_offset,
    _refuse_content_repair,
)
from kiro_crew.crew_log.store_units import (  # noqa: F401
    _any_now_provable,
    _proved_header,
    _refuse_unprovable_unit,
    _slot_root_fingerprint,
    _unit_header_object,
    _unproven_holding_content,
    session_units_by_slot,
    session_units_for_slot,
    unit_dir_for,
    unit_dirs,
    unit_header_created_at,
    unit_header_slot,
    unit_ids,
    unit_opened_previous,
    unprovable_session_units,
)

# This module is the facade of the crew-log store and every name is reached through it as
# ``kiro_crew.crew_log.store.<name>``. It binds each name its owners define, so its namespace
# holds the definitions the unsplit module held, and it serves the rest from the owner holding
# them: the names the owners import, and ``_slot_index``, which ``session_units_by_slot`` rebinds.
# A write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner holding the name
# (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New work goes to the
# owner of its responsibility:
#
#   ``store_paths`` -- where a unit's files live, segment names, private dirs, the lock
#   ``store_segment_reads`` -- reading a segment: the tail window, entries, the header line
#   ``store_cuts`` -- the cut counter a reader checks before continuing a fold
#   ``store_segment_writes`` -- one durable append, its rollback, and the one truncation
#   ``store_tail_repair`` -- the closers ``open(repair=True)`` appends to an interrupted tail
#   ``store_results`` -- ``Page``, ``Resolution``, ``ReadCursor``, ``ResumePoint``
#   ``store_units`` -- proving a unit's header, listing units, the slot index
#   ``store_removal`` -- ``remove_unit``, trash holds, staging a unit out and back
#   ``store_retention`` -- ``sweep_expired``, and the session tree's tree-edge and spawned reads
#   ``store_handle`` -- ``CrewLog``, the handle on one unit's log


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.crew_log.store_paths",
    "kiro_crew.crew_log.store_segment_reads",
    "kiro_crew.crew_log.store_cuts",
    "kiro_crew.crew_log.store_segment_writes",
    "kiro_crew.crew_log.store_tail_repair",
    "kiro_crew.crew_log.store_results",
    "kiro_crew.crew_log.store_units",
    "kiro_crew.crew_log.store_removal",
    "kiro_crew.crew_log.store_retention",
    "kiro_crew.crew_log.store_handle",
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
    from kiro_crew.crew_log.store_units import _slot_index  # noqa: F401

if TYPE_CHECKING:  # names it serves from outside the store family, from their homes
    from kiro_crew.atomic_write import (  # noqa: F401
        atomic_write,
        fsync_dir,
    )
    from kiro_crew.config.paths import (  # noqa: F401
        data_home,
    )
    from kiro_crew.crew_log.entry_types import (  # noqa: F401
        validate_data,
    )
    from kiro_crew.crew_log.errors import (  # noqa: F401
        CODE_ALREADY_EXISTS,
        CODE_ALREADY_OWNED,
        CODE_BAD_DATA,
        CODE_BAD_HEADER,
        CODE_BAD_ROOT,
        CODE_BAD_SEGMENT,
        CODE_BAD_THREAD,
        CODE_INVALID_ID,
        CODE_NO_LEDGER,
        CODE_SEGMENT_GAP,
        CODE_UNKNOWN_ENTRY_TYPE,
        CrewLogError,
        CutUnaccounted,
        IndeterminateAppend,
    )
    from kiro_crew.crew_log.lease import (  # noqa: F401
        LEASE_FILE,
    )
    from kiro_crew.crew_log.lease import acquire as acquire_lease  # noqa: F401
    from kiro_crew.crew_log.lease import release as release_lease  # noqa: F401
    from kiro_crew.crew_log.schema import (  # noqa: F401
        KIND_CREW,
        KIND_MEMBER,
        KIND_SESSION,
        MAX_ENTRY_BYTES,
        Entry,
        Header,
        Ref,
        build_header,
        check_ownership,
        parse_header,
        require_data,
        require_entry_line,
        require_kind,
        require_unit_id,
        serialize,
    )
    from kiro_crew.jsonl_util import (  # noqa: F401
        UnreadableRecord,
        bounded_raw_records,
        bounded_raw_records_with_offsets,
        strict_raw_records,
    )
    from kiro_crew.owner_only_files import (  # noqa: F401
        OWNER_ONLY_FILE_MODE,
        mkdirs_owner_only,
        owner_only_opener,
    )
    from kiro_crew.platform_compat import (  # noqa: F401
        file_lock,
        restrict_dir_to_owner,
    )
    from kiro_crew.session_ledger import (  # noqa: F401
        _store_name,
        is_link,
        resolved_within,
        unlink_lock_in_hold,
    )
