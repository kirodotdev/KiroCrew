"""``kiro_crew.knowledge.ingestion`` stays the one import path and patch surface of its owners.

The ingestion module was split into sibling owners (``ingestion_<role>.py``) behind a
:mod:`kiro_crew._facade_mirror` facade that keeps ``IngestionPipeline`` and the helpers it
reads. These tests pin what the split must keep:

* the facade serves a mirror over exactly the ``ingestion_*`` modules beside it, and every
  module-level name the unsplit file bound still resolves through it, to the object each
  owner holds;
* every name the test trees patch through the facade reaches every module holding it --
  ``bulk_pace_delay`` among them, which the re-embed sweep reads in its owner;
* the owners load lowest layer first and never import the facade;
* every name the facade serves from outside the family is named in a ``TYPE_CHECKING``
  group, so ``from kiro_crew.knowledge.ingestion import <name>`` stays visible to type
  checkers;
* nothing outside the family imports an owner: callers reach the budget and the re-embed
  sweep through the facade;
* the family logs as ``kiro_crew.knowledge.ingestion``, and ``ImportChunkBudgetError``
  keeps the facade's ``__module__``: it is an exception, and its dotted name is what a
  traceback or a log line shows.

Each structural check has a self-test that shows it can fail.
"""

from __future__ import annotations

import ast
import functools
import importlib
import logging
from collections.abc import Iterable
from pathlib import Path

import facade_mirror_scan
import pytest
import source_corpus

import kiro_crew
from kiro_crew import _facade_mirror
from kiro_crew.knowledge import ingestion as facade

# Walks the test trees and src/kiro_crew; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_ingestion_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = ("known_seams", "corpus_gate")

_SRC = Path(kiro_crew.__file__).resolve().parent
_PKG_DIR = _SRC / "knowledge"
_FACADE = "kiro_crew.knowledge.ingestion"
_OWNER_PREFIX = "kiro_crew.knowledge.ingestion_"
#: The owners, lowest layer first.
_OWNERS = (
    "kiro_crew.knowledge.ingestion_budget",
    "kiro_crew.knowledge.ingestion_rebuild",
)
_THIS_FILE = Path(__file__).resolve()

#: Every module-level name ``knowledge/ingestion.py`` bound before the split, less the
#: stdlib modules it bound with a plain ``import <module>``.
_BASE_MODULE_NAMES = frozenset("""
    AUTO_ADDED_PROP AbstractAsyncContextManager AsyncIterator CHUNK_OVERLAP CHUNK_TOKEN_SIZE
    CODE_EXTS Callable DEFAULT_MAX_INGEST_FILE_MB DUPLICATE_JOB_STATUS EmbedRateLimiter
    EntityExtractor FileReader FileTooLargeError HeadingAwareChunker ImportChunkBudget
    ImportChunkBudgetError IngestionPipeline KnowledgeStore MARKDOWN_EXTS
    MAX_CHUNKS_PER_FILE PERSISTENT_SOURCE_TYPES PRIORITY_BULK PRIORITY_NORMAL Path TypeVar
    _IMPORT_CHUNK_BUDGET_WINDOW_SECS _INGESTION_GATE_ADMITTED _INGESTION_GATE_DEPTH _MB
    _REBUILD_BATCH_SIZE _REBUILD_STALE_AFTER _REEMBED_RETRY_BACKOFF _SUMMARY_PAYLOAD_KEYS _T
    _coerce_chunk_param _commit_rebuild_progress _embed_rate_limiter _embed_row_paced
    _extract_json_of_type _fetch_rebuild_page _first_line_title _heartbeat_rebuild_job
    _import_chunk_budget _init_rebuild_total _job_status _max_ingest_file_mb _redact
    _redact_for_ingest _run_chunker _select_active_rebuild_job _stamp_embed_attempt
    _summary_shaped _write_item_embedding annotations asynccontextmanager bulk_pace_delay
    count_stale_items datetime dedup_document embedder_signature floats_to_bytes
    get_embed_rate_limiter handoff_gate_context is_sensitive_path logger rebuild_embeddings
    redact_credentials redact_exfiltration_urls run_to_completion sel start_rebuild_job
    timedelta uuid4
    """.split())

