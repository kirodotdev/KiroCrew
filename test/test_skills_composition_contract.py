"""Composition contract between ``SkillsLoader`` and ``skill_runtime``.

``kiro_crew.skills`` keeps the loader's state, its locks and the code repository
guards pin to ``skills.py`` by path, and delegates the rest of its rules to the
modules under ``kiro_crew.skill_runtime``. What this file pins is what callers of
the loader observe independently of where each rule lives:

* the class keeps every member it had, with the same kind and signature, and the
  module keeps every name it bound, moved names by identity with their owner;
* a patch applied to the facade -- a module seam such as ``validate_file_path`` or
  ``_COLD_CATALOG_WAIT_SECS``, or a class-level method patch -- still reaches the
  moved code that consumes it;
* the runtime modules follow the placement rules that make that true: no
  module-level facade import, no bare seam read, loader calls routed through the
  loader, a sibling's helper reached through its module, one logger, no path
  anchored on their own file;
* the source-keyed guards stay satisfied by construction: no runtime module holds
  a link-screen site, a sensitive-resolved-path call or a redactor call, so the
  registries naming ``skills.py`` still describe the code, and the ``repo_scope``
  normalisation rule is re-applied to the moved read sites;
* the one-slug-space allocator, the descriptor-pinned pending read, the HTML
  body refusal and the usage-ledger alias fold behave as the facade documents them.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import logging
import re
import sys
from pathlib import Path

import pytest
from source_corpus import repo_files_named, repo_root
from test_link_screen_hold_pin import SCREENS
from test_security_posture import _REDACTOR_CALL_RE

import kiro_crew.skills as sk
from kiro_crew import pinned_fs
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.skills import AutoSkillProvenance, ClaimRefusal, SkillsLoader

RUNTIME_PACKAGE = "kiro_crew.skill_runtime"
RUNTIME_DIR = Path(sk.__file__).resolve().with_name("skill_runtime")
FACADE_PATH = Path(sk.__file__).resolve()
FACADE_LOGGER = "kiro_crew.skills"

#: The owners the loader composes. A module added or removed changes the
#: composition, so the set is spelled out rather than globbed.
RUNTIME_MODULES = frozenset(
    {
        "authoring",
        "auto_skills",
        "catalog",
        "delivery",
        "listing",
        "read_credit",
        "search",
        "versions",
    }
)

#: Facade bindings that tests REBIND to change what the loader does, and which
#: moved code consumes. Runtime code reads each through ``kiro_crew.skills`` at
#: call time. ``os`` is rebound too, but only around ``installed_skill_currency``,
#: which stays in the facade; runtime code reads ``os`` directly.
FACADE_SEAMS = frozenset(
    {
        "PINNED_SKILL_BODIES_CAP",
        "_BUILTIN_SKILLS_DIR",
        "_COLD_CATALOG_WAIT_SECS",
        "_CRON_SOURCE_MAX_BYTES",
        "_DIR_FD_SUPPORTED",
        "_FINGERPRINT_MAX_BYTES",
        "_FINGERPRINT_MAX_ENTRIES",
        "_MAX_DOLLAR_SKILLS",
        "_PENDING_SCRIPT_MAX_ENTRIES",
        "_build_auto_skill_content",
        "_emit_pending_consumed",
        "_emit_pending_staged",
        "_ensure_builtin_skills",
        "_iter_skill_files",
        "_open_project_dir_chain",
        "_project_skills_dir",
        "_trusted_skill_roots",
        "atomic_write",
        "config_dir",
        "deployed_cron_script_sources",
        "file_lock",
        "get_recorder",
        "is_link_or_junction",
        "is_sensitive_path",
        "is_sensitive_resolved_path",
        "open_access_control_source",
        "project_scope_satisfied",
        "safe_read_file",
        "safe_read_file_bytes_nolink",
        "sel",
        "skills_dir",
        "validate_file_path",
        "validate_scripts",
    }
)

# ── The frozen surface ────────────────────────────────────────────────────────

_LOADER_MEMBERS = {
    "attr": """
        _ALLOWED_CANDIDATE_TOP
    """,
    "method": """
        __init__ _adopt_extra_paths _adopt_snapshot
        _append_project_skill_bodies _archive_root _audit_project_skill_enforcement
        _auto_activity _auto_created_ts _auto_slug_available _auto_slug_claim_lock
        _body_hits _body_matches _cached_frontmatter _candidate_layout_findings_at
        _candidate_layout_ok _catalog_fingerprint_hint _catalog_scope_id
        _catalog_scope_key _catalog_worker_loop _collect_scripts_pinned
        _confined_frontmatter_and_size _create_skill_pinned
        _exact_read_while_building _get_disabled_app_names _invalidate_iter_cache _is_user_authored
        _iter _iter_uncached _iter_visible _legacy_context _load_catalog_snapshot
        _max_triggered_now _on_config_change _owned_hint _owning_app _pending_root
        _pending_scripts_verdict _pending_scripts_verdict_at _prune_versions _rank_key
        _read_candidate_pinned _read_enumerated_skill_bytes _read_exact_key
        _read_global_skill_text
        _read_pending_meta _readable_frontmatter _recency_boost _record_use _redact_deep
        _redact_file_in_place
        _redact_validation_report _request_catalog_refresh _resolve_path
        _resolve_path_and_root _resolve_snapshot_version _run_catalog_build
        _scoped_entries _served_key_by_realpath _snapshot_admitted_roots
        _trusted_project_key _usage_fields _user_first _validate_and_redact_candidate
        _versions_root _vet_unconfined_path
        approve_pending_skill approve_pending_skill_checked approve_pending_update
        approve_pending_update_checked archive_auto_skill catalog_project_skills
        catalog_status close confined_triggered create_auto_skill create_skill credit_skill_reads
        delete_skill dismiss_all_pending dismiss_pending_skill dismiss_pending_slugs
        find_similar get_always_skills get_auto_skill_version get_context
        get_pending_skill get_triggered_skills is_auto_generated
        list_archived_auto_skills list_auto_skills list_pending_skills list_skills
        load_skill pending_candidate_is_staged preview_pending_update prune_pending
        read_auto_skill_body read_scoped_skill read_scoped_skill_page reconfigure
        resolve_dollar_skills
        resolve_ledger_aliases resolve_tool_read_keys restore_auto_skill
        run_skill_lifecycle scoped_skills search_skills search_skills_report
        set_inject_on_trigger set_pinned split_triggered stage_skill_candidate sync_builtins trigger_hint
        update_auto_skill update_skill
    """,
    "static": """
        _auto_slug_from_name _candidate_has_symlink _catalog_fingerprints_for
        _collect_scripts _cron_referenced_skills _emit_lazy_load_metric
        _is_pending_slug_safe _key_denotes_path _parse_frontmatter
        _parse_frontmatter_text _redact_text _repo_scope_satisfied
        _rewrite_update_frontmatter _safe_name _screen_extra_paths _short_desc
        _write_skill_md has_dollar_candidate strip_frontmatter
    """,
}

_LOADER_SIGNATURES = {
    "_ALLOWED_CANDIDATE_TOP": "['.meta.json', 'SKILL.md', 'scripts']",
    "__init__": "(self, skills_path: 'Path | None' = None, install_builtins: 'bool' = True, config: 'KiroCrewConfig | None' = None)",
    "_adopt_snapshot": "(self, project_key: 'str', snapshot: '_StoredCatalog', *, generation: 'int') -> 'list[tuple[str, Path, str | None]] | None'",
    "_adopt_extra_paths": "(self, resolved_paths: 'list[Path]') -> 'None'",
    "_append_project_skill_bodies": "(self, parts: 'list[str]', project_skills: 'list[dict]', project_dir: 'str | Path | None', budget: 'int | None', pinned: 'set[str] | None' = None) -> '_delivery.SkippedProjectSkills'",
    "_archive_root": "(self) -> 'Path'",
    "_audit_project_skill_enforcement": "(self, project_dir: 'str | Path', key: 'str | None', allowed: 'bool') -> 'None'",
    "_auto_activity": "(self, key: 'str', path_str: 'str', meta: 'dict') -> 'tuple[int, float]'",
    "_auto_created_ts": "(self, meta: 'dict') -> 'float'",
    "_auto_slug_available": "(self, slug: 'str', *, claim: \"Literal['live', 'pending-new', 'pending-update']\" = 'live') -> 'bool'",
    "_auto_slug_claim_lock": "(self) -> 'Iterator[bool]'",
    "_auto_slug_from_name": "(name: 'str') -> 'str'",
    "_body_hits": "(self, skills: 'list[dict]', terms: 'Iterable[str]', live_keys: 'list[str]', project_dir: 'str | Path | None') -> 'dict[str, int]'",
    "_body_matches": "(self, skills: 'list[dict]', terms: 'Iterable[str]', live_keys: 'list[str]', project_dir: 'str | Path | None') -> 'tuple[dict[str, set[str]], bool]'",
    "_cached_frontmatter": "(self, path: 'Path', mtime: 'float | None' = None, *, within: 'str | None', canonical_root: 'str | None' = None, for_write: 'bool' = False) -> 'dict[str, str]'",
    "_candidate_has_symlink": "(pdir: 'Path') -> 'bool'",
    "_candidate_layout_findings_at": "(self, root_fd: 'int') -> 'list[str]'",
    "_candidate_layout_ok": "(self, src: 'Path', name: 'str') -> 'bool'",
    "_catalog_fingerprint_hint": "(self, project_dir: 'str | Path | None') -> 'dict[str, str]'",
    "_catalog_fingerprints_for": "(rows: 'list[tuple[str, Path, str | None]]') -> 'dict[str, str]'",
    "_catalog_scope_id": "(self, project_key: 'str') -> 'str'",
    "_catalog_scope_key": "(self, project_dir: 'str | Path | None') -> 'str'",
    "_catalog_worker_loop": "(self) -> 'None'",
    "_collect_scripts": "(sdir: 'Path') -> 'list[dict]'",
    "_collect_scripts_pinned": "(self, pinned: 'PinnedDirectory', rel: 'tuple[str, ...]', out: 'list[dict]', budget: 'dict[str, int]') -> 'bool'",
    "_confined_frontmatter_and_size": "(self, path: 'Path', within: 'str') -> 'tuple[dict[str, str], int]'",
    "_create_skill_pinned": "(self, name: 'str', content: 'str', skill_dir: 'Path', parent_fd: 'int') -> 'bool'",
    "_cron_referenced_skills": "() -> 'set[str]'",
    "_emit_lazy_load_metric": "(t0: 'float', *, hit: 'bool') -> 'None'",
    "_exact_read_while_building": "(self, key: 'str', only: 'list[str] | None', project_dir: 'str | Path | None', max_bytes: 'int', refusal_reasons: 'list[str] | None' = None) -> 'str | None'",
    "_get_disabled_app_names": "(self) -> 'frozenset[str]'",
    "_invalidate_iter_cache": "(self) -> 'None'",
    "_is_pending_slug_safe": "(slug: 'str') -> 'bool'",
    "_is_user_authored": "(self, s: 'dict') -> 'bool'",
    "_iter": "(self, project_dir: 'str | Path | None' = None) -> 'list[tuple[str, Path, str | None]]'",
    "_iter_uncached": "(self, project_key: 'str | None' = None) -> 'list[tuple[str, Path, str | None]]'",
    "_iter_visible": "(self, project_dir: 'str | Path | None' = None) -> 'list[tuple[str, Path, str | None]]'",
    "_key_denotes_path": "(key: 'str', absolute: 'str', own_roots: 'tuple[Path, ...]', provider_roots: 'tuple[str, ...]') -> 'bool'",
    "_legacy_context": "(self, all_skills: 'list[dict]', restricted: 'bool' = False, project_dir: 'str | Path | None' = None, project_body_budget: 'int | None' = None) -> 'str'",
    "_load_catalog_snapshot": "(self, project_key: 'str') -> '_StoredCatalog | None'",
    "_max_triggered_now": "(self) -> 'int'",
    "_on_config_change": "(self, change: \"'live.ConfigChange'\") -> 'None'",
    "_owned_hint": "(self, skill_file: 'Path') -> 'bool'",
    "_owning_app": "(self, name: 'str', skill_file: 'Path') -> 'str | None'",
    "_parse_frontmatter": "(path: 'Path') -> 'dict[str, str]'",
    "_parse_frontmatter_text": "(content: 'str') -> 'dict[str, str]'",
    "_pending_root": "(self) -> 'Path'",
    "_pending_scripts_verdict": "(self, pdir: 'Path') -> 'tuple[bool, dict] | None'",
    "_pending_scripts_verdict_at": "(self, root_fd: 'int') -> 'tuple[bool, dict] | None'",
    "_prune_versions": "(self, versions_dir: 'Path') -> 'None'",
    "_rank_key": "(self, s: 'dict') -> 'tuple[float, float]'",
    "_read_candidate_pinned": "(self, pdir: 'Path') -> 'tuple[str, dict, list[dict]] | None'",
    "_read_enumerated_skill_bytes": "(self, path: 'Path', within: 'str | None', *, max_bytes: 'int | None' = None, refusal_reasons: 'list[str] | None' = None, canonical_root: 'str | None' = None) -> 'bytes | None'",
    "_read_exact_key": "(self, key: 'str', *, only: 'list[str] | None', project_dir: 'str | Path | None', max_bytes: 'int') -> '_ExactRead'",
    "_read_global_skill_text": "(self, path: 'Path', max_bytes: 'int | None', *, canonical_root: 'str | None' = None, refusal_reasons: 'list[str] | None' = None) -> 'str | None'",
    "_readable_frontmatter": "(self, path: 'Path', *, within: 'str | None', mtime: 'float | None' = None, canonical_root: 'str | None' = None) -> 'dict[str, str] | None'",
    "_read_pending_meta": "(self, slug: 'str') -> 'dict'",
    "_recency_boost": "(self, path_str: 'str', fingerprint: 'str' = '') -> 'float'",
    "_record_use": "(self, key: 'str') -> 'None'",
    "_redact_deep": "(self, obj: 'object') -> 'object'",
    "_redact_file_in_place": "(self, fp: 'Path') -> 'bool'",
    "_redact_text": "(text: 'object') -> 'str'",
    "_redact_validation_report": "(self, report: 'dict') -> 'dict'",
    "_repo_scope_satisfied": "(relpath: 'str', project_dir: 'str | Path | None') -> 'bool'",
    "_request_catalog_refresh": "(self, project_key: 'str') -> 'threading.Event | None'",
    "_resolve_path": "(self, name: 'str', project_dir: 'str | Path | None' = None) -> 'Path | None'",
    "_resolve_path_and_root": "(self, name: 'str', project_dir: 'str | Path | None' = None) -> 'tuple[Path, str | None] | None'",
    "_resolve_snapshot_version": "(self, versions_dir: 'Path', fm_version: 'int') -> 'int'",
    "_rewrite_update_frontmatter": "(candidate_content: 'str', *, target_name: 'str', created_at: 'str', version: 'int', pinned: 'bool' = False, pointer_only: 'bool' = False) -> 'str'",
    "_run_catalog_build": "(self, project_key: 'str', generation: 'int') -> 'None'",
    "_safe_name": "(name: 'str') -> 'bool'",
    "_scoped_entries": "(self, project_dir: 'str | Path | None', only: 'list[str] | None') -> 'list[_ScopedSkillEntry]'",
    "_screen_extra_paths": "(cfg: 'KiroCrewConfig') -> 'list[Path]'",
    "_served_key_by_realpath": "(self) -> 'dict[str, str]'",
    "_short_desc": "(desc: 'str', suffix: 'str' = '...') -> 'str'",
    "_snapshot_admitted_roots": "(self) -> 'tuple[str, ...]'",
    "_trusted_project_key": "(self, project_dir: 'str | Path | None') -> 'str'",
    "_usage_fields": "(self, key: 'str') -> 'dict[str, int | float | None]'",
    "_user_first": "(self, ranked: 'list[dict]') -> 'list[dict]'",
    "_validate_and_redact_candidate": "(self, src: 'Path', name: 'str') -> 'dict[Path, bytes]'",
    "_vet_unconfined_path": "(self, path: 'Path') -> 'bool'",
    "_versions_root": "(self, target_slug: 'str') -> 'Path'",
    "_write_skill_md": "(skill_file: 'Path', content: 'str', *, dir_fd: 'int | None') -> 'bool'",
    "approve_pending_skill": "(self, slug: 'str') -> 'str | None'",
    "approve_pending_skill_checked": "(self, slug: 'str') -> 'str'",
    "approve_pending_update": "(self, slug: 'str') -> 'str | None'",
    "approve_pending_update_checked": "(self, slug: 'str') -> 'str'",
    "archive_auto_skill": "(self, name: 'str') -> 'bool'",
    "catalog_project_skills": "(self, project_dir: 'str | Path') -> 'list[dict]'",
    "catalog_status": "(self, project_dir: 'str | Path | None' = None) -> 'str'",
    "close": "(self) -> 'None'",
    "confined_triggered": "(self, names: 'list[str]', project_dir: 'str | Path | None' = None) -> 'set[str]'",
    "create_auto_skill": "(self, slug: 'str', *, description: 'str', triggers: 'str', procedure_md: 'str', provenance: 'AutoSkillProvenance', refusal: 'ClaimRefusal | None' = None) -> 'str | None'",
    "create_skill": "(self, name: 'str', content: 'str') -> 'bool'",
    "credit_skill_reads": "(self, keys: 'list[str]') -> 'None'",
    "delete_skill": "(self, name: 'str') -> 'bool'",
    "dismiss_all_pending": "(self) -> 'int'",
    "dismiss_pending_skill": "(self, slug: 'str') -> 'bool'",
    "dismiss_pending_slugs": "(self, slugs: 'list[str]') -> 'int'",
    "find_similar": "(self, description: 'str', threshold: 'float' = 0.85, *, exclude: 'str' = '') -> 'str | None'",
    "get_always_skills": "(self, project_dir: 'str | Path | None' = None) -> 'list[str]'",
    "get_auto_skill_version": "(self, name: 'str') -> 'int'",
    "get_context": "(self, budget: 'int | None' = None, only: 'list[str] | None' = None, project_dir: 'str | Path | None' = None, project_body_budget: 'int | None' = None, *, discovery_only: 'bool' = False, required_parts_out: 'list[str] | None' = None) -> 'str'",
    "get_pending_skill": "(self, slug: 'str') -> 'dict | None'",
    "get_triggered_skills": "(self, text: 'str', project_dir: 'str | Path | None' = None, *, select: 'Callable[[], list[str] | None] | None' = None) -> 'list[str]'",
    "has_dollar_candidate": "(text: 'str') -> 'bool'",
    "is_auto_generated": "(self, name: 'str') -> 'bool'",
    "list_archived_auto_skills": "(self) -> 'list[dict]'",
    "list_auto_skills": "(self) -> 'list[dict]'",
    "list_pending_skills": "(self) -> 'list[dict]'",
    "list_skills": "(self, project_dir: 'str | Path | None' = None, *, _entries: 'list[_ScopedSkillEntry] | None' = None) -> 'list[dict]'",
    "load_skill": "(self, name: 'str', project_dir: 'str | Path | None' = None, *, max_bytes: 'int | None' = None, refusal_reasons: 'list[str] | None' = None) -> 'str | None'",
    "pending_candidate_is_staged": "(self, slug: 'str') -> 'bool'",
    "preview_pending_update": "(self, slug: 'str') -> 'dict | None'",
    "prune_pending": "(self, ttl_days: 'int', *, now: 'float | None' = None) -> 'int'",
    "read_auto_skill_body": "(self, name: 'str') -> 'str | None'",
    "read_scoped_skill": "(self, key: 'str', *, only: 'list[str] | None' = None, project_dir: 'str | Path | None' = None, max_bytes: 'int' = 99000) -> 'str | None'",
    "read_scoped_skill_page": "(self, key: 'str', *, only: 'list[str] | None' = None, project_dir: 'str | Path | None' = None, offset: 'int | None' = None, limit: 'int | None' = None, capacity: 'int' = 99000) -> 'SkillBodyPage | SkillReadRefusal'",
    "reconfigure": "(self, cfg: 'KiroCrewConfig') -> 'None'",
    "resolve_dollar_skills": "(self, text: 'str', project_dir: 'str | Path | None' = None, *, only: 'list[str] | None' = None) -> 'list[tuple[str, str, str]]'",
    "resolve_ledger_aliases": "(self) -> 'dict[str, list[str]]'",
    "resolve_tool_read_keys": "(self, tool_name: 'str' = '', raw_params: 'dict | None' = None, command: 'str | None' = None) -> 'list[str]'",
    "restore_auto_skill": "(self, slug: 'str') -> 'str | None'",
    "run_skill_lifecycle": "(self, *, max_auto_skills: 'int', stale_after_days: 'int', archive_after_days: 'int', cron_referenced: 'set[str] | None' = None, exempt: 'set[str] | None' = None, now: 'float | None' = None) -> 'dict'",
    "scoped_skills": "(self, *, project_dir: 'str | Path | None' = None, only: 'list[str] | None' = None) -> 'list[dict]'",
    "search_skills": "(self, query: 'str', limit: 'int' = 20, *, project_dir: 'str | Path | None' = None, only: 'list[str] | None' = None, offset: 'int' = 0, browse: 'bool' = False) -> 'list[dict]'",
    "search_skills_report": "(self, query: 'str', limit: 'int' = 20, *, project_dir: 'str | Path | None' = None, only: 'list[str] | None' = None, offset: 'int' = 0, browse: 'bool' = False) -> 'SkillSearchReport'",
    "set_inject_on_trigger": "(self, name: 'str', inject: 'bool') -> 'bool'",
    "set_pinned": "(self, name: 'str', pinned: 'bool') -> 'bool'",
    "split_triggered": "(self, names: 'list[str]', project_dir: 'str | Path | None' = None) -> 'tuple[list[str], list[str]]'",
    "stage_skill_candidate": "(self, slug: 'str', *, description: 'str', triggers: 'str', procedure_md: 'str', provenance: 'AutoSkillProvenance', scripts: 'list[dict] | None' = None, source: 'str' = 'consolidation', kind: 'str' = 'new', target: 'str | None' = None, base_version: 'int | None' = None, refusal: 'ClaimRefusal | None' = None) -> 'str | None'",
    "strip_frontmatter": "(content: 'str') -> 'str'",
    "sync_builtins": "(self) -> 'None'",
    "trigger_hint": "(self, names: 'list[str]', project_dir: 'str | Path | None' = None) -> 'str'",
    "update_auto_skill": "(self, name: 'str', *, description: 'str', triggers: 'str', procedure_md: 'str', provenance: 'AutoSkillProvenance') -> 'bool'",
    "update_skill": "(self, name: 'str', content: 'str') -> 'bool'",
}

_MODULE_NAMES = """
_GLOB_CHARS _canonical_glob _canonical_prefix _glob_with_prefix _literal_split
_project_prefix _with_canonical_globs
AUTO_ARCHIVE_DIRNAME AUTO_PENDING_DIRNAME AUTO_SKILL_MAX_PROCEDURE_CHARS
AUTO_SKILL_NAMESPACE AUTO_SKILL_SOURCE_VALUE AUTO_SLUG_CLAIM_LOCK_NAME
AUTO_SLUG_CLAIM_LOCK_TIMEOUT_SECS AutoSkillProvenance CRON_SOURCE_DIVERGED
CRON_SOURCE_IN_SYNC CRON_SOURCE_UNVERIFIABLE ClaimRefusal CronScriptSource
InstalledSkillCurrency MAX_SKILL_VERSIONS PINNED_SKILL_BODIES_CAP PROJECT_SKILL_BODY_CAP
PendingApprovalRefused RETIRED_CONDUCTOR_SKILL_SHA256 SKILLS_DIR_NAME
SKILL_INSTALL_BEHIND SKILL_INSTALL_EDITED SKILL_INSTALL_IN_SYNC
SKILL_INSTALL_UNVERIFIABLE SkillContextCapacityError SkillsLoader VERSIONS_DIRNAME
_AUTO_NAME_PATTERN _BUILTIN_SKILLS_DIR _CATALOG_READ_BATCH _CATALOG_READ_WORKERS
_CATALOG_REVALIDATE_AFTER_SECS _COLD_CATALOG_WAIT_SECS _CONTENT_READ_TOOLS
_CRON_SOURCE_MAX_BYTES _DIR_FD_SUPPORTED _DISCOVERY_IN_PROGRESS_NOTICE
_DOLLAR_SKILL_PATTERN _FAMILY_LINE_MAX_LABELS _FINGERPRINT_MAX_BYTES
_FINGERPRINT_MAX_ENTRIES _ITER_CACHE_TTL_SECS _MAPPED_BLOCK_OVERHEAD_BYTES
_MAX_DOLLAR_SKILLS _MIN_TRIGGER_OVERLAP _NEW_SKILL_BOOST_WINDOW_SECS
_PENDING_CONSUMED_HOOK _PENDING_SCRIPT_MAX_DEPTH _PENDING_SCRIPT_MAX_ENTRIES
_PENDING_STAGED_HOOK _PROJECT_DIR_OPEN_FLAGS _PROJECT_SKILL_MAX_DEPTH _PROVENANCE_FORMAT
_PROVENANCE_MARKER _RELOCATED_SKILLS _RETIRED_CONDUCTOR_SKILL_MAX_BYTES
_SHELL_READ_VERBS _SHELL_SEGMENT_RE _SHELL_SKILL_PATH_RE _SHORT_DESC_CHARS _SKILL_FILE
_ScopedSkillEntry _TOOL_READ_PATH_KEYS _VALIDATION_REPORT_MAX_FINDINGS
_VALIDATION_REPORT_MAX_STRING_CHARS _VALIDATION_REPORT_TRUNCATION_KEY _body_term_hits
_build_auto_skill_content _builtin_dir_app_name _claim_dir_for_replacement
_decode_skill_text _dedupe_identical_skills _disabled_app_names _dispose_superseded_slot
_emit_pending_consumed _emit_pending_staged _ensure_builtin_skills _family_line
_finalize_user_backup _fingerprint_mtime_and_size _first_linked_skill_component
_html_skill_refused _iter_skill_files _manifest_is_newer _matches_any
_namespace_groups _open_project_dir_chain _project_skills_dir
_read_for_comparison _record_builtin_provenance _recorded_fingerprint
_remove_ignorable_dir _retire_verified_claim _shell_segments_reading_content
_skill_currency_state _skill_script_index _skill_tree_fingerprint
_tool_read_path_candidates _tree_entries _tree_has_content _tree_newest_mtime
_trees_stat_equal _trusted_skill_roots _verified_unchanged_fingerprint
_walk_confined_skill_fd _walk_confined_skill_tree _warn_html_skill _within_any
_write_provenance_marker deployed_cron_script_sources installed_skill_currency
is_retired_conductor_skill logger remove_retired_conductor_skill
set_pending_consumed_hook set_pending_staged_hook skills_dir
""".split()

_IMPORTED_KIRO_CREW_NAMES = """
FileTooLargeError KiroCrewConfig MAX_SCRIPT_BYTES MIN_TRIGGER_OVERLAP PinnedDirectory
SKILL_LOADER SKILL_SEARCH_INDEX_FILENAME SKILL_USAGE_FILENAME SkillSearchIndex
SkillUsageLedger atomic_write body_fingerprint config_dir ensure_owner_rwx_dirs
file_lock get_recorder hooks_module is_link_or_junction is_sensitive_path
is_sensitive_resolved_path live open_access_control_source parse_frontmatter
pinned_directory pinned_fs pinned_parent_replace_supported project_scope_satisfied
recall_terms redact_credentials redact_exfiltration_urls referenced_skill_names
rmtree_force safe_read_file safe_read_file_bytes_nolink sel skill_trust trigger_score
validate_file_path validate_scripts words_of
""".split()


def _member_row(raw: object) -> tuple[str, str]:
    if isinstance(raw, staticmethod):
        return "static", str(inspect.signature(raw.__func__))
    if isinstance(raw, property):
        return "property", str(inspect.signature(raw.fget))
    if callable(raw):
        return "method", str(inspect.signature(raw))
    if isinstance(raw, (set, frozenset)):
        return "attr", repr(sorted(raw))
    return "attr", repr(raw)


def _runtime_sources() -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(RUNTIME_DIR.glob("*.py"))
        if path.stem != "__init__"
    }


def _runtime_module(stem: str):
    return importlib.import_module(f"{RUNTIME_PACKAGE}.{stem}")


def _load_probe(tmp_path: Path, name: str, source: str):
    """Import *source* as a module from a file under *tmp_path* (never exec)."""
    path = tmp_path / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _typing_only(tree: ast.Module) -> set[int]:
    return {
        id(sub)
        for block in tree.body
        if isinstance(block, ast.If) and ast.unparse(block.test) == "TYPE_CHECKING"
        for sub in ast.walk(block)
    }


def _imported_module(node: ast.ImportFrom) -> str:
    """The absolute module an ``ImportFrom`` names, relative spellings resolved
    against the runtime package the scanned source lives in."""
    if not node.level:
        return node.module or ""
    return RUNTIME_PACKAGE + (f".{node.module}" if node.module else "")


def _sibling_modules(tree: ast.Module) -> dict[str, str]:
    """``local name -> runtime module`` for each owner a module imports as a module."""
    return {
        alias.asname or alias.name: alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and _imported_module(node) == RUNTIME_PACKAGE
        for alias in node.names
        if alias.name in RUNTIME_MODULES
    }


def _sibling_name_imports(source: str) -> list[str]:
    """Every import, at any depth, that binds a runtime owner's function rather
    than the owner module.

    ``from kiro_crew.skill_runtime.listing import f`` copies ``f`` into the
    importer once, so a patch of ``listing.f`` never reaches that caller."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = _imported_module(node)
        if module.startswith(RUNTIME_PACKAGE + "."):
            hits.extend(f"{node.lineno}: {module}.{alias.name}" for alias in node.names)
        elif module == RUNTIME_PACKAGE:
            hits.extend(
                f"{node.lineno}: {module}.{alias.name}"
                for alias in node.names
                if alias.name not in RUNTIME_MODULES
            )
    return hits


