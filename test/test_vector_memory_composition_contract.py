"""Composition contract between ``VectorMemoryStore`` and ``vector_memory_runtime``.

``kiro_crew.vector_memory`` keeps the store's one connection, lock and pinned write
paths and delegates its retrieval, ranking, repair and parsing rules to the modules
under ``kiro_crew.vector_memory_runtime``. What this file pins is what callers of
the store observe independently of where each rule lives:

* the class keeps every member it had, with the same kind and signature, and the
  module keeps every name it bound, moved names by identity with their owner;
* a patch applied to the facade -- a module seam such as ``np`` or ``_now_iso``, or a
  class-level method patch -- still reaches the moved code that consumes it, the seams
  being the facade names the tests themselves rebind;
* the runtime modules follow the placement rules that make that true: no module-level
  facade import, no bare seam read, no runtime binding of a name tests rebind on the
  facade, store calls routed through the store, one logger;
* a name two modules read has one binding, its owner's: siblings and the facade read
  it through the owner module, so one patch of the owner reaches every caller;
* the read-only source guards that scan ``vector_memory.py`` by name
  (``test_memory_lineage_drift``, ``test_memory_v2_schema``,
  ``test_core_path_redact_before_bound``, ``test_cse_2026_08_05_fixes`` and the
  redaction-sink registry) are re-applied to the runtime modules with their own
  helpers, each with a planted violation proving the re-applied check can fail;
* the optional FAISS accelerator's lifecycle and search tier, driven through a
  numpy-backed stand-in because ``faiss`` is not a declared dependency.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib
import importlib.util
import inspect
import itertools
import json
import logging
import math
import re
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import test_memory_lineage_drift as drift
import test_memory_v2_schema as v2_schema
from source_corpus import repo_files_named, repo_root
from test_core_path_redact_before_bound import _find_slice_inside_redact_call
from test_security_posture import _REDACTOR_CALL_RE

import kiro_crew.vector_memory as vm
from kiro_crew.vector_memory import VectorMemoryStore

RUNTIME_PACKAGE = "kiro_crew.vector_memory_runtime"
RUNTIME_DIR = Path(vm.__file__).resolve().with_name("vector_memory_runtime")
FACADE_PATH = Path(vm.__file__).resolve()
STORE_LOGGER = "kiro_crew.vector_memory"

#: The runtime modules the store composes. A module added or removed changes the
#: composition, so the set is spelled out rather than globbed.
RUNTIME_MODULES = frozenset(
    {
        "embedding",
        "embedding_repair",
        "episodic_search",
        "faiss_index",
        "lessons",
        "migration",
        "recall",
        "retirement",
        "semantic",
        "text_scoring",
    }
)

#: A test module that rebinds a facade name names the facade in one of these spellings.
_MENTIONS_THE_FACADE = re.compile(
    r"kiro_crew\.vector_memory\b|from kiro_crew import [^\n]*\bvector_memory\b"
)


def _facade_patch_targets(source: str) -> set[str]:
    """Names one test module rebinds on ``kiro_crew.vector_memory`` itself.

    A rebinding is ``setattr`` or ``patch.object`` on a facade alias with a literal
    name, or a ``"kiro_crew.vector_memory.<name>"`` string target. An alias is
    ``import kiro_crew.vector_memory as x``, ``from kiro_crew import vector_memory
    as x`` or ``x = importlib.import_module("kiro_crew.vector_memory")``.
    """
    tree = ast.parse(source)
    aliases = {"kiro_crew.vector_memory"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
            aliases |= {a.asname or a.name for a in node.names if a.name == "vector_memory"}
        elif isinstance(node, ast.Import):
            aliases |= {
                a.asname for a in node.names if a.name == "kiro_crew.vector_memory" and a.asname
            }
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func).endswith("import_module")
            and [ast.unparse(arg) for arg in node.value.args] == ["'kiro_crew.vector_memory'"]
        ):
            aliases |= {target.id for target in node.targets if isinstance(target, ast.Name)}
    found = set(re.findall(r"""["']kiro_crew\.vector_memory\.(\w+)["']""", source))
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
    return found


#: The seams the store had when it was split into runtime modules. The derived set
#: always contains them, so it never drops below what the split pinned when a test
#: stops patching one.
_BASE_FACADE_SEAMS = frozenset(
    {
        "np",
        "faiss",
        "_HAS_NUMPY",
        "_HAS_FAISS",
        "datetime",
        "sqlite3",
        "uuid4",
        "_now_iso",
        "config_dir",
        "_contains_injection",
        "bulk_pace_delay",
        "time",
        "_ROW_STEM_CACHE_SIZE",
        "_EPISODIC_SCORING_MAX_BYTES",
        "_SEMANTIC_SCORING_MAX_BYTES",
        "_MAX_AUDITED_REJECTS",
        "_MAX_PROMOTION_REFUSED",
        "_MAX_SQL_PARAMS",
    }
)


@functools.lru_cache(maxsize=1)
def _corpus_facade_patches() -> frozenset[str]:
    """Every name a ``test_*.py`` under ``test/`` or ``src/**/tests`` rebinds on the facade."""
    root = repo_root()
    tests, src = root / "test", root / "src"
    found: set[str] = set()
    for path in repo_files_named(".py"):
        in_tests = path.is_relative_to(tests) or (
            path.is_relative_to(src) and "tests" in path.relative_to(src).parts
        )
        if not (in_tests and path.name.startswith("test_")):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _MENTIONS_THE_FACADE.search(text):
            found |= _facade_patch_targets(text)
    return frozenset(found)


def _facade_seams() -> frozenset[str]:
    """Facade bindings tests REBIND to change what the store does: the base seams plus
    every name the test corpus rebinds on the facade.

    Runtime code reads each through ``kiro_crew.vector_memory`` at call time. Beyond
    the base, the set is derived, not listed, so a test that starts patching another
    facade name puts that name under the placement rules below instead of passing
    silently against an owner's own binding.
    """
    return _BASE_FACADE_SEAMS | _corpus_facade_patches()


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """``local name -> runtime module`` for each owner a module imports as a module."""
    return {
        alias.asname or alias.name: alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == RUNTIME_PACKAGE
        for alias in node.names
        if alias.name in RUNTIME_MODULES
    }


# ── The frozen surface ────────────────────────────────────────────────────────

_STORE_MEMBERS = {
    "attr": """_EPISODIC_SEARCH_COLUMNS _LAST_ACCESSED_CACHE_MAX _LAST_ACCESSED_DEBOUNCE_SECS""",
    "static": """_cosine_sim _extract_value_from_text _fact_label _infer_semantic_key
        _lesson_keywords _matches_tags _parse_preference _stored_similarity_scorer""",
    "property": """algorithm_version db policy_revision space_generation""",
    "method": """__init__ _any_lesson_overlap _append_history _backfill_lesson_embeddings
        _backfill_rows _backfill_semantic_kv_embeddings _bind_lineage
        _build_episodic_scoring_set _check_recall_query _decay_rate_for
        _delete_episodic_row _eligible_rows _embed_bulk_row _embedding_config_guard
        _embedding_current _embedding_token _enforce_episodic_cap _episodic_candidate
        _episodic_relevance_threshold _episodic_scoring_set _fact_identities
        _faiss_content_signature _fetch_all_locked _fetch_one_locked _filter_by_relevance
        _fts5_episodic_search _get_episodic _get_episodic_batch _ineligible_ids
        _init_database _invalidate_episodic_scoring _invalidate_semantic_scoring _log_event
        _matches_allowlist
        _rank_from_scoring_set _rank_lessons _read_editable_history_for_day _read_meta
        _recall_once _reconcile_embedding_space_locked _record_mutation
        _release_store_use_lock _require_facets _restrict_memory_files
        _retire_one_episodic _retire_stale_episodic _retire_stale_episodic_v1
        _search_episodic_v2 _secret_bearing_files _semantic_candidates_v1 _semantic_scoring_set
        _semantic_candidates_v2 _sqlite_data_version _sqlite_vector_search _stamp_facets
        _touch_last_accessed _try_embed _validate_key _vector_commit _write_history
        _write_meta _write_meta_in_transaction _write_semantic append_history
        apply_consolidation backfill_missing_embeddings begin_space_change
        build_faiss_index close consolidation_receipt count_by_facet count_lessons
        delete_episodic delete_lesson delete_semantic embed_episodic embed_lesson
        embed_semantic embed_semantic_retirement embedding_repair_state
        find_contradiction_candidates get_all_semantic get_context_preview
        get_episodic_context get_episodic_list get_events get_lessons get_lessons_context
        get_preferences_context get_rejection_stats get_retired_episodic get_semantic
        get_semantic_context has_any_decodable_lesson has_any_lesson has_episodic_text
        has_pending_embeddings has_stored_embeddings import_memory init
        invalidate_episode_content invalidate_semantic_content list_by_facets load_faiss_index log_reject_event
        memory_stats migrate_from_markdown promote_episodic_patterns
        propose_semantic_delete read_counters read_editable_history read_history_entries
        rebuild_memory_index recall reconcile_embedding_space reconfigure
        recorded_embedding_space recorded_rebuild_generation replace_today_history
        restore_episodic rotate_events save_faiss_index search_episodic search_memory
        search_semantic seed_item_if_absent set_embedding_dim set_semantic
        set_semantic_if_absent turn_lessons validate_semantic with_record_metadata
        write_episodic write_episodic_outcome write_lesson""",
}

#: ``str(inspect.signature(...))`` of every member above as it stood before the
#: store delegated to ``vector_memory_runtime`` (attributes contribute their ``repr``).
_STORE_SIGNATURES = {
    "_EPISODIC_SEARCH_COLUMNS": "'id, conversation_id, text, tags, importance, created_at, last_accessed_at'",
    "_LAST_ACCESSED_CACHE_MAX": "4096",
    "_LAST_ACCESSED_DEBOUNCE_SECS": "60.0",
    "__init__": "(self, db_path: 'Path | None' = None, confidence_threshold: 'float' = 0.8, extra_prefixes: 'list[str] | None' = None, dedup_threshold: 'float' = 0.88, episodic_max: 'int' = 10000, embedding_dim: 'int' = 1024, episodic_limit: 'int' = 8, decay_rates: 'dict[str, float] | None' = None, config: 'object | None' = None)",
    "_any_lesson_overlap": "(self, entries: 'list[tuple[dict, str]]', query_text: 'str') -> 'bool'",
    "_append_history": "(self, entry: 'str') -> 'None'",
    "_backfill_lesson_embeddings": "(self, progress: \"'Callable[[int, int], None] | None'\" = None, *, pace: 'bool' = True, max_rows: 'int | None' = None, should_stop: \"'Callable[[], bool] | None'\" = None) -> 'int'",
    "_backfill_rows": "(self, sql: 'str', *, kind: 'str', identity: 'str', limit: 'int | None') -> 'list[sqlite3.Row]'",
    "_backfill_semantic_kv_embeddings": "(self, progress: \"'Callable[[int, int], None] | None'\" = None, *, pace: 'bool' = True, max_rows: 'int | None' = None, should_stop: \"'Callable[[], bool] | None'\" = None) -> 'int'",
    "_bind_lineage": "(self, lineage: 'str') -> 'None'",
    "_build_episodic_scoring_set": "(self, dim: 'int', version: 'int') -> '_EpisodicScoringSet | None'",
    "_check_recall_query": "(self, query: '_RecallQuery | None') -> 'None'",
    "_cosine_sim": "(a: 'list[float]', b: 'list[float]') -> 'float'",
    "_decay_rate_for": "(self, raw_tags: 'str | list[str] | None') -> 'float'",
    "_delete_episodic_row": "(self, mem_id: 'str') -> 'None'",
    "_eligible_rows": "(self, rows, kind: 'str') -> 'list'",
    "_embed_bulk_row": "(self, text: 'str', *, pace: 'bool') -> \"'list[float] | None'\"",
    "_embedding_config_guard": "(self, vector: 'list[float] | None')",
    "_embedding_current": "(self, vector: 'list[float] | None') -> 'bool'",
    "_embedding_token": "(self) -> 'tuple[str | None, str]'",
    "_enforce_episodic_cap": "(self) -> 'None'",
    "_episodic_candidate": "(self, r: 'sqlite3.Row', cosine_sim: 'float', now: 'datetime') -> 'dict'",
    "_episodic_relevance_threshold": "(self, text: 'str') -> 'float'",
    "_episodic_scoring_set": "(self, dim: 'int') -> '_EpisodicScoringSet | None'",
    "_extract_value_from_text": "(text: 'str') -> 'str'",
    "_fact_identities": "(self) -> 'dict[str, dict]'",
    "_fact_label": "(row: 'dict') -> 'str'",
    "_faiss_content_signature": "(self) -> 'str'",
    "_fetch_all_locked": "(self, sql: 'str', params: 'Sequence[object]' = (), *, scan: '_ScanSurface | None' = None) -> 'list[sqlite3.Row]'",
    "_fetch_one_locked": "(self, sql: 'str', params: 'Sequence[object]' = ()) -> 'sqlite3.Row | None'",
    "_filter_by_relevance": "(self, candidates: 'list[dict]') -> 'list[dict]'",
    "_fts5_episodic_search": "(self, query: 'str', limit: 'int', tag_filter: 'list[str] | None' = None) -> 'list[dict]'",
    "_get_episodic": "(self, mem_id: 'str') -> 'dict | None'",
    "_get_episodic_batch": "(self, mem_ids: 'list[str]') -> 'dict[str, dict]'",
    "_ineligible_ids": "(self, record_ids: 'list[str] | None' = None) -> 'set[str]'",
    "_infer_semantic_key": "(text: 'str') -> 'str | None'",
    "_init_database": "(self) -> 'None'",
    "_invalidate_episodic_scoring": "(self) -> 'None'",
    "_invalidate_semantic_scoring": "(self) -> 'None'",
    "_lesson_keywords": "(text: 'str') -> 'set[str]'",
    "_log_event": "(self, event_type: 'str', memory_type: 'str', key: 'str', old_value: 'str | None', new_value: 'str | None', source: 'str') -> 'None'",
    "_matches_allowlist": "(self, key: 'str') -> 'bool'",
    "_matches_tags": "(mem: 'dict', tag_filter: 'list[str]') -> 'bool'",
    "_parse_preference": "(text: 'str') -> 'tuple[str, str] | None'",
    "_rank_from_scoring_set": "(self, scoring: '_EpisodicScoringSet', q: 'list[float]', limit: 'int', mmr: 'bool', tag_filter: 'list[str] | None', relevance_filter: 'bool', now: 'datetime', blocked: 'set[str] | None' = None) -> 'list[dict]'",
    "_rank_lessons": "(self, entries: 'list[tuple[dict, str]]', query_text: 'str', *, recall_query: '_RecallQuery | None' = None) -> 'list[tuple[dict, str]]'",
    "_read_editable_history_for_day": "(self, day: 'str') -> 'str'",
    "_read_meta": "(self, key: 'str') -> 'str | None'",
    "_recall_once": "(self, query_text: 'str', *, cap: 'int', project_dir: 'str | Path | None', query: '_RecallQuery', keep: 'Callable[[list[dict]], list[dict] | None] | None' = None) -> 'dict'",
    "_reconcile_embedding_space_locked": "(self, signature: 'str', *, clear_when_unknown: 'bool' = False, force: 'bool' = False, rebuild_generation: 'str' = '') -> 'int'",
    "_record_mutation": "(self, kind: 'str', item_id: 'str', before: 'dict | None', source: 'str', *, metadata: 'dict | None' = None, operation: 'str' = 'update') -> 'dict'",
    "_release_store_use_lock": "(self) -> 'None'",
    "_require_facets": "(self) -> 'None'",
    "_restrict_memory_files": "(self) -> 'None'",
    "_retire_one_episodic": "(self, mem_id: 'str', text: 'str', superseded_by: 'str') -> 'None'",
    "_retire_stale_episodic": "(self, key: 'str', old_value: 'str', *, defer_embedding: 'bool' = False, query_embedding: 'list[float] | None' = None, embedding_resolved: 'bool' = False) -> 'None'",
    "_retire_stale_episodic_v1": "(self, key: 'str', old_value: 'str', *, defer_embedding: 'bool' = False, query_embedding: 'list[float] | None' = None, embedding_resolved: 'bool' = False) -> 'None'",
    "_search_episodic_v2": "(self, query_embedding: 'list[float] | None', query_text: 'str', limit: 'int', mmr: 'bool', tag_filter: 'list[str] | None', relevance_filter: 'bool') -> 'list[dict]'",
    "_secret_bearing_files": "(self) -> 'tuple[Path, ...]'",
    "_semantic_candidates_v1": "(self, query_text: 'str', *, recall_query: '_RecallQuery | None' = None) -> 'list[dict]'",
    "_semantic_candidates_v2": "(self, query_text: 'str', *, recall_query: '_RecallQuery | None' = None) -> 'list[dict]'",
    "_semantic_scoring_set": "(self) -> '_SemanticScoringSet | None'",
    "_sqlite_data_version": "(self) -> 'int | None'",
    "_sqlite_vector_search": "(self, query_embedding: 'list[float]', query_text: 'str', limit: 'int', mmr: 'bool' = True, tag_filter: 'list[str] | None' = None, relevance_filter: 'bool' = False) -> 'list[dict]'",
    "_stamp_facets": "(self, item_id: 'str', facets: \"'memory_schema.MemoryFacets | None'\") -> 'None'",
    "_stored_similarity_scorer": "(query_emb: 'list[float] | None') -> 'Callable[[dict], float]'",
    "_touch_last_accessed": "(self, mem_ids: 'list[str]') -> 'None'",
    "_try_embed": "(self, text: 'str', priority: 'int' = 1) -> 'list[float] | None'",
    "_validate_key": "(self, key: 'str') -> 'str | None'",
    "_vector_commit": "(self, vector: 'list[float] | None', *, best_effort: 'bool' = False)",
    "_write_history": "(self, day: 'str', content: 'str') -> 'None'",
    "_write_meta": "(self, key: 'str', value: 'str') -> 'None'",
    "_write_meta_in_transaction": "(self, key: 'str', value: 'str') -> 'None'",
    "_write_semantic": "(self, key: 'str', value_json: 'str', confidence: 'float', source: 'str', *, metadata: 'dict | None' = None, expected_revision: 'int | None' = None, correction: 'record_meta.CorrectionEvidence | None' = None, _consolidation: 'bool' = False, defer_embedding: 'bool' = False, embedding: 'list[float] | None' = None, embedding_resolved: 'bool' = False, embedding_generation: 'int | None' = None, retirement_embedding: 'list[float] | None' = None, retirement_embedding_resolved: 'bool' = False, retirement_value_json: 'str | None' = None) -> 'str | None'",
    "algorithm_version": "(self) -> 'str'",
    "append_history": "(self, entry: 'str') -> 'None'",
    "apply_consolidation": "(self, *, source_id: 'str', session_key: 'str', source_total: 'int', result: 'dict', snapshot: 'dict', messages: 'list[dict]', facets: 'memory_schema.MemoryFacets | None' = None) -> 'dict'",
    "backfill_missing_embeddings": "(self, progress: \"'Callable[[int, int], None] | None'\" = None, *, pace: 'bool' = True, max_rows_per_kind: 'int | None' = None, should_stop: \"'Callable[[], bool] | None'\" = None) -> 'int'",
    "begin_space_change": "(self) -> 'None'",
    "build_faiss_index": "(self) -> 'int'",
    "close": "(self) -> 'None'",
    "consolidation_receipt": "(self, source_id: 'str') -> 'dict | None'",
    "count_by_facet": "(self, group_by: 'str', filters: 'Mapping[str, str] | None' = None, *, kind: 'str' = '') -> 'dict[str, int]'",
    "count_lessons": "(self) -> 'int'",
    "db": "(self) -> 'sqlite3.Connection'",
    "delete_episodic": "(self, mem_id: 'str', source: 'str' = 'user_explicit') -> 'bool'",
    "delete_lesson": "(self, rule_substring: 'str', repo_scope: 'str | None' = None, *, exact: 'bool' = False) -> 'bool'",
    "delete_semantic": "(self, key: 'str', source: 'str', *, expect_value_json: 'str | None' = None, superseded_by: 'str | None' = None, supersede_reason: 'str | None' = None) -> 'bool'",
    "embed_episodic": "(self, text: 'str') -> 'list[float] | None'",
    "embed_lesson": "(self, rule: 'str') -> 'list[float] | None'",
    "embed_semantic": "(self, key: 'str', value: 'object') -> 'list[float] | None'",
    "embed_semantic_retirement": "(self, key: 'str', value_json: 'str') -> 'list[float] | None'",
    "embedding_repair_state": "(self, generation: 'str') -> 'tuple[bool, int]'",
    "find_contradiction_candidates": "(self, rule: 'str', threshold_low: 'float' = 0.4, threshold_high: 'float' = 0.85, rule_emb: 'list[float] | None' = None, repo_scope: 'str | None' = None) -> 'list[dict]'",
    "get_all_semantic": "(self, limit: 'int | None' = None, offset: 'int' = 0, *, q: 'str' = '') -> 'list[dict]'",
    "get_context_preview": "(self, query_text: 'str' = '') -> 'dict'",
    "get_episodic_context": "(self, query_embedding: 'list[float] | None' = None, query_text: 'str' = '', cap: 'int' = 3000) -> 'str'",
    "get_episodic_list": "(self, limit: 'int' = 50, offset: 'int' = 0, tag_filter: 'list[str] | None' = None, *, q: 'str' = '') -> 'list[dict]'",
    "get_events": "(self, limit: 'int' = 50, offset: 'int' = 0) -> 'list[dict]'",
    "get_lessons": "(self, limit: 'int | None' = None, offset: 'int' = 0) -> 'list[dict]'",
    "get_lessons_context": "(self, query_text: 'str' = '', cap: 'int' = 0, project_dir: 'str | Path | None' = None, *, recall_query: '_RecallQuery | None' = None, background: 'bool' = False, hard_cap: 'int' = 0, directive_budget: 'int' = 0, experience_budget: 'int' = 0) -> 'str'",
    "get_preferences_context": "(self, query_text: 'str' = '', cap: 'int' = 0) -> 'str'",
    "get_rejection_stats": "(self) -> 'dict[str, int]'",
    "get_retired_episodic": "(self, limit: 'int' = 50, offset: 'int' = 0) -> 'list[dict]'",
    "get_semantic": "(self, key: 'str') -> 'dict | None'",
    "get_semantic_context": "(self, query_text: 'str' = '', cap: 'int' = 1500, *, facts_only: 'bool' = False) -> 'str'",
    "has_any_decodable_lesson": "(self) -> 'bool'",
    "has_any_lesson": "(self) -> 'bool'",
    "has_episodic_text": "(self, text: 'str') -> 'bool'",
    "has_pending_embeddings": "(self) -> 'bool'",
    "has_stored_embeddings": "(self) -> 'bool'",
    "import_memory": "(self, data: 'dict') -> 'dict[str, int]'",
    "init": "(self) -> 'None'",
    "invalidate_episode_content": "(self) -> 'None'",
    "invalidate_semantic_content": "(self) -> 'None'",
    "list_by_facets": "(self, filters: 'Mapping[str, str] | None' = None, *, kind: 'str' = '', limit: 'int' = 50, offset: 'int' = 0) -> 'list[dict]'",
    "load_faiss_index": "(self) -> 'bool'",
    "log_reject_event": "(self, code: 'SemanticRejectCode', key: 'str', value: 'object', source: 'str', *, value_json: 'str | None' = None) -> 'None'",
    "memory_stats": "(self) -> 'dict'",
    "migrate_from_markdown": "(self) -> 'dict[str, int]'",
    "policy_revision": "(self) -> 'str'",
    "promote_episodic_patterns": "(self, min_count: 'int' = 5, min_sim: 'float' = 0.75) -> 'int'",
    "propose_semantic_delete": "(self, key: 'str', source: 'str') -> 'bool'",
    "read_counters": "(self) -> 'dict[str, int]'",
    "read_editable_history": "(self) -> 'str'",
    "read_history_entries": "(self, *, since: 'str | None' = None, limit: 'int' = 366, max_bytes: 'int' = 8388608) -> 'list[dict]'",
    "rebuild_memory_index": "(self) -> 'int'",
    "recall": "(self, query_text: 'str', *, cap: 'int' = 3000, project_dir: 'str | Path | None' = None, keep: 'Callable[[list[dict]], list[dict] | None] | None' = None) -> 'dict'",
    "reconcile_embedding_space": "(self, signature: 'str', *, clear_when_unknown: 'bool' = False, force: 'bool' = False, rebuild_generation: 'str' = '') -> 'int'",
    "reconfigure": "(self, cfg: 'object') -> 'None'",
    "recorded_embedding_space": "(self) -> 'str | None'",
    "recorded_rebuild_generation": "(self) -> 'str'",
    "replace_today_history": "(self, content: 'str', *, expected_baseline: 'str', validate_current: 'Callable[[str], None]') -> 'bool'",
    "restore_episodic": "(self, mem_id: 'str', source: 'str' = 'user_explicit') -> 'bool'",
    "rotate_events": "(self, max_rows: 'int' = 10000) -> 'int'",
    "save_faiss_index": "(self) -> 'None'",
    "search_episodic": "(self, query_embedding: 'list[float] | None' = None, query_text: 'str' = '', limit: 'int' = 8, mmr: 'bool' = True, tag_filter: 'list[str] | None' = None, relevance_filter: 'bool' = False, *, recall_query: '_RecallQuery | None' = None) -> 'list[dict]'",
    "search_memory": "(self, query: 'str', *, limit: 'int' = 5) -> 'list[dict]'",
    "search_semantic": "(self, prefix: 'str') -> 'list[dict]'",
    "seed_item_if_absent": "(self, item: 'Mapping[str, object]', *, source_store: 'str', source_id: 'str', kind: 'str') -> 'dict'",
    "set_embedding_dim": "(self, dim: 'int') -> 'bool'",
    "set_semantic": "(self, key: 'str', value: 'object', confidence: 'float', source: 'str', *, facets: \"'memory_schema.MemoryFacets | None'\" = None, metadata: 'dict | None' = None, expected_revision: 'int | None' = None, correction: 'record_meta.CorrectionEvidence | None' = None, defer_embedding: 'bool' = False, embedding: 'list[float] | None' = None, embedding_resolved: 'bool' = False, embedding_generation: 'int | None' = None, retirement_embedding: 'list[float] | None' = None, retirement_embedding_resolved: 'bool' = False, retirement_value_json: 'str | None' = None) -> 'tuple[SemanticRejectCode, str] | None'",
    "set_semantic_if_absent": "(self, key: 'str', value: 'object', confidence: 'float', source: 'str', *, facets: \"'memory_schema.MemoryFacets | None'\" = None) -> 'str'",
    "space_generation": "(self) -> 'int'",
    "turn_lessons": "(self, query_text: 'str', *, shown: 'Callable[[str], bool]', project_dir: 'str | Path | None' = None, max_rows: 'int', max_chars: 'int', render_lesson: 'Callable[[str], str] | None' = None) -> 'list[tuple[str, str]]'",
    "validate_semantic": "(self, key: 'str', value: 'object', confidence: 'float', source: 'str', *, value_json: 'str | None' = None) -> 'tuple[SemanticRejectCode, str] | None'",
    "with_record_metadata": "(self, rows: 'list[dict]') -> 'list[dict]'",
    "write_episodic": "(self, text: 'str', embedding: 'list[float] | None' = None, conversation_id: 'str' = '', tags: 'list[str] | None' = None, importance: 'float' = 0.5, source: 'str' = 'consolidation', *, preserve_existing: 'bool' = False, defer_embedding: 'bool' = False, embedding_resolved: 'bool' = False, embedding_generation: 'int | None' = None, facets: \"'memory_schema.MemoryFacets | None'\" = None, metadata: 'dict | None' = None) -> 'bool'",
    "write_episodic_outcome": "(self, text: 'str', embedding: 'list[float] | None' = None, conversation_id: 'str' = '', tags: 'list[str] | None' = None, importance: 'float' = 0.5, source: 'str' = 'consolidation', *, preserve_existing: 'bool' = False, defer_embedding: 'bool' = False, embedding_resolved: 'bool' = False, embedding_generation: 'int | None' = None, facets: \"'memory_schema.MemoryFacets | None'\" = None, metadata: 'dict | None' = None) -> \"'EpisodicWriteOutcome'\"",
    "write_lesson": "(self, rule: 'str', category: 'str' = 'knowledge', negative: 'str | None' = None, source: 'str' = 'user_explicit', rule_emb: 'list[float] | None' = None, rule_emb_generation: 'int | None' = None, repo_scope: 'str | None' = None, *, applies: 'str | None' = None, rule_emb_resolved: 'bool' = False, defer_backfills: 'bool' = False, facets: \"'memory_schema.MemoryFacets | None'\" = None, cites: 'list[dict[str, str]] | None' = None, cited_commit: 'str | None' = None) -> 'LessonWriteResult'",
}

#: Every name ``kiro_crew.vector_memory`` itself bound (definitions, not imports),
#: plus the explicit ``vector_memory_constants`` re-export block.
_MODULE_NAMES = """
EPISODIC_BLOCK_TEXT_CHARS EpisodicWriteOutcome LessonWriteOutcome LessonWriteResult MAX_MEMORY_SEARCH_QUERY
SemanticRejectCode VectorMemoryStore _AUDITABLE_REJECT_CODES _AUDIT_ONCE_REJECT_CODES
_BUILTIN_PREFIXES _DB_FILE _DECAY_DEFAULT_KEY _DECAY_RATE_MAX _DECAY_RATE_MIN
_DEFAULT_CONFIDENCE_THRESHOLD _DEFAULT_DECAY_RATE _DEFAULT_DEDUP_THRESHOLD
_DEFAULT_EPISODIC_LIMIT _DEFAULT_EPISODIC_MAX _DENSE_SCRIPT_RANGES _EMBED_SIG_KEY
_EMPTY_VALUE_JSON _EPISODIC_LONG_TEXT_CHARS _EPISODIC_LONG_TEXT_THRESHOLD
_EPISODIC_RELEVANCE_THRESHOLD _EPISODIC_SCORING_MAX_BYTES _EPISODIC_TEXT_MAX
_EPISODIC_TEXT_MIN _EmbeddingVector _EpisodicScoringSet _FAISS_FILE _FAISS_SAVE_INTERVAL
_HAS_FAISS _HAS_NUMPY _KEY_PATTERN _LESSON_NEGATIVE_SEP _LESSON_WROTE_OUTCOMES
_MAX_AUDITED_REJECTS _MAX_BACKFILLS_PER_CALL _MAX_EVENTS _MAX_KEY_LEN
_MAX_PROMOTION_REFUSED _MAX_SQL_PARAMS _MAX_VALUE_BYTES _MEMORY_META_TABLE _MIGRATIONS
_MMR_LAMBDA _MMR_MAX_POOL _PREFS_OMISSION_NOTICE _ROW_STEM_CACHE_SIZE _ReadCounters
_RecallQuery _RecallSpaceChanged _SCHEMA_V1 _SECURITY_REJECT_CODES _SEMANTIC_SCORING_MAX_BYTES
_SEMANTIC_KEYWORD_WEIGHT _SEMANTIC_VECTOR_WEIGHT _STEM_CACHE_SIZE _ScanSurface
_SemanticScoringSet
_contains_memory_search_text _decoded_lesson_value _get_snowball _hybrid_score
_is_degenerate_value _is_degenerate_value_json _is_selective_keyword _jaccard
_json_value_equal _kept_episodes _keyword_score _lesson_applies _lesson_display_text
_lesson_embed_text _lesson_fields _lesson_fields_for_row _lesson_key _lesson_scope
_lesson_scope_unusable _lesson_slug _member_identity _migrate_v2 _mmr_rerank
_normalize_memory_search_query _now_iso _renderable_lesson_text _row_stem_tokens
_row_stem_tokens_for_scan _row_stem_tokens_uncached _sanitize_decay_rates
_snowball_local _split_stored _stem_one _stem_words _strict_json_equal _tokenize
consolidation_source_digest create_member_database faiss logger np
open_member_database read_member_database_identity
_INJECTION_PATTERNS _MAX_EPISODIC_PER_CONSOLIDATION _MAX_EPISODIC_RETIRED_PER_WRITE
_MAX_LESSONS_PER_CONSOLIDATION _MAX_SEMANTIC_PER_CONSOLIDATION _contains_injection
""".split()


#: Public names the facade exposes by importing them from other ``kiro_crew``
#: modules. Callers may import them from here, so they stay; standard-library
#: bindings (``re``, ``heapq``, ...) are implementation detail, not surface.
_IMPORTED_PUBLIC_NAMES = """
ALLOWED_LESSON_CATEGORIES LESSON_APPLIES_ON_TOPIC LESSON_APPLIES_UNSTATED
LESSON_APPLIES_VALUES MEMORY_DB_FILE PRIORITY_BULK PRIORITY_INTERACTIVE PRIORITY_NORMAL
authored_lesson_applies bulk_pace_delay canonical_scope config_dir
contains_volatile_lesson_fact extracted_lesson_applies live memory_schema memory_stores
memory_v2 normalize_lesson_applies normalize_lesson_category order_by_request_relevance
platform_compat project_scope_satisfied record_meta redact_and_truncate render_lesson_tier
render_withheld_tier scope_is_admissible scope_selector_is_inadmissible sqlite3
tighter_lesson_budget timed
""".split()


def _member_row(name: str, raw: object) -> tuple[str, str]:
    if isinstance(raw, staticmethod):
        return "static", str(inspect.signature(raw.__func__))
    if isinstance(raw, property):
        return "property", str(inspect.signature(raw.fget))
    if callable(raw):
        return "method", str(inspect.signature(raw))
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
    return module, path


class TestSurface:
    def test_the_runtime_package_holds_exactly_the_composed_modules(self) -> None:
        assert set(_runtime_sources()) == RUNTIME_MODULES

    def test_every_store_member_keeps_its_name_and_kind(self) -> None:
        current: dict[str, list[str]] = {}
        for name, raw in vars(VectorMemoryStore).items():
            if name.startswith("__") and name != "__init__":
                continue
            current.setdefault(_member_row(name, raw)[0], []).append(name)
        expected = {kind: sorted(names.split()) for kind, names in _STORE_MEMBERS.items()}
        assert {kind: sorted(names) for kind, names in current.items()} == expected

    def test_every_store_member_keeps_its_signature(self) -> None:
        current = {
            name: _member_row(name, raw)[1]
            for name, raw in vars(VectorMemoryStore).items()
            if not name.startswith("__") or name == "__init__"
        }
        assert current == _STORE_SIGNATURES

    def test_every_module_name_still_resolves_on_the_facade(self) -> None:
        missing = [name for name in _MODULE_NAMES if not hasattr(vm, name)]
        assert missing == []

    def test_every_imported_public_name_still_resolves_on_the_facade(self) -> None:
        missing = [name for name in _IMPORTED_PUBLIC_NAMES if not hasattr(vm, name)]
        assert missing == []

    def test_a_moved_name_is_its_owner_object_not_a_copy(self) -> None:
        """A re-export is the owner's object, so identity checks and patches of a
        MUTABLE owner attribute (a cache, a set) see one thing, not two."""
        moved: list[str] = []
        for stem in sorted(RUNTIME_MODULES):
            module = _runtime_module(stem)
            for name in _MODULE_NAMES:
                if name in vars(module) and not inspect.ismodule(vars(module)[name]):
                    if name in _facade_seams():
                        continue  # the facade's binding is the seam, by design
                    assert getattr(vm, name) is vars(module)[name], (stem, name)
                    moved.append(name)
        # Non-vacuous: the stemmer, the lesson codec and the scoring set did move.
        assert {"_mmr_rerank", "_lesson_display_text", "_EpisodicScoringSet"} <= set(moved)

    def test_a_star_import_still_binds_every_public_name(self, tmp_path: Path) -> None:
        probe, _ = _load_probe(tmp_path, "vm_star_probe", "from kiro_crew.vector_memory import *\n")
        public = [name for name in _MODULE_NAMES if not name.startswith("_") and name != "logger"]
        public += _IMPORTED_PUBLIC_NAMES
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


class TestPlacement:
    """The rules that keep a facade patch effective once the code has moved."""

    @staticmethod
    def _module_level_imports(tree: ast.Module) -> list[ast.stmt]:
        """Imports at module scope, excluding those under ``if TYPE_CHECKING:``."""
        found = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.append(node)
        return found

    def test_the_facade_imports_every_runtime_module_when_it_loads(self) -> None:
        """Owners load with the facade, so their by-name imports are taken when
        ``kiro_crew.vector_memory`` is imported, never later inside a test's patch."""
        tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == RUNTIME_PACKAGE:
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                RUNTIME_PACKAGE + "."
            ):
                imported.add(node.module.rsplit(".", 1)[-1])
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
            for node in self._module_level_imports(ast.parse(source)):
                names = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [f"{node.module}.{alias.name}" for alias in node.names]
                )
                assert "kiro_crew.vector_memory" not in names, stem
                assert not any(
                    name.startswith("numpy") or name.startswith("faiss") for name in names
                )

    def test_runtime_imports_form_a_dag(self) -> None:
        graph: dict[str, set[str]] = {}
        for stem, source in _runtime_sources().items():
            tree = ast.parse(source)
            deps = set(_module_aliases(tree).values())
            for node in self._module_level_imports(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    RUNTIME_PACKAGE + "."
                ):
                    deps.add(node.module.rsplit(".", 1)[-1])
            graph[stem] = deps
        # Non-vacuous: owners reach each other through module imports too.
        assert "text_scoring" in graph["episodic_search"] and "lessons" in graph["semantic"]
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

    def test_runtime_functions_import_the_facade_as_a_module(self) -> None:
        """``from kiro_crew import vector_memory`` and then ``vector_memory.<seam>``:
        never a by-name import of a facade binding, which would read the binding
        once instead of through the module."""
        imports = 0
        for stem, source in _runtime_sources().items():
            tree = ast.parse(source)
            typing_only = {
                id(sub)
                for block in tree.body
                if isinstance(block, ast.If) and ast.unparse(block.test) == "TYPE_CHECKING"
                for sub in ast.walk(block)
            }
            for node in ast.walk(tree):
                if id(node) in typing_only:
                    continue
                if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew":
                    imports += "vector_memory" in [alias.name for alias in node.names]
                if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew.vector_memory":
                    raise AssertionError(f"{stem}:{node.lineno} imports facade names by name")
                if isinstance(node, ast.Import):
                    assert "kiro_crew.vector_memory" not in [a.name for a in node.names], stem
        assert imports > 10

    @staticmethod
    def _bare_seam_loads(source: str) -> list[str]:
        """Seam reads inside a function that a patch of the facade would miss.

        A runtime function reads a seam as ``vm.<name>`` (or binds a local from it).
        Three spellings bypass that: a bare global ``np`` or ``_now_iso``, which is the
        module's own binding; a function-local import of a seam, under its own name or
        an alias (``import time``, ``from datetime import datetime as when``), which
        reads the real object; and a seam read through a sibling owner module
        (``_text_scoring._ROW_STEM_CACHE_SIZE``), which reads that owner's copy. Every
        function counts, class methods and nested classes included;
        the one allowed import is the facade itself (``from kiro_crew import
        vector_memory``). Module-level code runs once, at import, and is out of
        scope here.
        """
        seams = _facade_seams()
        tree = ast.parse(source)
        siblings = _module_aliases(tree)
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
                        facade = isinstance(node, ast.ImportFrom) and (
                            node.module == "kiro_crew" and alias.name == "vector_memory"
                        )
                        if {bound, imported} & seams and not facade:
                            hits.append(f"{node.lineno}: import binds {imported}")
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in seams
                    and node.id not in local
                    and id(node) not in annotations
                ):
                    hits.append(f"{node.lineno}: {node.id}")
                elif (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in siblings
                    and node.value.id not in local
                    and node.attr in seams
                ):
                    hits.append(f"{node.lineno}: {node.value.id}.{node.attr}")
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
            "WIDTH = _ROW_STEM_CACHE_SIZE\n"
            "def f(store, row: 'datetime') -> 'np.ndarray':\n"
            "    from kiro_crew import vector_memory as vm\n"
            "    good = vm.np.zeros(2)\n"
            "    faiss = vm.faiss\n"
            "    faiss.IndexFlatIP(2)\n"
            "    return np.zeros(2), _now_iso(), datetime.now()\n"
            "class Scoring:\n"
            "    def build(self):\n"
            "        from kiro_crew import vector_memory\n"
            "        return vector_memory.time.monotonic(), _HAS_NUMPY\n"
            "    class Inner:\n"
            "        def stamp(self):\n"
            "            import time\n"
            "            from datetime import datetime as when\n"
            "            from datetime import datetime\n"
            "            return time.monotonic(), datetime.now(), when\n"
        )
        assert [hit.split(": ")[1] for hit in self._bare_seam_loads(planted)] == [
            "_now_iso",
            "datetime",
            "np",
            "_HAS_NUMPY",
            "import binds time",
            "import binds datetime",
            "import binds datetime",
        ]

    def test_every_runtime_logger_is_the_store_logger(self) -> None:
        for stem, source in _runtime_sources().items():
            tree = ast.parse(source)
            calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getLogger"
            ]
            for call in calls:
                assert [ast.literal_eval(arg) for arg in call.args] == [STORE_LOGGER], stem
            module = _runtime_module(stem)
            if "logger" in vars(module):
                assert module.logger is logging.getLogger(STORE_LOGGER)

    def test_no_runtime_module_defines_an_async_function(self) -> None:
        for stem, source in _runtime_sources().items():
            assert not [
                n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef)
            ], stem

    def test_the_seam_scan_catches_a_seam_read_through_a_sibling_module(self) -> None:
        planted = (
            "from kiro_crew.vector_memory_runtime import text_scoring as _text_scoring\n"
            "def f(width):\n"
            "    return width > _text_scoring._ROW_STEM_CACHE_SIZE, _text_scoring._MMR_MAX_POOL\n"
        )
        assert self._bare_seam_loads(planted) == ["3: _text_scoring._ROW_STEM_CACHE_SIZE"]

    def test_the_seams_are_the_names_tests_rebind_on_the_facade(self) -> None:
        """Non-vacuous: the scan reads other test modules, in the spellings they rebind
        with. No test here patches these three: ``sqlite3`` is set through a local
        ``from kiro_crew import vector_memory``, ``_MAX_PROMOTION_REFUSED`` through
        ``patch.object`` on an ``import ... as`` alias, and
        ``read_member_database_identity`` through a dotted string."""
        assert {"sqlite3", "_MAX_PROMOTION_REFUSED", "read_member_database_identity"} <= (
            _corpus_facade_patches()
        )

    def test_the_patch_scan_reads_every_rebinding_spelling(self) -> None:
        # The corpus scan reads this file's text too, so the dotted target names a
        # facade-only function that no runtime module binds.
        planted = (
            "import importlib\n"
            "import kiro_crew.vector_memory as memory\n"
            "from unittest import mock\n"
            "from kiro_crew import vector_memory as vm\n"
            "from kiro_crew.vector_memory_runtime import semantic\n"
            "def test_it(monkeypatch, name):\n"
            "    store = importlib.import_module('kiro_crew.vector_memory')\n"
            "    monkeypatch.setattr(vm, '_MAX_VALUE_BYTES', 16)\n"
            "    monkeypatch.setattr(memory, '_KEY_PATTERN', None)\n"
            "    monkeypatch.setattr(store, '_MMR_MAX_POOL', 8)\n"
            "    mock.patch.object(vm, '_json_value_equal')\n"
            "    mock.patch('kiro_crew.vector_memory.open_member_database')\n"
            "    monkeypatch.setattr(vm, name, 1)\n"
            "    monkeypatch.setattr(vm.VectorMemoryStore, 'recall', None)\n"
            "    monkeypatch.setattr(semantic, '_MAX_KEY_LEN', 1)\n"
            "    mock.patch('kiro_crew.vector_memory_runtime.semantic._EMPTY_VALUE_JSON')\n"
        )
        assert _facade_patch_targets(planted) == {
            "_MAX_VALUE_BYTES",
            "_KEY_PATTERN",
            "_MMR_MAX_POOL",
            "_json_value_equal",
            "open_member_database",
        }

    @staticmethod
    def _reads_through_the_facade(source: str) -> set[str]:
        """Names a module's functions read as ``vector_memory.<name>``."""
        names: set[str] = set()
        for function in ast.walk(ast.parse(source)):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            facade = {
                alias.asname or alias.name
                for node in ast.walk(function)
                if isinstance(node, ast.ImportFrom) and node.module == "kiro_crew"
                for alias in node.names
                if alias.name == "vector_memory"
            }
            names |= {
                node.attr
                for node in ast.walk(function)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in facade
            }
        return names

    @classmethod
    def _runtime_rebinds(cls, patched: frozenset[str] | set[str]) -> dict[str, list[str]]:
        """Runtime modules holding their own binding of a facade-patched name.

        A module may keep one only when it reads the name back through the facade at
        call time, as ``text_scoring`` does for the memo width its ``lru_cache`` takes
        at import; ``_bare_seam_loads`` keeps every other read of it off the copy.
        """
        sources = _runtime_sources()
        found = {}
        for stem in sorted(RUNTIME_MODULES):
            bound = {
                name
                for name, value in vars(_runtime_module(stem)).items()
                if not inspect.ismodule(value)
            }
            if rebound := sorted(
                bound & set(patched) - cls._reads_through_the_facade(sources[stem])
            ):
                found[stem] = rebound
        return found

    def test_no_runtime_module_binds_a_name_tests_rebind_on_the_facade(self) -> None:
        """A runtime module that defined or imported a rebound name would keep using
        its own binding, and the facade patch would silently stop applying there."""
        assert self._runtime_rebinds(_facade_seams()) == {}

    def test_the_rebinding_check_names_the_owner_a_facade_patch_would_miss(self) -> None:
        planted = {"_MAX_VALUE_BYTES", "_MMR_MAX_POOL", "_ROW_STEM_CACHE_SIZE", "_now_iso"}
        assert self._runtime_rebinds(planted) == {
            "semantic": ["_MAX_VALUE_BYTES"],
            "text_scoring": ["_MMR_MAX_POOL"],
        }

    @staticmethod
    def _delegation_map() -> dict[tuple[str, str], str]:
        """``(runtime module, function) -> store method`` for every delegate.

        A delegate is a store method whose body forwards to ``<alias>.<function>``
        where ``<alias>`` is a runtime module the facade imported.
        """
        tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
        aliases = {
            alias.asname or alias.name: alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == RUNTIME_PACKAGE
            for alias in node.names
        }
        # A module-level helper the facade also re-exports by name (``_json_value_equal``)
        # is shared code a store method calls, not the implementation it forwards to.
        helpers = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith(RUNTIME_PACKAGE + ".")
            for alias in node.names
        }
        klass = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "VectorMemoryStore"
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
                    and node.func.attr not in helpers
                ):
                    mapping.setdefault((aliases[node.func.value.id], node.func.attr), method.name)
        return mapping

    def test_runtime_code_reaches_a_delegated_method_only_through_the_store(self) -> None:
        """A class-level patch of a store method must reach every caller. Runtime
        code therefore calls ``store.<method>(...)``, never the function that
        implements it, even inside its own module."""
        delegated = self._delegation_map()
        # Non-vacuous: most of the store's rules are delegated.
        assert len(delegated) > 80
        offenders = []
        for stem, source in _runtime_sources().items():
            tree = ast.parse(source)
            aliases = {
                alias.asname or alias.name: alias.name
                for node in tree.body
                if isinstance(node, ast.ImportFrom) and node.module == RUNTIME_PACKAGE
                for alias in node.names
            }
            names = {
                alias.asname or alias.name: (node.module.rsplit(".", 1)[-1], alias.name)
                for node in tree.body
                if isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith(RUNTIME_PACKAGE + ".")
                for alias in node.names
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                target = None
                if isinstance(func, ast.Name):
                    target = names.get(func.id, (stem, func.id))
                elif (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id in aliases
                ):
                    target = (aliases[func.value.id], func.attr)
                if target in delegated:
                    offenders.append(f"{stem}:{node.lineno} calls {target} for {delegated[target]}")
        assert offenders == []


# ── One binding per shared name ───────────────────────────────────────────────


def _owned_names(sources: dict[str, str]) -> dict[str, str]:
    """``name -> runtime module`` for each module-level function or value exactly one
    runtime module defines. Classes are left out: a class is patched on the class
    object, which every binding of its name shares."""
    defined: dict[str, list[str]] = {}
    for stem, source in sources.items():
        for node in ast.parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.setdefault(node.name, []).append(stem)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        defined.setdefault(target.id, []).append(stem)
    return {name: stems[0] for name, stems in defined.items() if len(stems) == 1}


def _split_reads(sources: dict[str, str]) -> set[tuple[str, str]]:
    """``(module, name)`` for each module that reads a SHARED runtime name from its
    own binding instead of its owner's.

    *sources* maps each scanned module -- the facade and the runtime modules -- to its
    source. A name is shared when two or more of them read it: as a bare global
    (``_MMR_MAX_POOL``) or through an owner module alias
    (``_text_scoring._MMR_MAX_POOL``). Only the owner may read a shared name bare;
    any other bare read is a by-name copy that a patch of the owner never reaches.
    """
    owners = _owned_names({stem: src for stem, src in sources.items() if stem in RUNTIME_MODULES})
    readers: dict[str, set[str]] = {}
    bare: set[tuple[str, str]] = set()
    for stem, source in sources.items():
        tree = ast.parse(source)
        aliases = _module_aliases(tree)
        skipped = {
            id(sub)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for sub in ast.walk(node)
        }
        for node in ast.walk(tree):
            for field in ("annotation", "returns"):
                sub = getattr(node, field, None)
                if sub is not None:
                    skipped.update(id(part) for part in ast.walk(sub))
        for node in ast.walk(tree):
            if id(node) in skipped:
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in owners:
                readers.setdefault(node.id, set()).add(stem)
                bare.add((stem, node.id))
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.attr in owners
                and aliases.get(node.value.id) == owners[node.attr]
            ):
                readers.setdefault(node.attr, set()).add(stem)
    return {(stem, name) for stem, name in bare if stem != owners[name] and len(readers[name]) > 1}