#: Names the test trees are known to patch through the facade, one of each seam: the
#: explicit-import budget reader and the embed limiter singleton, the size ceiling, the
#: chunker and its dispatcher, the sensitive-path refusal and its audit sink -- all read
#: by the facade's own pipeline -- and the pace derivation the re-embed sweep reads in its
#: owner. The scan below must find all of them.
_KNOWN_SEAMS = frozenset(
    {
        "HeadingAwareChunker",
        "_embed_rate_limiter",
        "_import_chunk_budget",
        "_max_ingest_file_mb",
        "_run_chunker",
        "bulk_pace_delay",
        "is_sensitive_path",
        "sel",
    }
)


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _source(module_name: str) -> str:
    path = importlib.import_module(module_name).__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


def _family() -> list:
    return [facade, *(importlib.import_module(name) for name in _index().owners)]


# --------------------------------------------------------------------------- #
# The mirror and the names it serves
# --------------------------------------------------------------------------- #


def test_the_facade_serves_a_mirror_over_every_sibling_owner() -> None:
    assert isinstance(facade, _facade_mirror.MirroredModule)
    index = _index()
    on_disk = sorted(f"kiro_crew.knowledge.{path.stem}" for path in _PKG_DIR.glob("ingestion_*.py"))
    assert list(index.owners) == list(_OWNERS)
    assert sorted(index.owners) == on_disk
    assert index.excluded == ()
    assert index.divergent() == ()


def test_every_base_module_name_resolves_through_the_facade() -> None:
    index = _index()
    missing = sorted(name for name in _BASE_MODULE_NAMES if not hasattr(facade, name))
    assert missing == []
    for name in sorted(_BASE_MODULE_NAMES):
        assert all(
            getattr(importlib.import_module(holder), name) is getattr(facade, name)
            for holder in index.holders.get(name, ())
        ), name


def test_the_facade_keeps_the_default_read_path_and_forwards_no_table() -> None:
    """``ingestion.py`` was a plain module with no forwarding table, so the facade forwards
    no legacy table and keeps the default ``forward_reads``."""
    index = _index()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports)


def test_the_pipeline_and_its_helpers_stay_defined_in_the_facade() -> None:
    """The split moved the budget and the re-embed sweep; the pipeline, its gate, the
    embed limiter and the worker hop are still the facade's own code."""
    for name in (
        "IngestionPipeline",
        "EmbedRateLimiter",
        "FileTooLargeError",
        "get_embed_rate_limiter",
        "handoff_gate_context",
        "run_to_completion",
        "_redact",
        "_redact_for_ingest",
    ):
        assert name in vars(facade), name
        assert getattr(facade, name).__module__ == _FACADE, name


