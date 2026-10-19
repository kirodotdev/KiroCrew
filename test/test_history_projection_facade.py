"""``kiro_crew.history_projection`` stays the import path and the patch surface of its family.

The module keeps ``SessionMetadataProjection`` and is a ``kiro_crew._facade_mirror`` facade
over two owners: ``history_projection_contract`` (the family's module-level vocabulary: the
line states, the page and revision types, the read tuning and the logger) and
``history_projection_reads`` (``TranscriptReadProjection``). ``test_facade_mirror_guards``
holds every mirrored facade to the shared contract; this file pins what is particular to
this one: its owners, the exception that keeps the facade as its module, the one logger,
a patch through the facade reaching each reader of the names the test trees patch, and
that nothing outside the family imports an owner.
"""

from __future__ import annotations

import ast
import importlib
import logging
import secrets
from collections.abc import Callable
from pathlib import Path

import pytest
import source_corpus

from kiro_crew import _facade_mirror
from kiro_crew import history_projection as facade

_FACADE = "kiro_crew.history_projection"
_CONTRACT = "kiro_crew.history_projection_contract"
_READS = "kiro_crew.history_projection_reads"
_OWNERS = (_CONTRACT, _READS)


def _index() -> _facade_mirror.MirrorIndex:
    return vars(facade)[_facade_mirror.INDEX_ATTRIBUTE]


def _source(dotted: str) -> str:
    path = importlib.import_module(dotted).__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


def test_the_facade_mirrors_its_two_owners_lowest_first() -> None:
    assert type(facade) is _facade_mirror.MirroredModule
    assert facade._OWNERS == _OWNERS
    assert _index().owners == _OWNERS
    assert _index().excluded == ()


def test_the_facade_serves_only_names_its_owners_define() -> None:
    """Every name it imported from elsewhere stays bound on the facade itself, so a type
    checker reads each through the facade path with no second ``TYPE_CHECKING`` group."""
    defined = {owner: _facade_mirror._defined_names(ast.parse(_source(owner))) for owner in _OWNERS}
    exports = dict(_index().exports)
    assert exports.get("TranscriptReadProjection") == _READS
    assert {name: owner for name, owner in exports.items() if name not in defined[owner]} == {}


def test_the_revision_exception_is_named_through_the_facade() -> None:
    """It reaches logged tracebacks, which render ``module.qualname``."""
    cls = facade.TranscriptRevisionChanged
    assert issubclass(cls, RuntimeError)
    assert f"{cls.__module__}.{cls.__qualname__}" == f"{_FACADE}.TranscriptRevisionChanged"
    for owner in _OWNERS:
        assert vars(importlib.import_module(owner))["TranscriptRevisionChanged"] is cls


def _logs_under_own_name(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "getLogger"
            and any(isinstance(arg, ast.Name) and arg.id == "__name__" for arg in node.args)
        ):
            return True
    return False


def test_the_own_name_logger_detector_answers_both_ways() -> None:
    assert _logs_under_own_name("import logging\nlog = logging.getLogger(__name__)\n")
    assert not _logs_under_own_name('import logging\nlog = logging.getLogger("kiro_crew.x")\n')


def test_the_family_logs_on_the_history_logger() -> None:
    logger = logging.getLogger("kiro_crew.history")
    for dotted in (_FACADE, *_OWNERS):
        assert vars(importlib.import_module(dotted))["_HISTORY_LOGGER"] is logger, dotted
        assert not _logs_under_own_name(_source(dotted)), dotted


def _contract_fn(name: str) -> Callable[[], object]:
    return lambda: getattr(importlib.import_module(_CONTRACT), name)


def _read_method(name: str) -> Callable[[], object]:
    return lambda: getattr(facade.TranscriptReadProjection, name)


def _metadata_method(name: str) -> Callable[[], object]:
    return lambda: getattr(facade.SessionMetadataProjection, name)


#: Each name the test trees patch through this facade -> the functions reading it, and the
#: module whose namespace those functions read it from.
_PATCHED: dict[str, tuple[str, tuple[Callable[[], object], ...]]] = {
    "_TRANSCRIPT_READ_CHUNK": (_CONTRACT, (_contract_fn("_iter_splitlines"),)),
    "_TRANSCRIPT_PAGE_INDEX_STRIDE": (_READS, (_read_method("_row_index"),)),
    "_TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS": (_READS, (_read_method("_row_index"),)),
    "count_messages": (_READS, (_read_method("_count_messages"),)),
    "strict_raw_records_with_offsets": (
        _READS,
        (_read_method("_row_index"), _read_method("_read_message_range")),
    ),
    "open": (
        _READS,
        (
            _read_method("recent_from_source"),
            _read_method("read_file_change_messages"),
            _read_method("_row_index"),
            _read_method("_read_message_range"),
            _read_method("_read_messages_locked"),
            _read_method("_read_tail_messages"),
            _read_method("_tail_walk"),
            _read_method("_read_metadata_state"),
        ),
    ),
    "replace_with_retry": (_FACADE, (_metadata_method("_update_metadata_locked"),)),
    "stage_attachments_removal": (_FACADE, (_metadata_method("delete_session"),)),
}


@pytest.mark.parametrize("name", sorted(_PATCHED))
def test_a_patch_through_the_facade_reaches_every_reader(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, readers = _PATCHED[name]
    namespace = vars(importlib.import_module(home))
    functions = [reader() for reader in readers]
    assert all(getattr(fn, "__globals__", None) is namespace for fn in functions), name
    sentinel = object()
    monkeypatch.setattr(facade, name, sentinel, raising=False)
    assert getattr(facade, name) is sentinel
    assert [fn.__qualname__ for fn in functions if fn.__globals__[name] is not sentinel] == []


def test_the_secrets_seam_is_the_module_delete_session_reads() -> None:
    reader = facade.SessionMetadataProjection.delete_session
    assert facade.secrets is secrets
    assert reader.__globals__["secrets"] is secrets


def _owner_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in _OWNERS:
                found.add(node.module)
            elif node.module == "kiro_crew":
                found |= {f"kiro_crew.{a.name}" for a in node.names} & set(_OWNERS)
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names} & set(_OWNERS)
    return found


def test_the_owner_import_scan_answers_both_ways() -> None:
    source = (
        "from kiro_crew.history_projection_reads import TranscriptReadProjection\n"
        "from kiro_crew import history_projection_contract, platform_compat\n"
        "import kiro_crew.history_projection\n"
    )
    assert _owner_imports(ast.parse(source)) == set(_OWNERS)


def test_only_the_family_imports_an_owner() -> None:
    """Callers keep importing ``kiro_crew.history_projection``; the owners are its own."""
    family = {"kiro_crew/history_projection.py", "kiro_crew/history_projection_reads.py"}
    root = source_corpus.src_root().parent
    offenders = sorted(
        path.relative_to(root).as_posix()
        for path, _text, tree in source_corpus.parsed_candidates(
            require_any=("history_projection_contract", "history_projection_reads")
        )
        if path.relative_to(root).as_posix() not in family and _owner_imports(tree)
    )
    assert offenders == []