class TestSurface:
    def test_the_runtime_package_holds_exactly_the_composed_modules(self) -> None:
        assert set(_runtime_sources()) == RUNTIME_MODULES

    def test_every_loader_member_keeps_its_name_and_kind(self) -> None:
        current: dict[str, list[str]] = {}
        for name, raw in vars(SkillsLoader).items():
            if name.startswith("__") and name != "__init__":
                continue
            current.setdefault(_member_row(raw)[0], []).append(name)
        expected = {kind: sorted(names.split()) for kind, names in _LOADER_MEMBERS.items()}
        assert {kind: sorted(names) for kind, names in current.items()} == expected

    def test_every_loader_member_keeps_its_signature(self) -> None:
        current = {
            name: _member_row(raw)[1]
            for name, raw in vars(SkillsLoader).items()
            if not name.startswith("__") or name == "__init__"
        }
        assert current == _LOADER_SIGNATURES

    def test_no_search_leaves_its_answer_on_the_shared_loader(self, tmp_path) -> None:
        """Whether a search is incomplete travels with that call's result.

        One loader serves every concurrent search, so a flag kept on it would be
        read by whichever caller looked last, not the one that searched.
        """
        assert "search_incomplete" not in getattr(SkillsLoader, "__annotations__", {})
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        try:
            before = set(vars(loader))
            loader.search_skills_report("anything")
            assert set(vars(loader)) == before
            assert not hasattr(loader, "search_incomplete")
        finally:
            loader.close()

    def test_every_module_name_still_resolves_on_the_facade(self) -> None:
        missing = [name for name in _MODULE_NAMES if not hasattr(sk, name)]
        assert missing == []

    def test_every_imported_kiro_crew_name_still_resolves_on_the_facade(self) -> None:
        missing = [name for name in _IMPORTED_KIRO_CREW_NAMES if not hasattr(sk, name)]
        assert missing == []

    def test_a_moved_name_is_its_owner_object_not_a_copy(self) -> None:
        """A re-export is the owner's object, so an identity check and a cache
        (``_builtin_dir_app_name`` is an ``lru_cache``) see one thing, not two."""
        moved: list[str] = []
        for stem in sorted(RUNTIME_MODULES):
            module = _runtime_module(stem)
            for name in _MODULE_NAMES:
                value = vars(module).get(name)
                if value is None or inspect.ismodule(value):
                    continue
                assert getattr(sk, name) is value, (stem, name)
                moved.append(name)
        # Non-vacuous: the helpers that moved with their owners are re-exported.
        assert {"_dedupe_identical_skills", "_family_line", "_matches_any"} <= set(moved)

    def test_a_star_import_still_binds_every_public_name(self, tmp_path: Path) -> None:
        probe = _load_probe(tmp_path, "sk_star_probe", "from kiro_crew.skills import *\n")
        public = [name for name in _MODULE_NAMES if not name.startswith("_") and name != "logger"]
        public += [name for name in _IMPORTED_KIRO_CREW_NAMES if not name.startswith("_")]
        assert [name for name in public if not hasattr(probe, name)] == []

    def test_the_package_ships_with_the_wheel(self) -> None:
        """``packages = find:`` (not ``find_namespace:``) only ships a directory that
        carries an ``__init__.py``; an editable install would import it anyway."""
        import configparser

        config = configparser.ConfigParser()
        config.read(FACADE_PATH.parents[2] / "setup.cfg", encoding="utf-8")
        assert config.get("options", "packages").strip() == "find:"
        assert config.get("options.packages.find", "where").strip() == "src"
        assert (RUNTIME_DIR / "__init__.py").is_file()