# --------------------------------------------------------------------------- #
# Patch reach
# --------------------------------------------------------------------------- #


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_tree = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_tree) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker.

    This file is left out: it patches through the facade itself, so counting it would let
    the known-seam check below pass on its own patches.
    """
    found: set[str] = set()
    for path in _test_tree_files():
        if path.resolve() == _THIS_FILE:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "ingestion" not in text:
            continue
        names = facade_mirror_scan.patched_names(text, {_FACADE: _FACADE})
        found |= names.get(_FACADE, set())
    return frozenset(found)


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    """The reach the unsplit module had: a write to ``ingestion.<name>`` was what every
    reader of that name read. Each module of the family that binds a patched name must
    see the patched object, and the undo must restore all of them."""
    family = _family()
    missed: dict[str, list[str]] = {}
    checked = 0
    for name in sorted(_corpus_patched_names()):
        if not hasattr(facade, name):
            continue
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        originals = {module.__name__: vars(module)[name] for module in holders}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(facade, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(facade, name) is not sentinel:
                stale.append(_FACADE)
        restored = {m.__name__: vars(m).get(name) for m in holders}
        if stale or any(restored[k] is not v for k, v in originals.items()):
            missed[name] = stale or ["undo"]
        checked += 1
    assert missed == {}
    assert checked >= len(_KNOWN_SEAMS)
    assert _index().divergent() == ()


class _Embedder:
    def embed_for_item(self, title, summary, content, priority):
        return [float(priority)]


def test_a_facade_patch_of_the_pace_derivation_reaches_the_rebuild_owner() -> None:
    """``_embed_row_paced`` reads ``bulk_pace_delay`` in ``ingestion_rebuild``; the bulk
    pacing tests patch it through the facade, so that patch is what the sweep reads."""
    rebuild = importlib.import_module("kiro_crew.knowledge.ingestion_rebuild")
    assert "bulk_pace_delay" not in vars(facade)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(facade, "bulk_pace_delay", lambda elapsed: 0.25)
        assert vars(rebuild)["bulk_pace_delay"] is facade.bulk_pace_delay
        assert facade._embed_row_paced(_Embedder(), "t", "s", "c", 3, True) == ([3.0], 0.25)
        assert facade._embed_row_paced(_Embedder(), "t", "s", "c", 3, False) == ([3.0], 0.0)
    assert (
        vars(rebuild)["bulk_pace_delay"]
        is importlib.import_module("kiro_crew.embeddings").bulk_pace_delay
    )


# --------------------------------------------------------------------------- #
# Layering and logging
# --------------------------------------------------------------------------- #


def _resolved_imports(tree: ast.Module, module: str) -> set[str]:
    """Dotted modules the ``from`` imports of *module* name, relative spellings resolved."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                package = module.split(".")[: -node.level]
                base = ".".join([*package, *([node.module] if node.module else [])])
            out.add(base)
            out |= {f"{base}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            out |= {alias.name for alias in node.names}
    return out


def test_the_owners_load_lowest_layer_first_and_never_import_the_facade() -> None:
    owners = list(_index().owners)
    late: dict[str, list[str]] = {}
    for position, owner in enumerate(owners):
        imported = _resolved_imports(ast.parse(_source(owner)), owner)
        later = sorted(m for m in imported if m in owners and owners.index(m) >= position)
        if _FACADE in imported:
            later.append(_FACADE)
        if later:
            late[owner] = later
    assert late == {}


def test_the_layer_scan_resolves_a_relative_import_of_the_facade() -> None:
    owner = "kiro_crew.knowledge.ingestion_rebuild"
    planted = ast.parse("from .ingestion import IngestionPipeline\n")
    assert _FACADE in _resolved_imports(planted, owner)
    planted = ast.parse("from . import ingestion\n")
    assert _FACADE in _resolved_imports(planted, owner)


def test_the_family_logs_as_the_facade() -> None:
    """``caplog`` filters keyed on ``kiro_crew.knowledge.ingestion`` keep seeing the family."""
    shared = logging.getLogger(_FACADE)
    assert facade.logger is shared
    stray = {
        f"{owner}.{name}"
        for owner in _index().owners
        for name, value in vars(importlib.import_module(owner)).items()
        if isinstance(value, logging.Logger) and value is not shared
    }
    assert stray == set()


def test_the_refusal_exception_keeps_the_facade_module() -> None:
    """A traceback or a log line renders a raised class as ``module.qualname``, so the
    exception class an owner defines reports ``kiro_crew.knowledge.ingestion``, as it did
    before it moved."""
    found = {
        name: value
        for owner in _index().owners
        for name, value in vars(importlib.import_module(owner)).items()
        if isinstance(value, type)
        and issubclass(value, BaseException)
        and name in _facade_mirror._defined_names(ast.parse(_source(owner)))
    }
    assert {"ImportChunkBudgetError"} <= set(found)
    assert {name: cls.__module__ for name, cls in found.items()} == dict.fromkeys(found, _FACADE)
    for name, cls in found.items():
        assert cls.__qualname__ == name
        assert getattr(importlib.import_module(cls.__module__), cls.__qualname__) is cls


# --------------------------------------------------------------------------- #
# Type checkers
# --------------------------------------------------------------------------- #


def _served_imports(index: _facade_mirror.MirrorIndex) -> set[str]:
    defined = set().union(
        *(_facade_mirror._defined_names(ast.parse(_source(o))) for o in index.owners)
    )
    family = {*index.owners, _FACADE}
    return {
        name
        for name in index.exports
        if name not in defined
        and name != "TYPE_CHECKING"
        and getattr(getattr(facade, name), "__name__", None) not in family
    }


def _typing_names(source: str, owners: Iterable[str]) -> set[str]:
    """Names the ``TYPE_CHECKING`` groups of *source* import from outside *owners*."""
    named: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom) and sub.module not in owners:
                    named |= {alias.asname or alias.name for alias in sub.names}
                elif isinstance(sub, ast.Import):
                    named |= {alias.asname or alias.name.split(".")[0] for alias in sub.names}
    return named


