"""Skills loader — markdown skill files for agent capabilities.

``SkillsLoader`` is the composition root: it owns the loader's state and locks, and
its behaviour lives in two families of owner modules. A member that delegates to
:mod:`kiro_crew.skill_runtime` keeps a one-line body here that calls the function of
the same name; those owners read this module's constants, and every name it imports
from another ``kiro_crew`` module, through it at call time. A member whose body lives
in a ``kiro_crew.skills_*`` owner is bound by name (``member = _owner.member``), and
the module-level code those owners hold is re-imported here, so every public name, and
every name a test patches here, stays importable from this module as the same object.
The facade mirror at the end of this file sends a write of a name this module binds to
every ``skills_*`` owner binding the same object, so a patch of
``kiro_crew.skills.<name>`` reaches the moved code exactly as it reached one module
global. A helper that lives in a ``skill_runtime`` owner is patched on that owner;
sibling owners call it as an attribute of that module.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import os
import re
import shutil
import stat
import sys as _sys
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Iterable, Iterator, Literal, NamedTuple

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import hooks as hooks_module  # noqa: F401
from kiro_crew import pinned_fs, skill_trust  # noqa: F401
from kiro_crew import skills_body_reads as _body_reads
from kiro_crew import skills_builtin_sync as _builtin_sync
from kiro_crew import skills_candidate_tree as _candidate_tree
from kiro_crew import skills_discovery as _discovery
from kiro_crew import skills_review as _review
from kiro_crew import skills_selection as _selection

# Some names below are bound for others rather than for this module's own code:
# the owners in ``skill_runtime`` read them through this module at call time, and
# callers may import any of them from here.
from kiro_crew.atomic_write import (  # noqa: F401
    atomic_write,
    open_access_control_source,
    pinned_parent_replace_supported,
    replace_with_retry,
)
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig, config_dir  # noqa: F401
from kiro_crew.cron import referenced_skill_names  # noqa: F401
from kiro_crew.dep_sync import normalize as normalize_distribution_name  # noqa: F401
from kiro_crew.frontmatter import SKILL_LOADER, parse_frontmatter
from kiro_crew.hooks import (  # noqa: F401
    FileTooLargeError,
    safe_read_file,
    safe_read_file_bytes_nolink,
    validate_file_path,
)
from kiro_crew.memory_recall import recall_terms  # noqa: F401
from kiro_crew.metrics.provider import get_recorder  # noqa: F401
from kiro_crew.platform_compat import (  # noqa: F401
    PinnedDirectory,
    ensure_owner_rwx_dirs,
    file_lock,
    is_link_or_junction,
    pinned_directory,
    rmtree_force,
)
from kiro_crew.project_scope import project_scope_satisfied  # noqa: F401
from kiro_crew.security import (  # noqa: F401
    is_sensitive_path,
    is_sensitive_resolved_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel  # noqa: F401

# The loader's rules live in these owners; the imports below them keep every public
# name that moved, and every name a test patches here, importable from here as the
# same object.
from kiro_crew.skill_runtime import authoring as _authoring
from kiro_crew.skill_runtime import auto_skills as _auto_skills
from kiro_crew.skill_runtime import catalog as _catalog
from kiro_crew.skill_runtime import delivery as _delivery
from kiro_crew.skill_runtime import listing as _listing
from kiro_crew.skill_runtime import read_credit as _read_credit
from kiro_crew.skill_runtime import search as _search
from kiro_crew.skill_runtime import versions as _versions
from kiro_crew.skill_runtime.catalog import (  # noqa: F401
    _GLOB_CHARS,
    _builtin_dir_app_name,
    _canonical_glob,
    _canonical_prefix,
    _disabled_app_names,
    _glob_with_prefix,
    _literal_split,
    _matches_any,
    _project_prefix,
    _StoredCatalog,
    _with_canonical_globs,
)
from kiro_crew.skill_runtime.delivery import _family_line, _namespace_groups  # noqa: F401
from kiro_crew.skill_runtime.listing import (  # noqa: F401
    _dedupe_identical_skills,
    _fingerprint_mtime_and_size,
)
from kiro_crew.skill_runtime.read_credit import (  # noqa: F401
    _shell_segments_reading_content,
    _tool_read_path_candidates,
)
from kiro_crew.skill_runtime.search import SkillSearchReport, _body_term_hits  # noqa: F401
from kiro_crew.skill_search_index import (  # noqa: F401
    SKILL_SEARCH_INDEX_FILENAME,
    SkillSearchIndex,
    body_fingerprint,
)
from kiro_crew.skill_usage import names_skill_file  # noqa: F401  (read_credit reads it via sk)
from kiro_crew.skill_usage import SKILL_USAGE_FILENAME, SkillUsageLedger
from kiro_crew.skills_auto_model import (  # noqa: F401
    _AUTO_NAME_PATTERN,
    _PENDING_DOCUMENT_MAX_BYTES,
    AUTO_ARCHIVE_DIRNAME,
    AUTO_PENDING_DIRNAME,
    AUTO_SKILL_MAX_PROCEDURE_CHARS,
    AUTO_SKILL_NAMESPACE,
    AUTO_SKILL_SOURCE_VALUE,
    AUTO_SLUG_CLAIM_LOCK_NAME,
    AUTO_SLUG_CLAIM_LOCK_TIMEOUT_SECS,
    MAX_SKILL_VERSIONS,
    VERSIONS_DIRNAME,
    AutoSkillProvenance,
    ClaimRefusal,
    _build_auto_skill_content,
    _emit_pending_consumed,
    _emit_pending_staged,
    set_pending_consumed_hook,
    set_pending_staged_hook,
)
from kiro_crew.skills_body_reads import (  # noqa: F401
    _CONTENT_READ_TOOLS,
    _SHELL_READ_VERBS,
    _SHELL_SEGMENT_RE,
    _SHELL_SKILL_PATH_RE,
    _TOOL_READ_PATH_KEYS,
    PINNED_SKILL_BODIES_CAP,
    PROJECT_SKILL_BODY_CAP,
    SKILL_READ_CAPACITY,
    SKILL_READ_OUTSIDE_SCOPE,
    SKILL_READ_OVER_CAPACITY,
    SKILL_READ_UNREADABLE,
    SkillBodyPage,
    SkillReadRefusal,
    _decode_skill_text,
    _html_skill_refused,
    _page_skill_body,
    _warn_html_skill,
)
from kiro_crew.skills_builtin_provenance import (  # noqa: F401
    _CRON_SOURCE_MAX_BYTES,
    _FINGERPRINT_MAX_BYTES,
    _FINGERPRINT_MAX_ENTRIES,
    _PROVENANCE_FORMAT,
    _PROVENANCE_MARKER,
    CRON_SOURCE_DIVERGED,
    CRON_SOURCE_IN_SYNC,
    CRON_SOURCE_UNVERIFIABLE,
    SKILL_INSTALL_BEHIND,
    SKILL_INSTALL_EDITED,
    SKILL_INSTALL_IN_SYNC,
    SKILL_INSTALL_UNVERIFIABLE,
    CronScriptSource,
    InstalledSkillCurrency,
    _first_linked_skill_component,
    _read_for_comparison,
    _record_builtin_provenance,
    _recorded_fingerprint,
    _skill_currency_state,
    _skill_script_index,
    _skill_tree_fingerprint,
    _strip_carried_opt_out,
    _tree_entries,
    _trees_stat_equal,
    _verified_unchanged_fingerprint,
    _write_provenance_marker,
    deployed_cron_script_sources,
    installed_skill_currency,
)
from kiro_crew.skills_builtin_sync import (  # noqa: F401
    _RELOCATED_SKILLS,
    _RETIRED_CONDUCTOR_SKILL_MAX_BYTES,
    RETIRED_CONDUCTOR_SKILL_SHA256,
    _claim_dir_for_replacement,
    _copy_with_carried_skill_md,
    _dispose_superseded_slot,
    _ensure_builtin_skills,
    _finalize_user_backup,
    _manifest_is_newer,
    _remove_ignorable_dir,
    _retire_verified_claim,
    _tree_has_content,
    _tree_newest_mtime,
    is_retired_conductor_skill,
    remove_retired_conductor_skill,
)
from kiro_crew.skills_candidate_tree import (  # noqa: F401
    _PENDING_SCRIPT_MAX_DEPTH,
    _PENDING_SCRIPT_MAX_ENTRIES,
)
from kiro_crew.skills_discovery import (  # noqa: F401
    _BUILTIN_SKILLS_DIR,
    _CATALOG_READ_BATCH,
    _CATALOG_READ_WORKERS,
    _CATALOG_REVALIDATE_AFTER_SECS,
    _CHAIN_REFUSALS_WARNED,
    _COLD_CATALOG_WAIT_SECS,
    _DISCOVERY_IN_PROGRESS_NOTICE,
    _INSTALLED_PACKAGE_DIR,
    _ITER_CACHE_TTL_SECS,
    _PROJECT_DIR_OPEN_FLAGS,
    _PROJECT_SKILL_MAX_DEPTH,
    _SKILL_FILE,
    _VETTED_READS_MAX,
    SKILLS_DIR_NAME,
    _iter_skill_files,
    _open_project_dir_chain,
    _package_record_cache,
    _project_skills_dir,
    _read_package_record,
    _record_stamp,
    _recorded_skill_digests,
    _trusted_skill_roots,
    _walk_confined_skill_fd,
    _walk_confined_skill_tree,
    _within_any,
    project_skill_tree_present,
    skills_dir,
)
from kiro_crew.skills_review import (  # noqa: F401
    _VALIDATION_REPORT_MAX_FINDINGS,
    _VALIDATION_REPORT_MAX_STRING_CHARS,
    _VALIDATION_REPORT_TRUNCATION_KEY,
    PendingApprovalRefused,
)
from kiro_crew.skills_script_validator import MAX_SCRIPT_BYTES, validate_scripts  # noqa: F401
from kiro_crew.skills_selection import (  # noqa: F401
    _FAMILY_LINE_MAX_LABELS,
    _INDEX_HEAD_SLOTS,
    _MAPPED_BLOCK_OVERHEAD_BYTES,
    _MIN_TRIGGER_OVERLAP,
    _NEW_SKILL_BOOST_WINDOW_SECS,
    _SHORT_DESC_CHARS,
    _packaged_skill_names,
)
from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP, trigger_score, words_of  # noqa: F401

logger = logging.getLogger(__name__)


# Whether skill CRUD can address the skill directory and its SKILL.md relative to
# a pinned parent descriptor. supports_pinned_walk covers the openat capability
# itself; the extras are exactly the OTHER descriptor-relative syscalls the CRUD
# pinned branches issue (create, update and create's rollback in
# ``skill_runtime/authoring.py``, delete here), named one per call site so the
# probe stays derived from the code rather than copied from a neighbour:
#   os.mkdir  -- create, the leaf skill directory under the pinned parent
#   os.unlink -- create's rollback (the partial SKILL.md), and update, via
#                atomic_write's staging cleanup under the pinned parent
#   os.stat   -- delete, via pinned_fs.stat_at, and create's rollback, via
#                pinned_fs.remove_dir_verified (os.lstat is not a supports_dir_fd
#                member even on Linux; the capability belongs to os.stat)
#   os.rename -- create's rollback, via remove_dir_verified's stage-aside
#   os.rmdir  -- create's rollback, both the staged-aside directory and the
#                reclaim when the leaf open loses a race to the mkdir
# delete's own removal is still a by-name shutil.rmtree, the residual documented
# there -- os.rmdir is here for the ROLLBACK, not for that. update additionally
# needs a descriptor-relative rename for atomic_write's publish, which is that
# module's own probe and is asked at the call site. Where this is False (Windows)
# the by-name create/write/rmtree are the floor, unchanged.
_DIR_FD_SUPPORTED = pinned_fs.supports_pinned_walk() and {
    os.mkdir,
    os.unlink,
    os.stat,
    os.rename,
    os.rmdir,
}.issubset(os.supports_dir_fd)


# ── $skill inline trigger ──
# A ``$skillname`` token anywhere in a user message explicitly loads that skill,
# across all three sources (kirocrew builtin, workspace, extra paths).
# Resolution is allowlist-only: the token must match the last path segment of an
# already-enumerated skill key (per input-validation guidance — no path
# is ever constructed from the raw token, which structurally blocks traversal like
# ``$../../etc/passwd``). The charset is deliberately lowercase-led so shell-style
# tokens (``$PATH``, ``$5``) and prose ($variable mid-sentence in caps) don't match
# real skill slugs.
#   (?<![\w$])  — not preceded by a word char or another $ (avoids ``foo$bar``, ``$$x``)
#   [a-z0-9]    — must start with a lowercase letter or digit
#   [a-z0-9/_-]* — slug body: lowercase, digits, slash (nested keys), underscore, hyphen
_DOLLAR_SKILL_PATTERN = re.compile(r"(?<![\w$])\$([a-z0-9][a-z0-9/_-]*)")
# Cap how many distinct $skills one message may expand — bounds prompt growth and
# matches the spirit of the per-message trigger cap.
_MAX_DOLLAR_SKILLS = 5


class SkillContextCapacityError(ValueError):
    """Required instructions cannot fit; never silently cut a required skill."""


class _ScopedSkillEntry(NamedTuple):
    """Admission provenance travels with the entry, independently of its key."""

    key: str
    path: Path
    project_root: str | None
    mapping_root: str | None = None


class SkillsLoader:
    """Load skill markdown files from ~/.kiro/crew/skills/.

    Supports nested directories. Each skill is identified by its
    relative path from the skills root (e.g. ``utils/tiny-url``).

    Directory layout::

        ~/.kiro/crew/skills/
        ├── learn/SKILL.md
        ├── subagent/SKILL.md
        ├── code/
        │   ├── code-review/SKILL.md
        │   └── code-task-generation/SKILL.md
        └── utils/
            ├── url-shortener/SKILL.md
            └── mcp-debug/SKILL.md
    """

    def __init__(
        self,
        skills_path: Path | None = None,
        install_builtins: bool = True,
        config: KiroCrewConfig | None = None,
    ):
        self._dir = skills_path or skills_dir()
        if install_builtins:
            # Never sync on a running event loop: the sync verifies user-owned
            # trees (stat walks, capped content hashing) before it may replace
            # them, so a loader built inside a dashboard/Slack handler would
            # stall the loop and the liveness heartbeat. The gateway already
            # syncs at startup in a worker thread; on-loop constructions just
            # read the already-synced tree.
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                _ensure_builtin_skills(self._dir)
            else:
                logger.debug(
                    "Skipping builtin-skill sync on a running event loop; "
                    "gateway startup owns the sync"
                )
        # Cache: path → (mtime or confined-content digest, parsed_frontmatter).
        self._fm_cache: dict[str, tuple[float | bytes, dict[str, str]]] = {}
        # TTL cache of the discovered (name, path) list — avoids an os.walk per
        # message in get_triggered_skills. Keyed by canonical project directory
        # ("" when no project, or the project's skills are not trusted): a
        # trusted project contributes its own skills root, so a single shared
        # slot would serve one session's project skills to a session working in
        # a different project for the whole TTL. (monotonic_deadline, results)
        self._iter_cache: dict[str, tuple[float, list[tuple[str, Path, str | None]]]] = {}
        # Stat fingerprints from the walk that produced each scope's list, so
        # `list_skills` can decide whether a persisted metadata row is still usable
        # WITHOUT re-stat'ing every skill. Keyed scope key → path → fingerprint;
        # an absent entry simply means "stat it yourself".
        self._catalog_fingerprints: dict[str, dict[str, str]] = {}
        # Scope keys whose served list is known to be partial, because the first
        # build outran `_COLD_CATALOG_WAIT_SECS`. Read by `catalog_status` so a
        # search, a list or a directory build can say "still discovering" instead
        # of reporting a truncated answer as the whole truth.
        self._catalog_incomplete: set[str] = set()
        # Unconfined paths this process has VETTED, the only ones
        # `_read_enumerated_skill_bytes` reads without first checking them
        # (`_vet_unconfined_path`). Fail-closed: any other path, such as a row
        # adopted from the agent-writable stored snapshot, is checked before its
        # first read. `_walk_vetted` holds exactly the paths the newest published
        # walk returned, replaced whole by the next; `_read_vetted` holds the
        # paths a check admitted, bounded to `_VETTED_READS_MAX` and emptied by
        # every invalidation and every published walk.
        self._walk_vetted: frozenset[str] = frozenset()
        self._read_vetted: OrderedDict[str, None] = OrderedDict()
        # `_snapshot_admitted_roots`, keyed by (generation, skills dir, extra
        # paths), so a vet resolves the roots once per root set, not per read.
        self._admitted_roots_memo: tuple[tuple[object, ...], tuple[str, ...]] | None = None
        # Single-flight background builds: scope key → the event its build sets on
        # completion. Concurrent sessions sharing this loader join one walk rather
        # than each walking the same tree.
        self._catalog_refreshes: dict[str, tuple[threading.Event, int]] = {}
        # Scope keys queued for the refresh worker, with the generation current when
        # each was queued. One worker drains them, so N scopes cost N SERIAL walks
        # rather than N concurrent ones — a session count must not multiply the
        # filesystem work a shared corpus costs.
        self._catalog_pending: dict[str, int] = {}
        self._catalog_wakeup = threading.Event()
        self._catalog_worker: threading.Thread | None = None
        # True while a build holds the search-index handle. `close()` then leaves
        # shutting the index down to that build, so its store still lands: a host
        # served only by short-lived loaders converges on nothing else.
        self._catalog_building = False
        # Guards the four structures above AND `_iter_cache`: background builds
        # publish into them from a worker thread while foreground callers read.
        self._catalog_lock = threading.Lock()
        # Bumped by every in-process invalidation. A build that started before the
        # bump is publishing an answer that predates a change already known, so its
        # result is dropped rather than allowed to overwrite the newer state. The
        # index's own epoch covers the same race BETWEEN processes.
        self._catalog_generation = 0
        # The newest generation a committed `drop_catalog` covers. The stored
        # snapshot is served only while it equals `_catalog_generation`: an
        # invalidation moves the generation before it drops, so the tier is off
        # while that drop runs, and a drop that FAILS (a neighbour held the write
        # lock past the busy timeout) leaves it behind until a later one lands.
        self._snapshot_clean_generation = 0
        # Held across an invalidation's drop and across a build's store, so the
        # two never interleave: a drop always lands after a store a mutation
        # overtook, and a drop one of them landed is not repeated by the other.
        # Ordered before the index's own lock, never inside it.
        self._catalog_drop_lock = threading.Lock()
        self._closed = False
        self._disabled_apps_cache: tuple[float, frozenset[str]] | None = None
        #: ``id(catalog list) -> (that list, disabled apps, visible rows)``; see
        #: ``skill_runtime.catalog._iter_visible``.
        self._visible_memo: dict[
            int, tuple[list[tuple[str, Path, str | None]], frozenset[str], list]
        ] = {}
        # (canonical key, allowed) pairs already audited, so the enforcement
        # record is written on first use rather than once per message.
        self._audited_projects: set[tuple[str, bool]] = set()
        # Whether the unsupported-platform warning has been considered, so it
        # is logged at most once per loader rather than once per message.
        self._project_skills_unsupported_warned = False
        # Extra skill paths from config (config injectable for testing)
        cfg = config or KiroCrewConfig.load()
        # The per-message trigger cap is resolved at USE from the live snapshot
        # (see _max_triggered_now), so `kirocrew config set skills.max_triggered`
        # applies to the next message without rebuilding the loader. The
        # construction-time value stays as the fallback for a loader built from an
        # explicitly injected config, before any snapshot exists.
        self._max_triggered = cfg.skills.max_triggered
        self._extra_paths: list[Path] = []
        self._configured_extra_paths: list[Path] = []
        for p in cfg.skills.extra_paths:
            resolved = Path(p).expanduser().resolve()
            if is_sensitive_path(str(resolved)):
                logger.warning("Skipping sensitive extra skill path: %s", p)
            elif resolved.is_dir():
                self._extra_paths.append(resolved)
                self._configured_extra_paths.append(resolved)
            else:
                logger.debug("Extra skill path does not exist: %s", p)

        # Edition-contributed skill paths (CPP seam). A companion returns extra
        # SKILL.md source roots via McpToolingProvider.extra_skills(); the public
        # Default returns [] so this is a no-op for the standalone edition.
        # Lowest precedence (appended last, after local + configured extra_paths),
        # sensitivity- and
        # existence-checked exactly like the configured extra_paths. Deferred
        # context read via the sel.py pattern so skills.py never imports the
        # platform package at module load; fails closed to no extra paths.
        from kiro_crew.platform.context import current_context, safe_context_call

        edition_skill_paths: list[Path] = safe_context_call(
            lambda: list(current_context().mcp_tooling.extra_skills()),
            fallback_factory=list,
            log_message="extra_skills lookup failed; using none",
        )
        self._edition_extra_paths: list[Path] = []
        for edition_path in edition_skill_paths:
            resolved = Path(edition_path).expanduser().resolve()
            if resolved in self._extra_paths:
                continue
            if is_sensitive_path(str(resolved)):
                logger.warning("Skipping sensitive edition skill path: %s", edition_path)
            elif resolved.is_dir():
                self._extra_paths.append(resolved)
                self._edition_extra_paths.append(resolved)
            else:
                logger.debug("Edition skill path does not exist: %s", edition_path)

        # Persistent usage ledger for hotness-ranked lazy skill injection.
        # Co-located with the skills root's parent (the KiroCrew home) so it
        # travels with runtime state. Best-effort: a failure here must not break
        # skill loading — ranking then falls back to recency/unweighted order.
        self._usage: SkillUsageLedger | None
        try:
            self._usage = SkillUsageLedger(self._dir.parent / SKILL_USAGE_FILENAME)
        except Exception:  # pragma: no cover — ledger is best-effort telemetry
            logger.warning(
                "skill-usage: ledger init failed; ranking falls back to unweighted",
                exc_info=True,
            )
            self._usage = None
        # Term index behind `search_skills`'s body fallback, in the same home as
        # the usage ledger. Best-effort for the same reason: an unusable index
        # only costs the search its old per-file read path.
        self._search_index: SkillSearchIndex | None
        try:
            self._search_index = SkillSearchIndex(self._dir.parent / SKILL_SEARCH_INDEX_FILENAME)
        except Exception:  # pragma: no cover — index is best-effort
            logger.warning(
                "skill-search-index: init failed; search reads bodies from disk",
                exc_info=True,
            )
            self._search_index = None
        # `skills.extra_paths` is a set of source ROOTS, so it is pushed rather than
        # read at use: re-resolving every root (a realpath plus a sensitivity check
        # per entry) on every message is exactly the cost the iter-cache exists to
        # avoid. `max_triggered` is a single int and IS read at use, so it needs no
        # subscription. Held on self because the watcher keeps a bound method weakly.
        self._config_sub = live.subscribe(
            "skills.extra_paths", callback=self._on_config_change, name="SkillsLoader"
        )

    def close(self) -> None:
        """Release persistent resources owned by this loader.

        ``_closed`` is set FIRST, so a build already on the worker publishes
        nothing into a loader that is going away, and so no new build can be queued
        behind this call. The worker is a DAEMON thread and is not joined: a cold
        first walk can outlive the loader that triggered it, and the unsigned MCP
        fallback closes its loader as soon as one search returns, so joining here
        would charge that call the very walk this design moved off the request path.

        A build still in flight KEEPS the index handle, and closes it itself when it
        finishes. Closing it here instead would discard that walk's store, and on a
        host served only by short-lived loaders the store is the one thing that lets
        the next call skip the walk — so discarding it makes every call re-walk
        forever. Publishing into this loader's memory is still refused; only the
        persistence survives.

        Every outstanding completion event is SET, because a queued build the
        worker abandons never reaches the ``finally`` that would have set it — and a
        cold caller waiting on that event would otherwise sit out its whole budget
        for an answer that will never arrive.
        """
        with self._catalog_lock:
            self._closed = True
            self._catalog_pending.clear()
            pending_events = [event for event, _gen in self._catalog_refreshes.values()]
            self._catalog_refreshes.clear()
            # A build in flight owns the handle until it returns; it closes the
            # index on its own way out.
            index = None if self._catalog_building else self._search_index
        for event in pending_events:
            event.set()
        self._catalog_wakeup.set()
        if index is not None:
            index.close()

    # ── Skill discovery: ``kiro_crew.skills_discovery`` ──
    _on_config_change = _discovery._on_config_change
    reconfigure = _discovery.reconfigure
    _screen_extra_paths = staticmethod(_discovery._screen_extra_paths)
    _adopt_extra_paths = _discovery._adopt_extra_paths
    # ── Skill body reads: ``kiro_crew.skills_body_reads`` ──
    _trusted_project_key = _body_reads._trusted_project_key
    _audit_project_skill_enforcement = _body_reads._audit_project_skill_enforcement
    _read_enumerated_skill_bytes = _body_reads._read_enumerated_skill_bytes
    _cached_frontmatter = _body_reads._cached_frontmatter
    _confined_frontmatter_and_size = _body_reads._confined_frontmatter_and_size
    load_skill = _body_reads.load_skill
    _read_global_skill_text = _body_reads._read_global_skill_text
    _emit_lazy_load_metric = staticmethod(_body_reads._emit_lazy_load_metric)
    read_scoped_skill = _body_reads.read_scoped_skill
    read_scoped_skill_page = _body_reads.read_scoped_skill_page
    _read_exact_key = _body_reads._read_exact_key

    def _iter(self, project_dir: str | Path | None = None) -> list[tuple[str, Path, str | None]]:
        """Return all ``(name, skill_file, within)`` triples without walking the tree."""
        return _catalog._iter(self, project_dir)

    def _catalog_scope_key(self, project_dir: str | Path | None) -> str:
        """The scope this request reads, as a string the snapshot layer can key on."""
        return _catalog._catalog_scope_key(self, project_dir)

    def catalog_status(self, project_dir: str | Path | None = None) -> str:
        """``"complete"`` or ``"building"`` for the scope *project_dir* selects.

        Contract and rationale: ``skill_runtime.catalog.catalog_status``.
        """
        return _catalog.catalog_status(self, project_dir)

    def _catalog_fingerprint_hint(self, project_dir: str | Path | None) -> dict[str, str]:
        """Stat fingerprints from the walk that produced this scope's list."""
        return _catalog._catalog_fingerprint_hint(self, project_dir)

    def _catalog_scope_id(self, project_key: str) -> str:
        """Stable identity of the ROOT SET a stored snapshot belongs to."""
        return _catalog._catalog_scope_id(self, project_key)

    def _snapshot_admitted_roots(self) -> tuple[str, ...]:
        """Roots an unconfined row read off disk may legitimately name."""
        return _catalog._snapshot_admitted_roots(self)

    def _load_catalog_snapshot(self, project_key: str) -> _StoredCatalog | None:
        """Read this scope's stored enumeration, or ``None`` when there is none."""
        return _catalog._load_catalog_snapshot(self, project_key)

    def _vet_unconfined_path(self, path: Path) -> bool:
        """May *path* be read unconfined? Checks it once unless a walk returned it."""
        return _catalog._vet_unconfined_path(self, path)

    @staticmethod
    def _key_denotes_path(
        key: str, absolute: str, own_roots: tuple[Path, ...], provider_roots: tuple[str, ...]
    ) -> bool:
        """Does *key* name the skill that *absolute* holds?"""
        return _catalog._key_denotes_path(key, absolute, own_roots, provider_roots)

    def _adopt_snapshot(
        self, project_key: str, snapshot: _StoredCatalog, *, generation: int
    ) -> list[tuple[str, Path, str | None]] | None:
        """Serve a stored enumeration for *project_key*, or ``None`` when it is fenced."""
        return _catalog._adopt_snapshot(self, project_key, snapshot, generation=generation)

    def _request_catalog_refresh(self, project_key: str) -> threading.Event | None:
        """Queue one background walk of *project_key*'s roots; join any in flight."""
        return _catalog._request_catalog_refresh(self, project_key)

    def _catalog_worker_loop(self) -> None:
        """Drain queued scopes one at a time until this loader closes."""
        return _catalog._catalog_worker_loop(self)

    def _run_catalog_build(self, project_key: str, generation: int) -> None:
        """Walk *project_key*'s roots off the request path and publish the result."""
        return _catalog._run_catalog_build(self, project_key, generation)

    @staticmethod
    def _catalog_fingerprints_for(
        rows: list[tuple[str, Path, str | None]],
    ) -> dict[str, str]:
        """Stat each unconfined row once, so ``list_skills`` need not stat again."""
        return _catalog._catalog_fingerprints_for(rows)

    def _get_disabled_app_names(self) -> frozenset[str]:
        return _catalog._get_disabled_app_names(self)

    def _iter_visible(
        self, project_dir: str | Path | None = None
    ) -> list[tuple[str, Path, str | None]]:
        """Return all ``(name, skill_file, within)`` pairs, filtering out disabled app skills."""
        return _catalog._iter_visible(self, project_dir)

    # ── Operator review of untrusted skill text: ``kiro_crew.skills_review`` ──
    catalog_project_skills = _review.catalog_project_skills
    _read_pending_meta = _review._read_pending_meta
    list_pending_skills = _review.list_pending_skills
    _redact_text = staticmethod(_review._redact_text)
    _redact_deep = _review._redact_deep
    _redact_file_in_place = _review._redact_file_in_place
    _read_candidate_pinned = _review._read_candidate_pinned
    _pending_scripts_verdict = _review._pending_scripts_verdict
    _pending_scripts_verdict_at = _review._pending_scripts_verdict_at
    get_pending_skill = _review.get_pending_skill
    _validate_and_redact_candidate = _review._validate_and_redact_candidate
    _redact_validation_report = _review._redact_validation_report
    preview_pending_update = _review.preview_pending_update
    approve_pending_update_checked = _review.approve_pending_update_checked
    approve_pending_skill_checked = _review.approve_pending_skill_checked
    approve_pending_as_read = _review.approve_pending_as_read

    def _iter_uncached(self, project_key: str | None = None) -> list[tuple[str, Path, str | None]]:
        """Walk the skills dir, extra paths, and an already-canonical project root."""
        return _catalog._iter_uncached(self, project_key)

    def _invalidate_iter_cache(self) -> None:
        """Drop cached skill state so a just-written mutation is visible now."""
        return _catalog._invalidate_iter_cache(self)

    def _readable_frontmatter(
        self,
        path: Path,
        *,
        within: str | None,
        mtime: float | None = None,
        canonical_root: str | None = None,
    ) -> dict[str, str] | None:
        """Frontmatter for a READER, or ``None`` when the row must be dropped.

        The reader-side counterpart of :meth:`_cached_frontmatter`, whose
        failures propagate for the writers' sake. Contract and rationale:
        ``skill_runtime.listing._readable_frontmatter``.
        """
        return _listing._readable_frontmatter(
            self,
            path,
            within=within,
            mtime=mtime,
            canonical_root=canonical_root,
        )

    def list_skills(
        self,
        project_dir: str | Path | None = None,
        *,
        _entries: list[_ScopedSkillEntry] | None = None,
    ) -> list[dict]:
        """Return per-skill metadata for the dashboard's Skills page.

        Blocking filesystem and SQLite work: async callers must offload it. Contract and
        rationale: ``skill_runtime.listing.list_skills``.
        """
        return _listing.list_skills(self, project_dir, _entries=_entries)

    def _owning_app(self, name: str, skill_file: Path, within: str | None = None) -> str | None:
        """The app whose bundle this skill came from, or ``None``."""
        return _catalog._owning_app(self, name, skill_file, within)

    def _owned_hint(self, skill_file: Path) -> bool:
        """Whether *skill_file* sits under the directory Kiro Crew owns."""
        return _listing._owned_hint(self, skill_file)

    def _served_key_by_realpath(self) -> dict[str, str]:
        """Map each served skill file's realpath to its canonical served key."""
        return _read_credit._served_key_by_realpath(self)

    def resolve_tool_read_keys(
        self,
        tool_name: str = "",
        raw_params: dict | None = None,
        command: str | None = None,
    ) -> list[str]:
        """Served skill keys whose body a tool call is about to deliver.

        Filesystem-bound: callers keep it off the event loop. Contract and rationale:
        ``skill_runtime.read_credit.resolve_tool_read_keys``.
        """
        return _read_credit.resolve_tool_read_keys(self, tool_name, raw_params, command)

    def credit_skill_reads(self, keys: list[str]) -> None:
        """Record a delivery for each key in *keys*. Best-effort, never raises.

        Contract and rationale: ``skill_runtime.read_credit.credit_skill_reads``.
        """
        return _read_credit.credit_skill_reads(self, keys)

    def resolve_ledger_aliases(self) -> dict[str, list[str]]:
        """Map served skill keys to ledger keys that resolve to the same file.

        Contract and rationale: ``skill_runtime.read_credit.resolve_ledger_aliases``.
        """
        return _read_credit.resolve_ledger_aliases(self)

    def _usage_fields(self, key: str) -> dict[str, int | float | None]:
        """The listing's ``deliveries`` and ``last_used_at`` for *key*."""
        return _listing._usage_fields(self, key)

    @staticmethod
    def _safe_name(name: str) -> bool:
        """Return True if skill name is safe (no traversal, rooted, or dot-only).

        A rooted name must be rejected because ``Path.__truediv__`` discards
        the base directory when the joined segment is absolute, so
        ``self._dir / name`` would resolve outside the skills root. Both
        flavours are checked: POSIX-absolute (``/etc/x``) and Windows
        rooted/drive-qualified in the forward-slash spelling (``C:/x``,
        ``C:x``, ``//server/share/x``) — the backslash spelling is already
        caught by the ``"\\\\"`` rule. Dot-only spellings (``.``, ``./``)
        must also be rejected: pathlib drops ``.`` components on join, so
        ``self._dir / "."`` collapses to the skills root itself and a delete
        would remove every installed skill. ``PurePosixPath(name).parts`` is
        empty exactly for those spellings.
        """
        return (
            bool(name)
            and ".." not in name
            and "\\" not in name
            and bool(PurePosixPath(name).parts)
            and not PurePosixPath(name).is_absolute()
            and not PureWindowsPath(name).is_absolute()
            and not PureWindowsPath(name).drive
        )

    def create_skill(self, name: str, content: str) -> bool:
        """Create a new skill directory with SKILL.md.  Returns True on success.

        Contract and rationale: ``skill_runtime.authoring.create_skill``.
        """
        return _authoring.create_skill(self, name, content)

    def _create_skill_pinned(
        self, name: str, content: str, skill_dir: Path, parent_fd: int
    ) -> bool:
        """Create *skill_dir* and its SKILL.md under *parent_fd*, or leave nothing behind."""
        return _authoring._create_skill_pinned(self, name, content, skill_dir, parent_fd)

    def update_skill(self, name: str, content: str) -> bool:
        """Overwrite an existing skill's SKILL.md.  Returns True if found.

        Contract and rationale: ``skill_runtime.authoring.update_skill``.
        """
        return _authoring.update_skill(self, name, content)

    @staticmethod
    def _write_skill_md(skill_file: Path, content: str, *, dir_fd: int | None) -> bool:
        """Atomically replace *skill_file*, carrying its access-control xattrs."""
        return _authoring._write_skill_md(skill_file, content, dir_fd=dir_fd)

    def delete_skill(self, name: str) -> bool:
        """Delete a skill directory.  Returns True if found and removed."""
        if not self._safe_name(name):
            return False
        skill_dir = self._dir / name
        if not skill_dir.is_dir():
            return False
        if _DIR_FD_SUPPORTED:
            # A recursive descriptor-relative delete is out of proportion for a
            # skill dir, so the residual guarded here is narrower: pin the parent,
            # answer "is this name a real directory?" from a descriptor-relative
            # lstat, and only then rmtree. The is_dir() above FOLLOWS a link, so a
            # symlinked skill dir reaches this point; shutil.rmtree then refuses it
            # with an OSError the caller would surface as a 500 instead of the
            # not-found the by-name floor gives. A directory swapped for a link
            # after this check is the remaining window -- recorded, and the by-name
            # floor below carries the same posture.
            try:
                parent_fd = pinned_fs.open_dir_pinned(skill_dir.parent, what="skill directory")
            except pinned_fs.PinnedPathRefusal:
                return False
            except OSError:
                return False
            try:
                st = pinned_fs.stat_at(parent_fd, skill_dir.name)
                if st is None or not stat.S_ISDIR(st.st_mode):
                    return False
            finally:
                os.close(parent_fd)
        elif is_link_or_junction(skill_dir):
            return False
        shutil.rmtree(skill_dir)
        self._invalidate_iter_cache()  # so the removal is reflected in list_skills() now
        logger.info("Deleted skill: %s", name)
        return True

    # ── Auto skill creation ──

    def is_auto_generated(self, name: str) -> bool:
        """Return True if *name* refers to a skill in the auto namespace.

        Contract and rationale: ``skill_runtime.auto_skills.is_auto_generated``.
        """
        return _auto_skills.is_auto_generated(self, name)

    def find_similar(
        self,
        description: str,
        threshold: float = 0.85,
        *,
        exclude: str = "",
    ) -> str | None:
        """Return the name of an existing skill whose description overlaps with *description*.

        Contract and rationale: ``skill_runtime.auto_skills.find_similar``.
        """
        return _auto_skills.find_similar(self, description, threshold, exclude=exclude)

    def create_auto_skill(
        self,
        slug: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
        refusal: ClaimRefusal | None = None,
    ) -> str | None:
        """Write a new auto-generated skill under ``auto/<slug>/SKILL.md``.

        The caller passes already-redacted content. ``None`` is a refusal; a
        ``ClaimRefusal`` says whether it was the lock. Contract and rationale:
        ``skill_runtime.auto_skills.create_auto_skill``.
        """
        return _auto_skills.create_auto_skill(
            self,
            slug,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
            refusal=refusal,
        )

    def update_auto_skill(
        self,
        name: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
    ) -> bool:
        """Update an existing auto-generated skill with a refined procedure.

        The caller passes already-redacted content. Contract and rationale:
        ``skill_runtime.auto_skills.update_auto_skill``.
        """
        return _auto_skills.update_auto_skill(
            self,
            name,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
        )

    def list_auto_skills(self) -> list[dict]:
        """Return metadata dicts for all skills under the auto namespace.

        Contract and rationale: ``skill_runtime.auto_skills.list_auto_skills``.
        """
        return _auto_skills.list_auto_skills(self)

    # ── Auto skill lifecycle: pin / archive / restore / eviction ──

    @staticmethod
    def _cron_referenced_skills() -> set[str]:
        """Skill keys referenced by any cron job (best-effort, never raises)."""
        return _auto_skills._cron_referenced_skills()

    def _auto_created_ts(self, meta: dict) -> float:
        """Parse ``created_at`` frontmatter to a unix timestamp, else 0.0."""
        return _auto_skills._auto_created_ts(self, meta)

    def _auto_activity(self, key: str, path_str: str, meta: dict) -> tuple[int, float]:
        """Return ``(hits, anchor_ts)`` for an auto-skill."""
        return _auto_skills._auto_activity(self, key, path_str, meta)

    def set_pinned(self, name: str, pinned: bool) -> bool:
        """Pin/unpin an auto-skill (exempt from lifecycle eviction).

        Contract and rationale: ``skill_runtime.authoring.set_pinned``.
        """
        return _authoring.set_pinned(self, name, pinned)

    def set_inject_on_trigger(self, name: str, inject: bool) -> bool:
        """Opt a skill in or out of full-body injection on a trigger match.

        Contract and rationale: ``skill_runtime.authoring.set_inject_on_trigger``.
        """
        return _authoring.set_inject_on_trigger(self, name, inject)

    def _archive_root(self) -> Path:
        return _auto_skills._archive_root(self)

    @staticmethod
    def _is_pending_slug_safe(slug: str) -> bool:
        """Strict guard for a single-segment auto-skill slug."""
        return _auto_skills._is_pending_slug_safe(slug)

    def archive_auto_skill(self, name: str) -> bool:
        """Move an auto-skill into the archive (recoverable, never deleted).

        Contract and rationale: ``skill_runtime.auto_skills.archive_auto_skill``.
        """
        return _auto_skills.archive_auto_skill(self, name)

    def restore_auto_skill(self, slug: str) -> str | None:
        """Restore an archived auto-skill back to ``auto/<slug>``.

        ``None`` is a refusal: not found, a live name clash, or the claim lock. Contract
        and rationale: ``skill_runtime.auto_skills.restore_auto_skill``.
        """
        return _auto_skills.restore_auto_skill(self, slug)

    def list_archived_auto_skills(self) -> list[dict]:
        """Return ``{slug, path}`` for every archived auto-skill.

        Contract and rationale: ``skill_runtime.auto_skills.list_archived_auto_skills``.
        """
        return _auto_skills.list_archived_auto_skills(self)

    def run_skill_lifecycle(
        self,
        *,
        max_auto_skills: int,
        stale_after_days: int,
        archive_after_days: int,
        cron_referenced: set[str] | None = None,
        exempt: set[str] | None = None,
        now: float | None = None,
    ) -> dict:
        """Age + bound the auto-skill set. Archives (never deletes).

        Contract and rationale: ``skill_runtime.auto_skills.run_skill_lifecycle``.
        """
        return _auto_skills.run_skill_lifecycle(
            self,
            max_auto_skills=max_auto_skills,
            stale_after_days=stale_after_days,
            archive_after_days=archive_after_days,
            cron_referenced=cron_referenced,
            exempt=exempt,
            now=now,
        )

    # ── Auto skill staging: pending-approval queue ──

    def _pending_root(self) -> Path:
        return _auto_skills._pending_root(self)

    @contextmanager
    def _auto_slug_claim_lock(self) -> Iterator[bool]:
        """Hold one exclusive lock across an availability test and its claim."""
        yield from _auto_skills._auto_slug_claim_lock(self)

    def _auto_slug_available(
        self,
        slug: str,
        *,
        claim: Literal["live", "pending-new", "pending-update"] = "live",
    ) -> bool:
        """True when ``slug`` is free for the allocation named by ``claim``."""
        return _auto_skills._auto_slug_available(self, slug, claim=claim)

    def stage_skill_candidate(
        self,
        slug: str,
        *,
        description: str,
        triggers: str,
        procedure_md: str,
        provenance: AutoSkillProvenance,
        scripts: list[dict] | None = None,
        source: str = "consolidation",
        kind: str = "new",
        target: str | None = None,
        base_version: int | None = None,
        refusal: ClaimRefusal | None = None,
        missing_scripts: list[str] | None = None,
        rejected_scripts: dict[str, list[str]] | None = None,
    ) -> str | None:
        """Write a skill candidate to the pending queue (not live).

        The caller passes already-redacted content. ``None`` means nothing was staged; a
        ``ClaimRefusal`` says whether it was the lock. Contract and rationale:
        ``skill_runtime.auto_skills.stage_skill_candidate``.
        """
        return _auto_skills.stage_skill_candidate(
            self,
            slug,
            description=description,
            triggers=triggers,
            procedure_md=procedure_md,
            provenance=provenance,
            scripts=scripts,
            source=source,
            kind=kind,
            target=target,
            base_version=base_version,
            refusal=refusal,
            missing_scripts=missing_scripts,
            rejected_scripts=rejected_scripts,
        )

    # ── Pending candidate trees: ``kiro_crew.skills_candidate_tree`` ──
    _candidate_has_symlink = staticmethod(_candidate_tree._candidate_has_symlink)
    _collect_scripts = staticmethod(_candidate_tree._collect_scripts)
    _collect_scripts_pinned = _candidate_tree._collect_scripts_pinned
    _candidate_layout_findings_at = _candidate_tree._candidate_layout_findings_at
    _candidate_layout_ok = _candidate_tree._candidate_layout_ok

    _ALLOWED_CANDIDATE_TOP = frozenset({"SKILL.md", ".meta.json", "scripts"})

    def pending_candidate_is_staged(self, slug: str) -> bool:
        """Whether a candidate is still staged at *slug*, for CHOOSING A MESSAGE.

        Contract and rationale:
        ``skill_runtime.auto_skills.pending_candidate_is_staged``.
        """
        return _auto_skills.pending_candidate_is_staged(self, slug)

    @staticmethod
    def _auto_slug_from_name(name: str) -> str:
        """Return the bare slug for an auto-skill *name*, accepting either ``auto/<slug>`` or a bare ``<slug>``. Non-auto namespaces (any name with a slash after stripping the ``auto/`` prefix) fall through and are caught by the ``_is_pending_slug_safe`` guard at the call sites."""
        return _versions._auto_slug_from_name(name)

    def get_auto_skill_version(self, name: str) -> int:
        """Return the ``version`` frontmatter of a live auto-skill (default 1).

        Contract and rationale: ``skill_runtime.versions.get_auto_skill_version``.
        """
        return _versions.get_auto_skill_version(self, name)

    def read_auto_skill_body(self, name: str) -> str | None:
        """Return the full live ``SKILL.md`` text for an auto-skill, or ``None``.

        Contract and rationale: ``skill_runtime.versions.read_auto_skill_body``.
        """
        return _versions.read_auto_skill_body(self, name)

    @staticmethod
    def _rewrite_update_frontmatter(
        candidate_content: str,
        *,
        target_name: str,
        created_at: str,
        version: int,
        pinned: bool = False,
        pointer_only: bool = False,
    ) -> str:
        """Rebuild an update candidate's body as the new live SKILL.md."""
        return _versions._rewrite_update_frontmatter(
            candidate_content,
            target_name=target_name,
            created_at=created_at,
            version=version,
            pinned=pinned,
            pointer_only=pointer_only,
        )

    def _versions_root(self, target_slug: str) -> Path:
        return _versions._versions_root(self, target_slug)

    def _prune_versions(self, versions_dir: Path) -> None:
        """Keep only the newest ``MAX_SKILL_VERSIONS`` ``v<N>-SKILL.md`` snapshots in *versions_dir*, deleting the lowest-numbered excess."""
        return _versions._prune_versions(self, versions_dir)

    def _resolve_snapshot_version(self, versions_dir: Path, fm_version: int) -> int:
        """Return the version number to snapshot the CURRENT live body under."""
        return _versions._resolve_snapshot_version(self, versions_dir, fm_version)

    def dismiss_pending_skill(self, slug: str) -> bool:
        """Delete a pending candidate. Returns True if it existed.

        Contract and rationale: ``skill_runtime.auto_skills.dismiss_pending_skill``.
        """
        return _auto_skills.dismiss_pending_skill(self, slug)

    def dismiss_all_pending(self) -> int:
        """Delete all pending candidates. Returns count dismissed.

        Contract and rationale: ``skill_runtime.auto_skills.dismiss_all_pending``.
        """
        return _auto_skills.dismiss_all_pending(self)

    def dismiss_pending_slugs(self, slugs: list[str]) -> int:
        """Delete only the specified pending candidates. Returns count dismissed.

        Contract and rationale: ``skill_runtime.auto_skills.dismiss_pending_slugs``.
        """
        return _auto_skills.dismiss_pending_slugs(self, slugs)

    def prune_pending(self, ttl_days: int, *, now: float | None = None) -> int:
        """Remove pending candidates older than ``ttl_days``. Returns count pruned.

        Contract and rationale: ``skill_runtime.auto_skills.prune_pending``.
        """
        return _auto_skills.prune_pending(self, ttl_days, now=now)

    # ── Skill selection: ``kiro_crew.skills_selection`` ──
    _max_triggered_now = _selection._max_triggered_now
    _repo_scope_satisfied = staticmethod(_selection._repo_scope_satisfied)
    get_always_skills = _selection.get_always_skills
    get_triggered_skills = _selection.get_triggered_skills
    _is_user_authored = _selection._is_user_authored
    _user_first = _selection._user_first
    scoped_skills = _selection.scoped_skills
    # ── Packaged-skill sync: ``kiro_crew.skills_builtin_sync`` ──
    sync_builtins = _builtin_sync.sync_builtins

    def split_triggered(
        self, names: list[str], project_dir: str | Path | None = None
    ) -> tuple[list[str], list[str]]:
        """Split matched *names* into (inject-body, pointer-only), order preserved.

        Contract and rationale: ``skill_runtime.delivery.split_triggered``.
        """
        return _delivery.split_triggered(self, names, project_dir)

    def confined_triggered(
        self, names: list[str], project_dir: str | Path | None = None
    ) -> set[str]:
        """Return the subset of *names* that are confined project skills.

        Contract and rationale: ``skill_runtime.delivery.confined_triggered``.
        """
        return _delivery.confined_triggered(self, names, project_dir)

    def trigger_hint(self, names: list[str], project_dir: str | Path | None = None) -> str:
        """Return a pointer block naming *names* and where to read each one.

        Contract and rationale: ``skill_runtime.delivery.trigger_hint``.
        """
        return _delivery.trigger_hint(self, names, project_dir)

    def _resolve_path(self, name: str, project_dir: str | Path | None = None) -> Path | None:
        """Return the ``SKILL.md`` path for an enumerated skill *name*."""
        return _catalog._resolve_path(self, name, project_dir)

    def _resolve_path_and_root(
        self, name: str, project_dir: str | Path | None = None
    ) -> tuple[Path, str | None] | None:
        """The enumerated path for *name* PLUS the root it is confined to."""
        return _catalog._resolve_path_and_root(self, name, project_dir)

    def get_context(
        self,
        budget: int | None = None,
        only: list[str] | None = None,
        project_dir: str | Path | None = None,
        project_body_budget: int | None = None,
        *,
        discovery_only: bool = False,
        required_parts_out: list[str] | None = None,
    ) -> str:
        """Build a bounded directory over the agent's resolved available set.

        Raises ``SkillContextCapacityError`` rather than trimming an operator's
        required body; a project's ``always: true`` body that cannot be delivered
        is skipped with a warning and an in-prompt notice instead.
        Contract and rationale: ``skill_runtime.delivery.get_context``.
        """
        return _delivery.get_context(
            self,
            budget,
            only,
            project_dir,
            project_body_budget,
            discovery_only=discovery_only,
            required_parts_out=required_parts_out,
        )

    def _legacy_context(
        self,
        all_skills: list[dict],
        restricted: bool = False,
        project_dir: str | Path | None = None,
        project_body_budget: int | None = None,
    ) -> str:
        """Explicit unbudgeted reader, not the default startup path."""
        return _delivery._legacy_context(
            self, all_skills, restricted, project_dir, project_body_budget
        )

    def _append_project_skill_bodies(
        self,
        parts: list[str],
        project_skills: list[dict],
        project_dir: str | Path | None,
        budget: int | None,
        pinned: set[str] | None = None,
    ) -> _delivery.SkippedProjectSkills:
        """Append confined bodies within the section budget; return what was skipped.

        Each skipped key is also discarded from *pinned*, when given, in place.
        """
        return _delivery._append_project_skill_bodies(
            self, parts, project_skills, project_dir, budget, pinned
        )

    def _record_use(self, key: str) -> None:
        """Best-effort usage bump for the lazy-load ranking. Never raises."""
        return _read_credit._record_use(self, key)

    def _recency_boost(self, path_str: str, fingerprint: str = "") -> float:
        """Return the file mtime if the skill is newer than the boost window, else 0.0. Lets a freshly-added, never-used skill rank above stale unused ones (cold-start protection) without flooding the top of the list."""
        return _delivery._recency_boost(self, path_str, fingerprint)

    def _rank_key(self, s: dict) -> tuple[float, float]:
        """Sort key for on-demand skills: (usage_hits, effective_recency). Higher sorts first. Falls back to recency-only if the ledger is absent."""
        return _delivery._rank_key(self, s)

    @staticmethod
    def _short_desc(desc: str, suffix: str = "...") -> str:
        """Collapse whitespace and truncate a description for the summary line."""
        return _delivery._short_desc(desc, suffix)

    def _body_hits(
        self,
        skills: list[dict],
        terms: Iterable[str],
        live_keys: list[str],
        project_dir: str | Path | None,
    ) -> dict[str, int]:
        return _search._body_hits(self, skills, terms, live_keys, project_dir)

    def _body_matches(
        self,
        skills: list[dict],
        terms: Iterable[str],
        live_keys: list[str],
        project_dir: str | Path | None,
    ) -> tuple[dict[str, set[str]], bool]:
        """Refresh once per query, with bounded work and explicit incomplete recall."""
        return _search._body_matches(self, skills, terms, live_keys, project_dir)

    def _scoped_entries(
        self,
        project_dir: str | Path | None,
        only: list[str] | None,
    ) -> list[_ScopedSkillEntry]:
        return _catalog._scoped_entries(self, project_dir, only)

    def _exact_read_while_building(
        self,
        key: str,
        only: list[str] | None,
        project_dir: str | Path | None,
        max_bytes: int,
        refusal_reasons: list[str] | None = None,
    ) -> str | None:
        """Serve a COMPLETE key during an unfinished first walk, or ``None``."""
        return _search._exact_read_while_building(
            self, key, only, project_dir, max_bytes, refusal_reasons=refusal_reasons
        )

    def search_skills(
        self,
        query: str,
        limit: int = 20,
        *,
        project_dir: str | Path | None = None,
        only: list[str] | None = None,
        offset: int = 0,
        browse: bool = False,
    ) -> list[dict]:
        """Rank total query coverage before rarity, metadata preference and usage.

        The matches of :meth:`search_skills_report`, for callers that do not report
        whether the answer may be missing matches.
        """
        return self.search_skills_report(
            query, limit, project_dir=project_dir, only=only, offset=offset, browse=browse
        ).matches

    def search_skills_report(
        self,
        query: str,
        limit: int = 20,
        *,
        project_dir: str | Path | None = None,
        only: list[str] | None = None,
        offset: int = 0,
        browse: bool = False,
    ) -> SkillSearchReport:
        """One search's matches plus whether they may be incomplete.

        Contract and rationale: ``skill_runtime.search.SkillSearchReport`` and
        ``skill_runtime.search.search_skills_report``.
        """
        return _search.search_skills_report(
            self, query, limit, project_dir=project_dir, only=only, offset=offset, browse=browse
        )

    def resolve_dollar_skills(
        self,
        text: str,
        project_dir: str | Path | None = None,
        *,
        only: list[str] | None = None,
    ) -> list[tuple[str, str, str]]:
        """Resolve ``$skillname`` tokens in *text* to loadable skills.

        Contract and rationale: ``skill_runtime.search.resolve_dollar_skills``.
        """
        return _search.resolve_dollar_skills(self, text, project_dir, only=only)

    @staticmethod
    def has_dollar_candidate(text: str) -> bool:
        """True if *text* contains at least one ``$skill``-shaped token.

        Contract and rationale: ``skill_runtime.search.has_dollar_candidate``.
        """
        return _search.has_dollar_candidate(text)

    # ── Private ──

    @staticmethod
    def _parse_frontmatter(path: Path) -> dict[str, str]:
        """Parse YAML frontmatter from a markdown file (simple key: value).

        Only a key at column 0 is a field. An indented ``key: value`` belongs to
        the enclosing block scalar — a description that documents a setting, for
        instance — and reading it as the setting would make the writer and the
        reader disagree: ``set_inject_on_trigger`` deliberately leaves an indented
        occurrence alone (deleting it would rewrite the author's prose), so
        honoring it here would keep the opt-in from ever taking effect. Ignoring
        indented lines also drops the junk keys a prose line like
        ``  Steps: do x`` would otherwise invent.

        A value that is a YAML block-scalar indicator (``>``, ``|``, with an
        optional chomping ``-``/``+``) is resolved from the indented lines that
        follow it: folded (``>``) folds single breaks to spaces while keeping
        blank-line and more-indented structure, literal (``|``) preserves
        newlines. Without this, the stored value would be the indicator
        character itself and the real content — a multi-line ``description``
        used for routing — would be dropped, leaving the skill unroutable.
        That grammar is pinned as ``frontmatter.SKILL_LOADER``.
        """
        content = path.read_text(encoding="utf-8")
        return parse_frontmatter(content, SKILL_LOADER)

    @staticmethod
    def _parse_frontmatter_text(content: str) -> dict[str, str]:
        """Same grammar as :meth:`_parse_frontmatter`, on text already read.

        Split out so the enumerated-skill path can read through the containment
        choke point and still share one grammar. `_parse_frontmatter` keeps its
        Path signature because it has a legitimate non-skill caller (the Agent SOP
        description reader) that is not subject to skill confinement.
        """
        meta = parse_frontmatter(content, SKILL_LOADER)
        body = SkillsLoader.strip_frontmatter(content.lstrip("\ufeff")).lstrip()[:10]
        if re.match(r"<(?:!doctype|html)[\s>]", body, re.IGNORECASE):
            meta["_html:body"] = "true"
        return meta

    @staticmethod
    def strip_frontmatter(content: str) -> str:
        """Remove YAML frontmatter from markdown.

        A fence LOCATOR, not a field parser — deliberately outside
        ``kiro_crew.frontmatter``. Its closer grammar matches
        ``frontmatter._COLUMN0_BLOCK_RE`` — the ``column0_fence`` extraction
        that ``frontmatter.SKILL_LOADER`` binds to the skills surface: the
        closer is the first line after the opener that STARTS with ``---`` —
        trailing text on the closer line is tolerated and consumed, and
        an optional carriage return before each fence newline is tolerated the
        way the parser tolerates one. Anything
        the display parser reads as frontmatter must also be stripped here:
        a stricter closer (a ``---`` must-be-followed-by-newline
        grammar) would let a ``---junk`` or ``--- `` closer parse fields in the UI
        while the whole block leaked to the model. Editing either grammar
        means revisiting the other.
        """
        if content.startswith("---"):
            match = re.match(r"^---\r?\n.*?\r?\n---[^\n]*\n?", content, re.DOTALL)
            if match:
                return content[match.end() :].strip()
        return content