#: Every spelling a test uses to reach the facade module: ``import kiro_crew.skills``
#: (aliased or not) and ``from kiro_crew import ..., skills, ...``.
_MENTIONS_THE_FACADE = re.compile(r"kiro_crew\.skills\b|from kiro_crew import [^\n]*\bskills\b")


class TestPlacement:
    """The rules that keep a facade patch effective once the code has moved."""

    def test_the_facade_imports_every_runtime_module_when_it_loads(self) -> None:
        """Owners load with the facade, so every owner exists before a test patches
        the facade, and nothing is imported for the first time inside a patch."""
        tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == RUNTIME_PACKAGE:
                imported.update(alias.name for alias in node.names)
        assert imported == RUNTIME_MODULES
        for stem in RUNTIME_MODULES:
            assert f"{RUNTIME_PACKAGE}.{stem}" in sys.modules

    def test_the_package_init_imports_nothing(self) -> None:
        tree = ast.parse((RUNTIME_DIR / "__init__.py").read_text(encoding="utf-8"))
        assert [
            node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        ] == []

    def test_no_runtime_module_imports_the_facade_at_module_scope(self) -> None:
        for stem, source in _runtime_sources().items():
            for node in ast.parse(source).body:
                if isinstance(node, ast.Import):
                    assert "kiro_crew.skills" not in [alias.name for alias in node.names], stem
                elif isinstance(node, ast.ImportFrom):
                    assert node.module != "kiro_crew.skills", stem
                    assert not (
                        node.module == "kiro_crew"
                        and "skills" in [alias.name for alias in node.names]
                    ), stem

    def test_runtime_modules_import_no_other_kiro_crew_name(self) -> None:
        """Every name the facade imports from another ``kiro_crew`` module was a facade
        binding a caller could patch. Taking those names through ``sk`` at call time,
        rather than importing them again at module scope, keeps each such patch
        effective on the moved code. Sibling owner modules and ``TYPE_CHECKING``
        imports are the only exceptions."""
        for stem, source in _runtime_sources().items():
            tree = ast.parse(source)
            typing_only = _typing_only(tree)
            for node in tree.body:
                if id(node) in typing_only:
                    continue
                if isinstance(node, ast.ImportFrom) and _imported_module(node).startswith(
                    "kiro_crew"
                ):
                    assert _imported_module(node) == RUNTIME_PACKAGE, (stem, node.module)
                elif isinstance(node, ast.Import):
                    assert not [a.name for a in node.names if a.name.startswith("kiro_crew")], stem

    def test_runtime_imports_form_a_dag(self) -> None:
        graph = {
            stem: set(_sibling_modules(ast.parse(source)).values())
            for stem, source in _runtime_sources().items()
        }
        # Non-vacuous: owners do share helpers.
        assert any(graph.values())
        done: set[str] = set()

        def visit(stem: str, path: tuple[str, ...]) -> None:
            assert stem not in path, f"import cycle: {' -> '.join(path + (stem,))}"
            if stem in done:
                return
            for dep in graph[stem]:
                visit(dep, path + (stem,))
            done.add(stem)

        for stem in graph:
            visit(stem, ())

    def test_no_runtime_module_imports_a_function_from_a_sibling(self) -> None:
        """A helper that moved is patched on its owner module, so a sibling owner
        calls it as ``_listing._dedupe_identical_skills(...)``, looked up per call."""
        offenders = {
            stem: hits
            for stem, source in _runtime_sources().items()
            if (hits := _sibling_name_imports(source))
        }
        assert offenders == {}

    @pytest.mark.parametrize(
        "planted",
        [
            "from kiro_crew.skill_runtime.listing import _dedupe_identical_skills\n",
            "from kiro_crew.skill_runtime.listing import _dedupe_identical_skills as dedupe\n",
            "def f():\n    from kiro_crew.skill_runtime.catalog import _matches_any\n",
            "from kiro_crew.skill_runtime import _matches_any\n",
            "from .listing import _fingerprint_mtime_and_size\n",
        ],
    )
    def test_the_sibling_import_scan_catches_a_planted_import(self, planted: str) -> None:
        assert _sibling_name_imports(planted)

    @pytest.mark.parametrize(
        "allowed",
        [
            "from kiro_crew.skill_runtime import listing as _listing\n",
            "from . import catalog\n",
        ],
    )
    def test_the_sibling_import_scan_allows_a_module_import(self, allowed: str) -> None:
        assert _sibling_name_imports(allowed) == []

    def test_runtime_functions_import_the_facade_as_a_module(self) -> None:
        """``from kiro_crew import skills as sk`` and then ``sk.<seam>``: never a
        by-name import of a facade binding, which would read it once, not per call."""
        imports = 0
        for stem, source in _runtime_sources().items():
            tree = ast.parse(source)
            typing_only = _typing_only(tree)
            for node in ast.walk(tree):
                if id(node) in typing_only:
                    continue
                if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
                    imports += "skills" in [alias.name for alias in node.names]
                if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.skills":
                    raise AssertionError(f"{stem}:{node.lineno} imports facade names by name")
        assert imports > 30

    @staticmethod
    def _bare_seam_loads(source: str) -> list[str]:
        """Seam reads inside a function that a patch of the facade would miss.

        A runtime function reads a seam as ``sk.<name>``. Two spellings bypass that:
        a bare global (the module's own binding) and a function-local import that
        binds a seam's name. Module-level code runs once, at import, and is covered
        by the binding check below.
        """
        tree = ast.parse(source)
        annotations: set[int] = set()
        for node in ast.walk(tree):
            for field in ("annotation", "returns"):
                sub = getattr(node, field, None)
                if sub is not None:
                    annotations.update(id(part) for part in ast.walk(sub))
        function_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        nested = {
            id(inner)
            for outer in ast.walk(tree)
            if isinstance(outer, function_types)
            for inner in ast.walk(outer)
            if inner is not outer and isinstance(inner, function_types)
        }
        hits = []
        for function in ast.walk(tree):
            if not isinstance(function, function_types) or id(function) in nested:
                continue
            local = {arg.arg for arg in ast.walk(function) if isinstance(arg, ast.arg)}
            for node in ast.walk(function):
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    local.add(node.id)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        bound = (alias.asname or alias.name).split(".")[0]
                        imported = alias.name.split(".")[-1]
                        local.add(bound)
                        if {bound, imported} & FACADE_SEAMS:
                            hits.append(f"{node.lineno}: import binds {imported}")
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in FACADE_SEAMS
                    and node.id not in local
                    and id(node) not in annotations
                ):
                    hits.append(f"{node.lineno}: {node.id}")
        return sorted(hits, key=lambda hit: (int(hit.split(":")[0]), hit))

    def test_no_runtime_module_reads_a_seam_as_a_bare_global(self) -> None:
        offenders = {
            stem: hits
            for stem, source in _runtime_sources().items()
            if (hits := self._bare_seam_loads(source))
        }
        assert offenders == {}

    def test_the_seam_scan_catches_a_planted_bare_read(self) -> None:
        planted = (
            "def f(loader, path: 'validate_file_path') -> 'sel':\n"
            "    from kiro_crew import skills as sk\n"
            "    good = sk.validate_file_path(str(path))\n"
            "    return validate_file_path(str(path)), _COLD_CATALOG_WAIT_SECS\n"
            "class Owner:\n"
            "    def lock(self):\n"
            "        from kiro_crew.platform_compat import file_lock\n"
            "        from kiro_crew.hooks import safe_read_file as read\n"
            "        return file_lock, read\n"
        )
        assert [hit.split(": ")[1] for hit in self._bare_seam_loads(planted)] == [
            "_COLD_CATALOG_WAIT_SECS",
            "validate_file_path",
            "import binds file_lock",
            "import binds safe_read_file",
        ]

    @staticmethod
    def _facade_patched_names() -> set[str]:
        """Names any test rebinds on ``kiro_crew.skills`` itself."""
        tests = repo_root() / "test"
        found: set[str] = set()
        for path in repo_files_named(".py"):
            if not path.is_relative_to(tests) or not path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not _MENTIONS_THE_FACADE.search(text):
                continue
            tree = ast.parse(text)
            aliases = {"kiro_crew.skills"}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
                    aliases |= {a.asname or a.name for a in node.names if a.name == "skills"}
                elif isinstance(node, ast.Import):
                    aliases |= {
                        a.asname for a in node.names if a.name == "kiro_crew.skills" and a.asname
                    }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or len(node.args) < 2:
                    continue
                target, name = node.args[0], node.args[1]
                if (
                    ast.unparse(target) in aliases
                    and isinstance(name, ast.Constant)
                    and isinstance(name.value, str)
                    and ast.unparse(node.func).endswith(("setattr", "patch.object"))
                ):
                    found.add(name.value)
            found |= set(re.findall(r"""["']kiro_crew\.skills\.(\w+)["']""", text))
        return found - {"SkillsLoader"}

    def test_no_runtime_module_binds_a_name_tests_rebind_on_the_facade(self) -> None:
        """The contract the seam list stands for, derived from the tests themselves:
        a runtime module that defined or imported a rebound name would keep using
        its own binding, and the patch would silently stop applying there."""
        patched = self._facade_patched_names()
        # Non-vacuous: the scan sees the seams the loader's own tests rebind.
        assert {"validate_file_path", "_COLD_CATALOG_WAIT_SECS", "file_lock"} <= patched
        for stem in RUNTIME_MODULES:
            bound = {
                name
                for name, value in vars(_runtime_module(stem)).items()
                if not inspect.ismodule(value)
            }
            assert bound & patched == set(), stem

    @staticmethod
    def _module_seam_imports(source: str) -> list[str]:
        """Module-level imports that bind a seam under any name, alias included."""
        hits = []
        tree = ast.parse(source)
        typing_only = _typing_only(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and id(node) not in typing_only:
                hits += [
                    f"{node.lineno}: {alias.name}"
                    for alias in node.names
                    if alias.name in FACADE_SEAMS
                ]
        return hits

    def test_no_runtime_module_imports_a_seam_under_any_name(self) -> None:
        offenders = {
            stem: hits
            for stem, source in _runtime_sources().items()
            if (hits := self._module_seam_imports(source))
        }
        assert offenders == {}

    def test_the_seam_import_scan_catches_an_aliased_import(self) -> None:
        planted = (
            "from kiro_crew.hooks import validate_file_path as _vfp\n"
            "from kiro_crew.platform_compat import file_lock, pinned_directory\n"
            "if TYPE_CHECKING:\n"
            "    from kiro_crew.hooks import safe_read_file\n"
        )
        assert self._module_seam_imports(planted) == ["1: validate_file_path", "2: file_lock"]

    def test_every_runtime_logger_is_the_facade_logger(self) -> None:
        """caplog filters on ``kiro_crew.skills``; a ``skill_runtime.*`` logger is not
        its child, so its records would fall outside every such assertion."""
        for stem, source in _runtime_sources().items():
            calls = [
                node
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getLogger"
            ]
            for call in calls:
                assert [ast.literal_eval(arg) for arg in call.args] == [FACADE_LOGGER], stem
            module = _runtime_module(stem)
            if "logger" in vars(module):
                assert module.logger is logging.getLogger(FACADE_LOGGER)

    def test_no_runtime_module_defines_an_async_function(self) -> None:
        for stem, source in _runtime_sources().items():
            assert not [
                n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef)
            ], stem

    def test_no_runtime_module_anchors_a_path_on_its_own_file(self) -> None:
        """``Path(__file__).parent`` in the facade is the ``kiro_crew`` package; in a
        runtime module it is ``skill_runtime``, which would narrow the builtin-app
        root the ownership check compares against. Anchors go through the facade."""
        for stem, source in _runtime_sources().items():
            assert not [
                n
                for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Name) and n.id == "__file__"
            ], stem

    @staticmethod
    def _delegation_map() -> dict[tuple[str, str], str]:
        """``(runtime module, function) -> loader method`` for every delegate."""
        tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
        aliases = {
            alias.asname or alias.name: alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == RUNTIME_PACKAGE
            for alias in node.names
        }
        klass = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SkillsLoader"
        )
        mapping: dict[tuple[str, str], str] = {}
        for method in klass.body:
            if not isinstance(method, ast.FunctionDef):
                continue
            for node in ast.walk(method):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in aliases
                ):
                    mapping.setdefault((aliases[node.func.value.id], node.func.attr), method.name)
        return mapping

    def test_every_delegate_calls_the_function_that_carries_its_name(self) -> None:
        """The link-screen gate builds its resolver set by function NAME, one call
        hop deep. A delegate whose implementation had another name would drop out of
        that set, and a new screen-then-resolve site calling the delegate would go
        unreported; sharing the name keeps the gate's view of the loader unchanged."""
        mismatched = sorted(
            f"{method} -> {stem}.{function}"
            for (stem, function), method in self._delegation_map().items()
            if function != method
        )
        assert mismatched == []

    def test_runtime_code_reaches_a_delegated_method_only_through_the_loader(self) -> None:
        """A class-level patch of a loader method must reach every caller. Runtime
        code therefore calls ``loader.<method>(...)``, never the function that
        implements it, even inside its own module."""
        delegated = self._delegation_map()
        # Non-vacuous: most of the loader's rules are delegated.
        assert len(delegated) > 70
        offenders = []
        for stem, source in _runtime_sources().items():
            tree = ast.parse(source)
            siblings = _sibling_modules(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = None
                if isinstance(node.func, ast.Name):
                    target = (stem, node.func.id)
                elif (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in siblings
                ):
                    target = (siblings[node.func.value.id], node.func.attr)
                if target in delegated:
                    offenders.append(f"{stem}:{node.lineno} calls {target} for {delegated[target]}")
        assert offenders == []


# ── Source-keyed guards stay satisfied by construction ────────────────────────


def _called_names(source: str) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            out.append((node.lineno, name))
    return out


def _repo_scope_violations(source: str) -> list[str]:
    """``test_frontmatter``'s repo_scope normalisation rule, applied to any source.

    Every read that feeds the gate strips the value, and every gate call passes one
    of the normalised spellings. The read-only guard scans ``skills.py`` by path;
    this carries the same rule to the read sites that moved.
    """
    reads = re.findall(r'"repo_scope":\s*meta\.get\("repo_scope"[^\n]*', source)
    reads += re.findall(r'scope\s*=\s*meta\.get\("repo_scope"[^\n]*', source)
    bad = [read for read in reads if ".strip()" not in read]
    calls = re.findall(r"(?:self|loader)\._repo_scope_satisfied\(([^,]+),", source)
    allowed = {"scope", 'meta["repo_scope"]', 'str(s["repo_scope"])', 'str(row["repo_scope"])'}
    bad += [call for call in calls if call.strip() not in allowed]
    return bad


class TestSourceKeyedGuards:
    """Registries that name ``skills.py`` stay true because nothing they count moved."""

    def test_no_runtime_module_screens_a_path_for_links(self) -> None:
        """The link-screen baseline keys its sites to ``skills.py``; a screen in a
        runtime module would be a site that baseline cannot name."""
        for stem, source in _runtime_sources().items():
            assert [name for _line, name in _called_names(source) if name in SCREENS] == [], stem

    def test_the_link_screen_scan_catches_a_planted_screen(self) -> None:
        planted = "def f(p):\n    if sk.is_link_or_junction(p):\n        return shutil.rmtree(p)\n"
        assert [name for _line, name in _called_names(planted) if name in SCREENS] == [
            "is_link_or_junction"
        ]

    def test_no_runtime_module_calls_the_resolved_path_gate(self) -> None:
        """The gate's reviewed caller list counts two calls in ``skills.py``."""
        for stem, source in _runtime_sources().items():
            assert "is_sensitive_resolved_path" not in [name for _l, name in _called_names(source)]

    def test_no_runtime_module_calls_a_redactor(self) -> None:
        """Pending detail and promotion stay in ``skills.py``, the module the
        redaction-sink registry names; a redactor call here would be an unregistered
        sink, and even prose spelling one trips the registry's drift guard."""
        for stem, source in _runtime_sources().items():
            assert _REDACTOR_CALL_RE.search(source) is None, stem

    def test_the_redactor_scan_catches_a_planted_call(self) -> None:
        assert _REDACTOR_CALL_RE.search("x = loader._redact_text(body)\n")

    def test_moved_repo_scope_reads_stay_normalised(self) -> None:
        sources = _runtime_sources()
        assert any('meta.get("repo_scope"' in source for source in sources.values())
        assert {
            stem: bad for stem, source in sources.items() if (bad := _repo_scope_violations(source))
        } == {}

    def test_the_repo_scope_check_catches_a_planted_unstripped_read(self) -> None:
        planted = (
            '    row = {"repo_scope": meta.get("repo_scope", "")}\n'
            '    if loader._repo_scope_satisfied(meta.get("repo_scope"), project_dir):\n'
        )
        assert len(_repo_scope_violations(planted)) == 2


# ── Seams: a facade patch reaches the moved code ──────────────────────────────


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _skill(
    root: Path, name: str, *, description: str = "", extra: str = "", body: str = ""
) -> Path:
    fm = f"---\nname: {name}\ndescription: {description or name + ' skill'}\n{extra}---\n"
    return _write(root / name / "SKILL.md", fm + (body or f"# {name}\n"))


@pytest.fixture
def make_loader(tmp_path):
    made: list[SkillsLoader] = []

    def build(*, extra_paths: list[str] | None = None, max_triggered: int = 3) -> SkillsLoader:
        cfg = KiroCrewConfig(
            skills=SkillsConfig(extra_paths=extra_paths or [], max_triggered=max_triggered)
        )
        loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False, config=cfg)
        made.append(loader)
        return loader

    yield build
    for loader in made:
        loader.close()