def _owner_typing_names(source: str, owners: Iterable[str]) -> set[str]:
    """Names the ``TYPE_CHECKING`` groups of *source* import from the *owners*."""
    named: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for sub in node.body:
                if isinstance(sub, ast.ImportFrom) and sub.module in owners:
                    named |= {alias.asname or alias.name for alias in sub.names}
    return named


def test_every_served_import_is_named_for_type_checkers() -> None:
    """A name the family imports and the facade serves is in a ``TYPE_CHECKING`` group.

    The facade binds none of them, so this group is what keeps
    ``from kiro_crew.knowledge.ingestion import bulk_pace_delay`` visible to mypy.
    """
    index = _index()
    served = _served_imports(index)
    named = _typing_names(_source(_FACADE), index.owners)
    assert "bulk_pace_delay" in served, "the census found nothing to check"
    assert sorted(served - named) == []
    assert sorted(named - served) == []


def test_every_served_owner_name_is_named_for_type_checkers() -> None:
    """Every owner-defined name the facade serves is in its owner's ``TYPE_CHECKING``
    import, so ``from kiro_crew.knowledge.ingestion import rebuild_embeddings`` type-checks."""
    index = _index()
    defined = set().union(
        *(_facade_mirror._defined_names(ast.parse(_source(o))) for o in index.owners)
    )
    served = {name for name in index.exports if name in defined}
    named = _owner_typing_names(_source(_FACADE), index.owners)
    assert "rebuild_embeddings" in served, "the census found nothing to check"
    assert sorted(served - named) == []
    assert sorted(named - served) == []


def test_the_type_checker_census_finds_a_dropped_name() -> None:
    index = _index()
    source = _source(_FACADE)
    assert "bulk_pace_delay" in _typing_names(source, index.owners)
    pruned = source.replace("        bulk_pace_delay,\n", "", 1)
    assert pruned != source
    assert "bulk_pace_delay" not in _typing_names(pruned, index.owners)
    assert "rebuild_embeddings" in _owner_typing_names(source, index.owners)
    pruned = source.replace("        rebuild_embeddings,\n", "", 1)
    assert pruned != source
    assert "rebuild_embeddings" not in _owner_typing_names(pruned, index.owners)


# --------------------------------------------------------------------------- #
# Who imports an owner
# --------------------------------------------------------------------------- #


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name.startswith(_OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    family = {f"knowledge/{name.rpartition('.')[2]}.py" for name in (_FACADE, *_index().owners)}
    sources = source_corpus.iter_candidate_sources(require_all=("ingestion_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(_SRC)), _SRC
    )
    assert "knowledge/ingestion.py" in importers  # the facade imports the owners it serves
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    sources = {
        root / "pkg" / "caller.py": "from kiro_crew.knowledge.ingestion_rebuild import x\n",
        root / "other.py": "from kiro_crew.knowledge import ingestion_budget as low\n",
        root / "knowledge" / "relative.py": "from .ingestion_budget import ImportChunkBudget\n",
        root / "knowledge" / "deep" / "up.py": "from .. import ingestion_rebuild\n",
        root / "lazy.py": "def f():\n    import kiro_crew.knowledge.ingestion_rebuild\n",
        root / "facade_user.py": "from kiro_crew.knowledge.ingestion import rebuild_embeddings\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "knowledge/relative.py",
        "knowledge/deep/up.py",
        "lazy.py",
    }


# --------------------------------------------------------------------------- #
# Corpus-gate selection
# --------------------------------------------------------------------------- #


def test_the_test_tree_walk_reaches_this_file() -> None:
    walked = {path.resolve() for path in _test_tree_files()}
    assert _THIS_FILE in walked
    assert source_corpus.repo_root() / "test" / "test_knowledge_rebuild_bulk_pacing.py" in walked