#: The one split the owners keep on purpose. ``test_lesson_keyword_scoring`` patches
#: ``lessons._stem_one`` and ``text_scoring._stem_one`` separately to count stemmer
#: calls on each side of a lesson ranking, so ``lessons`` keeps its own binding.
_SPLIT_BY_A_PATCHING_TEST = {("lessons", "_stem_one")}


def _sibling_name_imports(source: str) -> set[str]:
    """Every import, at any depth, that binds a runtime owner's function or value
    rather than the owner module. Classes are exempt (see ``_owned_names``)."""
    hits = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if node.level:
            module = RUNTIME_PACKAGE + (f".{module}" if module else "")
        if module.startswith(RUNTIME_PACKAGE + "."):
            owner = sys.modules.get(module)
            for alias in node.names:
                if not isinstance(getattr(owner, alias.name, None), type):
                    hits.add(f"{module}.{alias.name}")
        elif module == RUNTIME_PACKAGE:
            hits |= {
                f"{module}.{alias.name}"
                for alias in node.names
                if alias.name not in RUNTIME_MODULES
            }
    return hits


class TestOneBindingPerSharedName:
    """A name two modules read has one binding: its owner's. Every other reader goes
    through the owner module, so one patch of the owner reaches every caller."""

    @staticmethod
    def _scanned_sources() -> dict[str, str]:
        return {"vector_memory": FACADE_PATH.read_text(encoding="utf-8"), **_runtime_sources()}

    def test_every_shared_name_is_read_through_its_owner(self) -> None:
        assert _split_reads(self._scanned_sources()) == _SPLIT_BY_A_PATCHING_TEST

    def test_shared_names_are_found_across_the_facade_and_the_owners(self) -> None:
        """Non-vacuous: names read through an owner alias count as shared."""
        sources = self._scanned_sources()
        owners = _owned_names({s: src for s, src in sources.items() if s in RUNTIME_MODULES})
        assert owners["_MMR_MAX_POOL"] == "text_scoring"
        assert owners["_json_value_equal"] == "semantic"
        planted = dict(sources)
        planted["episodic_search"] = sources["episodic_search"].replace(
            "_text_scoring._MMR_MAX_POOL", "_MMR_MAX_POOL"
        )
        planted["vector_memory"] = sources["vector_memory"].replace(
            "_semantic._json_value_equal(", "_json_value_equal("
        )
        assert _split_reads(planted) == _SPLIT_BY_A_PATCHING_TEST | {
            ("episodic_search", "_MMR_MAX_POOL"),
            ("vector_memory", "_json_value_equal"),
        }

    def test_no_runtime_module_imports_a_value_from_a_sibling(self) -> None:
        """Siblings read each other's helpers and constants as module attributes."""
        found = {
            (stem, hit)
            for stem, source in _runtime_sources().items()
            for hit in _sibling_name_imports(source)
        }
        owners = _owned_names(_runtime_sources())
        assert found == {
            (stem, f"{RUNTIME_PACKAGE}.{owners[name]}.{name}")
            for stem, name in _SPLIT_BY_A_PATCHING_TEST
        }

    @pytest.mark.parametrize(
        "planted",
        [
            "from kiro_crew.vector_memory_runtime.text_scoring import _MMR_MAX_POOL\n",
            "from kiro_crew.vector_memory_runtime.lessons import _lesson_fields as fields\n",
            "def f():\n    from kiro_crew.vector_memory_runtime.semantic import _json_value_equal\n",
            "from kiro_crew.vector_memory_runtime import _hybrid_score\n",
            "from .text_scoring import _stem_words\n",
        ],
    )
    def test_the_sibling_import_scan_catches_a_planted_import(self, planted: str) -> None:
        assert _sibling_name_imports(planted)

    @pytest.mark.parametrize(
        "allowed",
        [
            "from kiro_crew.vector_memory_runtime import text_scoring as _text_scoring\n",
            "from . import lessons\n",
            "from kiro_crew.vector_memory_runtime.embedding import _RecallQuery\n",
        ],
    )
    def test_the_sibling_import_scan_allows_a_module_or_a_class(self, allowed: str) -> None:
        assert _sibling_name_imports(allowed) == set()

    def test_the_split_read_scan_catches_a_planted_copy(self) -> None:
        sources = {
            "text_scoring": "LIMIT = 3\ndef cap(rows):\n    return rows[:LIMIT]\n",
            "episodic_search": (
                "from kiro_crew.vector_memory_runtime.text_scoring import LIMIT\n"
                "def head(rows):\n    return rows[:LIMIT]\n"
            ),
            "semantic": (
                "from kiro_crew.vector_memory_runtime import text_scoring as _ts\n"
                "def head(rows):\n    return rows[: _ts.LIMIT]\n"
            ),
        }
        assert _split_reads(sources) == {("episodic_search", "LIMIT")}