def _prov() -> AutoSkillProvenance:
    return AutoSkillProvenance(session_key="s", created_at="2026-01-01T00:00:00+00:00")


class TestSeamsReachTheRuntime:
    def test_path_validation_is_read_through_the_facade(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        extra = tmp_path / "extra"
        _skill(extra, "ext")
        loader = make_loader(extra_paths=[str(extra)])
        assert [name for name, _p, _w in loader._iter_uncached()] == ["ext"]
        monkeypatch.setattr(sk, "validate_file_path", lambda _raw: None)
        assert loader._iter_uncached() == []

    def test_the_tree_walk_is_read_through_the_facade(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        loader = make_loader()
        planted = tmp_path / "skills" / "planted" / "SKILL.md"
        monkeypatch.setattr(
            sk,
            "_iter_skill_files",
            lambda base, **_kw: [("planted", planted)] if base == loader._dir else [],
        )
        assert loader._iter_uncached() == [("planted", planted, None)]

    def test_the_provider_roots_are_read_through_the_facade(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        loader = make_loader()
        monkeypatch.setattr(sk, "_trusted_skill_roots", lambda: ("/srv/provider-root",))
        assert loader._snapshot_admitted_roots()[-1] == "/srv/provider-root"

    def test_the_cold_wait_is_read_through_the_facade(self, make_loader, monkeypatch) -> None:
        """With no budget the cold path must not wait at all; with the real one it
        would wait on the build it queued, so the waits observed tell them apart."""
        loader = make_loader()
        waits: list[float | None] = []

        class _NeverDone:
            def wait(self, timeout=None):
                waits.append(timeout)
                return False

        monkeypatch.setattr(loader, "_request_catalog_refresh", lambda _key: _NeverDone())
        monkeypatch.setattr(sk, "_COLD_CATALOG_WAIT_SECS", 0.0)
        assert loader._iter() == []
        assert loader.catalog_status() == "building"
        assert waits == []

    def test_the_descriptor_capability_is_read_through_the_facade(
        self, make_loader, monkeypatch
    ) -> None:
        loader = make_loader()

        def refuse(*_a, **_kw):
            raise AssertionError("the pinned branch ran")

        monkeypatch.setattr(pinned_fs, "open_dir_pinned", refuse)
        monkeypatch.setattr(sk, "_DIR_FD_SUPPORTED", False)
        assert loader.create_skill("floor", "---\nname: floor\n---\nbody\n") is True
        assert (loader._dir / "floor" / "SKILL.md").read_text(encoding="utf-8").endswith("body\n")

    def test_the_access_control_source_is_read_through_the_facade(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        _skill(tmp_path / "skills", "edit-me")
        loader = make_loader()

        def refuse(*_a, **_kw):
            raise OSError("refused")

        monkeypatch.setattr(sk, "open_access_control_source", refuse)
        assert loader.update_skill("edit-me", "---\nname: edit-me\n---\nnew\n") is False

    def test_the_atomic_writer_is_read_through_the_facade(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        loader = make_loader()
        assert loader.create_auto_skill(
            "pin-me", description="d", triggers="t", procedure_md="p", provenance=_prov()
        )
        writes: list[str] = []
        monkeypatch.setattr(sk, "atomic_write", lambda path, text, **_kw: writes.append(text))
        assert loader.set_pinned("auto/pin-me", True) is True
        assert len(writes) == 1 and "pinned: true" in writes[0]

    def test_the_claim_lock_is_read_through_the_facade(self, make_loader, monkeypatch) -> None:
        loader = make_loader()

        def unavailable(*_a, **_kw):
            raise OSError("held elsewhere")

        monkeypatch.setattr(sk, "file_lock", unavailable)
        refusal = ClaimRefusal()
        staged = loader.stage_skill_candidate(
            "locked-out",
            description="d",
            triggers="t",
            procedure_md="p",
            provenance=_prov(),
            refusal=refusal,
        )
        assert staged is None and refusal.retryable is True
        assert not (loader._pending_root() / "locked-out").exists()

    def test_the_skill_renderer_is_read_through_the_facade(self, make_loader, monkeypatch) -> None:
        loader = make_loader()
        monkeypatch.setattr(
            sk,
            "_build_auto_skill_content",
            lambda **_kw: "---\nname: auto/rendered\n---\nPLANTED\n",
        )
        assert loader.create_auto_skill(
            "rendered", description="d", triggers="t", procedure_md="p", provenance=_prov()
        )
        assert (
            (loader._dir / "auto" / "rendered" / "SKILL.md")
            .read_text(encoding="utf-8")
            .endswith("PLANTED\n")
        )

    def test_the_required_body_capacity_is_read_through_the_facade(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        _skill(tmp_path / "skills", "must", extra="always: true\n", body="# Must\n" + "x" * 400)
        loader = make_loader()
        assert "### Skill: must" in loader.get_context(budget=10_000)
        monkeypatch.setattr(sk, "PINNED_SKILL_BODIES_CAP", 200)
        with pytest.raises(sk.SkillContextCapacityError):
            loader.get_context(budget=10_000)

    def test_the_dollar_cap_is_read_through_the_facade(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        for name in ("one", "two"):
            _skill(tmp_path / "skills", name)
        loader = make_loader()
        assert [key for _t, key, _b in loader.resolve_dollar_skills("use $one and $two")] == [
            "one",
            "two",
        ]
        monkeypatch.setattr(sk, "_MAX_DOLLAR_SKILLS", 1)
        assert [key for _t, key, _b in loader.resolve_dollar_skills("use $one and $two")] == ["one"]

    def test_the_live_body_readers_are_read_through_the_facade(
        self, make_loader, monkeypatch
    ) -> None:
        loader = make_loader()
        assert loader.create_auto_skill(
            "body", description="d", triggers="t", procedure_md="p", provenance=_prov()
        )
        monkeypatch.setattr(sk, "safe_read_file", lambda _path: "PLANTED")
        assert loader.read_auto_skill_body("auto/body") == "PLANTED"
        monkeypatch.setattr(sk, "is_sensitive_path", lambda _path: True)
        assert loader.read_auto_skill_body("auto/body") is None

    def test_the_pending_observers_are_the_facade_hooks(self, make_loader, monkeypatch) -> None:
        loader = make_loader()
        staged: list[dict] = []
        consumed: list[dict] = []
        # Recorded so teardown restores whatever hook was registered before.
        monkeypatch.setattr(sk, "_PENDING_STAGED_HOOK", sk._PENDING_STAGED_HOOK)
        monkeypatch.setattr(sk, "_PENDING_CONSUMED_HOOK", sk._PENDING_CONSUMED_HOOK)
        sk.set_pending_staged_hook(staged.append)
        sk.set_pending_consumed_hook(consumed.append)
        assert loader.stage_skill_candidate(
            "hooked", description="d", triggers="t", procedure_md="p", provenance=_prov()
        )
        assert loader.dismiss_pending_skill("hooked") is True
        assert [row["slug"] for row in staged] == ["hooked"]
        assert [(row["slug"], row["outcome"]) for row in consumed] == [("hooked", "dismissed")]

    def test_a_class_level_enumeration_patch_reaches_every_moved_reader(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        real = _skill(tmp_path / "skills", "seen", description="shared words here")
        loader = make_loader()
        monkeypatch.setattr(
            SkillsLoader, "_iter", lambda self, project_dir=None: [("renamed", real, None)]
        )
        assert [row["key"] for row in loader.list_skills()] == ["renamed"]
        assert loader.find_similar("shared words here") == "renamed"
        assert loader._resolve_path("renamed") == real
        assert loader.split_triggered(["renamed"]) == (["renamed"], [])

    def test_a_class_level_body_read_patch_reaches_the_search_fallback(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        _skill(tmp_path / "skills", "plain")
        loader = make_loader()
        loader._search_index = None
        reads: list[str] = []
        monkeypatch.setattr(
            SkillsLoader,
            "load_skill",
            lambda self, name, *a, **kw: reads.append(name) or "zebra body",
        )
        assert [row["key"] for row in loader.search_skills("zebra")] == ["plain"]
        assert reads == ["plain"]

    def test_a_class_level_metadata_patch_reaches_the_slug_allocator(
        self, make_loader, monkeypatch
    ) -> None:
        loader = make_loader()
        assert loader.stage_skill_candidate(
            "queued", description="d", triggers="t", procedure_md="p", provenance=_prov()
        )
        assert loader._auto_slug_available("queued", claim="live") is False
        monkeypatch.setattr(
            SkillsLoader, "_read_pending_meta", lambda self, slug: {"kind": "update"}
        )
        assert loader._auto_slug_available("queued", claim="live") is True

    def test_a_shared_helper_patched_on_its_owner_reaches_every_sibling_caller(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        """A helper that moved is patched on its owner module; the owners that share
        it look it up there per call, so the patch reaches each of them."""
        _skill(tmp_path / "skills", "alpha", description="alpha things")
        loader = make_loader()
        listing, catalog = _runtime_module("listing"), _runtime_module("catalog")
        calls: list[str] = []

        def spy(name, real):
            def wrapped(*args, **kwargs):
                calls.append(name)
                return real(*args, **kwargs)

            return wrapped

        for module, name in (
            (listing, "_dedupe_identical_skills"),
            (listing, "_fingerprint_mtime_and_size"),
            (catalog, "_matches_any"),
        ):
            monkeypatch.setattr(module, name, spy(name, getattr(module, name)))

        loader.get_context(budget=10_000)
        assert calls.count("_dedupe_identical_skills") == 1
        loader.search_skills("alpha")
        assert calls.count("_dedupe_identical_skills") == 2
        row = next(row for row in loader.list_skills() if row["key"] == "alpha")
        del calls[:]
        loader._recency_boost(row["path"], str(row.get("fingerprint") or "planted"))
        assert calls == ["_fingerprint_mtime_and_size"]
        monkeypatch.setattr(loader, "catalog_status", lambda _project_dir=None: "building")
        assert loader._exact_read_while_building("alpha", ["nothing-matches"], None, 4096) is None
        assert calls[-1] == "_matches_any"

    def test_the_ownership_check_anchors_at_the_package_root(self, make_loader) -> None:
        """A builtin app's skill resolves inside ``kiro_crew/apps/builtins``, and its
        owner is the manifest name of that package directory."""
        builtins_root = FACADE_PATH.parent / "apps" / "builtins"
        manifest = next(
            p for p in sorted(builtins_root.glob("*/app.json")) if (p.parent / "skills").is_dir()
        )
        skill = next(p for p in sorted((manifest.parent / "skills").rglob("SKILL.md")))
        name = json.loads(manifest.read_text(encoding="utf-8"))["name"]
        assert make_loader()._owning_app(skill.parent.name, skill) == name


# ── Characterization of the loader's delta contracts ──────────────────────────


def _stage(loader: SkillsLoader, slug: str, **kw) -> str | None:
    kw.setdefault("description", f"desc {slug}")
    kw.setdefault("triggers", slug)
    kw.setdefault("procedure_md", "## Steps\n\nrun it")
    kw.setdefault("provenance", _prov())
    return loader.stage_skill_candidate(slug, **kw)


class TestOneSlugSpace:
    """The allocator shared by live publish, staging and restore."""

    def test_only_the_lock_makes_a_refusal_retryable(self, make_loader) -> None:
        loader = make_loader()
        oversized = "x" * (sk.AUTO_SKILL_MAX_PROCEDURE_CHARS + 1)
        cases = {
            "create, invalid slug": lambda r: loader.create_auto_skill(
                "Bad Slug",
                description="d",
                triggers="t",
                procedure_md="p",
                provenance=_prov(),
                refusal=r,
            ),
            "create, oversized": lambda r: loader.create_auto_skill(
                "too-long",
                description="d",
                triggers="t",
                procedure_md=oversized,
                provenance=_prov(),
                refusal=r,
            ),
            "stage, oversized": lambda r: _stage(
                loader, "too-long", procedure_md=oversized, refusal=r
            ),
        }
        for label, attempt in cases.items():
            refusal = ClaimRefusal()
            assert attempt(refusal) is None, label
            assert refusal.retryable is False, label
        assert not (loader._dir / sk.AUTO_SKILL_NAMESPACE).exists(), "a refusal made the namespace"

    def test_an_exhausted_sibling_walk_is_not_retryable(self, make_loader) -> None:
        loader = make_loader()
        root = loader._pending_root()
        for name in ("walk", *(f"walk-{n}" for n in range(2, 51))):
            (root / name).mkdir(parents=True)
        refusal = ClaimRefusal()
        assert _stage(loader, "walk", refusal=refusal) is None
        assert refusal.retryable is False

    def test_the_walk_reaches_the_fiftieth_sibling(self, make_loader) -> None:
        loader = make_loader()
        root = loader._pending_root()
        for name in ("edge", *(f"edge-{n}" for n in range(2, 50))):
            (root / name).mkdir(parents=True)
        assert _stage(loader, "edge") == "auto/edge-50"

    def test_the_lock_is_exclusive_bounded_and_outside_the_namespace(
        self, make_loader, monkeypatch
    ) -> None:
        loader = make_loader()
        taken: list[dict] = []
        real = sk.file_lock

        def spy(fd, **kw):
            taken.append(kw)
            assert not (loader._dir / sk.AUTO_SKILL_NAMESPACE).exists(), "the lock made auto/"
            return real(fd, **kw)

        monkeypatch.setattr(sk, "file_lock", spy)
        assert loader.create_auto_skill(
            "locked", description="d", triggers="t", procedure_md="p", provenance=_prov()
        )
        assert taken == [{"exclusive": True, "timeout": sk.AUTO_SLUG_CLAIM_LOCK_TIMEOUT_SECS}]
        assert (loader._dir / sk.AUTO_SLUG_CLAIM_LOCK_NAME).is_file()

    def test_a_lock_refusal_while_staging_leaves_no_candidate(
        self, make_loader, monkeypatch
    ) -> None:
        """The queue root is created before the claim; the candidate directory is not."""
        loader = make_loader()

        def unavailable(*_a, **_kw):
            raise OSError("held elsewhere")

        monkeypatch.setattr(sk, "file_lock", unavailable)
        assert _stage(loader, "held") is None
        assert loader._pending_root().is_dir()
        assert list(loader._pending_root().iterdir()) == []

    def test_live_availability_fails_closed_on_unknown_candidate_metadata(
        self, make_loader
    ) -> None:
        loader = make_loader()
        root = loader._pending_root()
        (root / "kindless").mkdir(parents=True)
        (root / "kindless" / ".meta.json").write_text(
            json.dumps({"slug": "kindless"}), encoding="utf-8"
        )
        (root / "unreadable" / ".meta.json").mkdir(parents=True)
        assert loader._auto_slug_available("kindless", claim="live") is False
        assert loader._auto_slug_available("unreadable", claim="live") is False

    def test_each_claim_consults_only_its_own_halves(self, make_loader) -> None:
        loader = make_loader()
        (loader._dir / sk.AUTO_SKILL_NAMESPACE / "live-only").mkdir(parents=True)
        (loader._pending_root() / "queued").mkdir(parents=True)
        assert loader._auto_slug_available("live-only", claim="live") is False
        assert loader._auto_slug_available("live-only", claim="pending-new") is False
        assert loader._auto_slug_available("live-only", claim="pending-update") is True
        assert loader._auto_slug_available("queued", claim="pending-update") is False
        assert loader._auto_slug_available("free-name", claim="pending-new") is True
        assert loader._auto_slug_available("x", claim="pending-new") is False

    def test_restore_claims_under_the_same_lock(self, make_loader, monkeypatch) -> None:
        loader = make_loader()
        assert loader.create_auto_skill(
            "kept", description="d", triggers="t", procedure_md="p", provenance=_prov()
        )
        assert loader.archive_auto_skill("auto/kept")
        with monkeypatch.context() as patched:
            patched.setattr(
                sk, "file_lock", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("held"))
            )
            assert loader.restore_auto_skill("kept") is None
        assert (loader._archive_root() / "kept").is_dir()
        assert (
            _stage(loader, "kept-update", kind="update", target="auto/kept") == "auto/kept-update"
        )
        (loader._pending_root() / "kept").mkdir()
        (loader._pending_root() / "kept" / ".meta.json").write_text(
            json.dumps({"kind": "update"}), encoding="utf-8"
        )
        assert (
            loader.restore_auto_skill("kept") == "auto/kept"
        ), "an update candidate reserves nothing"
        assert loader.archive_auto_skill("auto/kept")
        (loader._dir / "auto" / "kept").mkdir()
        assert loader.restore_auto_skill("kept") is None, "a live directory already holds the name"


class TestThePinnedPendingRead:
    """``get_pending_skill`` reads one descriptor-pinned tree, or refuses it."""

    @staticmethod
    def _candidate(make_loader):
        loader = make_loader()
        assert _stage(loader, "cand", scripts=[{"filename": "run.py", "content": "print(1)\n"}])
        return loader, loader._pending_root() / "cand"

    def test_the_entry_budget_is_exact(self, make_loader) -> None:
        loader, pdir = self._candidate(make_loader)
        for n in range(sk._PENDING_SCRIPT_MAX_ENTRIES - 1):
            (pdir / "scripts" / f"s{n}.py").write_text("x = 1\n", encoding="utf-8")
        assert loader.get_pending_skill("cand") is not None, "exactly the budget was refused"
        (pdir / "scripts" / "one-more.py").write_text("x = 1\n", encoding="utf-8")
        assert loader.get_pending_skill("cand") is None

    def test_one_level_past_the_depth_cap_refuses(self, make_loader) -> None:
        loader, pdir = self._candidate(make_loader)
        deep = pdir / "scripts"
        for n in range(sk._PENDING_SCRIPT_MAX_DEPTH + 1):
            deep = deep / f"d{n}"
        deep.mkdir(parents=True)
        (deep / "past.py").write_text("print(1)\n", encoding="utf-8")
        assert loader.get_pending_skill("cand") is None

    def test_an_unreadable_metadata_file_refuses_the_candidate(self, make_loader) -> None:
        loader, pdir = self._candidate(make_loader)
        (pdir / ".meta.json").unlink()
        (pdir / ".meta.json").mkdir()
        assert loader.get_pending_skill("cand") is None
        assert loader.pending_candidate_is_staged("cand") is True

    @pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"text"'])
    def test_undecodable_or_non_object_metadata_serves_empty_metadata(
        self, make_loader, content
    ) -> None:
        loader, pdir = self._candidate(make_loader)
        (pdir / ".meta.json").write_text(content, encoding="utf-8")
        detail = loader.get_pending_skill("cand")
        assert detail is not None and detail["meta"] == {} and detail["kind"] == "new"

    def test_an_oversized_body_refuses_the_candidate(self, make_loader) -> None:
        from kiro_crew.skills_script_validator import MAX_SCRIPT_BYTES

        loader, pdir = self._candidate(make_loader)
        (pdir / "SKILL.md").write_text("# pad\n" * (MAX_SCRIPT_BYTES // 6 + 10), encoding="utf-8")
        assert loader.get_pending_skill("cand") is None

    def test_staged_probe_only_names_a_candidate_by_its_safe_slug(self, make_loader) -> None:
        loader, _pdir = self._candidate(make_loader)
        assert loader.pending_candidate_is_staged("cand") is True
        assert loader.pending_candidate_is_staged("gone") is False
        for unsafe in ("", ".", "..", ".hidden", "a/b", "a\\b"):
            assert loader.pending_candidate_is_staged(unsafe) is False, unsafe


class TestTheHtmlBodyRefusal:
    @pytest.mark.parametrize(
        ("body", "refused"),
        [
            ("<html lang='en'><body>x</body></html>", True),
            ("<!doctype>\n<p>x</p>", True),
            ("<!DOCTYPE html>\n<html>", True),
            ("<html-guide> is prose about a tag\n", False),
            ("<htmlfoo>\n", False),
        ],
    )
    def test_the_sentinel_marks_only_a_page_shaped_body(self, body, refused) -> None:
        meta = SkillsLoader._parse_frontmatter_text(f"---\nname: n\n---\n{body}")
        assert bool(meta.get("_html:body")) is refused

    def test_the_warning_cache_is_bounded(self) -> None:
        assert sk._warn_html_skill.cache_info().maxsize == 256

    def test_an_extra_path_page_is_not_loaded(self, tmp_path, make_loader) -> None:
        extra = tmp_path / "extra"
        _skill(extra, "page", body="<!DOCTYPE html>\n<html></html>\n")
        loader = make_loader(extra_paths=[str(extra)])
        assert loader.load_skill("page") is None
        assert "page" not in [row["key"] for row in loader.list_skills()]

    @pytest.mark.skipif(
        not sk.skill_trust.project_skill_traversal_supported(),
        reason="project skills need descriptor-relative no-follow traversal",
    )
    def test_a_project_page_misses_and_records_the_miss(
        self, tmp_path, make_loader, monkeypatch
    ) -> None:
        project = tmp_path / "proj"
        _skill(project / ".kiro" / "skills", "page", body="<html>\n<body>x</body>\n")
        sk.skill_trust.grant_project_trust(project)
        loader = make_loader()
        hits: list[bool] = []

        class _Recorder:
            def histogram(self, *_a, **kw):
                hits.append(kw["attrs"]["hit"])

            def counter(self, *_a, **_kw):
                pass

        monkeypatch.setattr(sk, "get_recorder", lambda: _Recorder())
        assert loader.load_skill("page", project) is None
        assert hits == [False]


class TestTheLedgerAliasFold:
    """``resolve_ledger_aliases`` folds a ledger key that names a served skill's
    file under that skill's served key, and drops a key with no file behind it."""

    class _Ledger:
        def __init__(self, *keys: str) -> None:
            self._keys = keys

        def snapshot(self) -> dict[str, tuple[int, float]]:
            return {key: (1, 0.0) for key in self._keys}

    def test_an_alias_key_folds_under_the_served_key(self, tmp_path, make_loader) -> None:
        root = tmp_path / "skills"
        _skill(root, "real")
        (root / "renamed").symlink_to(root / "real", target_is_directory=True)
        loader = make_loader()
        loader._usage = self._Ledger("real", "renamed")
        assert loader.resolve_ledger_aliases() == {"real": ["renamed"]}

    def test_a_key_with_no_skill_file_is_dropped(self, tmp_path, make_loader) -> None:
        _skill(tmp_path / "skills", "real")
        (tmp_path / "skills" / "ghost").mkdir()
        loader = make_loader()
        loader._usage = self._Ledger("real", "ghost", "never-existed")
        assert loader.resolve_ledger_aliases() == {}

    def test_no_ledger_answers_an_empty_map(self, tmp_path, make_loader) -> None:
        _skill(tmp_path / "skills", "real")
        loader = make_loader()
        loader._usage = None
        assert loader.resolve_ledger_aliases() == {}
        loader._usage = self._Ledger()
        assert loader.resolve_ledger_aliases() == {}