# ── Facade mirror ──
# The ``skills_*`` owner modules hold the ``SkillsLoader`` member bodies the class binds
# and the module-level names re-imported above, which this module keeps binding.
# ``_facade_mirror`` keeps the patch surface whole: a write of a name this module binds
# reaches every owner that binds the same object, so a patch through this module reaches
# every reader of that name. ``surface`` is this module's own namespace plus the two
# pending-queue hooks, which live in ``skills_auto_model`` alone because their setters
# rebind them there; a read of either is served from that owner.
#: The owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS: tuple[str, ...] = (
    "kiro_crew.skills_auto_model",
    "kiro_crew.skills_discovery",
    "kiro_crew.skills_builtin_provenance",
    "kiro_crew.skills_builtin_sync",
    "kiro_crew.skills_body_reads",
    "kiro_crew.skills_selection",
    "kiro_crew.skills_candidate_tree",
    "kiro_crew.skills_review",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(
        _sys.modules[__name__],
        _OWNERS,
        excluded=(),
        surface=frozenset(globals()) | {"_PENDING_CONSUMED_HOOK", "_PENDING_STAGED_HOOK"},
    ).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # the names the mirror serves, for type checkers and IDEs
    from kiro_crew.skills_auto_model import (  # noqa: F401
        _PENDING_CONSUMED_HOOK,
        _PENDING_STAGED_HOOK,
    )