# ── Seams: a facade patch reaches the moved code ──────────────────────────────


def _embed(text: str) -> list[float]:
    vec = [0.0] * 8
    for token in re.findall(r"\w+", text.lower()):
        digest = int(hashlib.md5(token.encode(), usedforsecurity=False).hexdigest(), 16)
        vec[digest % 8] += 1.0 + digest % 3
    if not any(vec):
        vec[0] = 1.0
    return vec


@pytest.fixture
def store(tmp_path, opened):
    s = opened(VectorMemoryStore(tmp_path / "memory.db", embedding_dim=8))
    s.init()
    s.embed_fn = _embed
    return s


def _seed_episodes(s: VectorMemoryStore) -> None:
    for text, tags in (
        ("The user prefers blue color themes for the dashboard", ["ui"]),
        ("Project alpha uses python for the backend services", ["project"]),
        ("We debugged the flaky websocket reconnect on windows", ["bug"]),
        ("Release planning notes for the next quarter roadmap", []),
    ):
        assert s.write_episodic(text, tags=tags)


class _Poisoned:
    """A module stand-in whose every attribute read fails, naming the attribute."""

    def __init__(self, label: str) -> None:
        self._label = label

    def __getattr__(self, name: str):
        raise AssertionError(f"{self._label}.{name} was read")


