"""Measure the disk a session costs, and reclaim it under user control.

A session is reported as ONE thing with ONE size and one session count. Its bytes
are split across two stores that Kiro Crew and kiro-cli each own:

* ``<data home>/sessions/<stem>.jsonl`` plus its rotated
  ``sessions/archive/<stem>__<stamp>.jsonl`` segments — the transcript, read by
  dashboard history, search and memory consolidation — and
  ``sessions/<stem>.attachments/`` — the images its messages show, which
  ``/api/file-raw`` serves by the paths the transcript holds
  (:mod:`kiro_crew.chat_attachments`) — and ``sessions/.threads/<stem>.json``,
  the reply threads on its messages (``dashboard/chat_threads.py``).
* ``<kiro home>/sessions/cli/<sid>.json`` + ``<sid>.jsonl`` — kiro-cli's replay
  log, read to resume the session.

A session's ``<data home>/crew-log/sessions/<unit>/`` directories, one per ACP
session id, are measured and reclaimed with it but staged inside the crew-log tree
(see :func:`_stage_crew_logs`).

**A session's halves are always reclaimed together.** Without the replay log the
transcript lists a session that cannot resume; without the transcript a resumable
session has no history or search. :func:`_unit_paths` is the single answer to "what
files is this session made of", and every move, restore and delete goes through it.
A session may have only one half (a subagent run has no transcript, a pruned mapping
leaves no replay log); whatever halves exist move together.

Reclaiming moves files into ``<data home>/trash/sessions/<batch>/``: on a default
install a same-filesystem :func:`os.rename`, instant and reversible. Space returns
only when the trash is emptied, which :attr:`StorageReport.trash_bytes` reports.
Each batch has an append-only manifest of every file's original absolute path, so a
restore needs no layout inference, a six-figure batch costs linear bytes, and an
interrupted batch stays restorable.

Reading is cached; reclaiming never is
--------------------------------------
Enumerating the stores is the whole cost of a read (half a million replay files on
the measured machine), so a pass is kept briefly and shared by the row list, totals
and row detail. Only the filesystem scan and the read-path co-tenant pass are
cached; index flags are recomputed every call, refusal paths run uncached, and every
mutation re-reads inside the lock. A stale cache can make a report old, never make a
reclaim take something it should not.

Sessions this instance does not own
-----------------------------------
A pod gets its own replay store (``pod/runtime.py`` exports ``KIRO_HOME``), but a
pod directory from before that export may still own sessions in the machine-wide
store. :func:`cotenant_sids` reads those maps and protects the sessions they name.
A blanket refusal is kept only where ownership cannot be established: an unreadable
co-tenant map, or a data home isolated from the store it reads (see
:func:`reclaim_block_reason`).
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror
from kiro_crew.session_storage_parts.batch_manifest import (  # noqa: F401
    _MANIFEST_RECORD_CAP,
    _append_entry,
    _entry_bytes,
    _fsync_tree,
    _header_names_this_batch,
    _levels_between,
    _manifest_records,
    _manifest_rels,
    _open_manifest,
    _OversizedManifestRecord,
    _read_manifest,
    _rels_digest,
    _rewrite_manifest,
    _summarize_manifest,
    _sync_batch,
    _warn_undecodable,
    _write_header,
)
from kiro_crew.session_storage_parts.batch_removal import (  # noqa: F401
    _approve_batch,
    _approve_emptied_batch,
    _discard_restored_batch,
    _empty_trash_locked,
    _remove_emptied_batch,
    empty_trash,
    staged_targets,
)
from kiro_crew.session_storage_parts.crew_staging import (  # noqa: F401
    _CREW_HOLDS_FILE,
    _crew_holds_path,
    _crew_logs_left_staged,
    _crew_staging,
    _discard_crew_staging,
    _has_staged_crew,
    _put_back_crew_logs,
    _read_crew_holds,
    _record_crew_holds,
    _release_crew_holds,
    _release_restored_holds,
    _restore_crew_logs,
    _return_crew_logs,
    _stage_crew_logs,
    _staged_crew_dirs,
    _sync_batch_or_warn,
    _unlisted_crew_staging,
)
from kiro_crew.session_storage_parts.file_moves import (  # noqa: F401
    _batch_dir,
    _canonical_origin,
    _copy_across_filesystems,
    _file_stamp,
    _identity_of,
    _move_file,
    _move_file_exclusive,
    _move_sidecar_pinned,
    _mutation_lock,
    _open_source_no_follow,
    _origin_path,
    _publish_then_drop,
    _rollback,
    _staged_path,
    _unlisted_files,
)
from kiro_crew.session_storage_parts.pinned_delete import (  # noqa: F401
    _FD_SAFE_DELETE,
    _PROGRESS_EVERY_FILES,
    _close_all,
    _coarse_remove,
    _delete_listed_files,
    _dir_open_flags,
    _drain,
    _incomplete_if_present,
    _inode_of,
    _listed_bytes,
    _open_absolute_nofollow,
    _open_chain,
    _plain_parts,
    _remove_pinned_batch,
    _remove_scanned_dirs,
    _rename_back,
    _scan_tree,
    _stage_batch_by_name,
    _sweep_staged,
    _unlink_debris,
)
from kiro_crew.session_storage_parts.store_inventory import (  # noqa: F401
    _SCAN_CACHE_TTL,
    _attachment_files,
    _bucketize,
    _cached_scan,
    _cli_index,
    _cotenant_cache_lock,
    _cotenant_key,
    _crew_log_owner,
    _crew_log_units,
    _has_own_replay_store,
    _pod_root,
    _replay_store_cotenants,
    _same_filesystem,
    _scan_cache_lock,
    _scan_epoch,
    _scan_key,
    _scan_raw,
    _scan_raw_uncached,
    _scan_started,
    _scan_units,
    _store_scan,
    _unit_paths,
    cotenant_sids,
    invalidate_scan_cache,
    list_trash,
    list_units,
    measure,
    reclaim_block_reason,
    select_reclaimable,
)
from kiro_crew.session_storage_parts.store_layout import (  # noqa: F401
    _CLI_SUFFIXES,
    _O_NOFOLLOW,
    _SECONDS_PER_DAY,
    _TRANSCRIPT_SUFFIX,
    _UNIT_ID_RE,
    BUCKET_DAYS,
    INCOMING_PREFIX,
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    MIN_RECLAIM_AGE_DAYS,
    MUTATION_LOCK_NAME,
    SKIP_IDENTITY_CHANGED,
    SKIP_INCOMPLETE,
    SKIP_OUTSIDE_ROOT,
    SKIP_UNLISTED_FILES,
    SKIP_UNREADABLE,
    STAGE_CLI_LEAF,
    STAGE_CREW_LEAF,
    TRASH_DIR_NAME,
    TRASH_SESSIONS_LEAF,
    BatchIdentity,
    EmptyProgress,
    EmptySkip,
    SessionIndex,
    SessionStorageError,
    SessionUnit,
    StorageBucket,
    StorageReport,
    TrashBatch,
    _archive_index,
    _crew_archive_dir,
    _crew_sessions_dir,
    _RawUnit,
    _tree_bytes,
    _tree_files,
    _validate_unit_id,
    logger,
    trash_root,
)
from kiro_crew.session_storage_parts.trash_reclaim import (  # noqa: F401
    _move_to_trash_locked,
    _purge_search_index,
    move_to_trash,
)
from kiro_crew.session_storage_parts.trash_restore import (  # noqa: F401
    _restore_locked,
    restore,
)

# Facade of the session store: every name is reached as ``kiro_crew.session_storage.<name>``.
# It binds each name its owners define but two and serves the rest (imported names, and
# the scan and co-tenant caches ``store_inventory`` rebinds) from the owner. A patch
# reaches every owner holding the name (:mod:`kiro_crew._facade_mirror`). Owners never
# import this module. New work goes to the owner of its responsibility:
#   ``store_layout`` -- names and limits, the data types, the trash and transcript roots
#   ``batch_manifest`` -- a batch's manifest: write, read under a cap, summarize, sync
#   ``crew_staging`` -- a session's crew logs in the trash, and their holds
#   ``store_inventory`` -- the scan, its caches, measure, co-tenants, list_trash
#   ``pinned_delete`` -- deleting a batch's listed files by descriptor, or coarsely
#   ``file_moves`` -- the mutation lock, moving one file in or out, origins, rollback
#   ``batch_removal`` -- staged_targets, empty_trash, removing an emptied batch
#   ``trash_reclaim`` -- move_to_trash
#   ``trash_restore`` -- restore
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.session_storage_parts.store_layout",
    "kiro_crew.session_storage_parts.batch_manifest",
    "kiro_crew.session_storage_parts.crew_staging",
    "kiro_crew.session_storage_parts.store_inventory",
    "kiro_crew.session_storage_parts.pinned_delete",
    "kiro_crew.session_storage_parts.file_moves",
    "kiro_crew.session_storage_parts.batch_removal",
    "kiro_crew.session_storage_parts.trash_reclaim",
    "kiro_crew.session_storage_parts.trash_restore",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: the unsplit module's public names, and TYPE_CHECKING.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # each kiro_crew name in _EXPORTS from its home, for type checkers and IDEs
    from kiro_crew import hooks, pinned_fs, platform_compat  # noqa: F401
    from kiro_crew.atomic_write import atomic_write, fsync_dir, replace_with_retry  # noqa: F401
    from kiro_crew.chat_attachments import ATTACHMENTS_DIR_SUFFIX, attachments_dir  # noqa: F401
    from kiro_crew.config.paths import (  # noqa: F401
        CONFIG_DIR_LEAF,
        KIRO_BASE_DIR_NAME,
        data_home,
        kiro_home,
        kiro_sessions_dir,
        legacy_home,
    )
    from kiro_crew.history import (  # noqa: F401
        ARCHIVE_DIR_NAME,
        ARCHIVE_SEGMENT_DELIMITER,
        SESSIONS_DIR_NAME,
        THREADS_DIR_NAME,
        THREADS_SIDECAR_SUFFIX,
        ConversationLog,
        HistoryLockTimeout,
        threads_sidecar_for_stem,
        transcript_lock_stems,
    )
    from kiro_crew.history_index import INDEX_FILENAME, SessionSearchIndex  # noqa: F401
    from kiro_crew.session_map import SESSION_MAP_FILENAME  # noqa: F401
    from kiro_crew.session_storage_parts.store_inventory import (  # noqa: F401
        _cotenant_cache,
        _scan_cache,
    )