class TestSeamsReachTheRuntime:
    def test_numpy_is_read_through_the_facade(self, store, monkeypatch) -> None:
        _seed_episodes(store)
        query = _embed("blue dashboard")
        monkeypatch.setattr(vm, "np", _Poisoned("np"))
        with pytest.raises(AssertionError, match="np."):
            store._stored_similarity_scorer(query)
        with pytest.raises(AssertionError, match="np."):
            store._sqlite_vector_search(query, "", 3)
        # With the flag off the same calls take the stdlib rungs and never touch np.
        monkeypatch.setattr(vm, "_HAS_NUMPY", False)
        assert store._stored_similarity_scorer(query)({"embedding": b""}) == 0.0
        assert store._sqlite_vector_search(query, "", 3)

    def test_faiss_is_read_through_the_facade(self, store, monkeypatch) -> None:
        _seed_episodes(store)
        monkeypatch.setattr(vm, "faiss", _FakeFaiss)
        monkeypatch.setattr(vm, "_HAS_FAISS", True)
        assert store.build_faiss_index() == 4
        assert isinstance(store._faiss_index, _FakeIndex)
        monkeypatch.setattr(vm, "faiss", _Poisoned("faiss"))
        with pytest.raises(AssertionError, match="faiss.IndexFlatIP"):
            store.build_faiss_index()

    def test_the_pacing_function_is_read_through_the_facade(self, store, monkeypatch) -> None:
        store.write_episodic(
            "Deferred note about pacing the backfill sweep",
            defer_embedding=True,
            preserve_existing=True,
        )
        calls: list[float] = []
        monkeypatch.setattr(vm, "bulk_pace_delay", lambda elapsed: calls.append(elapsed) or 0.0)
        assert store.backfill_missing_embeddings(pace=True) == 1
        assert len(calls) == 1

    def test_the_clock_seams_are_read_through_the_facade(self, store, monkeypatch) -> None:
        stamps = iter(f"2031-01-0{day}T00:00:00+00:00" for day in range(1, 10))
        monkeypatch.setattr(vm, "_now_iso", lambda: next(stamps))
        assert store.set_semantic("pref.color", "blue", 1.0, "user_explicit") is None
        assert store.delete_semantic("pref.color", "user_explicit")
        row = store.db.execute(
            "SELECT updated_at FROM semantic_memory WHERE key='pref.color'"
        ).fetchone()
        assert row[0].startswith("2031-01-")
        assert store.set_semantic_if_absent("pref.editor", "vim", 1.0, "import") == "imported"
        created = store.db.execute(
            "SELECT created_at FROM semantic_memory WHERE key='pref.editor'"
        ).fetchone()[0]
        assert created.startswith("2031-01-")

    def test_the_monotonic_clock_is_read_through_the_facade(self, store, monkeypatch) -> None:
        slept: list[float] = []
        monkeypatch.setattr(
            vm, "time", SimpleNamespace(monotonic=lambda: 5000.0, sleep=slept.append)
        )
        monkeypatch.setattr(vm, "bulk_pace_delay", lambda elapsed: 0.25)
        assert store._embed_bulk_row("pacing the sweep", pace=True) == _embed("pacing the sweep")
        assert slept == [0.25]
        _seed_episodes(store)
        mem_id = store.db.execute("SELECT id FROM episodic_memories").fetchone()[0]
        store._touch_last_accessed([mem_id])
        assert store._last_accessed_touch[mem_id] == 5000.0
        monkeypatch.setattr(store, "embed_fn", None)
        monkeypatch.setattr(store, "embed_fn_factory", lambda: None)
        assert store._try_embed("rebind cooldown") is None
        assert store._embed_fn_last_rebind_attempt == 5000.0

    def test_datetime_is_read_through_the_facade(self, store, monkeypatch) -> None:
        _seed_episodes(store)
        query = _embed("blue color themes dashboard")
        fresh = store._sqlite_vector_search(query, "", 1, mmr=False)[0]["score"]
        later = datetime.now(tz=timezone.utc) + timedelta(days=400)

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return later if tz is not None else later.replace(tzinfo=None)

        monkeypatch.setattr(vm, "datetime", Later)
        store._invalidate_episodic_scoring()
        aged = store._sqlite_vector_search(query, "", 1, mmr=False)[0]["score"]
        assert aged < fresh

    def test_the_injection_screen_is_read_through_the_facade(self, store, monkeypatch) -> None:
        monkeypatch.setattr(vm, "_contains_injection", lambda text: True)
        verdict = store.validate_semantic("pref.color", "blue", 1.0, "user_explicit")
        assert verdict is not None and verdict[0] is vm.SemanticRejectCode.INJECTION

    def test_the_row_memo_width_is_read_through_the_facade(self, monkeypatch) -> None:
        assert vm._row_stem_tokens_for_scan(2) is vm._row_stem_tokens
        monkeypatch.setattr(vm, "_ROW_STEM_CACHE_SIZE", 1)
        assert vm._row_stem_tokens_for_scan(2) is vm._row_stem_tokens_uncached

    def test_the_scoring_budget_is_read_through_the_facade(self, store, monkeypatch) -> None:
        _seed_episodes(store)
        monkeypatch.setattr(vm, "_EPISODIC_SCORING_MAX_BYTES", 16)
        assert store._sqlite_vector_search(_embed("blue"), "", 2)
        assert store._episodic_scoring is None
        assert store._episodic_scoring_refused is not None

    def test_the_semantic_scoring_budget_is_read_through_the_facade(
        self, store, monkeypatch
    ) -> None:
        assert store.set_semantic("pref.color", "blue", 1.0, "user_explicit") is None
        assert "pref.color: blue" in store.get_semantic_context(query_text="blue")
        assert store._semantic_scoring is not None
        store._invalidate_semantic_scoring()
        monkeypatch.setattr(vm, "_SEMANTIC_SCORING_MAX_BYTES", 1)
        assert "pref.color: blue" in store.get_semantic_context(query_text="blue")
        assert store._semantic_scoring is None
        assert store._semantic_scoring_refused is not None

    def test_the_audit_bound_is_read_through_the_facade(self, store, monkeypatch) -> None:
        monkeypatch.setattr(vm, "_MAX_AUDITED_REJECTS", 0)
        assert store.set_semantic("pref.blank", "   ", 1.0, "user_explicit") is not None
        assert len(store._audited_rejects) == 0

    def test_the_parameter_ceiling_is_read_through_the_facade(self, store, monkeypatch) -> None:
        _seed_episodes(store)
        ids = [row["id"] for row in store.get_episodic_list(10, 0)]
        before = store.read_counters()["statements_executed"]
        monkeypatch.setattr(vm, "_MAX_SQL_PARAMS", 1)
        assert set(store._get_episodic_batch(ids)) == set(ids)
        assert store.read_counters()["statements_executed"] - before == len(ids)

    def test_uuid_and_config_dir_stay_facade_owned(self, tmp_path, monkeypatch, opened) -> None:
        import uuid

        monkeypatch.setattr(vm, "config_dir", lambda: tmp_path / "home")
        (tmp_path / "home").mkdir()
        s = opened(VectorMemoryStore(embedding_dim=8))
        assert s._db_path == tmp_path / "home" / vm._DB_FILE
        s.init()
        monkeypatch.setattr(vm, "uuid4", lambda: uuid.UUID(int=7))
        assert s.write_episodic("An episode written under a pinned identifier")
        assert s.get_episodic_list(5, 0)[0]["id"] == str(uuid.UUID(int=7))


# ── Class-level patches reach the moved callers ───────────────────────────────


@pytest.fixture
def member_store(tmp_path, opened, monkeypatch):
    """A V2 member store, which ranks with the member retrieval policy."""
    from kiro_crew import memory_stores

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "memory_stores")
    path = tmp_path / "memory_stores" / "member-store" / "memory.db"
    vm.create_member_database(path, member_id="member-id", store_id="member-store")
    s = opened(vm.open_member_database(path, member_id="member-id", store_id="member-store"))
    assert s.algorithm_version == "v2"
    return s


class TestRankingAndTransactionPins:
    """Orders and transaction shapes the other suites leave unobserved. Each
    fixture is built so that the pinned rule, not insertion order or key order,
    decides the answer, and each assertion fails if the rule is changed."""

    def test_equal_score_v2_episodes_are_ordered_by_id(self, member_store, monkeypatch) -> None:
        ids = iter(["b-episode", "a-episode"])
        monkeypatch.setattr(vm, "uuid4", lambda: next(ids))
        assert member_store.write_episodic("The zebra rollout ships every alpha morning")
        assert member_store.write_episodic("The zebra rollout ships every omega evening")
        ranked = member_store.search_episodic(
            query_text="zebra rollout", limit=5, mmr=False, relevance_filter=False
        )
        assert [row["id"] for row in ranked] == ["a-episode", "b-episode"]
        assert ranked[0]["score"] == ranked[1]["score"]

    def test_equal_score_v2_facts_are_ordered_by_key(self, member_store) -> None:
        for key in ("project.zeta_owner", "project.alpha_owner"):
            assert member_store.set_semantic(key, "zebra rollout", 1.0, "user_explicit") is None
        ranked = member_store._semantic_candidates_v2("zebra rollout")
        assert [row["key"] for row in ranked] == ["project.alpha_owner", "project.zeta_owner"]
        assert ranked[0]["score"] == ranked[1]["score"]

    def test_equal_score_v1_facts_are_ordered_oldest_first(self, tmp_path, opened, monkeypatch):
        store = opened(VectorMemoryStore(tmp_path / "memory.db"))
        store.init()
        # Each clock read is a day EARLIER, so the key written first is the newer one
        # and neither insertion order nor key order predicts the answer.
        reads = itertools.count()
        monkeypatch.setattr(
            vm, "_now_iso", lambda: f"2031-01-{30 - next(reads):02d}T00:00:00+00:00"
        )
        for key in ("project.alpha_owner", "project.zeta_owner"):
            assert store.set_semantic(key, "zebra rollout", 1.0, "user_explicit") is None
        ranked = store._semantic_candidates_v1("zebra rollout")
        assert [row["key"] for row in ranked] == ["project.zeta_owner", "project.alpha_owner"]

    def test_contradiction_candidates_are_the_five_most_similar_best_first(
        self, tmp_path, opened
    ) -> None:
        store = opened(VectorMemoryStore(tmp_path / "memory.db"))
        store.init()
        rules = {
            0.60: "amber paint suits garden sheds",
            0.80: "birch invoices need monthly checks",
            0.55: "cedar logs stay covered outdoors",
            0.70: "dahlia bulbs rot when overwatered",
            0.65: "elm roots crack shallow pipes",
            0.75: "fern spores spread through damp corners",
        }
        for cosine, rule in rules.items():
            assert store.write_lesson(rule, "knowledge")
            # A unit vector at the chosen cosine to the query axis below.
            blob = struct.pack("2f", cosine, math.sqrt(1 - cosine * cosine))
            with store._db_lock, store.db:
                store.db.execute(
                    "UPDATE semantic_memory SET embedding = ? "
                    "WHERE key LIKE 'lesson.%' AND is_deleted = 0 AND value_json LIKE ?",
                    (blob, f"%{rule}%"),
                )
        found = store.find_contradiction_candidates("anything", rule_emb=[1.0, 0.0])
        assert [round(row["similarity"], 2) for row in found] == [0.8, 0.75, 0.7, 0.65, 0.6]
        assert [row["rule"] for row in found][0] == rules[0.80]

    def test_the_default_mmr_balance_keeps_relevance_ahead_of_diversity(self) -> None:
        candidates = [
            {"text": "alpha beta gamma", "score": 1.0},
            {"text": "alpha beta delta", "score": 0.95},
            {"text": "epsilon zeta", "score": 0.5},
        ]
        # At the default 0.6 the near-duplicate's relevance wins; at 0.4 diversity would.
        assert [c["text"] for c in vm._mmr_rerank(candidates, limit=2)] == [
            "alpha beta gamma",
            "alpha beta delta",
        ]
        assert [c["text"] for c in vm._mmr_rerank(candidates, limit=2, lam=0.4)] == [
            "alpha beta gamma",
            "epsilon zeta",
        ]

    def test_reconciling_the_embedding_space_takes_the_write_lock_first(self, store) -> None:
        statements: list[str] = []
        store.db.set_trace_callback(statements.append)
        try:
            store.reconcile_embedding_space("pinned-space")
        finally:
            store.db.set_trace_callback(None)
        assert statements[0] == "BEGIN IMMEDIATE"
        assert statements[-1] == "COMMIT"

    def test_an_inferred_delete_is_recorded_as_a_forget_proposal(self, store) -> None:
        assert store.set_semantic("project.color", "blue", 1.0, "user_explicit") is None
        assert store.propose_semantic_delete("project.color", "inferred") is True
        row = store.db.execute(
            "SELECT status, operation FROM memory_revisions "
            "WHERE record_id = 'key:project.color' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(row) == ("conflict", "forget")
        assert store.get_semantic("project.color") is not None


class TestMovedRecallAndScoringBehaviour:
    """Characterizes the moved ``recall`` fallbacks and text-scoring helpers through
    the facade names, so the same assertions hold on the pre-split module."""

    def test_the_keep_hook_can_only_narrow_the_ranked_episodes(self) -> None:
        first, second, third = {"id": "a"}, {"id": "b"}, {"id": "c"}
        ranked = [first, second, third]
        assert vm._kept_episodes(ranked, None) is ranked
        assert vm._kept_episodes(ranked, lambda rows: [third, first]) == [first, third]

        def fails(rows):
            raise RuntimeError("hook failed")

        for hook in (fails, lambda rows: None, lambda rows: tuple(rows[:1])):
            assert vm._kept_episodes(ranked, hook) is ranked
        # An equal dict that this search did not rank admits nothing: identity rules.
        assert vm._kept_episodes(ranked, lambda rows: [{"id": "a"}]) is ranked

    def test_a_small_cap_truncates_evidence_instead_of_dropping_it(self, store) -> None:
        long_text = "Dashboard color notes: the user prefers blue themes " + (
            "and spacious layouts with blue accents " * 20
        )
        assert store.write_episodic(long_text, tags=["ui"])
        palette = "blue " + "with a long explanation of the dashboard palette " * 8
        assert store.set_semantic("pref.dashboard_color", palette, 1.0, "user_explicit") is None
        result = store.recall("blue dashboard color", cap=400)
        assert (result["total_chars"], result["semantic_chars"], result["episodic_chars"]) == (
            400,
            200,
            200,
        )
        (fact,) = result["retrieval"]["facts"]
        (episode,) = result["retrieval"]["episodes"]
        assert fact["snippet_truncated"] is True
        assert episode["text_truncated"] is True
        # Below the smallest locatable snippet the recall returns no memory at all.
        assert store.recall("blue dashboard color", cap=120)["total_chars"] == 0

    def test_a_space_change_mid_recall_retries_once_without_inference(
        self, store, monkeypatch
    ) -> None:
        _seed_episodes(store)
        original = VectorMemoryStore._recall_once
        queries: list = []

        def changes_once(self, query_text, **kwargs):
            queries.append(kwargs["query"])
            if len(queries) == 1:
                raise vm._RecallSpaceChanged
            return original(self, query_text, **kwargs)

        monkeypatch.setattr(VectorMemoryStore, "_recall_once", changes_once)
        result = store.recall("blue dashboard", cap=1000)
        assert queries[0].vector is not None
        assert queries[1] == vm._RecallQuery(None, None, None)
        assert result["total_chars"] <= 1000

    def test_a_query_from_another_space_is_refused(self, store) -> None:
        vector = _embed("blue dashboard")
        signature = store.recorded_embedding_space()
        generation = store._space_generation
        store._check_recall_query(None)
        store._check_recall_query(vm._RecallQuery(vector, generation, signature))
        for stale in (
            vm._RecallQuery(vector, generation + 1, signature),
            vm._RecallQuery(vector, generation, "another-space"),
            # A vector inferred against another configured space is not current.
            vm._RecallQuery(
                vm._EmbeddingVector(vector, ("another", "space")), generation, signature
            ),
        ):
            with pytest.raises(vm._RecallSpaceChanged):
                store._check_recall_query(stale)

    def test_the_list_search_predicate_matches_decoded_visible_text(self) -> None:
        with pytest.raises(ValueError, match="at most"):
            vm._normalize_memory_search_query("x" * (vm.MAX_MEMORY_SEARCH_QUERY + 1))
        with pytest.raises(ValueError):
            vm._normalize_memory_search_query(None)  # type: ignore[arg-type]
        query = vm._normalize_memory_search_query("  Caf\u00c9 ")
        stored = json.dumps({"notes": ["see", {"place": "Caf\u00e9 Blue"}], "n": 7})
        assert vm._contains_memory_search_text(stored, query, 1) == 1
        assert vm._contains_memory_search_text(stored, "7", 1) == 1
        assert vm._contains_memory_search_text(stored, "tea", 1) == 0
        # Text that is not JSON is searched as it is stored; NULL searches as empty.
        assert vm._contains_memory_search_text("{not json caf\u00e9", query, 1) == 1
        assert vm._contains_memory_search_text(None, query, 0) == 0

    def test_diversity_scoring_handles_empty_and_unrankable_candidates(self) -> None:
        assert vm._jaccard(set(), {"blue"}) == 0.0
        assert vm._jaccard({"blue", "red"}, {"blue"}) == 0.5
        unrankable = [{"text": "a", "score": math.nan}, {"text": "b", "score": math.nan}]
        assert vm._mmr_rerank(unrankable) == []


class TestClassPatchesReachTheRuntime:
    @pytest.mark.parametrize(
        "method, drive",
        [
            ("_try_embed", lambda s: s.get_episodic_context(query_text="blue dashboard")),
            ("_episodic_candidate", lambda s: s._sqlite_vector_search(_embed("blue"), "", 2)),
            ("search_episodic", lambda s: s.recall("what color does the user like")),
            ("_rank_lessons", lambda s: s.get_lessons_context("tests before pushing")),
            ("_write_semantic", lambda s: s.set_semantic("pref.x", "y", 1.0, "user_explicit")),
            ("validate_semantic", lambda s: s.set_semantic_if_absent("pref.z", "y", 1.0, "import")),
            (
                "_retire_stale_episodic",
                lambda s: s.set_semantic("pref.color", "red", 1.0, "user_explicit"),
            ),
            ("_vector_commit", lambda s: s.backfill_missing_embeddings(pace=False)),
            ("_recall_once", lambda s: s.recall("blue")),
            ("_check_recall_query", lambda s: s.recall("blue")),
        ],
    )
    def test_a_class_level_patch_reaches_the_moved_caller(
        self, store, monkeypatch, method, drive
    ) -> None:
        if method == "_episodic_candidate":
            monkeypatch.setattr(vm, "_HAS_NUMPY", False)
        _seed_episodes(store)
        assert store.write_lesson("Always run the tests before pushing", "tool")
        store.set_semantic("pref.color", "blue", 1.0, "user_explicit")
        store.write_episodic(
            "Deferred row awaiting its vector in the sweep",
            defer_embedding=True,
            preserve_existing=True,
        )
        original = getattr(VectorMemoryStore, method)
        calls: list[str] = []

        def spy(*args, **kwargs):
            calls.append(method)
            return original(*args, **kwargs)

        monkeypatch.setattr(VectorMemoryStore, method, spy)
        drive(store)
        assert calls, f"{method} was not reached through the store"


# ── Read-only guards re-applied to the runtime modules ────────────────────────


class TestReadOnlyGuardsReachTheRuntime:
    """Each guard below scans ``vector_memory.py`` by name; the runtime modules get
    the same check through the guard's own helper, and a planted violation proves
    the re-applied check is not vacuous."""

    def test_no_runtime_statement_writes_a_crew_view(self) -> None:
        for stem, source in _runtime_sources().items():
            assert drift._write_violations(ast.parse(source), stem) == []

    def test_the_view_write_check_catches_a_planted_statement(self) -> None:
        planted = ast.parse(
            "def f(store):\n"
            "    with store._db_lock:\n"
            "        store.db.execute('UPDATE semantic_memory SET is_deleted = 1 WHERE key = ?')\n"
        )
        assert len(drift._write_violations(planted, "planted")) == 1

    @staticmethod
    def _unguarded_writes(source: str, label: str) -> list[str]:
        """The lineage guard's own kind-guard rule, for the ``store`` receiver."""
        return drift._unguarded_write_violations(source.replace("store.", "self."), label)

    def test_every_runtime_per_lineage_write_carries_its_kind_guard(self) -> None:
        interpolated = 0
        for stem, source in _runtime_sources().items():
            assert self._unguarded_writes(source, stem) == []
            interpolated += len(re.findall(r"\{\s*store\._(?:sem|epi)_rel\s*\}", source))
        assert interpolated > 10  # the moved statements really are scanned

    def test_the_kind_guard_check_catches_a_planted_statement(self) -> None:
        planted = (
            "def f(store):\n"
            '    store.db.execute(f"UPDATE {store._epi_rel} SET is_deleted = 1 WHERE id = ?", (1,))\n'
            "    store.db.execute(\n"
            '        f"UPDATE {store._sem_rel} SET x = 1 WHERE key = ?{store._sem_guard}", (1,)\n'
            "    )\n"
        )
        violations = self._unguarded_writes(planted, "planted")
        assert len(violations) == 1 and "_epi_rel" in violations[0]

    @staticmethod
    def _relation_violations(tree: ast.AST) -> list[str]:
        """Relations a runtime module names that are not shared by both lineages.

        The runtime holds none of the member-database operations, so every relation
        it names must exist on v1 and on a crew silo alike.
        """
        named = drift._named_relations(tree)
        v1, crew = drift._v1_db(), drift._crew_db()
        try:
            both = drift._relations_of(v1) & drift._relations_of(crew)
        finally:
            v1.close()
            crew.close()
        return sorted(named - both)

    def test_every_runtime_relation_exists_in_both_lineages(self) -> None:
        named: set[str] = set()
        for stem, source in _runtime_sources().items():
            tree = ast.parse(source)
            assert self._relation_violations(tree) == [], stem
            named |= drift._named_relations(tree)
        assert {"semantic_memory", "episodic_memories", "memory_events", "memory_meta"} <= named

    def test_the_relation_check_catches_member_and_crew_only_statements(self) -> None:
        planted = ast.parse(
            "def f(store):\n"
            "    store.db.execute('SELECT content FROM memory_history WHERE day = ?')\n"
            "    store.db.execute('UPDATE memory_items SET is_deleted = 1 WHERE id = ?')\n"
        )
        assert self._relation_violations(planted) == ["memory_history", "memory_items"]

    def test_no_runtime_statement_through_a_view_names_a_facet(self) -> None:
        through_a_view = [
            statement
            for stem in sorted(RUNTIME_MODULES)
            for statement in v2_schema._sql_statements(_runtime_module(stem))
            if any(relation in statement for relation in v2_schema._V1_RELATIONS)
        ]
        assert [s for s in through_a_view if "{}" in s], "no interpolated statement was collected"
        for statement in through_a_view:
            assert not v2_schema._FACET_WORD.findall(statement), statement

    def test_the_facet_check_catches_a_planted_statement(self, tmp_path: Path) -> None:
        module, _ = _load_probe(
            tmp_path,
            "planted_facet_read",
            "def f(store):\n"
            '    return store.db.execute("SELECT crew FROM episodic_memories WHERE id = ?")\n',
        )
        statements = v2_schema._sql_statements(module)
        assert any(v2_schema._FACET_WORD.findall(s) for s in statements)

    def test_no_runtime_module_calls_a_redactor(self) -> None:
        """A redactor call belongs to ``vector_memory.py``, the registered sink."""
        for stem in RUNTIME_MODULES:
            path = RUNTIME_DIR / f"{stem}.py"
            assert not _REDACTOR_CALL_RE.search(path.read_text(encoding="utf-8")), stem
            assert _find_slice_inside_redact_call(path) == []
        assert _REDACTOR_CALL_RE.search(FACADE_PATH.read_text(encoding="utf-8"))

    def test_the_redactor_checks_catch_a_planted_call(self, tmp_path: Path) -> None:
        path = tmp_path / "planted_redact.py"
        path.write_text("def f(text):\n    return redact_and_truncate(text[:200], 200)\n")
        assert _REDACTOR_CALL_RE.search(path.read_text())
        assert _find_slice_inside_redact_call(path)

    def test_no_runtime_module_hashes_with_md5(self) -> None:
        for stem, source in _runtime_sources().items():
            assert "hashlib.md5(" not in source, stem
        assert "hashlib.md5(" in FACADE_PATH.read_text(encoding="utf-8")


# ── The FAISS accelerator, through a numpy-backed stand-in ────────────────────


class _FakeIndex:
    """``faiss.IndexFlatIP`` semantics over a numpy matrix: exact inner product."""

    fail_add = False

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.rows = np.zeros((0, dim), dtype=np.float32)

    @property
    def ntotal(self) -> int:
        return int(self.rows.shape[0])

    def add(self, vectors) -> None:
        if type(self).fail_add:
            raise RuntimeError("index full")
        self.rows = np.vstack(
            [self.rows, np.asarray(vectors, dtype=np.float32).reshape(-1, self.dim)]
        )

    def search(self, query, k: int):
        sims = self.rows @ np.asarray(query, dtype=np.float32).reshape(-1)
        order = np.argsort(-sims, kind="stable")[:k]
        distances = np.full((1, k), -3.4e38, dtype=np.float32)
        indices = np.full((1, k), -1, dtype=np.int64)
        distances[0, : len(order)] = sims[order]
        indices[0, : len(order)] = order
        return distances, indices


class _FakeFaiss:
    IndexFlatIP = _FakeIndex

    @staticmethod
    def write_index(index: _FakeIndex, path: str) -> None:
        with open(path, "wb") as handle:
            np.save(handle, index.rows, allow_pickle=False)

    @staticmethod
    def read_index(path: str) -> _FakeIndex:
        with open(path, "rb") as handle:
            rows = np.load(handle, allow_pickle=False)
        index = _FakeIndex(rows.shape[1])
        index.rows = rows
        return index


@pytest.fixture
def faiss_store(tmp_path, opened, monkeypatch):
    monkeypatch.setattr(vm, "faiss", _FakeFaiss)
    monkeypatch.setattr(vm, "_HAS_FAISS", True)
    monkeypatch.setattr(_FakeIndex, "fail_add", False)
    s = opened(VectorMemoryStore(tmp_path / "memory.db", embedding_dim=8))
    s.init()
    s.embed_fn = _embed
    return s


def _ranked(results: list[dict]) -> list[str]:
    return [row["id"] for row in results]


class TestFaissAccelerator:
    def test_writes_extend_the_index_and_its_id_map_together(self, faiss_store) -> None:
        _seed_episodes(faiss_store)
        assert faiss_store._faiss_index.ntotal == len(faiss_store._faiss_id_map) == 4

    def test_the_faiss_tier_ranks_like_the_stored_vector_tier(
        self, faiss_store, monkeypatch
    ) -> None:
        _seed_episodes(faiss_store)
        query = _embed("blue color themes for the dashboard")
        via_faiss = faiss_store.search_episodic(query_embedding=query, limit=3)
        assert via_faiss
        monkeypatch.setattr(vm, "_HAS_FAISS", False)
        via_sqlite = faiss_store.search_episodic(query_embedding=query, limit=3)
        assert _ranked(via_faiss) == _ranked(via_sqlite)
        assert [r["cosine_sim"] for r in via_faiss] == [r["cosine_sim"] for r in via_sqlite]

    def test_the_faiss_tier_honours_the_tag_filter_and_forgotten_rows(self, faiss_store) -> None:
        _seed_episodes(faiss_store)
        query = _embed("project alpha python backend")
        tagged = faiss_store.search_episodic(query_embedding=query, limit=4, tag_filter=["project"])
        assert [json.loads(r["tags"]) for r in tagged] == [["project"]]
        forgotten = tagged[0]["id"]
        assert faiss_store.delete_episodic(forgotten)
        again = faiss_store.search_episodic(query_embedding=query, limit=4)
        assert forgotten not in _ranked(again)

    def test_a_near_duplicate_write_is_refused_or_merged_by_the_index(self, faiss_store) -> None:
        vector = _embed("blue color themes")
        assert faiss_store.write_episodic("The user prefers blue color themes", embedding=vector)
        # Same vector, different text: an import is merge-only, a shorter write loses.
        assert not faiss_store.write_episodic(
            "Blue themes, per the user", embedding=vector, preserve_existing=True
        )
        assert not faiss_store.write_episodic("Blue themes are liked", embedding=vector)
        longer = "The user prefers blue color themes across the dashboard and the editor"
        assert faiss_store.write_episodic(longer, embedding=vector)
        assert [row["text"] for row in faiss_store.get_episodic_list(10, 0)] == [longer]

    def test_save_then_load_round_trips_the_stamped_index(self, faiss_store) -> None:
        _seed_episodes(faiss_store)
        faiss_store.save_faiss_index()
        stamp = json.loads(faiss_store._read_meta("faiss_content_signature"))
        assert set(stamp) == {"database", "index", "ids"}
        faiss_store._faiss_index = None
        assert faiss_store.load_faiss_index() is True
        assert faiss_store._faiss_index.ntotal == 4

    def test_a_stale_or_damaged_saved_index_is_rebuilt_from_sqlite(self, faiss_store) -> None:
        _seed_episodes(faiss_store)
        faiss_store.save_faiss_index()
        faiss_store._write_meta("faiss_content_signature", json.dumps({"database": "old"}))
        assert faiss_store.load_faiss_index() is False
        assert faiss_store._faiss_index.ntotal == 4
        faiss_store.save_faiss_index()
        faiss_store._faiss_path.write_bytes(b"not an index")
        assert faiss_store.load_faiss_index() is False  # the stamp does not match the file
        assert faiss_store._faiss_index.ntotal == 4

    def test_a_desynced_id_map_is_rebuilt(self, faiss_store, monkeypatch) -> None:
        _seed_episodes(faiss_store)
        faiss_store.save_faiss_index()
        monkeypatch.setattr(_FakeFaiss, "read_index", staticmethod(lambda path: _FakeIndex(8)))
        assert faiss_store.load_faiss_index() is False
        assert faiss_store._faiss_index.ntotal == len(faiss_store._faiss_id_map) == 4

    def test_another_connection_commit_drops_the_index_before_search(
        self, faiss_store, tmp_path
    ) -> None:
        _seed_episodes(faiss_store)
        faiss_store.build_faiss_index()
        from kiro_crew._sqlite_compat import sqlite3

        other = sqlite3.connect(tmp_path / "memory.db")
        try:
            other.execute("UPDATE episodic_memories SET importance = 0.9")
            other.commit()
        finally:
            other.close()
        results = faiss_store.search_episodic(query_embedding=_embed("blue dashboard"), limit=2)
        assert results and faiss_store._faiss_index is None

    def test_invalidating_episode_content_drops_both_derived_populations(self, faiss_store) -> None:
        _seed_episodes(faiss_store)
        generation = faiss_store._episodic_scoring_generation
        faiss_store.invalidate_episode_content()
        assert faiss_store._faiss_index is None and faiss_store._faiss_id_map == []
        assert faiss_store._episodic_scoring_generation == generation + 1

    def test_backfill_extends_the_index_and_survives_a_rejecting_index(
        self, faiss_store, monkeypatch
    ) -> None:
        faiss_store.write_episodic(
            "Deferred row one awaiting the sweep", defer_embedding=True, preserve_existing=True
        )
        faiss_store.write_episodic(
            "Deferred row two awaiting the sweep", defer_embedding=True, preserve_existing=True
        )
        before = faiss_store._faiss_index.ntotal
        assert faiss_store.backfill_missing_embeddings(pace=False, max_rows_per_kind=1) == 1
        assert faiss_store._faiss_index.ntotal == before + 1
        monkeypatch.setattr(_FakeIndex, "fail_add", True)
        assert faiss_store.backfill_missing_embeddings(pace=False) == 1
        assert faiss_store._faiss_index is None  # accelerator disabled, SQLite kept the vector
        assert not faiss_store.has_pending_embeddings()

    def test_a_mismatched_width_row_is_left_out_of_the_index(self, faiss_store) -> None:
        _seed_episodes(faiss_store)
        faiss_store.db.execute(
            "UPDATE episodic_memories SET embedding = ? WHERE rowid = 1", (b"\x00" * 12,)
        )
        faiss_store.db.commit()
        assert faiss_store.build_faiss_index() == 3
